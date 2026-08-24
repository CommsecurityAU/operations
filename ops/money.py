"""Money. ONE rounding function, one place (CS-OP-ARCH-002 §4, ADR-15).

Every value is an integer number of cents. No float ever touches money: at
64-bit double precision `19.99 * 100` is 1998.9999999999998, and a system
whose job is reconciling to the cent cannot afford that anywhere.

Rounding is HALF AWAY FROM ZERO (ADR-15): 0.5 rounds to 1, -0.5 rounds to
-1. That is what Google Sheets does, so it is what the workbook figures this
platform must reconcile against encode, and it is ordinary GST practice.
Banker's rounding was rejected -- it is right for statistical aggregates and
wrong for a ledger someone signs.

Everything here is integer arithmetic, so the mode is a decision this module
makes rather than one the hardware makes for us.
"""

from typing import Any

CENTS_PER_DOLLAR = 100
BASIS_POINTS = 10_000


class MoneyError(ValueError):
    """Carries the offending text; never a partially-parsed number."""


def divide(numerator: int, denominator: int) -> int:
    """Integer division rounded HALF AWAY FROM ZERO.

    The one rounding primitive. Nothing else in the codebase may round
    money: Python's own round() is banker's, and `int()` truncates toward
    zero, so either used by accident silently contradicts ADR-15.
    """
    if denominator == 0:
        raise ZeroDivisionError("money division by zero")
    if denominator < 0:
        numerator, denominator = -numerator, -denominator
    sign = -1 if numerator < 0 else 1
    magnitude, remainder = divmod(abs(numerator), denominator)
    # `2 * remainder >= denominator` is the half-way test done in integers,
    # so a value exactly on the boundary rounds up rather than depending on
    # how a float landed.
    if 2 * remainder >= denominator:
        magnitude += 1
    return sign * magnitude


def apply_rate(cents: int, rate_bp: int) -> int:
    """Apply a basis-point rate to an amount. 1000 bp = 10% (GST).

    Rates are basis points and not percentages so that 4.85% payroll tax is
    485, an exact integer, rather than a decimal that has to be rounded
    before it is even used.
    """
    return divide(cents * rate_bp, BASIS_POINTS)


def parse(text: Any) -> int:
    """Money text -> integer cents, rounded half away from zero.

    Handles what the source workbooks actually contain: `$1,234.56`,
    `(1,234.56)` for negatives, `-` for zero, blanks. A value carrying more
    than two decimals is ROUNDED, not truncated -- truncation drifts
    consistently downward, which is the worst shape of error: small,
    one-directional and invisible.
    """
    if text is None:
        return 0
    s = str(text).strip().replace("$", "").replace(",", "").replace(" ", "")
    if s in ("", "-"):
        return 0

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    if s.startswith("-"):
        negative, s = True, s[1:]
    elif s.startswith("+"):
        s = s[1:]
    if not s:
        raise MoneyError(f"unparseable money value: {text!r}")

    whole, _, frac = s.partition(".")
    whole = whole or "0"
    if not whole.isdigit() or (frac and not frac.isdigit()):
        raise MoneyError(f"unparseable money value: {text!r}")

    if len(frac) <= 2:
        cents = int(whole) * CENTS_PER_DOLLAR + int((frac + "00")[:2])
    else:
        # Round at the cent, in integers. Scale the whole thing up, then
        # divide back down through the one rounding primitive.
        scale = 10 ** len(frac)
        total = int(whole) * scale + int(frac)
        cents = divide(total * CENTS_PER_DOLLAR, scale)
    return -cents if negative else cents


def format(cents: int) -> str:
    """Cents -> '$1,234.56'. Negatives in parentheses, matching the source."""
    sign = cents < 0
    whole, part = divmod(abs(cents), CENTS_PER_DOLLAR)
    text = f"${whole:,}.{part:02d}"
    return f"({text})" if sign else text
