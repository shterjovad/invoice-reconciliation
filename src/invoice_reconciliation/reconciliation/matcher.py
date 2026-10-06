"""Lookups for reconciliation: matching an invoice to its purchase order and
receipt, and finding an earlier duplicate.

This module performs no classification and no money arithmetic — that is
``reconciliation/rules.py``. It only answers two questions:

1. Given an invoice's *current* field values, which purchase order (if any)
   and which receipt (if any) does it refer to?
2. Given a supplier and invoice number, is there an earlier invoice (by
   ``received_at``) that shares both?

The match join is always ``(supplier_id, po_id)`` together, read from
``extracted_fields.current_value`` via ``repository.get_current_fields`` /
a direct join through ``extracted_fields``. Never a single column, and
never amount, SKU, or quantity — two seeded purchase orders (``PO-1`` and
``PO-2``) are identical apart from their ID, so any fallback to matching on
amount would silently pick the wrong one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from invoice_reconciliation.db import repository

__all__ = ["MatchResult", "find_purchase_order_and_receipt", "find_earlier_duplicate"]


@dataclass(frozen=True, slots=True)
class MatchResult:
    """The outcome of matching an invoice's current fields to reference data.

    ``purchase_order`` and ``receipt`` are ``sqlite3.Row`` objects (or
    ``None``) so the caller can read provenance columns by name
    (``purchase_order["po_id"]``, ``receipt["receipt_id"]``) without this
    module inventing its own shape for that data.
    """

    purchase_order: sqlite3.Row | None
    receipt: sqlite3.Row | None

    @property
    def is_matched(self) -> bool:
        """True only when a purchase order was found for this invoice."""
        return self.purchase_order is not None


_NO_MATCH = MatchResult(purchase_order=None, receipt=None)


def find_purchase_order_and_receipt(
    conn: sqlite3.Connection, *, current_fields: dict[str, str | None]
) -> MatchResult:
    """Match an invoice to its purchase order and receipt.

    ``current_fields`` is the dict returned by
    ``repository.get_current_fields`` — the *current* (possibly corrected)
    value of every extracted field, keyed by field name. This function reads
    ``supplier_id`` and ``po_id`` from it, never from ``original_value``, so
    a reviewer's correction changes what the matcher sees.

    Matching rules:

    - A null (missing) ``po_id`` matches nothing. Returns "no match" rather
      than guessing — inventing a reference would destroy the
      ``unresolved`` classification for the ``missing-reference`` fixture.
    - The lookup joins on ``(supplier_id, po_id)`` together, via
      ``repository.get_purchase_order``. A ``po_id`` that exists but under a
      different supplier is a conflicting reference, not a match: the query
      requires both columns to agree, so it returns no row rather than the
      purchase order under the wrong supplier.
    - Never falls back to matching on amount, SKU, or quantity — two seeded
      purchase orders (``PO-1``, ``PO-2``) are identical apart from their ID.

    Returns a ``MatchResult``. ``receipt`` is only populated when a purchase
    order was found; it is then looked up by that purchase order's own
    ``po_id`` (not the invoice's, though they are the same value once
    matched) via ``repository.get_receipt_for_po``.
    """
    supplier_id = current_fields.get("supplier_id")
    po_id = current_fields.get("po_id")

    if po_id is None or supplier_id is None:
        return _NO_MATCH

    purchase_order = repository.get_purchase_order(
        conn, supplier_id=supplier_id, po_id=po_id
    )
    if purchase_order is None:
        return _NO_MATCH

    receipt = repository.get_receipt_for_po(conn, po_id=purchase_order["po_id"])
    return MatchResult(purchase_order=purchase_order, receipt=receipt)


def find_earlier_duplicate(
    conn: sqlite3.Connection,
    *,
    supplier_id: str,
    invoice_number: str,
    received_at: str,
    exclude_invoice_id: int | None = None,
) -> sqlite3.Row | None:
    """Find an earlier invoice sharing ``(supplier_id, invoice_number)``.

    "Earlier" means a strictly smaller ``received_at`` than the invoice
    being checked. When two invoices share the same ``received_at`` exactly,
    neither is earlier than the other under this rule — ``received_at`` is
    expected to disambiguate, and ties are not resolved here by any other
    column (e.g. ``invoice_id``). This is a recorded ambiguity: the task
    does not specify a tiebreaker.

    ``supplier_id`` and ``invoice_number`` live in ``extracted_fields`` (they
    are correctable values), so this query joins through that table rather
    than reading a column on ``invoices``. Both are read from
    ``current_value``, never ``original_value``.

    Ordered by ``received_at`` ascending so the result, if any, is
    deterministic. ``exclude_invoice_id`` lets a caller checking "is invoice
    X a duplicate" exclude X itself when its own ``received_at`` is passed
    in (otherwise an invoice could never be earlier than itself, so this is
    mostly a defensive no-op, but it keeps the query honest if the same
    ``received_at`` value is ever reused for the invoice under test).

    Returns the earliest matching invoice row (columns from ``invoices``),
    or ``None`` if there is no earlier copy.
    """
    query = """
        SELECT i.invoice_id, i.file_id, i.image_path, i.layout,
               i.received_at, i.extraction_source
        FROM invoices AS i
        JOIN extracted_fields AS supplier
          ON supplier.invoice_id = i.invoice_id
         AND supplier.field_name = 'supplier_id'
        JOIN extracted_fields AS inv_num
          ON inv_num.invoice_id = i.invoice_id
         AND inv_num.field_name = 'invoice_number'
        WHERE supplier.current_value = ?
          AND inv_num.current_value = ?
          AND i.received_at < ?
    """
    params: list[object] = [supplier_id, invoice_number, received_at]

    if exclude_invoice_id is not None:
        query += " AND i.invoice_id != ?"
        params.append(exclude_invoice_id)

    query += " ORDER BY i.received_at ASC LIMIT 1"

    cursor = conn.execute(query, params)
    return cursor.fetchone()
