# @layer: unit
# @spec: 001-invoice-reconciliation-review
# @regression
"""Unit tests for the money conversion module (src/invoice_reconciliation/money.py).

Covers the full table from technical-considerations.md section 2.3: string
dollar amounts, numeric (float/int) dollar amounts as returned intermittently
by the extraction model, and the malformed/over-precision/absent inputs that
must raise rather than silently coerce.
"""

from __future__ import annotations

import pytest

from invoice_reconciliation.money import (
    MoneyFormatError,
    cents_to_display,
    dollars_to_cents,
)


class TestDollarsToCentsConverts:
    """Valid inputs — string and numeric forms — convert to the correct cents."""

    @pytest.mark.parametrize(
        ("value", "expected_cents"),
        [
            ("24.00", 2400),  # trailing zeros
            ("120.00", 12000),  # the worked example
            ("5.5", 550),  # one decimal place
            ("5", 500),  # no decimal point
            ("0.09", 9),  # leading zero in the cents
            ("-12.50", -1250),  # negative; sign applied once to combined result
            (24.0, 2400),  # a float from the model
            (120.0, 12000),  # a float, the worked example in numeric form
            (5, 500),  # an int, whole-dollar amount
        ],
    )
    def test_converts_valid_input_to_expected_cents(self, value, expected_cents):
        assert dollars_to_cents(value) == expected_cents

    def test_negative_sign_applied_once_not_per_component(self):
        # Regression guard for the specific bug class: a naive
        # `whole * 100 + frac` applied to "-12.50" before negating gives
        # -12*100+50 = -1150. The sign must be applied once to the
        # combined whole+frac result, giving -1250.
        assert dollars_to_cents("-12.50") == -1250


class TestDollarsToCentsRaises:
    """Malformed, absent, or over-precision inputs raise MoneyFormatError."""

    @pytest.mark.parametrize(
        "value",
        [
            "24.999",  # over-precision: must raise, not truncate
            "",  # empty string never coerces to zero
            "abc",  # non-numeric garbage
            None,  # absent value never becomes zero
        ],
    )
    def test_raises_money_format_error_on_invalid_input(self, value):
        with pytest.raises(MoneyFormatError):
            dollars_to_cents(value)


class TestCentsToDisplay:
    """cents_to_display round-trips cents back to a human-readable string."""

    @pytest.mark.parametrize(
        ("cents", "expected_display"),
        [
            (2400, "24.00"),
            (-1250, "-12.50"),
            (9, "0.09"),
        ],
    )
    def test_displays_cents_as_dollar_string(self, cents, expected_display):
        assert cents_to_display(cents) == expected_display
