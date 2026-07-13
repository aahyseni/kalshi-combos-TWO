"""Inventory-aware quote skew as a PURE function (Phase 5, R3 Part A).

Sibling of ``risk/lastlook.py``: takes warm in-memory state (the exposure
snapshot the lifecycle already computed for its limit checks + the candidate's
per-leg deltas) and returns a single ``int`` of skew in NO-bid centi-cents. No
I/O, no clock reads, no network.

THE SIGN (load-bearing — R3 §A0/§A6). We are a SELL-ONLY parlay seller: every
combo fill leaves us LONG NO, and the only live lever is ``no_bid`` (the implied
YES ask we show the requester is ``$1 − no_bid``). In ``pricing/quote.py`` the
skew enters as::

    no_raw = ($1 − fair) − half − fee_no + inventory_skew_cc

so a POSITIVE ``inventory_skew_cc`` at the PRICER RAISES ``no_bid`` ⇒ LOWERS the
implied YES ask ($1 − no_bid) ⇒ the combo gets CHEAPER ⇒ we sell MORE. To WIDEN
(sell less) we must pass a NEGATIVE number to the pricer, and to REBATE (sell
more) a POSITIVE one. That is the OPPOSITE of the classifier convention below,
so the flip is applied ONCE, at the pricer boundary (``applied_cc``), keeping the
honest classifier direction readable in every shadow log and internal decomp.

The CLASSIFIER convention (``skew_cc`` and the ``concentration_cc`` /
``offset_cc`` decomposition) reads the intuitive way:

- **CONCENTRATING candidate ⇒ ``skew_cc`` ≥ 0** (adds to a game's net per-game
  direction). We must WIDEN (sell less), so ``applied_cc`` NEGATES it ⇒ a
  negative number lowers ``no_bid`` ⇒ dearer combo ⇒ sell less. ✓
- **OFFSETTING candidate ⇒ ``skew_cc`` ≤ 0** (opposes the book's per-game
  direction). We want to WIN MORE of that flattening flow, so ``applied_cc``
  NEGATES it ⇒ a positive number raises ``no_bid`` ⇒ cheaper combo ⇒ sell more. ✓

So the honest classifier is::

    skew_cc(C) =  Σ_game concentration_term(game)     # ≥ 0, CONCENTRATING
                − Σ_game offset_term(game)            # ≥ 0, OFFSETTING
      clamped to [−skew_max_tighten_cc, +skew_max_widen_cc]

and the pricer receives ``applied_cc = −skew_cc`` (when enabled). Because the
NEGATION swaps sides, the OFFSETTING rebate — now the POSITIVE, no_bid-RAISING,
free-money-dangerous direction at the pricer — is the one bounded by the small
``skew_max_tighten_cc`` cap (and doubly contained by the free-money clamp in
``construct_quote``), exactly as intended.

Every term is driven PRIMARILY off the per-GAME aggregates (the real risk unit,
2026-07-12 sweep): ``snapshot.delta_by_game`` (signed net direction) and the two
per-game headroom maps (``worst_case_loss_by_game_cc`` for loss utilisation,
``gross_settlement_notional_by_game_cc`` for the notional / capital-tie-up axis).
The candidate's own per-game delta comes from ``analytic_leg_deltas`` on the hot
path; an OPTIONAL slow-path per-game ΔES cache (from the Phase-4
``sim/book_risk`` machinery) may override the analytic *direction* when present,
else we fall back to the analytic sign. Correlations / ΔES are NEVER invented
here — a game absent from the cache uses the analytic direction, a game with an
empty book contributes exactly 0.

DARK SHIP. ``SkewConfig.enabled`` defaults False. This function always COMPUTES
the honest skew (so it can be logged as a shadow classifier), but the caller
passes 0 into ``engine.price`` while disabled — a zero-P&L shadow. Only a
markout-validated enable flips it live (never refit on a P&L window).
"""

from __future__ import annotations

from dataclasses import dataclass

from combomaker.core.conventions import Conventions
from combomaker.risk.exposure import (
    ExposureSnapshot,
    MarginalProvider,
    OpenPosition,
    analytic_leg_deltas,
)


@dataclass(frozen=True, slots=True)
class SkewParams:
    """Structural weights + hard caps for the inventory skew.

    Weights (``w_conc`` / ``w_off``) and the convexity ``gamma`` are structural
    knobs tuned on exposure-vs-markout, NEVER on a P&L window. The two caps are
    HARD safety, not tuning. They bound the CLASSIFIER ``skew_cc`` (before the
    ``applied_cc`` negation that flips it into the pricer):

    - ``skew_max_widen_cc`` bounds the concentrating (positive ``skew_cc``) side.
      At the pricer this NEGATES to a WIDEN (lower ``no_bid``, dearer combo, sell
      less). Widening is safe (it only makes us sell less), but a cap (~600cc)
      stops a mispriced game delta posting an absurd near-$1 ask that looks like
      a fat-finger.
    - ``skew_max_tighten_cc`` bounds the offsetting (negative ``skew_cc``) rebate.
      At the pricer this NEGATES to a POSITIVE ``inventory_skew_cc`` that RAISES
      ``no_bid`` toward the free-money cap — the dangerous side. It is ALREADY
      doubly contained by the free-money clamp in ``construct_quote`` (the clamp
      fires after skew, the capture invariant re-checks), so the rebate can
      shrink our edge but never make an arb quote. The cap (~150cc, ~½ base
      width) just stops us rebating away the whole markup chasing a balance.
    """

    w_conc: float = 1.0
    w_off: float = 1.0
    gamma: float = 2.0
    skew_max_widen_cc: int = 600
    skew_max_tighten_cc: int = 150
    enabled: bool = False

    def validate(self) -> None:
        if self.w_conc < 0.0 or self.w_off < 0.0:
            raise ValueError(
                f"skew weights must be >= 0 (w_conc={self.w_conc}, w_off={self.w_off})"
            )
        if self.gamma <= 0.0:
            raise ValueError(f"skew gamma must be > 0, got {self.gamma}")
        if self.skew_max_widen_cc < 0 or self.skew_max_tighten_cc < 0:
            raise ValueError(
                "skew caps must be >= 0 "
                f"(widen={self.skew_max_widen_cc}, tighten={self.skew_max_tighten_cc})"
            )


@dataclass(frozen=True, slots=True)
class SkewLimits:
    """The per-game headroom denominators the utilisation ramps divide by.

    These are the SAME hard-dollar limits the ``LimitChecker`` enforces, passed
    in so the skew's ``util`` measures how little headroom is left before a real
    cap binds (the last combos before a limit pay the most — a convex ramp).
    ``max_event_delta_contracts`` is in whole contracts; the loss/notional caps
    are in dollars and converted to cc internally. All must be > 0.
    """

    max_event_delta_contracts: float
    max_event_worst_case_loss_dollars: float
    max_event_gross_notional_dollars: float


@dataclass(frozen=True, slots=True)
class GameSkewCache:
    """OPTIONAL slow-path per-game direction hint from the Phase-4 sim.

    ``direction_by_game`` maps a game key to the sign of that game's marginal
    ΔES under the copula (+1 = the book is net-adverse in a direction such that
    a candidate ALIGNED with ``delta_by_game`` concentrates; −1 = inverted). It
    is read ONLY to override the analytic per-game direction when the candidate's
    game is present; an absent game falls back to the analytic sign. Correlations
    are never invented here — this cache is populated off the hot path from
    ``sim/book_risk`` (``per_game_tail_cc``) or left empty, in which case the skew
    is a pure analytic-direction computation.
    """

    direction_by_game: dict[str, int]

    def direction(self, game: str) -> int | None:
        return self.direction_by_game.get(game)


@dataclass(frozen=True, slots=True)
class InventorySkew:
    """The computed skew + its decomposition, for shadow logging.

    ``skew_cc`` is the honest CLASSIFIER value (≥ 0 concentrating, ≤ 0
    offsetting) — the intuitive-direction number a human reads in a shadow log.
    ``applied_cc`` is what the caller passes to ``engine.price``: the classifier
    NEGATED (pricer-frame) when enabled, a hard 0 while dark. ``shadow_applied_cc``
    is the would-be pricer-frame value REGARDLESS of the dark flag — the
    correctly-signed signal the pooled shadow-markout gate must study (studying
    ``applied_cc`` while dark would only ever see 0). ``concentration_cc`` /
    ``offset_cc`` are the (non-negative) halves before the net + clamp, so a
    shadow log can see which side drove the number.
    """

    skew_cc: int
    concentration_cc: int
    offset_cc: int
    per_game: tuple[tuple[str, int], ...]  # (game, signed contribution cc)
    enabled: bool

    @property
    def applied_cc(self) -> int:
        """The value the caller actually passes to the pricer: the honest skew
        NEGATED (the pricer's ``+ inventory_skew_cc`` on ``no_bid`` runs opposite
        the classifier convention — a CONCENTRATING ``skew_cc >= 0`` must WIDEN,
        i.e. LOWER ``no_bid``, so it enters the pricer negative), when enabled;
        a hard 0 while dark (a zero-P&L shadow). This is the SINGLE place the
        classifier→pricer sign flip lives."""
        return -self.skew_cc if self.enabled else 0

    @property
    def shadow_applied_cc(self) -> int:
        """The pricer-frame value the skew WOULD apply if enabled (``−skew_cc``),
        independent of the dark flag. Logged so the pooled shadow-markout
        validation gate studies the SAME sign it would live-apply — the gate runs
        entirely while dark, where ``applied_cc`` is pinned to 0."""
        return -self.skew_cc


def _sign(x: float) -> int:
    if x > 0.0:
        return 1
    if x < 0.0:
        return -1
    return 0


def compute_inventory_skew(
    candidate: OpenPosition,
    snapshot: ExposureSnapshot,
    marginals: MarginalProvider,
    conventions: Conventions,
    limits: SkewLimits,
    params: SkewParams,
    *,
    cache: GameSkewCache | None = None,
) -> InventorySkew:
    """Compute the inventory skew for ``candidate`` against the current book.

    Pure. ``candidate`` is the hypothetical NO position a fill would create (the
    lifecycle builds exactly this for its limit check — reuse it). ``snapshot`` is
    the book's current per-game aggregates (already computed for the limits).
    ``marginals`` provides the candidate's own per-leg deltas via
    ``analytic_leg_deltas``. ``limits`` gives the headroom denominators;
    ``cache`` optionally overrides the per-game direction from the Phase-4 sim.

    Returns an :class:`InventorySkew` whose ``applied_cc`` is 0 while
    ``params.enabled`` is False (dark ship) and the honest ``skew_cc`` otherwise.
    ``skew_cc`` itself is ALWAYS the honest number (for shadow logging).
    """
    _ = conventions  # candidate.our_side already carries the side; kept for parity
    cand_deltas = analytic_leg_deltas(candidate, marginals)

    # Per-game candidate delta (contracts-equivalent), aggregated the same way
    # the book does — Σ over the legs the candidate touches per game.
    cand_by_game: dict[str, float] = {}
    if cand_deltas is not None:
        for leg in candidate.legs:
            if leg.event_ticker is None:
                continue
            game = _game_key(leg.event_ticker)
            cand_by_game[game] = cand_by_game.get(game, 0.0) + cand_deltas.get(
                leg.market_ticker, 0.0
            )

    max_delta = limits.max_event_delta_contracts
    max_loss_cc = limits.max_event_worst_case_loss_dollars * 10_000.0
    max_notional_cc = limits.max_event_gross_notional_dollars * 10_000.0

    concentration = 0.0
    offset = 0.0
    per_game: list[tuple[str, int]] = []

    for game, cand_delta in cand_by_game.items():
        net = snapshot.delta_by_game.get(game, 0.0)
        d_e = abs(cand_delta)
        if d_e == 0.0:
            continue
        # Empty book for this game ⇒ nothing to concentrate into or offset ⇒ 0.
        if net == 0.0:
            per_game.append((game, 0))
            continue

        # Direction: analytic by default; the slow-path cache may override which
        # way the book is adverse for this game (copula ΔES sign). We never
        # INVENT correlation — an absent cache game keeps the analytic sign.
        book_dir = _sign(net)
        if cache is not None:
            hint = cache.direction(game)
            if hint is not None and hint != 0:
                book_dir = hint
        cand_dir = _sign(cand_delta)
        aligns = cand_dir == book_dir

        # Utilisation: how little headroom is left before a real cap binds. Take
        # the MAX over the delta / loss / notional axes so the tightest binds.
        util = 0.0
        if max_delta > 0.0:
            util = max(util, min(1.0, abs(net) / max_delta))
        loss_cc = snapshot.worst_case_loss_by_game_cc.get(game, 0)
        if max_loss_cc > 0.0:
            util = max(util, min(1.0, loss_cc / max_loss_cc))
        notional_cc = snapshot.gross_settlement_notional_by_game_cc.get(game, 0)
        if max_notional_cc > 0.0:
            util = max(util, min(1.0, notional_cc / max_notional_cc))

        if aligns:
            # concentration_term = d_e · f(util), f(u) = u**gamma (convex): a
            # near-empty book is nearly free, a near-limit book pays the full
            # widen. WIDEN (positive) when the candidate ADDS to the net.
            term = params.w_conc * d_e * (util**params.gamma)
            concentration += term
            per_game.append((game, int(round(term))))
        else:
            # offset_term = min(d_e, |net|) · g(util), g(u) = u: a REBATE
            # (negative) bounded by how much you actually offset and how
            # overweight you were. A game you have no position in earns nothing
            # (net==0 handled above), and you only rebate up to the amount offset.
            rebate = params.w_off * min(d_e, abs(net)) * util
            offset += rebate
            per_game.append((game, -int(round(rebate))))

    conc_cc = int(round(concentration))
    off_cc = int(round(offset))
    raw = conc_cc - off_cc
    # Clamp: [−skew_max_tighten_cc, +skew_max_widen_cc]. The tighten side is the
    # dangerous one and is doubly contained (here + the free-money clamp).
    skew_cc = max(-params.skew_max_tighten_cc, min(params.skew_max_widen_cc, raw))

    return InventorySkew(
        skew_cc=skew_cc,
        concentration_cc=conc_cc,
        offset_cc=off_cc,
        per_game=tuple(per_game),
        enabled=params.enabled,
    )


def _game_key(event_ticker: str) -> str:
    # Local import to keep the module dependency-light and mirror the exact key
    # the exposure book aggregates on (pricing.grouping.game_key).
    from combomaker.pricing.grouping import game_key

    return game_key(event_ticker)


# ---------------------------------------------------------------------------
# Widen-vs-DECLINE (R3 Part R2). On NORMAL/uncertain flow NEAR a cap, DECLINE
# rather than post a wide quote — widening a thin book near a limit only attracts
# hitters (our own P&L-sweep finding). SHADOW by default: a would-be decision is
# LOGGED with zero live impact until an operator enables it.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WidenPolicyParams:
    """When to prefer DECLINE over a wide quote.

    ``enabled`` DARK by default (would-be decision logged, quote still goes out).
    ``util_threshold`` is the per-game utilisation (0..1, same ``util`` the skew
    ramps on) above which a candidate is "near a cap". A candidate is declined
    only when it is BOTH near a cap AND concentrating (adds to the net) — an
    OFFSETTING candidate near a cap still helps balance the book, so it is never
    declined by this policy (that would spurn the flow that flattens us).
    """

    enabled: bool = False
    util_threshold: float = 0.75

    def validate(self) -> None:
        if not 0.0 < self.util_threshold <= 1.0:
            raise ValueError(
                f"widen util_threshold must be in (0, 1], got {self.util_threshold}"
            )


@dataclass(frozen=True, slots=True)
class WidenDecision:
    """Result of the widen-vs-decline policy.

    ``would_decline`` is the honest verdict (for shadow logging); ``applied`` is
    whether it takes effect live (``would_decline`` AND ``enabled``). ``max_util``
    is the tightest per-game utilisation seen (why it fired)."""

    would_decline: bool
    applied: bool
    max_util: float
    reason: str


def decide_widen_or_decline(
    skew: InventorySkew,
    snapshot: ExposureSnapshot,
    candidate: OpenPosition,
    limits: SkewLimits,
    params: WidenPolicyParams,
) -> WidenDecision:
    """Pure widen-vs-decline verdict, per-GAME.

    Fires (``would_decline`` True) when SOME touched game is BOTH (a)
    CONCENTRATING — the candidate adds to that game's net direction, i.e. the
    skew's per-game contribution for it is > 0 — AND (b) near its cap
    (``util >= util_threshold``). Widening a thin quote into a near-cap game only
    attracts hitters, so we decline rather than post it.

    Per-game (not aggregate): a candidate that CONCENTRATES a near-cap game
    declines even if it OFFSETS a different, un-stressed game — the near-cap game
    is the risk. A game the candidate only offsets is never a decline trigger
    (that flow balances the book). ``applied`` is True only when
    ``params.enabled`` — dark by default (shadow classifier)."""
    max_delta = limits.max_event_delta_contracts
    max_loss_cc = limits.max_event_worst_case_loss_dollars * 10_000.0
    max_notional_cc = limits.max_event_gross_notional_dollars * 10_000.0

    def game_util(game: str) -> float:
        u = 0.0
        net = snapshot.delta_by_game.get(game, 0.0)
        if max_delta > 0.0:
            u = max(u, min(1.0, abs(net) / max_delta))
        loss_cc = snapshot.worst_case_loss_by_game_cc.get(game, 0)
        if max_loss_cc > 0.0:
            u = max(u, min(1.0, loss_cc / max_loss_cc))
        notional_cc = snapshot.gross_settlement_notional_by_game_cc.get(game, 0)
        if max_notional_cc > 0.0:
            u = max(u, min(1.0, notional_cc / max_notional_cc))
        return u

    would_decline = False
    max_conc_util = 0.0  # tightest util among CONCENTRATING games (why it fired)
    for game, contribution in skew.per_game:
        if contribution <= 0:
            continue  # this game is offset/neutral — never a decline trigger
        util = game_util(game)
        max_conc_util = max(max_conc_util, util)
        if util >= params.util_threshold:
            would_decline = True

    reason = (
        f"near cap (util={max_conc_util:.2f}) on concentrating flow"
        if would_decline
        else "keep quoting"
    )
    return WidenDecision(
        would_decline=would_decline,
        applied=would_decline and params.enabled,
        max_util=max_conc_util,
        reason=reason,
    )
