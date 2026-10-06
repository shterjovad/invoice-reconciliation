"""The seven-field JSON Schema the extraction call constrains its response to.

Two defences live in this schema, both measured, not theoretical:

1. ``unit_price`` and ``total`` are typed ``"string"``. Three identical
   calls on ``wrong-price.png`` returned a JSON number (``24.0``) once and
   a JSON string (``"24.00"``) twice. A float reaching ``s.split(".")``
   raises ``AttributeError``. Typing the field ``"string"`` removes the
   variance at the source; ``money.dollars_to_cents`` (the only other
   defence) still accepts a number, in case a model ignores the schema.
2. ``po_id`` is typed ``["string", "null"]`` and listed in ``required``.
   The model must emit the key on every call, and ``null`` is a valid
   answer — this is what lets ``missing-reference`` extract as "no
   purchase-order reference" rather than silently omitting the field.
"""

from __future__ import annotations

__all__ = ["EXTRACTION_SCHEMA"]

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
