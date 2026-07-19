"""Per-game PEAK-STATE profile of the COMMITTED book + quote-time containment
(operator directive 2026-07-18 evening — the "peak-stacking premium").

PROBLEM. A sell-only parlay seller under one-way demand builds books where 2-3
correlated legs cluster on one scoreline (live 2026-07-18: FRA-win x27 combos,
Mbappe-1+ x18, BTTS-yes x9 — all hitting together on "FRA wins ~2-1, Mbappe
scores, both score"). Every existing directional/mutex read is per-LEG or
per-DELTA; nothing prices "this candidate lands ON the book's current worst
scoreline". This module supplies the missing input as a PURE PRICING signal:

  1. ``build_peak_profile`` — OFF the hot path (maintenance tick, on
     position-generation change only, mirroring the ``compute_book_risk``
     snapshot discipline): enumerate the COMMITTED book's per-game
     state-consistent loss surface with the SAME machinery the last-look waiver
     uses (``sim/state_worst_case`` — DC scoreline enumeration, full signed
     netting between committed positions, zero correlation table) and cache the
     top-K loss scorelines per game with their loss levels. Generation-stamped
     exactly like ``BookRiskSnapshot.input_generation``: a fill/settlement bumps
     the position generation and the stale profile goes NEUTRAL, never wrong.
  2. ``evaluate_peak_containment`` — ON the hot path but O(K x legs) simple
     arithmetic on cached rows: does THIS candidate's parlay still HIT in the
     cached peak scorelines? Structural settlement is the exact live indicator
     logic (``state_worst_case._selected_possible`` reused verbatim — hard rule
     8c), evaluated against <= K cached state rows. NO Monte Carlo, NO
     enumeration, NO I/O at quote time.

The consumer is the EXISTING skew seam (``risk/skew.py``): a candidate that
hits the peak WIDENS (scaled by the severity of the cached state/cluster it
lands on AND by how large the game's peak already is relative to the game-loss
budget — 2026-07-19 magnitude recalibration: never by the clip size, which the
caps/velocity brake already govern), a candidate that provably MISSES the
ENTIRE top-loss plateau — the
full argmax level, certified against ``plateau_slices``, never just the K
sample (2026-07-18 verify fix) — TIGHTENS (its premium pays into our loss
states — distribution-flattening flow). PRICING ONLY: this module never
declines, never caps, never raises to a caller.

MULTI-CLUSTER (operator directive 2026-07-19 — the live ESPARG shape). A book
can carry TWO (or more) loss clusters: the argmax plateau (cluster A) plus a
second correlated pile on a mutually exclusive branch (cluster B — live: the
ARG-champ+Messi ladder at ~60-80% of the top loss). Single-plateau pricing let
B-stackers ride free (near-zero peak charge, even a small rebate for provably
missing A). The profile therefore caches up to ``n_clusters`` DISTINCT loss
LEVELS per game (descending; a level qualifies at >= ``cluster_min_frac`` x
top loss), each as its FULL level set, all under the SHARED
``_PLATEAU_CACHE_MAX_STATES`` cap (overflow drops the LOWEST clusters first —
an uncached cluster is neutral, exactly today's behaviour). Hitting ANY cached
cluster widens, scaled by that cluster's loss relative to the top; the rebate
certifies a provable miss of the FULL TOP cluster (the argmax level — the
2026-07-18 certification, unchanged in strictness) and is DISCOUNTED by the
severity the candidate can still reach (2026-07-19 cluster-asymmetry hotfix:
hitting a lower cluster no longer voids the rebate, it scales it by
(1 - hit_severity) while the hit pays its own widen — genuinely balancing
flow, whose premium provably pays into every argmax state, now quotes
tighter). ``n_clusters == 1`` restores the single-plateau CLUSTER semantics
exactly (profile content + severity walk; the skew's 2026-07-19 magnitude
recalibration applies at every n).

SEMANTICS (inherited from ``state_worst_case``, committed-book subset):

  * Entities are the COMMITTED positions only — full signed netting (a real
    holding hedges), no open quotes, no reservations (the profile prices the
    book we actually HOLD; resting mass is the caps'/waiver's job, and folding
    quote hypotheticals in would let quote churn repaint the peak every tick).
  * Per game, a non-structural or cross-game leg resolves ADVERSARIALLY
    (assumed HIT — it can never block a parlay from hitting); a structural leg
    that provably MISSES in a state kills the parlay there.
  * FAIL-SAFE EVERYWHERE (the pricing-steer inversion of the waiver's
    fail-CLOSED): a game with no structural plan, an enumeration error, an
    unparseable candidate leg universe, a half-leg candidate against a
    non-halves profile — ALL yield "no answer" (game absent / ``None``), which
    the skew maps to a ZERO adder (neutral pricing). Never a refusal, never a
    crash, never a widen born from UNKNOWN.

Money is int centi-cents (hard rule 5); probability floats appear only inside
the reused enumeration machinery. Everything here is pure and deterministic
(ties in the top-K ranking break by enumeration index via a stable sort).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from fractions import Fraction

import numpy as np
from numpy.typing import NDArray

from combomaker.pricing.grouping import game_key
from combomaker.pricing.structural_api import (
    Advance,
    HalfBtts,
    HalfDraw,
    HalfGoalSpread,
    HalfResult,
    HalfTotalOver,
    LegSpec,
    ModelParams,
    States,
    Team,
    parse_leg,
    parse_match,
    resolve_pricing_alias,
)
from combomaker.pricing.structural_api import (
    states as _enum_states,
)
from combomaker.risk.exposure import LegRef, OpenPosition

# Reused seams from the waiver enumeration (hard rule 8c: settlement/parse/
# enumeration are the LIVE machinery, never re-derived). These are
# sim-package-internal imports of the exact functions the confirm-path waiver
# runs — keep in sync with sim/state_worst_case.py.
from combomaker.sim.state_worst_case import (
    WorstCaseEntity,
    _entity_loss_matrix,
    _leg_game,
    _selected_possible,
    _settle_specs,
    entity_from_position,
)
from combomaker.sim.structural_book import (
    StructuralConfigView,
    _match_format,
    build_game_plans,
)

_HALF_SPECS = (HalfResult, HalfDraw, HalfTotalOver, HalfBtts, HalfGoalSpread)

# REBATE-CERTIFICATION cap (2026-07-18 adversarial-verify fix): the anti-peak
# rebate certifies a miss of the FULL top-loss plateau (``PlateauSlice``); a
# plateau larger than this is NOT cached and the rebate simply never fires for
# that game (fail-safe NEUTRAL — never a rebate the quote path cannot afford
# to verify).
# MULTI-CLUSTER (2026-07-19): this is the SHARED budget across ALL cached
# clusters of a game together — the top plateau plus every cached lower level
# set. Overflow drops the LOWEST clusters first; an uncached cluster is
# neutral (no widen from it, no rebate certification against it).
# 2026-07-19 LIVE HOTFIX (zero-rebate tape diagnosis): 4096 covered the FT
# enumerations (1586 branch-doubled) but NOT the with-halves grids the live
# book actually runs on (ESPARG 47,593 states; FRAENG 95,186 — the book holds
# KXWC1H* legs): a coarse top region ties across thousands of
# (scoreline x half-split) cells, the plateau overflowed, and the overflow
# CASCADE disabled BOTH the rebate certification and every lower cluster
# (live tape: ``clusters: {}`` in all 5 snapshots, 0 rebates in 300 shadow
# quotes while the K-sample widen kept working at exactly
# 600 x severity x (top/budget)**2). 131_072 covers the branch-doubled halves
# grid with headroom; the >cap fail-safe (neutral) remains for degenerate
# surfaces. The certification walk only runs for candidates that already
# missed the argmax K rows and is vectorised — measured cost in the tests.
_PLATEAU_CACHE_MAX_STATES = 131_072


@dataclass(frozen=True, slots=True)
class PlateauSlice:
    """The FULL argmax plateau of one game's committed-book loss surface, for
    one shootout branch: EVERY state whose loss equals the game's top loss
    level (exact int equality — losses are exact centi-cent sums), not a K
    sample of it.

    2026-07-18 adversarial-verify fix (SERIOUS). The K cached ``slices`` rows
    are a SAMPLE; on a one-way Advance book every same-outcome state (~793 of
    the 1586 branch-expanded states) carries the IDENTICAL loss, argsort ties
    break by enumeration index, and all K rows land on the lowest-scoreline
    corner of the plateau. A plateau-STACKING refinement (NO {ARG-adv & over
    5.5}, NO {ARG-adv & BTTS}) then provably missed all K cached rows and
    collected the anti-peak rebate while RAISING the certified worst case by
    its full premium — on exactly the cluster flow this feature surcharges.
    The rebate therefore certifies a miss of the ENTIRE top-loss level: these
    slices hold the whole plateau (bounded by ``_PLATEAU_CACHE_MAX_STATES``;
    larger ⇒ ``plateau_slices`` is None ⇒ no rebate). The WIDEN side keeps
    reading the K sample unchanged — a hit on ANY plateau state is a hit on a
    top-loss state, and every plateau state has the same severity 1.0, so the
    K sample loses no widen information."""

    branch: Team | None
    states: States


@dataclass(frozen=True, slots=True)
class LossCluster:
    """One cached loss cluster BELOW the top plateau (2026-07-19 multi-cluster
    steer): the FULL level set of one distinct loss level — every enumerated
    state whose committed-book loss equals ``loss_cc`` exactly (int centi-cent
    arithmetic makes level sets exact ties), grouped by shootout branch with
    the same representation as the top plateau. A cluster qualifies at
    ``loss_cc >= cluster_min_frac x top_loss`` (so always > 0) and is cached
    under the SHARED ``_PLATEAU_CACHE_MAX_STATES`` budget; overflow drops the
    lowest clusters first (an uncached cluster is neutral — no widen from it,
    exactly the pre-multi-cluster behaviour)."""

    loss_cc: int
    slices: tuple[PlateauSlice, ...]


@dataclass(frozen=True, slots=True)
class PeakStateSlice:
    """Up to K cached peak (largest-loss) states of ONE game, for ONE shootout
    branch. ``states`` is a row-slice of the game's full enumeration (each row
    one terminal scoreline state); ``losses_cc`` is the committed book's SIGNED
    per-state loss for each row, int centi-cents, descending within the game's
    overall top-K (not necessarily within one branch slice)."""

    branch: Team | None
    states: States
    losses_cc: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class GamePeakProfile:
    """One game's cached peak-state view of the COMMITTED book.

    ``params`` is the game's inverted ``ModelParams`` AFTER any with-halves
    upgrade (so the cached state rows and any half-leg indicator agree);
    ``top_loss_cc`` is the game's worst-state loss (== max over every slice's
    ``losses_cc``; may be <= 0 for a book whose worst state still profits —
    the skew's peak ratio then zeroes the whole component).

    ``plateau_slices`` (2026-07-18 verify fix) is the FULL argmax plateau —
    every state at exactly ``top_loss_cc`` — grouped by branch; None when the
    plateau exceeds ``_PLATEAU_CACHE_MAX_STATES`` (rebate then never fires:
    fail-safe neutral). At ``n_clusters == 1`` it is consumed ONLY by the
    rebate certification in ``evaluate_peak_containment`` (the widen path
    reads ``slices`` alone — the pre-multi-cluster behaviour, byte-identical);
    at ``n_clusters >= 2`` it is ALSO cluster 1 of the severity walk.

    ``lower_clusters`` (2026-07-19 multi-cluster steer) are the cached loss
    clusters BELOW the top plateau, descending by loss level — full level sets
    under the shared state cap, empty at ``n_clusters == 1`` or when nothing
    qualifies/fits. ``n_clusters`` records the build knob and gates the
    multi-cluster severity semantics in ``evaluate_peak_containment``."""

    game: str
    params: ModelParams
    slices: tuple[PeakStateSlice, ...]
    top_loss_cc: int
    n_states_enumerated: int
    plateau_slices: tuple[PlateauSlice, ...] | None = None
    lower_clusters: tuple[LossCluster, ...] = ()
    n_clusters: int = 1

    @property
    def n_peak_states(self) -> int:
        return sum(len(s.losses_cc) for s in self.slices)

    @property
    def n_plateau_states(self) -> int:
        """Size of the cached full top-loss plateau (0 when uncached)."""
        if self.plateau_slices is None:
            return 0
        return sum(int(s.states.w.size) for s in self.plateau_slices)

    @property
    def n_lower_cluster_states(self) -> int:
        """Total cached states across the lower clusters (0 at n_clusters=1)."""
        return sum(
            int(s.states.w.size) for c in self.lower_clusters for s in c.slices
        )


@dataclass(frozen=True, slots=True)
class PeakProfile:
    """The whole-book peak-state cache, generation-stamped.

    ``input_generation`` is the ``ExposureBook.position_generation`` the
    committed positions were read at (mirrors
    ``BookRiskSnapshot.input_generation``): the consumer applies the profile
    ONLY while the live position generation still equals this value — a stale
    profile is NEUTRAL (zero adder), never re-read as truth. Defaults to -1
    (un-stamped), which never equals a real generation (>= 0), so an un-stamped
    profile fails a generation match closed — the neutral direction.

    ``by_game`` holds ONLY the games that certified a structural plan and
    enumerated cleanly; every other game is simply absent (fail-safe: absent =
    neutral). ``knockout_series`` is carried from the build-time
    ``StructuralConfigView`` so quote-time candidate-leg parsing uses the SAME
    format rule the cached states were enumerated under."""

    input_generation: int = -1
    by_game: Mapping[str, GamePeakProfile] = field(default_factory=dict)
    knockout_series: tuple[str, ...] = ()
    k: int = 0


@dataclass(frozen=True, slots=True)
class PeakContainment:
    """Quote-time verdict for one candidate x one game's cached peaks.

    ``hit_severity`` in [0, 1]: the severity weight (state loss / the game's
    worst-state loss, losses clamped at >= 0) of the WORST cached state the
    candidate's parlay can still HIT — 1.0 means it stacks squarely on the
    book's worst scoreline, 0.0 means it hits no loss-carrying peak state. At
    ``n_clusters >= 2`` (2026-07-19 multi-cluster steer) the hit test ALSO
    runs over every cached cluster's FULL level set:
    ``hit_severity = max over cached clusters of (cluster_loss / top_loss) x
    hit(cluster)`` folded with the K-row severity — so stacking a SECOND loss
    cluster (mutually exclusive from the argmax plateau, hence invisible to
    the K sample) is priced at that cluster's relative loss.
    ``provably_misses_top`` (2026-07-19 cluster-asymmetry rebate, replacing
    the all-clusters ``provably_misses_all``): True iff a structural leg of
    the candidate provably MISSES in EVERY state of the FULL top-loss plateau
    (``GamePeakProfile.plateau_slices`` — the entire argmax level, never the K
    sample: the 2026-07-18 verify-fix certification is UNCHANGED in
    strictness). THE INVARIANT: ``provably_misses_top`` implies the
    candidate's parlay adds ZERO loss in every argmax state — its premium
    arrives in exactly the states where the book bleeds worst. The skew turns
    this into the balancing rebate, DISCOUNTED by ``hit_severity`` (the worst
    cached loss the candidate can still reach): hitting a lower cluster no
    longer voids the rebate (the old all-clusters rule graded the live
    ESP-side balancing flow neutral), it scales it by (1 - hit_severity)
    while the cluster hit pays its own widen. An uncached plateau (beyond the
    shared state cap) makes it False (neutral, never a rebate the quote path
    cannot verify)."""

    hit_severity: float
    provably_misses_top: bool
    n_states: int


# ----------------------------- build (off hot path) --------------------------


def _slice_states(st: States, rows: NDArray[np.intp]) -> States:
    """Row-slice every parallel array of a ``States`` enumeration (the cached
    peak rows keep the exact field layout ``_selected_possible`` reads)."""
    return States(
        w=st.w[rows],
        a90=st.a90[rows],
        b90=st.b90[rows],
        a_et=st.a_et[rows],
        b_et=st.b_et[rows],
        a_1h=st.a_1h[rows],
        b_1h=st.b_1h[rows],
    )


def _branch_slices(
    st: States, idx: NDArray[np.intp], branches: tuple[Team | None, ...], n: int
) -> tuple[PlateauSlice, ...]:
    """Group flat branch-major state indices into per-branch ``PlateauSlice``
    rows (the exact representation both the plateau and the lower clusters
    cache — ``_selected_possible``-ready)."""
    out: list[PlateauSlice] = []
    for bi, branch in enumerate(branches):
        rows = idx[idx // n == bi] % n
        if rows.size == 0:
            continue
        out.append(PlateauSlice(branch=branch, states=_slice_states(st, rows)))
    return tuple(out)


def _game_peak(
    game: str,
    plan_specs: dict[str, LegSpec],
    params: ModelParams,
    entities: Sequence[WorstCaseEntity],
    events: Mapping[str, str | None] | None,
    cfg: StructuralConfigView,
    k: int,
    n_clusters: int,
    cluster_min_frac: Fraction,
) -> GamePeakProfile | None:
    """Top-K loss states of one certified game's COMMITTED book (full signed
    netting — mirrors ``state_worst_case._certified_worst_case`` minus quotes
    and minus the reservation clamp, which do not exist here)."""
    game_legs: list[LegRef] = []
    for e in entities:
        game_legs.extend(leg for leg in e.legs if _leg_game(leg, events) == game)
    settle = _settle_specs(game, plan_specs, game_legs, cfg)
    if any(isinstance(s, _HALF_SPECS) for s in settle.values()) and not params.with_halves:
        params = replace(params, with_halves=True)
    st = _enum_states(params)
    n = int(st.w.size)
    branches: tuple[Team | None, ...] = (
        (Team.A, Team.B)
        if any(isinstance(s, Advance) for s in settle.values())
        else (None,)
    )
    total = np.zeros((len(branches), n), dtype=np.int64)
    for entity in entities:
        total += _entity_loss_matrix(entity, game, settle, st, params, branches, events)
    flat = total.ravel()
    kk = min(max(0, k), int(flat.size))
    if kk == 0:
        return None
    # Deterministic top-K: stable sort of the negated losses breaks ties by
    # enumeration index (branch-major), so the same book always caches the
    # same rows. Off the hot path — a full argsort of the (<= tens of
    # thousands) state vector is microseconds-to-milliseconds.
    order = np.argsort(-flat, kind="stable")[:kk]
    slices: list[PeakStateSlice] = []
    for bi, branch in enumerate(branches):
        rows = order[order // n == bi] % n
        if rows.size == 0:
            continue
        slices.append(
            PeakStateSlice(
                branch=branch,
                states=_slice_states(st, rows),
                losses_cc=tuple(int(v) for v in total[bi, rows]),
            )
        )
    top_loss = int(flat[order[0]])
    # REBATE CERTIFICATION (2026-07-18 verify fix): cache the FULL argmax
    # plateau — every state whose loss equals the top level exactly (int
    # arithmetic ⇒ plateaus are exact ties, e.g. all ~793 same-outcome states
    # of a one-way Advance book). The rebate must prove a miss of the WHOLE
    # level, never of a K sample of it. A plateau too large to verify cheaply
    # at quote time is not cached (None ⇒ the rebate never fires — neutral).
    plateau_idx = np.nonzero(flat == top_loss)[0]
    plateau_slices: tuple[PlateauSlice, ...] | None
    if int(plateau_idx.size) > _PLATEAU_CACHE_MAX_STATES:
        plateau_slices = None
    else:
        plateau_slices = _branch_slices(st, plateau_idx, branches, n)
    # MULTI-CLUSTER identification (2026-07-19): up to ``n_clusters - 1``
    # DISTINCT loss LEVELS below the top plateau, descending, each qualifying
    # at level >= cluster_min_frac x top_loss (exact int x Fraction compare —
    # levels are exact centi-cent sums, so a "cluster" is an exact level set).
    # All clusters share the ONE ``_PLATEAU_CACHE_MAX_STATES`` budget with the
    # plateau; a cluster that does not fit is dropped WITH everything below it
    # (drop-lowest-first — an uncached cluster is neutral: no widen from it,
    # no rebate certification against it, exactly the single-plateau ship).
    # Guards: an uncached plateau caches no lower clusters either (severity
    # weights are anchored on the top level), and a top loss <= 0 has nothing
    # to cluster (the skew's peak_ratio zeroes the whole component anyway).
    lower_clusters: tuple[LossCluster, ...] = ()
    if plateau_slices is not None and n_clusters >= 2 and top_loss > 0:
        remaining = _PLATEAU_CACHE_MAX_STATES - int(plateau_idx.size)
        num = cluster_min_frac.numerator
        den = cluster_min_frac.denominator
        picked: list[LossCluster] = []
        for level_v in np.unique(flat)[::-1]:
            level = int(level_v)
            if level >= top_loss:
                continue  # the top plateau itself — cluster 1, cached above
            if level * den < num * top_loss:
                break  # descending levels: nothing below meets the threshold
            if len(picked) >= n_clusters - 1:
                break  # cluster budget spent (the top plateau is cluster 1)
            idx = np.nonzero(flat == level)[0]
            if int(idx.size) > remaining:
                break  # shared state cap: drop this and every LOWER cluster
            remaining -= int(idx.size)
            picked.append(
                LossCluster(loss_cc=level, slices=_branch_slices(st, idx, branches, n))
            )
        lower_clusters = tuple(picked)
    return GamePeakProfile(
        game=game,
        params=params,
        slices=tuple(slices),
        top_loss_cc=top_loss,
        n_states_enumerated=n * len(branches),
        plateau_slices=plateau_slices,
        lower_clusters=lower_clusters,
        n_clusters=n_clusters,
    )


def build_peak_profile(
    positions: Sequence[OpenPosition],
    marginals: Mapping[str, float],
    events: Mapping[str, str | None] | None,
    structural_cfg: StructuralConfigView,
    *,
    k: int = 5,
    n_clusters: int = 3,
    cluster_min_frac: Fraction = Fraction(30, 100),
    input_generation: int = -1,
) -> PeakProfile:
    """Build the per-game peak-state cache of the COMMITTED book.

    ``positions`` are the committed positions ONLY (the caller reads
    ``ExposureBook.positions`` at ``input_generation`` — the same
    read-generation-stamp-publish discipline as ``_build_book_risk_inputs``).
    ``marginals`` maps market_ticker -> P(YES) for the plan INVERSION only
    (settlement is marginal-free — a missing entry just drops that leg from
    the inversion, exactly as in ``state_worst_case_by_game``).

    ``n_clusters`` / ``cluster_min_frac`` (2026-07-19 multi-cluster steer):
    cache up to ``n_clusters`` distinct loss clusters per game — the top
    plateau plus the descending lower loss levels at >= ``cluster_min_frac`` x
    top loss, full level sets under the shared state cap (module docstring).
    ``n_clusters=1`` reproduces the 2026-07-18 single-plateau profile
    byte-identically.

    FAIL-SAFE: a game with no buildable structural plan, or whose enumeration
    raises, is simply ABSENT from the profile (the skew's neutral zero-adder
    branch) — never a guess, never an exception to the caller. An empty book
    returns an empty profile (every game neutral)."""
    entities = [entity_from_position(p) for p in positions]

    # Inversion universe: committed legs only, unique tickers, first-seen event.
    uni_tickers: list[str] = []
    uni_events: list[str | None] = []
    uni_marginals: list[float | None] = []
    seen: set[str] = set()
    for entity in entities:
        for leg in entity.legs:
            market = leg.market_ticker
            if market in seen:
                continue
            seen.add(market)
            uni_tickers.append(market)
            uni_events.append(
                leg.event_ticker
                if leg.event_ticker
                else (events.get(market) if events is not None else None)
            )
            uni_marginals.append(marginals.get(market))

    by_game: dict[str, GamePeakProfile] = {}
    try:
        plans, _copula = build_game_plans(
            uni_tickers, uni_events, uni_marginals, structural_cfg
        )
    except Exception:
        plans = []  # fail-safe: whole book neutral rather than a crash
    plan_specs_by_game: dict[str, dict[str, LegSpec]] = {}
    params_by_game: dict[str, ModelParams] = {}
    for plan in plans:
        ev = uni_events[plan.global_indices[0]]
        if ev is None:  # pragma: no cover - build_game_plans only groups gamed legs
            continue
        g = game_key(ev)
        plan_specs_by_game[g] = {
            uni_tickers[j]: spec
            for j, spec in zip(plan.global_indices, plan.specs, strict=True)
        }
        params_by_game[g] = plan.params

    entity_games: list[tuple[WorstCaseEntity, frozenset[str]]] = [
        (
            e,
            frozenset(
                gk for leg in e.legs if (gk := _leg_game(leg, events)) is not None
            ),
        )
        for e in entities
    ]
    touched: set[str] = set()
    for _e, egs in entity_games:
        touched |= egs

    for game in sorted(touched):
        plan_specs = plan_specs_by_game.get(game)
        if plan_specs is None:
            continue  # no structural plan -> absent -> neutral (fail-safe)
        game_entities = [e for e, egs in entity_games if game in egs]
        if not game_entities:
            continue
        try:
            gp = _game_peak(
                game,
                plan_specs,
                params_by_game[game],
                game_entities,
                events,
                structural_cfg,
                k,
                n_clusters,
                cluster_min_frac,
            )
        except Exception:
            continue  # enumeration error -> absent -> neutral (never a guess)
        if gp is not None:
            by_game[game] = gp

    return PeakProfile(
        input_generation=input_generation,
        by_game=by_game,
        knockout_series=tuple(structural_cfg.knockout_series),
        k=k,
    )


# ----------------------------- evaluate (hot path) ---------------------------


def _parse_candidate_leg(
    market: str, game: str, knockout_series: tuple[str, ...]
) -> LegSpec | None:
    """Parse ONE candidate leg into a scoreline-settleable spec, or None when
    the leg is non-structural for this game (corners/cards/foreign blob) — the
    ADVERSARIAL (assumed-hit) bucket, exactly the ``_settle_specs`` rule."""
    resolved = resolve_pricing_alias(market)
    parts = resolved.split("-")
    if len(parts) < 2 or parts[1] != game:
        return None
    match = parse_match(parts[1])
    if match is None:
        return None
    spec = parse_leg(market, match, fmt=_match_format(resolved, knockout_series))
    return None if isinstance(spec, str) else spec


def _hits_any(
    slices: Sequence[PlateauSlice],
    struct: Sequence[tuple[str, LegSpec]],
    params: ModelParams,
) -> bool:
    """Can the candidate's structural parlay still HIT in ANY state of the
    given cached slices? One vectorised ``_selected_possible`` per structural
    leg per branch slice (the exact waiver settlement logic); non-structural
    legs are adversarial (assumed hit) and already absent from ``struct``."""
    for sl in slices:
        hit = np.ones(int(sl.states.w.size), dtype=np.bool_)
        for side, spec in struct:
            hit &= _selected_possible(spec, side, sl.states, params, sl.branch)
            if not bool(hit.any()):
                break
        if bool(hit.any()):
            return True
    return False


def evaluate_peak_containment(
    profile: PeakProfile,
    game: str,
    legs: Sequence[LegRef],
    events: Mapping[str, str | None] | None = None,
) -> PeakContainment | None:
    """Does this candidate's parlay HIT in the cached peak states of ``game``?

    O((K + cached cluster states) x legs) simple arithmetic on cached rows:
    per structural leg ONE vectorised indicator per slice
    (``_selected_possible`` — the exact waiver settlement logic) over the <= K
    sampled rows and, at ``n_clusters >= 2``, the cached cluster level sets
    (all clusters together bounded by the shared 4096-state cap, walked with
    early exits). Non-structural legs of the game (and every other-game
    leg, which the caller never passes) are ADVERSARIAL: assumed hit, so they
    never block a hit and never certify a miss.

    Returns ``None`` on ANY doubt — game absent from the profile, an unknown
    leg side, no structural leg at all (nothing to evaluate), or any indicator
    error (e.g. a half-leg candidate against a non-halves profile, whose
    ``_NO_HALF`` sentinel raises by design). ``None`` means NEUTRAL at the
    skew: a zero adder, never a refusal (module docstring fail-safe)."""
    gp = profile.by_game.get(game)
    if gp is None:
        return None
    try:
        struct: list[tuple[str, LegSpec]] = []
        for leg in legs:
            if _leg_game(leg, events) != game:
                continue
            if leg.side not in ("yes", "no"):
                return None  # unknown selection side -> doubt -> neutral
            spec = _parse_candidate_leg(
                leg.market_ticker, game, profile.knockout_series
            )
            if spec is None:
                continue  # non-structural -> adversarial (assumed hit)
            struct.append((leg.side, spec))
        if not struct:
            return None  # nothing structurally evaluable -> doubt -> neutral

        losses: list[int] = []
        hits: list[bool] = []
        for sl in gp.slices:
            hit = np.ones(int(sl.states.w.size), dtype=np.bool_)
            for side, spec in struct:
                hit &= _selected_possible(spec, side, sl.states, gp.params, sl.branch)
            losses.extend(sl.losses_cc)
            hits.extend(bool(h) for h in hit)

        worst = max((max(0, loss) for loss in losses), default=0)
        severity = 0.0
        if worst > 0:
            for loss, h in zip(losses, hits, strict=True):
                if h and loss > 0:
                    severity = max(severity, loss / worst)

        # MULTI-CLUSTER severity (operator directive 2026-07-19): at
        # ``n_clusters >= 2`` the hit indicator ALSO runs over every cached
        # cluster's FULL level set —
        #     hit_severity = max over cached clusters of
        #                    (cluster_loss / top_loss) x hit(cluster)
        # folded into the K-row severity above, so a candidate stacking a
        # SECOND loss cluster (mutually exclusive from the argmax plateau,
        # hence invisible to the K sample — the live ESPARG ARG-champ+Messi
        # ladder) widens, scaled by that cluster's loss relative to the top.
        # Cluster 1 is the full top plateau (weight exactly 1.0 — worst ==
        # top_loss whenever positive, K row 0 is the argmax); lower clusters
        # walk in DESCENDING loss order with an early exit once no remaining
        # weight can raise the max, so a candidate that already hit an argmax
        # K row (the common stacker) pays zero extra cost here.
        # ``n_clusters == 1`` skips this block entirely — the single-plateau
        # legacy path, byte-identical.
        # ONE shared full-plateau walk (2026-07-19 hotfix restructure) feeds
        # BOTH the multi-cluster severity (a plateau hit is severity 1.0 at
        # n_clusters >= 2) and the TOP-MISS certification below. Skipped
        # entirely when an argmax K row already hit (severity == 1.0 — the
        # common stacker pays zero extra cost); ``None`` = uncertifiable
        # (plateau beyond the shared cap — fail-safe: no rebate, and at
        # n_clusters == 1 severity stays the K-sample-only legacy read).
        plateau_hit: bool | None = None
        if gp.plateau_slices is not None:
            if severity >= 1.0:
                plateau_hit = True
            else:
                plateau_hit = _hits_any(gp.plateau_slices, struct, gp.params)
        if plateau_hit is True and gp.n_clusters >= 2 and worst > 0:
            severity = 1.0
        if gp.n_clusters >= 2 and worst > 0:
            for cluster in gp.lower_clusters:
                if cluster.loss_cc <= 0:
                    break  # descending: nothing below carries a loss
                weight = cluster.loss_cc / worst
                if weight <= severity:
                    break  # descending: no lower cluster can raise the max
                if _hits_any(cluster.slices, struct, gp.params):
                    severity = weight

        # TOP-MISS CERTIFICATION (2026-07-19 cluster-asymmetry rebate;
        # operator directive — the zero-rebate live tape). The certification
        # itself is UNCHANGED in strictness since the 2026-07-18 verify fix:
        # a provable miss of the ENTIRE argmax level (``plateau_slices``,
        # never the K sample; uncached ⇒ False ⇒ neutral). What changed is
        # the TRIGGER: the old rule additionally demanded a provable miss of
        # every cached row AND every lower cluster, which graded genuinely
        # balancing flow (pays exactly when the top cluster pays) as neutral
        # whenever it could reach ANY lower loss state. Now the top-miss
        # certificate alone earns the rebate and ``hit_severity`` DISCOUNTS
        # it in the skew (x (1 - severity)) while any lower-cluster hit keeps
        # paying its own widen. INVARIANT (property-tested): a rebated
        # candidate provably adds ZERO loss in every top-cluster (argmax)
        # state.
        return PeakContainment(
            hit_severity=severity,
            provably_misses_top=plateau_hit is False,
            n_states=len(losses),
        )
    except Exception:
        return None  # fail-safe: any evaluation doubt -> neutral, never a crash
