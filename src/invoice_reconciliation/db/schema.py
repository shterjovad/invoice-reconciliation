"""DDL for the invoice reconciliation database.

Every money column is ``INTEGER`` holding whole USD cents. No money column
is ever ``REAL`` or TEXT-formatted currency.

The database is a rebuildable artifact, never a source of truth. The
fixtures in ``tasks/invoices/`` are authoritative; ``init_db`` is safe to
run repeatedly (``CREATE TABLE IF NOT EXISTS``) because the batch command
rebuilds the database from those fixtures on every run.
"""

from __future__ import annotations

import sqlite3

DDL_STATEMENTS: tuple[str, ...] = (
    # Supplied reference data: purchase orders.
    """
    CREATE TABLE IF NOT EXISTS purchase_orders (
        po_id       TEXT    PRIMARY KEY,
        supplier_id TEXT    NOT NULL,
        sku         TEXT    NOT NULL,
        quantity    INTEGER NOT NULL,
        unit_cents  INTEGER NOT NULL
    )
    """,
    # Supplied reference data: receipts against a purchase order.
    """
    CREATE TABLE IF NOT EXISTS receipts (
        receipt_id TEXT    PRIMARY KEY,
        po_id      TEXT    NOT NULL REFERENCES purchase_orders (po_id),
        sku        TEXT    NOT NULL,
        quantity   INTEGER NOT NULL
    )
    """,
    # One row per source invoice document.
    #
    # extraction_failed distinguishes "extraction did not run or did not
    # return usable fields" from "extraction ran and genuinely found no
    # purchase-order reference" (the missing-reference fixture). Both leave
    # extracted_fields empty or po_id null, so the rules engine cannot tell
    # them apart from field values alone -- this column is the one place
    # that failure state is recorded, read by pipeline.recalculate_one to
    # classify the invoice as 'failed' rather than 'unresolved'. 0 (false)
    # for every invoice whose extraction succeeded, including the
    # prepared-record (seeded) path, where extraction never ran at all but
    # the values are known good.
    """
    CREATE TABLE IF NOT EXISTS invoices (
        invoice_id        INTEGER PRIMARY KEY,
        file_id           TEXT    NOT NULL UNIQUE,
        image_path        TEXT    NOT NULL,
        layout            TEXT    NOT NULL,
        received_at       TEXT    NOT NULL,
        extraction_source TEXT    NOT NULL,
        extraction_failed INTEGER NOT NULL DEFAULT 0,
        -- The reason text for a failed extraction (e.g. "CacheMissError:
        -- no cache entry for file_id ..."), for the reviewer detail page.
        -- NULL whenever extraction_failed = 0.
        extraction_failure_reason TEXT
    )
    """,
    # The seven extracted values, as TEXT (what the model returned, before
    # interpretation). original_value is never overwritten; current_value
    # changes when a reviewer corrects it. Both nullable: po_id can
    # legitimately be null.
    """
    CREATE TABLE IF NOT EXISTS extracted_fields (
        invoice_id     INTEGER NOT NULL REFERENCES invoices (invoice_id),
        field_name     TEXT    NOT NULL,
        original_value TEXT,
        current_value  TEXT,
        PRIMARY KEY (invoice_id, field_name)
    )
    """,
    # Append-only audit trail of reviewer corrections.
    """
    CREATE TABLE IF NOT EXISTS corrections (
        correction_id INTEGER PRIMARY KEY,
        invoice_id    INTEGER NOT NULL REFERENCES invoices (invoice_id),
        field_name    TEXT    NOT NULL,
        value_before  TEXT,
        value_after   TEXT,
        changed_by    TEXT    NOT NULL,
        changed_at    TEXT    NOT NULL
    )
    """,
    # Reconciliation outcome. Overwritten on each recompute. All cent
    # columns are nullable: unresolved/failed carry no amounts, duplicate
    # carries none either.
    """
    CREATE TABLE IF NOT EXISTS reconciliation_results (
        invoice_id        INTEGER PRIMARY KEY REFERENCES invoices (invoice_id),
        status            TEXT    NOT NULL CHECK (
                               status IN (
                                   'reconciled',
                                   'discrepant',
                                   'duplicate',
                                   'unresolved',
                                   'failed'
                               )
                           ),
        expected_cents    INTEGER,
        billed_cents      INTEGER,
        difference_cents  INTEGER,
        -- Populated only for 'duplicate'; NULL for every other status.
        count_as_payable  INTEGER,
        matched_po_id     TEXT    REFERENCES purchase_orders (po_id),
        -- TEXT, matching receipts.receipt_id ("RC-1"), not an integer.
        matched_receipt_id TEXT   REFERENCES receipts (receipt_id),
        computed_at       TEXT    NOT NULL
    )
    """,
    # One row per discrepant invoice: the drafted note and any reviewer edit.
    #
    # drafted_by records how drafted_text was produced: 'model' when a
    # verified model draft was stored, 'calculated' when the model was
    # skipped, never called, or every attempt failed verification (the
    # pure notes.draft_note fallback). model_id/drafted_at/attempts/
    # rejection_reasons are all nullable and only ever populated on the
    # 'model' path (model_id is also null on 'calculated', since no model
    # produced the stored text). Added additively, with a NOT NULL default
    # of 'calculated' on drafted_by so a row written before this column
    # existed (there are none yet, but the pattern follows
    # extraction_failure_reason's additive precedent) still reads back a
    # valid value instead of NULL.
    """
    CREATE TABLE IF NOT EXISTS discrepancy_notes (
        invoice_id        INTEGER PRIMARY KEY REFERENCES invoices (invoice_id),
        drafted_text      TEXT    NOT NULL,
        current_text      TEXT    NOT NULL,
        is_reviewer_edited INTEGER NOT NULL,
        edited_at         TEXT,
        drafted_by        TEXT    NOT NULL DEFAULT 'calculated'
                               CHECK (drafted_by IN ('model', 'calculated')),
        model_id          TEXT,
        drafted_at        TEXT,
        attempts          INTEGER,
        rejection_reasons TEXT,
        -- Set when a correction recalculates the invoice's figures while a
        -- reviewer's edited note is on file: the edit is never overwritten
        -- (technical-considerations.md's "a human edit is the most trusted
        -- text"), but the figures it describes may now be stale. 0 for
        -- every row by default, including every model/calculated draft
        -- that has no reviewer edit at all; set to 1 only on the one
        -- codepath that detects a correction landing on top of an
        -- existing reviewer edit.
        edit_superseded   INTEGER NOT NULL DEFAULT 0
    )
    """,
    # Append-only version history of a discrepancy note, one row per
    # version, in the same spirit as ``corrections`` for field edits. Rows
    # are never updated or deleted: a redraft or a reviewer's edit is
    # always a new row. ``discrepancy_notes`` stays the current-state row
    # and is kept in step with the latest version by the repository
    # functions that write both. model_id/attempts/rejection_reasons are
    # filled only for 'model' and 'calculated' drafts; created_by is
    # 'model', 'system' (the calculated fallback) or 'reviewer'.
    """
    CREATE TABLE IF NOT EXISTS discrepancy_note_versions (
        version_id        INTEGER PRIMARY KEY,
        invoice_id        INTEGER NOT NULL REFERENCES invoices (invoice_id),
        version_no        INTEGER NOT NULL,
        text              TEXT    NOT NULL,
        source            TEXT    NOT NULL
                               CHECK (source IN ('model', 'calculated', 'reviewer')),
        model_id          TEXT,
        attempts          INTEGER,
        rejection_reasons TEXT,
        created_by        TEXT    NOT NULL,
        created_at        TEXT    NOT NULL,
        UNIQUE (invoice_id, version_no)
    )
    """,
)

INDEX_STATEMENTS: tuple[str, ...] = (
    # The match join: supplier_id and po_id together.
    "CREATE INDEX IF NOT EXISTS ix_purchase_orders_supplier_po "
    "ON purchase_orders (supplier_id, po_id)",
    "CREATE INDEX IF NOT EXISTS ix_receipts_po_id ON receipts (po_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_invoices_file_id ON invoices (file_id)",
    "CREATE INDEX IF NOT EXISTS ix_corrections_invoice_id ON corrections (invoice_id)",
)


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes if they do not already exist.

    Idempotent: safe to call on every batch run against the same
    connection/database file.
    """
    for statement in DDL_STATEMENTS:
        conn.execute(statement)
    for statement in INDEX_STATEMENTS:
        conn.execute(statement)
    conn.commit()
