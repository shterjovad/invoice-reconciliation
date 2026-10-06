"""The reconciliation rules engine.

One pure function, ``reconcile``. It takes the current field values for an
invoice, the ``MatchResult`` the matcher produced, and whether an earlier
duplicate exists — and returns a ``ReconciliationResult``. It performs no
database access, no network access, and no logging side effects: this is
what lets the batch command and the web correction route call exactly the
same code after exactly the same kind of input, with no second
implementation of the rules to drift out of sync.

Evaluation order, from ``technical-considerations.md`` section 2.5:

1. **Failed.** If extraction raised for this invoice, status is ``failed``
   with no amounts. Stop.
2. **Unresolved.** If ``po_id`` is null, or no purchase order matches
   ``(supplier_id, po_id)``, or the purchase order exists under a different
   supplier, status is ``unresolved`` with no amounts. Stop. The platform
   never falls back to matching on amount.
3. **Duplicate.** If an earlier invoice shares ``(supplier_id,
   invoice_number)``, status is ``duplicate`` with ``count_as_payable =
   False`` and no cent fields. Stop. The first copy by ``received_at``
   continues through steps 4 and 5 (the caller is responsible for passing
   ``is_duplicate=False`` for that first copy).
4. **Compute.** ``expected_cents = ordered_quantity * unit_cents`` (both
   read from the matched purchase order — never from the invoice's own
   claimed quantity or price). ``billed_cents`` comes from the invoice's own
   total. ``difference_cents = billed_cents - expected_cents``.
5. **Classify.** If the billed quantity differs from the purchase order's
   ordered quantity, or from the receipt's received quantity, status is
   ``discrepant`` (valued at the agreed unit price, i.e. ``expected_cents``
   stays ``ordered_quantity * unit_cents`` regardless of the billed
   quantity). Otherwise a zero difference is ``reconciled``; a non-zero
   difference — positive or negative — is ``discrepant``.

Money contract (read carefully — this is the subtle part): the seeded
fields ``unit_cents`` and ``total_cents`` are **already integer USD cents
stored as text** (``"2400"`` means 2400 cents, i.e. $24.00), not dollar
strings. They must therefore be parsed with a plain base-10 integer parse,
never with ``money.dollars_to_cents`` (which would reinterpret ``"2400"``
as $2,400.00 and return 240000). ``money.dollars_to_cents`` is reserved for
genuine dollar-amount strings (e.g. ``"24.00"``) — a shape this seeded data
does not use for these two fields. ``_parse_cents`` below does the plain
integer parse and raises ``MoneyFormatError`` on anything malformed, so a
bad value still funnels into the single error type the ``failed`` status
is built to catch.
"""

from __future__ import annotations

from dataclasses import dataclass

from invoice_reconciliation.money import MoneyFormatError
from invoice_reconciliation.reconciliation.matcher import MatchResult

__all__ = ["ReconciliationResult", "reconcile"]

_STATUS_FAILED = "failed"
_STATUS_UNRESOLVED = "unresolved"
_STATUS_DUPLICATE = "duplicate"
_STATUS_RECONCILED = "reconciled"
_STATUS_DISCREPANT = "discrepant"


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconciliationResult:
    """Everything a ``reconciliation_results`` row needs.

    Mirrors the nullability of that table: the three cent fields,
    ``count_as_payable``, and the two matched-id fields are all ``None``
    except where the status in question populates them.
    """

    status: str
    expected_cents: int | None = None
    billed_cents: int | None = None
    difference_cents: int | None = None
    count_as_payable: bool | None = None
    matched_po_id: str | None = None
    matched_receipt_id: str | None = None


def _parse_cents(value: str | None) -> int:
    """Parse a field that already holds integer USD cents as text.

    Unlike a dollar-amount string (``"24.00"``, handled by
    ``money.dollars_to_cents``), the seeded ``unit_cents`` and
    ``total_cents`` fields are integer cents already (``"2400"`` ==
    2400 cents). This parses that text directly as a base-10 integer.

    Raises:
        MoneyFormatError: on ``None`` or any text that is not a plain
            (optionally signed) integer.
    """
    if value is None:
        raise MoneyFormatError("cents value is None")
    text = value.strip()
    if not text:
        raise MoneyFormatError("empty cents string")
    negative = text.startswith("-")
    unsigned = text[1:] if negative else text
    if not unsigned.isdigit():
        raise MoneyFormatError(f"invalid integer-cents string: {value!r}")
    magnitude = int(unsigned)
    return -magnitude if negative else magnitude


def reconcile(
    *,
    current_fields: dict[str, str | None],
    match: MatchResult,
    is_duplicate: bool,
    extraction_failed: bool = False,
) -> ReconciliationResult:
    """Apply the reconciliation rules to one invoice. Pure; no I/O.

    Args:
        current_fields: the invoice's current field values, keyed by field
            name (``supplier_id``, ``po_id``, ``invoice_number``, ``sku``,
            ``quantity``, ``unit_cents``, ``total_cents``) — the same shape
            ``repository.get_current_fields`` returns.
        match: the ``MatchResult`` from
            ``matcher.find_purchase_order_and_receipt``, computed by the
            caller against ``current_fields``.
        is_duplicate: whether the caller's duplicate lookup
            (``matcher.find_earlier_duplicate``) found an earlier invoice
            sharing ``(supplier_id, invoice_number)``. The caller passes
            ``False`` for the first copy, so that copy proceeds through
            steps 4 and 5 rather than stopping at step 3.
        extraction_failed: True when extraction raised for this invoice.
            When True, every other argument is ignored and the result is
            ``failed`` with no amounts.

    Returns:
        A ``ReconciliationResult`` carrying the status and, where
        applicable, the computed cent values and matched ids.
    """
    # 1. Failed.
    if extraction_failed:
        return ReconciliationResult(status=_STATUS_FAILED)

    # 2. Unresolved. Never falls back to matching on amount — a non-match
    # from the matcher (null po_id, no such po_id/supplier_id pair, or a
    # po_id that exists under a different supplier) is already folded into
    # match.is_matched being False.
    if not match.is_matched:
        return ReconciliationResult(status=_STATUS_UNRESOLVED)

    purchase_order = match.purchase_order
    assert purchase_order is not None  # guaranteed by is_matched above

    # 3. Duplicate.
    if is_duplicate:
        return ReconciliationResult(
            status=_STATUS_DUPLICATE,
            count_as_payable=False,
        )

    # 4. Compute. Quantity and unit price come from the matched purchase
    # order — the agreed terms — never from what the invoice claims.
    ordered_quantity = int(purchase_order["quantity"])
    unit_cents = int(purchase_order["unit_cents"])
    expected_cents = ordered_quantity * unit_cents

    billed_cents = _parse_cents(current_fields.get("total_cents"))
    difference_cents = billed_cents - expected_cents

    matched_po_id = purchase_order["po_id"]
    matched_receipt_id = match.receipt["receipt_id"] if match.receipt is not None else None

    # 5. Classify.
    billed_quantity_text = current_fields.get("quantity")
    billed_quantity = int(billed_quantity_text) if billed_quantity_text is not None else None
    received_quantity = (
        int(match.receipt["quantity"]) if match.receipt is not None else None
    )

    quantity_mismatch = billed_quantity is not None and (
        billed_quantity != ordered_quantity
        or (received_quantity is not None and billed_quantity != received_quantity)
    )

    if quantity_mismatch:
        status = _STATUS_DISCREPANT
    elif difference_cents == 0:
        status = _STATUS_RECONCILED
    else:
        # A negative difference (an undercharge) is discrepant too — the
        # sign alone never changes the status.
        status = _STATUS_DISCREPANT

    return ReconciliationResult(
        status=status,
        expected_cents=expected_cents,
        billed_cents=billed_cents,
        difference_cents=difference_cents,
        matched_po_id=matched_po_id,
        matched_receipt_id=matched_receipt_id,
    )
