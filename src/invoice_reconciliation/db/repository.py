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
    extraction_failed: bool = False,
) -> int:
    """Insert one invoice row and return its generated ``invoice_id``.

    ``extraction_failed`` records whether this invoice's extraction raised
    or produced no usable response (``False`` for the prepared-record seed
    path, and for a live/cache call that succeeded). ``pipeline.
    recalculate_one`` reads it back to classify the invoice ``failed``
    instead of ``unresolved`` — see the column's docstring in
    ``db/schema.py``.
    """
    cursor = conn.execute(
        """
        INSERT INTO invoices (
            file_id, image_path, layout, received_at, extraction_source,
            extraction_failed
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (file_id, image_path, layout, received_at, extraction_source, int(extraction_failed)),
    )
    return int(cursor.lastrowid)


def get_invoice_by_file_id(
    conn: sqlite3.Connection, *, file_id: str
) -> sqlite3.Row | None:
    """Look up an invoice by its unique ``file_id``."""
    cursor = conn.execute(
        """
        SELECT invoice_id, file_id, image_path, layout, received_at,
               extraction_source, extraction_failed, extraction_failure_reason
        FROM invoices
        WHERE file_id = ?
        """,
        (file_id,),
    )
    return cursor.fetchone()


def list_invoices(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all invoices."""
    cursor = conn.execute(
        "SELECT invoice_id, file_id, image_path, layout, received_at, "
        "extraction_source, extraction_failed, extraction_failure_reason FROM invoices"
    )
    return cursor.fetchall()


def mark_extraction_failed(
    conn: sqlite3.Connection, *, invoice_id: int, reason: str | None = None
) -> None:
    """Flag an invoice's extraction as failed, after its row already exists.

    ``ingest_invoices_via_extraction`` inserts the ``invoices`` row before
    it knows whether the image file exists or the model call will succeed
    (so a bad invoice still gets a row other tables can reference). This is
    the one call that records failure once it is known, rather than
    requiring the caller to know it up front.

    ``reason`` is the human-readable cause (e.g. the same text
    ``ExtractionIngestOutcome.error`` carries), persisted so the reviewer
    detail page can show *why* an invoice failed, not just that it did.
    Optional and defaults to ``None`` so existing callers that do not pass
    it keep working unchanged.
    """
    conn.execute(
        "UPDATE invoices SET extraction_failed = 1, extraction_failure_reason = ? "
        "WHERE invoice_id = ?",
        (reason, invoice_id),
    )


def get_invoice(conn: sqlite3.Connection, *, invoice_id: int) -> sqlite3.Row | None:
    """Look up an invoice by its ``invoice_id``, including ``extraction_failed``."""
    cursor = conn.execute(
        """
        SELECT invoice_id, file_id, image_path, layout, received_at,
               extraction_source, extraction_failed, extraction_failure_reason
        FROM invoices
        WHERE invoice_id = ?
        """,
        (invoice_id,),
    )
    return cursor.fetchone()


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
# corrections
# ---------------------------------------------------------------------------


def insert_correction(
    conn: sqlite3.Connection,
    *,
    invoice_id: int,
    field_name: str,
    value_before: str | None,
    value_after: str | None,
    changed_by: str,
    changed_at: str,
) -> None:
    """Append one row to the ``corrections`` audit trail.

    Append-only: this function never updates or deletes an existing row.
    The caller (the correction route) writes this row and the matching
    ``extracted_fields.current_value`` update inside the same transaction,
    so the two never diverge.
    """
    conn.execute(
        """
        INSERT INTO corrections (
            invoice_id, field_name, value_before, value_after,
            changed_by, changed_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (invoice_id, field_name, value_before, value_after, changed_by, changed_at),
    )


def list_corrections(conn: sqlite3.Connection, *, invoice_id: int) -> list[sqlite3.Row]:
    """Return every correction recorded for an invoice, oldest first."""
    cursor = conn.execute(
        """
        SELECT correction_id, invoice_id, field_name, value_before, value_after,
               changed_by, changed_at
        FROM corrections
        WHERE invoice_id = ?
        ORDER BY correction_id ASC
        """,
        (invoice_id,),
    )
    return cursor.fetchall()


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


def count_results_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    """Return the number of ``reconciliation_results`` rows per status.

    Keys are only the statuses actually present — an invoice with no
    result row yet (no entry at all) is not counted under any status. The
    summary route fills in zero for any of the five known statuses this
    dict omits, rather than this function inventing zero-rows itself.
    """
    cursor = conn.execute(
        "SELECT status, COUNT(*) AS n FROM reconciliation_results GROUP BY status"
    )
    return {row["status"]: row["n"] for row in cursor.fetchall()}


def get_recoverable_total_cents(conn: sqlite3.Connection) -> int:
    """The sum of positive differences on discrepant invoices only.

    ``SUM(difference_cents) WHERE status = 'discrepant' AND
    difference_cents > 0`` — domain.md: "Do not total duplicates or
    unresolved matches as recoverable amounts," and a negative difference
    (an underbill) is not owed back, so it is excluded by the ``> 0``
    condition rather than being netted against a genuine overcharge.
    ``SUM`` over zero matching rows returns SQL ``NULL``, read back here as
    ``0`` — the recoverable total is always a number, never ``None``.
    """
    cursor = conn.execute(
        """
        SELECT SUM(difference_cents) AS total
        FROM reconciliation_results
        WHERE status = 'discrepant' AND difference_cents > 0
        """
    )
    row = cursor.fetchone()
    total = row["total"]
    return int(total) if total is not None else 0


def list_discrepant_results(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return every discrepant invoice's figures, joined with its file_id
    and invoice_number, for the summary page's per-difference listing.

    ``invoice_number`` comes from ``extracted_fields`` (its current,
    possibly corrected, value) rather than from the invoice row itself,
    matching the same join the matcher and duplicate check use elsewhere.
    Ordered the same way the queue is (``received_at`` then ``invoice_id``)
    so the summary lists differences in a stable, predictable order.
    """
    cursor = conn.execute(
        """
        SELECT i.invoice_id, i.file_id,
               ef.current_value AS invoice_number,
               r.matched_po_id, r.expected_cents, r.billed_cents,
               r.difference_cents
        FROM reconciliation_results r
        JOIN invoices i ON i.invoice_id = r.invoice_id
        LEFT JOIN extracted_fields ef
            ON ef.invoice_id = i.invoice_id AND ef.field_name = 'invoice_number'
        WHERE r.status = 'discrepant'
        ORDER BY i.received_at ASC, i.invoice_id ASC
        """
    )
    return cursor.fetchall()


def list_invoices_with_results(
    conn: sqlite3.Connection, *, status: str | None = None
) -> list[sqlite3.Row]:
    """Return every invoice joined with its reconciliation result, for the queue.

    A ``LEFT JOIN`` so an invoice that has not yet been reconciled (no row
    in ``reconciliation_results`` yet) still appears, with every result
    column ``NULL``, rather than being silently dropped from the queue.

    ``status`` is an optional exact-match filter over
    ``reconciliation_results.status``. An unknown status value simply
    matches no row — the query returns an empty list, never an error — so
    the route never needs to validate it against the five known statuses
    before filtering.

    Ordered by ``received_at`` (ties broken by ``invoice_id``), the same
    stable order ``pipeline.run_batch`` processes invoices in, so the
    queue's row order matches the order a reviewer would see them appear
    in a batch report.
    """
    params: tuple[object, ...]
    if status is None:
        where_clause = ""
        params = ()
    else:
        where_clause = "WHERE r.status = ?"
        params = (status,)

    cursor = conn.execute(
        f"""
        SELECT i.invoice_id, i.file_id, i.image_path, i.layout,
               i.received_at, i.extraction_source, i.extraction_failed,
               i.extraction_failure_reason,
               r.status, r.expected_cents, r.billed_cents,
               r.difference_cents, r.count_as_payable, r.matched_po_id,
               r.matched_receipt_id, r.computed_at
        FROM invoices i
        LEFT JOIN reconciliation_results r ON r.invoice_id = i.invoice_id
        {where_clause}
        ORDER BY i.received_at ASC, i.invoice_id ASC
        """,
        params,
    )
    return cursor.fetchall()


# ---------------------------------------------------------------------------
# discrepancy_notes
# ---------------------------------------------------------------------------


def upsert_discrepancy_note(
    conn: sqlite3.Connection,
    *,
    invoice_id: int,
    drafted_text: str,
) -> None:
    """Write the drafted note for a discrepant invoice.

    Called once per recalculation that resolves an invoice to
    ``discrepant`` (the same point the pipeline persists a
    ``reconciliation_results`` row). ``INSERT OR REPLACE`` keyed on
    ``invoice_id``, matching ``upsert_reconciliation_result``'s pattern.

    ``current_text`` starts equal to ``drafted_text`` and
    ``is_reviewer_edited`` starts ``0`` (false) — the same "two columns
    side by side" shape ``extracted_fields`` uses for original/current, so
    the draft stays recoverable even after a reviewer edits the note.
    ``edited_at`` starts ``NULL`` since no edit has happened yet.

    Overwriting on every recompute means a redraft (e.g. after a field
    correction changes the figures) replaces the previous draft text. A
    reviewer's own edit to ``current_text`` is a separate write
    (``update_discrepancy_note_text``), made only by the note-save route,
    never by this function.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO discrepancy_notes (
            invoice_id, drafted_text, current_text, is_reviewer_edited, edited_at
        )
        VALUES (?, ?, ?, 0, NULL)
        """,
        (invoice_id, drafted_text, drafted_text),
    )


def get_discrepancy_note(
    conn: sqlite3.Connection, *, invoice_id: int
) -> sqlite3.Row | None:
    """Look up the discrepancy note row for an invoice, if any.

    ``None`` for any invoice that has never been ``discrepant`` (no row
    was ever drafted) — this is not an error, just the absence of a note.
    """
    cursor = conn.execute(
        """
        SELECT invoice_id, drafted_text, current_text, is_reviewer_edited, edited_at
        FROM discrepancy_notes
        WHERE invoice_id = ?
        """,
        (invoice_id,),
    )
    return cursor.fetchone()


def update_discrepancy_note_text(
    conn: sqlite3.Connection,
    *,
    invoice_id: int,
    current_text: str,
    edited_at: str,
) -> None:
    """Record a reviewer's edit to a note's text.

    Updates only ``current_text``, ``is_reviewer_edited`` (set to ``1``),
    and ``edited_at``. ``drafted_text`` is never touched here — exactly
    the same "the original stays readable beside the current" shape
    ``update_current_field_value`` keeps for ``extracted_fields``.

    This function performs no reconciliation recompute, and the route
    that calls it must not call one either: a note edit is a convenience
    for the reviewer, not a correction to a figure the rules engine reads.
    """
    conn.execute(
        """
        UPDATE discrepancy_notes
        SET current_text = ?, is_reviewer_edited = 1, edited_at = ?
        WHERE invoice_id = ?
        """,
        (current_text, edited_at, invoice_id),
    )
