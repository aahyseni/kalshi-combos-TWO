"""Exposure book: open combo positions + open quotes, decomposed to per-leg
deltas and aggregated per market / event / collection.

Delta convention: exposure to leg L is in contracts-equivalent — the change in
portfolio value, in dollars, per +1.00 change in P(L settles YES). Analytic
independence deltas (∏ of the other selected-side marginals, signed) serve the
hot path; the conditional-MC deltas in ``sim.engine.leg_deltas`` are for the
slower full-book refresh.

Mass acceptance (quiet-failure defense + FIX PreferBetterQuote): every open
quote is instantly executable at ANY moment — an accept aimed at a competitor
can land on us. The worst-case book therefore assumes every open quote fills
NOW, each on whichever side is worse for the aggregate being checked
(sign-aligned magnitudes — a conservative upper bound, never an average).

Direction semantics come ONLY from ``Conventions`` (which side we end up long
when a side of our quote is hit).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents, cc_from_prob
from combomaker.core.quantity import CentiContracts

MarginalProvider = Callable[[str], float | None]
"""market_ticker -> current P(YES), or None when unavailable."""


@dataclass(frozen=True, slots=True)
class LegRef:
    market_ticker: str
    event_ticker: str | None
    side: str  # selected side, "yes"|"no" (validated upstream)


@dataclass(frozen=True, slots=True)
class OpenPosition:
    position_id: str
    combo_ticker: str
    collection: str | None
    our_side: Side               # from Conventions at fill time
    contracts: CentiContracts
    entry_price_cc: CentiCents   # what we paid per contract
    legs: tuple[LegRef, ...]
    # True iff this position came from FARMING a logically-impossible combo
    # (we are long the certain-NO side). Such a combo must settle NO; if it
    # ever settles YES, that is a classification/settlement failure the
    # settlement guard turns into HALT_RECONCILIATION_MISMATCH.
    farmed: bool = False

    @property
    def max_loss_cc(self) -> int:
        """We always PAY our bid to open — worst case loses the full price."""
        return int(self.contracts) * int(self.entry_price_cc) // 100


@dataclass(frozen=True, slots=True)
class OpenQuoteRisk:
    quote_id: str
    rfq_id: str
    combo_ticker: str
    collection: str | None
    yes_bid_cc: CentiCents       # 0 = side declined
    no_bid_cc: CentiCents
    contracts: CentiContracts
    legs: tuple[LegRef, ...]

    def hypothetical_positions(self, conventions: Conventions) -> list[OpenPosition]:
        """The position each acceptable side would create, at quoted price."""
        out: list[OpenPosition] = []
        for accepted, bid in ((Side.YES, self.yes_bid_cc), (Side.NO, self.no_bid_cc)):
            if bid == 0:
                continue
            out.append(
                OpenPosition(
                    position_id=f"{self.quote_id}:{accepted}",
                    combo_ticker=self.combo_ticker,
                    collection=self.collection,
                    our_side=conventions.maker_position_side(accepted),
                    contracts=self.contracts,
                    entry_price_cc=bid,
                    legs=self.legs,
                )
            )
        return out


def analytic_leg_deltas(
    position: OpenPosition, marginals: MarginalProvider
) -> dict[str, float] | None:
    """Independence deltas in contracts-equivalent; None if any marginal is
    missing (missing data must surface as UNKNOWN upstream, not zero)."""
    selected: list[float] = []
    for leg in position.legs:
        p_yes = marginals(leg.market_ticker)
        if p_yes is None:
            return None
        selected.append(p_yes if leg.side == "yes" else 1.0 - p_yes)

    contracts = int(position.contracts) / 100
    position_sign = 1.0 if position.our_side is Side.YES else -1.0
    deltas: dict[str, float] = {}
    for i, leg in enumerate(position.legs):
        product_others = 1.0
        for j, m in enumerate(selected):
            if j != i:
                product_others *= m
        leg_sign = 1.0 if leg.side == "yes" else -1.0
        deltas[leg.market_ticker] = (
            deltas.get(leg.market_ticker, 0.0)
            + position_sign * leg_sign * contracts * product_others
        )
    return deltas


@dataclass
class ExposureSnapshot:
    delta_by_market: dict[str, float]
    delta_by_event: dict[str, float]
    gross_notional_cc: int                  # Σ contracts × entry price
    worst_case_loss_by_event_cc: dict[str, int]
    open_quote_count: int
    unknown_marginals: bool                 # any delta was uncomputable


class ExposureBook:
    def __init__(self, conventions: Conventions) -> None:
        self._conventions = conventions
        self.positions: dict[str, OpenPosition] = {}
        self.open_quotes: dict[str, OpenQuoteRisk] = {}

    # --- mutation ---

    def add_position(self, position: OpenPosition) -> None:
        self.positions[position.position_id] = position

    def upsert_quote(self, quote: OpenQuoteRisk) -> None:
        self.open_quotes[quote.quote_id] = quote

    def remove_quote(self, quote_id: str) -> None:
        self.open_quotes.pop(quote_id, None)

    # --- snapshots ---

    def snapshot(
        self,
        marginals: MarginalProvider,
        *,
        mass_acceptance: bool,
        extra_positions: Iterable[OpenPosition] = (),
    ) -> ExposureSnapshot:
        """Current exposures; with ``mass_acceptance`` every open quote fills
        on its per-aggregate WORSE side (sign-aligned magnitude bound)."""
        delta_market: dict[str, float] = defaultdict(float)
        delta_event: dict[str, float] = defaultdict(float)
        event_worst: dict[str, int] = defaultdict(int)
        gross_cc = 0
        unknown = False

        real_positions = list(self.positions.values()) + list(extra_positions)
        for position in real_positions:
            gross_cc += position.max_loss_cc
            deltas = analytic_leg_deltas(position, marginals)
            if deltas is None:
                unknown = True
            else:
                for ticker, delta in deltas.items():
                    delta_market[ticker] += delta
            events = {leg.event_ticker for leg in position.legs if leg.event_ticker}
            for event in events:
                event_worst[event] += position.max_loss_cc
            if deltas is not None:
                # Leg market tickers are unique within a position (duplicate
                # legs are rejected by the relationship classifier upstream).
                for leg in position.legs:
                    if leg.event_ticker:
                        delta_event[leg.event_ticker] += deltas.get(leg.market_ticker, 0.0)

        if mass_acceptance:
            for quote in self.open_quotes.values():
                hypos = quote.hypothetical_positions(self._conventions)
                if not hypos:
                    continue
                # Worst notional side.
                gross_cc += max(h.max_loss_cc for h in hypos)
                for event in {leg.event_ticker for leg in quote.legs if leg.event_ticker}:
                    event_worst[event] += max(h.max_loss_cc for h in hypos)
                # Sign-aligned delta bound per market/event.
                per_market: dict[str, float] = defaultdict(float)
                for hypo in hypos:
                    deltas = analytic_leg_deltas(hypo, marginals)
                    if deltas is None:
                        unknown = True
                        continue
                    for ticker, delta in deltas.items():
                        per_market[ticker] = max(per_market[ticker], abs(delta))
                for ticker, magnitude in per_market.items():
                    current = delta_market[ticker]
                    delta_market[ticker] = current + (
                        magnitude if current >= 0 else -magnitude
                    )
                for leg in quote.legs:
                    if leg.event_ticker and leg.market_ticker in per_market:
                        current = delta_event[leg.event_ticker]
                        delta_event[leg.event_ticker] = current + (
                            per_market[leg.market_ticker]
                            if current >= 0
                            else -per_market[leg.market_ticker]
                        )

        return ExposureSnapshot(
            delta_by_market=dict(delta_market),
            delta_by_event=dict(delta_event),
            gross_notional_cc=gross_cc,
            worst_case_loss_by_event_cc=dict(event_worst),
            open_quote_count=len(self.open_quotes),
            unknown_marginals=unknown,
        )


@dataclass(frozen=True, slots=True)
class MtMResult:
    value_cc: int          # current portfolio value at fair
    cost_cc: int           # what we paid
    unrealized_cc: int     # value − cost


def mark_to_market(
    positions: Iterable[OpenPosition], joint_fair: Callable[[OpenPosition], float | None]
) -> MtMResult | None:
    """Portfolio MTM at model fair; None if any position can't be marked."""
    value = 0
    cost = 0
    for position in positions:
        fair = joint_fair(position)
        if fair is None:
            return None
        payout_prob = fair if position.our_side is Side.YES else 1.0 - fair
        value += int(cc_from_prob(payout_prob)) * int(position.contracts) // 100
        cost += position.max_loss_cc
    return MtMResult(value_cc=value, cost_cc=cost, unrealized_cc=value - cost)
