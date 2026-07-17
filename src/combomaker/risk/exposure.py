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
portfolio value, in dollars, per +1.00 change in P(L settles YES). There are THREE
provenances (P1.8), and consumers must know which they hold (``DeltaProvenance``):
``analytic_leg_deltas`` are INDEPENDENCE PROXIES (∏ of the other selected-side
marginals, signed) and serve the enforced hot-path directional backstop (they are
deliberately the loose MONOTONE bound the mass-acceptance cap binds on — see P0-9);
``structural_leg_deltas`` reads the SAME sensitivity off the coherent Dixon-Coles
scoreline distribution where a game is structurally modelled, so same-game hedges
the proxy assumes away are recognised (analysis/telemetry only, NOT a cap loosener);
and the conditional-MC deltas in ``sim.engine.leg_deltas`` are the slow full-book
refresh. ``leg_deltas_labeled`` dispatches structural-where-available, else proxy.

Mass acceptance (quiet-failure defense + FIX PreferBetterQuote): every open
quote is instantly executable at ANY moment — an accept aimed at a competitor
can land on us. The worst-case book therefore assumes every open quote fills
NOW, each on whichever side is worse for the aggregate being checked
(sign-aligned magnitudes — a conservative upper bound, never an average).

Direction semantics come ONLY from ``Conventions`` (which side we end up long
when a side of our quote is hit).
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CC_PER_DOLLAR, CentiCents, cc_from_prob
from combomaker.core.quantity import CentiContracts
from combomaker.pricing.grouping import game_key

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from combomaker.pricing.structural_api import LegSpec
    from combomaker.sim.structural_book import StructuralConfigView

MarginalProvider = Callable[[str], float | None]
"""market_ticker -> current P(YES), or None when unavailable."""


class DeltaProvenance(Enum):
    """How a set of per-leg deltas was derived — its modelling assumption.

    P1.8 (RISK_ENGINE_AUDIT_ACTION_PLAN.txt): leg deltas are NOT all equal. The
    hot-path ``analytic_leg_deltas`` are INDEPENDENCE PROXIES — the product of the
    other legs' marginals, which silently assumes every leg is independent. Where a
    game is structurally modelled (Dixon-Coles), ``structural_leg_deltas`` derives
    the SAME sensitivity from the coherent scoreline distribution, so same-game
    dependence (an opposing-advance hedge, a BTTS sign flip, a scorer×win tie) is
    recognised instead of assumed away. Tagging the provenance forces every consumer
    to know which it holds — a proxy is a backstop, not a truth.
    """

    INDEPENDENCE_PROXY = "independence_proxy"
    STRUCTURAL_SCENARIO = "structural_scenario"


@dataclass(frozen=True, slots=True)
class LabeledLegDeltas:
    """Per-leg deltas (contracts-equivalent) tagged with their ``DeltaProvenance``.

    ``deltas is None`` iff the deltas were uncomputable (a missing/stale marginal —
    which fails closed upstream, never a p=0.5 default). Consumers that must not
    trust an independence proxy where structure was available read ``provenance``.
    """

    deltas: dict[str, float] | None
    provenance: DeltaProvenance


@dataclass(frozen=True, slots=True)
class LegRef:
    market_ticker: str
    event_ticker: str | None
    side: str  # selected side, "yes"|"no" (validated upstream)


def leg_set_hash(legs: Iterable[LegRef]) -> str:
    """Stable, order-independent identity of a combo's leg SET for the durable
    position ledger (P1.10). Two positions are the same combo iff their sets of
    ``(market_ticker, side)`` selections are identical — so we canonicalise by
    sorting the ``market_ticker|side`` pairs and hashing the joined string. This
    is deterministic across processes/restarts (SHA-256, no salt), unlike Python's
    hash randomisation, so the ledger's ``leg_set_hash`` is a durable join key.

    Fail-closed (CLAUDE.md hard rule 6, defense #2): a combo with NO legs has no
    identity to record, so we RAISE rather than emit a hash of the empty set that
    would silently collide every leg-less write. The caller must have legs.

    ``event_ticker`` is intentionally EXCLUDED: the market_ticker already uniquely
    identifies the outcome market, and event_ticker is nullable on a ``LegRef``, so
    including it would let a present/absent event split one real combo into two
    ledger identities. Side IS included — YES M1 and NO M1 are different positions.
    """
    pairs = sorted(f"{leg.market_ticker}|{leg.side}" for leg in legs)
    if not pairs:
        raise ValueError("leg_set_hash: empty leg set has no durable identity")
    joined = "\n".join(pairs)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


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
    """INDEPENDENCE-PROXY leg deltas in contracts-equivalent (P1.8).

    Each leg's delta is ``position_sign · leg_sign · contracts · ∏(other selected-side
    marginals)`` — the product form ASSUMES the other legs are INDEPENDENT. It is a
    fast, monotone *proxy*, NOT a structural sensitivity: it cannot see a same-game
    hedge or exclusion (two opposing advances, a BTTS yes/no flip, a scorer×win tie),
    so it OVER-states a hedged directional bet. Provenance is
    ``DeltaProvenance.INDEPENDENCE_PROXY``; where a game is structurally modelled use
    ``structural_leg_deltas`` (or the dispatcher ``leg_deltas_labeled``) instead.

    This proxy REMAINS the enforced hot-path directional backstop deliberately: it is
    the loose HARD monotone bound the mass-acceptance directional cap binds on
    (P0-9), and a structural refinement would break that monotonicity (the design
    note above ``_DirEntry`` explains why richer hedge credit lives only in the
    candidate-aware MC, never in the enforced snapshot cap). ``structural_leg_deltas``
    is therefore an analysis/telemetry sensitivity, not a cap loosener.

    Returns None if any marginal is missing (missing data must surface as UNKNOWN
    upstream, never a p=0.5 default — hard rule 6 / quiet-failure defense 2).
    """
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


def structural_leg_deltas(
    position: OpenPosition, marginals: MarginalProvider,
    cfg: StructuralConfigView | None = None,
) -> dict[str, float] | None:
    """STRUCTURAL scenario-sensitivity leg deltas for a SINGLE structurally-modelled
    game (P1.8: "use structural scenario sensitivities where available").

    Same units and sign convention as ``analytic_leg_deltas`` — contracts-equivalent
    dollars of portfolio value per +1.00 in P(leg YES) — but the "other legs
    satisfied" mass is read from the COHERENT Dixon-Coles scoreline distribution the
    live pricer inverts, NOT from a product of independent marginals. Concretely, for
    a long-NO combo the value indicator is ``∏(selected-side leg indicators)`` over
    the joint state, so the exact delta to leg i is::

        position_sign · leg_sign · contracts · Σ_states w · ∏_{j≠i} indicator_j

    Because the co-satisfaction mass ``Σ_states w · ∏_{j≠i} indicator_j`` uses the
    real joint (states where an opposing advance CANNOT co-occur contribute 0), this
    recognises the same-game hedges/exclusions the independence proxy assumes away.
    In the independence limit (mutually independent legs) it collapses to the product
    form, so it is a strict refinement, never a contradiction.

    Returns None (⇒ the caller falls back to the labelled proxy — fail-closed, never a
    default) when the position is not risk-modelled, spans more than one game, is not
    fully representable by the structural model (any leg the DC parser declines —
    corners/cards/other sports), the game fails to invert, or ANY marginal is missing.
    Reuses the live parse/invert/sample/settle contract verbatim through
    ``pricing.structural_api`` (hard rule 8c), so its per-state settlement is
    byte-identical to the pricer's and the structural-book parity gate covers it.
    """
    if not position.risk_modeled or not position.legs:
        return None

    # Lazy imports: keep the pricing<-risk seam narrow and avoid an import cycle at
    # module load; only pay the cost when a structural sensitivity is actually asked.
    import numpy as np

    from combomaker.pricing.grouping import game_key as _game_key
    from combomaker.pricing.structural_api import (
        Advance,
        HalfBtts,
        HalfDraw,
        HalfGoalSpread,
        HalfResult,
        HalfTotalOver,
        MatchFormat,
        PlayerScores,
        half_indicator,
        invert,
        parse_leg,
        parse_match,
        team_goals,
        team_indicator,
    )
    from combomaker.pricing.structural_api import (
        states as enum_states,
    )
    from combomaker.sim.structural_book import StructuralConfigView

    _HALF = (HalfResult, HalfDraw, HalfTotalOver, HalfBtts, HalfGoalSpread)
    c = cfg if cfg is not None else StructuralConfigView()

    # Single game only — a cross-game position has no single scoreline model.
    games = {_game_key(leg.event_ticker) for leg in position.legs if leg.event_ticker}
    if len(games) != 1 or any(leg.event_ticker is None for leg in position.legs):
        return None

    # Alias-aware, order-independent reads (verify follow-up 2026-07-16,
    # mirrors sim.structural_book._try_build_game): the match parses from the
    # GAME KEY — an aliased champion leg's raw blob ('26') parses to no match
    # and silently dropped the position to the independence proxy whenever it
    # iterated first — and the format flag OR-folds over ALL legs so member
    # order cannot flip it. Telemetry/hedge-credit path only (caps bind on
    # the proxy), but it must see the final's book.
    from combomaker.pricing.legtypes import resolve_pricing_alias

    match = parse_match(next(iter(games)))
    if match is None:
        return None
    fmt = MatchFormat.GROUP
    for leg in position.legs:
        series = resolve_pricing_alias(leg.market_ticker).split("-", 1)[0].upper()
        if any(series.startswith(p.upper()) for p in c.knockout_series):
            fmt = MatchFormat.KNOCKOUT
            break

    specs: list[LegSpec] = []
    targets: list[tuple[LegSpec, float]] = []
    for leg in position.legs:
        p_yes = marginals(leg.market_ticker)
        if p_yes is None:
            return None
        spec = parse_leg(leg.market_ticker, match, fmt=fmt)
        if isinstance(spec, str):          # unrepresentable ⇒ not fully structural
            return None
        specs.append(spec)
        targets.append((spec, float(p_yes)))

    try:
        model = invert(
            targets, dc_rho=c.dc_rho, et_factor=c.et_factor, match_format=fmt,
            max_goals=c.max_goals, pens_win_a=c.pens_win_a, half_share=c.half_share,
        )
    except Exception:                      # any StructuralError ⇒ fall back to proxy
        return None

    params = model.params
    need_halves = any(isinstance(s, _HALF) for s in specs)
    if need_halves and not params.with_halves:
        from dataclasses import replace as _replace
        params = _replace(params, with_halves=True)
    st = enum_states(params)
    w = np.asarray(st.w, dtype=np.float64)

    # Per-leg YES-settlement 0/1 over every enumerated state, then flip to the
    # SELECTED side (NO leg ⇒ 1 - indicator). Advance/player legs are settled
    # analytically per state (shootout/scorer coins integrate out to their share:
    # advance on a level state contributes ``pens_win`` mass; a scorer contributes
    # its per-team-goal thinning probability), matching the pricer's marginal.
    sel_ind: list[NDArray[np.float64]] = []
    for k, (leg, spec) in enumerate(zip(position.legs, specs, strict=True)):
        if isinstance(spec, Advance):
            yes = _advance_indicator_mass(st, spec, params)
        elif isinstance(spec, PlayerScores):
            share = float(model.shares.get(k, 0.0))
            n_team = np.asarray(
                team_goals(st, spec.team, spec.include_et), dtype=np.float64
            )
            yes = _player_indicator_mass(n_team, share, spec.min_goals)
        elif isinstance(spec, _HALF):
            yes = np.asarray(half_indicator(st, spec), dtype=np.float64)
        else:
            yes = np.asarray(team_indicator(st, spec, params), dtype=np.float64)
        sel = yes if leg.side == "yes" else (1.0 - yes)
        sel_ind.append(np.clip(sel, 0.0, 1.0))

    contracts = int(position.contracts) / 100
    position_sign = 1.0 if position.our_side is Side.YES else -1.0
    deltas: dict[str, float] = {}
    for i, leg in enumerate(position.legs):
        product_others = np.ones_like(w)
        for j, ind in enumerate(sel_ind):
            if j != i:
                product_others = product_others * ind
        co_mass = float(np.dot(w, product_others))   # Σ w · ∏_{j≠i} indicator_j
        leg_sign = 1.0 if leg.side == "yes" else -1.0
        deltas[leg.market_ticker] = (
            deltas.get(leg.market_ticker, 0.0)
            + position_sign * leg_sign * contracts * co_mass
        )
    return deltas


def _advance_indicator_mass(
    states: object, spec: object, params: object
) -> NDArray[np.float64]:
    """Per-state YES mass for an advance leg: 1 on a decided win, ``pens_win`` on a
    level-after-ET state (the shootout coin integrated out to its share — the same
    marginal the pricer/`_advance_settle` produce in expectation)."""
    import numpy as np

    from combomaker.pricing.structural_api import Team

    if spec.team is Team.A:  # type: ignore[attr-defined]
        us90, them90 = states.a90, states.b90        # type: ignore[attr-defined]
        us_et, them_et = states.a_et, states.b_et    # type: ignore[attr-defined]
        pens = float(params.pens_win_a)              # type: ignore[attr-defined]
    else:
        us90, them90 = states.b90, states.a90        # type: ignore[attr-defined]
        us_et, them_et = states.b_et, states.a_et    # type: ignore[attr-defined]
        pens = 1.0 - float(params.pens_win_a)        # type: ignore[attr-defined]
    us90 = np.asarray(us90)
    them90 = np.asarray(them90)
    us_et = np.asarray(us_et)
    them_et = np.asarray(them_et)
    win = (us90 > them90) | ((us90 == them90) & (us_et > them_et))
    level = (us90 == them90) & (us_et == them_et)
    out: NDArray[np.float64] = win.astype(np.float64) + level.astype(np.float64) * pens
    return out


def _player_indicator_mass(
    n_team: NDArray[np.float64], share: float, min_goals: int
) -> NDArray[np.float64]:
    """Per-state YES mass for a player-scores leg: P(player scores >= min | n team
    goals) integrated per state = ``1 - (1 - share)**n`` for min_goals==1 (the scorer
    coin's expectation), the pricer's per-state player marginal."""
    import numpy as np

    if min_goals <= 1:
        out: NDArray[np.float64] = 1.0 - np.power(1.0 - share, n_team)
        return out
    # min_goals >= 2 is rare; fall back to the exact binomial tail per state.
    from scipy.stats import binom

    return np.asarray(
        [1.0 - binom.cdf(min_goals - 1, int(nt), share) for nt in n_team],
        dtype=np.float64,
    )


def leg_deltas_labeled(
    position: OpenPosition, marginals: MarginalProvider,
    prefer_structural: bool = True,
    cfg: StructuralConfigView | None = None,
) -> LabeledLegDeltas:
    """Leg deltas with explicit provenance (P1.8 dispatcher).

    Returns the STRUCTURAL scenario sensitivity where it is available (a single,
    fully-representable structurally-modelled game) and the labelled INDEPENDENCE
    PROXY otherwise — "structural where available, proxy elsewhere". ``deltas is
    None`` only when even the proxy is uncomputable (a missing marginal, which fails
    closed upstream). This is a telemetry/analysis surface; the enforced hot-path
    directional cap deliberately keeps binding on the monotone independence proxy
    (see ``analytic_leg_deltas``), so this dispatcher never loosens a cap.
    """
    if prefer_structural:
        structural = structural_leg_deltas(position, marginals, cfg=cfg)
        if structural is not None:
            return LabeledLegDeltas(structural, DeltaProvenance.STRUCTURAL_SCENARIO)
    proxy = analytic_leg_deltas(position, marginals)
    return LabeledLegDeltas(proxy, DeltaProvenance.INDEPENDENCE_PROXY)


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
    # P0-9: MUTUAL-EXCLUSION-AWARE directional bound per game, in LOSS-equivalent
    # centi-cents (contracts-equivalent × $1). Opposing-advance long-NO positions
    # NET here (the hedge credit ``delta_by_game`` cannot see, since it sums
    # independence proxies). Monotonic (mass-acceptance dominance preserved) and
    # ≤ the summed-magnitude directional bound; fails closed to that sum on 0 or ≥2
    # ME events. The R2 directional cap binds on THIS; ``delta_by_game`` stays the
    # loose HARD monotone directional backstop for the enforced max_event_delta cap.
    directional_by_game_cc: dict[str, int]
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


# --- P0-9: mutual-exclusion-aware DIRECTIONAL bound ------------------------
# ``_DirEntry`` = (legs_on_this_game, magnitude, requires_all) per position/
# hypothetical touching a game. ``magnitude`` is that entry's DIRECTIONAL
# magnitude on the game — |Σ this-game leg deltas| in contracts-equivalent (a
# nonneg magnitude). ``requires_all`` is True iff the entry loses iff every one
# of its legs is satisfied (a long-NO combo); any other side is COMMON.
#
# WHY A MUTEX-AWARE DIRECTIONAL CAP (P0-9). The R2 directional cap used to bind on
# ``delta_by_game`` — a sum of ``analytic_leg_deltas``, which are INDEPENDENCE
# proxies. Summed independence deltas do NOT recognize an opposing-advance HEDGE:
# long-NO on ARG-advance + long-NO on ENG-advance is short two mutually-exclusive
# outcomes (exactly ONE team advances), so both cannot resolve adverse the
# concentrated way — yet the independence sum treats them as ordinary same-game
# concentration and over-states the directional bet. That made skip_directional_cap
# the largest LEGITIMATE quote blocker post-fanout. The fix does NOT raise the
# limit: it awards hedge credit through the SAME monotone single-ME-event
# max-over-branches fold the LOSS axis uses (``_mutex_game_worst_cc``). Opposing
# advances land in DIFFERENT branches ⇒ they NET (max) instead of summing. It is
# provably >= the largest single directional entry and <= the summed magnitude, and
# adding any entry never lowers it — so the all-accepted mass-acceptance snapshot
# DOMINATES every realizable accepted subset (E2). Richer all-legs / cross-market
# hedge credit that would BREAK monotonicity stays in the candidate-aware MC (P0-1),
# NEVER here. Parity-tested against tools/proto_mutex_directional.py.
#
# The plain summed magnitude (``delta_by_game`` × $1) REMAINS the loose HARD
# monotone directional/model-sensitivity BACKSTOP (the enforced max_event_delta
# mass-acceptance cap still binds on it); this bound is the tighter, hedge-crediting
# measure the %-of-bankroll directional cap binds on.
_DirEntry = tuple[tuple["LegRef", ...], float, bool]


def _mutex_directional_event_bound(entries: list[_DirEntry], event: str) -> float:
    """Max over the ME event's branches of the Σ directional magnitude of entries
    that can lose in that branch. Same partition as ``_mutex_event_bound_cc`` (the
    loss axis) but on the directional magnitude. Monotonic in the entry set."""
    reqs = [
        (_mutex_required(legs, req, event), mag) for legs, mag, req in entries
    ]
    outs = {r[1] for r, _m in reqs if r is not None and r[0] == "is"}
    branches = (*outs, "__OTHER__")
    best = 0.0
    for b in branches:
        s = 0.0
        for r, mag in reqs:
            if r is None:                       # common — pressures every branch
                s += mag
            elif r[0] == "is":
                if b == r[1]:
                    s += mag
            elif b != r[1]:                     # ("not", m) — every branch except m
                s += mag
        if s > best:
            best = s
    return best


def _mutex_directional_game_cc(
    entries: list[_DirEntry], is_me_event: Callable[[str], bool | None] | None
) -> float:
    """Mutual-exclusion-aware upper bound on a game's DIRECTIONAL magnitude (P0-9).

    Nets the game's single RESULT mutually-exclusive event (advance / moneyline) via
    max-over-branches; fails closed to the SUMMED magnitude on 0 or ≥2 ME events (so
    the bound is MONOTONIC — E2 mass-acceptance dominance holds, exactly as the loss
    axis). Always ≤ the summed magnitude and ≥ the largest single entry. Parity-tested
    against tools/proto_mutex_directional.py."""
    summed = sum(mag for _legs, mag, _r in entries)
    if not entries or is_me_event is None:
        return summed
    me_events: list[str] = []
    seen: set[str] = set()
    for legs, _mag, requires in entries:
        if not requires:
            continue
        for leg in legs:
            e = leg.event_ticker
            if e and e not in seen:
                seen.add(e)
                if is_me_event(e) is True:
                    me_events.append(e)
    if len(me_events) != 1:                     # 0 ⇒ no ME; ≥2 ⇒ fail-closed
        return summed
    return _mutex_directional_event_bound(entries, me_events[0])


# --- P1-7: mutex-metadata settlement tripwire -------------------------------
# The netting above (loss axis Stage B + directional axis P0-9) trusts ONE fact
# about an event the metadata flagged ``is_me_event(e) is True``: its outcome
# markets are MUTUALLY EXCLUSIVE, so AT MOST ONE settles YES. That is the exact
# assumption that lets opposing-outcome long-NO positions net to the max instead
# of summing (ARG-advance ⊥ ENG-advance). If the exchange EVER settles two or
# more distinct outcome markets of the SAME netted event YES, that exclusivity
# was FALSE — every netting decision that trusted it UNDER-stated risk. Metadata
# (even explicit-True metadata) is not ground truth; the SETTLEMENT is. This
# tripwire is the settlement-side proof, mirroring the farmed settle-YES tripwire
# (lifecycle.reconcile_combo_settlement): a classification/metadata failure on a
# real position must HALT, never log.
#
# ``settled_yes_by_event`` maps a netted ME event → the set of its DISTINCT
# outcome markets the exchange settled YES. ≥2 ⇒ the exclusivity we netted on was
# violated. Only events we ACTUALLY netted (explicit-True) are audited; an event
# whose flag was None/False was never netted (fail-closed → summed), so a
# multi-YES there was already priced comonotone and is not a tripwire.
def mutex_exclusivity_violations(
    settled_yes_by_event: dict[str, set[str]],
) -> dict[str, set[str]]:
    """Events whose netted mutual-exclusivity was CONTRADICTED by settlement:
    ≥2 distinct outcome markets of the same event settled YES. Pure; returns the
    offending {event: settled-YES markets}. Empty ⇒ every audited event behaved
    mutually exclusively."""
    return {
        event: markets
        for event, markets in settled_yes_by_event.items()
        if len(markets) >= 2
    }


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
        # P0-2 book generations: monotonically-increasing counters bumped on EVERY
        # mutation that can change the portfolio.
        #
        # ``_generation`` (the full book generation) increments on ALL mutations —
        # position add (a confirmed fill / rehydration / reconciliation / reserve),
        # settlement (remove_position), AND quote mutations (an open quote is a
        # mass-acceptance input to the live ``check()`` even though it is not priced
        # by the async MC). This is the plan's "increment on ... and relevant quote
        # mutations" — a general staleness signal for any consumer that reads the
        # whole book.
        #
        # ``_position_generation`` increments ONLY when the POSITION set changes
        # (fills/settlements/rehydration/reconciliation/reservation — never a bare
        # quote upsert/remove). The async book-risk MC prices POSITIONS ONLY (it
        # reads ``self.positions``, never the open quotes), so this is the exact
        # consistency proof the plan's "invalidate on fills and settlements" needs:
        # the snapshot stamps ``input_generation`` from this counter, and the
        # freshness guard discards any async result whose stamped position-generation
        # is no longer current. A fill/settlement invalidates the MC IMMEDIATELY;
        # unrelated quote churn does NOT spuriously invalidate a still-consistent
        # positions-only snapshot. Time age is thereby a SECONDARY guard: a snapshot
        # still time-fresh (~15s) after a fill mutated the portfolio is invalidated
        # at once because its input_generation is stale. Both start at 0 (a fresh
        # book that never mutated); a never-mutated empty book is self-consistent.
        self._generation: int = 0
        self._position_generation: int = 0

    # --- book generations (P0-2) ---

    @property
    def generation(self) -> int:
        """Full book generation: increments on EVERY mutation (position add,
        settlement, rehydration, reconciliation, reservation, and quote upsert/
        remove). A general staleness signal for any whole-book consumer."""
        return self._generation

    @property
    def position_generation(self) -> int:
        """Position-set generation: increments ONLY on a real position mutation
        (fill/settlement/rehydration/reconciliation/reservation), never on a bare
        quote mutation. This is the consistency proof for the async book-risk MC,
        which prices POSITIONS ONLY: a ``BookRiskSnapshot`` is consistent with the
        current portfolio iff its ``input_generation`` equals this value."""
        return self._position_generation

    def _bump_generation(self) -> None:
        self._generation += 1

    def _bump_position_generation(self) -> None:
        # A position mutation is also a book mutation, so both counters advance.
        self._generation += 1
        self._position_generation += 1

    # --- mutation ---

    def add_position(self, position: OpenPosition) -> None:
        self.positions[position.position_id] = position
        self._bump_position_generation()

    def remove_position(self, position_id: str) -> None:
        """Drop a position from the live book. Called once a position SETTLES
        (SettlementHandler, after apply_settlement books it): a settled position
        no longer carries live risk, so it must stop counting toward the enforced
        game/slate/gross/CVaR caps and the daily-P&L mark. Leaving it in would
        (a) inflate the risk view forever as settlements pile up over a long run,
        and (b) make the settlement reconcile re-sum an already-settled position
        against a re-quote's revenue on the same ticker → a false
        HALT_RECONCILIATION_MISMATCH. Idempotent: a missing id is a no-op.

        Bumps the position generation only when a position was actually removed (a
        real settlement mutates the priced book; a no-op removal does not), so an
        in-flight book-risk snapshot is invalidated by every settlement (P0-2)."""
        removed = self.positions.pop(position_id, None)
        if removed is not None:
            self._bump_position_generation()

    def audit_mutex_settlements(
        self, settled_yes_markets: Iterable[str]
    ) -> dict[str, set[str]]:
        """P1-7 settlement tripwire. Given the market tickers the exchange settled
        YES in a batch, return the events whose NETTED mutual-exclusivity was
        contradicted (≥2 distinct outcome markets of one explicit-True ME event
        settled YES). Empty ⇒ no violation.

        The market→event map is derived from the legs of positions we HOLD (the
        only markets our netting ever touched). Only events the metadata flagged
        ``is_me_event(e) is True`` are audited — the SAME explicit-True gate the
        loss/directional netting uses, so an event we never netted (None/False ⇒
        summed comonotone) can never false-trip. Called BEFORE settled positions
        are removed from the book, so the map is complete."""
        if self._is_me_event is None:
            return {}  # no ME dimension was ever used → nothing was netted
        settled = set(settled_yes_markets)
        settled_yes_by_event: dict[str, set[str]] = defaultdict(set)
        for position in self.positions.values():
            for leg in position.legs:
                event = leg.event_ticker
                if (
                    event
                    and leg.market_ticker in settled
                    and self._is_me_event(event) is True
                ):
                    settled_yes_by_event[event].add(leg.market_ticker)
        return mutex_exclusivity_violations(settled_yes_by_event)

    def upsert_quote(self, quote: OpenQuoteRisk) -> None:
        self.open_quotes[quote.quote_id] = quote
        # Quote mutation ⇒ full book generation advances, but NOT the position
        # generation (the async book-risk MC prices positions only, so quote churn
        # must not invalidate a still-consistent positions snapshot).
        self._bump_generation()

    def remove_quote(self, quote_id: str) -> None:
        removed = self.open_quotes.pop(quote_id, None)
        if removed is not None:
            self._bump_generation()

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
        # DIRECTIONAL axis (P0-9): per-game directional entries, each carrying the
        # entry's |Σ this-game leg deltas| magnitude, folded with the SAME monotone
        # mutex bound so opposing-advance hedges net (mass-acceptance dominance kept).
        game_dir_entries: dict[str, list[_DirEntry]] = defaultdict(list)
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
            requires_all = position.our_side is Side.NO
            for game, glegs in pos_legs_by_game.items():
                game_notional[game] += position.gross_settlement_notional_cc
                game_entries[game].append(
                    (tuple(glegs), position.max_loss_cc, requires_all)
                )
            if deltas is not None:
                # Leg market tickers are unique within a position (duplicate
                # legs are rejected by the relationship classifier upstream).
                for leg in position.legs:
                    if leg.event_ticker:
                        delta_game[game_key(leg.event_ticker)] += deltas.get(
                            leg.market_ticker, 0.0
                        )
                # P0-9 directional entry per game: the entry's DIRECTIONAL magnitude
                # on the game is |Σ this-game leg deltas| (contracts-equivalent),
                # carried as LOSS-equivalent cc (× $1). Only priced positions
                # contribute a computable direction; an un-pricable held/reserved
                # holding (deltas is None) carries no directional sensitivity (its
                # whole-account risk is already held by the loss/notional/det caps).
                for game, glegs in pos_legs_by_game.items():
                    game_delta = sum(deltas.get(g.market_ticker, 0.0) for g in glegs)
                    magnitude = abs(game_delta) * CC_PER_DOLLAR
                    game_dir_entries[game].append(
                        (tuple(glegs), magnitude, requires_all)
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
                # P0-9 directional entry for the open quote (mass acceptance): the
                # per-game magnitude is the WORST-SIDE |delta| bound (per_market, the
                # sign-aligned upper bound) summed over this game's legs — a
                # conservative per-quote directional magnitude, mirroring the loss
                # axis's ``worst_loss`` choice. Folded with the SAME monotone mutex
                # bound so an opposing-advance quote nets against a held hedge while
                # the mass snapshot still dominates every realizable accepted subset.
                for game, glegs in q_legs_by_game.items():
                    magnitude = (
                        sum(per_market.get(g.market_ticker, 0.0) for g in glegs)
                        * CC_PER_DOLLAR
                    )
                    game_dir_entries[game].append(
                        (tuple(glegs), magnitude, requires_all)
                    )

        # Fold each game's entries with the mutual-exclusion-aware bound (Stage B).
        game_worst = {
            game: _mutex_game_worst_cc(entries, self._is_me_event)
            for game, entries in game_entries.items()
        }
        # P0-9: fold the directional entries with the SAME monotone mutex bound so
        # opposing-advance hedges net; round to int cc (loss-equivalent, ints only).
        game_directional = {
            game: int(_mutex_directional_game_cc(entries, self._is_me_event))
            for game, entries in game_dir_entries.items()
        }
        return ExposureSnapshot(
            delta_by_market=dict(delta_market),
            delta_by_game=dict(delta_game),
            gross_notional_cc=gross_cc,
            worst_case_loss_by_game_cc=game_worst,
            gross_settlement_notional_by_game_cc=dict(game_notional),
            directional_by_game_cc=game_directional,
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
