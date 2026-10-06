"""The pipeline: the single path that runs reconciliation for one invoice or
for the whole set.

This module wires ``reconciliation/matcher.py`` and
``reconciliation/rules.py`` together with ``db/repository.py``. It contains
no reconciliation logic of its own — no status decisions, no money
arithmetic, no duplicate-detection rule beyond calling the matcher. One
pipeline, two entry points: the headless batch command and the FastAPI web
view (including a reviewer correction, in a later slice) both call
``recalculate_one`` for a single invoice, or ``run_batch`` for all of them.
Neither grows its own copy of the reconciliation path.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from invoice_reconciliation.db import repository
from invoice_reconciliation.money import MoneyFormatError
from invoice_reconciliation.reconciliation import matcher, rules

__all__ = ["InvoiceOutcome", "recalculate_one", "run_batch"]


@dataclass(frozen=True, slots=True)
class InvoiceOutcome:
    """What happened for one invoice during a batch or single recompute."""

    invoice_id: int
    file_id: str
    result: rules.ReconciliationResult


def _now_iso() -> str:
    """Current UTC timestamp in ISO-8601, for ``computed_at``."""
    return datetime.now(timezone.utc).isoformat()


def recalculate_one(conn: sqlite3.Connection, invoice_id: int) -> rules.ReconciliationResult:
    """Recompute and persist the reconciliation result for one invoice.

    This is the function a web correction calls (Slice 11) and the function
    the batch loop calls per invoice — the one path both entry points share.

    Steps:

    1. Read the invoice row (for ``received_at``) and its current field
       values.
    2. Find the matching purchase order and receipt
       (``matcher.find_purchase_order_and_receipt``).
    3. Determine whether an earlier duplicate exists
       (``matcher.find_earlier_duplicate``), excluding this invoice itself.
    4. Call ``rules.reconcile`` — the pure decision function. If it raises
       ``MoneyFormatError`` (malformed money text already stored for this
       invoice), the result is recorded as ``failed`` with no amounts
       instead of propagating — one bad invoice must not abort the batch.
    5. Persist the result into ``reconciliation_results``, overwriting any
       existing row for this invoice.

    Returns the ``ReconciliationResult`` that was persisted.
    """
    current_fields = repository.get_current_fields(conn, invoice_id=invoice_id)

    invoice_row = conn.execute(
        """
        SELECT invoice_id, received_at
        FROM invoices
        WHERE invoice_id = ?
        """,
        (invoice_id,),
    ).fetchone()
    received_at = invoice_row["received_at"]

    match = matcher.find_purchase_order_and_receipt(conn, current_fields=current_fields)

    is_duplicate = False
    supplier_id = current_fields.get("supplier_id")
    invoice_number = current_fields.get("invoice_number")
    if supplier_id is not None and invoice_number is not None:
        earlier = matcher.find_earlier_duplicate(
            conn,
            supplier_id=supplier_id,
            invoice_number=invoice_number,
            received_at=received_at,
            exclude_invoice_id=invoice_id,
        )
        is_duplicate = earlier is not None

    try:
        result = rules.reconcile(
            current_fields=current_fields,
            match=match,
            is_duplicate=is_duplicate,
        )
    except MoneyFormatError:
        # Malformed money text already stored in the database (not a
        # programming bug) must not abort the batch. Isolate this one
        # invoice as 'failed', with no amounts, and let every other
        # invoice keep processing. ``rules.reconcile`` itself stays pure
        # and keeps raising on bad input — this is the one place that
        # catches it.
        result = rules.ReconciliationResult(status="failed")

    repository.upsert_reconciliation_result(
        conn,
        invoice_id=invoice_id,
        status=result.status,
        expected_cents=result.expected_cents,
        billed_cents=result.billed_cents,
        difference_cents=result.difference_cents,
        count_as_payable=result.count_as_payable,
        matched_po_id=result.matched_po_id,
        matched_receipt_id=result.matched_receipt_id,
        computed_at=_now_iso(),
    )

    return result


def run_batch(conn: sqlite3.Connection) -> list[InvoiceOutcome]:
    """Recompute reconciliation for every invoice, in a stable order.

    Invoices are processed ordered by ``received_at`` (ties broken by
    ``invoice_id``) so duplicate detection behaves identically on every run:
    the first copy by ``received_at`` is evaluated before any later copy
    sharing its ``(supplier_id, invoice_number)``, and so stays payable.

    Returns one ``InvoiceOutcome`` per invoice, in the order processed.
    """
    rows = conn.execute(
        """
        SELECT invoice_id, file_id
        FROM invoices
        ORDER BY received_at ASC, invoice_id ASC
        """
    ).fetchall()

    outcomes: list[InvoiceOutcome] = []
    for row in rows:
        result = recalculate_one(conn, row["invoice_id"])
        outcomes.append(
            InvoiceOutcome(
                invoice_id=row["invoice_id"],
                file_id=row["file_id"],
                result=result,
            )
        )
    return outcomes
