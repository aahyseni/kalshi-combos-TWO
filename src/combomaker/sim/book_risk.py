"""Full book-risk Monte Carlo + tail attribution + challenger/stress overlay
(RISK_BUILD_PLAN Phase 4 / research doc M1 §4, M2). Off the hot path.

Given a ``BookModel`` (sim/book_model.py — the pricer-consistent leg/corr/position
triple), this runs the portfolio MC and produces the five key risk outputs:

1. **P&L distribution** — EV ± MC standard error, std, P(profit).
2. **VaR / CVaR (tail loss)** at 0.95/0.99, reported on the TAIL-DEPENDENCE
   STRESS joint at the ``high`` band (``corr_tail_stress_high``: correlation
   uncertainty widens risk, never hides it, and a game moves as one in the corner)
   — **CVaR_0.99 is the headline book-risk number** and the one the halts/limits
   consume. The EV/P(book) LOCATION axis rides the OTHER joint
   (``corr_location_point``, the exact per-pair matrix the fills were priced on);
   the split is spelled out on ``compute_book_risk`` and in ``sim/book_model.py``.
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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from combomaker.core.conventions import Side
from combomaker.risk.cap_family import det_max_backstop_frac
from combomaker.risk.exposure import (
    LegRef,
    MarginalProvider,
    OpenPosition,
    mutex_scenario_bound,
)
from combomaker.sim.book_model import (
    BookModel,
    SettledFactProvider,
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

# JOINT NAMES (2026-07-26 axis split). Every snapshot and every book_risk_snapshot
# log line stamps which of the TWO joints on ``BookModel`` produced which axis, so
# the two can never be confused by a reader: the enforced gates ride the
# game-collapsed tail-dependence STRESS joint, the EV/P(book) axis rides the exact
# per-pair PRICING joint. ``NO_JOINT`` marks a snapshot that sampled nothing.
TAIL_STRESS_JOINT = "corr_tail_stress"
LOCATION_JOINT = "corr_location"
NO_JOINT = "none"



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
    TAIL/GATING stats were computed at ("high" for the gating number);
    ``location_band`` is the band the LOCATION stats (EV, P(book), p_night) were
    computed at — the PRICING joint, which is what the quotes were priced from
    (2026-07-26 band-mismatch fix; see the field comment below).

    Every snapshot ALSO stamps WHICH OF THE TWO JOINTS produced each axis —
    ``tail_joint`` and ``location_joint`` (see :class:`sim.book_model.BookModel`):
    ``corr_tail_stress`` is the game-collapsed tail-dependence stress joint that
    every enforced gate rides, ``corr_location`` is the exact per-pair pricing
    joint. A reader of one snapshot (or one log line) can therefore never mistake
    one axis' joint for the other's.

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

    # AXIS SPLIT (2026-07-26). ``band`` above is the ADVERSE band the TAIL axes
    # gate at (es_99 / challenger / governing / p_ruin / loss_quantiles / det-max /
    # tail attribution) — unchanged. ``location_band`` is the band the LOCATION
    # axes (ev_cc, ev_stderr_cc, std_cc, p_profit, p_night, p_loss_worse_than) are
    # sampled at: the PRICING joint ("point"), because those fills were PRICED on
    # the point joint. Publishing them off the corr-HIGH book marked every fill
    # sold at fair+markup NEGATIVE on arrival BY CONSTRUCTION (measured +5.78pp of
    # p_book and ≈+$33 of EV on the live book). This is a CORRECTNESS split, not a
    # policy knob — there is no flag to turn it off.
    location_band: str = "point"

    # WHICH JOINT PRODUCED WHICH AXIS (the other half of the split — the band alone
    # does not identify a joint, because the two joints DIFFER AT THE SAME BAND).
    #   tail_joint     — always ``corr_tail_stress`` for a sampled snapshot: the
    #                    game-collapsed stress joint every ENFORCED gate rides.
    #   location_joint — ``corr_location`` (the exact per-pair pricing joint) when
    #                    the location axis drew its own sample; ``corr_tail_stress``
    #                    when the two matrices were IDENTICAL and one shared CRN
    #                    draw served both (then the numbers literally came off the
    #                    stress sample, and saying otherwise would be a lie).
    #   Both are ``none`` on a snapshot that sampled nothing (UNKNOWN / empty /
    #   all-reserved) — it never advertises a joint it never used.
    tail_joint: str = TAIL_STRESS_JOINT
    location_joint: str = LOCATION_JOINT

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
    # LOSS-QUANTILE ENVELOPE (2026-07-25): 1001 evenly spaced LOSS quantiles,
    # elementwise worst over every model book that ran. The quote-time
    # tail-probability portfolio cap computes P(loss ≥ any live threshold)
    # from this; () on empty/legacy snapshots (the cap then falls back to the
    # ES form — never a free pass).
    loss_quantiles_cc: tuple[float, ...] = field(default_factory=tuple)
    # P(NIGHT) (2026-07-25 operator KPI): P(realized-so-far + open book > 0)
    # — does NOT reset as winners settle out. == p_profit when no realized
    # feed was supplied.
    p_night: float = 0.0
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
    # FIX 3 HEDGE ACCOUNTING (2026-07-28) — the det-max credit released by
    # positions that PROVABLY CANNOT BOTH LOSE (a complement/sub-parlay of a
    # combo we already hold), measured over units the fold charges at FULL
    # comonotone loss. ALWAYS computed (shadow visibility); SUBTRACTED from the
    # gated number only when ``RiskLimits.det_max_hedge_credit`` is ARMED, so
    # the default build is byte-identical. This is the RISK ACCOUNTING axis and
    # is distinct from the 2026-07-27 skew work, which changed the PRICE on
    # offsetting flow — see the FIX 3 block above ``_loss_literals``.
    det_max_hedge_credit_cc: float = 0.0
    # FIX 2 SETTLED-LEG RESOLUTION (2026-07-28) — the forward det-max the
    # EXCHANGE HAS ALREADY RETIRED: the summed premium of every held combo whose
    # graded legs prove it can no longer lose (a NO parlay with one leg settled
    # against the parlay is WON — our NO pays). ALWAYS measured, so an unarmed
    # bot still reports the headroom it is throwing away; only REMOVED from
    # ``deterministic_max_loss_cc`` / ``mutex_aware_det_max_cc`` when
    # ``det_max_settlement_aware`` is armed. Distinct from the hedge credit
    # above: that one nets two positions that cannot BOTH lose, this one retires
    # a position that can no longer lose AT ALL, on the exchange's own
    # determination rather than any model.
    det_max_settled_credit_cc: float = 0.0
    # FIX 5 (2026-07-28) — the BOOK LOSS at which this settlement wave reaches
    # the ruin floor: ``current_equity_cc − ruin_floor_cc``. ``p_ruin`` is
    # exactly P(loss > this), so carrying it lets a consumer that must charge the
    # snapshot for unmeasured book growth (``lifecycle._decay_book_risk``)
    # re-read the ruin probability at the SHIFTED threshold off the loss-quantile
    # envelope instead of having to GUESS where the threshold sits. Guessing is
    # not a small error: when p_ruin is 0 the envelope proves only that the
    # threshold lies somewhere ABOVE the largest sampled loss, so the tightest
    # admissible guess (== that largest loss) manufactured a 34.7% ruin
    # probability on a book that was nowhere near ruin. None ⇒ equity/bankroll
    # were unavailable, the ruin cap does not evaluate, and there is nothing to
    # shift.
    ruin_loss_threshold_cc: float | None = None

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


def position_settled_cannot_lose(
    pos: ComboPosition, settled: Mapping[int, float]
) -> bool:
    """FIX 2 (2026-07-28). True iff the EXCHANGE'S OWN determinations already
    prove this position CANNOT lose another cent — so its deterministic
    max-loss contribution is exactly 0.

    ``settled`` maps a latent leg index to that leg's graded value (0.0 or 1.0),
    and carries an entry ONLY for a leg the exchange has actually determined
    (``BookModel.settled_leg_values``; never the feed marginal — see
    ``book_model.SettledFactProvider``). An index that is ABSENT is UNKNOWN.

    The combo's payout is ``V = Π over legs of (value if leg_side=='yes' else
    1 − value)``; each factor is the leg's SELECTED-SIDE value. For the sell-only
    book every position is a long NO, which pays ``1 − V``:

      * LONG NO — we lose our premium only if the parlay HITS (``V = 1``), which
        requires EVERY leg's selected-side value to be 1. So a SINGLE leg the
        exchange has graded to selected-side 0 forces ``V = 0`` permanently: the
        parlay has MISSED, our NO is WON, and no further loss is possible. This
        is the operator's stated common case — a combo carrying legs from
        yesterday's slate plus one or two live legs today — and it resolves on
        the settled legs ALONE, with the live legs left completely unconstrained.
      * LONG YES — we lose if the parlay MISSES (``V = 0``), so we are safe only
        when EVERY leg is graded to selected-side 1. One unknown leg is enough to
        keep the full charge.

    FAIL CLOSED, both directions: an UNKNOWN leg can never make a NO position
    safe (we only ever act on a leg PROVEN to have gone against the parlay), and
    it always keeps a YES position charged. Nothing is inferred — a leg is
    resolved only when the exchange determination for it is in hand. Returning
    False is always the conservative answer, and it is what every unrecognised
    shape falls through to.

    Both live shapes (the MC's index-keyed ``ComboPosition`` and the candidate
    gate's ticker-keyed ``OpenPosition``) resolve through ``_settled_cannot_lose``
    below, so the two paths cannot drift apart."""
    sides = pos.leg_sides or ("yes",) * len(pos.leg_indices)
    return _settled_cannot_lose(
        pos.side == "no",
        (
            settled.get(idx)
            for idx in pos.leg_indices
        ),
        sides,
    )


def _settled_cannot_lose(
    our_side_is_no: bool,
    leg_values: Iterable[float | None],
    leg_sides: Iterable[str],
) -> bool:
    """THE ONE settlement-resolution rule (see ``position_settled_cannot_lose``
    for the full derivation). ``leg_values`` are the exchange-graded YES values
    aligned with ``leg_sides``; None means UNKNOWN.

    LONG NO  ⇒ won as soon as ONE leg is PROVEN to have broken the parlay.
    LONG YES ⇒ safe only when EVERY leg is proven to have gone the parlay's way.
    Both directions fail closed on an UNKNOWN leg."""
    if our_side_is_no:
        for value, leg_side in zip(leg_values, leg_sides, strict=False):
            if value is None:
                continue  # UNKNOWN — proves nothing either way
            selected = value if leg_side == "yes" else 1.0 - value
            if selected == 0.0:
                return True
        return False
    for value, leg_side in zip(leg_values, leg_sides, strict=False):
        if value is None:
            return False  # UNKNOWN ⇒ charged in full
        selected = value if leg_side == "yes" else 1.0 - value
        if selected != 1.0:
            return False
    return True


def open_position_settled_cannot_lose(
    position: OpenPosition, settled_facts: SettledFactProvider
) -> bool:
    """FIX 2, the CANDIDATE-GATE shape. Same rule as
    ``position_settled_cannot_lose``, resolved from leg TICKERS through the
    exchange-determination provider instead of the model's latent indices — the
    gate merges raw ``OpenPosition``s and never builds the index map.

    A value that is not exactly 0.0/1.0 is treated as UNKNOWN (a settlement fact
    is binary; anything else is not one), so it charges in full."""
    values: list[float | None] = []
    for leg in position.legs:
        fact = settled_facts(leg.market_ticker)
        values.append(float(fact) if fact == 0.0 or fact == 1.0 else None)
    return _settled_cannot_lose(
        position.our_side is Side.NO,
        values,
        (leg.side for leg in position.legs),
    )


def settled_det_max_credit_cc(model: BookModel) -> float:
    """FIX 2. The deterministic max-loss the EXCHANGE has already retired on this
    book: the summed premium (+ fee) of every position ``position_settled_cannot_
    lose`` proves is won, in float cc.

    This is the headroom the forward max-loss axis was charging for outcomes that
    are no longer possible. Always computable and always reported (the shadow
    number); whether it is SUBTRACTED from the enforced axis is the caller's
    arming decision. 0.0 on a model with no settled facts."""
    settled = model.settled_leg_values
    if not settled:
        return 0.0
    total = 0.0
    for pos in model.positions:
        if position_settled_cannot_lose(pos, settled):
            total += float(pos.price_cc) * pos.contracts + float(pos.fee_cc)
    return total


def _deterministic_all_hit_loss_cc(
    model: BookModel, *, settlement_aware: bool = False
) -> float:
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

    FIX 2 (2026-07-28) — ``settlement_aware``. "Unconditional" was the defect:
    the sum above charges a full forward max-loss for a combo the EXCHANGE HAS
    ALREADY DETERMINED cannot lose (measured on the live book 2026-07-28: 14 of
    77 open positions, all legs on games that finished the previous day, still
    carrying $80.20 of forward max-loss against $3.34–$4.60 of binding
    headroom). When True, a position ``position_settled_cannot_lose`` PROVES is
    won contributes 0; everything else — including every position with even one
    UNKNOWN or ungraded leg — is charged in full exactly as before. This is not
    a model estimate: it is the exchange's own determination, so the axis stays
    a hard upper bound. False (the default) reproduces the old sum bit for bit.

    Returned as a POSITIVE loss magnitude in float cc."""
    settled = model.settled_leg_values if settlement_aware else {}
    total = 0.0
    for pos in model.positions:
        if settled and position_settled_cannot_lose(pos, settled):
            continue  # exchange-DETERMINED win — no forward loss remains
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


def kill_tail_prob_upper(
    pnls: Sequence[NDArray[np.float64] | None],
    threshold_cc: float,
    n_samples: int,
    z: float,
) -> float:
    """GOVERNING P(book loss ≥ ``threshold_cc``) with a fail-closed Wilson
    upper bound (operator anchor ratified 2026-07-25: the TOTAL-book gate
    binds the PROBABILITY of a KILL-distance night, not the ES99 average —
    "more bets = more variance = more money"; diversification directly buys
    capacity while a one-way book stays hard-blocked).

    ``pnls`` are the per-model P&L vectors (production / challenger / bridge
    / split — None or empty entries skipped); the returned probability is the
    MAX over models (gate on the worst credible model, mirroring the
    governing ES), each wrapped in the one-sided Wilson upper bound at ``z``
    (0 ⇒ the point estimate) so a p̂ that only clears the budget by sampling
    luck is treated as over-budget. Empty books (no vectors) return 0.0 —
    the deterministic caps still bound them."""
    worst = 0.0
    for pnl in pnls:
        if pnl is None or pnl.size == 0:
            continue
        p_hat = float(np.mean(pnl <= -float(threshold_cc)))
        worst = max(worst, wilson_upper_bound(p_hat, n_samples, z))
    return worst


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
    axis). Clamped to (0,1) exclusive so inversion never sees a degenerate 0/1.

    SETTLED-LEG exception (2026-07-18): a DETERMINISTIC marginal (exactly
    0.0/1.0 — an exchange-GRADED settled leg) is a fact, not a feed mark: no
    feed error can apply to it, so it passes through UNSHOCKED (shocking would
    also un-degenerate it back into the structural inversion, re-treating a
    settled leg as probabilistic)."""
    if shock <= 0.0:
        return None
    out: dict[int, float] = {}
    for i, leg in enumerate(model.legs):
        p = float(leg.p)
        if not 0.0 < p < 1.0:
            out[i] = p
            continue
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
    realized_pnl_cc: int | None = None,
    det_max_settlement_aware: bool = False,
) -> BookRiskSnapshot:
    """Run the full book-risk MC and build the halt-feeding snapshot.

    Gates at the ``band`` correlation matrix ("high" = conservative under
    correlation uncertainty). The operative ES is the max of the production-copula
    ES (at ``band``), the correlation-inflated challenger ES, and the exact
    deterministic all-hit stress. ``ruin_fractions`` × ``bankroll_cc`` set the
    P(loss > threshold) thresholds (skipped when no bankroll).

    AXIS SPLIT (2026-07-26 correctness fix, not a policy knob — there is no flag).
    Two axes, two NAMED joints (see :class:`sim.book_model.BookModel`):
      * TAIL / GATING axis — ``var_99_cc``, ``es_99_cc``, ``challenger_es_99_cc``,
        ``bridge_es_99_cc``, ``governing_model_es_99_cc``, ``p_ruin`` (+ its Wilson
        bound), ``loss_quantiles_cc``, the deterministic maxima and the per-game /
        per-leg tail attribution — sampled on the TAIL-DEPENDENCE STRESS joint
        ``model.corr_tail_stress_for_band(band)`` (the game-wide collapse at the
        adverse band), EXACTLY as before this split existed. Every one of those
        fields is BIT-IDENTICAL to the pre-split build; the exact per-pair matrix
        is deliberately NOT used here, because a Gaussian copula at a low per-pair
        rho has almost no tail dependence while real games blow up together.
      * LOCATION axis — ``ev_cc``, ``ev_stderr_cc``, ``std_cc``, ``p_profit``
        (P(book)), ``p_night`` and the report-only ``p_loss_worse_than`` — sampled
        on the PRICING joint (``model.corr_location_point``, the exact per-pair
        point matrix the quotes were actually priced from). Marking those on the
        collapsed corr-HIGH book made every fill sold at fair+markup mark NEGATIVE
        on arrival BY CONSTRUCTION (worth +5.78pp of P(book) and ≈+$33 of EV on the
        live book).
    When the two MATRICES coincide (a book whose games each carry one uniform rho)
    ONE shared CRN sample serves both and the output is bit-identical to the
    pre-split behaviour; otherwise the location book is one extra draw on its own
    spawned substream (off the hot path — this function runs async/off-loop).
    ``tail_joint`` / ``location_joint`` on the returned snapshot stamp which joint
    actually produced which axis.

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
            # Nothing sampled ⇒ no location axis to split; stamp it == band and
            # both joints "none" so an empty/UNKNOWN snapshot never advertises a
            # joint it never used.
            location_band=band,
            tail_joint=NO_JOINT,
            location_joint=NO_JOINT,
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
            location_band=band,  # nothing sampled (all-reserved) — see above
            tail_joint=NO_JOINT,
            location_joint=NO_JOINT,
            n_samples=n_samples,
            seed=seed,
            n_positions=0,
            input_generation=input_generation,
            deterministic_max_loss_cc=reserve,
            # A reserve has no leg structure to net — the mutex-aware bound IS
            # the comonotone reserve (equality when no structure, by contract).
            mutex_aware_det_max_cc=reserve,
        )

    # --- AXIS SPLIT (2026-07-26): two joints, chosen BY NAME, never by band ----
    # ``corr`` is the TAIL-DEPENDENCE STRESS joint at the adverse band — the
    # game-wide collapse. EVERYTHING ENFORCED samples it, unchanged. ``corr_loc``
    # is the PRICING joint — the exact per-pair POINT matrix the quotes were
    # actually priced from. The LOCATION axes (EV, p_profit/P(book), p_night,
    # p_loss_worse_than) must ride ``corr_loc`` or a fill sold at fair+markup marks
    # NEGATIVE on arrival BY CONSTRUCTION (the whole book re-marked on a joint
    # nobody quoted). The tail must ride ``corr`` because a Gaussian copula at the
    # exact low per-pair rho has essentially NO tail dependence while real games
    # blow up together — see the construction site in ``sim/book_model.py``.
    corr = model.corr_tail_stress_for_band(band)
    corr_loc = model.corr_location_point
    # When the two joints are the SAME matrix (a book whose games each carry one
    # uniform rho — max == mean == min — so the collapse IS the per-pair matrix)
    # the two samples are the same sample: share it (one CRN draw, byte-identical
    # to the pre-split behaviour, and no extra MC cost). Band NAMES are never
    # compared here: at ``band="point"`` the stress joint is still the collapse,
    # so sharing on the name would have leaked the collapse into the location axis.
    share_location_sample = bool(np.array_equal(corr, corr_loc))
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
    # BAND-MISMATCH SPLIT: a SIXTH substream for the LOCATION (pricing-joint)
    # sample, spawned unconditionally and consumed only when the location band
    # differs from the gating band, so the five streams above stay byte-identical
    # whether or not the location re-sample runs (``SeedSequence.spawn(6)`` yields
    # the same first five children ``spawn(5)`` did).
    seq_prod, seq_chal, seq_bridge, seq_struct, seq_split, seq_loc = (
        np.random.SeedSequence(seed).spawn(6)
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
    # LOCATION book: the SAME positions re-scored on the PRICING joint (per-pair
    # point matrix). Shared with the gating book whenever the two matrices coincide
    # (no extra draw, bit-identical to pre-split); otherwise one extra sample on the
    # dedicated substream. Off the hot path (this whole function runs async /
    # off-loop), so the extra draw costs the MC worker, never the quote path.
    if share_location_sample:
        book_loc = book
    else:
        values_loc = _sampler(
            model.legs, corr_loc, n_samples, np.random.default_rng(seq_loc)
        )
        book_loc = _book_pnl_from_values(values_loc, model.positions)
        # Release the (n_samples x n_legs) value matrix at once — only the P&L
        # vector is used downstream, so peak memory stays where it was.
        del values_loc
    ev = float(book_loc.mean())
    std = float(book_loc.std(ddof=1)) if book_loc.size > 1 else 0.0
    ev_stderr = std / math.sqrt(book_loc.size) if book_loc.size > 0 else 0.0
    p_profit = float(np.mean(book_loc > 0.0))
    # P(NIGHT) (operator KPI 2026-07-25: "we just want the day to end
    # positive"): P(realized-so-far + open-book P&L > 0) on the production
    # book. Unlike ``p_profit`` (which RESETS as winning positions settle out
    # of the book — the 0.81→0.51 confusion), this number keeps the banked
    # edge: once realized profit exceeds the open book's plausible downside
    # it pins toward 1.0. None realized ⇒ equals p_profit. NOTE: the realized
    # feed resets at process start (restart-scoped) until the day-anchored
    # settlement-ledger reconstruction lands.
    p_night = (
        float(np.mean(book_loc + float(realized_pnl_cc) > 0.0))
        if realized_pnl_cc is not None and book_loc.size
        else p_profit
    )
    # TAIL axis — stays on the ADVERSE band book (correlation uncertainty widens
    # the tail; it must not relocate the mean, which is the location axis' job).
    var_99, es_99 = _es_from_pnl(book, HEADLINE_LEVEL)
    # REPORT-ONLY loss probabilities: location axis (the operator-facing view of the
    # book they actually priced). The GATING tail-probability object is
    # ``loss_quantiles_cc`` below, which stays on the adverse-band envelope —
    # risk/limits.py reads that, never this.
    p_loss_worse_than = {
        float(t): float(np.mean(book_loc < -float(t))) for t in loss_thresholds_cc
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
    # LOSS-QUANTILE ENVELOPE books (2026-07-25 tail-probability gate): every
    # model book that runs joins the envelope; branches below append theirs.
    envelope_books: list[NDArray[np.float64]] = [book, book_c]
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
        envelope_books.append(book_b)
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
        envelope_books.append(book_sp)
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
            envelope_books.append(book_s)
            _, struct_es = _es_from_pnl(book_s, HEADLINE_LEVEL)
            struct_p_ruin = _p_ruin_from_pnl(book_s, current_equity_cc, ruin_floor_cc)

    # --- deterministic stress: exact all-hit worst case -----------------------
    # P0-4: add the CONSERVATIVELY-RESERVED holdings' exact premium as a
    # deterministic reserve OUTSIDE model ES. The sampled ES/challenger cover only
    # the risk-modeled sub-book; the reserved holdings (unavailable marginals, not
    # sampled) add their full premium to the all-hit worst case, so their
    # whole-account risk is never hidden from the operative tail number.
    # FIX 2 (2026-07-28): the SETTLED-LEG credit — the forward max-loss the
    # exchange has already retired. ALWAYS measured (so the shadow readout is
    # honest on an unarmed bot); only SUBTRACTED from the enforced axes when
    # ``det_max_settlement_aware`` is armed. Unarmed ⇒ every number below is
    # byte-identical to before this existed.
    settled_credit = settled_det_max_credit_cc(model)
    deterministic_max = (
        _deterministic_all_hit_loss_cc(
            model, settlement_aware=det_max_settlement_aware
        )
        + reserve
    )

    # Mutex/scenario-aware deterministic bound (2026-07-18): same counted
    # losses, co-aggregated soundly — within-game exclusive branches max, across
    # games sum, comonotone for every unproven slice. The comonotone number
    # above keeps emitting unchanged; the det-max caps read THIS field when
    # armed. Computed here (off the hot path) so the snapshot is the quote-time
    # cache — recomputed only on a book change via the generation stamp. Any
    # failure falls back to the comonotone number (fail closed, never open).
    hedge_credit = 0.0
    try:
        marg_map = {t: model.legs[i].p for t, i in model.leg_index.items()}
        mutex_raw, hedge_credit = mutex_aware_det_max_and_credit(
            _det_units_from_model(
                model, settlement_aware=det_max_settlement_aware
            ),
            reserved_loss_cc=reserve,
            marginals=marg_map.get,
            structural_cfg=structural_cfg,
        )
        mutex_det = min(deterministic_max, mutex_raw)
        # The credit is measured only over FULL-charged units, so it stays
        # subtractable after the comonotone clamp; clamp it defensively anyway.
        hedge_credit = max(0.0, min(hedge_credit, mutex_det))
    except Exception:
        mutex_det = deterministic_max
        hedge_credit = 0.0

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

    # LOSS-QUANTILE ENVELOPE (2026-07-25 tail-probability book gate): 1001
    # evenly spaced quantiles of LOSS (−P&L), elementwise MAX over every model
    # book that ran (worst credible model per quantile — the same governing
    # discipline as the ES/ruin axes). The quote-time portfolio cap reads
    # P(loss ≥ live threshold) off this grid for ANY threshold (the bankroll
    # moves between snapshot and check), conservative by construction.
    # Sampled books only — conservatively-reserved holdings stay on the
    # deterministic axis, exactly as they do for the ES axes.
    loss_quantiles_cc: tuple[float, ...] = ()
    if book.size:
        q_grid = np.linspace(0.0, 1.0, 1001)
        envelope = np.maximum.reduce(
            [np.quantile(-b, q_grid) for b in envelope_books]
        )
        loss_quantiles_cc = tuple(float(x) for x in envelope)

    return BookRiskSnapshot(
        unknown=False,
        band=band,
        location_band=band if share_location_sample else "point",
        tail_joint=TAIL_STRESS_JOINT,
        location_joint=(
            TAIL_STRESS_JOINT if share_location_sample else LOCATION_JOINT
        ),
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
        loss_quantiles_cc=loss_quantiles_cc,
        p_night=p_night,
        p_ruin=p_ruin,
        p_ruin_upper=p_ruin_upper,
        production_es_99_cc=es_99,
        challenger_es_99_cc=challenger_es,
        bridge_es_99_cc=bridge_es,
        bridge_active=bridge_active,
        governing_model_es_99_cc=governing_model_es,
        deterministic_max_loss_cc=deterministic_max,
        mutex_aware_det_max_cc=mutex_det,
        det_max_hedge_credit_cc=hedge_credit,
        det_max_settled_credit_cc=settled_credit,
        # FIX 5: the loss level at which this wave hits the ruin floor, so a
        # growth-charged consumer can shift it exactly (None when the ruin cap
        # does not evaluate for want of an equity/bankroll reading).
        ruin_loss_threshold_cc=(
            None
            if current_equity_cc is None or ruin_floor_cc is None
            else float(current_equity_cc) - float(ruin_floor_cc)
        ),
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
    # P(book) — P(this book state's P&L > 0) under the PRODUCTION model
    # (2026-07-25 operator directive: P(book) must STEER the betting — more
    # variance/diversity = higher P(book)). Phase A is VISIBILITY: candidate
    # ΔP(book) = post − pre is logged at the gate, gated on by NOTHING yet.
    # 0.0 when nothing was sampled (empty/all-reserved book — honest unknown
    # in a shadow-only field).
    p_profit: float = 0.0
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
    # FIX 3 HEDGE ACCOUNTING (2026-07-28): the det-max credit released by
    # provably-cannot-both-lose positions on THIS book. Always measured, never
    # pre-subtracted — the candidate gate subtracts it only when
    # ``det_max_hedge_credit`` is armed (shadow default ⇒ byte-identical).
    det_max_hedge_credit_cc: float = 0.0


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

    # ΔP(book) (2026-07-25 operator directive — "P(book) should be steering
    # our betting"): the candidate's marginal effect on P(book P&L > 0) under
    # the production model, post − pre on COMMON random numbers. POSITIVE ⇒
    # the fill ADDS variance/diversity (a balancing/offsetting/variance bet);
    # NEGATIVE ⇒ it concentrates the book further one-way. Phase A: logged at
    # every gate verdict (visibility), gated on by NOTHING — the steering
    # mechanism (Phase B) derives from this measured signal.
    candidate_delta_p_book: float = 0.0

    # GATE EV SOURCE audit trail (2026-07-25 review): the EV that actually
    # judged admission and which fair produced it — "mc" (default),
    # "pricing_fair" (armed, fresh re-price succeeded), or "mc_fallback"
    # (armed but the fresh re-price failed/no-quoted ⇒ MC judged).
    admission_ev_cc: float = 0.0
    admission_ev_source: str = "mc"

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
    det_max_hedge_credit_cc: float = 0.0,
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
        p_profit=float(np.mean(pnl > 0.0)) if pnl.size else 0.0,
        p_ruin=p_ruin,
        p_ruin_upper=p_ruin_upper,
        challenger_ev_cc=challenger_ev,
        bridge_ev_cc=bridge_ev,
        split_ev_cc=split_ev,
        governing_model_tail_loss_cc=governing_tail_loss,
        mutex_aware_det_max_cc=mutex_aware_det_max_cc,
        det_max_hedge_credit_cc=det_max_hedge_credit_cc,
    )


def _reserved_loss_of(positions: Sequence[OpenPosition]) -> float:
    """Exact premium of the CONSERVATIVELY-RESERVED (unmodeled) holdings in a
    subset — a DETERMINISTIC reserve added OUTSIDE model ES (P0-4)."""
    return float(sum(p.max_loss_cc for p in positions if not p.risk_modeled))


def _det_and_gross(
    positions: Sequence[OpenPosition],
    combos: Sequence[ComboPosition],
    settled: Mapping[int, float] | None = None,
) -> tuple[float, float]:
    """(deterministic all-hit max loss, gross settlement notional) for a subset,
    in float cc. Deterministic max = Σ (premium + fee) over sampled combos
    + reserved-holding premium (the exact comonotone all-hit worst case, P0-3/P0-4).
    Gross = Σ contracts×$1 over EVERY position (modeled AND reserved) — the
    utilization axis is size-based, so reserved holdings count too.

    FIX 2 (2026-07-28): ``settled`` (the merged model's ``settled_leg_values``)
    retires a combo the exchange has already DETERMINED cannot lose. None/empty
    ⇒ every combo charged in full, byte-identical. The GROSS axis is deliberately
    untouched — utilization is about capital tied up, and a determined-but-unswept
    position still ties its collateral up until the exchange actually pays."""
    det = 0.0
    for combo in combos:
        if settled and position_settled_cannot_lose(combo, settled):
            continue  # exchange-DETERMINED win — no forward loss remains
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
    settled_facts: SettledFactProvider | None = None,
) -> tuple[list[DetMaxUnit], float]:
    """(risk-modeled units, reserved premium) for a position set, with per-unit
    ``loss_cc`` computed by the EXACT arithmetic ``_det_and_gross`` uses for the
    comonotone number (``float(price) * (centi/100)``, fee 0 — build_book_model
    sets every sampled combo's fee to 0), so no-netting parity is exact.

    FIX 2 (2026-07-28): with ``settled_facts``, a position the exchange has
    already determined cannot lose is OMITTED — the same drop-don't-zero rule as
    ``_det_units_from_model`` (the fold recharges INT premium from
    ``contracts_centi × entry_price_cc``, so a zero-``loss_cc`` unit would still
    be charged in every enumerated scenario). RESERVED holdings are never
    resolved: their legs are unpriceable by definition, so they stay charged in
    full (fail-closed)."""
    units: list[DetMaxUnit] = []
    reserved = 0.0
    for p in positions:
        if (
            settled_facts is not None
            and p.risk_modeled
            and open_position_settled_cannot_lose(p, settled_facts)
        ):
            continue  # exchange-DETERMINED win — in no realizable loss scenario
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


def _det_units_from_model(
    model: BookModel, *, settlement_aware: bool = False
) -> list[DetMaxUnit]:
    """Units for the async snapshot path, reconstructed from the frozen
    ``BookModel`` (the workers never see the live ``ExposureBook``): per combo,
    legs are rebuilt from the shared leg universe (ticker + event + selected
    side) and ``loss_cc`` uses the EXACT ``_deterministic_all_hit_loss_cc``
    arithmetic (``float(price)·contracts + fee``). A combo whose legs cannot be
    reconstructed gets an EMPTY leg tuple ⇒ the fold routes it comonotone
    (fail-closed).

    FIX 2 (2026-07-28): with ``settlement_aware``, a position the exchange has
    already determined cannot lose is OMITTED ENTIRELY rather than emitted with
    ``loss_cc = 0``. Dropping it is the only correct move — the mutex fold's
    state enumeration charges each unit its INT premium recomputed from
    ``contracts_centi × entry_price_cc``, NOT from ``loss_cc``, so a zero-loss
    unit left in the list would still be charged its full premium in every
    enumerated scenario and the credit would silently vanish on exactly the
    axis the det-max caps actually gate on. A won position participates in no
    scenario, so removing it changes no other unit's bound."""
    settled = model.settled_leg_values if settlement_aware else {}
    ticker_of = {i: t for t, i in model.leg_index.items()}
    units: list[DetMaxUnit] = []
    for k, pos in enumerate(model.positions):
        if settled and position_settled_cannot_lose(pos, settled):
            continue  # exchange-DETERMINED win — in no realizable loss scenario
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
    bound, _credit = mutex_aware_det_max_and_credit(
        units,
        reserved_loss_cc=reserved_loss_cc,
        marginals=marginals,
        structural_cfg=structural_cfg,
        is_me_event=is_me_event,
    )
    return bound


def mutex_aware_det_max_and_credit(
    units: Sequence[DetMaxUnit],
    *,
    reserved_loss_cc: float = 0.0,
    marginals: MarginalProvider | None = None,
    structural_cfg: StructuralConfigView | None = None,
    is_me_event: Callable[[str], bool | None] | None = None,
) -> tuple[float, float]:
    """``(mutex_aware_bound_cc, hedge_offset_credit_cc)`` — the bound above PLUS
    the separately-reported OFFSETTING-POSITION credit (FIX 3, 2026-07-28).

    The credit is the amount by which the bound may be reduced once positions
    that PROVABLY CANNOT BOTH LOSE stop being charged twice; it is returned
    alongside (never pre-subtracted) so the caller decides — SHADOW reports it,
    ARMED subtracts it. ``bound − credit`` is a sound upper bound on the book's
    realizable deterministic worst case (proof in ``_hedge_offset_credit_cc``);
    because ``credit`` is measured only over units the fold charges at FULL
    comonotone loss, it is equally subtractable from ``min(bound, comonotone)``.
    Any failure returns ``(comonotone, 0.0)`` — fail closed on BOTH axes."""
    comonotone = float(sum(u.loss_cc for u in units)) + max(0.0, reserved_loss_cc)
    try:
        bound, credit = _mutex_aware_det_fold(
            units,
            reserved_loss_cc=max(0.0, reserved_loss_cc),
            marginals=marginals,
            structural_cfg=structural_cfg,
            is_me_event=is_me_event,
        )
    except Exception:
        return comonotone, 0.0  # fail closed: larger bound, zero credit
    gated = min(bound, comonotone)
    # Never credit more than the bound itself (defensive; the matching
    # construction already guarantees credit <= bound − max single unit loss).
    return gated, max(0.0, min(credit, gated))


def _mutex_aware_det_fold(
    units: Sequence[DetMaxUnit],
    *,
    reserved_loss_cc: float,
    marginals: MarginalProvider | None,
    structural_cfg: StructuralConfigView | None,
    is_me_event: Callable[[str], bool | None] | None,
) -> tuple[float, float]:
    """The aggregation body (see ``mutex_aware_det_max_from_units``).

    Returns ``(total, hedge_offset_credit)``. The credit is computed but NEVER
    subtracted here — see ``mutex_aware_det_max_and_credit``."""
    residual = reserved_loss_cc
    buckets: dict[str, list[DetMaxUnit]] = {}
    # FIX 3 bookkeeping: units this fold charges at FULL comonotone loss. Only
    # these are eligible for the offsetting-position credit, so the credit can
    # never double-count a netting the state/ME folds already granted.
    residual_units: list[DetMaxUnit] = []
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
            residual_units.append(u)
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
    full_charged: list[DetMaxUnit] = list(residual_units)
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
        # A bucket that earned NO netting credit contributes exactly the sum of
        # its units' full losses, so those units are individually full-charged
        # and may take the offsetting credit. A bucket that DID net is skipped
        # entirely (we cannot attribute its bound per unit) — conservative.
        if bound_g >= como_g:
            full_charged.extend(bucket)
    return total, _hedge_offset_credit_cc(full_charged)


# ---------------------------------------------------------------------------
# FIX 3 — HEDGE ACCOUNTING (2026-07-28).
#
# WHAT THIS IS, AND WHAT IT IS NOT. The operator asked whether the 2026-07-27
# skew/widening work already covered this. IT DID NOT, and the distinction is
# recorded here on purpose:
#
#   * The SKEW work changed the PRICE we quote on offsetting flow (pricing/
#     skew.py — peak-concentration widening, anti-peak rebate). It decides what
#     a hedge COSTS the taker.
#   * THIS is the RISK ACCOUNTING. It decides what a hedge CONSUMES of our
#     deterministic max-loss budget. Before it, ``_mutex_aware_det_fold``
#     bucketed only long-NO units into game folds, so a COMPLEMENT position (the
#     opposite side of a combo we already hold) fell into the comonotone
#     residual and was charged its FULL premium ON TOP of the position it
#     offsets — two positions that cannot both lose, both charged. The measured
#     mutex credit on the live book was $30.77 = 3.79% of an $812.66 book.
#
# CERTIFICATION IS STATE ENUMERATION, NEVER A LEG-SIGN HEURISTIC. Each unit's
# LOSS CONDITION is a proposition over leg outcomes, read from the settlement
# rule alone (no correlation model, no game structure, no marginals):
#
#     long NO  on legs L  loses  <=>  every leg of L resolves to its selected
#                                     side   (the parlay HITS; NO pays nothing)
#     long YES on legs L  loses  <=>  some leg of L resolves against its
#                                     selected side   (the parlay MISSES)
#
# Two units are CERTIFIED EXCLUSIVE iff their loss conditions are jointly
# UNSATISFIABLE over the free assignment of every distinct market to {yes, no}.
# Treating distinct exchange markets as unconstrained free variables is the
# ADVERSARIAL direction: a real joint distribution can only ever be a SUBSET of
# those assignments, so an unsatisfiable pair here is unsatisfiable in reality.
# The enumeration is exact and closed-form for these two shapes:
#
#     NO / NO   exclusive iff some shared market is required on OPPOSITE sides
#     NO / YES  exclusive iff the YES unit's literals are a SUBSET of the NO
#               unit's literals (the NO unit's loss forces every one of them
#               true, so the YES unit cannot miss) — this is the same-combo
#               complement, and any sub-parlay of it
#     YES / YES never certified (two parlays can both miss)
#
# AGGREGATION IS A MATCHING, NOT A CLIQUE. Pairwise exclusivity does not imply
# joint exclusivity for three or more units, so we never take a max over a
# group. We take a set of DISJOINT certified pairs (each unit in at most one)
# and charge each pair ``max(loss_a, loss_b)`` instead of ``loss_a + loss_b``.
# SOUNDNESS: fix any realizable outcome; the set S of units that lose in it
# contains at most one member of each certified pair, so the realized loss is
# <= sum over pairs of max + sum of unpaired full losses = (full sum − credit).
# FAIL TOWARD THE LARGER WORST CASE: anything not certified — unknown leg side,
# a reserved holding, a self-contradictory leg set, an unrecognised side, an
# exception — is simply not paired and stays charged in full.
# ---------------------------------------------------------------------------


def _loss_literals(unit: DetMaxUnit) -> dict[str, str] | None:
    """The unit's loss condition as ``market_ticker -> required side``, or None
    when the unit is NOT certifiable (⇒ never paired, always charged in full).

    Rejects: reserved/unmodeled holdings, empty leg sets, any leg side outside
    ``{yes, no}``, and a leg set that names one market on BOTH sides (a
    self-contradictory combo — charging it in full is conservative and we
    refuse to reason about it)."""
    if not unit.risk_modeled or not unit.legs:
        return None
    if unit.our_side not in (Side.NO, Side.YES):
        return None
    lits: dict[str, str] = {}
    for leg in unit.legs:
        if leg.side not in ("yes", "no"):
            return None
        prev = lits.get(leg.market_ticker)
        if prev is not None and prev != leg.side:
            return None
        lits[leg.market_ticker] = leg.side
    return lits


def _certified_cannot_both_lose(
    side_a: Side,
    lits_a: dict[str, str],
    side_b: Side,
    lits_b: dict[str, str],
) -> bool:
    """True iff the two loss conditions are provably jointly unsatisfiable —
    the exact three-case enumeration documented in the FIX 3 block above."""
    if side_a is Side.NO and side_b is Side.NO:
        # Both need every literal true; impossible iff they disagree anywhere.
        return any(lits_b.get(m, s) != s for m, s in lits_a.items())
    if side_a is Side.NO and side_b is Side.YES:
        # b needs SOME literal false; a forces all of ITS literals true.
        return all(lits_a.get(m) == s for m, s in lits_b.items())
    if side_a is Side.YES and side_b is Side.NO:
        return all(lits_b.get(m) == s for m, s in lits_a.items())
    return False  # YES/YES: two parlays can both miss — never certified.


# Hard ceiling on candidate pairs EXAMINED by the offsetting-credit scan. Sized
# from the measured shape: the live 77-unit book examines a handful of pairs and
# a 308-unit stress (4x the book, every leg shared) stays under 3ms. Exhausting
# the budget only means FEWER pairs found ⇒ LESS credit ⇒ a larger charged
# number, so this can never become a soundness hole — it is a latency bound.
_HEDGE_PAIR_BUDGET = 200_000


def _hedge_offset_credit_cc(units: Sequence[DetMaxUnit]) -> float:
    """The det-max credit from disjoint CERTIFIED-EXCLUSIVE pairs among units
    that are all charged at FULL loss (float cc, >= 0).

    Greedy maximal matching over pairs ranked by the credit they release
    (``min(loss_a, loss_b)`` descending, unit_id-tie-broken for determinism).
    Greedy is not the optimal matching, but EVERY matching is sound — the
    credit is only ever an under-claim of the true offset, never an over-claim.

    Candidate pairs are drawn from an inverted market->unit index: certification
    in all three cases requires a SHARED market ticker, so units that share no
    market can never certify and are never compared (keeps the scan near-linear
    on a real book instead of O(n^2) over ~200 entities). A hard
    ``_HEDGE_PAIR_BUDGET`` on pairs EXAMINED bounds the worst case (a leg shared
    by very many units); exhausting it stops the scan, which can only find FEWER
    pairs and therefore claim LESS credit — conservative by construction, never
    a soundness hole."""
    lits_by_id: dict[str, dict[str, str]] = {}
    unit_by_id: dict[str, DetMaxUnit] = {}
    by_market: dict[str, list[str]] = {}
    for u in units:
        lits = _loss_literals(u)
        if lits is None:
            continue
        # Duplicate unit_ids would corrupt the matching's disjointness; a
        # collision means the caller built ambiguous units ⇒ refuse to pair.
        if u.unit_id in unit_by_id:
            return 0.0
        lits_by_id[u.unit_id] = lits
        unit_by_id[u.unit_id] = u
        for market in lits:
            by_market.setdefault(market, []).append(u.unit_id)

    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[float, str, str]] = []
    budget = _HEDGE_PAIR_BUDGET
    # Smallest market buckets first: a leg shared by very many units is the only
    # way to blow the budget, and spending it there would starve every cheap,
    # high-signal bucket. Ordering changes only WHICH pairs a truncated scan
    # sees, never soundness (any matching is sound).
    for ids in sorted(by_market.values(), key=len):
        for i, a_id in enumerate(ids):
            for b_id in ids[i + 1 :]:
                key = (a_id, b_id) if a_id < b_id else (b_id, a_id)
                if key in seen:
                    continue
                if budget <= 0:
                    break
                seen.add(key)
                budget -= 1
                a, b = unit_by_id[key[0]], unit_by_id[key[1]]
                if not _certified_cannot_both_lose(
                    a.our_side, lits_by_id[key[0]], b.our_side, lits_by_id[key[1]]
                ):
                    continue
                credit = min(a.loss_cc, b.loss_cc)
                if credit > 0.0:
                    pairs.append((credit, key[0], key[1]))
            if budget <= 0:
                break
        if budget <= 0:
            break

    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))
    used: set[str] = set()
    total = 0.0
    for credit, a_id, b_id in pairs:
        if a_id in used or b_id in used:
            continue
        used.add(a_id)
        used.add(b_id)
        total += credit
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
    hedge_budget_tail_derived: bool = False,
    tail_prob_gate: bool = False,
    kill_tail_prob: float = 0.02,
    # KILL-ANCHORED BOOK GATE (2026-07-29; demotion RATIFIED 2026-07-31) —
    # ONE arming flag, default SHADOW. ``hard_trip_frac`` is the ratified 12%
    # KILL line the tail-probability budget is measured AT. Armed (with the
    # tail-probability form governing), det-max is DEMOTED to the ruin-anchor
    # backstop ``cap_family.det_max_backstop_frac()`` — see
    # ``risk/limits.RiskLimits.kill_anchored_book_gate`` and the derivation on
    # that function.
    kill_anchored_book_gate: bool = False,
    hard_trip_frac: float | None = None,
    # MARGINAL KILL GATE (2026-08-01 sunk-book ruling): threaded from the SAME
    # ``RiskLimits.kill_gate_marginal`` the quote-time cap reads (one flag,
    # two sites, no divergence). Armed, an inherited over-budget PRE book no
    # longer level-refuses every candidate at the confirm gate — see
    # ``_candidate_gate``. Default off = byte-identical.
    kill_gate_marginal: bool = False,
    gate_ev_from_pricing_fair: bool = False,
    pricing_edge_cc: float | None = None,
    require_p_book_non_decreasing: bool = False,
    worst_challenger_ev_tolerance: float = float("-inf"),
    det_max_mutex_aware: bool = True,
    # FIX 2 (2026-07-28): the EXCHANGE-DETERMINATION provider + its arming bit.
    # Threaded so the CONFIRM-time gate resolves settled legs exactly as the
    # quote-time cap does; without it the gate would recharge $80.20 of already-
    # decided outcomes at the one moment the headroom is actually spent.
    settled_facts: SettledFactProvider | None = None,
    det_max_settlement_aware: bool = False,
    det_max_hedge_credit: bool = False,
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
        # FIX 2: only when ARMED — unarmed the merged model carries no settled
        # map at all, so every det-max path below is byte-identical.
        settled_facts=settled_facts if det_max_settlement_aware else None,
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

    def _modeled(p: OpenPosition) -> bool:
        # SAMPLED iff risk-modeled AND every leg entered the merged universe. A leg
        # with no marginal (in-play empty book / closed-ungraded) RESERVED the
        # position out in build_book_model — it is bounded deterministically, never
        # sampled — so its legs are absent from ``leg_index``.
        return p.risk_modeled and all(
            leg.market_ticker in leg_index for leg in p.legs
        )

    # The CANDIDATE itself must be priceable — we will NOT quote a combo whose own
    # leg has no marginal. A HELD position with such a leg only RESERVES (keeping the
    # book quoting); the CANDIDATE fail-closes. A reserved candidate is absent from
    # the leg universe, so projecting it would KeyError — decline first.
    if candidate.risk_modeled and not _modeled(candidate):
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

    pre_combos = [position_to_combo(p, leg_index) for p in pre_positions if _modeled(p)]
    cand_combos = (
        [position_to_combo(candidate, leg_index)] if _modeled(candidate) else []
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
        # THE QUOTE-TIME GATE IS 100% TAIL AXIS. Every number this evaluator
        # produces — PRE/POST ES, P(ruin), det-max, and the CRN candidate EV
        # DIFFERENCE that feeds the confirm decision — is a GATING number, so it
        # rides the TAIL-DEPENDENCE STRESS joint end to end. The exact per-pair
        # PRICING joint is deliberately never read here (2026-07-26 axis split):
        # loosening a gate is the one thing that split must not do.
        corr = model.corr_tail_stress_for_band(band)
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

    settled_map = model.settled_leg_values
    pre_det, pre_gross = _det_and_gross(pre_positions, pre_combos, settled_map)
    post_det, post_gross = _det_and_gross(all_positions, post_combos, settled_map)

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
    # FIX 3 (2026-07-28): the offsetting-position credit on each book, measured
    # alongside the bound and NEVER pre-subtracted. Shadow (the default) reports
    # it on the verdict's axes; ARMED, ``_candidate_gate`` subtracts the POST
    # credit before the det budget check. See the FIX 3 block in this module.
    pre_hedge_credit = 0.0
    post_hedge_credit = 0.0
    if (
        det_max_mutex_aware
        and portfolio_det_max_frac is not None
        and bankroll_cc is not None
        and bankroll_cc > 0
    ):
        try:
            gate_facts = settled_facts if det_max_settlement_aware else None
            pre_units, pre_reserved = _det_units_from_positions(
                pre_positions, gate_facts
            )
            post_units, post_reserved = _det_units_from_positions(
                all_positions, gate_facts
            )
            pre_raw, pre_hedge_credit = mutex_aware_det_max_and_credit(
                pre_units,
                reserved_loss_cc=pre_reserved,
                marginals=marginals,
                structural_cfg=structural_cfg,
            )
            post_raw, post_hedge_credit = mutex_aware_det_max_and_credit(
                post_units,
                reserved_loss_cc=post_reserved,
                marginals=marginals,
                structural_cfg=structural_cfg,
            )
            pre_mutex = min(pre_det, pre_raw)
            post_mutex = min(post_det, post_raw)
            pre_hedge_credit = max(0.0, min(pre_hedge_credit, pre_mutex))
            post_hedge_credit = max(0.0, min(post_hedge_credit, post_mutex))
        except Exception:
            pre_mutex = None
            post_mutex = None
            pre_hedge_credit = 0.0
            post_hedge_credit = 0.0

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
        det_max_hedge_credit_cc=pre_hedge_credit,
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
        det_max_hedge_credit_cc=post_hedge_credit,
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

    # GATE EV SOURCE (2026-07-25): choose the admission EV HERE so the result
    # records WHICH fair judged the fill (audit trail — the decline detail
    # and gate logs carry both the value and the source).
    if gate_ev_from_pricing_fair and pricing_edge_cc is not None:
        admission_ev = float(pricing_edge_cc)
        admission_ev_source = "pricing_fair"
    else:
        admission_ev = candidate_ev
        admission_ev_source = (
            "mc_fallback" if gate_ev_from_pricing_fair else "mc"
        )
    # P(BOOK) NON-DECREASE inputs (1c). ``delta_p_book`` is the measured
    # effect; ``ideal_delta_p_book`` is the INDEPENDENCE BENCHMARK (2026-07-25
    # v2 — the armed v1 absolute floor misfired on the live knife-edge book:
    # 9 refusals of ordinary +EV growth fills whose −0.01..−0.06 dents were
    # the small-book parity artifact, not concentration): the ΔP(book) an
    # independent bet of IDENTICAL per-scenario P&L would have produced,
    # estimated by shuffling the candidate's own CRN P&L vector against the
    # book's scenarios (seeded — deterministic). The gate refuses only fills
    # that do MEANINGFULLY WORSE than their independent twin — the definition
    # of correlation drag; size drag stays the size caps' job.
    delta_p_book = post_axes.p_profit - pre_axes.p_profit
    delta_p_book_se = 0.0
    ideal_delta_p_book = 0.0
    if post_pnl.size > 1 and pre_pnl.size == post_pnl.size:
        diff_ind = (post_pnl > 0.0).astype(np.float64) - (
            pre_pnl > 0.0
        ).astype(np.float64)
        delta_p_book_se = float(
            diff_ind.std(ddof=1) / math.sqrt(diff_ind.size)
        )
        cand_pnl = post_pnl - pre_pnl
        shuffle_rng = np.random.default_rng(
            np.random.SeedSequence(seed).spawn(6)[5]
        )
        shuffled = shuffle_rng.permutation(cand_pnl)
        ideal_delta_p_book = float(
            np.mean(pre_pnl + shuffled > 0.0)
        ) - pre_axes.p_profit
    confirm, reason = _candidate_gate(
        admission_ev=admission_ev,
        worst_credible_candidate_ev=worst_credible_candidate_ev,
        worst_challenger_ev_tolerance=worst_challenger_ev_tolerance,
        pre=pre_axes,
        post=post_axes,
        bankroll_cc=bankroll_cc,
        portfolio_cvar_frac=portfolio_cvar_frac,
        portfolio_det_max_frac=portfolio_det_max_frac,
        det_max_hedge_credit=det_max_hedge_credit,
        portfolio_ruin_prob_budget=portfolio_ruin_prob_budget,
        absolute_notional_multiple=absolute_notional_multiple,
        hedge_cost_budget_cc=hedge_cost_budget_cc,
        allow_negative_ev_hedge=allow_negative_ev_hedge,
        hedge_budget_tail_derived=hedge_budget_tail_derived,
        tail_prob_gate=tail_prob_gate,
        kill_tail_prob=kill_tail_prob,
        # KILL-ANCHORED BOOK GATE (2026-07-29) — the ratified anchors, threaded
        # so the CONFIRM-time gate and the quote-time cap re-anchor together
        # (one flag, two sites, no divergence).
        kill_anchored_book_gate=kill_anchored_book_gate,
        hard_trip_frac=hard_trip_frac,
        # MARGINAL KILL GATE (2026-08-01): the PRE-book vectors of the SAME
        # model streams, same order, same CRN sample — the regime probe
        # (is the inherited book already over budget?) and the marginal
        # charge both read post − pre on shared randomness.
        kill_gate_marginal=kill_gate_marginal,
        pre_pnls=(pre_pnl, pre_pnl_c, pre_bridge_pnl, pre_split_pnl),
        require_p_book_non_decreasing=require_p_book_non_decreasing,
        delta_p_book=delta_p_book,
        delta_p_book_se=delta_p_book_se,
        ideal_delta_p_book=ideal_delta_p_book,
        # Every post-book model vector that ran, on the shared CRN sample —
        # the tail-probability form gates on the WORST model's P(KILL night).
        post_pnls=(post_pnl, post_pnl_c, post_bridge_pnl, post_split_pnl),
        n_samples=n_samples,
        ruin_prob_ci_z=ruin_prob_ci_z,
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
        candidate_delta_p_book=post_axes.p_profit - pre_axes.p_profit,
        admission_ev_cc=admission_ev,
        admission_ev_source=admission_ev_source,
        confirm=confirm,
        decline_reason=reason,
    )


def _candidate_gate(
    *,
    admission_ev: float,
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
    det_max_hedge_credit: bool = False,
    hedge_budget_tail_derived: bool = False,
    tail_prob_gate: bool = False,
    kill_tail_prob: float = 0.02,
    # KILL-ANCHORED BOOK GATE (2026-07-29). ONE arming flag, default False =
    # SHADOW = byte-identical. ``hard_trip_frac`` (the ratified 12% KILL line)
    # is the ONLY number it consumes, straight from config; None degrades to
    # today's behaviour (fail closed — never a free pass). det-max is NOT
    # re-anchored here — see the det_thr comment below.
    kill_anchored_book_gate: bool = False,
    hard_trip_frac: float | None = None,
    # MARGINAL KILL GATE (2026-08-01 sunk-book ruling — see
    # ``risk/limits.RiskLimits.kill_gate_marginal``, the ONE flag both sites
    # read). When armed AND the PRE book — scored on the SAME CRN sample —
    # is already OVER the KILL tail budget, the POST level check in (2) is
    # replaced by the candidate's MARGINAL admission test. ``pre_pnls`` are
    # the PRE-book P&L vectors of the same model streams ``post_pnls``
    # carries, in the same order (CRN: post − pre is the candidate's true
    # marginal effect, not sampling noise). Default off/empty ⇒ every path
    # below is byte-identical.
    kill_gate_marginal: bool = False,
    pre_pnls: Sequence[NDArray[np.float64] | None] = (),
    require_p_book_non_decreasing: bool = False,
    delta_p_book: float = 0.0,
    delta_p_book_se: float = 0.0,
    ideal_delta_p_book: float = 0.0,
    post_pnls: Sequence[NDArray[np.float64] | None] = (),
    n_samples: int = 0,
    ruin_prob_ci_z: float = 0.0,
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
    # GATE EV SOURCE (2026-07-25 renege root cause #2): ``admission_ev`` is
    # chosen by the caller — the CALIBRATED PRICING fair's FRESH edge when
    # armed (the same model that priced the quote — the backtested moat;
    # re-priced at confirm so stale-quote pickoffs still show up), else the
    # band-high MC EV, which scores heavily-correlated same-game combos
    # structurally negative and reneged 20 won auctions in one evening.
    # Tail budgets below still gate on the conservative risk models; only
    # the candidate's OWN edge judgment switches.
    if admission_ev <= 0.0:
        if not allow_negative_ev_hedge:
            return False, "negative_ev_no_hedge_budget"
        if post.governing_model_tail_loss_cc > pre.governing_model_tail_loss_cc:
            return False, "negative_ev_not_risk_reducing"
        # The hedge's cost is the EV we give up = −admission_ev (a positive $).
        budget = float(hedge_cost_budget_cc)
        if hedge_budget_tail_derived:
            # B2 DERIVED BUDGET (operator directive 2026-07-25: the book pays
            # up to win offsetting flow when lopsided — with no manual
            # number): pay up to $1 of EV per $1 of CERTIFIED governing-tail
            # reduction, both sides measured on the SAME common-random-number
            # sample. Self-scaling: a one-way book offers a large budget for
            # exactly the balancing flow it lacks; a balanced book offers
            # ~nothing (no reduction to buy); and the price can never exceed
            # the risk actually removed (ES reduction valued at par). The
            # static ``hedge_cost_budget_cc`` remains a manual floor/override.
            budget = max(
                budget,
                pre.governing_model_tail_loss_cc
                - post.governing_model_tail_loss_cc,
            )
        if -admission_ev > budget:
            return False, "negative_ev_exceeds_hedge_budget"

    # (1c) P(BOOK) NON-DECREASE (operator doctrine 2026-07-25: "anything we
    # take in should push it up, or neutral" — measured same day: 23 of 74
    # admitted fills LOWERED p_book, worst −0.226). v2 INDEPENDENCE
    # BENCHMARK (same day, live misfire fix: the v1 absolute floor refused
    # ordinary growth fills on a knife-edge book whose −0.01..−0.06 dents
    # were the small-book parity artifact): decline only a fill whose
    # measured ΔP(book) is MEANINGFULLY WORSE (beyond 3× the CRN noise
    # floor) than the ΔP an INDEPENDENT bet of identical per-scenario P&L
    # would have produced — correlation drag, the thing the doctrine
    # actually targets; size/parity drag passes here and stays owned by the
    # size caps. Certified tail-reducers stay exempt (a hedge may pay
    # P(book) to cut the tail — priced by the B2 budget, not refused).
    # Default OFF.
    if (
        require_p_book_non_decreasing
        and delta_p_book < ideal_delta_p_book - 3.0 * delta_p_book_se
        and post.governing_model_tail_loss_cc > pre.governing_model_tail_loss_cc
    ):
        return False, "lowers_p_book"

    # (1b) OPTIONAL worst-challenger-EV tolerance (audit "+EV IS PRODUCTION-MODEL EV").
    # The admission policy above stays production-model-EV based; this ONLY ADDS a
    # decline: a candidate that is +EV under production yet whose WORST credible
    # challenger EV falls below the operator's tolerance is declined. The tolerance
    # DEFAULTS to −inf, so ``worst >= −inf`` is always True and NO behaviour changes
    # unless the operator opts a finite (negative) tolerance in. Strictly additive —
    # it can only flip an already-admitted confirm to a decline, never the reverse.
    if worst_credible_candidate_ev < worst_challenger_ev_tolerance:
        return False, "worst_challenger_ev_below_tolerance"

    # (2) The TOTAL-book joint-tail budget. TWO FORMS, operator-selected:
    #
    # DEFAULT (tail_prob_gate False, byte-identical): POST governing model
    # ES_0.99 vs the %-of-bankroll CVaR ceiling — "the average of the worst
    # 1% of nights stays under the KILL distance". At small N with ~50%-loss
    # positions this barely credits diversification (the worst 1% is "most
    # lose at once" even independent), so it caps TOTAL premium near the
    # KILL distance regardless of variance.
    #
    # TAIL-PROBABILITY FORM (operator anchor ratified 2026-07-25: "more bets
    # = more variance = more money"; risk-on per bet, one-sided concentration
    # capped by the per-game/direction/structure walls, the TOTAL book bound
    # by the PROBABILITY of a KILL night): decline iff the governing
    # (worst-model, Wilson-upper) P(post-book loss ≥ the SAME cvar threshold)
    # exceeds ``kill_tail_prob``. Diversification directly buys capacity —
    # ~40 independent bets clear at 3-8x the premium a one-way book is
    # blocked at (P ≈ 40% there) — while every concentration wall and the
    # realized-P&L halts (daily/KILL) stand unchanged. Empty vectors (an
    # all-reserved book) fail back to the ES form (never a free pass).
    if (
        portfolio_cvar_frac is not None
        and bankroll_cc is not None
        and bankroll_cc > 0
    ):
        cvar_thr = portfolio_cvar_frac * bankroll_cc
        # KILL-ANCHORED RE-ANCHOR (2026-07-29, ONE arming flag, default SHADOW
        # ⇒ ``tail_thr is cvar_thr`` and this gate is byte-identical). The
        # ratified anchor is "P(KILL-night) ≤ 2% at the 12% KILL line", but the
        # armed form has been thresholding on ``portfolio_cvar_frac`` (0.35
        # live) — 97.22% of the comonotone maximum, which is why it has never
        # fired once in 104,803 live risk_audit rows. Both anchors come from
        # config; nothing here is derived. The ES fallback below deliberately
        # keeps binding on ``cvar_thr`` (an ES magnitude is not a KILL-distance
        # probability). Keep in sync with ``risk/limits.py`` §(8a).
        tail_thr = (
            hard_trip_frac * bankroll_cc
            if (kill_anchored_book_gate and hard_trip_frac is not None)
            else cvar_thr
        )
        has_vectors = any(p is not None and p.size for p in post_pnls)
        if tail_prob_gate and has_vectors and n_samples > 0:
            p_kill = kill_tail_prob_upper(
                post_pnls, tail_thr, n_samples, ruin_prob_ci_z
            )
            if p_kill > kill_tail_prob:
                # ── MARGINAL KILL GATE (2026-08-01 sunk-book ruling). The
                # POST book is over the budget — but if the PRE book (same
                # CRN sample, same worst-model Wilson-upper read) was ALREADY
                # over, the level is inherited/sunk and refusing this
                # candidate cannot lower it. Armed, the decision becomes the
                # candidate's MARGINAL test:
                #   * CERTIFIED RISK-REDUCER — the existing hedge
                #     certification measure verbatim (POST governing model
                #     UNCLAMPED expected tail loss <= PRE, same CRN — never
                #     a leg-sign heuristic): always admit ("hedges are +EV").
                #   * else admit iff the candidate does NOT RAISE the
                #     measured P(KILL-night) — post vs pre on the SAME CRN
                #     sample, worst model, same Wilson read — i.e. its
                #     marginal effect on THE RATIFIED ANCHOR itself is
                #     non-adverse ("quoting more and filling more" that
                #     leaves P(KILL) where the sunk book put it, or lowers
                #     it, is exactly the flow the ruling orders admitted).
                #
                # JUSTIFIED DEVIATION from the dossier's dEV×P−dES99 form at
                # THIS site (the dossier invites a strictly better
                # anchor-derived criterion): the CRN governing-ES difference
                # is structurally ~the candidate's FULL premium for ANY small
                # diversifier on a flat-tail book — the post-sort worst-1%
                # re-SELECTS the (tail ∧ candidate-loses) scenarios, measured
                # 2026-08-01: a $30.60-premium new-game candidate with
                # +$1.21 EV and IDENTICAL pre/post P(KILL) (0.04675 ==
                # 0.04675) charged dES99 +$28.80 — ES "barely credits
                # diversification" is the exact pathology the tail-prob
                # anchor was ratified AGAINST (2026-07-25), so an ES-delta
                # arm here would re-freeze the book at the confirm site. The
                # dEV×P−dES99 form DOES gate — at the deterministic §(8a)
                # sites (quote-time cap, reservation, confirm floor), where
                # dES99 is the ALLOCATED per-game decomposition, not the
                # resort-inflated CRN delta; DEPTH beyond the KILL line
                # (which this probability comparison cannot see) stays
                # governed there and by the det-max backstop / per-game /
                # entity walls that all still run.
                #
                # PRE not measurably over (empty pre vectors count as NOT
                # over — no free pass: the level check below then stands) ⇒
                # today's armed level behaviour, byte-identical.
                pre_over = False
                pre_p_kill = 0.0
                if kill_gate_marginal and kill_anchored_book_gate:
                    has_pre = any(
                        p is not None and p.size for p in pre_pnls
                    )
                    if has_pre:
                        pre_p_kill = kill_tail_prob_upper(
                            pre_pnls, tail_thr, n_samples, ruin_prob_ci_z
                        )
                        pre_over = pre_p_kill > kill_tail_prob
                if pre_over:
                    certified = (
                        post.governing_model_tail_loss_cc
                        <= pre.governing_model_tail_loss_cc
                    )
                    if not (certified or p_kill <= pre_p_kill):
                        return False, "kill_marginal_raises_p_kill"
                else:
                    return False, "post_kill_tail_prob_over_budget"
        elif post.governing_model_es_99_cc > cvar_thr:
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
        # DET-MAX DEMOTION (operator RATIFICATION 2026-07-31): identical
        # rule, SAME guard, as the quote-time cap in ``risk/limits.py`` —
        # armed AND the tail-probability form governing ⇒ the wall is the
        # ruin-anchor backstop ``cap_family.det_max_backstop_frac()``; any
        # half-wired state (no KILL line threaded, tail gate off) keeps
        # today's ``portfolio_det_max_frac`` wall — the demotion NEVER applies
        # without its governor, and quote/confirm can never disagree about
        # which wall is in force (a looser gate than cap is the renege zone;
        # a looser cap than gate declines flow the cap admitted). The
        # 2026-07-31 measured caveats (ruin-convention collision, copula
        # sweep) live on ``det_max_backstop_frac``; keep in sync.
        det_thr = (
            float(det_max_backstop_frac()) * bankroll_cc
            if (
                kill_anchored_book_gate
                and tail_prob_gate
                and hard_trip_frac is not None
            )
            else portfolio_det_max_frac * bankroll_cc
        )
        post_det_gate = post.deterministic_max_loss_cc
        if post.mutex_aware_det_max_cc is not None:
            post_det_gate = min(post_det_gate, post.mutex_aware_det_max_cc)
            # FIX 3 HEDGE ACCOUNTING (2026-07-28, arming flag defaults SHADOW).
            # Two positions that provably CANNOT BOTH LOSE must not both be
            # charged. Applied ONLY on top of the mutex-aware bound the credit
            # was measured against (never on the raw comonotone fallback, whose
            # None case means the fold never ran). Disarmed ⇒ byte-identical.
            if det_max_hedge_credit:
                post_det_gate = max(
                    0.0, post_det_gate - post.det_max_hedge_credit_cc
                )
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
