"""Money and price primitives.

All money and prices outside the simulator are integers in **centi-cents**:
1 centi-cent (cc) = 1/100 cent = $0.0001, so $1.00 == 10_000 cc.

Binary floats are banned for money. ``decimal.Decimal`` appears only at the wire
boundary, to parse and format Kalshi's fixed-point string fields exactly.
Probabilities (floats in [0, 1]) are a separate space; conversion between price
and probability happens only through the helpers here.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Literal, NewType

CentiCents = NewType("CentiCents", int)

CC_PER_CENT = 100
CC_PER_DOLLAR = 10_000
ONE_DOLLAR = CentiCents(CC_PER_DOLLAR)
ZERO = CentiCents(0)

_CC_QUANTUM = Decimal("0.0001")  # one centi-cent, in dollars


class MoneyParseError(ValueError):
    """A wire value could not be represented exactly in centi-cents."""


def cc_from_decimal_dollars(value: Decimal) -> CentiCents:
    """Convert an exact Decimal dollar amount to centi-cents, or raise."""
    scaled = value / _CC_QUANTUM
    if scaled != scaled.to_integral_value():
        raise MoneyParseError(f"{value} dollars is not a whole number of centi-cents")
    return CentiCents(int(scaled))


def cc_from_dollars_str(s: str) -> CentiCents:
    """Parse a Kalshi ``*_dollars`` fixed-point string (e.g. ``"0.5600"``) exactly."""
    try:
        value = Decimal(s)
    except InvalidOperation as exc:
        raise MoneyParseError(f"unparseable dollars string: {s!r}") from exc
    return cc_from_decimal_dollars(value)


def cc_to_decimal_dollars(cc: CentiCents) -> Decimal:
    return Decimal(cc) * _CC_QUANTUM


def cc_to_dollars_str(cc: CentiCents, places: int = 4) -> str:
    """Format centi-cents as a fixed-point dollars string with ``places`` decimals.

    Raises if the value cannot be represented exactly in that many places.
    """
    quantum = Decimal(1).scaleb(-places)
    value = cc_to_decimal_dollars(cc)
    quantized = value.quantize(quantum) if value == value.quantize(quantum) else None
    if quantized is None:
        raise MoneyParseError(f"{cc} cc does not fit in {places} decimal places")
    return f"{quantized:.{places}f}"


def cc_from_cents(cents: int) -> CentiCents:
    return CentiCents(cents * CC_PER_CENT)


def prob_from_cc(cc: CentiCents) -> float:
    """Contract price → implied probability (probability space; float is fine)."""
    return cc / CC_PER_DOLLAR


def cc_from_prob(p: float, rounding: Literal["nearest", "down", "up"] = "nearest") -> CentiCents:
    """Probability → price in centi-cents.

    Fair values round to nearest; bid construction typically rounds *against*
    ourselves explicitly at the call site via ``down``/``up``.
    """
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"probability out of range: {p}")
    exact = p * CC_PER_DOLLAR
    if rounding == "nearest":
        return CentiCents(round(exact))
    if rounding == "down":
        return CentiCents(int(exact))
    return CentiCents(-int(-exact // 1))


def round_to_tick(
    cc: CentiCents, tick: CentiCents, direction: Literal["down", "up", "nearest"]
) -> CentiCents:
    """Round a price onto a grid of ``tick`` centi-cents (grid anchored at 0)."""
    if tick <= 0:
        raise ValueError(f"tick must be positive, got {tick}")
    quotient, remainder = divmod(cc, tick)
    if remainder == 0:
        return cc
    if direction == "down":
        return CentiCents(quotient * tick)
    if direction == "up":
        return CentiCents((quotient + 1) * tick)
    down = quotient * tick
    up = (quotient + 1) * tick
    return CentiCents(up if remainder * 2 >= tick else down)
