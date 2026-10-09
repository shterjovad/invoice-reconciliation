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

The model-drafting tests below (``draft_discrepancy_note``) mock the
model at the one seam ``notes.py`` imports it through —
``invoice_reconciliation.extraction.client.build_client`` — so none of
them make a live call. A fake client's ``invoke_model`` returns a
canned, already-JSON-encoded Bedrock response body, exactly the shape
``extraction.client.extract_invoice_fields`` already returns from a real
call, so this test suite and the production code read the same response
shape.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from invoice_reconciliation.reconciliation.notes import (
    DraftedNote,
    draft_discrepancy_note,
    draft_note,
    verify_note,
)

# The hand-calculated wrong-price figures, reused across the
# draft_discrepancy_note tests below so every test starts from the same
# verified inputs the rules engine would have produced.
_WRONG_PRICE_FIGURES = dict(
    invoice_number="INV-2",
    po_id="PO-2",
    quantity=5,
    unit_cents=2000,
    expected_cents=10000,
    billed_cents=12000,
    difference_cents=2000,
)


def _fake_body(note_text: str) -> MagicMock:
    """A fake ``invoke_model`` response ``body`` stream-alike: its
    ``.read()`` returns the JSON-encoded Bedrock response bytes a real
    call would return, carrying one tool_use block with the given note
    text — the same shape ``_extract_note_text`` parses in production.
    """
    payload = {
        "content": [
            {
                "type": "tool_use",
                "name": "record_discrepancy_note",
                "input": {"note": note_text},
            }
        ]
    }
    body = MagicMock()
    body.read.return_value = json.dumps(payload).encode("utf-8")
    return body


def _fake_client(note_texts: list[str]) -> MagicMock:
    """A fake boto3 ``bedrock-runtime`` client whose ``invoke_model``
    returns one canned response per call, in order — the Nth call in a
    retry sequence returns ``note_texts[N - 1]``."""
    client = MagicMock()
    client.invoke_model.side_effect = [
        {"body": _fake_body(text)} for text in note_texts
    ]
    return client

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


# ---------------------------------------------------------------------------
# verify_note: the pure fact-checker the model path runs every draft through
# ---------------------------------------------------------------------------


def test_verify_note_accepts_a_factually_correct_draft() -> None:
    # A stiff but factually correct sentence must pass — verify_note checks
    # facts, not style.
    text = (
        "Invoice INV-2, matched to purchase order PO-2, was billed at "
        "$120.00. The agreed amount for 5 units at $20.00 each was "
        "$100.00. The billed amount is $20.00 above the agreed amount."
    )
    assert verify_note(text, **_WRONG_PRICE_FIGURES) is None


def test_verify_note_rejects_a_wrong_dollar_figure() -> None:
    # The trap this project exists to avoid at the money layer, now at the
    # note layer: $12.00 where $120.00 was computed must be caught, not
    # silently accepted because *some* dollar sign is present.
    text = (
        "Invoice INV-2 (purchase order PO-2) bills $12.00 against an "
        "agreed $100.00 for 5 units at $20.00 each. The billed amount is "
        "$20.00 above the agreed amount."
    )
    reason = verify_note(text, **_WRONG_PRICE_FIGURES)
    assert reason is not None
    assert "$12.00" in reason


def test_verify_note_rejects_the_po_id_off_by_one_trap() -> None:
    # PO-1 and PO-2 differ only by ID — the exact trap this project exists
    # to avoid. A draft naming the wrong one must be rejected even though
    # every other fact in the sentence is correct.
    text = (
        "Invoice INV-2 (purchase order PO-1) bills $120.00 against an "
        "agreed $100.00 for 5 units at $20.00 each. The billed amount is "
        "$20.00 above the agreed amount."
    )
    reason = verify_note(text, **_WRONG_PRICE_FIGURES)
    assert reason is not None
    assert "PO-2" in reason


def test_verify_note_rejects_a_wrong_direction_word() -> None:
    # difference_cents is +2000 (an overbill) — "below" is the wrong
    # direction and must be rejected even though every figure is correct.
    text = (
        "Invoice INV-2 (purchase order PO-2) bills $120.00 against an "
        "agreed $100.00 for 5 units at $20.00 each. The billed amount is "
        "$20.00 below the agreed amount."
    )
    reason = verify_note(text, **_WRONG_PRICE_FIGURES)
    assert reason is not None
    assert "below" in reason


def test_verify_note_rejects_rounded_or_approximated_figures() -> None:
    # "about $20" is not the computed "$20.00" — no exact substring match,
    # so this must be rejected rather than accepted as "close enough".
    text = (
        "Invoice INV-2 (purchase order PO-2) bills $120.00 against an "
        "agreed $100.00 for 5 units at $20.00 each. The billed amount is "
        "about $20 above the agreed amount."
    )
    reason = verify_note(text, **_WRONG_PRICE_FIGURES)
    assert reason is not None


def test_verify_note_rejects_accusatory_wording_even_with_correct_figures() -> None:
    text = (
        "Invoice INV-2 (purchase order PO-2) was overcharged: billed "
        "$120.00 against an agreed $100.00 for 5 units at $20.00 each. "
        "The billed amount is $20.00 above the agreed amount."
    )
    reason = verify_note(text, **_WRONG_PRICE_FIGURES)
    assert reason is not None
    assert "overcharge" in reason


# ---------------------------------------------------------------------------
# draft_discrepancy_note: model path, verify-and-retry, and fallback
# ---------------------------------------------------------------------------

_CLIENT_PATH = "invoice_reconciliation.extraction.client.build_client"

_GOOD_DRAFT = (
    "Invoice INV-2 (purchase order PO-2) bills $120.00 against an agreed "
    "$100.00 for 5 units at $20.00 each. The billed amount is $20.00 "
    "above the agreed amount."
)
_BAD_DRAFT_WRONG_FIGURE = (
    "Invoice INV-2 (purchase order PO-2) bills $12.00 against an agreed "
    "$100.00 for 5 units at $20.00 each. The billed amount is $20.00 "
    "above the agreed amount."
)


def test_a_verified_model_draft_is_returned_with_drafted_by_model_and_the_model_id() -> None:
    client = _fake_client([_GOOD_DRAFT])
    with patch(_CLIENT_PATH, return_value=client) as build_client:
        result = draft_discrepancy_note(**_WRONG_PRICE_FIGURES, use_model=True)

    build_client.assert_called_once()
    assert isinstance(result, DraftedNote)
    assert result.drafted_by == "model"
    assert result.text == _GOOD_DRAFT
    assert result.model_id is not None
    assert result.attempts == 1
    assert result.rejection_reasons == []
    client.invoke_model.assert_called_once()


def test_a_draft_with_a_wrong_figure_is_rejected_and_retried() -> None:
    # First attempt is wrong, second attempt (after being told why) is
    # accepted — two attempts, one recorded rejection reason.
    client = _fake_client([_BAD_DRAFT_WRONG_FIGURE, _GOOD_DRAFT])
    with patch(_CLIENT_PATH, return_value=client):
        result = draft_discrepancy_note(**_WRONG_PRICE_FIGURES, use_model=True)

    assert result.drafted_by == "model"
    assert result.text == _GOOD_DRAFT
    assert result.attempts == 2
    assert len(result.rejection_reasons) == 1
    assert "$12.00" in result.rejection_reasons[0]
    assert client.invoke_model.call_count == 2

    # The retry must have been told why the previous attempt failed —
    # not a blind retry at temperature 0.
    second_call_body = json.loads(client.invoke_model.call_args_list[1].kwargs["body"])
    retry_prompt = second_call_body["messages"][0]["content"]
    assert "previous attempt was rejected" in retry_prompt
    assert "$12.00" in retry_prompt


def test_after_three_failures_the_calculated_note_is_used_and_reasons_are_recorded() -> None:
    client = _fake_client(
        [_BAD_DRAFT_WRONG_FIGURE, _BAD_DRAFT_WRONG_FIGURE, _BAD_DRAFT_WRONG_FIGURE]
    )
    with patch(_CLIENT_PATH, return_value=client):
        result = draft_discrepancy_note(**_WRONG_PRICE_FIGURES, use_model=True)

    expected_calculated = draft_note(**_WRONG_PRICE_FIGURES)
    assert result.drafted_by == "calculated"
    assert result.text == expected_calculated
    assert result.model_id is None
    assert result.attempts == 3
    assert len(result.rejection_reasons) == 3
    assert client.invoke_model.call_count == 3


def test_use_model_false_never_builds_a_client_and_returns_calculated() -> None:
    with patch(_CLIENT_PATH) as build_client:
        result = draft_discrepancy_note(**_WRONG_PRICE_FIGURES, use_model=False)

    build_client.assert_not_called()
    assert result.drafted_by == "calculated"
    assert result.model_id is None
    assert result.attempts == 0
    assert result.rejection_reasons == []


def test_a_client_construction_failure_falls_back_to_calculated_without_raising() -> None:
    with patch(_CLIENT_PATH, side_effect=RuntimeError("no credentials")):
        result = draft_discrepancy_note(**_WRONG_PRICE_FIGURES, use_model=True)

    assert result.drafted_by == "calculated"
    assert result.model_id is None


def test_no_cause_words_reach_the_stored_text_on_either_path() -> None:
    # Calculated path: proven by the existing _assert_no_accusatory_wording
    # tests above. Model path: an accusatory draft must be rejected and
    # retried, never stored as-is.
    accusatory_draft = (
        "Invoice INV-2 (purchase order PO-2) was overcharged by $20.00: "
        "billed $120.00 against an agreed $100.00 for 5 units at $20.00 "
        "each."
    )
    client = _fake_client([accusatory_draft, accusatory_draft, accusatory_draft])
    with patch(_CLIENT_PATH, return_value=client):
        result = draft_discrepancy_note(**_WRONG_PRICE_FIGURES, use_model=True)

    assert result.drafted_by == "calculated"
    _assert_no_accusatory_wording(result.text)
    for reason in result.rejection_reasons:
        assert "overcharge" in reason
