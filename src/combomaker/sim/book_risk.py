"""Full book-risk Monte Carlo + tail attribution + challenger/stress overlay
(RISK_BUILD_PLAN Phase 4 / research doc M1 §4, M2). Off the hot path.

Given a ``BookModel`` (sim/book_model.py — the pricer-consistent leg/corr/position
triple), this runs the portfolio MC and produces the five key risk outputs:

1. **P&L distribution** — EV ± MC standard error, std, P(profit).
2. **VaR / CVaR (tail loss)** at 0.95/0.99, reported at the ``corr_high`` band
   (correlation uncertainty widens risk, never hides it) — **CVaR_0.99 is the
   headline book-risk number** and the one the halts/limits consume.
3. **P(large drawdown / ruin)** — P(loss > threshold) at bankroll-tied thresholds
   (the ruin proxy for a NO-seller: many shared games break together).
4. **Per-GAME and per-LEG tail attribution** — the one genuinely new computation:
   which games/legs carry the tail loss. Σ per-game contribution = CVaR exactly
   (an additive decomposition), naming the games the operator must watch.
5. **Challenger / stress overlay** — the operative tail number is
   ``max(production-copula ES, challenger ES, deterministic stress)`` so a single
   correlation error is NOT approved twice by a monoculture of the pricer. The
   challenger is a **correlation-inflated** re-sample (every within-game block
   pushed toward comonotone); the deterministic stress is the **exact all-hit
   worst case** (every parlay HITS at once — the sell-side catastrophe), computed
   in closed form (no sampling), an unconditional upper bound the MC can never
   exceed.

Determinism: every MC call takes an explicit ``seed`` (``np.random.default_rng``),
so the same book always yields the same CVaR — auditable, testable decisions.
UNKNOWN book model (a missing marginal) is a HARD no-score: ``compute_book_risk``
returns a snapshot flagged ``unknown=True`` with NO usable stats, and the caller
treats it as widen-or-no-quote (fail-closed, hard rule 6). Money is float cc
inside the simulator by design (hard rule 5).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from combomaker.core.conventions import Side
from combomaker.risk.exposure import (
    LegRef,
    MarginalProvider,
    OpenPosition,
    mutex_scenario_bound,
)
from combomaker.sim.book_model import (
    BookModel,
    WithinGameRhoProvider,
    build_book_model,
    position_to_combo,
)
from combomaker.sim.engine import (
    ComboPosition,
    LegModel,
    PortfolioStats,
    book_pnl,
    position_pnl,
    sample_leg_values,
)
from combomaker.sim.structural_book import (
    CopulaConditioning,
    GamePlan,
    StructuralConfigView,
    build_game_plans,
    sample_structural_values,
)

# Headline tail level. CVaR here = expected loss at/beyond the 0.99 VaR quantile.
HEADLINE_LEVEL = 0.99

# How hard the challenger inflates within-game correlation toward comonotone.
# The challenger is an anti-monoculture check, not a second point estimate, so it
# deliberately over-correlates the shared games (the sell-side tail driver) to
# see whether the copula ES is robust to a correlation mis-estimate. 0.5 = push
# each within-game rho halfway to +1; tunable via ``challenger_inflation``.
DEFAULT_CHALLENGER_INFLATION = 0.5



@dataclass(frozen=True, slots=True)
class TailContribution:
    """One game's or leg's contribution to the tail (CVaR) loss, float cc.

    ``loss_cc`` is a POSITIVE loss magnitude = −E[contribution | tail]. Σ over
    games reproduces the book CVaR exactly (additive decomposition)."""

    key: str
    loss_cc: float


@dataclass(frozen=True, slots=True)
class BookRiskSnapshot:
    """The persisted, halt-feeding book-risk view for one MC run.

    All money is float cc (simulator domain). ``band`` is the correlation band the
    stats were computed at ("high" for the gating number).

    P0-3 separates the SAMPLED model tail from the DETERMINISTIC maximum loss so
    the all-hit maximum can no longer dominate (and thereby silence) the sampled
    ES. Two independent axes, gated independently by the portfolio caps:
      * ``governing_model_es_99_cc = max(production_es_99_cc, challenger_es_99_cc)``
        — the worst SAMPLED CVaR across scenarios (model joint tail). Reflects
        same-game hedges: a balancing fill can LOWER it.
      * ``deterministic_max_loss_cc`` — the exact comonotone all-hit premium-at-
        risk (+ reserved holdings). An unconditional upper bound the sampled ES
        can never exceed; a premium-at-risk cap, NOT an ES, so it is no longer
        maxed INTO the ES number.
    ``unknown`` True ⇒ a missing marginal made the whole snapshot no-go; NO stat
    below is usable (fail-closed)."""

    unknown: bool
    band: str
    n_samples: int
    seed: int
    n_positions: int

    # P0-2 position generation this snapshot was computed against. The caller
    # records ``ExposureBook.position_generation`` at the instant it reads the
    # positions, threads it in here, and — because the MC runs ASYNC off the hot
    # path — publishes the result only while the book's live position generation
    # still equals this value. A fill or settlement bumps the position generation
    # immediately, so a snapshot that is still time-fresh but computed against a
    # superseded portfolio is discarded (time age becomes a secondary guard, not the
    # consistency proof). Defaults to -1 for snapshots built without a
    # generation stamp (unit tests, direct callers): -1 never equals a real
    # generation (>= 0), so an un-stamped snapshot fails the generation-match guard
    # closed — the safe direction.
    input_generation: int = -1

    ev_cc: float = 0.0
    ev_stderr_cc: float = 0.0
    std_cc: float = 0.0
    p_profit: float = 0.0
    var_99_cc: float = 0.0
    es_99_cc: float = 0.0  # production-copula CVaR at ``band`` (== production_es_99_cc)
    p_loss_worse_than: dict[float, float] = field(default_factory=dict)
    # A2: P(this settlement wave drops equity BELOW the ruin floor) =
    # P(current_equity + book_pnl < ruin_floor_frac·bankroll). 0.0 when equity/
    # bankroll unavailable (the ruin cap then does not evaluate). Reflects the
    # structural hedge (not a comonotone). P1-1: this is the GOVERNING ruin number —
    # ``max`` over the production, correlation-inflated challenger, and full-copula
    # bridge books (gate on the worst credible model), mirroring the governing ES.
    p_ruin: float = 0.0
    # P1-2: one-sided Wilson UPPER confidence bound on ``p_ruin`` at the caller's
    # ``ruin_prob_ci_z`` (0 ⇒ == p_ruin). The ruin CAP in limits.py reads this, not
    # the point estimate, so a p̂ that only just clears the budget by sampling luck
    # near the budget is treated as over-budget (fail-closed against MC error).
    p_ruin_upper: float = 0.0

    # --- P0-3 separated tail axes (§5) ---------------------------------------
    # SAMPLED model tail, by scenario, and their governing max. These reflect the
    # structural/same-game hedge — a balancing fill can lower them.
    production_es_99_cc: float = 0.0  # production-copula CVaR (mirror of es_99_cc)
    challenger_es_99_cc: float = 0.0  # correlation-inflated challenger CVaR
    # P0-7: full-copula same-game dependence-bridge challenger CVaR. P0-7 is now the
    # CONDITIONED approach where a defensible measured scoreline-state link exists
    # (the production sample conditions those straddling copula legs on the game's
    # shared structural factor — see ``sim/structural_book``). This full-copula
    # bridge REMAINS as the conservative BACKSTOP for leg types with NO defensible
    # link (their copula-only block is still sampled independently of the structural
    # block): when a game straddles both blocks the book is ALSO re-sampled
    # full-copula (all same-game pairs coupled through the block correlation, at the
    # CHALLENGER-inflated matrix) and its ES is folded into the governing model tail
    # (gate on the WORSE tail). 0.0 when no game straddles both blocks or structural
    # sampling is off.
    bridge_es_99_cc: float = 0.0
    # True iff the full-copula bridge challenger ran (a game held both a structural
    # and a copula leg) — observability that the worse-tail backstop is active for
    # the unconditioned (no-defensible-link) part of a straddling game.
    bridge_active: bool = False
    # max(production[conditioned], challenger, bridge, structural-challenger,
    # independent-split guard) — the model gate. The independent-split guard ensures
    # the conditioned production tail is never reported below the independent split.
    governing_model_es_99_cc: float = 0.0
    # DETERMINISTIC maximum loss: exact all-hit premium-at-risk (+ reserved
    # holdings). A hard upper bound the sampled ES can never exceed — gated as its
    # OWN axis (premium-at-risk cap), never maxed into the ES number.
    deterministic_max_loss_cc: float = 0.0
    # MUTEX/SCENARIO-AWARE deterministic maximum loss (operator directive
    # 2026-07-18): the comonotone all-hit number above pretends MUTUALLY
    # EXCLUSIVE parlays (FRA-wins and ENG-wins of ONE game; two champion
    # outcomes) can all hit SIMULTANEOUSLY, which is impossible — so it taxed
    # diversifying flow at the det-max caps. This field is the sound
    # scenario-aware bound (``mutex_aware_det_max_from_units``): within a game,
    # max over that game's provably-exclusive outcome branches; across games,
    # sum (independent games' worst cases CAN co-occur); comonotone fallback
    # for every slice whose exclusivity is not PROVEN by structure. Invariants:
    # <= ``deterministic_max_loss_cc`` ALWAYS; == it when no mutex structure
    # exists among held combos; never below any single realizable joint
    # scenario's loss. The det-max CAP CHECKS gate on this field when
    # ``portfolio_det_max_mutex_aware`` is armed (the default); the comonotone
    # field above keeps emitting unchanged for telemetry/log continuity. None
    # (an UNKNOWN/empty snapshot, or a pre-fix snapshot) ⇒ consumers fall back
    # to the comonotone number (fail-closed: the LARGER bound).
    mutex_aware_det_max_cc: float | None = None

    # Tail attribution (§4.4).
    per_game_tail_cc: tuple[TailContribution, ...] = ()
    per_leg_tail_cc: tuple[TailContribution, ...] = ()

    @property
    def usable(self) -> bool:
        """True iff the stats may drive a gate/halt (not UNKNOWN, describes a real
        measured book).

        P0-4: an ALL-RESERVED book (0 sampled positions, nonzero deterministic
        reserve) IS fully measured — the sampled model tail is exactly 0 (nothing
        to sample) and the deterministic axis carries the whole reserve — so it
        must NOT grade as an unmeasured no-go. Before this clause a bot whose only
        holding was a conservatively-reserved gated-series position fail-closed
        EVERY quote on SKIP_PORTFOLIO_CVAR (live 2026-07-16, 3k declines/8min).
        UNKNOWN stays unusable; a truly-empty snapshot (no positions AND no
        reserve) stays unusable."""
        if self.unknown:
            return False
        return self.n_positions > 0 or self.deterministic_max_loss_cc > 0.0


def _deterministic_all_hit_loss_cc(model: BookModel) -> float:
    """The EXACT worst case: every position's combo resolves against us at once.

    For a long NO position the worst outcome is the parlay HITS (payout $1/ct) →
    we lose the whole premium and pay nothing back, i.e. the P&L is
    ``−price·contracts − fee``... but wait: the sell-side catastrophe is the
    TAKER collecting $1 — our realized loss on the NO is exactly the premium we
    paid (``max_loss`` axis, verified ground truth). For a long YES the worst case
    is the combo MISSES (payout 0) → lose the premium. Either way the worst-case
    per-position loss is ``price_cc·contracts + fee_cc`` (premium + fee). This is
    the comonotone premium worst case the analytic exposure book already sums
    (``worst_case_loss_by_game_cc``), here rolled up over the whole book as an
    unconditional upper bound the sampled ES can never exceed.

    Returned as a POSITIVE loss magnitude in float cc."""
    total = 0.0
    for pos in model.positions:
        total += float(pos.price_cc) * pos.contracts + float(pos.fee_cc)
    return total


def modeled_cost_basis_cc(model: BookModel) -> float:
    """Total ENTRY COST (premium paid) of the risk-modeled positions, float cc:
    ``Σ price_cc · contracts`` over ``model.positions``.

    P1-3 (no double count of position value). The ruin check adds the sampled
    ``book_pnl`` — which is measured ENTRY-to-terminal (``payout − price_cc`` per
    contract; see ``engine._position_pnl``) — onto a scalar equity basis. The ONLY
    equity basis that reconciles that entry-based P&L to the true terminal equity
    without double-counting the position's already-marked value is the COST basis:

        available_cash + Σ price_cc·contracts + book_pnl
          = available_cash + Σ price_cc·c + Σ(payout − price_cc)·c
          = available_cash + Σ payout·c                    (= true terminal equity)

    i.e. the entry premium cancels exactly, leaving cash plus realized payout, with
    NO dependence on the intraday mark. Feeding ``exchange_equity`` (cash +
    portfolio_value) instead would leave a residual ``portfolio_value −
    Σ price_cc·c`` = the unrealized mark-to-market ALREADY in equity, double-
    counting the position value. ``build_book_model`` sets ``fee_cc = 0`` on every
    ``ComboPosition`` (fees are already debited from live cash and are 0 in
    ``book_pnl``), so the cost basis is premium only. Reserved (unmodeled) holdings
    are excluded here exactly as they are from ``book_pnl`` (their risk is the
    separate deterministic reserve, never in this settlement-wave P&L)."""
    return float(
        sum(float(p.price_cc) * p.contracts for p in model.positions)
    )


def _p_ruin_from_pnl(
    pnl: NDArray[np.float64],
    current_equity_cc: int | None,
    ruin_floor_cc: float | None,
) -> float:
    """P(this settlement wave drops equity BELOW the ruin floor) on one sampled
    book P&L vector: ``P(equity_basis + book_pnl < ruin_floor)``.

    ``current_equity_cc`` is the COST-basis equity for the modeled book
    (available_cash + ``modeled_cost_basis_cc``), NOT exchange equity — see
    ``modeled_cost_basis_cc`` for the no-double-count proof. Returns 0.0 when
    equity/floor are unavailable (the ruin cap then does not evaluate) or the P&L
    vector is empty. Uses LIVE cash so the probability tightens as we draw down (a
    fixed loss threshold would understate ruin once equity < bankroll)."""
    if current_equity_cc is None or ruin_floor_cc is None or pnl.size == 0:
        return 0.0
    return float(np.mean(current_equity_cc + pnl < ruin_floor_cc))


def wilson_upper_bound(p_hat: float, n: int, z: float) -> float:
    """One-sided Wilson-score UPPER confidence bound on a binomial proportion.

    P1-2 (confidence bounds near the ruin budget). ``p_ruin`` is a Monte-Carlo
    estimate ``p̂ = k/n`` of a binomial proportion, so it carries sampling error.
    When p̂ sits just under the ruin budget the TRUE ruin probability may be over
    it — gating on the point estimate would then admit a fill whose ruin risk is
    only statistically-indistinguishable-from-safe. Fail-closed (hard rule 6)
    means gating on the UPPER end of a confidence interval instead: a p̂ that
    could plausibly be over-budget is treated as over-budget.

    The Wilson score interval is used (not Wald): it is well-behaved for the small
    p̂ and finite n we operate at (Wald degenerates to a zero-width interval at
    p̂ = 0, which would defeat the whole point near a small ruin budget). Closed
    form for the one-sided upper bound at z standard normal deviations:

        centre = (p̂ + z²/2n) / (1 + z²/n)
        halfwidth = (z / (1 + z²/n)) · sqrt( p̂(1−p̂)/n + z²/4n² )
        upper = min(1, centre + halfwidth)

    ``z = 0`` returns p̂ exactly (no widening) — the default everywhere, so the
    point-estimate behaviour is preserved bit-for-bit unless an operator opts into
    a positive confidence level. ``n <= 0`` (nothing sampled ⇒ the ruin cap does
    not evaluate) returns p̂ unchanged. p̂ is clamped to [0,1] defensively."""
    if z <= 0.0 or n <= 0:
        return p_hat
    p = min(1.0, max(0.0, p_hat))
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / denom
    halfwidth = (z / denom) * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    return min(1.0, centre + halfwidth)


def ruin_samples_for_precision(
    p_hat: float, target_halfwidth: float, z: float
) -> int:
    """Adaptive sample count: the ``n`` a p̂ estimate needs so its z-level Wilson
    half-width is ``<= target_halfwidth`` near the ruin budget (P1-2).

    Solves the large-n Wald approximation ``z·sqrt(p̂(1−p̂)/n) <= target`` for n
    (Wald is the right guide for a SAMPLE-SIZE target — it is the limit the Wilson
    width converges to and is monotone in n, so a conservative n suffices):

        n >= z² · p̂(1−p̂) / target²

    Used to decide whether a first MC pass whose p̂ landed NEAR the budget must be
    RE-RUN at more samples before its ruin gate is trusted (an under-sampled
    estimate straddling the budget is exactly the fail-closed case). Returns 0
    when no widening is requested (``z <= 0`` or ``target_halfwidth <= 0``), and
    at least 1 otherwise. p̂ is clamped to [0,1]. Worst-case variance (p̂ = 0.5) is
    NOT assumed — the caller passes the OBSERVED p̂, so a tiny ruin probability does
    not demand an enormous n."""
    if z <= 0.0 or target_halfwidth <= 0.0:
        return 0
    p = min(1.0, max(0.0, p_hat))
    n = (z * z * p * (1.0 - p)) / (target_halfwidth * target_halfwidth)
    return max(1, int(math.ceil(n)))


def _es_from_pnl(pnl: NDArray[np.float64], level: float) -> tuple[float, float]:
    """(VaR, ES) at ``level`` from a P&L vector — positive loss magnitudes.

    Same definition the engine uses (``_stats_from_pnl``): VaR = max(0,
    −quantile(pnl, 1−level)); ES = mean loss at/beyond that quantile, falling
    back to VaR on an empty tail."""
    if pnl.size == 0:
        return 0.0, 0.0
    cut = float(np.quantile(pnl, 1.0 - level))
    var = max(0.0, -cut)
    tail = pnl[pnl <= cut]
    es = max(0.0, -float(tail.mean())) if tail.size > 0 else var
    return var, es


def _tail_loss_from_pnl(pnl: NDArray[np.float64], level: float) -> float:
    """UNCLAMPED expected tail loss at ``level`` — the same tail set as
    ``_es_from_pnl`` (P&L at/below the (1−level) quantile) WITHOUT the
    ``max(0, ·)`` clamp, so a still-profitable sampled tail reports a NEGATIVE
    loss (the size of the tail profit cushion) instead of 0.

    2026-07-18 verify fix: the certified-hedge gate compares THIS number pre vs
    post. The clamped ES is exactly 0.0 on any book whose worst-1% sampled
    outcome is still net-profitable (a fresh book after a settlement-day reset,
    or any small early book of +EV fills), which made the risk-reduction
    certification VACUOUS there — 0 <= 0 passed for EVERY candidate, including
    fills that hedge nothing, re-admitting the sniper tax the certification
    exists to exclude. On the unclamped number, eroding the tail cushion counts
    against the candidate; whenever the tail is a genuine loss it equals the
    clamped ES exactly, so the certification is unchanged in the loss regime
    and strictly TIGHTER (decline-only) in the profit-clamped regime. Empty
    ``pnl`` ⇒ 0.0."""
    if pnl.size == 0:
        return 0.0
    cut = float(np.quantile(pnl, 1.0 - level))
    tail = pnl[pnl <= cut]
    return -float(tail.mean()) if tail.size > 0 else -cut


def _same_game_mask(model: BookModel) -> NDArray[np.bool_]:
    """Boolean ``(n, n)`` mask: True where legs i and j are in the SAME game.

    The challenger over-correlates ONLY the intended within-game pairs — the
    block structure ``build_book_model`` already builds (cross-game pairs sit at
    ``cross_event_rho`` ≈ 0 and MUST stay there). Grouping uses the pricer's own
    ``game_key`` on each leg's ``event_ticker`` (the same key the copula
    correlates on and the exposure book aggregates on). A leg with no event
    ticker (``game_key`` cannot place it in a game) matches ONLY itself, so an
    ungamed leg never inflates against anything (fail-closed: an unknown game
    grouping never manufactures a cross-leg shock). The diagonal is left False —
    ``_inflate_corr`` restores it explicitly."""
    from combomaker.pricing.grouping import game_key

    n = len(model.legs)
    games: list[str | None] = [None] * n
    for idx in range(n):
        event = model.event_by_index.get(idx)
        games[idx] = game_key(event) if event else None
    mask = np.zeros((n, n), dtype=np.bool_)
    for i in range(n):
        gi = games[i]
        if gi is None:
            continue  # ungamed leg: no same-game partner (never inflated)
        for j in range(i + 1, n):
            if games[j] == gi:
                mask[i, j] = True
                mask[j, i] = True
    return mask


def _inflate_corr(
    corr: NDArray[np.float64],
    inflation: float,
    same_game_mask: NDArray[np.bool_] | None = None,
) -> NDArray[np.float64]:
    """Push SAME-GAME off-diagonal correlations toward +1 by ``inflation``
    fraction (the challenger's over-correlation), leaving CROSS-GAME values
    UNCHANGED. ``rho' = rho + inflation·(1 − rho)`` for every entry the
    ``same_game_mask`` selects; every other off-diagonal (and the diagonal) keeps
    its original value.

    P0-8: universal positive correlation is NOT always conservative — for a book
    that is HEDGED across games, forcing cross-game pairs from 0 toward +0.5 can
    REDUCE the tail rather than fatten it (the challenger would then understate
    risk, the opposite of its purpose). So the challenger inflates ONLY the
    intended within-game block (the sell-side tail driver) and preserves the
    measured cross-game independence. A cross-game shock, if ever wanted, belongs
    in a SEPARATE named regime scenario, not smuggled in here.

    ``same_game_mask`` None ⇒ NO pair is inflated (the matrix is returned
    unchanged bar a diagonal repair): with no game grouping the conservative
    default is to touch nothing rather than inflate blindly (fail-closed). The
    diagonal is restored exactly so the result is a valid correlation matrix;
    PSD repair happens in the engine's Cholesky-with-jitter at sample time."""
    if not 0.0 <= inflation <= 1.0:
        raise ValueError(f"inflation must be in [0,1], got {inflation}")
    n = corr.shape[0]
    out = corr.astype(np.float64, copy=True)
    if same_game_mask is not None:
        if same_game_mask.shape != (n, n):
            raise ValueError(
                f"same_game_mask shape {same_game_mask.shape} != corr {(n, n)}"
            )
        # Inflate ONLY the masked (same-game, off-diagonal) entries; cross-game
        # entries are copied through untouched.
        inflated = out + inflation * (1.0 - out)
        out = np.where(same_game_mask, inflated, out)
    # Restore the exact diagonal (guard float noise) so the matrix stays a valid
    # correlation matrix.
    idx = np.arange(n)
    out[idx, idx] = 1.0
    return out


def _tail_attribution(
    values: NDArray[np.float64],
    model: BookModel,
    tail_mask: NDArray[np.bool_],
) -> tuple[tuple[TailContribution, ...], tuple[TailContribution, ...]]:
    """Per-game and per-leg contribution to the tail loss.

    ``tail_mask`` selects the tail scenarios (book P&L ≤ the VaR cut). For each
    game g, ``contrib_g = −E[ Σ_{positions touching g} position_pnl | tail ]``,
    computed by re-running the engine's ``position_pnl`` on the tail rows and
    grouping by the leg's ``event_ticker`` game key. Σ_g contrib_g = CVaR by
    construction. Per-leg: attribute each position's tail P&L equally across the
    legs it references (a cheap, additive proxy for which legs carry the tail).
    Both returned as POSITIVE loss magnitudes, descending."""
    if not tail_mask.any():
        return (), ()
    tail_values = values[tail_mask]
    n_tail = tail_values.shape[0]

    # Map each latent index → its game code (via the model's event map + grouping
    # already applied in book_model: event_by_index holds the event ticker; we
    # regroup to the game key here for the attribution label).
    from combomaker.pricing.grouping import game_key

    game_of_index: dict[int, str] = {}
    for idx, event in model.event_by_index.items():
        game_of_index[idx] = game_key(event) if event else f"idx:{idx}"

    per_game: dict[str, float] = defaultdict(float)
    per_leg: dict[str, float] = defaultdict(float)
    for pos in model.positions:
        pnl_tail = position_pnl(tail_values, pos)  # (n_tail,) float cc
        mean_contrib = float(pnl_tail.mean())  # E[position pnl | tail] (signed)
        # Games this position touches (a position may span games).
        games = {game_of_index.get(i, f"idx:{i}") for i in pos.leg_indices}
        # Split the position's tail contribution across the games it touches so
        # the per-game sum stays additive to the book CVaR (a multi-game position
        # is shared; equal split is the neutral additive choice).
        share = mean_contrib / len(games) if games else mean_contrib
        for g in games:
            per_game[g] += share
        leg_share = mean_contrib / len(pos.leg_indices)
        for i in pos.leg_indices:
            per_leg[str(i)] += leg_share

    # Convert signed E[pnl|tail] into positive loss magnitudes (a positive
    # contribution REDUCES the loss; keep the sign so Σ = −CVaR consistent).
    def _to_contribs(d: dict[str, float]) -> tuple[TailContribution, ...]:
        items = [TailContribution(key=k, loss_cc=-v) for k, v in d.items()]
        items.sort(key=lambda c: c.loss_cc, reverse=True)
        return tuple(items)

    _ = n_tail  # documented: attribution is a conditional mean, size in the mask
    return _to_contribs(per_game), _to_contribs(per_leg)


# Sampler signature: (legs, corr, n, rng) -> (n, len(legs)) leg-value matrix.
_Sampler = Callable[
    [Sequence[LegModel], NDArray[np.float64], int, np.random.Generator],
    NDArray[np.float64],
]


@dataclass(frozen=True, slots=True)
class _SamplerBundle:
    """The value sampler for a model PLUS the structural/copula split it was built
    from — enough for P0-7's same-game dependence bridge to decide whether the
    structural split is discarding cross-block dependence (and therefore whether a
    full-copula challenger must be run and gated on the worse tail).

    ``sampler`` is the (legs, corr, n, rng) callable ``compute_book_risk`` /
    ``evaluate_candidate_book_risk`` already use. ``structural`` is True iff the
    sampler is the STRUCTURAL split (some game inverted); False ⇒ the whole book is
    Gaussian-copula sampled and no bridge is needed (the copula ALREADY carries
    every same-game cross-block pair through the block correlation).
    ``bridge_needed`` is True iff at least one game holds BOTH a structural leg and
    a copula leg — the exact case the structural split samples SEPARATELY (its two
    blocks draw from independent rng calls), discarding that game's structural↔
    copula dependence. When True the caller runs a full-copula challenger and gates
    on the worse tail (P0-7 interim)."""

    sampler: _Sampler
    structural: bool
    bridge_needed: bool
    # P0-7 PREFERRED: True iff the production ``sampler`` conditions at least one
    # straddling copula leg on its game's shared structural factor (a defensible
    # nonzero loading). When True the caller ALSO samples the UNCONDITIONED split
    # (``split_sampler``) and folds its ES into the governing max, so the conditioned
    # production tail is never reported below the independent split (never thinner).
    conditioned: bool = False
    split_sampler: _Sampler | None = None


def _bridge_needed(
    model: BookModel, plans: Sequence[GamePlan], copula_idx: Sequence[int]
) -> bool:
    """True iff some game has BOTH a structural leg (in a plan) and a copula leg.

    The structural split samples the structural block (per game, from the scoreline
    model) and the copula block (the remaining legs) from SEPARATE rng calls, so any
    game that straddles the two blocks — a structural scoreline leg AND a copula-only
    corners/cards leg on the SAME game — has its cross-block dependence discarded.
    Grouping uses the pricer's own ``game_key`` on each leg's event ticker (the same
    key the copula correlates on), so a copula leg with no game (``game_key`` None)
    can never straddle a structural game (fail-closed: an ungamed copula leg never
    triggers — nor suppresses — the bridge)."""
    from combomaker.pricing.grouping import game_key

    structural_games: set[str] = set()
    for plan in plans:
        for gidx in plan.global_indices:
            event = model.event_by_index.get(gidx)
            if event:
                structural_games.add(game_key(event))
    if not structural_games:
        return False
    for cidx in copula_idx:
        event = model.event_by_index.get(cidx)
        if event and game_key(event) in structural_games:
            return True
    return False


def _copula_leg_loading(
    ticker: str, is_knockout: bool, cfg: StructuralConfigView
) -> float:
    """The CONSERVATIVE shared-factor loading for ONE copula leg (P0-7 PREFERRED).

    Returns 0 (independence — the fail-closed default) for every copula leg type
    with NO defensible measured scoreline-state link, and a small positive loading
    ONLY for a TOTAL-corners leg in a KNOCKOUT game (the one measured link: corners
    settle including ET, so the extra-time window a level-after-90 scoreline opens
    adds corners — config ``advance|corners`` ET strength curve). Group-format
    corners are measured ⊥ goals (config ``corners|total`` = 0.00) ⇒ 0. Cards and any
    other copula leg type ⇒ 0. A leg left at 0 keeps independence in the production
    sample and is covered only by the worse-tail full-copula challenger (never
    underestimating the tail). Loading magnitude is capped conservatively small (the
    pooled ET effect is weak and orientation-dependent; we do not fabricate a strong
    correlation)."""
    from combomaker.pricing.legtypes import LegType, classify_leg

    if cfg.corners_et_loading == 0.0 or not is_knockout:
        return 0.0
    if classify_leg(ticker) is LegType.CORNERS:
        return float(max(0.0, min(0.30, cfg.corners_et_loading)))
    return 0.0


def _build_conditioning(
    model: BookModel,
    plans: Sequence[GamePlan],
    copula_idx: Sequence[int],
    cfg: StructuralConfigView,
) -> CopulaConditioning:
    """Map each straddling copula leg → (its structural game plan, conservative
    loading) for the P0-7 PREFERRED production-sample conditioning.

    A copula leg is conditioned only when it shares a game (via the pricer's
    ``game_key``) with a structural plan AND its leg type carries a defensible
    nonzero loading (``_copula_leg_loading``). Cross-game / ungamed / group-format /
    no-defensible-link copula legs get plan −1 / loading 0 ⇒ sampled plain-copula
    (independent of the structural block) exactly as before, and covered by the
    worse-tail challenger. Empty maps ⇒ conditioning is an exact no-op."""
    from combomaker.pricing.grouping import game_key
    from combomaker.pricing.legtypes import resolve_pricing_alias

    ticker_of_index = {i: t for t, i in model.leg_index.items()}
    # game_key(event) -> plan index, for every structural game.
    plan_of_game: dict[str, int] = {}
    knockout_of_game: dict[str, bool] = {}
    for pi, plan in enumerate(plans):
        for gidx in plan.global_indices:
            event = model.event_by_index.get(gidx)
            if not event:
                continue
            gk = game_key(event)
            plan_of_game.setdefault(gk, pi)
            # A game is knockout iff its structural legs were inverted under the
            # knockout format — proxied by the leg-series prefix the config
            # lists, read off the ALIAS-RESOLVED ticker (review 2026-07-16: the
            # raw champion series would flip the final's flag off whenever the
            # aliased leg iterated last) and OR-folded so any knockout leg in
            # the game marks it knockout, order-independent.
            series = (
                resolve_pricing_alias(ticker_of_index.get(gidx, ""))
                .split("-", 1)[0]
                .upper()
            )
            knockout_of_game[gk] = knockout_of_game.get(gk, False) or any(
                series.startswith(p.upper()) for p in cfg.knockout_series
            )
    plan_map: dict[int, int] = {}
    load_map: dict[int, float] = {}
    for cidx in copula_idx:
        event = model.event_by_index.get(cidx)
        if not event:
            continue
        gk = game_key(event)
        pi = plan_of_game.get(gk, -1)
        if pi < 0:
            continue
        ticker = ticker_of_index.get(cidx, "")
        beta = _copula_leg_loading(ticker, knockout_of_game.get(gk, False), cfg)
        if beta == 0.0:
            continue
        plan_map[cidx] = pi
        load_map[cidx] = beta
    return CopulaConditioning(plan_map, load_map)


def _select_sampler(
    model: BookModel, structural_cfg: StructuralConfigView | None
) -> _SamplerBundle:
    """The value sampler for this model (A1 structural seam) + its P0-7 bridge flag.

    With a ``structural_cfg`` the games Dixon-Coles can invert are sampled from the
    joint scoreline (every same-game hedge/exclusion exact, no rho) and only the
    copula legs (corners/cards/other sports) use the Gaussian copula; without it
    the whole book is copula-sampled (byte-identical to before). Extracted verbatim
    from ``compute_book_risk`` so the candidate-aware evaluator reuses the EXACT
    same seam (hard rule 8) rather than reimplementing the dispatch.

    P0-7: also reports whether the structural split is discarding same-game cross-
    block dependence (``bridge_needed``), so the caller can run a full-copula
    challenger and gate on the worse tail. The plain copula sampler needs no bridge
    (it already carries every same-game pair through the block correlation)."""
    if structural_cfg is None:
        return _SamplerBundle(sample_leg_values, structural=False, bridge_needed=False)
    tickers = [""] * len(model.legs)
    for ticker, i in model.leg_index.items():
        tickers[i] = ticker
    events = [model.event_by_index.get(i) for i in range(len(model.legs))]
    marginals = [leg.p for leg in model.legs]
    plans, copula_idx = build_game_plans(tickers, events, marginals, structural_cfg)
    # P0-7 PREFERRED: condition straddling copula legs on their game's shared
    # structural factor IN THE PRODUCTION SAMPLE (where a defensible measured link
    # exists); legs with no link stay independent + covered by the worse-tail bridge.
    conditioning = _build_conditioning(model, plans, copula_idx, structural_cfg)
    is_conditioned = conditioning.active()

    def _structural_sampler(
        leg_models: Sequence[LegModel],
        c: NDArray[np.float64],
        n_draw: int,
        r: np.random.Generator,
    ) -> NDArray[np.float64]:
        return sample_structural_values(
            plans, copula_idx, leg_models, c, n_draw, r, conditioning=conditioning
        )

    def _split_sampler(
        leg_models: Sequence[LegModel],
        c: NDArray[np.float64],
        n_draw: int,
        r: np.random.Generator,
    ) -> NDArray[np.float64]:
        # The UNCONDITIONED structural split (independent copula block) — the guard
        # baseline the conditioned production tail may never be reported below.
        return sample_structural_values(plans, copula_idx, leg_models, c, n_draw, r)

    return _SamplerBundle(
        _structural_sampler,
        structural=bool(plans),
        bridge_needed=_bridge_needed(model, plans, copula_idx),
        conditioned=is_conditioned,
        split_sampler=_split_sampler if is_conditioned else None,
    )


# ---------------------------------------------------------------------------
# P1.9: independent STRUCTURAL-PARAMETER challenger.
#
# The correlation-inflation challenger (P0-8) stresses the JOINT dependence but
# takes every structural INPUT — the inverted per-game goal rates, the DC low-score
# rho, the extra-time / shootout / half-share settlement constants, the knockout
# (mutex-metadata) classification, the feed marginals — as GROUND TRUTH. That is a
# monoculture on the structural axis: if a goal rate is mis-inverted, the DC rho is
# off, a game is mis-classified as knockout (turning on the advance/ET/shootout
# settlement geometry), or a marginal arrives shocked, the production tail and its
# correlation challenger are BOTH wrong the same way and neither catches it.
#
# This challenger re-inverts and re-samples the structural games under a
# conservatively-perturbed ``StructuralConfigView`` — each named input shifted to a
# plausible-but-adverse corner of the model-form band the pricer already publishes —
# and its ES / P(ruin) fold into the governing model max exactly as the correlation
# and bridge challengers do (gate on the WORSE tail). It is NOT a second point
# estimate and NEVER lowers a number: it can only WIDEN the governing tail, so it is
# purely a fail-closed anti-monoculture check on the structural inputs. Named
# dimensions it stresses, tied to the plan's item-9 list:
#   * goal rates      — re-inversion under the shifted rho/ET/half re-fits each
#                        game's Poisson means, so the challenger goal rates differ
#                        from production (the goal-rate perturbation IS the re-fit).
#   * DC rho          — dc_rho shifted by ``rho_band`` toward more low-score mass.
#   * marginals       — each target marginal shocked toward its combo-adverse edge
#                        by ``marginal_shock`` before inversion (a feed-error proxy:
#                        what if the leg books we inverted from were mis-marked?).
#   * settlement rules— et_factor / pens_win_a / half_share shifted by their bands
#                        (the extra-time, shootout, and half-split geometry that the
#                        settlement windows turn on).
#   * mutex metadata  — the knockout classification decides whether advance/ET/
#                        shootout settlement (a mutex family: advance(A) ⊥ advance(B))
#                        is active at all; ``force_knockout`` challenges a GROUP
#                        classification by ALSO pricing the book as knockout, so a
#                        mis-tagged group game that is really a knockout is stressed.
#   * feed errors     — subsumed by ``marginal_shock`` (a shocked marginal is exactly
#                        a stale/erroneous feed) and the fail-closed skip below (any
#                        game the challenger cannot re-invert is left to the copula,
#                        never silently dropped from the tail).
#   * cross-game regime— unchanged here: cross-game dependence is a SEPARATE named
#                        regime (P0-8) and is stressed by the correlation challenger,
#                        never smuggled into the structural re-inversion.


@dataclass(frozen=True, slots=True)
class StructuralChallengerBands:
    """Half-band shifts for the P1.9 structural-parameter challenger.

    Every field is a signed/again-positive perturbation applied to the production
    ``StructuralConfigView`` before the structural games are RE-INVERTED and
    re-sampled. All default 0.0 / False, so a ``StructuralChallengerBands()`` with
    no fields set perturbs NOTHING — the challenger config equals production and the
    re-sample is an exact no-op (it can never LOWER the governing tail; a zero-width
    challenger simply does not move it). A caller opts a real width in to make the
    challenger bite. Bands mirror ``ops.config.StructuralConfig`` model-form widths
    (dc_rho_band, et_factor_low/high, pens_band, half_share_band).

    Sign convention — every shift is applied in the TAIL-FATTENING direction for a
    NO-seller (the sell-side catastrophe is parlays HITTING), so the challenger is
    monotonically conservative:
      * ``rho_band``       lowers dc_rho (more low-score / draw mass → BTTS-no, unders,
                           and same-game exclusion structure shift adversely).
      * ``et_factor_band`` RAISES et_factor (more extra-time scoring → advance/BTTS/
                           totals settle differently on level-after-90 states).
      * ``pens_band``      pushes pens_win_a toward 0.5 (the max-entropy shootout, the
                           least predictable — most tail — coin) unless already there,
                           in which case it is left (0.5 is already worst-case).
      * ``half_share_band``RAISES half_share (more first-half mass → 1H legs settle
                           against a heavier first half).
      * ``marginal_shock`` widens each inverted target marginal toward 0.5 by this
                           fraction (an erroneous/stale feed mark → more uncertain,
                           tail-fattening leg) before inversion.
      * ``force_knockout`` also prices GROUP games as KNOCKOUT (challenges a possibly
                           wrong mutex/settlement classification)."""

    rho_band: float = 0.0
    et_factor_band: float = 0.0
    pens_band: float = 0.0
    half_share_band: float = 0.0
    marginal_shock: float = 0.0
    force_knockout: bool = False

    @property
    def active(self) -> bool:
        """True iff any band actually perturbs something (else an exact no-op)."""
        return bool(
            self.rho_band
            or self.et_factor_band
            or self.pens_band
            or self.half_share_band
            or self.marginal_shock
            or self.force_knockout
        )


# Conservative default bands used when the caller opts the structural challenger ON
# with ``structural_challenger_bands=None``: the config's published model-form widths
# (StructuralConfig.{dc_rho_band=0.08, et_factor half-width≈0.07, pens_band=0.10,
# half_share_band=0.03}) plus a small marginal feed shock and the knockout-metadata
# challenge. These are the SAME uncertainties the pricer already carries; the
# challenger just re-prices the joint at their adverse corner.
DEFAULT_STRUCTURAL_CHALLENGER_BANDS = StructuralChallengerBands(
    rho_band=0.08,
    et_factor_band=0.07,
    pens_band=0.10,
    half_share_band=0.03,
    marginal_shock=0.05,
    force_knockout=True,
)


def _challenger_structural_cfg(
    cfg: StructuralConfigView, bands: StructuralChallengerBands
) -> StructuralConfigView:
    """The production ``StructuralConfigView`` shifted to the challenger's adverse
    corner (P1.9). Each constant is moved by its band in the tail-fattening direction
    (see ``StructuralChallengerBands`` sign convention) and clamped to a valid range.
    ``force_knockout`` widens ``knockout_series`` to ``("",)`` — every ticker starts
    with "" so every game is classified KNOCKOUT (the settlement/mutex-metadata
    challenge). ``marginal_shock`` is NOT applied here (it perturbs the per-game
    target marginals at inversion time, not a scalar constant)."""
    from dataclasses import replace as _replace

    et = min(0.60, cfg.et_factor + max(0.0, bands.et_factor_band))
    # Push the shootout coin toward the max-entropy 0.5 (most tail), never past it.
    if cfg.pens_win_a <= 0.5:
        pens = min(0.5, cfg.pens_win_a + max(0.0, bands.pens_band))
    else:
        pens = max(0.5, cfg.pens_win_a - max(0.0, bands.pens_band))
    half = min(0.55, cfg.half_share + max(0.0, bands.half_share_band))
    rho = cfg.dc_rho - max(0.0, bands.rho_band)  # lower rho ⇒ more low-score mass
    knockout = ("",) if bands.force_knockout else cfg.knockout_series
    return _replace(
        cfg, dc_rho=rho, et_factor=et, pens_win_a=pens, half_share=half,
        knockout_series=knockout,
    )


def _shock_marginals(
    model: BookModel, shock: float
) -> dict[int, float] | None:
    """Per-leg-index marginals shifted toward 0.5 by ``shock`` fraction — a
    feed-error / stale-mark proxy (P1.9). ``p' = p + shock·(0.5 − p)`` widens each
    leg toward maximum uncertainty (the tail-fattening direction: a less-confident
    leg contributes more joint-tail mass). Returns None when ``shock <= 0`` (no
    shock ⇒ the challenger inverts the ORIGINAL marginals, an exact no-op on this
    axis). Clamped to (0,1) exclusive so inversion never sees a degenerate 0/1."""
    if shock <= 0.0:
        return None
    out: dict[int, float] = {}
    for i, leg in enumerate(model.legs):
        p = float(leg.p)
        shocked = p + shock * (0.5 - p)
        out[i] = min(0.999, max(0.001, shocked))
    return out


def _structural_challenger_bundle(
    model: BookModel,
    structural_cfg: StructuralConfigView,
    bands: StructuralChallengerBands,
) -> _SamplerBundle | None:
    """A sampler that re-inverts + re-samples the structural games under the
    challenger config + shocked marginals (P1.9), or None when the challenger cannot
    apply (no structural game inverts under the perturbed config, or ``bands`` is an
    exact no-op). Reuses the EXACT ``build_game_plans`` seam (hard rule 8) with the
    perturbed config so the challenger is byte-consistent with the production
    structural path save for the deliberate perturbation.

    Fail-closed: a game that will not RE-INVERT under the perturbed config (the shift
    pushed a marginal out of the model's feasible region) is left to the copula for
    the challenger too — never silently dropped from the tail (the copula still
    samples it; it just loses the structural coupling in the challenger run, which
    can only widen or leave the tail, never narrow it below production, because the
    production ES is folded in via the governing max regardless)."""
    if not bands.active:
        return None
    ch_cfg = _challenger_structural_cfg(structural_cfg, bands)
    shocked = _shock_marginals(model, bands.marginal_shock)
    tickers = [""] * len(model.legs)
    for ticker, i in model.leg_index.items():
        tickers[i] = ticker
    events = [model.event_by_index.get(i) for i in range(len(model.legs))]
    if shocked is not None:
        marginals: list[float | None] = [shocked.get(i) for i in range(len(model.legs))]
    else:
        marginals = [leg.p for leg in model.legs]
    plans, copula_idx = build_game_plans(tickers, events, marginals, ch_cfg)
    if not plans:
        return None  # nothing re-inverts under the perturbed config ⇒ no challenger

    def _sampler(
        leg_models: Sequence[LegModel],
        c: NDArray[np.float64],
        n_draw: int,
        r: np.random.Generator,
    ) -> NDArray[np.float64]:
        return sample_structural_values(plans, copula_idx, leg_models, c, n_draw, r)

    return _SamplerBundle(
        _sampler,
        structural=True,
        bridge_needed=_bridge_needed(model, plans, copula_idx),
    )


def compute_book_risk(
    model: BookModel,
    *,
    n_samples: int = 100_000,
    seed: int = 0,
    band: str = "high",
    bankroll_cc: int | None = None,
    ruin_fractions: tuple[float, ...] = (0.10, 0.25, 0.60),
    challenger_inflation: float = DEFAULT_CHALLENGER_INFLATION,
    structural_cfg: StructuralConfigView | None = None,
    structural_challenger: bool = False,
    structural_challenger_bands: StructuralChallengerBands | None = None,
    current_equity_cc: int | None = None,
    ruin_floor_frac: float = 0.70,
    ruin_prob_ci_z: float = 0.0,
    input_generation: int = -1,
) -> BookRiskSnapshot:
    """Run the full book-risk MC and build the halt-feeding snapshot.

    Gates at the ``band`` correlation matrix ("high" = conservative under
    correlation uncertainty). The operative ES is the max of the production-copula
    ES (at ``band``), the correlation-inflated challenger ES, and the exact
    deterministic all-hit stress. ``ruin_fractions`` × ``bankroll_cc`` set the
    P(loss > threshold) thresholds (skipped when no bankroll).

    UNKNOWN model or empty book → a no-go snapshot (``unknown``/no positions), no
    usable stats (fail-closed, hard rule 6).

    P0-4: ``model.reserved_loss_cc`` is the exact premium of CONSERVATIVELY-
    RESERVED holdings (gated-off positions with no sampleable marginals). It is a
    DETERMINISTIC reserve added OUTSIDE the model ES — folded into the
    deterministic all-hit stress and hence the operative ES — so a reserved
    holding's whole-account risk is always represented in the gating tail number,
    even when the sampled sub-book is empty. A book that is ALL reserved (no
    risk-modeled position) is therefore still USABLE: it has a real deterministic
    reserve to gate on, not a no-go.

    P0-2: ``input_generation`` is the ``ExposureBook.generation`` the caller read
    the positions at; it is stamped verbatim into every returned snapshot so the
    async publisher can discard a result computed against a book that has since been
    mutated by a fill or settlement. Defaults to -1 (un-stamped) for direct/test
    callers; -1 never equals a live book generation, so an un-stamped snapshot fails
    a generation-match check closed."""
    n_positions = len(model.positions)
    reserve = max(0.0, float(model.reserved_loss_cc))
    if model.unknown or (n_positions == 0 and reserve <= 0.0):
        return BookRiskSnapshot(
            unknown=model.unknown,
            band=band,
            n_samples=n_samples,
            seed=seed,
            n_positions=n_positions,
            input_generation=input_generation,
        )
    if n_positions == 0:
        # ALL-RESERVED book: no sampled positions, but a real deterministic reserve
        # (P0-4). The reserve is the entire DETERMINISTIC maximum (outside model
        # ES) so the deterministic-max cap sees the held risk; the sampled model-ES
        # axis stays zero (nothing sampled ⇒ no model tail).
        return BookRiskSnapshot(
            unknown=False,
            band=band,
            n_samples=n_samples,
            seed=seed,
            n_positions=0,
            input_generation=input_generation,
            deterministic_max_loss_cc=reserve,
            # A reserve has no leg structure to net — the mutex-aware bound IS
            # the comonotone reserve (equality when no structure, by contract).
            mutex_aware_det_max_cc=reserve,
        )

    corr = model.corr_for_band(band)
    bundle = _select_sampler(model, structural_cfg)
    _sampler = bundle.sampler
    # THREE INDEPENDENT, reproducible RNG substreams (production + challenger +
    # P0-7 full-copula bridge) via SeedSequence.spawn — never `seed`/`seed+1`,
    # which are correlated streams (M2 §4.3). All derive deterministically from the
    # single ``seed``. The third substream is consumed only when the bridge runs;
    # spawning it unconditionally keeps the production/challenger streams identical
    # whether or not the bridge fires (no determinism drift on the common path).
    # P1.9: a FOURTH substream for the structural-parameter challenger, spawned
    # unconditionally (consumed only when that challenger runs) so the production/
    # correlation-challenger/bridge streams are byte-identical whether or not the
    # structural challenger is enabled — enabling it never perturbs the other books.
    # P0-7 PREFERRED: a FIFTH substream for the independent-split GUARD — the
    # unconditioned structural split, folded into the governing max ONLY when the
    # production sample is conditioned, so the conditioned production tail can never
    # be reported BELOW the independent split (the conditioning may only make the
    # modeled tail fatter or equal, never thinner — spec P0-7). Spawned uncondition-
    # ally so the other four streams are byte-identical whether or not it is consumed.
    seq_prod, seq_chal, seq_bridge, seq_struct, seq_split = (
        np.random.SeedSequence(seed).spawn(5)
    )
    rng = np.random.default_rng(seq_prod)
    values = _sampler(model.legs, corr, n_samples, rng)

    # Book P&L per scenario (float cc) + engine-consistent stats.
    loss_thresholds_cc = (
        tuple(int(f * bankroll_cc) for f in ruin_fractions)
        if bankroll_cc is not None and bankroll_cc > 0
        else ()
    )
    book = _book_pnl_from_values(values, model.positions)
    ev = float(book.mean())
    std = float(book.std(ddof=1)) if book.size > 1 else 0.0
    ev_stderr = std / math.sqrt(book.size) if book.size > 0 else 0.0
    p_profit = float(np.mean(book > 0.0))
    var_99, es_99 = _es_from_pnl(book, HEADLINE_LEVEL)
    p_loss_worse_than = {
        float(t): float(np.mean(book < -float(t))) for t in loss_thresholds_cc
    }
    # A2 P(RUIN): P(current_equity + wave P&L < ruin floor). Uses live equity so it
    # tightens as we draw down (a fixed loss-threshold would understate ruin once
    # equity < bankroll). Reflects the structural hedge (same sampled ``book``).
    # P1-1: computed on the PRODUCTION book here, then max'd with the challenger and
    # bridge P(ruin) below — gate on the WORST credible model, mirroring the
    # governing ES (a single correlation error must not under-state ruin either).
    ruin_floor_cc: float | None = None
    if (
        current_equity_cc is not None
        and bankroll_cc is not None
        and bankroll_cc > 0
    ):
        ruin_floor_cc = ruin_floor_frac * bankroll_cc
    p_ruin = _p_ruin_from_pnl(book, current_equity_cc, ruin_floor_cc)

    # Tail attribution on the 0.99 tail set (same cut es_99 uses).
    cut = float(np.quantile(book, 1.0 - HEADLINE_LEVEL))
    tail_mask = book <= cut
    per_game_tail, per_leg_tail = _tail_attribution(values, model, tail_mask)

    # --- challenger: correlation-inflated re-sample (anti-monoculture) --------
    # P0-8: inflate ONLY same-game pairs; cross-game independence is preserved
    # (universal positive correlation is not conservative for a hedged book).
    challenger_corr = _inflate_corr(
        corr, challenger_inflation, _same_game_mask(model)
    )
    rng_c = np.random.default_rng(seq_chal)  # spawned substream (M2 §4.3)
    values_c = _sampler(model.legs, challenger_corr, n_samples, rng_c)
    book_c = _book_pnl_from_values(values_c, model.positions)
    _, challenger_es = _es_from_pnl(book_c, HEADLINE_LEVEL)
    # P1-1: challenger P(ruin) on the SAME equity/floor. The correlation-inflated
    # book breaks more shared games together, so its ruin probability is the
    # anti-monoculture check on the ruin axis (folded into the governing max below).
    challenger_p_ruin = _p_ruin_from_pnl(book_c, current_equity_cc, ruin_floor_cc)

    # --- P0-7: same-game dependence bridge (full-copula challenger) ------------
    # When the structural split straddles a game (a structural scoreline leg AND a
    # copula-only corners/cards leg on the SAME game), the split samples those two
    # blocks from SEPARATE rng calls and discards their same-game cross-block
    # dependence. Re-sample the WHOLE book full-copula (every same-game pair coupled
    # through the block correlation, at the challenger-inflated matrix) and gate on
    # the WORSE tail — the interim bridge (we do NOT claim exact all-leg hedging).
    # The plain copula path already couples every same-game pair, so no bridge is
    # needed there.
    bridge_es = 0.0
    bridge_p_ruin = 0.0
    bridge_active = bundle.bridge_needed
    if bridge_active:
        rng_b = np.random.default_rng(seq_bridge)  # spawned substream (M2 §4.3)
        values_b = sample_leg_values(model.legs, challenger_corr, n_samples, rng_b)
        book_b = _book_pnl_from_values(values_b, model.positions)
        _, bridge_es = _es_from_pnl(book_b, HEADLINE_LEVEL)
        # P1-1: bridge P(ruin) too (full-copula same-game dependence), folded into
        # the governing max — the ruin axis gates on the worse of the three books.
        bridge_p_ruin = _p_ruin_from_pnl(book_b, current_equity_cc, ruin_floor_cc)

    # --- P0-7 PREFERRED: independent-split GUARD ------------------------------
    # When the production sample is CONDITIONED (a straddling copula leg loaded onto
    # its game's shared structural factor), also sample the UNCONDITIONED split and
    # fold its ES / P(ruin) into the governing max. This enforces the spec invariant
    # that the conditioning may only make the modeled tail FATTER or equal, never
    # thinner: even a (hedging) negative-covariance case cannot report a governing
    # tail below the independent split. A no-op when conditioning is off.
    split_es = 0.0
    split_p_ruin = 0.0
    if bundle.conditioned and bundle.split_sampler is not None:
        rng_sp = np.random.default_rng(seq_split)  # spawned substream (M2 §4.3)
        values_sp = bundle.split_sampler(model.legs, corr, n_samples, rng_sp)
        book_sp = _book_pnl_from_values(values_sp, model.positions)
        _, split_es = _es_from_pnl(book_sp, HEADLINE_LEVEL)
        split_p_ruin = _p_ruin_from_pnl(book_sp, current_equity_cc, ruin_floor_cc)

    # --- P1.9: structural-parameter challenger (anti-monoculture on INPUTS) ----
    # Re-invert + re-sample the structural games under a conservatively-perturbed
    # StructuralConfigView (goal rates via the re-fit, DC rho, ET/shootout/half-share
    # settlement constants, knockout mutex-metadata, and shocked feed marginals) and
    # fold its tail into the governing max exactly as the correlation and bridge
    # challengers do — gate on the WORSE tail. Runs ONLY when the caller opts in
    # (``structural_challenger`` + a structural cfg with a game that re-inverts under
    # the perturbed config); otherwise it is an exact no-op and the numbers below are
    # bit-identical to before (safety default: it can only WIDEN the tail).
    struct_es = 0.0
    struct_p_ruin = 0.0
    if structural_challenger and structural_cfg is not None:
        bands = (
            structural_challenger_bands
            if structural_challenger_bands is not None
            else DEFAULT_STRUCTURAL_CHALLENGER_BANDS
        )
        struct_bundle = _structural_challenger_bundle(model, structural_cfg, bands)
        if struct_bundle is not None:
            rng_s = np.random.default_rng(seq_struct)  # spawned substream (M2 §4.3)
            # Sample the perturbed structural book at the SAME band correlation the
            # production book used (the structural axis is what is being stressed,
            # not the copula correlation — that is the OTHER challenger's job).
            values_s = struct_bundle.sampler(model.legs, corr, n_samples, rng_s)
            book_s = _book_pnl_from_values(values_s, model.positions)
            _, struct_es = _es_from_pnl(book_s, HEADLINE_LEVEL)
            struct_p_ruin = _p_ruin_from_pnl(book_s, current_equity_cc, ruin_floor_cc)

    # --- deterministic stress: exact all-hit worst case -----------------------
    # P0-4: add the CONSERVATIVELY-RESERVED holdings' exact premium as a
    # deterministic reserve OUTSIDE model ES. The sampled ES/challenger cover only
    # the risk-modeled sub-book; the reserved holdings (unavailable marginals, not
    # sampled) add their full premium to the all-hit worst case, so their
    # whole-account risk is never hidden from the operative tail number.
    deterministic_max = _deterministic_all_hit_loss_cc(model) + reserve

    # Mutex/scenario-aware deterministic bound (2026-07-18): same counted
    # losses, co-aggregated soundly — within-game exclusive branches max, across
    # games sum, comonotone for every unproven slice. The comonotone number
    # above keeps emitting unchanged; the det-max caps read THIS field when
    # armed. Computed here (off the hot path) so the snapshot is the quote-time
    # cache — recomputed only on a book change via the generation stamp. Any
    # failure falls back to the comonotone number (fail closed, never open).
    try:
        marg_map = {t: model.legs[i].p for t, i in model.leg_index.items()}
        mutex_det = min(
            deterministic_max,
            mutex_aware_det_max_from_units(
                _det_units_from_model(model),
                reserved_loss_cc=reserve,
                marginals=marg_map.get,
                structural_cfg=structural_cfg,
            ),
        )
    except Exception:
        mutex_det = deterministic_max

    # P0-3: the governing MODEL tail is the worst SAMPLED CVaR across scenarios —
    # NOT maxed with the deterministic maximum. The deterministic maximum is a
    # separate axis (deterministic_max_loss_cc), gated independently, so it can no
    # longer dominate and silence the sampled ES. P0-7: the full-copula bridge ES
    # (present only when a game straddles both blocks) joins the max — gate on the
    # worse of the structural-split and full-copula tails. P1.9: the
    # structural-parameter challenger ES (present only when it ran) joins the max
    # too — the model tail gates on the worst credible structural INPUT regime.
    # P0-7 PREFERRED: the independent-split guard ES (present only when the production
    # sample is conditioned) also joins the max, so the conditioned tail is never
    # reported below the independent split (conditioning may only fatten, never thin).
    governing_model_es = max(es_99, challenger_es, bridge_es, struct_es, split_es)

    # P1-1: gate ruin on the WORST credible model (production vs challenger vs
    # bridge vs P1.9 structural challenger vs P0-7 independent-split guard), exactly
    # as the ES axis does. ``p_ruin`` is the production value above; the reported/
    # gated number is the max so a single correlation OR structural-input error
    # cannot understate ruin (fail-closed).
    p_ruin = max(
        p_ruin, challenger_p_ruin, bridge_p_ruin, struct_p_ruin, split_p_ruin
    )
    # P1-2: the fail-closed UPPER confidence bound on the governing p̂. All three
    # books were sampled at ``n_samples``; that is the n of the interval. z == 0
    # (the default) leaves it == p_ruin, so the committed-book behaviour is
    # unchanged unless an operator opts into a positive ruin confidence level.
    p_ruin_upper = wilson_upper_bound(p_ruin, n_samples, ruin_prob_ci_z)

    return BookRiskSnapshot(
        unknown=False,
        band=band,
        n_samples=n_samples,
        seed=seed,
        n_positions=n_positions,
        input_generation=input_generation,
        ev_cc=ev,
        ev_stderr_cc=ev_stderr,
        std_cc=std,
        p_profit=p_profit,
        var_99_cc=var_99,
        es_99_cc=es_99,
        p_loss_worse_than=p_loss_worse_than,
        p_ruin=p_ruin,
        p_ruin_upper=p_ruin_upper,
        production_es_99_cc=es_99,
        challenger_es_99_cc=challenger_es,
        bridge_es_99_cc=bridge_es,
        bridge_active=bridge_active,
        governing_model_es_99_cc=governing_model_es,
        deterministic_max_loss_cc=deterministic_max,
        mutex_aware_det_max_cc=mutex_det,
        per_game_tail_cc=per_game_tail,
        per_leg_tail_cc=per_leg_tail,
    )


def _book_pnl_from_values(
    values: NDArray[np.float64], positions: tuple[ComboPosition, ...]
) -> NDArray[np.float64]:
    """Whole-book P&L on sampled values (reuses the engine's public alias)."""
    from combomaker.sim.engine import book_pnl

    return book_pnl(values, list(positions))


def stats_to_snapshot_fields(stats: PortfolioStats) -> dict[str, float]:
    """Small adapter: pull the EV/std/p_profit off a ``PortfolioStats`` (for
    callers that already ran ``simulate`` and want the same field names). Kept
    tiny and pure; not on any hot path."""
    return {
        "ev_cc": stats.ev_cc,
        "std_cc": stats.std_cc,
        "p_profit": stats.p_profit,
    }


# ---------------------------------------------------------------------------
# P0-1: candidate- and reservation-aware portfolio risk (A2 last-look gate).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _TailAxes:
    """One book state's sampled tail (all float cc, positive loss magnitudes)."""

    ev_cc: float
    es_99_cc: float  # production-copula CVaR at ``band``
    challenger_es_99_cc: float
    governing_model_es_99_cc: float  # max(production, challenger)
    deterministic_max_loss_cc: float
    gross_settlement_notional_cc: float
    # P1-1: GOVERNING ruin = max over production / challenger / bridge (worst model).
    p_ruin: float
    # P1-2: one-sided Wilson UPPER confidence bound on ``p_ruin`` at the caller's
    # ``ruin_prob_ci_z`` (0 ⇒ == p_ruin). The ruin GATE reads this, not the point
    # estimate, so a p̂ that is only statistically-indistinguishable-from-safe near
    # the budget is declined (fail-closed against MC sampling error).
    p_ruin_upper: float = 0.0
    # P1 EV VISIBILITY (audit "+EV IS PRODUCTION-MODEL EV"): the mean book P&L under
    # each CHALLENGER book state, mirroring ``ev_cc`` (the production EV). ``ev_cc``
    # is the production-model EV the ADMISSION policy still gates on; these are the
    # SAME-book EV under the correlation-inflated challenger / full-copula bridge /
    # unconditioned-split re-samples, so the caller can DIFFERENCE post−pre per book
    # and see a candidate that is +EV under production yet −EV under a challenger.
    # None ⇒ that path did not run for this book (bridge/split are conditional), so
    # its candidate EV is undefined (never a convenient 0). ``challenger_ev_cc``
    # ALWAYS runs alongside ``ev_cc``, so it is a plain float.
    challenger_ev_cc: float = 0.0
    bridge_ev_cc: float | None = None
    split_ev_cc: float | None = None
    # 2026-07-18 verify fix: the UNCLAMPED governing expected tail loss — the
    # max over every model that RAN of ``-tail.mean()`` BEFORE the ``max(0,·)``
    # clamp. NEGATIVE ⇒ even the worst credible model's 1% tail is still
    # net-profitable (the value is minus the cushion). The certified-hedge gate
    # compares THIS pre vs post so its risk-reduction certification is never
    # vacuous on a profit-clamped book (clamped ES 0 <= 0 admitted everything
    # there); equal to ``governing_model_es_99_cc`` whenever the governing tail
    # is a genuine loss. NEVER used for the %-of-bankroll budgets — those keep
    # gating the clamped ES.
    governing_model_tail_loss_cc: float = 0.0
    # MUTEX/SCENARIO-AWARE deterministic maximum (2026-07-18): the sound
    # co-aggregation of the SAME counted losses (within-game exclusive branches
    # max, across games sum, comonotone for unproven slices) — see
    # ``mutex_aware_det_max_from_units``. None ⇒ not computed for this book
    # (the mutex-aware gate is off, or the det budget is not evaluated) ⇒ the
    # candidate gate falls back to the comonotone number (fail closed). Always
    # <= ``deterministic_max_loss_cc`` when present. Both ride the verdict so
    # live monitoring can compare the two bounds per decision.
    mutex_aware_det_max_cc: float | None = None


@dataclass(frozen=True, slots=True)
class CandidateBookRisk:
    """The candidate-aware portfolio-risk verdict for ONE contemplated fill (P0-1).

    ``BookRiskSnapshot`` prices COMMITTED positions only, so a concentrating
    candidate can pass on the safer old book and a balancing candidate earns no MC
    credit in its own decision. This evaluates the PRE book (committed + outstanding
    reservations + any simultaneously-executable accepts) and the POST book
    (PRE + this candidate) on the SAME sampled leg-value matrix — common random
    numbers — so the candidate's marginal effect on the joint tail, ruin, and EV is
    measured directly (the same shared games are broken in the same scenarios for
    both, so the difference is the candidate, not sampling noise). New games the
    candidate introduces enter the shared leg universe automatically.

    All money is float cc (simulator domain). ``unknown`` True ⇒ a missing marginal
    made the merged model no-go: NOTHING below is usable and ``confirm`` is forced
    False (fail-closed, hard rule 6). ``confirm`` is True ONLY when the candidate's
    EV is positive (or a negative-EV hedge is explicitly authorized within budget),
    the POST tail/ruin/deterministic/gross budgets all pass, and no fail-closed
    condition tripped. It is an ADVISORY tail verdict layered ON TOP of the
    analytic/gross/burst controls the lifecycle already enforces — never a loosening
    of them (safety default: this only ever DECLINES a fill the other gates admit)."""

    unknown: bool
    band: str
    n_samples: int
    seed: int
    n_pre_positions: int
    n_post_positions: int

    # PRE (committed + reservations + simultaneous accepts) and POST (+ candidate).
    pre: _TailAxes
    post: _TailAxes

    # The candidate's marginal EV = post.ev − pre.ev (float cc) under the PRODUCTION
    # model. POSITIVE ⇒ the fill is expected-profitable on the shared states. This is
    # the EV the ADMISSION policy gates on (production_candidate_ev > 0) — see the
    # audit "positive expected value under the production model".
    candidate_ev_cc: float

    # P1 EV VISIBILITY (audit "+EV IS PRODUCTION-MODEL EV, NOT ROBUST EV"): the SAME
    # candidate's marginal EV (post−pre) measured under each CHALLENGER book state
    # that ran, on COMMON random numbers. A candidate can be +EV under production yet
    # −EV under a challenger; these make that visible in the logs. The correlation-
    # inflated challenger ALWAYS runs (plain float); the full-copula bridge and the
    # unconditioned-split guard run CONDITIONALLY (None when that path did not run —
    # never a convenient 0). ``worst_credible_candidate_ev_cc`` is the MIN over the
    # production EV and every challenger EV that ran — the most adverse credible EV.
    challenger_candidate_ev_cc: float = 0.0
    bridge_candidate_ev_cc: float | None = None
    split_candidate_ev_cc: float | None = None
    worst_credible_candidate_ev_cc: float = 0.0

    # The final gate verdict + the first reason it was declined (empty ⇒ confirm).
    confirm: bool = False
    decline_reason: str = ""

    @property
    def usable(self) -> bool:
        return not self.unknown


def _tail_axes_from_pnl(
    pnl: NDArray[np.float64],
    deterministic_max_loss_cc: float,
    gross_cc: float,
    *,
    challenger_pnl: NDArray[np.float64] | None,
    current_equity_cc: int | None,
    ruin_floor_cc: float | None,
    bridge_pnl: NDArray[np.float64] | None = None,
    split_pnl: NDArray[np.float64] | None = None,
    ruin_prob_ci_z: float = 0.0,
    mutex_aware_det_max_cc: float | None = None,
) -> _TailAxes:
    """Roll a per-scenario book P&L vector (and its correlation-inflated
    challenger re-sample, plus the optional P0-7 full-copula bridge re-sample and
    the optional P0-7 PREFERRED unconditioned-split guard) into the separated tail
    axes (P0-3 separation preserved: the sampled model ES is NEVER max'd with the
    deterministic maximum).

    ``bridge_pnl`` (P0-7) is the full-copula same-game dependence-bridge re-sample,
    present only when the structural split straddles a game (a structural leg AND a
    copula leg on the SAME game, whose cross-block dependence the split discards).
    Its ES joins the governing max so the model tail gates on the WORSE of the
    structural-split and full-copula tails. None ⇒ no bridge (plain copula, or no
    straddling game) ⇒ it never enters the max.

    ``split_pnl`` (P0-7 PREFERRED) is the UNCONDITIONED structural-split re-sample,
    present only when the production ``pnl`` is CONDITIONED (a straddling copula leg
    loaded onto its game's shared factor). Its ES joins the governing max too, so the
    conditioned tail is never reported below the independent split (conditioning may
    only fatten, never thin). None ⇒ not conditioned ⇒ never enters the max."""
    ev = float(pnl.mean()) if pnl.size else 0.0
    _, es = _es_from_pnl(pnl, HEADLINE_LEVEL)
    # 2026-07-18 verify fix: the UNCLAMPED expected tail loss per model, folded
    # into a governing max over the models that actually RAN (a path that did
    # not run must not contribute its 0.0 — on a profit-clamped tail 0.0 would
    # spuriously dominate the negative cushion). Production always runs.
    governing_tail_loss = _tail_loss_from_pnl(pnl, HEADLINE_LEVEL)
    # P1 EV VISIBILITY: the SAME-book mean P&L under each challenger re-sample, so
    # the caller can difference post−pre per book and surface a candidate that is
    # +EV under production yet −EV under a challenger. A path that did not run leaves
    # its EV None (undefined, never a convenient 0); the challenger always runs.
    if challenger_pnl is not None and challenger_pnl.size:
        _, challenger_es = _es_from_pnl(challenger_pnl, HEADLINE_LEVEL)
        challenger_ev = float(challenger_pnl.mean())
        governing_tail_loss = max(
            governing_tail_loss, _tail_loss_from_pnl(challenger_pnl, HEADLINE_LEVEL)
        )
    else:
        challenger_es = 0.0
        challenger_ev = 0.0
    if bridge_pnl is not None and bridge_pnl.size:
        _, bridge_es = _es_from_pnl(bridge_pnl, HEADLINE_LEVEL)
        bridge_ev: float | None = float(bridge_pnl.mean())
        governing_tail_loss = max(
            governing_tail_loss, _tail_loss_from_pnl(bridge_pnl, HEADLINE_LEVEL)
        )
    else:
        bridge_es = 0.0
        bridge_ev = None
    if split_pnl is not None and split_pnl.size:
        _, split_es = _es_from_pnl(split_pnl, HEADLINE_LEVEL)
        split_ev: float | None = float(split_pnl.mean())
        governing_tail_loss = max(
            governing_tail_loss, _tail_loss_from_pnl(split_pnl, HEADLINE_LEVEL)
        )
    else:
        split_es = 0.0
        split_ev = None
    # P1-1: gate ruin on the WORST credible model — production vs the
    # correlation-inflated challenger vs the optional full-copula bridge — exactly
    # as the governing ES does. A single correlation error must not understate ruin.
    p_ruin = _p_ruin_from_pnl(pnl, current_equity_cc, ruin_floor_cc)
    if challenger_pnl is not None:
        p_ruin = max(
            p_ruin,
            _p_ruin_from_pnl(challenger_pnl, current_equity_cc, ruin_floor_cc),
        )
    if bridge_pnl is not None:
        p_ruin = max(
            p_ruin,
            _p_ruin_from_pnl(bridge_pnl, current_equity_cc, ruin_floor_cc),
        )
    if split_pnl is not None:
        # P0-7 PREFERRED: fold the unconditioned-split ruin so the conditioned tail
        # never reports a ruin below the independent split.
        p_ruin = max(
            p_ruin,
            _p_ruin_from_pnl(split_pnl, current_equity_cc, ruin_floor_cc),
        )
    # P1-2: the ruin gate reads the UPPER Wilson bound at the SAME n the governing
    # p̂ came from — the smallest scenario count across the sampled books (the
    # widest, most conservative interval), so a p̂ that only just clears the budget
    # by luck of the draw is treated as over-budget. n = 0 (nothing sampled)
    # reduces the bound to p̂ itself (the ruin cap does not evaluate then anyway).
    n_ruin = int(pnl.size)
    if challenger_pnl is not None and challenger_pnl.size:
        n_ruin = min(n_ruin, int(challenger_pnl.size)) if n_ruin else int(
            challenger_pnl.size
        )
    if bridge_pnl is not None and bridge_pnl.size:
        n_ruin = min(n_ruin, int(bridge_pnl.size)) if n_ruin else int(bridge_pnl.size)
    if split_pnl is not None and split_pnl.size:
        n_ruin = min(n_ruin, int(split_pnl.size)) if n_ruin else int(split_pnl.size)
    p_ruin_upper = wilson_upper_bound(p_ruin, n_ruin, ruin_prob_ci_z)
    return _TailAxes(
        ev_cc=ev,
        es_99_cc=es,
        challenger_es_99_cc=challenger_es,
        governing_model_es_99_cc=max(es, challenger_es, bridge_es, split_es),
        deterministic_max_loss_cc=deterministic_max_loss_cc,
        gross_settlement_notional_cc=gross_cc,
        p_ruin=p_ruin,
        p_ruin_upper=p_ruin_upper,
        challenger_ev_cc=challenger_ev,
        bridge_ev_cc=bridge_ev,
        split_ev_cc=split_ev,
        governing_model_tail_loss_cc=governing_tail_loss,
        mutex_aware_det_max_cc=mutex_aware_det_max_cc,
    )


def _reserved_loss_of(positions: Sequence[OpenPosition]) -> float:
    """Exact premium of the CONSERVATIVELY-RESERVED (unmodeled) holdings in a
    subset — a DETERMINISTIC reserve added OUTSIDE model ES (P0-4)."""
    return float(sum(p.max_loss_cc for p in positions if not p.risk_modeled))


def _det_and_gross(
    positions: Sequence[OpenPosition], combos: Sequence[ComboPosition]
) -> tuple[float, float]:
    """(deterministic all-hit max loss, gross settlement notional) for a subset,
    in float cc. Deterministic max = Σ (premium + fee) over sampled combos
    + reserved-holding premium (the exact comonotone all-hit worst case, P0-3/P0-4).
    Gross = Σ contracts×$1 over EVERY position (modeled AND reserved) — the
    utilization axis is size-based, so reserved holdings count too."""
    det = 0.0
    for combo in combos:
        det += float(combo.price_cc) * combo.contracts + float(combo.fee_cc)
    det += _reserved_loss_of(positions)
    gross = float(sum(p.gross_settlement_notional_cc for p in positions))
    return det, gross


# ---------------------------------------------------------------------------
# Mutex/scenario-aware deterministic maximum loss (operator directive
# 2026-07-18): variety must stop being taxed by a bound that pretends mutually
# exclusive losses co-occur.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DetMaxUnit:
    """One counted unit of the deterministic premium-at-risk fold.

    ``loss_cc`` is this unit's FULL contribution to the comonotone all-hit
    number, computed by the CALL SITE with its own arithmetic (float premium +
    fee for a sampled combo) so the aggregation changes only HOW counted losses
    co-aggregate, never WHAT counts. ``contracts_centi`` / ``entry_price_cc``
    feed the state-exact enumeration (``WorstCaseEntity``); ``legs`` carry the
    structure (market tickers, event tickers, selected sides) the mutex proof
    reads — NEVER leg-sign heuristics."""

    unit_id: str
    our_side: Side
    contracts_centi: int
    entry_price_cc: int
    legs: tuple[LegRef, ...]
    loss_cc: float
    risk_modeled: bool = True


def _det_units_from_positions(
    positions: Sequence[OpenPosition],
) -> tuple[list[DetMaxUnit], float]:
    """(risk-modeled units, reserved premium) for a position set, with per-unit
    ``loss_cc`` computed by the EXACT arithmetic ``_det_and_gross`` uses for the
    comonotone number (``float(price) * (centi/100)``, fee 0 — build_book_model
    sets every sampled combo's fee to 0), so no-netting parity is exact."""
    units: list[DetMaxUnit] = []
    reserved = 0.0
    for p in positions:
        if p.risk_modeled:
            units.append(
                DetMaxUnit(
                    unit_id=p.position_id,
                    our_side=p.our_side,
                    contracts_centi=int(p.contracts),
                    entry_price_cc=int(p.entry_price_cc),
                    legs=p.legs,
                    loss_cc=float(int(p.entry_price_cc)) * (int(p.contracts) / 100),
                )
            )
        else:
            reserved += float(p.max_loss_cc)
    return units, reserved


def _det_units_from_model(model: BookModel) -> list[DetMaxUnit]:
    """Units for the async snapshot path, reconstructed from the frozen
    ``BookModel`` (the workers never see the live ``ExposureBook``): per combo,
    legs are rebuilt from the shared leg universe (ticker + event + selected
    side) and ``loss_cc`` uses the EXACT ``_deterministic_all_hit_loss_cc``
    arithmetic (``float(price)·contracts + fee``). A combo whose legs cannot be
    reconstructed gets an EMPTY leg tuple ⇒ the fold routes it comonotone
    (fail-closed)."""
    ticker_of = {i: t for t, i in model.leg_index.items()}
    units: list[DetMaxUnit] = []
    for k, pos in enumerate(model.positions):
        sides = pos.leg_sides or tuple("yes" for _ in pos.leg_indices)
        legs: list[LegRef] = []
        for i, s in zip(pos.leg_indices, sides, strict=True):
            ticker = ticker_of.get(i)
            if ticker is None:
                legs = []
                break
            legs.append(
                LegRef(
                    market_ticker=ticker,
                    event_ticker=model.event_by_index.get(i),
                    side=s,
                )
            )
        units.append(
            DetMaxUnit(
                unit_id=f"model:{k}",
                our_side=Side.YES if pos.side == "yes" else Side.NO,
                contracts_centi=max(1, int(round(pos.contracts * 100))),
                entry_price_cc=int(pos.price_cc),
                legs=tuple(legs),
                loss_cc=float(pos.price_cc) * pos.contracts + float(pos.fee_cc),
            )
        )
    return units


def _single_game_of(unit: DetMaxUnit) -> str | None:
    """The ONE game every leg of this unit lives in (``pricing.grouping.
    game_key`` — alias-resolved, so a champion leg joins the final's game), or
    None when the unit is ungamed / spans games (⇒ comonotone residual: a
    multi-game parlay's exclusivity vs the rest of the book is not certified
    here — fail-closed, never a netting guess)."""
    from combomaker.pricing.grouping import game_key

    game: str | None = None
    for leg in unit.legs:
        if not leg.event_ticker:
            return None
        g = game_key(leg.event_ticker)
        if game is None:
            game = g
        elif g != game:
            return None
    return game


def mutex_aware_det_max_from_units(
    units: Sequence[DetMaxUnit],
    *,
    reserved_loss_cc: float = 0.0,
    marginals: MarginalProvider | None = None,
    structural_cfg: StructuralConfigView | None = None,
    is_me_event: Callable[[str], bool | None] | None = None,
) -> float:
    """The sound MUTEX/SCENARIO-AWARE deterministic maximum loss, float cc.

    AGGREGATION (per-scenario soundness is the invariant):
      * Each unit is assigned to exactly ONE bucket: its single game (every leg
        in one ``game_key`` game, long-NO, risk-modeled, known leg sides) or
        the COMONOTONE RESIDUAL (multi-game / ungamed / non-NO / reserved /
        unknown-side units, plus ``reserved_loss_cc``) — counted at full loss.
      * Within a game bucket the bound is the MIN over the sound per-bucket
        bounds that apply, each >= the largest single unit (a floor that keeps
        the fold monotone) and <= the bucket's comonotone sum:
          - STATE-EXACT: ``state_worst_case_by_game`` over the bucket's units
            mapped ``earns_credit=False`` — per enumerated DC-scoreline state
            (× shootout branch) a long-NO parlay contributes its FULL premium
            iff every structural leg can still hit (non-structural legs
            adversarial), else 0, NEVER a negative credit; the bound is the max
            state total. Clamped-at-0 keeps it MONOTONE in the unit set (unlike
            the waiver's signed netting, which is confirm-path-only), so this
            use does NOT violate that module's quote-time prohibition.
          - ME-BRANCH: the P0-9 single-explicit-ME-event max-over-branches fold
            (``exposure.mutex_scenario_bound``) on full float losses, when the
            caller supplies ``is_me_event`` metadata. Fails closed to the sum
            on 0 or >= 2 ME events.
          - COMONOTONE: always a candidate (the fail-closed slice bound).
      * TOTAL = Σ bucket bounds + residual. Across DIFFERENT games worst cases
        CAN co-occur (independent events) — summing is required, and each unit
        is counted in exactly one bucket, so the total never double-counts.

    SOUNDNESS: fix any realizable joint outcome. Within a game its outcome
    selects one enumerated state / one ME branch; every unit that LOSES in that
    outcome is counted in that state/branch's total (a long-NO parlay loses ⇒
    ALL its legs hit ⇒ its in-game legs hit in the realized state ⇒ counted;
    the ME fold's per-branch requirement is implied the same way, GIVEN the
    event's exclusivity — the same explicit-True metadata trust the Stage-B/
    P0-9 caps net on, audited by the P1-7 settlement tripwire). Residual units
    are counted at full loss unconditionally. Hence realized book loss <=
    Σ bucket bounds + residual. Every candidate bound <= its bucket's sum and
    the residual is exact, so the total is <= the comonotone number, with
    equality when no netting structure is proven (also clamped so ordering
    noise can never exceed it).

    Int/float seam: the state enumeration charges each unit its INT premium
    (``centi·price//100``); the per-unit non-negative float remainder
    (``loss_cc − int premium``, incl. any fee) is added back comonotone-style,
    so the bound never undercounts the float arithmetic the caps compare.

    FAIL-CLOSED: any exception anywhere returns the comonotone number (the
    LARGER bound — never fail open); a game with no buildable structural plan
    or an enumeration error is already comonotone per ``state_worst_case_by_
    game``'s own fail-closed contract. Certification is structural (game plans
    from real tickers/marginals) or explicit ME metadata — never leg-sign
    heuristics. Pure and deterministic; the caller caches it (the async
    snapshot is the quote-time cache, generation-stamped)."""
    comonotone = float(sum(u.loss_cc for u in units)) + max(0.0, reserved_loss_cc)
    try:
        bound = _mutex_aware_det_fold(
            units,
            reserved_loss_cc=max(0.0, reserved_loss_cc),
            marginals=marginals,
            structural_cfg=structural_cfg,
            is_me_event=is_me_event,
        )
    except Exception:
        return comonotone  # fail closed: the larger, comonotone bound
    return min(bound, comonotone)


def _mutex_aware_det_fold(
    units: Sequence[DetMaxUnit],
    *,
    reserved_loss_cc: float,
    marginals: MarginalProvider | None,
    structural_cfg: StructuralConfigView | None,
    is_me_event: Callable[[str], bool | None] | None,
) -> float:
    """The aggregation body (see ``mutex_aware_det_max_from_units``)."""
    residual = reserved_loss_cc
    buckets: dict[str, list[DetMaxUnit]] = {}
    for u in units:
        game: str | None = None
        if (
            u.risk_modeled
            and u.our_side is Side.NO
            and u.legs
            and all(leg.side in ("yes", "no") for leg in u.legs)
        ):
            game = _single_game_of(u)
        if game is None:
            residual += u.loss_cc
        else:
            buckets.setdefault(game, []).append(u)

    # Netting can only bite where >= 2 units share a game; singleton buckets are
    # exactly their unit's loss under every candidate bound, so the enumeration
    # is skipped for them (hot-path thrift, value-identical).
    multi = {g: us for g, us in buckets.items() if len(us) >= 2}
    state_bounds: dict[str, object] = {}
    if multi and structural_cfg is not None and marginals is not None:
        from combomaker.sim.state_worst_case import (
            WorstCaseEntity,
            state_worst_case_by_game,
        )

        entities = [
            WorstCaseEntity(
                entity_id=f"{g}:{u.unit_id}",
                our_side=u.our_side,
                contracts_centi=u.contracts_centi,
                entry_price_cc=u.entry_price_cc,
                legs=u.legs,
                fee_cc=0,
                risk_modeled=True,
                # The CLAMPED treatment: per state a unit contributes its full
                # hit loss or 0 — never a miss-side credit — which is what makes
                # this bound monotone and therefore safe OUTSIDE the confirm
                # path (the module's signed-netting prohibition targets credit).
                earns_credit=False,
            )
            for g, us in multi.items()
            for u in us
        ]
        marg_map: dict[str, float] = {}
        for us in multi.values():
            for u in us:
                for leg in u.legs:
                    if leg.market_ticker not in marg_map:
                        p = marginals(leg.market_ticker)
                        if p is not None:
                            marg_map[leg.market_ticker] = float(p)
        state_bounds = dict(
            state_worst_case_by_game(entities, (), marg_map, None, structural_cfg)
        )

    total = residual
    for game, bucket in buckets.items():
        como_g = float(sum(u.loss_cc for u in bucket))
        bound_g = como_g
        if game in multi:
            largest = max(u.loss_cc for u in bucket)
            sw = state_bounds.get(game)
            if sw is not None and getattr(sw, "certified", False):
                # Int state bound + the non-negative float remainders (never
                # undercount the float arithmetic), floored at the largest
                # single unit (monotone across the singleton fast path).
                frac = sum(
                    max(0.0, u.loss_cc - u.contracts_centi * u.entry_price_cc // 100)
                    for u in bucket
                )
                state_cand = max(
                    float(getattr(sw, "worst_case_cc", 0)) + frac, largest
                )
                bound_g = min(bound_g, state_cand)
            if is_me_event is not None:
                entries = [(u.legs, u.loss_cc, True) for u in bucket]
                # >= largest single entry by the fold's own contract.
                bound_g = min(bound_g, mutex_scenario_bound(entries, is_me_event))
        total += bound_g
    return total


def evaluate_candidate_book_risk(
    committed: Sequence[OpenPosition],
    candidate: OpenPosition,
    *,
    marginals: MarginalProvider,
    reservations: Sequence[OpenPosition] = (),
    simultaneous_accepts: Sequence[OpenPosition] = (),
    within_game_rho: WithinGameRhoProvider | None = None,
    structural_cfg: StructuralConfigView | None = None,
    n_samples: int = 20_000,
    seed: int = 0,
    band: str = "high",
    challenger_inflation: float = DEFAULT_CHALLENGER_INFLATION,
    bankroll_cc: int | None = None,
    current_equity_cc: int | None = None,
    ruin_floor_frac: float = 0.70,
    ruin_prob_ci_z: float = 0.0,
    portfolio_cvar_frac: float | None = None,
    portfolio_det_max_frac: float | None = None,
    portfolio_ruin_prob_budget: float | None = None,
    absolute_notional_multiple: int | None = None,
    hedge_cost_budget_cc: int = 0,
    allow_negative_ev_hedge: bool = False,
    worst_challenger_ev_tolerance: float = float("-inf"),
    det_max_mutex_aware: bool = True,
) -> CandidateBookRisk:
    """Candidate- and reservation-aware portfolio risk on COMMON sampled states.

    Builds ONE merged ``BookModel`` over the PRE book (``committed`` +
    ``reservations`` + ``simultaneous_accepts``) AND the ``candidate``, so every
    leg — including games the candidate INTRODUCES — enters a single shared leg
    universe and correlation matrix. It then samples that universe ONCE per band
    (production + a correlation-inflated challenger substream, both derived from
    ``seed`` via ``SeedSequence.spawn``) and scores the PRE and POST books on the
    SAME sampled matrix (common random numbers). The candidate's effect on EV, the
    sampled model ES, P(ruin), the deterministic all-hit maximum, and gross is
    therefore the pure marginal difference, not sampling noise — so a BALANCING
    candidate (one that hedges a shared game) earns real MC credit in its own
    decision, and a CONCENTRATING candidate is charged for the joint tail it adds
    on the SAFER old book it would otherwise pass against.

    Gate (``confirm``): True ONLY when
      * the candidate's marginal EV (``post.ev − pre.ev``) is POSITIVE — UNLESS a
        negative-EV HEDGE is explicitly authorized (``allow_negative_ev_hedge``)
        AND the candidate is CERTIFIED risk-reducing (2026-07-18: POST governing
        model UNCLAMPED expected tail loss <= PRE, measured on the SAME
        common-random-numbers sample — UNCLAMPED so the certification is never
        vacuous on a book whose sampled 1% tail is still net-profitable, where
        the clamped ES comparison degenerated to 0 <= 0 and admitted every
        pickoff) AND its EV cost stays within
        ``hedge_cost_budget_cc`` (default disabled: a negative-EV fill is
        DECLINED absent an explicit enabled budget, and even with one it is
        NEVER admitted unless it measurably shrinks the book's tail — arming is
        "pay up to $X of EV only for certified hedges", not a sniper-tax subsidy
        on stale quotes); and
      * every POST-book budget passes — the governing model ES_0.99, deterministic
        all-hit maximum, and P(ruin) under their %-of-bankroll / probability
        budgets, plus the gross utilization backstop.
    A missing marginal makes the merged model UNKNOWN ⇒ ``unknown=True`` and
    ``confirm=False`` (fail-closed, hard rule 6). Any budget whose fraction is not
    supplied (None) is simply not evaluated here — the lifecycle's ``LimitChecker``
    still enforces the full analytic/gross/burst control set; this is the ADDED
    joint-tail credit/charge, never a replacement for or loosening of those caps
    (safety default: it can only DECLINE a fill the other gates admit).

    Determinism: the same inputs + ``seed`` always yield the same verdict (auditable
    last-look). Money is float cc inside the simulator (hard rule 5)."""
    pre_positions: list[OpenPosition] = [
        *committed,
        *reservations,
        *simultaneous_accepts,
    ]
    all_positions: list[OpenPosition] = [*pre_positions, candidate]

    # ONE merged model: shared leg universe + correlation for PRE and POST, so the
    # SAME sampled matrix scores both (common random numbers). New candidate games
    # enter the universe here automatically.
    model = build_book_model(
        all_positions,
        marginals=marginals,
        within_game_rho=within_game_rho,
    )

    empty = _TailAxes(
        ev_cc=0.0,
        es_99_cc=0.0,
        challenger_es_99_cc=0.0,
        governing_model_es_99_cc=0.0,
        deterministic_max_loss_cc=0.0,
        gross_settlement_notional_cc=0.0,
        p_ruin=0.0,
    )
    if model.unknown:
        # Fail-closed: a missing marginal anywhere in the merged decomposition ⇒
        # no usable tail, no confirm (UNKNOWN joint tail is never safe).
        return CandidateBookRisk(
            unknown=True,
            band=band,
            n_samples=n_samples,
            seed=seed,
            n_pre_positions=len(pre_positions),
            n_post_positions=len(all_positions),
            pre=empty,
            post=empty,
            candidate_ev_cc=0.0,
            confirm=False,
            decline_reason="unknown_marginal",
        )

    # Split the risk-modeled combos into PRE and POST against the SHARED leg index
    # (position_to_combo maps each position onto the merged universe, so both lists
    # index the SAME sampled columns). Reserved (unmodeled) holdings are not sampled
    # — their premium rides in via _det_and_gross / gross below.
    leg_index = model.leg_index
    pre_combos = [
        position_to_combo(p, leg_index) for p in pre_positions if p.risk_modeled
    ]
    cand_combos = (
        [position_to_combo(candidate, leg_index)] if candidate.risk_modeled else []
    )
    post_combos = [*pre_combos, *cand_combos]

    ruin_floor_cc: float | None = None
    if bankroll_cc is not None and bankroll_cc > 0:
        ruin_floor_cc = ruin_floor_frac * bankroll_cc

    # Sample the shared universe ONCE per substream (production + challenger). When
    # the merged universe has no sampleable legs (e.g. an all-reserved book plus a
    # reserved candidate) there is nothing to sample: PRE/POST P&L are empty and the
    # tail axes fall back to their deterministic reserves only.
    pre_bridge_pnl: NDArray[np.float64] | None = None
    post_bridge_pnl: NDArray[np.float64] | None = None
    pre_split_pnl: NDArray[np.float64] | None = None
    post_split_pnl: NDArray[np.float64] | None = None
    if model.legs:
        corr = model.corr_for_band(band)
        # P0-8: same-game-only inflation; cross-game rho preserved.
        challenger_corr = _inflate_corr(
            corr, challenger_inflation, _same_game_mask(model)
        )
        bundle = _select_sampler(model, structural_cfg)
        sampler = bundle.sampler
        # FOUR substreams (production + challenger + P0-7 bridge + P0-7 PREFERRED
        # independent-split guard). All spawned unconditionally so the production/
        # challenger streams match whether or not the bridge/split fire (no
        # determinism drift); the bridge stream is consumed only when a game
        # straddles both blocks, the split stream only when the production sample is
        # conditioned.
        seq_prod, seq_chal, seq_bridge, seq_split = (
            np.random.SeedSequence(seed).spawn(4)
        )
        values = sampler(
            model.legs, corr, n_samples, np.random.default_rng(seq_prod)
        )
        values_c = sampler(
            model.legs, challenger_corr, n_samples, np.random.default_rng(seq_chal)
        )
        pre_pnl = book_pnl(values, pre_combos)
        post_pnl = book_pnl(values, post_combos)
        pre_pnl_c = book_pnl(values_c, pre_combos)
        post_pnl_c = book_pnl(values_c, post_combos)
        # P0-7: full-copula bridge (only when a game straddles both blocks). Scores
        # PRE and POST on the SAME full-copula matrix (common random numbers) so the
        # candidate's marginal effect on the bridge tail is measured directly; the
        # bridge ES then joins each book's governing max (gate on the worse tail).
        if bundle.bridge_needed:
            values_b = sample_leg_values(
                model.legs, challenger_corr, n_samples,
                np.random.default_rng(seq_bridge),
            )
            pre_bridge_pnl = book_pnl(values_b, pre_combos)
            post_bridge_pnl = book_pnl(values_b, post_combos)
        # P0-7 PREFERRED: unconditioned-split guard (only when the production sample
        # is conditioned). PRE and POST scored on the SAME split matrix (common
        # random numbers); the split ES joins each book's governing max so the
        # conditioned tail is never reported below the independent split.
        if bundle.conditioned and bundle.split_sampler is not None:
            values_sp = bundle.split_sampler(
                model.legs, corr, n_samples, np.random.default_rng(seq_split)
            )
            pre_split_pnl = book_pnl(values_sp, pre_combos)
            post_split_pnl = book_pnl(values_sp, post_combos)
    else:
        empty_pnl = np.zeros(0, dtype=np.float64)
        pre_pnl = post_pnl = pre_pnl_c = post_pnl_c = empty_pnl

    pre_det, pre_gross = _det_and_gross(pre_positions, pre_combos)
    post_det, post_gross = _det_and_gross(all_positions, post_combos)

    # MUTEX/SCENARIO-AWARE deterministic bound (2026-07-18): the SAME counted
    # premium-at-risk (committed + reservations + simultaneous accepts [+ the
    # candidate], reserved holdings comonotone) co-aggregated soundly — see
    # ``mutex_aware_det_max_from_units``. Computed only when the det budget will
    # actually gate (flag armed + fraction + bankroll supplied); otherwise both
    # axes carry None and the gate reads the comonotone number, byte-identical
    # to the pre-fix behaviour (``det_max_mutex_aware=False`` restores it
    # exactly). Reservations participate in branch netting deliberately: the
    # branch max never SUBTRACTS a loss (no hedge credit), and the fold is
    # monotone, so a released reservation only ever LOWERS the bound — the
    # waiver's credit-outlives-release hazard cannot arise. Any failure leaves
    # None (fail closed to comonotone, never open).
    pre_mutex: float | None = None
    post_mutex: float | None = None
    if (
        det_max_mutex_aware
        and portfolio_det_max_frac is not None
        and bankroll_cc is not None
        and bankroll_cc > 0
    ):
        try:
            pre_units, pre_reserved = _det_units_from_positions(pre_positions)
            post_units, post_reserved = _det_units_from_positions(all_positions)
            pre_mutex = min(
                pre_det,
                mutex_aware_det_max_from_units(
                    pre_units,
                    reserved_loss_cc=pre_reserved,
                    marginals=marginals,
                    structural_cfg=structural_cfg,
                ),
            )
            post_mutex = min(
                post_det,
                mutex_aware_det_max_from_units(
                    post_units,
                    reserved_loss_cc=post_reserved,
                    marginals=marginals,
                    structural_cfg=structural_cfg,
                ),
            )
        except Exception:
            pre_mutex = None
            post_mutex = None

    pre_axes = _tail_axes_from_pnl(
        pre_pnl,
        pre_det,
        pre_gross,
        challenger_pnl=pre_pnl_c,
        current_equity_cc=current_equity_cc,
        ruin_floor_cc=ruin_floor_cc,
        bridge_pnl=pre_bridge_pnl,
        split_pnl=pre_split_pnl,
        ruin_prob_ci_z=ruin_prob_ci_z,
        mutex_aware_det_max_cc=pre_mutex,
    )
    post_axes = _tail_axes_from_pnl(
        post_pnl,
        post_det,
        post_gross,
        challenger_pnl=post_pnl_c,
        current_equity_cc=current_equity_cc,
        ruin_floor_cc=ruin_floor_cc,
        bridge_pnl=post_bridge_pnl,
        split_pnl=post_split_pnl,
        ruin_prob_ci_z=ruin_prob_ci_z,
        mutex_aware_det_max_cc=post_mutex,
    )
    # PRODUCTION-model candidate EV — the number the admission policy gates on.
    candidate_ev = post_axes.ev_cc - pre_axes.ev_cc
    # P1 EV VISIBILITY: the SAME marginal EV under each challenger book that ran, on
    # common random numbers. The challenger always runs; the bridge/split are
    # conditional (None ⇒ that path did not run for this book). ``worst_credible`` is
    # the MIN over the production EV + every challenger EV that ran — the most adverse
    # credible EV. Only differences of EVs that BOTH ran are defined (post and pre
    # share the same book states / substreams, so a path that runs for post runs for
    # pre too); a None on either side leaves that challenger EV None.
    challenger_candidate_ev = post_axes.challenger_ev_cc - pre_axes.challenger_ev_cc
    bridge_candidate_ev: float | None = None
    if post_axes.bridge_ev_cc is not None and pre_axes.bridge_ev_cc is not None:
        bridge_candidate_ev = post_axes.bridge_ev_cc - pre_axes.bridge_ev_cc
    split_candidate_ev: float | None = None
    if post_axes.split_ev_cc is not None and pre_axes.split_ev_cc is not None:
        split_candidate_ev = post_axes.split_ev_cc - pre_axes.split_ev_cc
    worst_credible_candidate_ev = min(
        ev
        for ev in (
            candidate_ev,
            challenger_candidate_ev,
            bridge_candidate_ev,
            split_candidate_ev,
        )
        if ev is not None
    )

    confirm, reason = _candidate_gate(
        candidate_ev=candidate_ev,
        worst_credible_candidate_ev=worst_credible_candidate_ev,
        worst_challenger_ev_tolerance=worst_challenger_ev_tolerance,
        pre=pre_axes,
        post=post_axes,
        bankroll_cc=bankroll_cc,
        portfolio_cvar_frac=portfolio_cvar_frac,
        portfolio_det_max_frac=portfolio_det_max_frac,
        portfolio_ruin_prob_budget=portfolio_ruin_prob_budget,
        absolute_notional_multiple=absolute_notional_multiple,
        hedge_cost_budget_cc=hedge_cost_budget_cc,
        allow_negative_ev_hedge=allow_negative_ev_hedge,
    )

    return CandidateBookRisk(
        unknown=False,
        band=band,
        n_samples=n_samples,
        seed=seed,
        n_pre_positions=len(pre_positions),
        n_post_positions=len(all_positions),
        pre=pre_axes,
        post=post_axes,
        candidate_ev_cc=candidate_ev,
        challenger_candidate_ev_cc=challenger_candidate_ev,
        bridge_candidate_ev_cc=bridge_candidate_ev,
        split_candidate_ev_cc=split_candidate_ev,
        worst_credible_candidate_ev_cc=worst_credible_candidate_ev,
        confirm=confirm,
        decline_reason=reason,
    )


def _candidate_gate(
    *,
    candidate_ev: float,
    worst_credible_candidate_ev: float,
    worst_challenger_ev_tolerance: float,
    pre: _TailAxes,
    post: _TailAxes,
    bankroll_cc: int | None,
    portfolio_cvar_frac: float | None,
    portfolio_det_max_frac: float | None,
    portfolio_ruin_prob_budget: float | None,
    absolute_notional_multiple: int | None,
    hedge_cost_budget_cc: int,
    allow_negative_ev_hedge: bool,
) -> tuple[bool, str]:
    """The confirm/decline decision from the candidate EV + PRE/POST tail axes.

    Order (first failing reason wins): EV sign (with the CERTIFIED-HEDGE
    exception), the OPTIONAL worst-challenger-EV tolerance, then each supplied POST
    budget. Returns ``(confirm, reason)``; ``reason`` is "" iff confirmed. Any budget
    whose fraction is None is skipped — the lifecycle's LimitChecker still enforces
    the full control set; this is the ADDED joint-tail gate, never a demotion of
    those caps."""
    # (1) EV sign — the PRODUCTION-model admission policy. A negative-EV fill is
    # DECLINED unless it is an explicitly authorized CERTIFIED HEDGE (2026-07-18):
    # the budget must be enabled, the candidate must MEASURABLY SHRINK the book's
    # tail — POST governing model UNCLAMPED expected tail loss <= PRE, both
    # scored on the SAME common-random-numbers sample so the comparison is the
    # candidate's true marginal effect, not MC noise — and its EV cost must fit
    # the enabled budget. Without the certification, arming the budget would pay
    # the sniper tax on EVERY stale quote (any negative-EV pickoff within budget
    # was admitted). A positive-EV candidate passes this gate untouched.
    #
    # WHY THE UNCLAMPED TAIL (2026-07-18 verify fix): the clamped governing
    # ES_0.99 is exactly 0.0 on any book whose worst-1% sampled outcome is
    # still net-profitable — a fresh book after a settlement-day reset, or any
    # small early book of +EV fills — so a clamped-ES comparison passed 0 <= 0
    # for EVERY candidate there, including fills that hedge nothing: the armed
    # budget would have paid the sniper tax on every stale-quote pickoff in
    # exactly that regime. The unclamped tail loss (negative = the tail profit
    # cushion) makes eroding the cushion count against the candidate; it equals
    # the clamped ES whenever the governing tail is a genuine loss, so the
    # certification is unchanged in the loss regime and strictly TIGHTER
    # (decline-only) in the profit-clamped one — and a genuine hedge that
    # GROWS the tail cushion still certifies there.
    #
    # NOTE (deliberate deviation from the 2026-07-18 spec, flagged for review):
    # the spec also asked for "post det-max <= pre det-max", but on a sell-only
    # book that comparison is PROVABLY DEGENERATE — the deterministic all-hit
    # maximum is comonotone-ADDITIVE by design (P0-3: it never nets mutually
    # exclusive parlays), so post det-max == pre det-max + candidate premium +
    # fee on EVERY real fill, strictly larger; requiring it would make the
    # exception dead code. Det-max protection instead stays where it already is:
    # budget (3) below still gates POST det-max against its ABSOLUTE
    # %-of-bankroll ceiling, so a certified hedge that would push the all-hit
    # maximum over the det budget still declines there.
    if candidate_ev <= 0.0:
        if not allow_negative_ev_hedge:
            return False, "negative_ev_no_hedge_budget"
        if post.governing_model_tail_loss_cc > pre.governing_model_tail_loss_cc:
            return False, "negative_ev_not_risk_reducing"
        # The hedge's cost is the EV we give up = −candidate_ev (a positive $).
        if -candidate_ev > float(hedge_cost_budget_cc):
            return False, "negative_ev_exceeds_hedge_budget"

    # (1b) OPTIONAL worst-challenger-EV tolerance (audit "+EV IS PRODUCTION-MODEL EV").
    # The admission policy above stays production-model-EV based; this ONLY ADDS a
    # decline: a candidate that is +EV under production yet whose WORST credible
    # challenger EV falls below the operator's tolerance is declined. The tolerance
    # DEFAULTS to −inf, so ``worst >= −inf`` is always True and NO behaviour changes
    # unless the operator opts a finite (negative) tolerance in. Strictly additive —
    # it can only flip an already-admitted confirm to a decline, never the reverse.
    if worst_credible_candidate_ev < worst_challenger_ev_tolerance:
        return False, "worst_challenger_ev_below_tolerance"

    # (2) POST governing model ES_0.99 vs the %-of-bankroll CVaR ceiling.
    if (
        portfolio_cvar_frac is not None
        and bankroll_cc is not None
        and bankroll_cc > 0
    ):
        cvar_thr = portfolio_cvar_frac * bankroll_cc
        if post.governing_model_es_99_cc > cvar_thr:
            return False, "post_governing_model_es_over_budget"

    # (3) POST deterministic maximum vs its INDEPENDENT %-of-bankroll ceiling
    # (P0-3: gated separately from the sampled ES). MUTEX-AWARE (2026-07-18):
    # when the evaluator computed the scenario-aware bound the gate reads IT —
    # mutually exclusive parlays (opposing moneylines of one game, two champion
    # outcomes) can no longer be charged as if they all hit simultaneously, so
    # diversifying flow stops being taxed. None (flag off / budget not armed /
    # any failure) ⇒ the comonotone number gates, byte-identical to pre-fix
    # (fail closed: comonotone is the LARGER bound). Both numbers ride the
    # verdict's ``post`` axes for decline logging/monitoring; the decline
    # reason string is unchanged.
    if (
        portfolio_det_max_frac is not None
        and bankroll_cc is not None
        and bankroll_cc > 0
    ):
        det_thr = portfolio_det_max_frac * bankroll_cc
        post_det_gate = post.deterministic_max_loss_cc
        if post.mutex_aware_det_max_cc is not None:
            post_det_gate = min(post_det_gate, post.mutex_aware_det_max_cc)
        if post_det_gate > det_thr:
            return False, "post_deterministic_max_over_budget"

    # (4) POST P(ruin) vs the probability budget (reflects the same-game hedge —
    # a balancing candidate LOWERS it and can pass). P1-2: gate the UPPER Wilson
    # confidence bound (== p_ruin when ruin_prob_ci_z == 0), so a p̂ that is only
    # statistically-indistinguishable-from-safe near the budget is declined
    # (fail-closed against MC sampling error, never a convenient point estimate).
    if portfolio_ruin_prob_budget is not None:
        if max(post.p_ruin, post.p_ruin_upper) > portfolio_ruin_prob_budget:
            return False, "post_ruin_prob_over_budget"

    # (5) POST gross utilization backstop (Σ contracts×$1 ≤ multiple×bankroll).
    if (
        absolute_notional_multiple is not None
        and bankroll_cc is not None
        and bankroll_cc > 0
    ):
        backstop = absolute_notional_multiple * bankroll_cc
        if post.gross_settlement_notional_cc > backstop:
            return False, "post_gross_over_backstop"

    return True, ""
