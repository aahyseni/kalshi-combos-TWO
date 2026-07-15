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

from combomaker.risk.exposure import MarginalProvider, OpenPosition
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
    # bankroll unavailable (the ruin cap then does not evaluate). Computed on the
    # SAME sampled book P&L, so it reflects the structural hedge (not a comonotone).
    p_ruin: float = 0.0

    # --- P0-3 separated tail axes (§5) ---------------------------------------
    # SAMPLED model tail, by scenario, and their governing max. These reflect the
    # structural/same-game hedge — a balancing fill can lower them.
    production_es_99_cc: float = 0.0  # production-copula CVaR (mirror of es_99_cc)
    challenger_es_99_cc: float = 0.0  # correlation-inflated challenger CVaR
    governing_model_es_99_cc: float = 0.0  # max(production, challenger) — the model gate
    # DETERMINISTIC maximum loss: exact all-hit premium-at-risk (+ reserved
    # holdings). A hard upper bound the sampled ES can never exceed — gated as its
    # OWN axis (premium-at-risk cap), never maxed into the ES number.
    deterministic_max_loss_cc: float = 0.0

    # Tail attribution (§4.4).
    per_game_tail_cc: tuple[TailContribution, ...] = ()
    per_leg_tail_cc: tuple[TailContribution, ...] = ()

    @property
    def usable(self) -> bool:
        """True iff the stats may drive a gate/halt (not UNKNOWN, has positions)."""
        return (not self.unknown) and self.n_positions > 0


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


def _inflate_corr(
    corr: NDArray[np.float64], inflation: float
) -> NDArray[np.float64]:
    """Push every off-diagonal correlation toward +1 by ``inflation`` fraction
    (the challenger's over-correlation). ``rho' = rho + inflation·(1 − rho)`` for
    the off-diagonal; the diagonal stays 1. Repaired to PSD by the engine's
    Cholesky-with-jitter at sample time (cross-game 0s keep it near-PSD, and
    pushing toward +1 only fattens the joint tail — the conservative direction)."""
    if not 0.0 <= inflation <= 1.0:
        raise ValueError(f"inflation must be in [0,1], got {inflation}")
    n = corr.shape[0]
    out = corr + inflation * (1.0 - corr)
    # Restore the exact diagonal (rho=1 → 1 + inflation·0 = 1 already, but guard
    # float noise) so the matrix is a valid correlation matrix.
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


def _select_sampler(
    model: BookModel, structural_cfg: StructuralConfigView | None
) -> _Sampler:
    """The value sampler for this model (A1 structural seam).

    With a ``structural_cfg`` the games Dixon-Coles can invert are sampled from the
    joint scoreline (every same-game hedge/exclusion exact, no rho) and only the
    copula legs (corners/cards/other sports) use the Gaussian copula; without it
    the whole book is copula-sampled (byte-identical to before). Extracted verbatim
    from ``compute_book_risk`` so the candidate-aware evaluator reuses the EXACT
    same seam (hard rule 8) rather than reimplementing the dispatch."""
    if structural_cfg is None:
        return sample_leg_values
    tickers = [""] * len(model.legs)
    for ticker, i in model.leg_index.items():
        tickers[i] = ticker
    events = [model.event_by_index.get(i) for i in range(len(model.legs))]
    marginals = [leg.p for leg in model.legs]
    plans, copula_idx = build_game_plans(tickers, events, marginals, structural_cfg)

    def _structural_sampler(
        leg_models: Sequence[LegModel],
        c: NDArray[np.float64],
        n_draw: int,
        r: np.random.Generator,
    ) -> NDArray[np.float64]:
        return sample_structural_values(plans, copula_idx, leg_models, c, n_draw, r)

    return _structural_sampler


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
    current_equity_cc: int | None = None,
    ruin_floor_frac: float = 0.70,
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
        )

    corr = model.corr_for_band(band)
    _sampler = _select_sampler(model, structural_cfg)
    # Two INDEPENDENT, reproducible RNG substreams (production + challenger) via
    # SeedSequence.spawn — never `seed`/`seed+1`, which are correlated streams
    # (M2 §4.3). Both derive deterministically from the single ``seed``.
    seq_prod, seq_chal = np.random.SeedSequence(seed).spawn(2)
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
    p_ruin = 0.0
    if (
        current_equity_cc is not None
        and bankroll_cc is not None
        and bankroll_cc > 0
    ):
        floor_cc = ruin_floor_frac * bankroll_cc
        p_ruin = float(np.mean(current_equity_cc + book < floor_cc))

    # Tail attribution on the 0.99 tail set (same cut es_99 uses).
    cut = float(np.quantile(book, 1.0 - HEADLINE_LEVEL))
    tail_mask = book <= cut
    per_game_tail, per_leg_tail = _tail_attribution(values, model, tail_mask)

    # --- challenger: correlation-inflated re-sample (anti-monoculture) --------
    challenger_corr = _inflate_corr(corr, challenger_inflation)
    rng_c = np.random.default_rng(seq_chal)  # spawned substream (M2 §4.3)
    values_c = _sampler(model.legs, challenger_corr, n_samples, rng_c)
    book_c = _book_pnl_from_values(values_c, model.positions)
    _, challenger_es = _es_from_pnl(book_c, HEADLINE_LEVEL)

    # --- deterministic stress: exact all-hit worst case -----------------------
    # P0-4: add the CONSERVATIVELY-RESERVED holdings' exact premium as a
    # deterministic reserve OUTSIDE model ES. The sampled ES/challenger cover only
    # the risk-modeled sub-book; the reserved holdings (unavailable marginals, not
    # sampled) add their full premium to the all-hit worst case, so their
    # whole-account risk is never hidden from the operative tail number.
    deterministic_max = _deterministic_all_hit_loss_cc(model) + reserve

    # P0-3: the governing MODEL tail is the worst SAMPLED CVaR across scenarios —
    # NOT maxed with the deterministic maximum. The deterministic maximum is a
    # separate axis (deterministic_max_loss_cc), gated independently, so it can no
    # longer dominate and silence the sampled ES.
    governing_model_es = max(es_99, challenger_es)

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
        production_es_99_cc=es_99,
        challenger_es_99_cc=challenger_es,
        governing_model_es_99_cc=governing_model_es,
        deterministic_max_loss_cc=deterministic_max,
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
    p_ruin: float


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

    # The candidate's marginal EV = post.ev − pre.ev (float cc). POSITIVE ⇒ the
    # fill is expected-profitable on the shared states.
    candidate_ev_cc: float

    # The final gate verdict + the first reason it was declined (empty ⇒ confirm).
    confirm: bool
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
) -> _TailAxes:
    """Roll a per-scenario book P&L vector (and its correlation-inflated
    challenger re-sample) into the separated tail axes (P0-3 separation preserved:
    the sampled model ES is NEVER max'd with the deterministic maximum)."""
    ev = float(pnl.mean()) if pnl.size else 0.0
    _, es = _es_from_pnl(pnl, HEADLINE_LEVEL)
    if challenger_pnl is not None and challenger_pnl.size:
        _, challenger_es = _es_from_pnl(challenger_pnl, HEADLINE_LEVEL)
    else:
        challenger_es = 0.0
    p_ruin = 0.0
    if current_equity_cc is not None and ruin_floor_cc is not None and pnl.size:
        p_ruin = float(np.mean(current_equity_cc + pnl < ruin_floor_cc))
    return _TailAxes(
        ev_cc=ev,
        es_99_cc=es,
        challenger_es_99_cc=challenger_es,
        governing_model_es_99_cc=max(es, challenger_es),
        deterministic_max_loss_cc=deterministic_max_loss_cc,
        gross_settlement_notional_cc=gross_cc,
        p_ruin=p_ruin,
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
    portfolio_cvar_frac: float | None = None,
    portfolio_det_max_frac: float | None = None,
    portfolio_ruin_prob_budget: float | None = None,
    absolute_notional_multiple: int | None = None,
    hedge_cost_budget_cc: int = 0,
    allow_negative_ev_hedge: bool = False,
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
        AND its EV cost stays within ``hedge_cost_budget_cc`` (default disabled:
        a negative-EV hedge is DECLINED absent an explicit enabled budget); and
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

    empty = _TailAxes(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
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
    if model.legs:
        corr = model.corr_for_band(band)
        challenger_corr = _inflate_corr(corr, challenger_inflation)
        sampler = _select_sampler(model, structural_cfg)
        seq_prod, seq_chal = np.random.SeedSequence(seed).spawn(2)
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
    else:
        empty_pnl = np.zeros(0, dtype=np.float64)
        pre_pnl = post_pnl = pre_pnl_c = post_pnl_c = empty_pnl

    pre_det, pre_gross = _det_and_gross(pre_positions, pre_combos)
    post_det, post_gross = _det_and_gross(all_positions, post_combos)

    pre_axes = _tail_axes_from_pnl(
        pre_pnl,
        pre_det,
        pre_gross,
        challenger_pnl=pre_pnl_c,
        current_equity_cc=current_equity_cc,
        ruin_floor_cc=ruin_floor_cc,
    )
    post_axes = _tail_axes_from_pnl(
        post_pnl,
        post_det,
        post_gross,
        challenger_pnl=post_pnl_c,
        current_equity_cc=current_equity_cc,
        ruin_floor_cc=ruin_floor_cc,
    )
    candidate_ev = post_axes.ev_cc - pre_axes.ev_cc

    confirm, reason = _candidate_gate(
        candidate_ev=candidate_ev,
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
        confirm=confirm,
        decline_reason=reason,
    )


def _candidate_gate(
    *,
    candidate_ev: float,
    post: _TailAxes,
    bankroll_cc: int | None,
    portfolio_cvar_frac: float | None,
    portfolio_det_max_frac: float | None,
    portfolio_ruin_prob_budget: float | None,
    absolute_notional_multiple: int | None,
    hedge_cost_budget_cc: int,
    allow_negative_ev_hedge: bool,
) -> tuple[bool, str]:
    """The confirm/decline decision from the candidate EV + POST tail axes.

    Order (first failing reason wins): EV sign (with the explicit hedge-budget
    exception), then each supplied POST budget. Returns ``(confirm, reason)``;
    ``reason`` is "" iff confirmed. Any budget whose fraction is None is skipped —
    the lifecycle's LimitChecker still enforces the full control set; this is the
    ADDED joint-tail gate, never a demotion of those caps."""
    # (1) EV sign. A negative-EV fill is DECLINED unless it is an explicitly
    # authorized hedge whose EV cost stays within the enabled budget (default
    # disabled ⇒ no negative-EV hedges). A positive-EV candidate passes this gate.
    if candidate_ev <= 0.0:
        if not allow_negative_ev_hedge:
            return False, "negative_ev_no_hedge_budget"
        # The hedge's cost is the EV we give up = −candidate_ev (a positive $).
        if -candidate_ev > float(hedge_cost_budget_cc):
            return False, "negative_ev_exceeds_hedge_budget"

    # (2) POST governing model ES_0.99 vs the %-of-bankroll CVaR ceiling.
    if (
        portfolio_cvar_frac is not None
        and bankroll_cc is not None
        and bankroll_cc > 0
    ):
        cvar_thr = portfolio_cvar_frac * bankroll_cc
        if post.governing_model_es_99_cc > cvar_thr:
            return False, "post_governing_model_es_over_budget"

    # (3) POST deterministic all-hit maximum vs its INDEPENDENT %-of-bankroll
    # ceiling (P0-3: gated separately from the sampled ES).
    if (
        portfolio_det_max_frac is not None
        and bankroll_cc is not None
        and bankroll_cc > 0
    ):
        det_thr = portfolio_det_max_frac * bankroll_cc
        if post.deterministic_max_loss_cc > det_thr:
            return False, "post_deterministic_max_over_budget"

    # (4) POST P(ruin) vs the probability budget (reflects the same-game hedge —
    # a balancing candidate LOWERS it and can pass).
    if portfolio_ruin_prob_budget is not None:
        if post.p_ruin > portfolio_ruin_prob_budget:
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
