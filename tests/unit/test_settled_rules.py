# @layer: unit
# @spec: 001-invoice-reconciliation-review
# @regression
"""End-to-end proof, through the real pipeline and real fixtures, of two
settled rules that ``test_rules.py`` can only prove with constructed inputs:

1. A quantity overbill (billed quantity differs from both the ordered and
   received quantity) is ``discrepant``, and ``expected_cents`` stays valued
   at the ORDERED quantity times the agreed unit price -- never at the
   billed quantity.
2. An undercharge (a negative ``difference_cents``) is ``discrepant`` too,
   not some third status, but the negative amount is excluded from any
   recoverable total quoted to a supplier.

``test_rules.py`` states both rules against a ``current_fields`` dict and a
``MatchResult`` built by hand. That is not the same claim as "ingest reads
the fixture correctly, the matcher resolves the right PO and receipt, and
the pipeline persists the right numbers" -- a constructed-input test can
pass while any of those real steps mishandles the record. This file drives
``run_ingest`` and ``pipeline.run_batch`` against a temporary database and
reads back ``reconciliation_results``, so a defect in ingest, matching, or
storage would surface here even if ``rules.reconcile`` itself is correct.

Hand-calculated expected values come from
``tasks/invoices/expected-new-fixtures.json`` and are asserted literally
here, never re-derived from whatever the pipeline currently returns.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from invoice_reconciliation.db import repository
from invoice_reconciliation.db.connection import get_connection
from invoice_reconciliation.db.ingest import run_ingest
from invoice_reconciliation.pipeline import run_batch

SEED_PATH = Path("tasks/invoices/seed.json")
IMAGES_DIR = Path("tasks/invoices/images")

# Hand-calculated in tasks/invoices/expected-new-fixtures.json. Repeated here
# as plain literals (not re-read from the JSON file) so a change to that
# file cannot silently move the goalposts this test checks against.
QUANTITY_OVERBILL_EXPECTED_CENTS = 10000
QUANTITY_OVERBILL_BILLED_CENTS = 16000
QUANTITY_OVERBILL_DIFFERENCE_CENTS = 6000

UNDERCHARGE_EXPECTED_CENTS = 10000
UNDERCHARGE_BILLED_CENTS = 9000
UNDERCHARGE_DIFFERENCE_CENTS = -1000

# Hand-worked across all six fixture invoices, not derived from the query
# under test:
#   clean              -> reconciled,  difference_cents =     0  (excluded: not discrepant)
#   wrong-price         -> discrepant,  difference_cents =  2000  (included: positive)
#   duplicate           -> duplicate,   difference_cents =  None (excluded: not discrepant)
#   missing-reference   -> unresolved,  difference_cents =  None (excluded: not discrepant)
#   quantity-overbill   -> discrepant,  difference_cents =  6000  (included: positive)
#   undercharge         -> discrepant,  difference_cents = -1000  (excluded: negative)
# Recoverable total = 2000 + 6000 = 8000
RECOVERABLE_TOTAL_CENTS = 8000


@pytest.fixture(scope="module")
def seeded_db_path(tmp_path_factory):
    """Build the seeded database once, outside the project tree, and run
    the full batch against it. Shared read-only across every test in this
    module -- nothing here corrects or re-ingests."""
    db_path = tmp_path_factory.mktemp("settled-rules-db") / "invoice_reconciliation.sqlite"
    run_ingest(db_path=db_path, seed_path=SEED_PATH, images_dir=IMAGES_DIR)
    with get_connection(db_path) as conn:
        run_batch(conn)
        conn.commit()
    return db_path


@pytest.fixture
def conn(seeded_db_path):
    connection = get_connection(seeded_db_path)
    yield connection
    connection.close()


def _result_for(conn, file_id: str):
    invoice = repository.get_invoice_by_file_id(conn, file_id=file_id)
    assert invoice is not None, f"fixture invoice {file_id!r} not found"
    result = repository.get_reconciliation_result(conn, invoice_id=invoice["invoice_id"])
    assert result is not None, f"no reconciliation result persisted for {file_id!r}"
    return result


class TestQuantityOverbillThroughThePipeline:
    """INV-4 (file_id quantity-overbill): bills 8 units at the PO-1/RC-1
    agreed price of 2000 cents, against an ordered and received quantity of
    5. The quantity mismatch rule applies regardless of the sign or size of
    difference_cents (domain.md step 5)."""

    def test_is_discrepant_matched_to_po1_and_rc1(self, conn):
        result = _result_for(conn, "quantity-overbill")

        assert result["status"] == "discrepant"
        assert result["matched_po_id"] == "PO-1"
        assert result["matched_receipt_id"] == "RC-1"

    def test_expected_billed_and_difference_match_hand_calculation(self, conn):
        result = _result_for(conn, "quantity-overbill")

        assert result["billed_cents"] == QUANTITY_OVERBILL_BILLED_CENTS
        assert result["difference_cents"] == QUANTITY_OVERBILL_DIFFERENCE_CENTS
        assert result["expected_cents"] == QUANTITY_OVERBILL_EXPECTED_CENTS

    def test_expected_cents_is_valued_at_ordered_quantity_not_billed_quantity(self, conn):
        # The distinguishing assertion: ordered_quantity (5) * unit_cents
        # (2000) = 10000. If expected_cents were instead valued at the
        # billed quantity (8), it would read 16000 -- the same number as
        # billed_cents, which would make this invoice look reconciled.
        result = _result_for(conn, "quantity-overbill")

        assert result["expected_cents"] == 10000
        assert result["expected_cents"] != result["billed_cents"]


class TestUndercargeThroughThePipeline:
    """INV-5 (file_id undercharge): bills 5 units (quantities agree with
    both PO-2's order and RC-2's receipt) at 1800 cents instead of the
    agreed 2000 cents -- isolating the sign case with no quantity mismatch
    involved."""

    def test_is_discrepant_matched_to_po2_and_rc2(self, conn):
        result = _result_for(conn, "undercharge")

        assert result["status"] == "discrepant"
        assert result["matched_po_id"] == "PO-2"
        assert result["matched_receipt_id"] == "RC-2"

    def test_expected_billed_and_difference_match_hand_calculation(self, conn):
        result = _result_for(conn, "undercharge")

        assert result["expected_cents"] == UNDERCHARGE_EXPECTED_CENTS
        assert result["billed_cents"] == UNDERCHARGE_BILLED_CENTS
        assert result["difference_cents"] == UNDERCHARGE_DIFFERENCE_CENTS

    def test_difference_cents_is_negative_not_a_separate_undercharge_status(self, conn):
        # The sign alone must never invent a third status -- an undercharge
        # is discrepant, exactly like an overcharge, distinguished only by
        # the sign of difference_cents.
        result = _result_for(conn, "undercharge")

        assert result["status"] == "discrepant"
        assert result["difference_cents"] == -1000
        assert result["difference_cents"] < 0


class TestRecoverableTotalExcludesTheUndercharge:
    """The recoverable total a reviewer quotes to a supplier is
    SUM(difference_cents) WHERE status='discrepant' AND
    difference_cents > 0, run over the real persisted results for the full
    six-invoice batch."""

    def test_recoverable_total_matches_hand_calculation_across_all_six_invoices(
        self, conn
    ):
        row = conn.execute(
            """
            SELECT SUM(difference_cents) AS total
            FROM reconciliation_results
            WHERE status = 'discrepant' AND difference_cents > 0
            """
        ).fetchone()

        assert row["total"] == RECOVERABLE_TOTAL_CENTS
        assert row["total"] == 8000

    def test_undercharge_difference_is_excluded_from_the_sum(self, conn):
        # Negative-control: if the undercharge's -1000 were wrongly
        # included (e.g. a query that sums all discrepant rows with no
        # sign filter), the total would read 7000 instead of 8000.
        row_with_sign_filter = conn.execute(
            """
            SELECT SUM(difference_cents) AS total
            FROM reconciliation_results
            WHERE status = 'discrepant' AND difference_cents > 0
            """
        ).fetchone()
        row_without_sign_filter = conn.execute(
            """
            SELECT SUM(difference_cents) AS total
            FROM reconciliation_results
            WHERE status = 'discrepant'
            """
        ).fetchone()

        assert row_with_sign_filter["total"] == 8000
        # Without the sign filter, the undercharge's -1000 nets against the
        # two overcharges (2000 + 6000 - 1000 = 7000) -- proving the sign
        # filter, not the status filter, is what does the excluding.
        assert row_without_sign_filter["total"] == 7000
        assert row_with_sign_filter["total"] != row_without_sign_filter["total"]


class TestOriginalFourInvoicesUndisturbed:
    """The two new fixtures must not change the oracle results for the
    original four invoices -- same statuses, same amounts, in the same
    batch run."""

    def test_clean_invoice_is_still_reconciled(self, conn):
        result = _result_for(conn, "clean")

        assert result["status"] == "reconciled"
        assert result["expected_cents"] == 10000
        assert result["billed_cents"] == 10000
        assert result["difference_cents"] == 0
        assert result["matched_po_id"] == "PO-1"
        assert result["matched_receipt_id"] == "RC-1"

    def test_wrong_price_invoice_is_still_discrepant_with_positive_difference(self, conn):
        result = _result_for(conn, "wrong-price")

        assert result["status"] == "discrepant"
        assert result["expected_cents"] == 10000
        assert result["billed_cents"] == 12000
        assert result["difference_cents"] == 2000
        assert result["matched_po_id"] == "PO-2"
        assert result["matched_receipt_id"] == "RC-2"

    def test_duplicate_invoice_is_still_duplicate_and_not_payable(self, conn):
        result = _result_for(conn, "duplicate")

        assert result["status"] == "duplicate"
        assert result["count_as_payable"] == 0
        assert result["expected_cents"] is None
        assert result["difference_cents"] is None

    def test_missing_reference_invoice_is_still_unresolved(self, conn):
        result = _result_for(conn, "missing-reference")

        assert result["status"] == "unresolved"
        assert result["expected_cents"] is None
        assert result["difference_cents"] is None
        assert result["matched_po_id"] is None
