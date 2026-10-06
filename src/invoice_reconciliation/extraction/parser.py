"""Parse a raw Bedrock response into ``ExtractedFields``. A pure function.

This is the one code path a live call and a cache replay both run through
— only the network call differs between the two; everything from the raw
response onward is this module. ``parse`` takes no network connection and
makes no I/O call, so it is directly unit-testable against saved or
hand-built response payloads.

Validation runs in three stages, each raising its own ``ExtractionError``
subclass:

1. **Shape** (``ExtractionShapeError``) — did the response actually carry
   the structured tool-use block this project asked for?
2. **Schema** (``ExtractionSchemaError``) — does the tool input have all
   seven required keys, with each key's JSON type matching the schema
   (``po_id`` additionally allowed to be ``None``)?
3. **Domain** (``ExtractionDomainError``) — is ``quantity`` a positive
   integer, and does each money field parse as a value
   ``money.dollars_to_cents`` accepts? **This step delegates the money
   check to ``money.dollars_to_cents`` rather than applying its own
   regular expression.** A hand-rolled pattern such as ``^\\d+\\.\\d{2}$``
   would reject the numeric ``24.0`` that the model returns roughly one
   call in three (see ``money.py``), and wrongly mark a good invoice
   ``failed``. One rule for what counts as a valid amount, in one module,
   is what this delegation buys.
"""

from __future__ import annotations

from dataclasses import dataclass

from invoice_reconciliation.money import MoneyFormatError, dollars_to_cents

__all__ = [
    "ExtractedFields",
    "ExtractionError",
    "ExtractionShapeError",
    "ExtractionSchemaError",
    "ExtractionDomainError",
    "parse",
]

_REQUIRED_FIELDS = (
    "invoice_number",
    "supplier_id",
    "po_id",
    "sku",
    "quantity",
    "unit_price",
    "total",
)

# Fields that must be non-empty strings (po_id is handled separately: it
# may legitimately be None).
_REQUIRED_STRING_FIELDS = ("invoice_number", "supplier_id", "sku")

# Money fields: left unconverted here, in whatever form the model
# returned (str, float, or int) — money.dollars_to_cents is the single
# place that interprets the value and converts to cents.
_MONEY_FIELDS = ("unit_price", "total")


class ExtractionError(Exception):
    """Base class for every extraction-parsing failure."""


class ExtractionShapeError(ExtractionError):
    """The raw response did not carry the expected structured tool-use block."""


class ExtractionSchemaError(ExtractionError):
    """The tool input is missing a required key, or a key has the wrong JSON type."""


class ExtractionDomainError(ExtractionError):
    """A value is present and correctly typed, but fails a domain rule."""


@dataclass(frozen=True, slots=True)
class ExtractedFields:
    """The seven extracted invoice fields, in the form the model returned them.

    ``po_id`` is ``str | None`` — ``None`` is a valid, meaningful answer
    (no purchase-order reference printed), not an error.

    ``unit_price`` and ``total`` are typed ``str | float | int`` and are
    **left unconverted** at this layer. The schema asks the model for a
    string; measured calls show it sometimes returns a number instead.
    Both shapes are valid here — ``money.dollars_to_cents`` is the single
    place downstream that converts either shape to integer cents.

    ``quantity`` is ``int``: a count, with no rounding risk.
    """

    invoice_number: str
    supplier_id: str
    po_id: str | None
    sku: str
    quantity: int
    unit_price: str | float | int
    total: str | float | int


def _extract_tool_input(raw_response) -> dict:
    """Pull the structured tool-use input out of the raw SDK response.

    Raises ``ExtractionShapeError`` if the response carries no
    ``tool_use`` content block at all — the shape check, run before any
    schema or domain validation.
    """
    content = getattr(raw_response, "content", None)
    if content is None and isinstance(raw_response, dict):
        content = raw_response.get("content")

    if not content:
        raise ExtractionShapeError("response has no content blocks")

    for block in content:
        block_type = getattr(block, "type", None)
        if block_type is None and isinstance(block, dict):
            block_type = block.get("type")

        if block_type == "tool_use":
            tool_input = getattr(block, "input", None)
            if tool_input is None and isinstance(block, dict):
                tool_input = block.get("input")

            if not isinstance(tool_input, dict):
                raise ExtractionShapeError(
                    "tool_use block has no dict 'input' payload"
                )
            return tool_input

    raise ExtractionShapeError("response has no tool_use content block")


def _check_schema(tool_input: dict) -> None:
    """Shape/type-check the tool input against the seven-field contract.

    Raises ``ExtractionSchemaError`` on a missing key or a wrong JSON
    type. Does not interpret the money fields' content — that is the
    domain check's job, delegated to ``money.dollars_to_cents``.
    """
    missing = [field for field in _REQUIRED_FIELDS if field not in tool_input]
    if missing:
        raise ExtractionSchemaError(f"missing required field(s): {missing}")

    for field in _REQUIRED_STRING_FIELDS:
        value = tool_input[field]
        if not isinstance(value, str) or not value.strip():
            raise ExtractionSchemaError(
                f"field {field!r} must be a non-empty string, got {value!r}"
            )

    po_id = tool_input["po_id"]
    if po_id is not None and (not isinstance(po_id, str) or not po_id.strip()):
        raise ExtractionSchemaError(
            f"field 'po_id' must be a string or null, got {po_id!r}"
        )

    quantity = tool_input["quantity"]
    # bool is a subclass of int; reject it explicitly so a stray `true`/`false`
    # does not silently pass as 1/0.
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise ExtractionSchemaError(
            f"field 'quantity' must be an integer, got {quantity!r}"
        )

    for field in _MONEY_FIELDS:
        value = tool_input[field]
        if isinstance(value, bool) or not isinstance(value, (str, float, int)):
            raise ExtractionSchemaError(
                f"field {field!r} must be a string or number, got {value!r}"
            )


def _check_domain(tool_input: dict) -> None:
    """Domain rules: quantity must be positive; money fields must convert.

    The money check **delegates to ``money.dollars_to_cents``** rather
    than matching its own regular expression. Whatever that function
    accepts is valid here; whatever it rejects raises
    ``ExtractionDomainError``. This is the one rule in the codebase for
    what counts as a valid money value.
    """
    quantity = tool_input["quantity"]
    if quantity <= 0:
        raise ExtractionDomainError(f"quantity must be positive, got {quantity!r}")

    for field in _MONEY_FIELDS:
        value = tool_input[field]
        try:
            dollars_to_cents(value)
        except MoneyFormatError as exc:
            raise ExtractionDomainError(
                f"field {field!r} is not a valid money value: {value!r}"
            ) from exc


def parse(raw_response) -> ExtractedFields:
    """Parse a raw Bedrock response into ``ExtractedFields``.

    Pure function: no network access, no database access, no logging
    side effects. Runs shape, then schema, then domain validation, in
    that order, each raising its own ``ExtractionError`` subclass.

    Money fields (``unit_price``, ``total``) are returned exactly as the
    model supplied them — string or number — for the caller to hand to
    ``money.dollars_to_cents``. This function never calls ``float()`` and
    never converts money to cents itself.
    """
    tool_input = _extract_tool_input(raw_response)
    _check_schema(tool_input)
    _check_domain(tool_input)

    po_id = tool_input["po_id"]

    return ExtractedFields(
        invoice_number=tool_input["invoice_number"],
        supplier_id=tool_input["supplier_id"],
        po_id=po_id,
        sku=tool_input["sku"],
        quantity=tool_input["quantity"],
        unit_price=tool_input["unit_price"],
        total=tool_input["total"],
    )
