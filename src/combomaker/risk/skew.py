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

    skew_cc(C) =  clamp( Σ_game concentration_term(game)   # ≥ 0, CONCENTRATING
                       − Σ_game offset_term(game),         # ≥ 0, OFFSETTING
                       [−skew_max_tighten_cc, +skew_max_widen_cc] )
                + clamp( peak_component(C),                # 2026-07-18 steer
                       [−peak_tighten_max_cc, +peak_widen_max_cc] )

(the PEAK-CONCENTRATION component — a candidate stacking on / provably missing
the cached committed-book worst scorelines, ``_peak_component`` — composes
ADDITIVELY under its own independent clamp, so the total is bounded by the sum
of the two cap pairs; see ``SkewParams``) and the pricer receives
``applied_cc = −skew_cc`` (when enabled). Because the
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

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CC_PER_DOLLAR
from combomaker.risk.exposure import (
    DirEntry,
    ExposureSnapshot,
    LegRef,
    MarginalProvider,
    OpenPosition,
    analytic_leg_deltas,
    mutex_directional_alignment_cc,
)

if TYPE_CHECKING:  # runtime import stays local (keep this module import-light)
    from combomaker.sim.peak_profile import PeakProfile


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
    # --- PEAK-CONCENTRATION pricing steer (operator directive 2026-07-18) ----
    # A SECOND, ADDITIVE classifier component fed by the cached committed-book
    # peak-state profile (sim/peak_profile.py). A candidate that HITS the
    # book's cached worst scoreline(s) — or, MULTI-CLUSTER (2026-07-19), ANY
    # cached loss cluster, scaled by that cluster's loss relative to the top —
    # widens by up to ``peak_widen_max_cc``
    # extra; one that provably MISSES the ENTIRE top-loss plateau (certified
    # against the full argmax level, 2026-07-18 verify fix) AND every cached
    # lower cluster (2026-07-19) rebates by up to
    # ``peak_tighten_max_cc`` extra. Each side is HARD-clamped independently of
    # the directional caps above, so the COMPOSED classifier is bounded by
    #   [-(skew_max_tighten_cc + peak_tighten_max_cc),
    #    +(skew_max_widen_cc  + peak_widen_max_cc)]
    # (defaults [-300cc, +1200cc] = [-3c, +12c]) — the documented overall
    # clamp. PRICING ONLY, never a refusal; any doubt (no profile, stale
    # generation, unparseable candidate) contributes exactly 0 (fail-safe
    # NEUTRAL — UNKNOWN can never widen at all, let alone de-facto block).
    peak_enabled: bool = True
    peak_widen_max_cc: int = 600
    peak_tighten_max_cc: int = 150
    # --- P(BOOK) STEER, Phase B1 (operator directive 2026-07-25: "Pbook
    # should be steering our betting, more variance = higher Pbook") --------
    # A THIRD additive classifier component fed by the cached P(book)
    # concentration profile (published off the book-risk snapshot). Fully
    # DERIVED from measured state — tail-share deviation from the uniform
    # 1/G book × the P(book) deficit (1 − p_book) × a caps-derived onset —
    # reusing the DOCUMENTED peak hard caps as its own component bounds (no
    # new tuning number). COMPOSITION (2026-07-25 review): when ARMED the
    # pbook component shares the documented two-pair overall clamp
    # [−(skew_max_tighten+peak_tighten), +(skew_max_widen+peak_widen)] —
    # the composed ``skew_cc`` is re-clamped to that bound, so arming NEVER
    # expands the documented range (a triple-stacked rebate cannot exceed
    # what the two-pair bound authorizes).
    # ``pbook_enabled`` computes + logs the component; ``pbook_armed`` is the
    # SEPARATE switch that adds it into ``skew_cc`` (default OFF = pure
    # shadow: pricing byte-identical while the live magnitude distribution is
    # measured — the derive-before-arm rule).
    pbook_enabled: bool = True
    pbook_armed: bool = False

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
        if self.peak_widen_max_cc < 0 or self.peak_tighten_max_cc < 0:
            raise ValueError(
                "peak skew caps must be >= 0 "
                f"(widen={self.peak_widen_max_cc}, tighten={self.peak_tighten_max_cc})"
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
class PBookProfile:
    """Cached committed-book P(book) concentration profile (2026-07-25).

    Published OFF the hot path from every accepted book-risk snapshot
    (``lifecycle._publish_book_risk``): the book's P(P&L > 0) under the
    production MC, and each game's SHARE of the tail loss
    (``per_game_tail_cc`` normalized — the additive CVaR decomposition, so
    shares sum to ~1 over games with tail presence). A perfectly diversified
    G-game book has share ≈ 1/G per game; the deviation IS the measured
    concentration, so the steer needs no target number. Generation-stamped:
    stale ⇒ the component is a hard ZERO (the peak-profile discipline)."""

    input_generation: int
    p_book: float
    tail_share_by_game: dict[str, float]
    # The book's POSITIVE tail mass (float cc, Σ positive per_game_tail_cc —
    # 2026-07-25 review: negative attribution entries excluded from BOTH the
    # numerator and denominator so shares sum to 1) — the SIZE of the hole.
    # The onset factor divides (share × total) by the ENFORCED budget so the
    # steer derives its "when" from the caps (operator 2026-07-25: never a
    # dollar constant, never too early): a $20 book is nearly free no matter
    # how "concentrated" its shares look.
    total_tail_cc: float = 0.0
    # The ENFORCED per-game loss budget in cc, published by the lifecycle
    # (the one owner that knows the live bankroll): min(hard $ game cap,
    # game_loss_frac × bankroll, hard_trip/KILL frac × bankroll) — the
    # tightest enforced loss bound a one-way game can burn (2026-07-25
    # review: the static SkewLimits dollar made the steer inert at 7/23
    # scale; this adapts with the bankroll and the derived-cap swap).
    game_budget_cc: float = 0.0
    # Games whose tail attribution is ZERO/NEGATIVE (hedged/protective —
    # they REDUCE the tail): excluded from shares, but flow there must not
    # collect the underweight rebate (it can erode the hedge) — the
    # component reads these as neutral "hedged_protected".
    protected_games: frozenset[str] = frozenset()


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
    # SKEW MUTEX FIX (2026-07-18): the games whose classification came from the
    # P0-9 mutex-aware branch-max fold (``mutex_directional_alignment_cc``)
    # instead of the raw delta-sum sign — the shadow-verification key (grep the
    # ``inventory_skew_shadow`` log for it to measure the ARG-champ flip live).
    mutex_direction_games: tuple[str, ...] = ()
    # PEAK-CONCENTRATION component (2026-07-18). ``peak_cc`` is the CLAMPED
    # signed component ALREADY INCLUDED in ``skew_cc`` (classifier convention:
    # >= 0 widens a peak-stacker, <= 0 rebates an anti-peak candidate), so the
    # composed ``skew_cc`` = clamped-directional + ``peak_cc`` and is bounded by
    # the SkewParams-documented overall clamp. ``peak_widen_cc`` /
    # ``peak_tighten_cc`` are the pre-clamp non-negative halves;
    # ``peak_per_game`` is the debug-level explanation: one
    # ``(game, adder_cc, factor, reason)`` row per candidate game — ``factor``
    # is the hit_severity on peak_hit/peak_partial_offset rows and the
    # peak_ratio on peak_miss_rebate rows (2026-07-19 magnitude recalibration:
    # the candidate-size factor is gone). Reasons:
    # peak_hit / peak_miss_rebate / peak_partial_offset (certified top-miss
    # with a reachable lower cluster — NET adder, 2026-07-19 asymmetry
    # hotfix) / no_peak_profile / peak_not_a_loss /
    # unknown / neutral, plus the global stale_profile / disabled sentinels.
    peak_cc: int = 0
    peak_widen_cc: int = 0
    peak_tighten_cc: int = 0
    peak_per_game: tuple[tuple[str, int, float, str], ...] = ()
    # P(BOOK) STEER component, Phase B1 (2026-07-25). ``pbook_cc`` is the
    # CLAMPED signed component (classifier convention: >= 0 widens a
    # same-way add on an overweight game, <= 0 rebates variance-adding /
    # offsetting flow). SHADOW-FIRST: included in ``skew_cc`` ONLY when
    # ``SkewParams.pbook_armed`` — until then it is computed + logged and
    # pricing is byte-identical. ``pbook_per_game`` mirrors the peak debug
    # rows: (game, adder_cc, factor, reason).
    pbook_cc: int = 0
    pbook_per_game: tuple[tuple[str, int, float, str], ...] = ()

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
    dir_entries_by_game: Mapping[str, Sequence[DirEntry]] | None = None,
    committed_dir_entries_by_game: Mapping[str, Sequence[DirEntry]] | None = None,
    is_me_event: Callable[[str], bool | None] | None = None,
    peak_profile: PeakProfile | None = None,
    peak_book_generation: int | None = None,
    pbook_profile: PBookProfile | None = None,
    pbook_book_generation: int | None = None,
) -> InventorySkew:
    """Compute the inventory skew for ``candidate`` against the current book.

    Pure. ``candidate`` is the hypothetical NO position a fill would create (the
    lifecycle builds exactly this for its limit check — reuse it). ``snapshot`` is
    the book's current per-game aggregates (already computed for the limits).
    ``marginals`` provides the candidate's own per-leg deltas via
    ``analytic_leg_deltas``. ``limits`` gives the headroom denominators;
    ``cache`` optionally overrides the per-game direction from the Phase-4 sim.

    SKEW MUTEX FIX (2026-07-18). ``dir_entries_by_game`` (the snapshot's
    exported P0-9 directional entries) + ``is_me_event`` (the exposure book's
    OWN metadata answer) arm the mutex-aware classifier: where the candidate's
    this-game legs carry exactly ONE explicit-ME event, the concentrating /
    offsetting split comes from how much the candidate RAISES the book's P0-9
    branch-max directional bound (``mutex_directional_alignment_cc`` — reused,
    never reimplemented). A long-NO candidate on outcome B of the event the
    book is short outcome A of NETS there (marginal 0 ⇒ full rebate) even
    though its raw delta sign MATCHES the book's — the mutex-blind raw read
    mis-widened exactly that hedge 63/63 on the live shadow tape. Anything the
    single-ME math cannot certify (no entries, 0 or >= 2 ME events among the
    candidate's legs, non-requires-all) FALLS BACK to the raw delta-sum read —
    never a larger rebate than the mutex math justifies. Omitting any new
    argument (every pre-existing caller) is byte-identical to the raw
    classifier.

    COMMITTED SECOND-ME-EVENT FALLBACK (2026-07-18 verify fix).
    ``committed_dir_entries_by_game`` (the snapshot's COMMITTED-positions-only
    entry subset) is REQUIRED for the mutex path to engage: a committed leg on
    a second explicit-ME event of the same game (champion event + regulation
    moneyline) is correlated mass the single-event fold rides as COMMON —
    inflating base and full equally, cancelling out of the marginal and
    OVER-REBATING a candidate that truly concentrates against it (see
    ``mutex_directional_alignment_cc``). Such games — and any caller that
    omits the committed census — fall back to the raw read (fail-closed).
    Resting-quote entries deliberately do NOT drive that fallback: the live
    200-slot book spans both ME events, and a quote-driven fallback would
    suppress the fix on exactly the shape it was built for.

    PEAK-CONCENTRATION STEER (operator directive 2026-07-18 evening).
    ``peak_profile`` (the cached committed-book peak-state profile —
    ``sim/peak_profile.build_peak_profile``, computed OFF the hot path on
    position-generation change) + ``peak_book_generation`` (the CURRENT
    ``ExposureBook.position_generation``) arm a SECOND additive classifier
    component: per candidate game, a structural containment check against the
    <= K cached peak scorelines (O(K x legs), no MC, no enumeration). Defining

        hit_severity = relative loss of the worst cached state/cluster the
                       candidate's parlay can still HIT (state loss / the
                       game's top loss, in [0, 1])
        peak_ratio   = min(1, game peak loss / game-loss budget)

    a HIT contributes  ``+ peak_widen_max_cc x hit_severity x peak_ratio**gamma``
    (convex in how close the book's peak already is to its budget: a small book
    is nearly free, a peak near budget pays the full widen). MAGNITUDE
    RECALIBRATION (operator directive 2026-07-19 evening): the old
    candidate-size factor ``min(1, candidate premium / budget)`` is GONE from
    both sides — a quote's per-contract price reflects WHERE its risk lands,
    never the clip size (size is the caps'/velocity brake's job; on the live
    tape the ~0.015 size factor of realistic clips multiplied against
    peak_ratio**2 and zeroed the whole steer — a $15 rung on a ~$300 cluster
    priced at ~0.01c). MULTI-CLUSTER
    (operator directive 2026-07-19): ``hit_severity`` is the max over ALL
    cached loss clusters of (cluster_loss / top_loss) x hit-indicator — the
    top plateau at weight 1.0 plus the cached lower clusters at their level
    weights (folded with the K-row severity; ``peak_n_clusters=1`` restores
    the K-sample-only read) — so stacking a SECOND loss
    cluster on a mutually exclusive branch (the live ESPARG ARG-champ+Messi
    ladder) now pays a widen scaled by that cluster's relative loss instead of
    riding free. A candidate
    that provably MISSES the ENTIRE top-loss plateau — certified against the
    FULL argmax level cached in the profile, never just the K sampled rows
    (2026-07-18 verify fix: on a plateau wider than K a plateau-stacking
    refinement missed all K rows and pocketed the rebate while raising the
    certified worst case) — contributes
    ``- peak_tighten_max_cc x peak_ratio x (1 - hit_severity)`` (2026-07-19
    cluster-asymmetry hotfix: the certified top-miss ALONE triggers the
    rebate — the old all-clusters trigger graded genuinely balancing flow
    neutral whenever it could reach any lower loss state; a lower-cluster hit
    now discounts the rebate via (1 - severity) AND pays its own widen, so
    both halves can fire on one candidate. Its premium provably pays into
    every argmax state, so we quote TIGHTER to win those auctions; an
    uncached/oversized plateau ⇒ no rebate, neutral).
    Summed over the candidate's games, then clamped to
    [-peak_tighten_max_cc, +peak_widen_max_cc] and ADDED to the independently
    clamped directional classifier, so the composed ``skew_cc`` obeys the
    overall clamp documented on :class:`SkewParams`. FAIL-SAFE: profile absent
    / generation-stale / candidate unparseable / any doubt => the component is
    EXACTLY 0 (neutral pricing — never a refusal, never a crash, and UNKNOWN
    can never produce a widen). The component NEVER feeds ``per_game`` (only
    ``peak_per_game``), so the widen-vs-decline policy can never decline on it
    — pricing only, by construction. Omitting both new arguments (every
    pre-existing caller) is byte-identical to the directional-only classifier.

    Returns an :class:`InventorySkew` whose ``applied_cc`` is 0 while
    ``params.enabled`` is False (dark ship) and the honest ``skew_cc`` otherwise.
    ``skew_cc`` itself is ALWAYS the honest number (for shadow logging).
    """
    _ = conventions  # candidate.our_side already carries the side; kept for parity
    cand_deltas = analytic_leg_deltas(candidate, marginals)

    # Per-game candidate delta (contracts-equivalent), aggregated the same way
    # the book does — Σ over the legs the candidate touches per game — plus the
    # per-game legs themselves (the mutex classifier's candidate entry).
    cand_by_game: dict[str, float] = {}
    cand_legs_by_game: dict[str, list[LegRef]] = {}
    if cand_deltas is not None:
        for leg in candidate.legs:
            if leg.event_ticker is None:
                continue
            game = _game_key(leg.event_ticker)
            cand_by_game[game] = cand_by_game.get(game, 0.0) + cand_deltas.get(
                leg.market_ticker, 0.0
            )
            cand_legs_by_game.setdefault(game, []).append(leg)

    max_delta = limits.max_event_delta_contracts
    max_loss_cc = limits.max_event_worst_case_loss_dollars * 10_000.0
    max_notional_cc = limits.max_event_gross_notional_dollars * 10_000.0

    concentration = 0.0
    offset = 0.0
    per_game: list[tuple[str, int]] = []
    mutex_games: list[str] = []

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

        # P0-9 mutex-aware direction (2026-07-18): applicable only where the
        # candidate's this-game legs carry exactly ONE explicit-ME event AND
        # the game's COMMITTED census certifies the single-event fold (no
        # committed leg on a second explicit-ME event — 2026-07-18 verify fix);
        # else None ⇒ the raw sign-alignment read below (fail-safe).
        mutex: tuple[float, float] | None = None
        if dir_entries_by_game is not None and is_me_event is not None:
            entries = dir_entries_by_game.get(game)
            if entries:
                committed_entries = (
                    committed_dir_entries_by_game.get(game, ())
                    if committed_dir_entries_by_game is not None
                    else None                   # census unavailable ⇒ raw read
                )
                mutex = mutex_directional_alignment_cc(
                    entries,
                    (
                        tuple(cand_legs_by_game.get(game, ())),
                        d_e * CC_PER_DOLLAR,
                        candidate.our_side is Side.NO,
                    ),
                    is_me_event,
                    committed_entries=committed_entries,
                )

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

        if mutex is not None:
            # Split the candidate's magnitude by the mutex math: only the part
            # that RAISES the book's branch-max bound concentrates (pays the
            # convex widen); the netted remainder rebates, bounded by the
            # book's own mutex-aware magnitude on the event (never rebate more
            # direction than the book actually holds there).
            marginal_cc, base_cc = mutex
            conc_mag = marginal_cc / CC_PER_DOLLAR
            off_mag = max(0.0, d_e - conc_mag)
            term = params.w_conc * conc_mag * (util**params.gamma)
            rebate = params.w_off * min(off_mag, base_cc / CC_PER_DOLLAR) * util
            concentration += term
            offset += rebate
            per_game.append((game, int(round(term)) - int(round(rebate))))
            mutex_games.append(game)
        elif aligns:
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
    # Directional clamp: [−skew_max_tighten_cc, +skew_max_widen_cc]. The tighten
    # side is the dangerous one and is doubly contained (here + the free-money
    # clamp in construct_quote).
    directional_cc = max(-params.skew_max_tighten_cc, min(params.skew_max_widen_cc, raw))

    # PEAK-CONCENTRATION component (2026-07-18): independently clamped to
    # [−peak_tighten_max_cc, +peak_widen_max_cc], then ADDED — so the composed
    # classifier obeys the overall clamp documented on SkewParams by
    # construction (each addend is bounded by its own cap).
    peak_widen_cc, peak_tighten_cc, peak_cc, peak_per_game = _peak_component(
        candidate, params, limits, peak_profile, peak_book_generation
    )
    # P(BOOK) STEER component, Phase B1 (2026-07-25): computed from the
    # per-game contributions the loop above already classified — SHADOW by
    # default (pbook_armed=False keeps skew_cc byte-identical while the live
    # magnitude distribution is measured).
    # Include candidate games the directional loop never appended (zero
    # analytic delta — 2026-07-25 review: a delta-neutral variance-adder on a
    # new game must still reach the diversification rebate).
    seen_games = {g for g, _c in per_game}
    pbook_input = list(per_game) + [
        (g, 0) for g in cand_by_game if g not in seen_games
    ]
    pbook_cc, pbook_per_game = _pbook_component(
        pbook_input, params, pbook_profile, pbook_book_generation
    )
    skew_cc = directional_cc + peak_cc
    if params.pbook_armed:
        skew_cc += pbook_cc
    # COMPOSED OVERALL CLAMP (2026-07-25 review): the documented SkewParams
    # bound [−(skew_max_tighten+peak_tighten), +(skew_max_widen+peak_widen)]
    # stays TRUE with the pbook component armed — pbook shares the documented
    # budget rather than expanding it (a triple-stacked rebate must never
    # exceed what the two-pair bound already authorizes). A no-op while
    # unarmed (directional + peak are each inside their own clamps).
    skew_cc = max(
        -(params.skew_max_tighten_cc + params.peak_tighten_max_cc),
        min(params.skew_max_widen_cc + params.peak_widen_max_cc, skew_cc),
    )

    return InventorySkew(
        skew_cc=skew_cc,
        concentration_cc=conc_cc,
        offset_cc=off_cc,
        per_game=tuple(per_game),
        enabled=params.enabled,
        mutex_direction_games=tuple(mutex_games),
        peak_cc=peak_cc,
        peak_widen_cc=peak_widen_cc,
        peak_tighten_cc=peak_tighten_cc,
        peak_per_game=peak_per_game,
        pbook_cc=pbook_cc,
        pbook_per_game=pbook_per_game,
    )


def _pbook_component(
    per_game: Sequence[tuple[str, int]],
    params: SkewParams,
    profile: PBookProfile | None,
    book_generation: int | None,
) -> tuple[int, tuple[tuple[str, int, float, str], ...]]:
    """The P(book) steering component (operator directive 2026-07-25: P(book)
    steers the betting; more variance/diversity = higher P(book); onset off
    the CAPS, never a dollar constant, never too early).

    Inputs are all MEASURED — no target number exists anywhere:
      deficit_g   = (tail_share_g − 1/G) / (1 − 1/G)   ∈ [−1, 1]
                    (0 on a perfectly diversified G-game book — deviation
                    from uniformity IS the concentration; G = games with
                    tail presence in the cached profile)
      need        = 1 − p_book                          ∈ [0, 1]
                    (a book already winning 90% of nights barely steers;
                    the 7/23 one-way 0.40 book steers hard)
      onset_g     = min(1, share_g × total_tail / profile.game_budget_cc)
                    — the CAPS-DERIVED "when": how close this game's
                    concentrated tail sits to the tightest ENFORCED loss
                    budget (published by the lifecycle from the live
                    bankroll: min(hard game $cap, game_loss_frac × bank,
                    KILL frac × bank) — 2026-07-25 review: one denominator,
                    one owner, adapts with the bankroll). A $20 book is
                    ~free however concentrated its shares look; the steer
                    bites as the hole approaches the cap (the operator's
                    "does it go off the caps" — yes).
      onset_book  = max over games of onset_g — the book-level hole depth
                    the diversification REBATE keys on (an underweight game
                    has ~no own tail, so its rebate must read the book).

    Per candidate game, using the directional loop's OWN classification
    (``per_game`` contribution sign — concentrating > 0, offsetting < 0),
    with the HOUSE ASYMMETRY (widen convex, rebate linear — the directional
    component's own util shape, so widens start LATE and rebates start
    gently but earlier):
      - same-way add on an OVERWEIGHT game (deficit > 0, contrib > 0) ⇒
        WIDEN  +peak_widen_max_cc × deficit × need × onset_g**gamma
      - offsetting an OVERWEIGHT game (deficit > 0, contrib < 0) ⇒
        REBATE −peak_tighten_max_cc × deficit × need × onset_g
      - ANY flow on an UNDERWEIGHT game (deficit < 0) — including the
        directional loop's neutral contrib on an EMPTY-book game (a
        brand-new game is the purest variance-adder: exactly the flow the
        7/23 book was missing) ⇒
        REBATE −peak_tighten_max_cc × |deficit| × need × onset_book
      - deficit == 0 / no budget / neutral ⇒ 0

    Outer bounds REUSE the documented peak hard caps (hard safety, not
    tuning — no new number); the free-money clamp in construct_quote remains
    the final bound when armed. FAIL-SAFE: profile absent / generation-stale
    / disabled / no budget ⇒ EXACTLY 0 (neutral; UNKNOWN can never widen).
    Pricing-only by construction: never feeds ``per_game``, so
    widen-vs-decline cannot decline on it."""
    if not params.pbook_enabled or profile is None:
        return 0, ()
    if book_generation is None or profile.input_generation != book_generation:
        return 0, (("*", 0, 0.0, "stale_profile"),)
    shares = profile.tail_share_by_game
    if not shares:
        return 0, (("*", 0, 0.0, "no_tail"),)
    budget_cc = profile.game_budget_cc
    if budget_cc <= 0.0 or profile.total_tail_cc <= 0.0:
        return 0, (("*", 0, 0.0, "no_budget"),)
    n = len(shares)
    uniform = 1.0 / n
    need = max(0.0, min(1.0, 1.0 - profile.p_book))
    onset_book = min(
        1.0, max(shares.values()) * profile.total_tail_cc / budget_cc
    )
    total = 0.0
    rows: list[tuple[str, int, float, str]] = []
    for game, contrib_cc in per_game:
        share = shares.get(game)
        if share is None and game in profile.protected_games:
            # Zero/negative tail attribution = a hedged/protective game
            # (2026-07-25 review): flow there must NOT collect the
            # underweight rebate — it can erode the hedge. Neutral.
            rows.append((game, 0, 0.0, "hedged_protected"))
            continue
        if share is None:
            # A game with NO tail presence at all: maximally underweight —
            # flow here is pure game-level diversification.
            share = 0.0
        if n > 1:
            deficit = (share - uniform) / (1.0 - uniform)
        else:
            # n == 1 (2026-07-25 review HIGH): the sole tail game is fully
            # overweight (+1); an ABSENT game is maximally underweight (−1)
            # — the continuous limit of the n>1 formula as the second share
            # → 0. The old 0.0 read zeroed the new-game rebate on exactly
            # the purest one-way (7/23) shape.
            deficit = 1.0 if share > 0.0 else -1.0
        deficit = max(-1.0, min(1.0, deficit))
        onset_g = min(1.0, share * profile.total_tail_cc / budget_cc)
        if deficit > 0.0 and contrib_cc > 0:
            factor = deficit * need * (onset_g**params.gamma)
            adder = int(round(params.peak_widen_max_cc * factor))
            rows.append((game, adder, factor, "pbook_concentrating"))
        elif deficit > 0.0 and contrib_cc < 0:
            factor = deficit * need * onset_g
            adder = -int(round(params.peak_tighten_max_cc * factor))
            rows.append((game, adder, factor, "pbook_offsetting"))
        elif deficit > 0.0:
            # Overweight game, delta-neutral contribution (2026-07-25
            # review): named so the shadow record can measure how often the
            # shape occurs before deciding whether it deserves a widen.
            adder = 0
            rows.append((game, 0, 0.0, "delta_neutral_on_overweight"))
        elif deficit < 0.0:
            factor = abs(deficit) * need * onset_book
            adder = -int(round(params.peak_tighten_max_cc * factor))
            rows.append((game, adder, factor, "pbook_diversifying"))
        else:
            adder = 0
            rows.append((game, 0, 0.0, "balanced"))
        total += adder
    clamped = int(
        max(-params.peak_tighten_max_cc, min(params.peak_widen_max_cc, total))
    )
    return clamped, tuple(rows)


def _peak_component(
    candidate: OpenPosition,
    params: SkewParams,
    limits: SkewLimits,
    profile: PeakProfile | None,
    book_generation: int | None,
) -> tuple[int, int, int, tuple[tuple[str, int, float, str], ...]]:
    """The peak-concentration classifier component (see compute_inventory_skew).

    Returns ``(widen_cc, tighten_cc, clamped_component_cc, per_game_debug)``
    where the component is clamped to [−peak_tighten_max_cc, +peak_widen_max_cc]
    and the debug rows are ``(game, adder_cc, factor, reason)`` — ``factor`` is
    the ``hit_severity`` on ``peak_hit`` rows and the ``peak_ratio`` on
    ``peak_miss_rebate`` rows (the one variable input of each side's price
    after the 2026-07-19 magnitude recalibration).

    Pure O((K + cached cluster states) x legs) arithmetic on the CACHED
    profile rows — the containment indicator
    (``sim.peak_profile.evaluate_peak_containment``) is <= one vectorised
    numpy op per structural leg per slice, over the K sample plus the cached
    cluster level sets (all clusters together bounded by the shared 4096-state
    cap; ``hit_severity`` is the max over ALL cached clusters of
    (cluster_loss/top_loss) x hit — see the multi-cluster notes there).
    FAIL-SAFE: every
    doubt branch returns a hard 0 (neutral), never raises, never refuses."""
    if not params.peak_enabled or profile is None:
        return 0, 0, 0, ()
    if book_generation is None or profile.input_generation != book_generation:
        # Stale (or unverifiable) profile: the committed book moved since the
        # peaks were cached — NEUTRAL until the off-hot-path rebuild lands.
        return 0, 0, 0, (("*", 0, 0.0, "stale_profile"),)
    if candidate.our_side is not Side.NO:
        # Sell-only seller: the hit-loss framing below is long-NO premium-at-
        # risk. Anything else is outside the certified semantics -> neutral.
        return 0, 0, 0, (("*", 0, 0.0, "non_no_candidate"),)
    budget_cc = limits.max_event_worst_case_loss_dollars * 10_000.0
    if budget_cc <= 0.0:
        return 0, 0, 0, (("*", 0, 0.0, "no_budget"),)
    # MAGNITUDE RECALIBRATION (operator directive 2026-07-19 evening): the old
    # candidate-size factor min(1, candidate.max_loss_cc / budget) is GONE — a
    # quote's per-contract price reflects WHERE its risk lands (severity x
    # book-peak ratio), never the clip size. Size is already governed exactly
    # by the caps / last-look / velocity brake; multiplying the ~0.015 size
    # factor of a realistic clip against peak_ratio**gamma zeroed the steer on
    # the live tape (a $15 rung on a ~$300 cluster priced at ~0.01c).

    legs_by_game: dict[str, list[LegRef]] = {}
    for leg in candidate.legs:
        if leg.event_ticker is None:
            continue
        legs_by_game.setdefault(_game_key(leg.event_ticker), []).append(leg)
    if not legs_by_game:
        return 0, 0, 0, ()

    # Local import: keeps this module import-light for every consumer that
    # never wires a profile (and avoids a hard risk->sim dependency at import).
    from combomaker.sim.peak_profile import evaluate_peak_containment

    widen = 0.0
    tighten = 0.0
    rows: list[tuple[str, int, float, str]] = []
    for game, legs in legs_by_game.items():
        gp = profile.by_game.get(game)
        if gp is None:
            rows.append((game, 0, 0.0, "no_peak_profile"))
            continue
        peak_ratio = min(1.0, max(0, gp.top_loss_cc) / budget_cc)
        if peak_ratio <= 0.0:
            # The committed book's worst state for this game is not even a
            # loss — nothing to protect, nothing to rebate (tiny-book branch).
            rows.append((game, 0, 0.0, "peak_not_a_loss"))
            continue
        containment = evaluate_peak_containment(profile, game, legs)
        if containment is None:
            rows.append((game, 0, 0.0, "unknown"))
            continue
        # CLUSTER-ASYMMETRY composition (2026-07-19 hotfix, operator
        # directive — the zero-rebate live tape): the widen prices WHAT the
        # candidate can still hit (severity x ratio**gamma); the rebate
        # prices the CERTIFIED top-cluster miss (its premium provably pays
        # into every argmax state — the invariant on PeakContainment),
        # discounted by the severity it can still reach:
        #     tighten_max x peak_ratio x (1 - hit_severity).
        # BOTH can fire on one candidate (miss-top-hit-lower-cluster flow):
        # the halves accumulate separately (peak_widen_cc / peak_tighten_cc
        # stay the honest non-negative decomposition) and the per-game row
        # carries the NET adder with reason "peak_partial_offset".
        severity = containment.hit_severity
        term = 0.0
        if severity > 0.0:
            term = params.peak_widen_max_cc * severity * (peak_ratio**params.gamma)
            widen += term
        rebate = 0.0
        if containment.provably_misses_top:
            rebate = params.peak_tighten_max_cc * peak_ratio * (1.0 - severity)
            tighten += rebate
        if severity > 0.0 and containment.provably_misses_top:
            rows.append(
                (
                    game,
                    int(round(term)) - int(round(rebate)),
                    round(severity, 6),
                    "peak_partial_offset",
                )
            )
        elif severity > 0.0:
            rows.append((game, int(round(term)), round(severity, 6), "peak_hit"))
        elif containment.provably_misses_top:
            rows.append(
                (game, -int(round(rebate)), round(peak_ratio, 6), "peak_miss_rebate")
            )
        else:
            # Hits only non-loss peak rows / no certificate: neither stacking
            # nor certified flattening.
            rows.append((game, 0, 0.0, "neutral"))
    widen_cc = int(round(widen))
    tighten_cc = int(round(tighten))
    component = max(
        -params.peak_tighten_max_cc,
        min(params.peak_widen_max_cc, widen_cc - tighten_cc),
    )
    return widen_cc, tighten_cc, component, tuple(rows)


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
