# @layer: unit
# @spec: 001-invoice-reconciliation-review
# @regression
"""Unit tests for the extraction parser (src/invoice_reconciliation/extraction/parser.py).

``parse`` is pure — it takes no network connection and makes no I/O call
— so every test here hand-builds a raw response payload (the same shape
``invoke_model`` returns: a dict with a ``content`` list holding one
``tool_use`` block whose ``input`` is the seven-field tool call) rather
than making a live call. This file exercises the three-stage validation
order from technical-considerations.md section 2.4: shape, then schema,
then domain.

Measured defence under direct test: the model returns a money field as a
JSON number (``24.0``) rather than a string roughly one call in three
(see money.py's docstring and technical-considerations.md 2.3). The
domain check must accept that numeric shape — it delegates to
``money.dollars_to_cents`` rather than applying its own regular
expression, so this file includes the float-where-string case as a
*passing* case, not a rejection.
"""

from __future__ import annotations

import pytest

from invoice_reconciliation.extraction.parser import (
    ExtractedFields,
    ExtractionDomainError,
    ExtractionSchemaError,
    ExtractionShapeError,
    parse,
)


def _raw_response(tool_input: dict) -> dict:
    """Build a raw response payload in the classic-Bedrock Messages-API shape.

    Mirrors exactly what ``extraction.client.extract_invoice_fields``
    returns: a dict with ``content`` (a list of blocks) and ``usage``.
    """
    return {
        "model": "claude-sonnet-4-5-20250929",
        "content": [
            {
                "type": "tool_use",
                "name": "record_invoice_fields",
                "input": tool_input,
            }
        ],
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }


_VALID_FIELDS = {
    "invoice_number": "INV-2",
    "supplier_id": "S1",
    "po_id": "PO-2",
    "sku": "CAB-1",
    "quantity": 5,
    "unit_price": "24.00",
    "total": "120.00",
}


class TestParseValidResponses:
    """Valid responses for both layouts (saved/constructed from the fixtures) parse cleanly."""

    def test_parses_layout_a_style_response_wrong_price(self):
        # Field values matching the committed wrong-price.png fixture:
        # INV-2 | S1 | PO-2 | CAB-1 | qty 5 | unit 24.00 | total 120.00.
        raw = _raw_response(_VALID_FIELDS)
        fields = parse(raw)
        assert fields == ExtractedFields(
            invoice_number="INV-2",
            supplier_id="S1",
            po_id="PO-2",
            sku="CAB-1",
            quantity=5,
            unit_price="24.00",
            total="120.00",
        )

    def test_parses_layout_b_style_response_missing_reference_po_id_is_none(self):
        # Field values matching the committed missing-reference.png fixture:
        # INV-3 | S1 | po_id null | CAB-1 | qty 5 | unit 20.00 | total 100.00.
        # The two-column layout is irrelevant to the parser: it only ever
        # sees the tool_use input dict, never the page layout.
        raw = _raw_response(
            {
                "invoice_number": "INV-3",
                "supplier_id": "S1",
                "po_id": None,
                "sku": "CAB-1",
                "quantity": 5,
                "unit_price": "20.00",
                "total": "100.00",
            }
        )
        fields = parse(raw)
        assert fields.po_id is None
        assert fields.invoice_number == "INV-3"
        assert fields.unit_price == "20.00"
        assert fields.total == "100.00"

    def test_float_where_string_expected_is_accepted_not_rejected(self):
        # Measured behaviour: roughly one call in three returns the money
        # fields as JSON numbers instead of strings. The domain check
        # delegates to money.dollars_to_cents, which accepts both shapes
        # -- a parser that rejected this would wrongly mark a good
        # invoice `failed`.
        raw = _raw_response({**_VALID_FIELDS, "unit_price": 24.0, "total": 120.0})
        fields = parse(raw)
        assert fields.unit_price == 24.0
        assert isinstance(fields.unit_price, float)
        assert fields.total == 120.0
        assert isinstance(fields.total, float)

    def test_int_money_field_is_also_accepted(self):
        # An int (e.g. a whole-dollar amount with no decimal) is the third
        # shape money.dollars_to_cents accepts.
        raw = _raw_response({**_VALID_FIELDS, "unit_price": 24, "total": 120})
        fields = parse(raw)
        assert fields.unit_price == 24
        assert fields.total == 120


class TestParseShapeErrors:
    """A response with no structured tool-use block raises ExtractionShapeError."""

    def test_raises_on_empty_content(self):
        raw = {"content": [], "usage": {"input_tokens": 1, "output_tokens": 1}}
        with pytest.raises(ExtractionShapeError):
            parse(raw)

    def test_raises_on_missing_content_key(self):
        raw = {"usage": {"input_tokens": 1, "output_tokens": 1}}
        with pytest.raises(ExtractionShapeError):
            parse(raw)

    def test_raises_on_text_only_response_no_tool_use(self):
        # The model replied in prose instead of using the forced tool —
        # should not happen with tool_choice pinned, but the parser must
        # not silently accept it if it does.
        raw = {
            "content": [{"type": "text", "text": "Here are the fields..."}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        with pytest.raises(ExtractionShapeError):
            parse(raw)


class TestParseSchemaErrors:
    """A malformed or incomplete tool input raises ExtractionSchemaError."""

    def test_raises_on_missing_field(self):
        incomplete = {k: v for k, v in _VALID_FIELDS.items() if k != "total"}
        raw = _raw_response(incomplete)
        with pytest.raises(ExtractionSchemaError):
            parse(raw)

    def test_raises_on_missing_po_id_key_entirely(self):
        # po_id must be present (even as null) -- an omitted key is a
        # schema violation, not an implicit null.
        incomplete = {k: v for k, v in _VALID_FIELDS.items() if k != "po_id"}
        raw = _raw_response(incomplete)
        with pytest.raises(ExtractionSchemaError):
            parse(raw)

    def test_raises_on_empty_invoice_number(self):
        raw = _raw_response({**_VALID_FIELDS, "invoice_number": ""})
        with pytest.raises(ExtractionSchemaError):
            parse(raw)

    def test_raises_on_non_string_supplier_id(self):
        raw = _raw_response({**_VALID_FIELDS, "supplier_id": 123})
        with pytest.raises(ExtractionSchemaError):
            parse(raw)

    def test_raises_on_non_integer_quantity(self):
        raw = _raw_response({**_VALID_FIELDS, "quantity": "5"})
        with pytest.raises(ExtractionSchemaError):
            parse(raw)

    def test_raises_on_boolean_quantity(self):
        # bool is a subclass of int in Python; must not silently pass as 1/0.
        raw = _raw_response({**_VALID_FIELDS, "quantity": True})
        with pytest.raises(ExtractionSchemaError):
            parse(raw)

    def test_raises_on_non_string_non_null_po_id(self):
        raw = _raw_response({**_VALID_FIELDS, "po_id": 2})
        with pytest.raises(ExtractionSchemaError):
            parse(raw)

    def test_raises_on_money_field_wrong_type(self):
        # A money field that is neither string nor number (e.g. a list,
        # or None) is a schema violation, not a domain one.
        raw = _raw_response({**_VALID_FIELDS, "total": None})
        with pytest.raises(ExtractionSchemaError):
            parse(raw)


class TestParseDomainErrors:
    """A correctly typed but domain-invalid value raises ExtractionDomainError.

    The money checks here are exactly the malformed-money-value table
    money.dollars_to_cents itself rejects -- the parser delegates to that
    one function rather than re-implementing the rule.
    """

    def test_raises_on_zero_quantity(self):
        raw = _raw_response({**_VALID_FIELDS, "quantity": 0})
        with pytest.raises(ExtractionDomainError):
            parse(raw)

    def test_raises_on_negative_quantity(self):
        raw = _raw_response({**_VALID_FIELDS, "quantity": -3})
        with pytest.raises(ExtractionDomainError):
            parse(raw)

    def test_raises_on_malformed_money_string(self):
        raw = _raw_response({**_VALID_FIELDS, "total": "not-a-number"})
        with pytest.raises(ExtractionDomainError):
            parse(raw)

    def test_raises_on_empty_money_string(self):
        raw = _raw_response({**_VALID_FIELDS, "unit_price": ""})
        with pytest.raises(ExtractionDomainError):
            parse(raw)

    def test_raises_on_over_precision_money_string(self):
        # money.dollars_to_cents raises on more than two decimal places --
        # the parser must surface that as ExtractionDomainError, not
        # silently truncate.
        raw = _raw_response({**_VALID_FIELDS, "total": "120.999"})
        with pytest.raises(ExtractionDomainError):
            parse(raw)
