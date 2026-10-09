"""The note states where a difference comes from: price, quantity or both.

Brief, Requirements > Analytics & Insights: "Summarize the supported price
or quantity differences and their amounts".
"""

from __future__ import annotations

import pytest

from invoice_reconciliation.prompts import build_note_prompt
from invoice_reconciliation.reconciliation.difference_source import difference_source
from invoice_reconciliation.reconciliation.notes import draft_note, verify_note

# The three discrepant seeded invoices, figures from seed.json.
WRONG_PRICE = dict(billed_unit_cents=2400, agreed_unit_cents=2000,
                   billed_quantity=5, ordered_quantity=5, received_quantity=5)
QUANTITY_OVERBILL = dict(billed_unit_cents=2000, agreed_unit_cents=2000,
                         billed_quantity=8, ordered_quantity=5, received_quantity=5)
UNDERCHARGE = dict(billed_unit_cents=1800, agreed_unit_cents=2000,
                   billed_quantity=5, ordered_quantity=5, received_quantity=5)

TOTALS = {
    "wrong-price": dict(invoice_number="INV-2", po_id="PO-2", quantity=5, unit_cents=2000,
                        expected_cents=10000, billed_cents=12000, difference_cents=2000),
    "quantity-overbill": dict(invoice_number="INV-4", po_id="PO-1", quantity=5, unit_cents=2000,
                              expected_cents=10000, billed_cents=16000, difference_cents=6000),
    "undercharge": dict(invoice_number="INV-5", po_id="PO-2", quantity=5, unit_cents=2000,
                        expected_cents=10000, billed_cents=9000, difference_cents=-1000),
}


@pytest.mark.parametrize(
    "inputs, price, quantity",
    [(WRONG_PRICE, True, False), (QUANTITY_OVERBILL, False, True), (UNDERCHARGE, True, False)],
    ids=["wrong-price", "quantity-overbill", "undercharge"],
)
def test_difference_source_names_the_right_cause(inputs, price, quantity) -> None:
    source = difference_source(**inputs)
    assert source is not None
    assert source.price_differs is price
    assert source.quantity_differs is quantity


@pytest.mark.parametrize(
    "case, inputs, sentence",
    [
        ("wrong-price", WRONG_PRICE,
         "The billed unit price is $24.00 against an agreed $20.00 per unit."),
        ("quantity-overbill", QUANTITY_OVERBILL,
         "The invoice bills 8 units against 5 ordered and 5 received."),
        ("undercharge", UNDERCHARGE,
         "The billed unit price is $18.00 against an agreed $20.00 per unit."),
    ],
)
def test_calculated_note_states_the_source(case, inputs, sentence) -> None:
    text = draft_note(**TOTALS[case], source=difference_source(**inputs))
    assert sentence in text


def test_a_missing_input_gives_no_source() -> None:
    assert difference_source(**{**WRONG_PRICE, "billed_unit_cents": None}) is None


def test_verify_accepts_a_correct_source_sentence() -> None:
    text = (
        "Invoice INV-5, matched to purchase order PO-2, bills $90.00 against an "
        "agreed $100.00. The billed amount is $10.00 below the agreed amount. "
        "The billed unit price is $18.00 against an agreed $20.00 per unit."
    )
    assert verify_note(text, **TOTALS["undercharge"], source=difference_source(**UNDERCHARGE)) is None


def test_verify_rejects_a_price_note_that_claims_a_quantity_difference() -> None:
    text = (
        "Invoice INV-5, matched to purchase order PO-2, bills $90.00 against an "
        "agreed $100.00. The billed amount is $10.00 below the agreed amount. "
        "The invoice bills 4 units against 5 ordered."
    )
    reason = verify_note(text, **TOTALS["undercharge"], source=difference_source(**UNDERCHARGE))
    assert reason is not None


def test_verify_rejects_a_quantity_note_that_omits_the_quantity() -> None:
    text = (
        "Invoice INV-4, matched to purchase order PO-1, bills $160.00 against an "
        "agreed $100.00. The billed amount is $60.00 above the agreed amount."
    )
    reason = verify_note(
        text, **TOTALS["quantity-overbill"], source=difference_source(**QUANTITY_OVERBILL)
    )
    assert reason is not None and "quantity difference" in reason


def test_verify_rejects_an_invented_unit_price() -> None:
    text = (
        "Invoice INV-2, matched to purchase order PO-2, bills $120.00 against an "
        "agreed $100.00. The billed amount is $20.00 above the agreed amount. "
        "The billed unit price is $25.00 against an agreed $20.00 per unit."
    )
    reason = verify_note(text, **TOTALS["wrong-price"], source=difference_source(**WRONG_PRICE))
    assert reason is not None and "$25.00" in reason


def test_prompt_lists_the_unit_prices_as_permitted_when_a_source_is_given() -> None:
    prompt = build_note_prompt(**TOTALS["undercharge"], rejection_reason=None,
                               source=difference_source(**UNDERCHARGE))
    assert "billed unit price        $18.00" in prompt
    assert "agreed unit price        $20.00" in prompt
    assert "In particular do not write a per-unit price" not in prompt


def test_prompt_without_a_source_still_forbids_a_unit_price() -> None:
    prompt = build_note_prompt(**TOTALS["undercharge"], rejection_reason=None)
    assert "In particular do not write a per-unit price" in prompt
