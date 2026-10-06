"""The one prompt constant for invoice field extraction.

Two layouts exist among the supplied fixtures. Layout ``a`` prints one
labelled field per line. Layout ``b`` prints two labelled fields per line,
left and right columns. The prompt does not branch on layout — it tells
the model to read every field **by its printed label**, never by position
on the page or line, so the same prompt and the same parsing path handle
both layouts without the code needing to know which one it is looking at.

The purchase-order field is sometimes printed with a placeholder in place
of a real reference (observed: the literal text ``"(missing)"``, also
``"N/A"``, ``"none"``, or a blank/dash). The prompt states explicitly that
a placeholder is not a value: the model must recognise it and return
``null``, not transcribe the placeholder text as if it were a real PO
identifier.
"""

from __future__ import annotations

__all__ = ["EXTRACTION_PROMPT"]

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
