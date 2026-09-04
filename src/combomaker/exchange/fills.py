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
"""

from __future__ import annotations

from typing import Any

from combomaker.core.money import MoneyParseError, cc_from_dollars_str, fee_cc_from_dollars_str
from combomaker.core.quantity import QuantityParseError, qty_from_fp_str
from combomaker.pricing.fee_observer import FeeObservation, collection_prefix_of

JsonDict = dict[str, Any]


def fee_observation_from_fill(row: JsonDict) -> FeeObservation | None:
    fill_id = row.get("fill_id") or row.get("trade_id") or row.get("order_id")
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
        fee_raw = row.get("fee_cost")
        fee_cc = 0 if fee_raw is None else int(fee_cc_from_dollars_str(str(fee_raw)))
    except (MoneyParseError, QuantityParseError, ValueError, TypeError):
        return None
    if contracts <= 0 or not 0 <= price_cc <= 10_000:
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
