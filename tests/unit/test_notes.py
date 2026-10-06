# @layer: unit
# @spec: 001-invoice-reconciliation-review
"""Unit tests for the discrepancy-note drafter
(src/invoice_reconciliation/reconciliation/notes.py).

``draft_note`` is pure, so every test here calls it directly with the
verified figures — no database, no reconciliation run. This mirrors
``test_rules.py``'s style for the equally pure ``rules.reconcile``.

The figures used below are the hand-calculated wrong-price case from
domain.md's worked example: PO-2 agrees 5 units at 2000 cents each
(expected 10000); INV-2 bills 12000 (difference +2000).
"""

from __future__ import annotations

from invoice_reconciliation.reconciliation.notes import draft_note

# Wording this function must never use, however the figures land — a
# literal cause or an assignment of intent, either of which would read as
# an accusation the product has no basis for making.
_ACCUSATORY_WORDS = (
    "overcharged",
    "overcharge",
    "error",
    "mistake",
    "fraud",
    "wrongly",
    "deliberately",
    "incorrectly",
    "negligent",
)


def _assert_no_accusatory_wording(text: str) -> None:
    lowered = text.lower()
    for word in _ACCUSATORY_WORDS:
        assert word not in lowered, f"found accusatory word {word!r} in: {text!r}"


def test_wrong_price_note_names_the_invoice_number_and_po_reference() -> None:
    text = draft_note(
        invoice_number="INV-2",
        po_id="PO-2",
        quantity=5,
        unit_cents=2000,
        expected_cents=10000,
        billed_cents=12000,
        difference_cents=2000,
    )
    assert "INV-2" in text
    assert "PO-2" in text


def test_wrong_price_note_carries_the_verified_figures() -> None:
    text = draft_note(
        invoice_number="INV-2",
        po_id="PO-2",
        quantity=5,
        unit_cents=2000,
        expected_cents=10000,
        billed_cents=12000,
        difference_cents=2000,
    )
    # Billed $120.00 against an agreed $100.00, for 5 units at $20.00 each,
    # a $20.00 difference — all rendered through money.cents_to_display,
    # never a hand-built cents/100 computation in this test either.
    assert "120.00" in text
    assert "100.00" in text
    assert "20.00" in text
    assert "5" in text


def test_wrong_price_note_contains_no_accusatory_wording() -> None:
    text = draft_note(
        invoice_number="INV-2",
        po_id="PO-2",
        quantity=5,
        unit_cents=2000,
        expected_cents=10000,
        billed_cents=12000,
        difference_cents=2000,
    )
    _assert_no_accusatory_wording(text)


def test_undercharge_note_also_names_figures_and_states_no_cause() -> None:
    """undercharge: expected $100.00, billed $90.00, difference -$10.00 —
    a negative difference must not flip into accusatory language either."""
    text = draft_note(
        invoice_number="INV-5",
        po_id="PO-2",
        quantity=5,
        unit_cents=2000,
        expected_cents=10000,
        billed_cents=9000,
        difference_cents=-1000,
    )
    assert "INV-5" in text
    assert "PO-2" in text
    assert "90.00" in text
    assert "100.00" in text
    assert "10.00" in text
    _assert_no_accusatory_wording(text)


def test_quantity_overbill_note_states_figures_without_naming_quantity_as_cause() -> None:
    """quantity-overbill: expected $100.00, billed $160.00, difference
    $60.00 — driven by a quantity mismatch, not a price difference, but
    the note states the same shape of fact either way: billed vs. agreed,
    with no cause named."""
    text = draft_note(
        invoice_number="INV-4",
        po_id="PO-1",
        quantity=5,
        unit_cents=2000,
        expected_cents=10000,
        billed_cents=16000,
        difference_cents=6000,
    )
    assert "INV-4" in text
    assert "PO-1" in text
    assert "160.00" in text
    assert "100.00" in text
    assert "60.00" in text
    _assert_no_accusatory_wording(text)


def test_a_zero_difference_discrepant_case_states_amounts_match() -> None:
    """A quantity mismatch can be discrepant with a zero amount
    difference (domain.md step 5) — the note must still render without
    forcing a spurious "above"/"below" direction."""
    text = draft_note(
        invoice_number="INV-9",
        po_id="PO-1",
        quantity=5,
        unit_cents=2000,
        expected_cents=10000,
        billed_cents=10000,
        difference_cents=0,
    )
    assert "INV-9" in text
    assert "PO-1" in text
    assert "matches the agreed amount" in text
    _assert_no_accusatory_wording(text)
