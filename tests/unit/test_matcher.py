# @layer: unit
# @spec: 001-invoice-reconciliation-review
# @regression
"""Unit tests for the matcher module
(src/invoice_reconciliation/reconciliation/matcher.py).

The seeded fixture contains a deliberate trap: ``PO-1`` and ``PO-2`` share
the same supplier (``S1``), SKU (``CAB-1``), quantity (5) and unit price
(2000 cents) -- they differ only by ``po_id``. A matcher that ever falls
back to amount, SKU, or supplier alone would silently resolve an invoice
naming ``PO-2`` to ``PO-1`` (or vice versa) while every downstream figure
still looked plausible. These tests exist to prove that cannot happen.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from invoice_reconciliation.db import repository
from invoice_reconciliation.db.connection import get_connection
from invoice_reconciliation.db.ingest import run_ingest
from invoice_reconciliation.reconciliation.matcher import (
    find_earlier_duplicate,
    find_purchase_order_and_receipt,
)

SEED_PATH = Path("tasks/invoices/seed.json")
IMAGES_DIR = Path("tasks/invoices/images")


@pytest.fixture(scope="module")
def seeded_db_path(tmp_path_factory):
    """Build the seeded database once and share it read-only across tests.

    ``tmp_path_factory`` (module scope) keeps the db outside the project
    tree. Tests that mutate state (corrections) get their own isolated
    database via a separate fixture instead of sharing this one.
    """
    db_path = tmp_path_factory.mktemp("matcher-db") / "invoice_reconciliation.sqlite"
    run_ingest(db_path=db_path, seed_path=SEED_PATH, images_dir=IMAGES_DIR)
    return db_path


@pytest.fixture
def conn(seeded_db_path):
    connection = get_connection(seeded_db_path)
    yield connection
    connection.close()


def _invoice_id(conn, file_id: str) -> int:
    invoice = repository.get_invoice_by_file_id(conn, file_id=file_id)
    assert invoice is not None, f"fixture invoice {file_id!r} not found"
    return invoice["invoice_id"]


class TestFindPurchaseOrderAndReceiptTrapCase:
    """PO-1 and PO-2 are identical apart from their ID; the matcher must
    resolve each invoice to the PO it actually names."""

    def test_invoice_naming_po2_matches_po2_not_po1(self, conn):
        invoice_id = _invoice_id(conn, "wrong-price")  # po_id == "PO-2"
        current_fields = repository.get_current_fields(conn, invoice_id=invoice_id)

        result = find_purchase_order_and_receipt(conn, current_fields=current_fields)

        assert result.is_matched
        assert result.purchase_order["po_id"] == "PO-2"
        assert result.purchase_order["po_id"] != "PO-1"

    def test_invoice_naming_po2_resolves_receipt_rc2_not_rc1(self, conn):
        invoice_id = _invoice_id(conn, "wrong-price")  # po_id == "PO-2"
        current_fields = repository.get_current_fields(conn, invoice_id=invoice_id)

        result = find_purchase_order_and_receipt(conn, current_fields=current_fields)

        assert result.receipt is not None
        assert result.receipt["receipt_id"] == "RC-2"
        assert result.receipt["receipt_id"] != "RC-1"

    def test_invoice_naming_po1_matches_po1_not_po2(self, conn):
        invoice_id = _invoice_id(conn, "clean")  # po_id == "PO-1"
        current_fields = repository.get_current_fields(conn, invoice_id=invoice_id)

        result = find_purchase_order_and_receipt(conn, current_fields=current_fields)

        assert result.is_matched
        assert result.purchase_order["po_id"] == "PO-1"
        assert result.receipt["receipt_id"] == "RC-1"


class TestFindPurchaseOrderAndReceiptNoMatch:
    """Negative cases: missing or conflicting references match nothing,
    cleanly, without raising."""

    def test_null_po_id_matches_nothing(self, conn):
        invoice_id = _invoice_id(conn, "missing-reference")  # po_id is NULL
        current_fields = repository.get_current_fields(conn, invoice_id=invoice_id)
        assert current_fields["po_id"] is None  # sanity: fixture really is null

        result = find_purchase_order_and_receipt(conn, current_fields=current_fields)

        assert result.is_matched is False
        assert result.purchase_order is None
        assert result.receipt is None

    def test_po_id_under_different_supplier_matches_nothing(self, conn):
        # PO-1 genuinely exists, but under supplier S1, not S9. A matcher
        # that joined on po_id alone (ignoring supplier_id) would wrongly
        # return PO-1 here.
        current_fields = {"supplier_id": "S9", "po_id": "PO-1"}

        result = find_purchase_order_and_receipt(conn, current_fields=current_fields)

        assert result.is_matched is False
        assert result.purchase_order is None
        assert result.receipt is None

    def test_missing_supplier_id_matches_nothing(self, conn):
        current_fields = {"supplier_id": None, "po_id": "PO-1"}

        result = find_purchase_order_and_receipt(conn, current_fields=current_fields)

        assert result.is_matched is False
        assert result.purchase_order is None


class TestFindEarlierDuplicate:
    """clean and duplicate share (S1, INV-1); ordering by received_at
    decides which copy is the earlier, payable one."""

    def test_earlier_invoice_reports_no_earlier_duplicate(self, conn):
        clean = repository.get_invoice_by_file_id(conn, file_id="clean")

        earlier = find_earlier_duplicate(
            conn,
            supplier_id="S1",
            invoice_number="INV-1",
            received_at=clean["received_at"],
            exclude_invoice_id=clean["invoice_id"],
        )

        assert earlier is None

    def test_later_invoice_reports_the_earlier_one(self, conn):
        clean = repository.get_invoice_by_file_id(conn, file_id="clean")
        duplicate = repository.get_invoice_by_file_id(conn, file_id="duplicate")

        earlier = find_earlier_duplicate(
            conn,
            supplier_id="S1",
            invoice_number="INV-1",
            received_at=duplicate["received_at"],
            exclude_invoice_id=duplicate["invoice_id"],
        )

        assert earlier is not None
        assert earlier["invoice_id"] == clean["invoice_id"]
        assert earlier["file_id"] == "clean"

    def test_no_shared_invoice_number_reports_no_duplicate(self, conn):
        wrong_price = repository.get_invoice_by_file_id(conn, file_id="wrong-price")

        earlier = find_earlier_duplicate(
            conn,
            supplier_id="S1",
            invoice_number="INV-2",
            received_at=wrong_price["received_at"],
            exclude_invoice_id=wrong_price["invoice_id"],
        )

        assert earlier is None


class TestMatcherRespectsCorrections:
    """A correction updates current_value, and the matcher must follow it
    rather than the original extracted value."""

    @pytest.fixture
    def corrected_db_conn(self, tmp_path):
        db_path = tmp_path / "invoice_reconciliation.sqlite"
        run_ingest(db_path=db_path, seed_path=SEED_PATH, images_dir=IMAGES_DIR)
        connection = get_connection(db_path)
        yield connection
        connection.close()

    def test_matcher_follows_corrected_po_id_not_original(self, corrected_db_conn):
        conn = corrected_db_conn
        invoice_id = _invoice_id(conn, "clean")  # originally po_id == "PO-1"

        original_fields = repository.get_current_fields(conn, invoice_id=invoice_id)
        assert original_fields["po_id"] == "PO-1"
        original_result = find_purchase_order_and_receipt(
            conn, current_fields=original_fields
        )
        assert original_result.purchase_order["po_id"] == "PO-1"

        repository.update_current_field_value(
            conn, invoice_id=invoice_id, field_name="po_id", current_value="PO-2"
        )
        conn.commit()

        corrected_fields = repository.get_current_fields(conn, invoice_id=invoice_id)
        assert corrected_fields["po_id"] == "PO-2"

        corrected_result = find_purchase_order_and_receipt(
            conn, current_fields=corrected_fields
        )

        assert corrected_result.is_matched
        assert corrected_result.purchase_order["po_id"] == "PO-2"
        assert corrected_result.receipt["receipt_id"] == "RC-2"

        # The original_value column must still hold the uncorrected value --
        # the matcher reading current_value never destroys provenance.
        original_values = repository.get_original_fields(conn, invoice_id=invoice_id)
        assert original_values["po_id"] == "PO-1"
