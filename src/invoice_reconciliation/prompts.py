"""Every prompt and response schema this project sends to a model, in one place.

Two model calls exist in this codebase:

1. **Extraction** (``EXTRACTION_PROMPT`` / ``EXTRACTION_SCHEMA``) — read one
   scanned invoice image and return its seven fields as structured output.
   Called from ``extraction.client.extract_invoice_fields``.
2. **Note drafting** (``build_note_prompt`` / ``NOTE_SCHEMA``) — phrase a
   short discrepancy note from figures the rules engine already verified.
   Called from ``reconciliation.notes.draft_discrepancy_note``. This one is
   a function of the invoice's own figures (invoice number, PO id,
   quantity, the three computed totals), not a fixed string, so it stays a
   function here rather than a constant.

Neither prompt supplies a figure the calling code did not already compute
or verify — extraction returns what is printed on the page; note-drafting
phrases totals the rules engine already produced and ``verify_note``
checks the model's text against afterwards. Moving them beside each other
is a pure readability refactor: the text each model receives is unchanged
from before this module existed.

Two hard-won lessons are recorded as comments next to the text they shaped,
because both look arbitrary without the history:

- The extraction prompt's instruction to read **by printed label, not
  position** — the fixtures use two different layouts, one of them
  two-column — and to return ``null`` for a missing purchase-order
  reference rather than guessing or transcribing a placeholder such as
  "(missing)".
- The note prompt's **deliberate omission of the unit price** — see the
  comment beside ``build_note_prompt`` below.
"""

from __future__ import annotations

from invoice_reconciliation.money import cents_to_display

__all__ = [
    "EXTRACTION_PROMPT",
    "EXTRACTION_SCHEMA",
    "NOTE_TOOL_NAME",
    "NOTE_SCHEMA",
    "build_note_prompt",
]

# ---------------------------------------------------------------------------
# Extraction: read one invoice image, return its seven fields.
# ---------------------------------------------------------------------------
#
# Two layouts exist among the supplied fixtures. Layout ``a`` prints one
# labelled field per line. Layout ``b`` prints two labelled fields per
# line, left and right columns. The prompt does not branch on layout — it
# tells the model to read every field **by its printed label**, never by
# position on the page or line, so the same prompt and the same parsing
# path handle both layouts without the code needing to know which one it
# is looking at.
#
# The purchase-order field is sometimes printed with a placeholder in
# place of a real reference (observed: the literal text "(missing)", also
# "N/A", "none", or a blank/dash). The prompt states explicitly that a
# placeholder is not a value: the model must recognise it and return
# ``null``, not transcribe the placeholder text as if it were a real PO
# identifier.
EXTRACTION_PROMPT = """\
You are reading one scanned invoice image. Extract exactly seven fields \
and return them in the structured format requested.

Read every field by its printed label, not by its position on the page \
or its order on the line. Some invoices print two labelled fields on the \
same line, side by side (for example "Supplier: S1" on the left and \
"Purchase order: PO-2" on the right of the same line). Match each value \
to the label immediately to its left — never assume the first value on a \
line belongs to the first field you are looking for.

The seven fields, by their printed label:
- invoice_number: the invoice number (e.g. "INV-1")
- supplier_id: the supplier identifier (e.g. "S1")
- po_id: the purchase-order identifier (e.g. "PO-1")
- sku: the item/SKU identifier (e.g. "CAB-1")
- quantity: the billed quantity, as a whole number
- unit_price: the unit price in US dollars, as a plain decimal string \
with exactly two decimal places (e.g. "24.00") — no currency symbol, no \
thousands separator
- total: the total amount in US dollars, in the same plain decimal-string \
form (e.g. "120.00")

Purchase-order field — read carefully: some invoices print no real \
purchase-order reference. In that case the printed value next to the \
"Purchase order" label is a placeholder, not an identifier — for example \
the literal text "(missing)", "N/A", "none", "-", or a blank. When the \
printed value is a placeholder rather than a real identifier like "PO-1" \
or "PO-2", set po_id to null. Do not transcribe the placeholder text as \
the value, and do not guess or invent a purchase-order identifier that is \
not printed on the invoice.
"""

# The seven-field JSON Schema the extraction call constrains its response
# to. Two defences live here, both measured, not theoretical:
#
# 1. ``unit_price`` and ``total`` are typed ``"string"``. Three identical
#    calls on ``wrong-price.png`` returned a JSON number (``24.0``) once
#    and a JSON string (``"24.00"``) twice. A float reaching
#    ``s.split(".")`` raises ``AttributeError``. Typing the field
#    ``"string"`` removes the variance at the source; ``money.
#    dollars_to_cents`` (the only other defence) still accepts a number,
#    in case a model ignores the schema.
# 2. ``po_id`` is typed ``["string", "null"]`` and listed in ``required``.
#    The model must emit the key on every call, and ``null`` is a valid
#    answer — this is what lets ``missing-reference`` extract as "no
#    purchase-order reference" rather than silently omitting the field.
EXTRACTION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "invoice_number": {
            "type": "string",
            "description": "The invoice number, exactly as printed (e.g. 'INV-1').",
        },
        "supplier_id": {
            "type": "string",
            "description": "The supplier identifier, exactly as printed (e.g. 'S1').",
        },
        "po_id": {
            "type": ["string", "null"],
            "description": (
                "The purchase-order identifier, exactly as printed (e.g. "
                "'PO-1'). null if no real purchase-order reference is "
                "printed on the invoice — including when the printed "
                "value is a placeholder such as '(missing)', 'N/A', "
                "'none', or a blank/dash. Never invent or guess a value."
            ),
        },
        "sku": {
            "type": "string",
            "description": "The item/SKU identifier, exactly as printed (e.g. 'CAB-1').",
        },
        "quantity": {
            "type": "integer",
            "description": "The billed quantity, as a whole number.",
        },
        "unit_price": {
            "type": "string",
            "description": (
                "The unit price in US dollars, as a plain decimal string "
                "with exactly two decimal places, e.g. '24.00'. Digits "
                "only plus one decimal point — no currency symbol, no "
                "thousands separator."
            ),
        },
        "total": {
            "type": "string",
            "description": (
                "The total amount in US dollars, as a plain decimal "
                "string with exactly two decimal places, e.g. '120.00'. "
                "Digits only plus one decimal point — no currency "
                "symbol, no thousands separator."
            ),
        },
    },
    "required": [
        "invoice_number",
        "supplier_id",
        "po_id",
        "sku",
        "quantity",
        "unit_price",
        "total",
    ],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Note drafting: phrase a short discrepancy note from verified figures.
# ---------------------------------------------------------------------------

NOTE_TOOL_NAME = "record_discrepancy_note"

# The tool-call schema the model's response is constrained to. Mirrors
# EXTRACTION_SCHEMA's forced-single-tool-call pattern: classic Bedrock's
# invoke_model has no structured-output parameter, so a forced tool call
# is how the response shape is constrained here too.
NOTE_SCHEMA: dict = {
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


def build_note_prompt(
    *,
    invoice_number: str,
    po_id: str,
    quantity: int,
    unit_cents: int,
    expected_cents: int,
    billed_cents: int,
    difference_cents: int,
    rejection_reason: str | None,
) -> str:
    """Build the note-drafting prompt text for one attempt.

    The model phrases; it never supplies a figure. The prompt states every
    figure and identifier as already-verified source material and asks
    only for a factual paragraph using them — the same figures
    ``notes.draft_note`` would render, so a model that follows instructions
    produces text ``notes.verify_note`` accepts on the first attempt.
    ``rejection_reason`` (``None`` on the first attempt) is appended as an
    explicit instruction on retry, per the owner's decision that a blind
    retry at temperature 0 tends to reproduce the same rejected output.

    ``unit_cents`` stays in the signature (callers pass the same figures
    they pass to ``draft_note``) but is deliberately absent from the
    prompt text below. It is a true fact about the invoice, but it is not
    one of the three verified totals (expected, billed, difference), so a
    note that mentions it fails ``verify_note``. Listing it and then
    forbidding it was a self-contradicting prompt, and the model used it
    on roughly half of first attempts, forcing a retry. Since removing it,
    all three discrepant invoices draft successfully on attempt 1.
    """
    expected_display = cents_to_display(expected_cents)
    billed_display = cents_to_display(billed_cents)
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
        f"- Agreed quantity: {quantity} units\n"
        + (
            f"- The billed amount is ${difference_display} {direction} the "
            "agreed amount.\n"
            if direction is not None
            else "- The billed amount matches the agreed amount exactly "
            "(no difference).\n"
        )
        + "\nThe ONLY three dollar figures you may write are:\n"
        f"  agreed (expected) total  ${expected_display}\n"
        f"  billed total             ${billed_display}\n"
        + (
            f"  difference               ${difference_display}\n"
            if direction is not None
            else "  difference               none\n"
        )
        # The unit price is deliberately withheld. It is a true fact about
        # the invoice but it is not one of the three verified totals, so a
        # note mentioning it fails verification. Supplying it and then
        # forbidding it made the model use it on roughly half of first
        # attempts — a self-contradicting prompt, not a model error.
        + "\nDo not write any other dollar amount. In particular do not "
        "write a per-unit price: you have not been given one, so do not "
        "state or derive one.\n"
        "\nRules:\n"
        "- State what was billed against what was agreed, and the "
        "difference (or that the amounts match).\n"
        "- Name both the invoice number and the purchase-order id, exactly "
        "as given above.\n"
        "- Never state or imply a cause (do not use words like error, "
        "mistake, overcharge, fraud) and never assign intent (do not use "
        "words like wrongly, deliberately, incorrectly, negligent). State "
        "only that the figures differ, not why.\n"
        "- Write each figure exactly as given above, with two decimal "
        'places (write "$100.00", never "$100" or "about $100").\n'
        "\nExample of the required shape, for a different invoice whose "
        "agreed total was $50.00, billed total $75.00 and difference "
        "$25.00:\n"
        '  "Invoice INV-9, matched to purchase order PO-7, bills $75.00 '
        'against an agreed $50.00. The billed amount is $25.00 above the '
        'agreed amount."\n'
        "Note that this example states three dollar figures and no "
        "per-unit price.\n"
    )
    if rejection_reason is not None:
        prompt += (
            "\nYour previous attempt was rejected because it "
            f"{rejection_reason}. Write a new paragraph that avoids this "
            "problem, following all the rules above.\n"
        )

    return prompt
