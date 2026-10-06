"""Draft a discrepancy note for one invoice.

One pure function, ``draft_note``, in the same style as ``rules.reconcile``:
it takes the verified figures already computed by the rules engine and
returns text. It performs no database access and raises nothing on the
inputs this product produces — the route that calls it (``POST
/invoices/{id}/note`` reads the note, and the detail route reads it for
display) owns persistence, not this module.

**State no cause and suggest no wrongdoing.** The note names the invoice
number and the purchase-order reference, states what was billed against
what was agreed, and gives the difference. It never names a cause (a
"wrong price", an "overcharge", an "error") and never assigns intent
("mistakenly", "incorrectly"): the reconciliation result tells us the
billed amount differs from the agreed amount, not why, and a note that
presumed a cause could read as an accusation of the supplier. Whether a
quantity differs or a unit price differs, the note describes the same
thing — a difference between billed and agreed — without naming which.
"""

from __future__ import annotations

from invoice_reconciliation.money import cents_to_display

__all__ = ["draft_note"]


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
