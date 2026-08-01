"""MARGINAL RUIN GATE — property tests (2026-08-01, the ruin axis of the
sunk-book ruling).

THE FREEZE UNDER TEST (measured live, 2026-08-01 evening slate): §(9)'s LEVEL
form read p_ruin 0.2994 (== its Wilson upper at the live z of 0) against the
0.05 budget and refused EVERY candidate — ``skip_portfolio_ruin`` 1,044/5 min,
``quote_sent`` = 0 — while every new fill was PREGAME-ONLY (future games =
diversifiers against the in-play book carrying the ruin mass); three in-flight
fills that DID land moved the measured p_ruin 0.2994 → 0.1649 within 90 s.
The standing book is SUNK: a level no refusal can lower must not freeze the
flow that lowers it.

The properties pinned here (one per operator requirement):
  * OVER-budget book + diversifier (allocated dES99 == 0)  ⇒ ADMITS, even on
    an empty acceptance tape (VALIDATE-CAPS-CAN-QUOTE);
  * OVER-budget book + concentrator (dES99 > EV credit)    ⇒ REFUSES;
  * certified risk-reducer                                  ⇒ always ADMITS;
  * UNDER-budget book                                       ⇒ byte-identical
    with and without the flag (the gate is silent there today, stays silent);
  * UNKNOWN (no marginal input / no pre sample)             ⇒ never admits —
    the level form stands;
  * staleness (unusable snapshot)                           ⇒ fails closed on
    the portfolio axes before any regime is read;
  * det-max backstop (0.70B)                                ⇒ absolute — no
    marginal bypass of the model-free floor.

The confirm-path mirror (``sim/book_risk._candidate_gate`` check (4)) is
pinned with the same two-regime cases on synthetic pre/post axes carrying
p_ruin directly — pre over budget: admit iff certified or post p_ruin <= pre;
pre under budget: today's level rule byte-identically.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from combomaker.core.conventions import Conventions, Side
from combomaker.core.quantity import CentiContracts
from combomaker.risk.cap_family import det_max_backstop_frac
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.limits import (
    DailyPnl,
    KillMarginalCandidate,
    LimitChecker,
    ReasonCode,
    RiskLimits,
    threshold_cc,
)
from combomaker.sim.book_model import build_book_model
from combomaker.sim.book_risk import (
    _candidate_gate,
    _TailAxes,
    compute_book_risk,
)

# The LIVE risk bankroll at the 2026-08-01 22:28:12Z freeze snapshot (derived
# from the logged det_max_backstop_cc 19,151,071 = 0.70·B): $2,735.87. The
# ruin floor in the assertions is the exact wall the frozen bot enforced.
BANKROLL = 27_358_673
RUIN_BUDGET = Fraction(5, 100)
RUIN_FLOOR_FRAC = 0.70
FLOOR_CC = int(RUIN_FLOOR_FRAC * BANKROLL)          # $1,915.11
BACKSTOP_CC = threshold_cc(det_max_backstop_frac(), BANKROLL)

CONVENTIONS = Conventions(
    verified=True,
    source="test",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)


def _pos(i: int, game: str, contracts: int, price_cc: int) -> OpenPosition:
    return OpenPosition(
        position_id=f"r{i}",
        combo_ticker=f"COMBO-r{i}",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(contracts),
        entry_price_cc=price_cc,  # type: ignore[arg-type]
        legs=(
            LegRef(
                market_ticker=f"L{game}",
                event_ticker=f"KXG-{game}",
                side="yes",
            ),
        ),
    )


def _book(n: int, premium_each_cc: int, price_cc: int) -> list[OpenPosition]:
    contracts = premium_each_cc * 100 // price_cc
    return [_pos(i, f"G{i}", contracts, price_cc) for i in range(n)]


def _snapshot(
    positions: list[OpenPosition],
    p_hit: float,
    equity_cc: int,
    seed: int = 11,
):
    model = build_book_model(
        positions, marginals=lambda _t: p_hit, within_game_rho=None
    )
    return compute_book_risk(
        model,
        n_samples=80_000,
        seed=seed,
        bankroll_cc=BANKROLL,
        current_equity_cc=equity_cc,
        ruin_floor_frac=RUIN_FLOOR_FRAC,
    )


def _limits(*, marginal: bool, **kw: object) -> RiskLimits:
    """The ruin flag ALONE — the KILL-gate flags stay at their defaults
    (disarmed), proving the ruin axis arms independently."""
    base: dict[str, object] = dict(
        caps_shadow_mode=False,
        portfolio_tail_prob_gate=True,
        portfolio_cvar_frac=Fraction(35, 100),
        portfolio_det_max_frac=Fraction(36, 100),
        portfolio_ruin_prob_budget=RUIN_BUDGET,
        ruin_gate_marginal=marginal,
    )
    base.update(kw)
    return RiskLimits(**base)  # type: ignore[arg-type]


def _check(
    limits: RiskLimits, snap, kill_marginal: KillMarginalCandidate | None = None
):
    return LimitChecker(limits).check(
        ExposureBook(CONVENTIONS),
        lambda _t: 0.5,
        DailyPnl(),
        risk_bankroll_cc=BANKROLL,
        book_risk=snap,
        kill_marginal=kill_marginal,
    )


def _reasons(limits, snap, kill_marginal=None) -> set[ReasonCode]:
    return {b.reason for b in _check(limits, snap, kill_marginal)}


# OVER-ruin-budget book, freeze-shaped: modest premium (far under every
# dollar wall — the KILL line is $328.30, total premium here is $186 — so
# ONLY the ruin axis fires) but equity sitting $10 above the floor, exactly
# the written-off-reserve geometry of the live freeze.
OVER_EQUITY_CC = FLOOR_CC + 100_000              # $10 of ruin distance
UNDER_EQUITY_CC = FLOOR_CC + 20_000_000          # $2,000 — no book can cross


@pytest.fixture(scope="module")
def over_budget():
    b = _book(6, 310_000, 6_800)
    snap = _snapshot(b, 0.295, OVER_EQUITY_CC)
    assert max(snap.p_ruin, snap.p_ruin_upper) > float(RUIN_BUDGET)
    return b, snap


@pytest.fixture(scope="module")
def under_budget():
    b = _book(6, 310_000, 6_800)
    snap = _snapshot(b, 0.295, UNDER_EQUITY_CC)
    assert max(snap.p_ruin, snap.p_ruin_upper) <= float(RUIN_BUDGET)
    return b, snap


DIVERSIFIER = KillMarginalCandidate(
    # +EV candidate on a game the book's tail decomposition does not touch:
    # allocated dES99 == 0 exactly; p_accept_lower 0.0 = an EMPTY acceptance
    # tape — the admit IS the VALIDATE-CAPS-CAN-QUOTE proof for this gate.
    ev_cc=5_000,
    p_accept_lower=0.0,
    des99_cc=0.0,
)
CONCENTRATOR = KillMarginalCandidate(
    ev_cc=5_000,
    p_accept_lower=1.0,
    des99_cc=400_000.0,
)
CERTIFIED = KillMarginalCandidate(
    ev_cc=-2_000,
    p_accept_lower=0.0,
    des99_cc=350_000.0,
    certified_risk_reducing=True,
)


class _Unusable:
    """A present-but-unusable snapshot (UNKNOWN marginal / empty book)."""

    usable = False
    governing_model_es_99_cc = 0.0
    deterministic_max_loss_cc = 0.0
    mutex_aware_det_max_cc = None
    p_ruin = 1.0                      # even a screaming p_ruin on an
    p_ruin_upper = 1.0                # unusable snapshot must not reach §(9)
    loss_quantiles_cc: tuple[float, ...] = ()
    n_samples = 0
    det_max_hedge_credit_cc = 0.0


class TestQuoteTimeOverBudget:
    def test_diversifier_admits_even_with_an_empty_acceptance_tape(
        self, over_budget
    ) -> None:
        _, snap = over_budget
        reasons = _reasons(_limits(marginal=True), snap, DIVERSIFIER)
        assert ReasonCode.SKIP_PORTFOLIO_RUIN not in reasons

    def test_concentrator_refuses_with_marginal_detail(self, over_budget) -> None:
        _, snap = over_budget
        breaches = _check(_limits(marginal=True), snap, CONCENTRATOR)
        ruin = [b for b in breaches if b.reason is ReasonCode.SKIP_PORTFOLIO_RUIN]
        assert len(ruin) == 1
        assert "(marginal form)" in ruin[0].detail
        assert "over ruin budget" in ruin[0].detail

    def test_measured_acceptance_buys_exactly_proportional_capacity(
        self, over_budget
    ) -> None:
        _, snap = over_budget
        at_boundary = KillMarginalCandidate(
            ev_cc=10_000, p_accept_lower=0.25, des99_cc=2_500.0
        )
        over_boundary = KillMarginalCandidate(
            ev_cc=10_000, p_accept_lower=0.25, des99_cc=2_501.0
        )
        assert ReasonCode.SKIP_PORTFOLIO_RUIN not in _reasons(
            _limits(marginal=True), snap, at_boundary
        )
        assert ReasonCode.SKIP_PORTFOLIO_RUIN in _reasons(
            _limits(marginal=True), snap, over_boundary
        )

    def test_certified_risk_reducer_always_admits(self, over_budget) -> None:
        _, snap = over_budget
        assert ReasonCode.SKIP_PORTFOLIO_RUIN not in _reasons(
            _limits(marginal=True), snap, CERTIFIED
        )

    def test_no_marginal_input_keeps_the_level_form(self, over_budget) -> None:
        """UNKNOWN never admits: a book-only/maintenance caller (no marginal
        facts) sees the exact level refusal, armed or not."""
        _, snap = over_budget
        armed = _check(_limits(marginal=True), snap, None)
        level = _check(_limits(marginal=False), snap, None)
        assert [(b.reason, b.detail) for b in armed] == [
            (b.reason, b.detail) for b in level
        ]
        assert ReasonCode.SKIP_PORTFOLIO_RUIN in {b.reason for b in armed}

    def test_ruin_axis_arms_independently_of_the_kill_flags(
        self, over_budget
    ) -> None:
        """The freeze fix must not depend on the (currently disarmed)
        KILL-anchored gate: ruin_gate_marginal alone admits the diversifier."""
        _, snap = over_budget
        limits = _limits(marginal=True)
        assert not limits.kill_anchored_book_gate
        assert not limits.kill_gate_marginal
        assert ReasonCode.SKIP_PORTFOLIO_RUIN not in _reasons(
            limits, snap, DIVERSIFIER
        )

    def test_unusable_snapshot_fails_closed_before_any_regime(self) -> None:
        """Staleness fail-closed UNCHANGED: the unusable branch refuses both
        portfolio axes and the marginal input buys nothing."""
        reasons = _reasons(_limits(marginal=True), _Unusable(), DIVERSIFIER)
        assert ReasonCode.SKIP_PORTFOLIO_CVAR in reasons
        assert ReasonCode.SKIP_PORTFOLIO_DET_MAX in reasons

    def test_det_max_backstop_stays_absolute(self) -> None:
        """No marginal bypass of the model-free 0.70B floor: a book past the
        backstop refuses on the det axis even while the ruin axis admits the
        diversifier — the ONE surviving level gate stays absolute."""
        book = _book(3, 8_000_000, 9_500)   # $2,400 premium > 0.70B $1,915.11
        snap = _snapshot(book, 0.05, OVER_EQUITY_CC)
        assert snap.deterministic_max_loss_cc > BACKSTOP_CC
        reasons = _reasons(
            _limits(
                marginal=True,
                kill_anchored_book_gate=True,
                kill_gate_marginal=True,
                hard_trip_frac=Fraction(12, 100),
            ),
            snap,
            DIVERSIFIER,
        )
        assert ReasonCode.SKIP_PORTFOLIO_DET_MAX in reasons


class TestQuoteTimeUnderBudgetUnchanged:
    def test_under_budget_is_byte_identical_with_and_without_the_flag(
        self, under_budget
    ) -> None:
        _, snap = under_budget
        base = _check(_limits(marginal=False), snap)
        flagged = _check(_limits(marginal=True), snap, DIVERSIFIER)
        assert [(b.reason, b.detail) for b in base] == [
            (b.reason, b.detail) for b in flagged
        ]
        assert ReasonCode.SKIP_PORTFOLIO_RUIN not in {b.reason for b in base}

    def test_flag_off_is_byte_identical_even_with_an_input_supplied(
        self, over_budget
    ) -> None:
        """The dark guarantee: flag OFF ignores the input entirely — same
        reasons, same detail strings as today's level form."""
        _, snap = over_budget
        base = _check(_limits(marginal=False), snap)
        with_input = _check(_limits(marginal=False), snap, DIVERSIFIER)
        assert [(b.reason, b.detail) for b in base] == [
            (b.reason, b.detail) for b in with_input
        ]


# ---------------------------------------------------------------- confirm gate


def _axes(
    p_ruin: float, tail_loss: float, *, es: float = 50_000.0
) -> _TailAxes:
    return _TailAxes(
        ev_cc=0.0,
        es_99_cc=es,
        challenger_es_99_cc=es,
        governing_model_es_99_cc=es,
        deterministic_max_loss_cc=1_000.0,
        gross_settlement_notional_cc=0.0,
        p_ruin=p_ruin,
        governing_model_tail_loss_cc=tail_loss,
        p_ruin_upper=p_ruin,
    )


GATE_BANKROLL = 1_000_000


def _gate(
    *,
    marginal: bool,
    pre_ruin: float,
    post_ruin: float,
    pre_tail: float = 150_000.0,
    post_tail: float = 150_000.0,
) -> tuple[bool, str]:
    return _candidate_gate(
        admission_ev=1_000.0,
        worst_credible_candidate_ev=1_000.0,
        worst_challenger_ev_tolerance=float("-inf"),
        pre=_axes(pre_ruin, pre_tail),
        post=_axes(post_ruin, post_tail),
        bankroll_cc=GATE_BANKROLL,
        portfolio_cvar_frac=None,
        portfolio_det_max_frac=None,
        portfolio_ruin_prob_budget=float(RUIN_BUDGET),
        absolute_notional_multiple=None,
        hedge_cost_budget_cc=0,
        allow_negative_ev_hedge=False,
        ruin_gate_marginal=marginal,
    )


class TestConfirmGateTwoRegimes:
    def test_over_budget_diversifier_admits_marginal(self) -> None:
        """PRE over the ruin budget, candidate leaves the CRN-measured
        P(ruin) untouched (or lowers it) => admit; flag off => the level
        decline that froze the live confirm path, byte-identical."""
        same = dict(pre_ruin=0.30, post_ruin=0.30)
        assert _gate(marginal=True, **same) == (True, "")
        assert _gate(marginal=False, **same) == (
            False, "post_ruin_prob_over_budget"
        )
        lowers = dict(pre_ruin=0.30, post_ruin=0.16)   # the live 22:31 move
        assert _gate(marginal=True, **lowers) == (True, "")

    def test_over_budget_concentrator_refuses(self) -> None:
        confirm, reason = _gate(
            marginal=True, pre_ruin=0.30, post_ruin=0.32,
            pre_tail=150_000.0, post_tail=155_000.0,   # tail grows too
        )
        assert (confirm, reason) == (False, "ruin_marginal_raises_p_ruin")

    def test_over_budget_certified_reducer_admits(self) -> None:
        """The certification measure verbatim (POST unclamped tail <= PRE on
        shared CRN) admits even when the quantized p_ruin count ticks up."""
        confirm, reason = _gate(
            marginal=True, pre_ruin=0.30, post_ruin=0.31,
            pre_tail=150_000.0, post_tail=149_000.0,
        )
        assert (confirm, reason) == (True, "")

    def test_under_budget_unchanged(self) -> None:
        """PRE under budget: today's armed rule exactly — a candidate that
        pushes the book over the ruin budget declines, marginal flag or not
        (UNKNOWN-regime never admits)."""
        for marginal in (True, False):
            confirm, reason = _gate(
                marginal=marginal, pre_ruin=0.04, post_ruin=0.06
            )
            assert (confirm, reason) == (
                False, "post_ruin_prob_over_budget"
            )

    def test_under_budget_pass_stays_a_pass(self) -> None:
        for marginal in (True, False):
            assert _gate(
                marginal=marginal, pre_ruin=0.01, post_ruin=0.02
            ) == (True, "")
