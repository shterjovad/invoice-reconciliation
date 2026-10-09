"""Draft a discrepancy note for one invoice.

Two layers:

1. ``draft_note`` — one pure function, in the same style as
   ``rules.reconcile``: it takes the verified figures already computed by
   the rules engine and returns text. It performs no database access and
   raises nothing on the inputs this product produces. This is the
   **calculated** path: an f-string, never a model call, used as the
   fallback whenever a model draft is unavailable or cannot be verified.
2. ``draft_discrepancy_note`` — the model-drafting orchestration the
   functional brief asks for ("use the model to draft a short discrepancy
   note from verified findings and source references"). It asks the
   model to *phrase* a note from the same verified figures, checks the
   returned text against those figures (``verify_note``), retries up to
   three times with the rejection reason fed back on failure, and falls
   back to ``draft_note`` if every attempt is rejected. The model never
   supplies a figure — every number in the returned
   ``DraftedNote.text`` is one ``draft_note`` could have produced from
   the same inputs, because ``verify_note`` rejects any text that
   contains a different one.

**State no cause and suggest no wrongdoing.** Both paths name the invoice
number and the purchase-order reference, state what was billed against
what was agreed, and give the difference. Neither names a cause (a
"wrong price", an "overcharge", an "error") or assigns intent
("mistakenly", "incorrectly"): the reconciliation result tells us the
billed amount differs from the agreed amount, not why, and a note that
presumed a cause could read as an accusation of the supplier. Whether a
quantity differs or a unit price differs, the note describes the same
thing — a difference between billed and agreed — without naming which.
``verify_note`` enforces this on the model path with the same word list
``tests/unit/test_notes.py`` asserts against.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from invoice_reconciliation.money import cents_to_display

__all__ = [
    "draft_note",
    "DraftedNote",
    "verify_note",
    "draft_discrepancy_note",
]

logger = logging.getLogger(__name__)

# Cause/blame wording the model draft must never contain, whatever figures
# it carries — the same list tests/unit/test_notes.py asserts the
# calculated path never produces. Checked case-insensitively, as a plain
# substring test (matching the test file's own ``in lowered`` check), not
# a word-boundary regex: a model draft containing "overcharged" inside a
# longer word is just as much an accusation as the bare word.
_ACCUSATORY_WORDS: tuple[str, ...] = (
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

_MAX_ATTEMPTS = 3
_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
_ANTHROPIC_VERSION = "bedrock-2023-05-31"
_TOOL_NAME = "record_discrepancy_note"
_NOTE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "note": {
            "type": "string",
            "description": (
                "A short, factual paragraph describing the discrepancy, "
                "using only the figures and identifiers supplied."
            ),
        },
    },
    "required": ["note"],
    "additionalProperties": False,
}

# Matches a dollar figure such as "$20.00" or "$120" — used only to find
# candidate amounts to check against the computed set in verify_note, not
# to validate money formatting itself (that remains money.py's job).
_DOLLAR_AMOUNT_RE = re.compile(r"\$(\d[\d,]*(?:\.\d+)?)")


def draft_note(
    *,
    invoice_number: str,
    po_id: str,
    quantity: int,
    unit_cents: int,
    expected_cents: int,
    billed_cents: int,
    difference_cents: int,
) -> str:
    """Draft a short note for a discrepant invoice from its verified figures.

    Args:
        invoice_number: the invoice's own number (e.g. ``"INV-2"``), not
            the internal ``invoice_id``.
        po_id: the matched purchase order's id (e.g. ``"PO-2"``).
        quantity: the purchase order's agreed (ordered) quantity.
        unit_cents: the purchase order's agreed unit price, in cents.
        expected_cents: ``quantity * unit_cents`` — the agreed total.
        billed_cents: the invoice's billed total, in cents.
        difference_cents: ``billed_cents - expected_cents``, signed.

    Returns:
        A short paragraph of plain text: no markup, no send control, no
        stated cause. Figures are rendered through
        ``money.cents_to_display``, never a hand-built cents/100 string.

    This function assumes its caller already resolved the invoice to
    ``discrepant`` with a matched purchase order — it does not re-derive or
    check that classification itself.
    """
    expected_display = cents_to_display(expected_cents)
    billed_display = cents_to_display(billed_cents)
    unit_display = cents_to_display(unit_cents)
    difference_display = cents_to_display(abs(difference_cents))

    if difference_cents > 0:
        difference_sentence = (
            f"The billed amount is ${difference_display} above the agreed amount."
        )
    elif difference_cents < 0:
        difference_sentence = (
            f"The billed amount is ${difference_display} below the agreed amount."
        )
    else:
        # A quantity mismatch can still be discrepant with zero amount
        # difference (domain.md step 5) — e.g. the billed quantity differs
        # from both the ordered and received quantity, but at a unit price
        # that happens to make the two totals equal. The note still states
        # the figures agree in total, rather than forcing a direction where
        # none exists.
        difference_sentence = "The billed amount matches the agreed amount."

    return (
        f"Invoice {invoice_number} (matched to purchase order {po_id}) "
        f"bills ${billed_display} against an agreed ${expected_display} "
        f"for {quantity} units at ${unit_display} each. "
        f"{difference_sentence}"
    )


@dataclass(frozen=True, slots=True)
class DraftedNote:
    """The outcome of ``draft_discrepancy_note``: the text to store, and
    how it was produced.

    ``drafted_by`` is ``"model"`` only when a model draft was returned and
    passed ``verify_note``; ``"calculated"`` whenever the model was never
    called, raised, or every attempt was rejected. ``model_id`` is
    ``None`` on the calculated path — no model produced the stored text,
    so there is nothing to name. ``attempts`` counts drafting attempts
    actually made (0 when the model path was skipped entirely, e.g. no
    credentials). ``rejection_reasons`` lists why each rejected attempt
    failed, oldest first, empty when no attempt was rejected.
    """

    text: str
    drafted_by: str
    model_id: str | None
    attempts: int
    rejection_reasons: list[str] = field(default_factory=list)


def verify_note(
    text: str,
    *,
    invoice_number: str,
    po_id: str,
    quantity: int,
    unit_cents: int,
    expected_cents: int,
    billed_cents: int,
    difference_cents: int,
) -> str | None:
    """Check a model-drafted note against the verified figures.

    Returns ``None`` when the draft passes every check. Returns a short,
    human-readable reason for the *first* failing check otherwise — fed
    back to the model on retry, and recorded in ``rejection_reasons`` on
    final failure. Verification rejects the **whole draft** on any single
    failure; it never tries to salvage or patch the text.

    Checks, in order:

    1. **No cause or blame wording** — the same list
       ``tests/unit/test_notes.py`` asserts the calculated path never
       produces (an overcharge is not a mistake, an error, or fraud; see
       module docstring).
    2. **Every dollar figure in the text is one we computed.** The three
       computed amounts (``expected``, ``billed``, and the unsigned
       difference) are rendered through ``money.cents_to_display`` — the
       same renderer ``draft_note`` uses — and every ``$<number>``
       substring the draft contains must be one of those three. A figure
       we did not compute (the trap case: ``$12.00`` where we computed
       ``$120.00``) is rejected, and so is a rounded or approximated
       figure (``$20`` for an exact ``$20.00``), since it will not appear
       as an exact substring.
    3. **The invoice number and PO id are the matched ones.** Rejects a
       draft naming a different invoice or purchase order — including the
       one-digit-apart case (``PO-1`` when ``PO-2`` matched) this project
       exists to avoid, since a substring check for the *wrong* id cannot
       pass while the *right* id's exact text is required present.
    4. **The direction word matches the sign of the difference.** A
       positive ``difference_cents`` means the model must not say the
       billed amount is *below* or *under* the agreed amount, and a
       negative one must not say *above* or *over*. A zero difference
       must not claim either direction.

    This function does not check prose quality or sentence structure — a
    stiff but factually correct sentence passes. It checks facts only.
    """
    lowered = text.lower()

    for word in _ACCUSATORY_WORDS:
        if word in lowered:
            return f"used accusatory wording {word!r}, which is never permitted"

    expected_display = cents_to_display(expected_cents)
    billed_display = cents_to_display(billed_cents)
    difference_display = cents_to_display(abs(difference_cents))
    computed_amounts = {expected_display, billed_display, difference_display}

    for dollar_match in _DOLLAR_AMOUNT_RE.finditer(text):
        amount = dollar_match.group(1)
        if amount not in computed_amounts:
            return (
                f"contains a dollar figure ${amount} that was not one of the "
                f"computed figures (${expected_display}, ${billed_display}, "
                f"${difference_display})"
            )

    if invoice_number not in text:
        return f"did not name the matched invoice number {invoice_number!r}"
    if po_id not in text:
        return f"did not name the matched purchase order {po_id!r}"

    if difference_cents > 0:
        for word in ("below", "under", "undercharge", "undercharged"):
            if word in lowered:
                return (
                    f"used direction word {word!r}, but the billed amount is "
                    "above the agreed amount, not below"
                )
    elif difference_cents < 0:
        for word in ("above", "over", "overbill", "overbilled"):
            if word in lowered:
                return (
                    f"used direction word {word!r}, but the billed amount is "
                    "below the agreed amount, not above"
                )
    else:
        for word in ("above", "below", "over", "under"):
            if word in lowered:
                return (
                    f"used direction word {word!r}, but the billed amount "
                    "matches the agreed amount exactly — no direction applies"
                )

    return None


def _build_tool_call_body(
    *,
    invoice_number: str,
    po_id: str,
    quantity: int,
    unit_cents: int,
    expected_cents: int,
    billed_cents: int,
    difference_cents: int,
    rejection_reason: str | None,
) -> dict:
    """Build the Bedrock ``invoke_model`` request body for one drafting attempt.

    The model phrases; it never supplies a figure. The prompt states every
    figure and identifier as already-verified source material and asks
    only for a factual paragraph using them — the same figures
    ``draft_note`` would render, so a model that follows instructions
    produces text ``verify_note`` accepts on the first attempt.
    ``rejection_reason`` (``None`` on the first attempt) is appended as an
    explicit instruction on retry, per the owner's decision that a blind
    retry at temperature 0 tends to reproduce the same rejected output.
    """
    expected_display = cents_to_display(expected_cents)
    billed_display = cents_to_display(billed_cents)
    unit_display = cents_to_display(unit_cents)
    difference_display = cents_to_display(abs(difference_cents))
    direction = (
        "above" if difference_cents > 0 else "below" if difference_cents < 0 else None
    )

    prompt = (
        "Write one short, factual paragraph describing a discrepant invoice, "
        "for a reviewer who will decide what to do about it. Use only the "
        "figures and identifiers below — do not compute, round, or restate "
        "them differently, and do not add any figure not listed here.\n\n"
        f"- Invoice number: {invoice_number}\n"
        f"- Matched purchase order: {po_id}\n"
        f"- Agreed quantity: {quantity} units at ${unit_display} each\n"
        f"- Agreed (expected) total: ${expected_display}\n"
        f"- Billed total: ${billed_display}\n"
        + (
            f"- The billed amount is ${difference_display} {direction} the "
            "agreed amount.\n"
            if direction is not None
            else "- The billed amount matches the agreed amount exactly "
            "(no difference).\n"
        )
        + "\nRules:\n"
        "- State what was billed against what was agreed, and the "
        "difference (or that the amounts match).\n"
        "- Name both the invoice number and the purchase-order id, exactly "
        "as given above.\n"
        "- Never state or imply a cause (do not use words like error, "
        "mistake, overcharge, fraud) and never assign intent (do not use "
        "words like wrongly, deliberately, incorrectly, negligent). State "
        "only that the figures differ, not why.\n"
        "- Use only the dollar figures listed above, written exactly as "
        "given (e.g. if the agreed total is $100.00, write \"$100.00\", "
        "never \"$100\" or \"about $100\").\n"
    )
    if rejection_reason is not None:
        prompt += (
            "\nYour previous attempt was rejected because it "
            f"{rejection_reason}. Write a new paragraph that avoids this "
            "problem, following all the rules above.\n"
        )

    tool = {
        "name": _TOOL_NAME,
        "description": "Record the drafted discrepancy note.",
        "input_schema": _NOTE_SCHEMA,
    }

    return {
        "anthropic_version": _ANTHROPIC_VERSION,
        "max_tokens": 512,
        "temperature": 0.0,
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": _TOOL_NAME},
        "messages": [{"role": "user", "content": prompt}],
    }


def _extract_note_text(raw_response: dict) -> str:
    """Pull the drafted note text out of a raw Bedrock response.

    Raises ``ValueError`` if the response carries no usable tool-use
    block — treated by ``draft_discrepancy_note`` as one failed attempt
    with that message as the rejection reason, not as a crash: a
    malformed response is exactly as retriable as a verification failure.
    """
    content = raw_response.get("content") if isinstance(raw_response, dict) else None
    if not content:
        raise ValueError("response has no content blocks")

    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tool_input = block.get("input")
            if isinstance(tool_input, dict) and isinstance(tool_input.get("note"), str):
                return tool_input["note"]
            raise ValueError("tool_use block has no string 'note' input")

    raise ValueError("response has no tool_use content block")


def draft_discrepancy_note(
    *,
    invoice_number: str,
    po_id: str,
    quantity: int,
    unit_cents: int,
    expected_cents: int,
    billed_cents: int,
    difference_cents: int,
    use_model: bool = True,
) -> DraftedNote:
    """Draft a discrepancy note, preferring a verified model draft.

    Flow (technical-considerations.md's discrepancy-note slice):

        rules engine figures --> model drafts --> verify
                                      ^              |
                                      |          pass -> drafted_by='model'
                                 retry (<=3,          |
                                 told why)        fail -> retry
                                      |
                             3 fails -> drafted_by='calculated'

    The model is asked for a draft up to three times
    (``_MAX_ATTEMPTS``). Each returned draft is checked with
    ``verify_note`` against the same figures the caller supplies — the
    model never supplies a figure of its own. The first attempt carries
    no rejection reason; every retry carries the previous attempt's
    rejection reason, since a blind retry at temperature 0 tends to
    reproduce the same output. After three rejected (or failed) attempts,
    or when ``use_model`` is ``False``, or when the model client cannot
    be built or called at all (no AWS credentials, a network error, a
    malformed response), this function falls back to the pure
    ``draft_note`` and returns ``drafted_by="calculated"`` — it never
    raises for any of these cases, since a reviewer must always get a
    usable note.

    The import of ``extraction.client`` is lazy (inside this function),
    matching that module's own lazy-``boto3`` discipline, so importing
    this module — or calling it with ``use_model=False`` — never requires
    the AWS credential chain to resolve.
    """
    calculated_text = draft_note(
        invoice_number=invoice_number,
        po_id=po_id,
        quantity=quantity,
        unit_cents=unit_cents,
        expected_cents=expected_cents,
        billed_cents=billed_cents,
        difference_cents=difference_cents,
    )

    if not use_model:
        return DraftedNote(
            text=calculated_text,
            drafted_by="calculated",
            model_id=None,
            attempts=0,
            rejection_reasons=[],
        )

    try:
        # Lazy import: calling this module never requires boto3 or AWS
        # credentials unless a live draft is actually attempted.
        from invoice_reconciliation.extraction.client import build_client
        from invoice_reconciliation.config import ModelConfig

        config = ModelConfig(model_id=_MODEL_ID)
        client = build_client(config)
    except Exception as exc:  # noqa: BLE001 - any client-construction failure falls back
        logger.warning(
            "could not build the Bedrock client for note drafting (%s: %s); "
            "falling back to the calculated note",
            type(exc).__name__,
            exc,
        )
        return DraftedNote(
            text=calculated_text,
            drafted_by="calculated",
            model_id=None,
            attempts=0,
            rejection_reasons=[],
        )

    rejection_reasons: list[str] = []
    rejection_reason: str | None = None

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        body = _build_tool_call_body(
            invoice_number=invoice_number,
            po_id=po_id,
            quantity=quantity,
            unit_cents=unit_cents,
            expected_cents=expected_cents,
            billed_cents=billed_cents,
            difference_cents=difference_cents,
            rejection_reason=rejection_reason,
        )
        try:
            raw_response = client.invoke_model(modelId=_MODEL_ID, body=json.dumps(body))
            response_body = json.loads(raw_response["body"].read())
            draft_text = _extract_note_text(response_body)
        except Exception as exc:  # noqa: BLE001 - network/parse failure is one failed attempt
            rejection_reason = f"raised {type(exc).__name__}: {exc}"
            rejection_reasons.append(rejection_reason)
            logger.warning(
                "note-drafting attempt %d/%d for invoice %r failed: %s",
                attempt,
                _MAX_ATTEMPTS,
                invoice_number,
                rejection_reason,
            )
            continue

        rejection_reason = verify_note(
            draft_text,
            invoice_number=invoice_number,
            po_id=po_id,
            quantity=quantity,
            unit_cents=unit_cents,
            expected_cents=expected_cents,
            billed_cents=billed_cents,
            difference_cents=difference_cents,
        )
        if rejection_reason is None:
            return DraftedNote(
                text=draft_text,
                drafted_by="model",
                model_id=_MODEL_ID,
                attempts=attempt,
                rejection_reasons=rejection_reasons,
            )

        rejection_reasons.append(rejection_reason)
        logger.warning(
            "note-drafting attempt %d/%d for invoice %r rejected: %s",
            attempt,
            _MAX_ATTEMPTS,
            invoice_number,
            rejection_reason,
        )

    return DraftedNote(
        text=calculated_text,
        drafted_by="calculated",
        model_id=None,
        attempts=_MAX_ATTEMPTS,
        rejection_reasons=rejection_reasons,
    )
