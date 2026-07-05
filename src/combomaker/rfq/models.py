"""Typed RFQ domain models parsed from communications-channel wire messages.

Parsing is strict on required fields (docs/api-notes/communications-ws.md) and
UNKNOWN-preserving on enums: an unexpected ``side`` string is kept raw and
flagged, never coerced — the filter layer turns UNKNOWN into no-quote
(quiet-failure defense #2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from combomaker.core.money import CentiCents, MoneyParseError, cc_from_dollars_str
from combomaker.core.quantity import CentiContracts, QuantityParseError, qty_from_fp_str

JsonDict = dict[str, Any]


class RfqParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RfqLeg:
    market_ticker: str
    event_ticker: str | None
    side: str  # raw wire value; expected "yes"|"no" but NOT guaranteed
    yes_settlement_value_cc: CentiCents | None  # filled only after determination

    @property
    def side_known(self) -> bool:
        return self.side in ("yes", "no")


@dataclass(frozen=True, slots=True)
class Rfq:
    rfq_id: str
    market_ticker: str
    event_ticker: str | None
    contracts: CentiContracts | None      # sizing mode A
    target_cost_cc: CentiCents | None     # sizing mode B (total dollars)
    created_ts: str
    mve_collection_ticker: str | None
    legs: tuple[RfqLeg, ...]
    raw: JsonDict

    @property
    def is_combo(self) -> bool:
        return self.mve_collection_ticker is not None and len(self.legs) > 0

    @property
    def leg_tickers(self) -> tuple[str, ...]:
        return tuple(leg.market_ticker for leg in self.legs)

    @property
    def all_leg_sides_known(self) -> bool:
        return all(leg.side_known for leg in self.legs)

    @classmethod
    def from_ws(cls, msg: JsonDict) -> Rfq:
        try:
            rfq_id = str(msg["id"])
            market_ticker = str(msg["market_ticker"])
            created_ts = str(msg["created_ts"])
        except KeyError as exc:
            raise RfqParseError(f"rfq_created missing required field {exc}") from exc

        contracts: CentiContracts | None = None
        if msg.get("contracts_fp") is not None:
            try:
                contracts = qty_from_fp_str(str(msg["contracts_fp"]))
            except QuantityParseError as exc:
                raise RfqParseError(f"bad contracts_fp: {exc}") from exc

        target_cost: CentiCents | None = None
        if msg.get("target_cost_dollars") is not None:
            try:
                target_cost = cc_from_dollars_str(str(msg["target_cost_dollars"]))
            except MoneyParseError as exc:
                raise RfqParseError(f"bad target_cost_dollars: {exc}") from exc

        legs: list[RfqLeg] = []
        for item in msg.get("mve_selected_legs") or []:
            if not isinstance(item, dict):
                raise RfqParseError(f"malformed mve_selected_legs item: {item!r}")
            settlement: CentiCents | None = None
            raw_settlement = item.get("yes_settlement_value_dollars")
            if raw_settlement is not None:
                try:
                    settlement = cc_from_dollars_str(str(raw_settlement))
                except MoneyParseError:
                    settlement = None  # informational field; never load-bearing
            legs.append(
                RfqLeg(
                    market_ticker=str(item.get("market_ticker", "")),
                    event_ticker=item.get("event_ticker"),
                    side=str(item.get("side", "")),
                    yes_settlement_value_cc=settlement,
                )
            )
        if any(not leg.market_ticker for leg in legs):
            raise RfqParseError("mve_selected_legs item without market_ticker")

        return cls(
            rfq_id=rfq_id,
            market_ticker=market_ticker,
            event_ticker=msg.get("event_ticker"),
            contracts=contracts,
            target_cost_cc=target_cost,
            created_ts=created_ts,
            mve_collection_ticker=msg.get("mve_collection_ticker"),
            legs=tuple(legs),
            raw=dict(msg),
        )
