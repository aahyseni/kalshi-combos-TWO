"""A1 — STRUCTURAL portfolio-risk sampling.

Sample ONE game outcome (scoreline + extra-time + first-half split + a shared
shootout coin + a shared per-team goal allocation) from the SAME Dixon-Coles state
enumeration that PRICES the game's legs, then settle EVERY leg against it. The
result is a ``(n, n_legs)`` YES-settlement value matrix that ``sim/engine.book_pnl``
consumes unchanged (the clean seam: everything downstream operates on a value
matrix and never asks how it was produced).

Because every leg of a game reads the ONE sampled state, all same-game hedges and
exclusions are EXACT with no correlation table: advance(A) ⊥ advance(B), BTTS
yes ⊥ no, over/under, goalscorer × total, 1H × FT. This is the model the operator
asked for — "track ALL legs, hedge via any leg" — realized as a joint sample.

Two fractional components are settled with SHARED coins so the correlation the
pricer marginalizes analytically is reproduced (and, for the shootout, made MORE
exact than the analytic — see below):

  * SHOOTOUT: one uniform per game decides advance(A) vs advance(B) on a
    level-after-ET state. advance(A) settles YES iff ``u < pens_win_a``; advance(B)
    iff ``u >= pens_win_a`` — so on any state exactly one advances. (The analytic
    ``joint_probability`` multiplies the two pens factors INDEPENDENTLY, which would
    give a spurious P(both advance) > 0; no valid combo ever holds both advance
    legs, so this never affects pricing, but the shared coin makes the cross-combo
    advance HEDGE exact in the portfolio MC.)
  * PLAYER GOALS: a shared multinomial allocation of the team's sampled goals to its
    scorers (sequential conditional binomials), so same-team scorers correlate the
    way ``_player_group_factor`` models them.

Parity gate (``tests/test_structural_book_mc.py``): the MC-estimated joint
P(all legs YES) equals ``dixon_coles.joint_probability`` to within a few standard
errors for every representable leg type. Legs Dixon-Coles cannot settle from a
scoreline (corners, cards, other sports) are settled on the copula path.

P0-7 PREFERRED — where a copula fallback leg shares a game with a structural leg AND
a DEFENSIBLE measured scoreline-state link exists (currently the knockout total-
corners / extra-time channel), that leg's copula latent is CONDITIONED on the game's
sampled scoreline intensity via a conservative shared factor (``sample_structural_
values(..., conditioning=...)``), so same-game structural↔copula dependence enters
the PRODUCTION sample (not only a challenger). A leg with no defensible link keeps
INDEPENDENCE and is covered by the worse-tail full-copula challenger in
``sim/book_risk.py`` (which also folds an unconditioned-split guard into the
governing tail so conditioning may only fatten it, never thin it).

Money/probability: pure probability space here (floats OK, hard rule 5); this
module builds only the inputs to the P&L engine and runs no P&L itself.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

import numpy as np
from numpy.typing import NDArray

from combomaker.pricing.grouping import game_key

# P1.5: the risk MC reconstructs the pricer's structural model through the PUBLIC
# parse/invert/sample/settle contract (``pricing.structural_api``), never the
# private internals of ``pricing.structural`` / ``pricing.dixon_coles``. The API
# names are the same objects as those internals, so parity is byte-identical
# (``test_structural_api`` pins the identity).
from combomaker.pricing.structural_api import (
    Advance,
    HalfBtts,
    HalfDraw,
    HalfGoalSpread,
    HalfResult,
    HalfTotalOver,
    LegSpec,
    MatchFormat,
    ModelParams,
    PlayerScores,
    StructuralError,
    Team,
    invert,
    resolve_pricing_alias,
)
from combomaker.pricing.structural_api import (
    States as _States,
)
from combomaker.pricing.structural_api import (
    half_indicator as _half_indicator,
)
from combomaker.pricing.structural_api import (
    parse_leg as _parse_leg,
)
from combomaker.pricing.structural_api import (
    parse_match as _parse_match,
)
from combomaker.pricing.structural_api import (
    states as _states,
)
from combomaker.pricing.structural_api import (
    team_goals as _team_goals,
)
from combomaker.pricing.structural_api import (
    team_indicator as _team_indicator,
)
from combomaker.sim.engine import LegModel, sample_leg_values

_FloatMatrix = NDArray[np.float64]
_HALF_SPECS = (HalfResult, HalfDraw, HalfTotalOver, HalfBtts, HalfGoalSpread)

# P0-7 PREFERRED: a per-copula-leg CONSERVATIVE loading onto its game's shared
# structural factor. Signature ``(copula_ticker) -> float`` in [-1, 1]. The
# DEFAULT is 0 for every leg (``_NO_LOADING`` below), so conditioning is an exact
# no-op — the production sample is byte-identical to the independent split — unless
# a caller supplies a defensible calibrated loading. A leg type with NO defensible
# structural link keeps a 0 loading and therefore stays INDEPENDENT (and routes to
# the worse-tail full-copula challenger backstop in ``sim/book_risk.py``); a
# nonzero loading is used only where a measured scoreline-state driver exists (e.g.
# the advance/corners extra-time channel). Fail-closed: an unknown ticker returns 0
# (independence), never a fabricated correlation. The DEFAULT is 0 for every leg (no
# conditioning), applied by ``sim/book_risk._copula_leg_loading``.
SharedFactorLoading = Callable[[str], float]


@dataclass(frozen=True, slots=True)
class StructuralConfigView:
    """The scalar Dixon-Coles constants the risk MC needs — a decoupled view of
    ``ops.config.StructuralConfig`` (pass its fields; avoids an ops<-sim import)."""

    dc_rho: float = -0.05
    et_factor: float = 0.3333
    pens_win_a: float = 0.5
    half_share: float = 0.45
    max_goals: int = 12
    knockout_series: tuple[str, ...] = ("KXWC",)
    enabled: bool = True
    # P0-7 PREFERRED — CONSERVATIVE shared-factor loading for the ONE defensible,
    # measured scoreline-state → copula-leg link: TOTAL corners settle INCLUDING
    # extra time, so in a KNOCKOUT game the extra-time window that a level-after-90
    # scoreline opens adds corners (config ``advance|corners`` measured a
    # dog +0.23 ↔ fav −0.23 ET strength curve, pooled ~0). The shared factor is the
    # game's total-goals intensity; this small positive loading (WITH WIDTH — a
    # conservative prior, not a strong fabricated correlation) couples a knockout
    # corners leg to it in the PRODUCTION sample. It is applied ONLY to knockout
    # corners legs (group-format corners are measured ⊥ goals — config
    # ``corners|total`` = 0.00 — and keep loading 0 → independence + the worse-tail
    # challenger). 0.0 ⇒ conditioning fully off (byte-identical to the independent
    # split). Cards / other copula leg types have no defensible measured link ⇒ 0.
    corners_et_loading: float = 0.10


@dataclass(frozen=True, slots=True)
class GamePlan:
    """One game the risk MC samples STRUCTURALLY: the inverted model + the specs it
    settles + the GLOBAL leg-universe column each spec fills."""

    params: ModelParams
    specs: tuple[LegSpec, ...]
    shares: dict[int, float]           # LOCAL spec index -> player thinning share
    global_indices: tuple[int, ...]    # value-matrix column for specs[k]


def _match_format(ticker: str, knockout_series: Sequence[str]) -> MatchFormat:
    series = ticker.split("-", 1)[0].upper()
    if any(series.startswith(p.upper()) for p in knockout_series):
        return MatchFormat.KNOCKOUT
    return MatchFormat.GROUP


def _try_build_game(
    idxs: list[int], tickers: Sequence[str], marginals: Sequence[float | None],
    cfg: StructuralConfigView,
) -> GamePlan | None:
    """Invert one game's structural legs, or None ⇒ the whole game is copula.

    Mirrors ``pricing.structural.StructuralPricer._price`` parse+invert (hard rule
    8c: reuses ``_parse_match``/``_parse_leg``/``invert`` verbatim; the sampled
    joint is parity-gated vs ``joint_probability``). A leg that won't parse
    (corners/cards) or has an unknown marginal is left to the copula (dropped from
    the plan). ``invert`` needs >=2 team-level legs + an orienting leg for scorers,
    else it raises and the game falls back to the copula entirely."""
    # Pricing aliases resolve for the game-code/format read exactly as they do
    # inside ``_parse_leg`` — an aliased champion leg carries the FINAL's game
    # code only on its synthetic ticker (its raw game segment is a bare season
    # code that parses to no match, which used to drop the whole game to copula).
    first = resolve_pricing_alias(tickers[idxs[0]])
    parts = first.split("-")
    if len(parts) < 2:
        return None
    match = _parse_match(parts[1])
    if match is None:
        return None
    fmt = _match_format(first, cfg.knockout_series)
    specs: list[LegSpec] = []
    gidx: list[int] = []
    targets: list[tuple[LegSpec, float]] = []
    for j in idxs:
        mgl = marginals[j]
        if mgl is None:
            continue
        spec = _parse_leg(tickers[j], match, fmt=fmt)
        if isinstance(spec, str):        # unrepresentable ⇒ copula
            continue
        specs.append(spec)
        gidx.append(j)
        targets.append((spec, float(mgl)))
    if not specs:
        return None
    try:
        model = invert(
            targets, dc_rho=cfg.dc_rho, et_factor=cfg.et_factor, match_format=fmt,
            max_goals=cfg.max_goals, pens_win_a=cfg.pens_win_a, half_share=cfg.half_share,
        )
    except StructuralError:
        return None
    return GamePlan(
        params=model.params, specs=tuple(specs), shares=dict(model.shares),
        global_indices=tuple(gidx),
    )


def build_game_plans(
    tickers: Sequence[str],
    events: Sequence[str | None],
    marginals: Sequence[float | None],
    cfg: StructuralConfigView,
) -> tuple[list[GamePlan], list[int]]:
    """Split the global leg universe into STRUCTURAL game plans + COPULA leg indices.

    Groups legs by game (``grouping.game_key`` of the event ticker), inverts each
    game's structural legs, and leaves everything the structural model can't settle
    (corners/cards, single-leg/unidentified games, ungamed legs) to the copula. The
    returned ``copula`` indices ∪ every plan's ``global_indices`` partition
    ``range(len(tickers))`` exactly."""
    n = len(tickers)
    if not cfg.enabled:
        return [], list(range(n))
    by_game: dict[str, list[int]] = defaultdict(list)
    copula: list[int] = []
    for j, ev in enumerate(events):
        if ev is None:
            copula.append(j)
        else:
            by_game[game_key(ev)].append(j)
    plans: list[GamePlan] = []
    for idxs in by_game.values():
        plan = _try_build_game(idxs, tickers, marginals, cfg)
        if plan is None:
            copula.extend(idxs)
            continue
        plans.append(plan)
        structural = set(plan.global_indices)
        copula.extend(j for j in idxs if j not in structural)
    return plans, copula


@dataclass(frozen=True, slots=True)
class CopulaConditioning:
    """P0-7 PREFERRED wiring: how each straddling-game copula leg is conditioned on
    its game's shared structural factor IN THE PRODUCTION SAMPLE.

    ``plan_of_copula_index`` maps a GLOBAL copula leg index → the index of the
    ``plans`` entry (the structural game) it straddles, or -1 if it straddles no
    structural game (cross-game / ungamed / same-game-with-no-inverted-structural-leg
    ⇒ NOT conditioned, sampled plain-copula as before). ``loading_of_copula_index``
    maps a GLOBAL copula leg index → its CONSERVATIVE loading ``beta`` in [-1, 1]
    onto that game's shared factor (0 ⇒ independence, the fail-closed default for a
    leg type with no defensible structural link). Both default empty ⇒ no leg is
    conditioned (byte-identical to the pre-P0-7 independent split)."""

    plan_of_copula_index: dict[int, int] = None  # type: ignore[assignment]
    loading_of_copula_index: dict[int, float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.plan_of_copula_index is None:
            object.__setattr__(self, "plan_of_copula_index", {})
        if self.loading_of_copula_index is None:
            object.__setattr__(self, "loading_of_copula_index", {})

    def active(self) -> bool:
        """True iff some copula leg has a nonzero loading onto a real plan (else the
        conditioning is an exact no-op and plain-copula sampling is used)."""
        return any(
            self.plan_of_copula_index.get(g, -1) >= 0 and abs(b) > 0.0
            for g, b in self.loading_of_copula_index.items()
        )


def sample_structural_values(
    plans: Sequence[GamePlan],
    copula_indices: Sequence[int],
    legs: Sequence[LegModel],
    corr: _FloatMatrix,
    n: int,
    rng: np.random.Generator,
    conditioning: CopulaConditioning | None = None,
) -> _FloatMatrix:
    """The full ``(n, len(legs))`` value matrix: structural columns sampled per game
    from the scoreline model, copula columns from the existing Gaussian-copula path
    (block corr restricted to those legs). Cross-game independence holds (each game
    draws from its own rng calls). Byte-compatible with ``sim/engine.book_pnl``.

    P0-7 PREFERRED — ``conditioning`` (default None ⇒ off, byte-identical to the
    independent split): where a copula leg straddles a structural game AND carries a
    nonzero CONSERVATIVE loading ``beta``, its Gaussian latent is blended with that
    game's shared structural factor

        z' = sqrt(1 − beta²)·z_copula + beta·f_game

    so the leg gains same-game structural↔copula dependence IN THE PRODUCTION SAMPLE
    while keeping its marginal AND its within-copula-block correlation exactly (the
    blend is a standard-normal-preserving rotation; ``f_game`` is the per-game
    standard-normal shared factor). A leg with loading 0 (no defensible link) is
    UNCHANGED — still independent of the structural block, still covered only by the
    worse-tail full-copula challenger in ``sim/book_risk.py`` (never underestimating
    the tail: the challenger is the backstop and the governing tail is the max)."""
    out = np.zeros((n, len(legs)), dtype=np.float64)
    cond = conditioning if conditioning is not None else None
    do_condition = cond is not None and cond.active()
    # Per-plan shared factor, materialized ONLY when the conditioning actually bites,
    # so the plain path takes no extra rng-free work and stays bit-identical.
    plan_factor: dict[int, _FloatMatrix] = {}
    for pi, plan in enumerate(plans):
        want_factor = do_condition and cond is not None and any(
            cond.plan_of_copula_index.get(g, -1) == pi
            and abs(cond.loading_of_copula_index.get(g, 0.0)) > 0.0
            for g in copula_indices
        )
        if want_factor:
            result = sample_game_values(
                plan.params, list(plan.specs), plan.shares, n, rng,
                with_shared_factor=True,
            )
            assert isinstance(result, tuple)
            vals, factor = result
            plan_factor[pi] = factor
        else:
            got = sample_game_values(
                plan.params, list(plan.specs), plan.shares, n, rng,
            )
            assert not isinstance(got, tuple)
            vals = got
        for local, gidx in enumerate(plan.global_indices):
            out[:, gidx] = vals[:, local]
    if copula_indices:
        idx = list(copula_indices)
        sub_legs = [legs[i] for i in idx]
        sub_corr = np.asarray(corr, dtype=np.float64)[np.ix_(idx, idx)]
        if do_condition:
            sub_vals = _sample_copula_conditioned(
                sub_legs, sub_corr, n, rng, idx, cond, plan_factor  # type: ignore[arg-type]
            )
        else:
            sub_vals = sample_leg_values(sub_legs, sub_corr, n, rng)
        for local, gidx in enumerate(idx):
            out[:, gidx] = sub_vals[:, local]
    return out


def _sample_copula_conditioned(
    sub_legs: Sequence[LegModel],
    sub_corr: _FloatMatrix,
    n: int,
    rng: np.random.Generator,
    global_idx: Sequence[int],
    cond: CopulaConditioning,
    plan_factor: dict[int, _FloatMatrix],
) -> _FloatMatrix:
    """Gaussian-copula sample of the copula block WITH per-leg shared-factor
    conditioning (P0-7 PREFERRED).

    KEEP IN SYNC with ``sim/engine.sample_leg_values`` (hard rule 8c): this mirrors
    its exact copula math — Cholesky-with-jitter of ``sub_corr``, ``z = N(0,1) @
    chol.T``, then the per-leg inverse-CDF table lookup — reproducing it byte-for-byte
    when NO leg is conditioned, and inserting exactly ONE extra step: a straddling
    leg with loading ``beta`` has its latent column rotated into its game's shared
    factor ``f`` via ``z' = sqrt(1−beta²)·z + beta·f`` BEFORE the ``ndtr``/table
    lookup. The rotation preserves the standard-normal marginal (so the leg CDF is
    untouched) and only adds the structural-state dependence. The engine's own
    ``sample_leg_values`` cannot host this (it has no game/structural context and
    must stay pristine), and the parity test pins that the unconditioned path here
    equals the engine's output on the same seed."""
    from scipy.special import ndtr

    from combomaker.sim.engine import _cholesky_with_jitter, _leg_table

    n_legs = len(sub_legs)
    corr_arr = np.asarray(sub_corr, dtype=np.float64)
    chol = _cholesky_with_jitter(corr_arr)
    z = rng.standard_normal((n, n_legs)) @ chol.T
    for local, gidx in enumerate(global_idx):
        pi = cond.plan_of_copula_index.get(gidx, -1)
        beta = cond.loading_of_copula_index.get(gidx, 0.0)
        if pi < 0 or abs(beta) <= 0.0 or pi not in plan_factor:
            continue
        beta = float(np.clip(beta, -0.999, 0.999))
        f = plan_factor[pi]
        z[:, local] = np.sqrt(1.0 - beta * beta) * z[:, local] + beta * f
    u = np.asarray(ndtr(z), dtype=np.float64)
    out = np.empty((n, n_legs), dtype=np.float64)
    for j, leg in enumerate(sub_legs):
        values, cum = _leg_table(leg)
        out[:, j] = values[np.searchsorted(cum, u[:, j], side="right")]
    return out


def _sampled_states(states: _States, idx: NDArray[np.int64]) -> _States:
    """A ``_States`` whose arrays are the sampled state rows (weights all 1 — the
    per-sample states are unweighted draws, the leg indicators are pointwise)."""
    return _States(
        w=np.ones(idx.size, dtype=np.float64),
        a90=states.a90[idx], b90=states.b90[idx],
        a_et=states.a_et[idx], b_et=states.b_et[idx],
        a_1h=states.a_1h[idx], b_1h=states.b_1h[idx],
    )


def _advance_settle(
    states: _States, spec: Advance, params: ModelParams, u_pens: NDArray[np.float64]
) -> _FloatMatrix:
    """0/1 advance settlement with a SHARED shootout coin (same ``u_pens`` for every
    advance leg on the game → advance(A) and advance(B) are exact opposites on a
    level-after-ET state)."""
    if spec.team is Team.A:
        us90, them90, us_et, them_et = states.a90, states.b90, states.a_et, states.b_et
        shoot_win = u_pens < params.pens_win_a
    else:
        us90, them90, us_et, them_et = states.b90, states.a90, states.b_et, states.a_et
        shoot_win = u_pens >= params.pens_win_a
    win = (us90 > them90) | ((us90 == them90) & (us_et > them_et))
    level = (us90 == them90) & (us_et == them_et)
    settled: _FloatMatrix = (win | (level & shoot_win)).astype(np.float64)
    return settled


def _shared_structural_factor(states: _States, idx: NDArray[np.int64]) -> _FloatMatrix:
    """The per-sample SHARED STRUCTURAL FACTOR for a game (P0-7 PREFERRED): the
    game's total sampled goals (incl. extra time) — "attacking pressure" /
    scoreline intensity — transformed to an (approx.) standard-normal latent via the
    distribution-free empirical-rank probability-integral transform (a copula PIT).

    A standard normal is exactly the latent scale the Gaussian copula legs live on,
    so a copula leg can be blended onto this factor with a loading ``beta`` and keep
    its marginal EXACTLY (``ndtr`` maps the blended standard normal back through the
    leg CDF). More goals ⇒ a higher factor; the rank transform is robust to the
    total-goals count being discrete and skewed. Ties (common on a discrete count)
    are broken by the stable argsort's arrival order — a within-tie permutation that
    does not bias the marginal or the loading."""
    total = (states.a90 + states.b90 + states.a_et + states.b_et)[idx].astype(
        np.float64
    )
    order = np.argsort(total, kind="stable")
    ranks = np.empty(total.size, dtype=np.float64)
    # Rank -> (0,1)-open plotting position, then the normal quantile.
    ranks[order] = (np.arange(total.size) + 0.5) / total.size
    from scipy.special import ndtri

    return np.asarray(ndtri(ranks), dtype=np.float64)


def sample_game_values(
    params: ModelParams,
    leg_specs: Sequence[LegSpec],
    shares: dict[int, float],
    n: int,
    rng: np.random.Generator,
    *,
    with_shared_factor: bool = False,
) -> _FloatMatrix | tuple[_FloatMatrix, _FloatMatrix]:
    """``(n, len(leg_specs))`` YES-settlement value matrix for ONE game's legs.

    ``leg_specs[j]`` is the Dixon-Coles spec for column j; ``shares[j]`` is the
    thinning share for a ``PlayerScores`` leg (ignored otherwise). Every column is
    settled 0/1 against the same ``n`` sampled game states (+ shared shootout /
    player coins), so columns are jointly correlated exactly as the model implies.

    P0-7: with ``with_shared_factor=True`` also returns the per-sample shared
    structural factor (``_shared_structural_factor``) computed from the SAME sampled
    states, so the caller can condition this game's copula-only fallback legs
    (corners/cards) on the game's scoreline intensity IN THE PRODUCTION SAMPLE.
    Returns ``(values, factor)`` then; ``values`` alone otherwise (byte-identical to
    the pre-P0-7 signature — the extra ``rng`` draws below are UNCHANGED, and the
    factor is a pure function of the already-sampled ``idx``, so enabling it does not
    perturb any other leg's sampled values)."""
    if n <= 0:
        raise ValueError(f"n must be > 0: {n}")
    need_halves = any(isinstance(s, _HALF_SPECS) for s in leg_specs)
    p = params if (params.with_halves or not need_halves) else replace(params, with_halves=True)
    states = _states(p)
    idx = rng.choice(states.w.size, size=n, p=states.w)
    sampled = _sampled_states(states, idx)
    u_pens = rng.random(n)                     # shared shootout coin (all advance legs)
    out = np.zeros((n, len(leg_specs)), dtype=np.float64)

    player_groups: dict[tuple[Team, bool], list[tuple[int, PlayerScores]]] = defaultdict(list)
    for j, spec in enumerate(leg_specs):
        if isinstance(spec, PlayerScores):
            player_groups[(spec.team, spec.include_et)].append((j, spec))
        elif isinstance(spec, Advance):
            out[:, j] = _advance_settle(sampled, spec, p, u_pens)
        elif isinstance(spec, _HALF_SPECS):
            out[:, j] = _half_indicator(sampled, spec)
        else:
            out[:, j] = _team_indicator(sampled, spec, p)

    for (team, inc_et), members in player_groups.items():
        n_team = _team_goals(sampled, team, inc_et)   # per-sample team goal count
        if len(members) == 1:
            j, spec = members[0]
            scored = rng.binomial(n_team, shares[j])
            out[:, j] = (scored >= spec.min_goals).astype(np.float64)
            continue
        # Shared multinomial: allocate the team's goals to its scorers via sequential
        # conditional binomials (min_goals == 1 enforced by the pricer for a group).
        remaining = n_team.copy()
        remaining_prob = 1.0
        for j, _spec in members:
            q = shares[j]
            pj = np.clip(q / remaining_prob, 0.0, 1.0) if remaining_prob > 0 else 0.0
            scored = rng.binomial(remaining, pj)
            out[:, j] = (scored >= 1).astype(np.float64)
            remaining = remaining - scored
            remaining_prob -= q
    if with_shared_factor:
        # Pure function of the already-sampled ``idx`` (NO rng draw) — computed last
        # so it can never perturb the leg sampling stream above.
        return out, _shared_structural_factor(states, idx)
    return out
