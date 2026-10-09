"""Where a discrepancy comes from: the unit price, the quantity, or both.

The brief asks the platform to "Summarize the supported price or quantity
differences and their amounts" and to "explain differences between what
was ordered, received, and billed". This module makes that comparison
once, as a pure function, so the note prompt, the note verifier and the
calculated note all state the same facts and can never disagree.

It reports facts only. It never says why a figure differs and never
suggests what value would make the difference zero: the correction
feature is for misread extractions, not for making an invoice agree with
its purchase order.
"""

from __future__ import annotations

from dataclasses import dataclass

from invoice_reconciliation.money import cents_to_display

__all__ = ["DifferenceSource", "difference_source"]


@dataclass(frozen=True, slots=True)
class DifferenceSource:
    """The invoice's billed unit price and quantity, set against the
    matched purchase order and receipt, and which of them differ."""

    billed_unit_cents: int
    agreed_unit_cents: int
    billed_quantity: int
    ordered_quantity: int
    received_quantity: int | None

    @property
    def price_differs(self) -> bool:
        return self.billed_unit_cents != self.agreed_unit_cents

    @property
    def quantity_differs(self) -> bool:
        if self.billed_quantity != self.ordered_quantity:
            return True
        return (
            self.received_quantity is not None
            and self.billed_quantity != self.received_quantity
        )

    @property
    def kind(self) -> str:
        """Which figures differ: ``price``, ``quantity``, ``price_and_quantity``,
        or ``total_only`` (both agree, but the billed total does not).

        One label per invoice. The amount is never split between price and
        quantity: the rules give no way to allocate it.
        """
        if self.price_differs and self.quantity_differs:
            return "price_and_quantity"
        if self.price_differs:
            return "price"
        if self.quantity_differs:
            return "quantity"
        return "total_only"

    @property
    def billed_unit_display(self) -> str:
        return cents_to_display(self.billed_unit_cents)

    @property
    def agreed_unit_display(self) -> str:
        return cents_to_display(self.agreed_unit_cents)

    def sentences(self) -> list[str]:
        """The factual source sentences a note states, in a fixed order."""
        out: list[str] = []
        if self.price_differs:
            out.append(
                f"The billed unit price is ${self.billed_unit_display} against "
                f"an agreed ${self.agreed_unit_display} per unit."
            )
        if self.quantity_differs:
            received = (
                f" and {self.received_quantity} received"
                if self.received_quantity is not None
                else ""
            )
            out.append(
                f"The invoice bills {self.billed_quantity} units against "
                f"{self.ordered_quantity} ordered{received}."
            )
        if not out:
            # Neither the unit price nor the quantity differs, yet the
            # invoice is discrepant: the billed total itself does not equal
            # the billed quantity times the billed unit price. State that,
            # with no figure that was not verified and no reason.
            out.append(
                "The billed unit price and quantity match the purchase order; "
                "the billed total does not equal them multiplied."
            )
        return out

    def permitted_dollar_figures(self) -> set[str]:
        """Unit prices a note may write, in addition to the three totals."""
        return {self.billed_unit_display, self.agreed_unit_display}

    def permitted_unit_counts(self) -> set[int]:
        counts = {self.billed_quantity, self.ordered_quantity}
        if self.received_quantity is not None:
            counts.add(self.received_quantity)
        return counts


def difference_source(
    *,
    billed_unit_cents: int | None,
    agreed_unit_cents: int | None,
    billed_quantity: int | None,
    ordered_quantity: int | None,
    received_quantity: int | None,
) -> DifferenceSource | None:
    """Build the comparison, or ``None`` when an input is missing.

    ``None`` means the note simply omits the source sentence; it never
    guesses a figure it was not given.
    """
    if None in (billed_unit_cents, agreed_unit_cents, billed_quantity, ordered_quantity):
        return None
    return DifferenceSource(
        billed_unit_cents=billed_unit_cents,
        agreed_unit_cents=agreed_unit_cents,
        billed_quantity=billed_quantity,
        ordered_quantity=ordered_quantity,
        received_quantity=received_quantity,
    )
