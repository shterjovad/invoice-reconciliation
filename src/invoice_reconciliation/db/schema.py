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
        extraction_failed INTEGER NOT NULL DEFAULT 0
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
    """
    CREATE TABLE IF NOT EXISTS discrepancy_notes (
        invoice_id        INTEGER PRIMARY KEY REFERENCES invoices (invoice_id),
        drafted_text      TEXT    NOT NULL,
        current_text      TEXT    NOT NULL,
        is_reviewer_edited INTEGER NOT NULL,
        edited_at         TEXT
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
