"""Exchange fill rows (GET /portfolio/fills) → fee observations.

The ONLY place a raw fill's maker/taker flag and prices are interpreted for
the fee observer (conventions quarantine: ``pricing/`` never reads the wire —
tests/test_architecture.py). Fail-closed: a row missing any field the fee
model needs yields None and is simply not observed — never a guessed
observation.

Fill schema (docs/api-notes/index-scan.md §portfolio, live-verified on the
2026-08-20+ tape): ``fill_id``/``trade_id``, ``created_time`` (ISO), ``ticker``
/``market_ticker``, ``count_fp`` (fixed-point contracts), ``yes_price_dollars``
/``no_price_dollars``, ``fee_cost`` (fixed-point dollars), ``is_taker``.
The quadratic fee base P·(1−P) is symmetric in P, so either side's price is
the same base — we take our held side's when stated, else whichever exists.

THE REPORTED ``fee_cost`` IS NOT THE FEE ALONE (2026-09-04 review fix M1).
The exchange debits the position cost and the fee each rounded UP to a
centi-cent and reports the whole excess over the EXACT cost as ``fee_cost``:

    fee_cost = ceil_cc(coef · C · P · (1−P)) + (ceil_cc(C · P) − C · P)

The second term is pure cost rounding — coefficient-free, < 1 cc, present on
UNCHARGED fills too (1,763 of the 3,582 pre-onset maker fills on the real
tape carry fee_cost = $0.00002-0.00008 and no maker fee at all). Ceiling the
raw ``fee_cost`` therefore booked a 1 cc "fee" on every one of them, which
made them CHARGED in the observer's eyes: a permanent mismatch alarm on every
sweep and two collections marked maker-fee-observed off a rounding residue.
The parser now removes the residue EXACTLY (Fraction arithmetic from the
row's own contracts and price) and ceils what remains — the exchange's own
integer ceiling of the fee, zero on an uncharged fill. Verified: on all
4,122 maker fills of the real tape ``fee_cost − residue`` is a whole number
of centi-cents (0 on every pre-onset fill, > 0 on every post-onset fill;
tests/test_fee_observer.py).
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

from combomaker.core.money import CC_PER_DOLLAR, MoneyParseError, cc_from_dollars_str
from combomaker.core.quantity import QuantityParseError, qty_from_fp_str
from combomaker.pricing.fee_observer import (
    FeeObservation,
    collection_prefix_of,
    cost_residue_cc,
)

JsonDict = dict[str, Any]


def charged_fee_cc(fee_cost_dollars: str, *, contracts_centi: int, price_cc: int) -> int:
    """The fee the exchange actually CHARGED on a fill, in whole cc: the
    reported ``fee_cost`` minus the cost-rounding residue, ceiled (never
    understates a cost we paid; exact on the real tape). Raises
    ``MoneyParseError`` on an unparseable string."""
    try:
        exact_cc = Fraction(Decimal(fee_cost_dollars)) * CC_PER_DOLLAR
    except (InvalidOperation, ValueError) as exc:
        raise MoneyParseError(f"unparseable fee_cost: {fee_cost_dollars!r}") from exc
    net = exact_cc - cost_residue_cc(contracts_centi, price_cc)
    return max(0, math.ceil(net))


def fee_observation_from_fill(row: JsonDict) -> FeeObservation | None:
    # The fill id is the dedup key. ``order_id`` is NOT a fallback: one order
    # fills in several partials sharing the order id (target-cost RFQs), so
    # keying on it would drop observations. Fail closed instead (S7).
    fill_id = row.get("fill_id") or row.get("trade_id")
    created = row.get("created_time")
    ticker = row.get("ticker") or row.get("market_ticker")
    if not fill_id or not created or not ticker:
        return None
    is_taker = row.get("is_taker")
    if not isinstance(is_taker, bool):
        return None
    side = row.get("side") or row.get("outcome_side")
    price_raw = None
    if side == "no":
        price_raw = row.get("no_price_dollars")
    elif side == "yes":
        price_raw = row.get("yes_price_dollars")
    if price_raw is None:
        price_raw = row.get("no_price_dollars") or row.get("yes_price_dollars")
    if price_raw is None:
        return None
    try:
        contracts = int(qty_from_fp_str(str(row.get("count_fp") or row.get("count"))))
        price_cc = int(cc_from_dollars_str(str(price_raw)))
        if contracts <= 0 or not 0 <= price_cc <= CC_PER_DOLLAR:
            return None
        fee_raw = row.get("fee_cost")
        fee_cc = (
            0
            if fee_raw is None
            else charged_fee_cc(str(fee_raw), contracts_centi=contracts, price_cc=price_cc)
        )
    except (MoneyParseError, QuantityParseError, ValueError, TypeError):
        return None
    return FeeObservation(
        fill_id=str(fill_id),
        created_time=str(created),
        collection_prefix=collection_prefix_of(str(ticker)),
        contracts_centi=contracts,
        price_cc=price_cc,
        fee_cc=fee_cc,
        maker=not is_taker,
    )


def fee_observations_from_fills(rows: list[JsonDict]) -> list[FeeObservation]:
    out: list[FeeObservation] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        obs = fee_observation_from_fill(row)
        if obs is not None:
            out.append(obs)
    return out
