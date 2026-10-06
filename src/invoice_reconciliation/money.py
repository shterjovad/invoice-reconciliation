"""Money conversion between dollar strings/numbers and integer USD cents.

All monetary arithmetic in this project happens in integer cents. This module
is the single boundary where a dollar amount (as produced by extraction, in
either string or numeric form) is converted to cents, and the single place
that is allowed to look at a decimal point.

Rule: ``float()`` is never called on a string here. A numeric input is first
normalised to text with ``f"{v:.2f}"`` — this fixes its text form without any
multiplication — and only the resulting string is ever split and parsed.
"""

from __future__ import annotations

__all__ = ["MoneyFormatError", "dollars_to_cents", "cents_to_display"]


class MoneyFormatError(Exception):
    """Raised when a value cannot be safely interpreted as a dollar amount."""


def dollars_to_cents(v: str | float | int) -> int:
    """Convert a dollar amount to integer cents.

    Accepts a string (``"24.00"``, ``"-12.50"``, ``"5"``) or a number
    (``24.0``, ``5``). A numeric input is normalised to a two-decimal-place
    string with ``f"{v:.2f}"`` before any parsing — ``float()`` is never
    applied to a string. The sign is applied exactly once to the combined
    whole/fractional result, so negative amounts round-trip correctly
    (``"-12.50"`` -> ``-1250``, not ``-1150``).

    Raises:
        MoneyFormatError: on ``None``, empty or malformed strings, and on
            over-precision (more than two decimal places).
    """
    if v is None:
        raise MoneyFormatError("money value is None")

    if isinstance(v, bool):
        # bool is a subclass of int; it is never a legitimate money value.
        raise MoneyFormatError(f"invalid money value: {v!r}")

    if isinstance(v, str):
        text = v.strip()
        if not text:
            raise MoneyFormatError("empty money string")
    elif isinstance(v, (float, int)):
        text = f"{v:.2f}"
    else:
        raise MoneyFormatError(f"unsupported money value type: {type(v)!r}")

    negative = text.startswith("-")
    unsigned = text[1:] if negative else text
    if unsigned.startswith("+"):
        raise MoneyFormatError(f"invalid money string: {v!r}")

    if "." in unsigned:
        whole_str, _, frac_str = unsigned.partition(".")
    else:
        whole_str, frac_str = unsigned, "00"

    if not whole_str or not whole_str.isdigit():
        raise MoneyFormatError(f"invalid money string: {v!r}")

    if len(frac_str) > 2:
        raise MoneyFormatError(f"over-precision money value: {v!r}")

    if not frac_str.isdigit():
        raise MoneyFormatError(f"invalid money string: {v!r}")

    frac_str = frac_str.ljust(2, "0")

    whole = int(whole_str)
    frac = int(frac_str)

    total = whole * 100 + frac
    return -total if negative else total


def cents_to_display(cents: int) -> str:
    """Format integer cents as a dollar string, for display only.

    Example: ``cents_to_display(2400) == "24.00"``,
    ``cents_to_display(-1250) == "-12.50"``.
    """
    negative = cents < 0
    magnitude = -cents if negative else cents
    whole, frac = divmod(magnitude, 100)
    sign = "-" if negative else ""
    return f"{sign}{whole}.{frac:02d}"
