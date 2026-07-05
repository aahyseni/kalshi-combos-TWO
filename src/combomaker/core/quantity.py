"""Contract quantities as integers in centi-contracts (fixed-point, 2 dp).

Kalshi counts are fixed-point strings with 2 decimals ("13.00" = 13 contracts);
we hold them as integer centi-contracts (1 contract = 100 centi-contracts), the
same exactness discipline as money. ``delta_fp`` values are signed.

Unit identity worth memorizing: centi-contracts × centi-cents = micro-dollars
(1e-2 × 1e-4 = 1e-6), so position cost/value arithmetic stays in integers.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import NewType

CentiContracts = NewType("CentiContracts", int)

CENTI_PER_CONTRACT = 100

MICRO_DOLLARS_PER_DOLLAR = 1_000_000


class QuantityParseError(ValueError):
    """A wire count could not be represented exactly in centi-contracts."""


def qty_from_fp_str(s: str) -> CentiContracts:
    """Parse a Kalshi ``*_fp`` count string (e.g. ``"13.00"``, ``"-54.00"``)."""
    try:
        value = Decimal(s)
    except InvalidOperation as exc:
        raise QuantityParseError(f"unparseable count string: {s!r}") from exc
    scaled = value * CENTI_PER_CONTRACT
    if scaled != scaled.to_integral_value():
        raise QuantityParseError(f"{s!r} is not a whole number of centi-contracts")
    return CentiContracts(int(scaled))


def qty_to_fp_str(qty: CentiContracts) -> str:
    """Format centi-contracts as the canonical 2-decimal wire string."""
    return f"{Decimal(qty) / CENTI_PER_CONTRACT:.2f}"


def qty_from_contracts(contracts: int) -> CentiContracts:
    return CentiContracts(contracts * CENTI_PER_CONTRACT)


def cost_micro_dollars(qty: CentiContracts, price_cc: int) -> int:
    """Cost of ``qty`` at ``price_cc`` in exact integer micro-dollars."""
    return qty * price_cc
