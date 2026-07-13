"""Exposure book: open combo positions + open quotes, decomposed to per-leg
deltas and aggregated per market / GAME / collection.

Aggregation key (B2): every per-event aggregate keys on the GAME — the gamecode
after the series prefix (``pricing.grouping.game_key``, the exact key the copula
correlates on) — NOT the raw event_ticker. So a match's market families
(GAME / TOTAL / SPREAD / props) fold into ONE game cluster (the operator's real
risk unit) instead of splitting silently across sibling events. The old
``*_by_event`` field names remain as back-compat aliases over the game-keyed
data.

Two money axes, NEVER summed (B1, R1/R2 invariant #2):
- ``max_loss_cc`` = premium PAID = our TRUE max loss on the side we hold (a long
  NO forfeits its premium if the parlay HITS, not the $1 payout). The LOSS axis.
- ``gross_settlement_notional_cc`` = contracts x $1 = gross settlement notional.
  The CAPITAL-UTILIZATION axis (the "$23.5M payout for $1.8M premium" dimension).
  NOT capital-at-risk and NOT a cash lock — no cash/loss cap may consume it.

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
from combomaker.core.money import CC_PER_DOLLAR, CentiCents, cc_from_prob
from combomaker.core.quantity import CentiContracts
from combomaker.pricing.grouping import game_key

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
        """Our TRUE maximum loss on this position, side-aware.

        Both sides of our quote are BIDS: we PAY ``entry_price_cc`` per contract
        to open, and Kalshi never margin-calls a bought contract. So — for the
        side we actually hold (NO on every sell-only fill) — the worst case is
        that the position expires worthless and we forfeit exactly the premium we
        paid. This is verified ground truth (E3, 2026-07-10 demo): a LONG NO of
        1.00 contract bought at $0.50 loses exactly $0.50 if the parlay HITS
        (settles YES) — not the $1 payout, which the taker collects out of the
        collateral the TAKER posted for their YES.

        This is the LOSS axis. It feeds daily-loss / genuine-P&L-at-risk caps.
        It must NEVER be summed with ``gross_settlement_notional_cc`` (the
        capital-utilization axis) — R1/R2 correctness invariant #2. The two are
        orthogonal.
        """
        return int(self.contracts) * int(self.entry_price_cc) // 100

    @property
    def gross_settlement_notional_cc(self) -> int:
        """Gross settlement notional = contracts x $1; NOT capital-at-risk and
        NOT a cash lock — do not cap cash/loss on this axis.

        For a sell-only long-NO position, when the parlay HITS the taker's YES
        pays $1/contract, collateralized against the bankroll while the position
        is open. This is the "$23.5M payout for $1.8M premium" dimension the P&L
        sweep flagged: a real, dominant CAPITAL-UTILIZATION / concentration
        constraint for a parlay seller — but it is NOT a loss (our loss is
        ``max_loss_cc``). Verified ground truth: 1.00 contract -> $1.00.

        Kept on a distinct axis so the R2 cluster/tail/utilization caps can bind
        on notional while daily-loss caps bind on premium. NEVER summed with
        ``max_loss_cc`` (R1/R2 correctness invariant #2).
        """
        return int(self.contracts) * CC_PER_DOLLAR // 100


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
    # Aggregated per GAME (the gamecode after the series prefix — the copula's
    # correlation key, ``pricing.grouping.game_key``), NOT per raw event_ticker.
    # All market families of one match (GAME/TOTAL/SPREAD/props) fold into ONE
    # game cluster — the operator's actual risk unit — instead of splitting
    # silently across sibling events. Field name kept for consumer compatibility;
    # the KEY is now the game code (B2, 2026-07-12).
    delta_by_game: dict[str, float]
    gross_notional_cc: int                  # Σ max_loss_cc (premium at risk)
    # LOSS axis, per game: Σ max_loss_cc over positions touching the game (the
    # comonotone premium worst case — every combo on the game resolving adverse
    # together). This is genuine P&L-at-risk.
    worst_case_loss_by_game_cc: dict[str, int]
    # GROSS SETTLEMENT NOTIONAL / capital-utilization axis, per game:
    # Σ gross_settlement_notional_cc (contracts x $1) over positions touching
    # the game. NOT capital-at-risk and NOT a cash lock — no cash/loss cap may
    # consume it. NEVER summed with the loss axis (R1/R2 correctness invariant
    # #2). New in B2.
    gross_settlement_notional_by_game_cc: dict[str, int]
    open_quote_count: int
    unknown_marginals: bool                 # any delta was uncomputable

    # --- back-compat aliases (old event-keyed names; now game-keyed data) ------
    # The pre-B2 field names ``delta_by_event`` / ``worst_case_loss_by_event_cc``
    # referred to raw-event aggregation; they now return the game-keyed data (the
    # correct risk unit). Kept so existing consumers/tests read without churn;
    # new code should prefer the ``*_by_game*`` names.
    @property
    def delta_by_event(self) -> dict[str, float]:
        return self.delta_by_game

    @property
    def worst_case_loss_by_event_cc(self) -> dict[str, int]:
        return self.worst_case_loss_by_game_cc


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
        on its per-aggregate WORSE side (sign-aligned magnitude bound).

        Per-market aggregation keys on ``market_ticker``; every per-event
        aggregate keys on the GAME (``pricing.grouping.game_key`` of the leg's
        event_ticker) — the copula's correlation unit — so a match's market
        families cluster into ONE bucket. The E2 mass-acceptance dominance bound
        (sign-aligned magnitude, per-aggregate worse side) is preserved verbatim
        on every axis, including the gross-settlement-notional one.
        """
        delta_market: dict[str, float] = defaultdict(float)
        delta_game: dict[str, float] = defaultdict(float)
        game_worst: dict[str, int] = defaultdict(int)      # LOSS axis (premium)
        game_notional: dict[str, int] = defaultdict(int)   # NOTIONAL axis ($1/ct)
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
            games = {
                game_key(leg.event_ticker) for leg in position.legs if leg.event_ticker
            }
            for game in games:
                game_worst[game] += position.max_loss_cc
                game_notional[game] += position.gross_settlement_notional_cc
            if deltas is not None:
                # Leg market tickers are unique within a position (duplicate
                # legs are rejected by the relationship classifier upstream).
                for leg in position.legs:
                    if leg.event_ticker:
                        delta_game[game_key(leg.event_ticker)] += deltas.get(
                            leg.market_ticker, 0.0
                        )

        if mass_acceptance:
            for quote in self.open_quotes.values():
                hypos = quote.hypothetical_positions(self._conventions)
                if not hypos:
                    continue
                # Worst side on each money axis (independently — the loss and
                # notional worst sides are the same side here, but computed per
                # axis so the invariant never depends on that coincidence).
                gross_cc += max(h.max_loss_cc for h in hypos)
                worst_loss = max(h.max_loss_cc for h in hypos)
                worst_notional = max(h.gross_settlement_notional_cc for h in hypos)
                for game in {
                    game_key(leg.event_ticker) for leg in quote.legs if leg.event_ticker
                }:
                    game_worst[game] += worst_loss
                    game_notional[game] += worst_notional
                # Sign-aligned delta bound per market/game.
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
                        game = game_key(leg.event_ticker)
                        current = delta_game[game]
                        delta_game[game] = current + (
                            per_market[leg.market_ticker]
                            if current >= 0
                            else -per_market[leg.market_ticker]
                        )

        return ExposureSnapshot(
            delta_by_market=dict(delta_market),
            delta_by_game=dict(delta_game),
            gross_notional_cc=gross_cc,
            worst_case_loss_by_game_cc=dict(game_worst),
            gross_settlement_notional_by_game_cc=dict(game_notional),
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
