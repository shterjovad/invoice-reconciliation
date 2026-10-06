"""Explicit, parameterised SQL for purchase orders, receipts, invoices and
extracted fields.

No ORM, no query builder, no string interpolation of values into SQL —
every value crosses the boundary through a ``?`` placeholder.

Return type convention: functions that return rows return ``sqlite3.Row``
objects (by way of the ``row_factory`` set in ``connection.py``), so callers
read columns by name (``row["po_id"]``). The one exception is
``get_current_fields``, which the task asks for as a ``dict`` because
callers (the matcher, the rules engine) want field values by name without
re-reading column metadata.

The match join is always ``(supplier_id, po_id)`` together. Never a single
column, and never amount — two seeded purchase orders are identical apart
from their ID.
"""

from __future__ import annotations

import sqlite3

# ---------------------------------------------------------------------------
# purchase_orders
# ---------------------------------------------------------------------------


def insert_purchase_order(
    conn: sqlite3.Connection,
    *,
    po_id: str,
    supplier_id: str,
    sku: str,
    quantity: int,
    unit_cents: int,
) -> None:
    """Insert one purchase order row."""
    conn.execute(
        """
        INSERT INTO purchase_orders (po_id, supplier_id, sku, quantity, unit_cents)
        VALUES (?, ?, ?, ?, ?)
        """,
        (po_id, supplier_id, sku, quantity, unit_cents),
    )


def get_purchase_order(
    conn: sqlite3.Connection, *, supplier_id: str, po_id: str
) -> sqlite3.Row | None:
    """Look up the purchase order matching both ``supplier_id`` and ``po_id``.

    This is the mandated match join. It never matches on ``po_id`` alone,
    and never on amount or SKU: two seeded purchase orders (``PO-1`` and
    ``PO-2``) share the same supplier, SKU, quantity and unit price, and
    differ only by ``po_id``.
    """
    cursor = conn.execute(
        """
        SELECT po_id, supplier_id, sku, quantity, unit_cents
        FROM purchase_orders
        WHERE supplier_id = ? AND po_id = ?
        """,
        (supplier_id, po_id),
    )
    return cursor.fetchone()


def list_purchase_orders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all purchase orders."""
    cursor = conn.execute(
        "SELECT po_id, supplier_id, sku, quantity, unit_cents FROM purchase_orders"
    )
    return cursor.fetchall()


# ---------------------------------------------------------------------------
# receipts
# ---------------------------------------------------------------------------


def insert_receipt(
    conn: sqlite3.Connection,
    *,
    receipt_id: str,
    po_id: str,
    sku: str,
    quantity: int,
) -> None:
    """Insert one receipt row."""
    conn.execute(
        """
        INSERT INTO receipts (receipt_id, po_id, sku, quantity)
        VALUES (?, ?, ?, ?)
        """,
        (receipt_id, po_id, sku, quantity),
    )


def get_receipt_for_po(conn: sqlite3.Connection, *, po_id: str) -> sqlite3.Row | None:
    """Find the receipt recorded against a given purchase order."""
    cursor = conn.execute(
        "SELECT receipt_id, po_id, sku, quantity FROM receipts WHERE po_id = ?",
        (po_id,),
    )
    return cursor.fetchone()


def list_receipts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all receipts."""
    cursor = conn.execute("SELECT receipt_id, po_id, sku, quantity FROM receipts")
    return cursor.fetchall()


# ---------------------------------------------------------------------------
# invoices
# ---------------------------------------------------------------------------


def insert_invoice(
    conn: sqlite3.Connection,
    *,
    file_id: str,
    image_path: str,
    layout: str,
    received_at: str,
    extraction_source: str,
) -> int:
    """Insert one invoice row and return its generated ``invoice_id``."""
    cursor = conn.execute(
        """
        INSERT INTO invoices (file_id, image_path, layout, received_at, extraction_source)
        VALUES (?, ?, ?, ?, ?)
        """,
        (file_id, image_path, layout, received_at, extraction_source),
    )
    return int(cursor.lastrowid)


def get_invoice_by_file_id(
    conn: sqlite3.Connection, *, file_id: str
) -> sqlite3.Row | None:
    """Look up an invoice by its unique ``file_id``."""
    cursor = conn.execute(
        """
        SELECT invoice_id, file_id, image_path, layout, received_at, extraction_source
        FROM invoices
        WHERE file_id = ?
        """,
        (file_id,),
    )
    return cursor.fetchone()


def list_invoices(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all invoices."""
    cursor = conn.execute(
        "SELECT invoice_id, file_id, image_path, layout, received_at, extraction_source "
        "FROM invoices"
    )
    return cursor.fetchall()


# ---------------------------------------------------------------------------
# extracted_fields
# ---------------------------------------------------------------------------


def insert_extracted_field(
    conn: sqlite3.Connection,
    *,
    invoice_id: int,
    field_name: str,
    original_value: str | None,
    current_value: str | None,
) -> None:
    """Write one extracted field row.

    ``original_value`` is written once here and is never overwritten by
    this layer afterwards. ``current_value`` starts equal to it and is the
    column a correction later updates. Both are nullable: ``po_id`` can
    legitimately be absent from an invoice (the ``missing-reference``
    fixture).
    """
    conn.execute(
        """
        INSERT INTO extracted_fields (invoice_id, field_name, original_value, current_value)
        VALUES (?, ?, ?, ?)
        """,
        (invoice_id, field_name, original_value, current_value),
    )


def update_current_field_value(
    conn: sqlite3.Connection,
    *,
    invoice_id: int,
    field_name: str,
    current_value: str | None,
) -> None:
    """Update only ``current_value`` for one field. ``original_value`` is untouched.

    This is the write a reviewer correction makes. The audit row recording
    the before/after values belongs to the ``corrections`` table, not this
    layer's concern in this task.
    """
    conn.execute(
        """
        UPDATE extracted_fields
        SET current_value = ?
        WHERE invoice_id = ? AND field_name = ?
        """,
        (current_value, invoice_id, field_name),
    )


def get_current_fields(conn: sqlite3.Connection, *, invoice_id: int) -> dict[str, str | None]:
    """Return the current value of every extracted field for an invoice, as a dict.

    Keys are field names, values are the current (possibly corrected)
    text, or ``None`` where the model returned no value (e.g. ``po_id`` on
    the ``missing-reference`` fixture). This is the flat read the rules
    engine and matcher use; it never falls back to ``original_value``.
    """
    cursor = conn.execute(
        """
        SELECT field_name, current_value
        FROM extracted_fields
        WHERE invoice_id = ?
        """,
        (invoice_id,),
    )
    return {row["field_name"]: row["current_value"] for row in cursor.fetchall()}


def get_original_fields(conn: sqlite3.Connection, *, invoice_id: int) -> dict[str, str | None]:
    """Return the original (model-extracted) value of every field for an invoice, as a dict.

    Kept alongside ``get_current_fields`` so both the extracted and the
    corrected value stay recoverable, per the domain requirement.
    """
    cursor = conn.execute(
        """
        SELECT field_name, original_value
        FROM extracted_fields
        WHERE invoice_id = ?
        """,
        (invoice_id,),
    )
    return {row["field_name"]: row["original_value"] for row in cursor.fetchall()}


# ---------------------------------------------------------------------------
# reconciliation_results
# ---------------------------------------------------------------------------


def upsert_reconciliation_result(
    conn: sqlite3.Connection,
    *,
    invoice_id: int,
    status: str,
    expected_cents: int | None,
    billed_cents: int | None,
    difference_cents: int | None,
    count_as_payable: bool | None,
    matched_po_id: str | None,
    matched_receipt_id: str | None,
    computed_at: str,
) -> None:
    """Write (or overwrite) the one reconciliation result row for an invoice.

    Results are overwritten on each recompute, never appended: a reviewer
    correction that triggers ``recalculate_one`` must replace the previous
    row for that ``invoice_id`` rather than adding a second one. ``INSERT OR
    REPLACE`` keyed on the ``invoice_id`` primary key gives exactly that
    behaviour in one statement.

    ``count_as_payable`` is stored as ``0``/``1``/``NULL`` — sqlite3 has no
    native boolean type, and the schema declares the column ``INTEGER``.
    """
    stored_count_as_payable = (
        None if count_as_payable is None else int(count_as_payable)
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO reconciliation_results (
            invoice_id, status, expected_cents, billed_cents,
            difference_cents, count_as_payable, matched_po_id,
            matched_receipt_id, computed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            invoice_id,
            status,
            expected_cents,
            billed_cents,
            difference_cents,
            stored_count_as_payable,
            matched_po_id,
            matched_receipt_id,
            computed_at,
        ),
    )


def get_reconciliation_result(
    conn: sqlite3.Connection, *, invoice_id: int
) -> sqlite3.Row | None:
    """Look up the current reconciliation result row for an invoice, if any."""
    cursor = conn.execute(
        """
        SELECT invoice_id, status, expected_cents, billed_cents,
               difference_cents, count_as_payable, matched_po_id,
               matched_receipt_id, computed_at
        FROM reconciliation_results
        WHERE invoice_id = ?
        """,
        (invoice_id,),
    )
    return cursor.fetchone()
