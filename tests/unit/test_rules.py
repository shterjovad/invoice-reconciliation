# @layer: unit
# @spec: 001-invoice-reconciliation-review
# @regression
"""Unit tests for the reconciliation rules engine
(src/invoice_reconciliation/reconciliation/rules.py).

``reconcile`` is pure, so every test here constructs its inputs directly —
a ``current_fields`` dict and a ``MatchResult`` — rather than standing up a
database. This lets each test state exactly one rule from
technical-considerations.md section 2.5 (evaluation order: failed ->
unresolved -> duplicate -> reconciled/discrepant) without incidental setup
obscuring which rule is under test.

The "recoverable total" (SUM(difference_cents) WHERE status = 'discrepant'
AND difference_cents > 0) is not implemented anywhere yet -- `pipeline.py`
and `seed_check.py` only persist/compare individual results; the summary
view arrives in a later slice (Slice 12). So this file does not invent a
summary function to test. Instead it tests the rule that total depends on:
that each status carries the right `difference_cents` and
`count_as_payable` combination so that such a SUM, once written, would
naturally and correctly include overcharges only and exclude duplicates,
unresolved, failed, and undercharges. See `TestRecoverableTotalInputs`.
"""

from __future__ import annotations

import pytest

from invoice_reconciliation.money import MoneyFormatError
from invoice_reconciliation.reconciliation.matcher import MatchResult
from invoice_reconciliation.reconciliation.rules import (
    ReconciliationResult,
    reconcile,
)

# ---------------------------------------------------------------------------
# Shared fixtures: a purchase order and receipt agreeing on 5 units @ 2000
# cents (the worked example's PO-2 / RC-2 figures from domain.md: expected
# 10,000 cents).
# ---------------------------------------------------------------------------

PO_ROW = {"po_id": "PO-2", "quantity": "5", "unit_cents": "2000"}
RECEIPT_ROW = {"receipt_id": "RC-2", "quantity": "5"}

MATCHED = MatchResult(purchase_order=PO_ROW, receipt=RECEIPT_ROW)
MATCHED_NO_RECEIPT = MatchResult(purchase_order=PO_ROW, receipt=None)
UNMATCHED = MatchResult(purchase_order=None, receipt=None)


def _fields(**overrides: str | None) -> dict[str, str | None]:
    base: dict[str, str | None] = {
        "supplier_id": "S1",
        "po_id": "PO-2",
        "invoice_number": "INV-2",
        "sku": "CAB-1",
        "quantity": "5",
        "unit_cents": "2400",
        "total_cents": "10000",
    }
    base.update(overrides)
    return base


class TestFailedStatus:
    """extraction_failed short-circuits everything else: no amounts."""

    def test_extraction_failed_returns_failed_with_no_amounts(self):
        result = reconcile(
            current_fields=_fields(),
            match=MATCHED,
            is_duplicate=False,
            extraction_failed=True,
        )

        assert result.status == "failed"
        assert result.expected_cents is None
        assert result.billed_cents is None
        assert result.difference_cents is None
        assert result.count_as_payable is None
        assert result.matched_po_id is None
        assert result.matched_receipt_id is None

    def test_extraction_failed_ignores_match_and_duplicate_flags(self):
        # extraction_failed=True must win even when match/is_duplicate look
        # like they'd otherwise produce a different status.
        result = reconcile(
            current_fields=_fields(total_cents="not-a-number"),
            match=UNMATCHED,
            is_duplicate=True,
            extraction_failed=True,
        )

        assert result.status == "failed"


class TestUnresolvedStatus:
    """po_id null, no matching PO, or PO under a different supplier -- no
    amount fallback is ever attempted."""

    def test_unmatched_purchase_order_is_unresolved_with_no_amounts(self):
        result = reconcile(
            current_fields=_fields(po_id=None),
            match=UNMATCHED,
            is_duplicate=False,
        )

        assert result.status == "unresolved"
        assert result.expected_cents is None
        assert result.billed_cents is None
        assert result.difference_cents is None
        assert result.count_as_payable is None
        assert result.matched_po_id is None
        assert result.matched_receipt_id is None

    def test_unresolved_never_falls_back_to_amount_matching(self):
        # Even when current_fields carries amounts that would otherwise
        # reconcile cleanly, an unmatched MatchResult must still produce
        # unresolved -- the platform never matches on amount.
        result = reconcile(
            current_fields=_fields(total_cents="10000"),
            match=UNMATCHED,
            is_duplicate=False,
        )

        assert result.status == "unresolved"
        assert result.expected_cents is None


class TestDuplicateStatus:
    """An earlier invoice sharing (supplier_id, invoice_number) makes a
    later copy a non-payable duplicate; the first copy stays payable."""

    def test_later_copy_is_duplicate_not_payable_no_cent_fields(self):
        result = reconcile(
            current_fields=_fields(),
            match=MATCHED,
            is_duplicate=True,
        )

        assert result.status == "duplicate"
        assert result.count_as_payable is False
        assert result.expected_cents is None
        assert result.billed_cents is None
        assert result.difference_cents is None
        assert result.matched_po_id is None
        assert result.matched_receipt_id is None

    def test_first_copy_is_not_marked_duplicate_and_stays_payable(self):
        # The caller is responsible for passing is_duplicate=False for the
        # earliest copy by received_at; it must then be judged on its own
        # figures, not short-circuited at the duplicate rule.
        result = reconcile(
            current_fields=_fields(total_cents="10000"),
            match=MATCHED,
            is_duplicate=False,
        )

        assert result.status != "duplicate"
        assert result.status == "reconciled"
        assert result.count_as_payable is None  # only set for duplicates
        assert result.expected_cents == 10000
        assert result.billed_cents == 10000
        assert result.difference_cents == 0


class TestReconciledStatus:
    """Billed equals expected (ordered_quantity * unit_cents), quantities
    agree with both ordered and received -- a zero difference."""

    def test_billed_equals_expected_is_reconciled_with_zero_difference(self):
        result = reconcile(
            current_fields=_fields(quantity="5", total_cents="10000"),
            match=MATCHED,
            is_duplicate=False,
        )

        assert result.status == "reconciled"
        assert result.expected_cents == 10000
        assert result.billed_cents == 10000
        assert result.difference_cents == 0
        assert result.matched_po_id == "PO-2"
        assert result.matched_receipt_id == "RC-2"


class TestDiscrepantStatus:
    """Covers overcharge, undercharge, and quantity mismatches (valued at
    the agreed unit price regardless of the billed quantity)."""

    def test_overcharge_is_discrepant_with_positive_difference(self):
        # The canonical worked example from domain.md: expected 10000,
        # billed 12000, difference 2000.
        result = reconcile(
            current_fields=_fields(quantity="5", total_cents="12000"),
            match=MATCHED,
            is_duplicate=False,
        )

        assert result.status == "discrepant"
        assert result.expected_cents == 10000
        assert result.billed_cents == 12000
        assert result.difference_cents == 2000

    def test_undercharge_is_discrepant_with_negative_difference(self):
        # The sign alone never changes the status -- an undercharge is
        # discrepant too, not some third status.
        result = reconcile(
            current_fields=_fields(quantity="5", total_cents="8000"),
            match=MATCHED,
            is_duplicate=False,
        )

        assert result.status == "discrepant"
        assert result.expected_cents == 10000
        assert result.billed_cents == 8000
        assert result.difference_cents == -2000

    def test_quantity_mismatch_against_ordered_is_discrepant_even_with_equal_amounts(
        self,
    ):
        # Billed quantity (4) differs from the ordered quantity (5), but
        # the invoice still happens to bill the same total (10000) as what
        # 5 units at the agreed price would cost. Must still be discrepant
        # -- valued at the agreed unit price, not reconciled just because
        # the totals happen to match.
        result = reconcile(
            current_fields=_fields(quantity="4", total_cents="10000"),
            match=MATCHED,
            is_duplicate=False,
        )

        assert result.status == "discrepant"
        assert result.expected_cents == 10000  # still ordered_qty * unit_cents
        assert result.billed_cents == 10000
        assert result.difference_cents == 0

    def test_quantity_mismatch_against_received_is_discrepant_even_with_equal_amounts(
        self,
    ):
        # Billed quantity (5) matches the ordered quantity (5) but not what
        # the receipt actually confirms (3 delivered) -- still discrepant.
        receipt_short = {"receipt_id": "RC-2", "quantity": "3"}
        match = MatchResult(purchase_order=PO_ROW, receipt=receipt_short)

        result = reconcile(
            current_fields=_fields(quantity="5", total_cents="10000"),
            match=match,
            is_duplicate=False,
        )

        assert result.status == "discrepant"
        assert result.expected_cents == 10000
        assert result.billed_cents == 10000
        assert result.difference_cents == 0

    def test_quantity_mismatch_valued_at_agreed_unit_price_not_billed_quantity(self):
        # expected_cents must stay ordered_quantity * unit_cents (10000)
        # regardless of what the invoice claims as its own quantity, even
        # when that claimed quantity would imply a different total.
        result = reconcile(
            current_fields=_fields(quantity="10", total_cents="24000"),
            match=MATCHED,
            is_duplicate=False,
        )

        assert result.status == "discrepant"
        assert result.expected_cents == 10000
        assert result.billed_cents == 24000
        assert result.difference_cents == 14000

    def test_no_receipt_only_checks_against_ordered_quantity(self):
        # When the matched PO has no receipt on file, quantity mismatch can
        # only be judged against the ordered quantity -- a billed quantity
        # equal to ordered, with no receipt, is not a mismatch by itself.
        result = reconcile(
            current_fields=_fields(quantity="5", total_cents="9000"),
            match=MATCHED_NO_RECEIPT,
            is_duplicate=False,
        )

        assert result.status == "discrepant"  # non-zero difference alone
        assert result.difference_cents == -1000
        assert result.matched_receipt_id is None


class TestRecoverableTotalInputs:
    """The recoverable total a reviewer quotes to a supplier is

        SUM(difference_cents) WHERE status = 'discrepant'
                                 AND difference_cents > 0

    No summary function exists yet in pipeline.py or seed_check.py to sum
    this (the summary view is Slice 12) -- these tests instead pin down
    that each status produces the exact status/difference_cents/
    count_as_payable combination such a SUM depends on, so that duplicates,
    unresolved, failed invoices, and undercharges are structurally excluded
    and only positive discrepant differences are structurally included.
    """

    def test_discrepant_overcharge_has_positive_difference_countable_in_total(self):
        result = reconcile(
            current_fields=_fields(quantity="5", total_cents="12000"),
            match=MATCHED,
            is_duplicate=False,
        )

        assert result.status == "discrepant"
        assert result.difference_cents == 2000
        assert result.difference_cents > 0

    def test_discrepant_undercharge_must_be_excluded_by_sign_not_status(self):
        # Status alone ('discrepant') is not enough to decide inclusion --
        # a SUM(...) WHERE status='discrepant' with no sign filter would
        # wrongly net this against overcharges. The difference must be
        # negative so that an explicit `difference_cents > 0` filter
        # excludes it.
        result = reconcile(
            current_fields=_fields(quantity="5", total_cents="8000"),
            match=MATCHED,
            is_duplicate=False,
        )

        assert result.status == "discrepant"
        assert result.difference_cents == -2000
        assert not (result.difference_cents > 0)

    def test_duplicate_has_no_difference_cents_to_sum(self):
        # A WHERE status='discrepant' filter already excludes duplicate
        # rows structurally, but this confirms duplicates carry no
        # difference_cents value at all -- there is nothing here that
        # could leak into a total even if a future query were careless.
        result = reconcile(
            current_fields=_fields(),
            match=MATCHED,
            is_duplicate=True,
        )

        assert result.status == "duplicate"
        assert result.difference_cents is None
        assert result.count_as_payable is False

    def test_unresolved_has_no_difference_cents_to_sum(self):
        result = reconcile(
            current_fields=_fields(po_id=None),
            match=UNMATCHED,
            is_duplicate=False,
        )

        assert result.status == "unresolved"
        assert result.difference_cents is None

    def test_failed_has_no_difference_cents_to_sum(self):
        result = reconcile(
            current_fields=_fields(),
            match=MATCHED,
            is_duplicate=False,
            extraction_failed=True,
        )

        assert result.status == "failed"
        assert result.difference_cents is None

    def test_reconciled_has_zero_difference_excluded_by_status_filter(self):
        result = reconcile(
            current_fields=_fields(quantity="5", total_cents="10000"),
            match=MATCHED,
            is_duplicate=False,
        )

        assert result.status == "reconciled"
        assert result.difference_cents == 0


class TestMoneyParsing:
    """total_cents is already integer cents as text -- never dollars."""

    def test_total_cents_parsed_as_plain_integer_not_dollars_to_cents(self):
        # "10000" must mean 10000 cents ($100.00), not be reinterpreted as
        # a dollar string (which would yield 1,000,000 cents).
        result = reconcile(
            current_fields=_fields(quantity="5", total_cents="10000"),
            match=MATCHED,
            is_duplicate=False,
        )

        assert result.billed_cents == 10000

    def test_malformed_total_cents_raises_money_format_error(self):
        with pytest.raises(MoneyFormatError):
            reconcile(
                current_fields=_fields(total_cents="not-a-number"),
                match=MATCHED,
                is_duplicate=False,
            )

    def test_missing_total_cents_raises_money_format_error(self):
        with pytest.raises(MoneyFormatError):
            reconcile(
                current_fields=_fields(total_cents=None),
                match=MATCHED,
                is_duplicate=False,
            )


class TestReconciliationResultDefaults:
    """Sanity check on the dataclass itself: a bare construction carries
    every optional field as None."""

    def test_bare_result_has_all_optional_fields_none(self):
        result = ReconciliationResult(status="failed")

        assert result.expected_cents is None
        assert result.billed_cents is None
        assert result.difference_cents is None
        assert result.count_as_payable is None
        assert result.matched_po_id is None
        assert result.matched_receipt_id is None
