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
    # P0-4 (usable MC without hiding unmodeled holdings). ``risk_modeled`` is
    # True for a position whose leg marginals we can price (deltas + joint MC).
    # It is FALSE for a CONSERVATIVELY-RESERVED holding — an exchange-held
    # position on a series we don't subscribe (gated-off allowlist), so its leg
    # books, and therefore its marginals, are unavailable. A reserved position
    # STILL counts its EXACT premium loss (``max_loss_cc``), its gross
    # settlement notional, and its known per-game concentration in every
    # deterministic/gross cap — its whole-account risk never vanishes — but it
    # is NEVER decomposed against marginals (so a missing marginal is never
    # scored as an ordinary usable p=0.5) and it is held OUTSIDE the model ES in
    # the portfolio MC (a deterministic unmodeled reserve, not a sampled leg).
    # ``True`` is the default so every existing (priced) position is unchanged.
    risk_modeled: bool = True

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


# --- Stage B: mutual-exclusion-aware per-game worst-case loss ---------------
# ``entries`` = (legs_on_this_game, loss_cc, requires_all) per position/hypothetical
# touching a game. ``requires_all`` is True iff the position LOSES iff every one of
# its legs is satisfied (a long-NO combo — every sell-only fill). A non-NO / unknown
# side passes False ⇒ treated as COMMON (loses in every branch) ⇒ conservative.
#
# DESIGN NOTE — why a SINGLE ME event, max-over-branches, else comonotone (and NOT
# min-over-many-dimensions): the E2 MASS-ACCEPTANCE DOMINANCE invariant requires the
# per-game bound to be MONOTONIC (adding an open quote never lowers it, so the mass
# snapshot dominates every realized acceptance). Recognizing MORE mutual-exclusion
# structure (a second ME event, or a binary yes/no market) REFINES the partition and
# LOWERS the bound — non-monotonic — so an open quote that introduces a hedge could
# push the mass bound BELOW a realized subset that doesn't hold that hedge. That is a
# real safety hole (a taker can accept only the concentrated side and decline the
# hedge). So B nets exactly ONE mutually-exclusive event (the result: advance / 1X2)
# via max-over-branches — provably monotonic + ≤ comonotone — and FAILS CLOSED to the
# comonotone sum on 0 or ≥2 ME events. Full all-legs hedging (BTTS yes/no, corners
# over/under, goalscorers) lives in the structural MC (A1), where the joint state is
# sampled and the bound is a probability, not a monotone worst-case cap.
_MutexEntry = tuple[tuple["LegRef", ...], int, bool]


def _mutex_required(
    legs: tuple[LegRef, ...], requires: bool, event: str
) -> tuple[str, str] | None:
    """The outcome this entry REQUIRES to lose on the ME ``event``: ("is", market)
    | ("not", market) | None (COMMON — loses in every branch). A YES leg on outcome
    m requires m; a NO leg on m requires NOT-m; prefer a YES leg (tightest)."""
    if not requires:
        return None
    yes = [g.market_ticker for g in legs if g.event_ticker == event and g.side == "yes"]
    if yes:
        return ("is", yes[0])
    no = [g.market_ticker for g in legs if g.event_ticker == event and g.side == "no"]
    if no:
        return ("not", no[0])
    return None


def _mutex_event_bound_cc(entries: list[_MutexEntry], event: str) -> int:
    """Max over the ME event's branches of the Σ loss of entries that can lose in
    that branch. Branches = every required YES-outcome + an ``__OTHER__`` catch-all
    (so a NO-leg's 'some other outcome' is always counted — never under-stated when
    an outcome is absent from the book). Monotonic in the entry set."""
    reqs = [(_mutex_required(legs, req, event), loss) for legs, loss, req in entries]
    outs = {r[1] for r, _l in reqs if r is not None and r[0] == "is"}
    branches = (*outs, "__OTHER__")
    best = 0
    for b in branches:
        s = 0
        for r, loss in reqs:
            if r is None:                       # common — loses in every branch
                s += loss
            elif r[0] == "is":
                if b == r[1]:
                    s += loss
            elif b != r[1]:                     # ("not", m) — every branch except m
                s += loss
        if s > best:
            best = s
    return best


def _mutex_game_worst_cc(
    entries: list[_MutexEntry], is_me_event: Callable[[str], bool | None] | None
) -> int:
    """Mutual-exclusion-aware upper bound on a game's worst-case loss (Stage B).

    Nets the game's single RESULT mutually-exclusive event (advance / moneyline) via
    max-over-branches; fails closed to the comonotone sum on 0 or ≥2 ME events (so
    the bound is MONOTONIC — E2 mass-acceptance dominance holds; see the design note
    above). Always ≤ comonotone and ≥ the largest single entry. Parity-tested against
    tools/proto_mutex_game_cap.py."""
    comonotone = sum(loss for _legs, loss, _r in entries)
    if not entries or is_me_event is None:
        return comonotone
    me_events: list[str] = []
    seen: set[str] = set()
    for legs, _loss, requires in entries:
        if not requires:
            continue
        for leg in legs:
            e = leg.event_ticker
            if e and e not in seen:
                seen.add(e)
                if is_me_event(e) is True:
                    me_events.append(e)
    if len(me_events) != 1:                     # 0 ⇒ no ME; ≥2 ⇒ fail-closed
        return comonotone
    return _mutex_event_bound_cc(entries, me_events[0])


class ExposureBook:
    def __init__(
        self,
        conventions: Conventions,
        is_me_event: Callable[[str], bool | None] | None = None,
    ) -> None:
        self._conventions = conventions
        # Stage B (2026-07-15): the per-GAME worst-case loss is a MUTUAL-EXCLUSION-
        # AWARE bound, not the old comonotone sum. ``is_me_event`` answers "is this
        # event's market family mutually exclusive?" (MetadataCache.
        # event_mutually_exclusive). None ⇒ no ME-event dimension is used (the cap
        # falls back to the comonotone sum + binary-market splits only) — a
        # fresh/paper build with no metadata is byte-identical to the old cap on
        # non-ME books. See ``_mutex_game_worst_cc`` and tools/proto_mutex_game_cap.py.
        self._is_me_event = is_me_event
        self.positions: dict[str, OpenPosition] = {}
        self.open_quotes: dict[str, OpenQuoteRisk] = {}

    # --- mutation ---

    def add_position(self, position: OpenPosition) -> None:
        self.positions[position.position_id] = position

    def remove_position(self, position_id: str) -> None:
        """Drop a position from the live book. Called once a position SETTLES
        (SettlementHandler, after apply_settlement books it): a settled position
        no longer carries live risk, so it must stop counting toward the enforced
        game/slate/gross/CVaR caps and the daily-P&L mark. Leaving it in would
        (a) inflate the risk view forever as settlements pile up over a long run,
        and (b) make the settlement reconcile re-sum an already-settled position
        against a re-quote's revenue on the same ticker → a false
        HALT_RECONCILIATION_MISMATCH. Idempotent: a missing id is a no-op."""
        self.positions.pop(position_id, None)

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
        # LOSS axis (premium): collect per-game entries, then fold each game with
        # the MUTUAL-EXCLUSION-AWARE bound (Stage B) instead of a comonotone sum.
        game_entries: dict[str, list[_MutexEntry]] = defaultdict(list)
        game_notional: dict[str, int] = defaultdict(int)   # NOTIONAL axis ($1/ct)
        gross_cc = 0
        unknown = False

        committed = list(self.positions.values())
        n_committed = len(committed)
        for i, position in enumerate(committed + list(extra_positions)):
            is_committed = i < n_committed
            gross_cc += position.max_loss_cc
            # P0-4: a CONSERVATIVELY-RESERVED holding (risk_modeled=False) has no
            # available marginals — we do NOT even query them (so a missing
            # marginal is never turned into an ordinary usable p=0.5). Its exact
            # premium loss, gross notional, and per-game concentration are still
            # folded below; it simply carries no computable delta.
            deltas = (
                None if not position.risk_modeled
                else analytic_leg_deltas(position, marginals)
            )
            if deltas is None:
                # A HELD (committed) position whose live marginal is temporarily
                # unavailable — e.g. a rehydrated position's leg book not yet
                # subscribed after a restart, or a conservatively-reserved gated
                # holding — still contributes its KNOWN max_loss to the loss/
                # notional/game caps (below), but has no computable delta. It must
                # NOT set ``unknown_marginals``: that flag fail-closes the WHOLE
                # check (SKIP_CLASSIFIER_UNKNOWN), so one un-pricable held position
                # would veto ALL quoting (verified live 2026-07-15). Only a
                # CANDIDATE / open-quote we cannot decompose is a genuine
                # "can't assess this fill" and fails closed.
                if not is_committed:
                    unknown = True
            else:
                for ticker, delta in deltas.items():
                    delta_market[ticker] += delta
            # Partition the position's legs by game; each game it touches gets an
            # entry carrying ONLY that game's legs (so the per-game mutex partition
            # sees only this game's outcomes) + the FULL position loss (a combo
            # loses fully, attributed to each game's worst case as before).
            pos_legs_by_game: dict[str, list[LegRef]] = defaultdict(list)
            for leg in position.legs:
                if leg.event_ticker:
                    pos_legs_by_game[game_key(leg.event_ticker)].append(leg)
            for game, glegs in pos_legs_by_game.items():
                game_notional[game] += position.gross_settlement_notional_cc
                game_entries[game].append(
                    (tuple(glegs), position.max_loss_cc, position.our_side is Side.NO)
                )
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
                worst_hypo = max(hypos, key=lambda h: h.max_loss_cc)
                worst_loss = worst_hypo.max_loss_cc
                worst_notional = max(h.gross_settlement_notional_cc for h in hypos)
                # requires_all: a long-NO hypothetical loses iff every leg is
                # satisfied → the mutex partition applies; any other side ⇒ COMMON.
                requires_all = worst_hypo.our_side is Side.NO
                q_legs_by_game: dict[str, list[LegRef]] = defaultdict(list)
                for leg in quote.legs:
                    if leg.event_ticker:
                        q_legs_by_game[game_key(leg.event_ticker)].append(leg)
                for game, glegs in q_legs_by_game.items():
                    game_notional[game] += worst_notional
                    game_entries[game].append((tuple(glegs), worst_loss, requires_all))
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

        # Fold each game's entries with the mutual-exclusion-aware bound (Stage B).
        game_worst = {
            game: _mutex_game_worst_cc(entries, self._is_me_event)
            for game, entries in game_entries.items()
        }
        return ExposureSnapshot(
            delta_by_market=dict(delta_market),
            delta_by_game=dict(delta_game),
            gross_notional_cc=gross_cc,
            worst_case_loss_by_game_cc=game_worst,
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
