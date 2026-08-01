"""MARGINAL KILL GATE (2026-08-01) — the sunk-book two-regime form.

THE DEFECT the marginal form repairs, measured live 2026-08-01 (boot 14:49Z):
``kill_anchored_book_gate`` armed on an INHERITED 26-position book whose
envelope read P(loss >= the 12% KILL line) = 0.110-0.115 against the 0.02
budget. §(8a) is a LEVEL check — it judges the BOOK, which no candidate can
change — so it refused EVERY candidate: 140,338 ``skip_portfolio_cvar``
decline rows, ``quote_sent`` = 0 for the whole session, hedges included.

THE OPERATOR RULING (verbatim, 2026-08-01): "The only reason the book could
be -EV is if odds have changed... even if we did have a -EV book we should
still quote to increase it; when we fill something we always fill at +EV;
what happens after that we can't decide, besides quoting more and filling
more."  Ratified translation: the STANDING book is SUNK (sell-only, no
unwind); the absolute level constraint belongs to the model-free det-max
backstop (0.70B); the KILL gate must judge the MARGINAL candidate.

Pinned here, one class per property the design owes:
  * UNDER budget: armed behaviour UNCHANGED (level silence at quote time,
    post-add <= budget at the confirm gate) — flag on == flag off there;
  * OVER budget + DIVERSIFYING candidate (zero allocated marginal tail):
    ADMITS — even on a fresh boot with an EMPTY acceptance tape (the
    VALIDATE-CAPS-CAN-QUOTE requirement: the ``<=`` boundary, justified in
    ``RiskLimits.kill_gate_marginal``);
  * OVER budget + CONCENTRATING candidate (marginal tail beyond its measured
    EV credit): REFUSES, and measured acceptance credit buys exactly
    proportional concentrating capacity;
  * certified risk-reducers always admit ("hedges are +EV");
  * the det-max BACKSTOP stays absolute (no marginal bypass);
  * UNKNOWN never admits: no marginal input => the level form stands; an
    unusable snapshot still fails closed on BOTH portfolio axes;
  * flag OFF is byte-identical to the armed level form (same reasons, same
    detail strings);
  * the regime probe (``kill_envelope_tail_upper``) and the cap agree on the
    same inputs — one implementation, no drift.
"""

from __future__ import annotations

from fractions import Fraction

import numpy as np
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
    kill_envelope_tail_upper,
    threshold_cc,
)
from combomaker.sim.book_model import build_book_model
from combomaker.sim.book_risk import (
    _candidate_gate,
    _TailAxes,
    compute_book_risk,
    evaluate_candidate_book_risk,
)

# The LIVE risk bankroll of the freeze day (account_standing
# exchange_equity_cc, 2026-08-01 14:50Z boot): $2,802.68 — the dollars in the
# assertions are the operator's actual walls that morning: KILL line $336.32,
# armed det-max backstop $1,961.87.
BANKROLL = 28_026_768
KILL_FRAC = Fraction(12, 100)
KILL_TAIL_PROB = 0.02
KILL_LINE_CC = threshold_cc(KILL_FRAC, BANKROLL)          # 3_363_212 = $336.32
BACKSTOP_CC = threshold_cc(det_max_backstop_frac(), BANKROLL)  # $1,961.87

CONVENTIONS = Conventions(
    verified=True,
    source="test",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)


def _pos(
    i: int, game: str, contracts: int, price_cc: int, *, tag: str = "p"
) -> OpenPosition:
    return OpenPosition(
        position_id=f"{tag}{i}",
        combo_ticker=f"COMBO-{tag}{i}",
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


def _snapshot(positions: list[OpenPosition], p_hit: float, seed: int = 11):
    model = build_book_model(
        positions, marginals=lambda _t: p_hit, within_game_rho=None
    )
    return compute_book_risk(
        model, n_samples=80_000, seed=seed, bankroll_cc=BANKROLL
    )


def _limits(*, marginal: bool, armed: bool = True, **kw: object) -> RiskLimits:
    base: dict[str, object] = dict(
        caps_shadow_mode=False,
        portfolio_tail_prob_gate=True,
        portfolio_kill_tail_prob=KILL_TAIL_PROB,
        portfolio_cvar_frac=Fraction(35, 100),
        portfolio_det_max_frac=Fraction(36, 100),
        hard_trip_frac=KILL_FRAC,
        kill_anchored_book_gate=armed,
        kill_gate_marginal=marginal,
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


# The live-shape OVER-budget book: concentrated, P(KILL-night) well over 2%.
OVER = dict(n=2, premium_each_cc=3_800_000, price_cc=9_500, p_hit=0.024)
# An UNDER-budget book: total premium below the KILL line => P(KILL) == 0.
UNDER = dict(n=6, premium_each_cc=310_000, price_cc=6_800, p_hit=0.295)


@pytest.fixture(scope="module")
def over_budget():
    b = _book(OVER["n"], OVER["premium_each_cc"], OVER["price_cc"])  # type: ignore[arg-type]
    return b, _snapshot(b, OVER["p_hit"])  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def under_budget():
    b = _book(UNDER["n"], UNDER["premium_each_cc"], UNDER["price_cc"])  # type: ignore[arg-type]
    return b, _snapshot(b, UNDER["p_hit"])  # type: ignore[arg-type]


DIVERSIFIER = KillMarginalCandidate(
    # A +EV candidate on a game the book's tail decomposition does not touch:
    # allocated dES99 == 0 exactly. p_accept_lower 0.0 = a FRESH BOOT's empty
    # acceptance tape — the admit below IS the VALIDATE-CAPS-CAN-QUOTE proof.
    ev_cc=5_000,
    p_accept_lower=0.0,
    des99_cc=0.0,
)
CONCENTRATOR = KillMarginalCandidate(
    # Marginal tail far beyond any EV credit — the fifth ticket on the hot game.
    ev_cc=5_000,
    p_accept_lower=1.0,
    des99_cc=400_000.0,
)


class _Unusable:
    """A present-but-unusable snapshot (UNKNOWN marginal / empty book)."""

    usable = False
    governing_model_es_99_cc = 0.0
    deterministic_max_loss_cc = 0.0
    mutex_aware_det_max_cc = None
    p_ruin = 0.0
    p_ruin_upper = 0.0
    loss_quantiles_cc: tuple[float, ...] = ()
    n_samples = 0
    det_max_hedge_credit_cc = 0.0


class TestRegimeProbeCoherence:
    def test_helper_and_gate_agree_on_the_regime(
        self, over_budget, under_budget
    ) -> None:
        """``kill_envelope_tail_upper`` IS §(8a)'s number: over budget exactly
        when the armed level gate refuses, under exactly when it is silent."""
        for _, snap in (over_budget, under_budget):
            lim = _limits(marginal=False)
            p_upper = kill_envelope_tail_upper(snap, lim, BANKROLL)
            assert p_upper is not None
            refused = ReasonCode.SKIP_PORTFOLIO_CVAR in _reasons(lim, snap)
            assert (p_upper > KILL_TAIL_PROB) == refused

    def test_helper_fails_none_on_unusable_gate_off_or_no_bankroll(
        self, over_budget
    ) -> None:
        _, snap = over_budget
        lim = _limits(marginal=True)
        assert kill_envelope_tail_upper(_Unusable(), lim, BANKROLL) is None
        assert kill_envelope_tail_upper(None, lim, BANKROLL) is None
        assert kill_envelope_tail_upper(snap, lim, None) is None
        off = _limits(marginal=True, portfolio_tail_prob_gate=False)
        assert kill_envelope_tail_upper(snap, off, BANKROLL) is None


class TestQuoteTimeOverBudget:
    def test_diversifier_admits_even_with_an_empty_acceptance_tape(
        self, over_budget
    ) -> None:
        """OVER budget + zero-marginal-tail candidate => NO portfolio-CVaR
        breach — the frozen book quotes again. p_accept_lower = 0 (fresh-boot
        empty tape) makes this the VALIDATE-CAPS-CAN-QUOTE pin: strict ``>``
        would have deadlocked (no quotes -> no tape -> no admits)."""
        _, snap = over_budget
        reasons = _reasons(_limits(marginal=True), snap, DIVERSIFIER)
        assert ReasonCode.SKIP_PORTFOLIO_CVAR not in reasons

    def test_concentrator_refuses_with_marginal_detail(self, over_budget) -> None:
        _, snap = over_budget
        breaches = _check(_limits(marginal=True), snap, CONCENTRATOR)
        cvar = [
            b for b in breaches if b.reason is ReasonCode.SKIP_PORTFOLIO_CVAR
        ]
        assert len(cvar) == 1
        assert "(marginal form)" in cvar[0].detail
        assert "candidate marginal tail" in cvar[0].detail

    def test_measured_acceptance_buys_exactly_proportional_capacity(
        self, over_budget
    ) -> None:
        """The admission boundary is dES99 <= ev x p_accept_lower — the
        candidate's marginal tail must be covered by the EV it REALISTICALLY
        brings (CP-lower measured acceptance), no other number."""
        _, snap = over_budget
        lim = _limits(marginal=True)
        ev, des = 10_000, 4_000.0
        covered = KillMarginalCandidate(
            ev_cc=ev, p_accept_lower=0.5, des99_cc=des  # credit 5000 >= 4000
        )
        uncovered = KillMarginalCandidate(
            ev_cc=ev, p_accept_lower=0.3, des99_cc=des  # credit 3000 < 4000
        )
        assert ReasonCode.SKIP_PORTFOLIO_CVAR not in _reasons(lim, snap, covered)
        assert ReasonCode.SKIP_PORTFOLIO_CVAR in _reasons(lim, snap, uncovered)

    def test_certified_risk_reducer_always_admits(self, over_budget) -> None:
        """Hedges are +EV: certification (state enumeration / CRN, never a
        leg-sign heuristic) bypasses the marginal arithmetic entirely."""
        _, snap = over_budget
        hedge = KillMarginalCandidate(
            ev_cc=-1,  # even at negative EV
            p_accept_lower=0.0,
            des99_cc=999_999.0,  # and an adverse allocation
            certified_risk_reducing=True,
        )
        reasons = _reasons(_limits(marginal=True), snap, hedge)
        assert ReasonCode.SKIP_PORTFOLIO_CVAR not in reasons

    def test_no_marginal_input_keeps_the_level_form(self, over_budget) -> None:
        """UNKNOWN never admits: a caller that cannot supply the candidate's
        marginal facts (book-only maintenance, unknown EV) gets the exact
        armed level refusal."""
        _, snap = over_budget
        armed_level = _check(_limits(marginal=False), snap)
        marginal_none = _check(_limits(marginal=True), snap, None)
        assert [
            (b.reason, b.detail) for b in armed_level
        ] == [(b.reason, b.detail) for b in marginal_none]
        assert any(
            b.reason is ReasonCode.SKIP_PORTFOLIO_CVAR
            and "(tail-probability form)" in b.detail
            for b in marginal_none
        )

    def test_unusable_snapshot_fails_closed_on_both_axes(self) -> None:
        """Staleness fail-closed UNCHANGED: an unmeasured book refuses both
        portfolio axes before any regime (or marginal input) is consulted."""
        reasons = _reasons(_limits(marginal=True), _Unusable(), DIVERSIFIER)
        assert ReasonCode.SKIP_PORTFOLIO_CVAR in reasons
        assert ReasonCode.SKIP_PORTFOLIO_DET_MAX in reasons

    def test_det_max_backstop_stays_absolute(self) -> None:
        """No marginal bypass of the model-free floor: a book past 0.70B
        refuses on the det axis even while the marginal form admits the
        candidate on the CVaR axis."""
        book = _book(3, 8_000_000, 9_500)  # $2,400 premium > $1,961.87
        snap = _snapshot(book, 0.05)
        assert snap.deterministic_max_loss_cc > BACKSTOP_CC
        reasons = _reasons(_limits(marginal=True), snap, DIVERSIFIER)
        assert ReasonCode.SKIP_PORTFOLIO_DET_MAX in reasons
        assert ReasonCode.SKIP_PORTFOLIO_CVAR not in reasons


class TestQuoteTimeUnderBudgetUnchanged:
    def test_under_budget_is_byte_identical_with_and_without_the_flag(
        self, under_budget
    ) -> None:
        _, snap = under_budget
        p_upper = kill_envelope_tail_upper(snap, _limits(marginal=True), BANKROLL)
        assert p_upper is not None and p_upper <= KILL_TAIL_PROB
        base = _check(_limits(marginal=False), snap)
        flagged = _check(_limits(marginal=True), snap, DIVERSIFIER)
        assert [(b.reason, b.detail) for b in base] == [
            (b.reason, b.detail) for b in flagged
        ]

    def test_flag_off_is_byte_identical_even_with_an_input_supplied(
        self, over_budget
    ) -> None:
        """The shadow guarantee: flag OFF ignores the input entirely — same
        reasons, same detail strings as the armed level form."""
        _, snap = over_budget
        base = _check(_limits(marginal=False), snap)
        with_input = _check(_limits(marginal=False), snap, DIVERSIFIER)
        assert [(b.reason, b.detail) for b in base] == [
            (b.reason, b.detail) for b in with_input
        ]


# ---------------------------------------------------------------- confirm gate


def _axes(
    es: float, tail_loss: float, *, ev: float = 0.0, det: float = 1_000.0
) -> _TailAxes:
    return _TailAxes(
        ev_cc=ev,
        es_99_cc=es,
        challenger_es_99_cc=es,
        governing_model_es_99_cc=es,
        deterministic_max_loss_cc=det,
        gross_settlement_notional_cc=0.0,
        p_ruin=0.0,
        governing_model_tail_loss_cc=tail_loss,
    )


def _pnl(frac_at_loss: float, loss_cc: float, n: int = 1_000):
    """A P&L vector with ``frac_at_loss`` of its mass at −loss_cc, rest +1."""
    k = int(round(frac_at_loss * n))
    return np.concatenate(
        [np.full(k, -loss_cc), np.full(n - k, 1.0)]
    ).astype(np.float64)


GATE_BANKROLL = 1_000_000
TAIL_THR = 0.12 * GATE_BANKROLL  # 120_000


def _gate(
    *,
    marginal: bool,
    pre_frac: float,
    post_frac: float,
    admission_ev: float = 1_000.0,
    pre_tail: float = 150_000.0,
    post_tail: float = 150_000.0,
) -> tuple[bool, str]:
    return _candidate_gate(
        admission_ev=admission_ev,
        worst_credible_candidate_ev=admission_ev,
        worst_challenger_ev_tolerance=float("-inf"),
        pre=_axes(150_000.0, pre_tail),
        post=_axes(150_000.0, post_tail),
        bankroll_cc=GATE_BANKROLL,
        portfolio_cvar_frac=0.35,
        portfolio_det_max_frac=None,
        portfolio_ruin_prob_budget=None,
        absolute_notional_multiple=None,
        hedge_cost_budget_cc=0,
        allow_negative_ev_hedge=False,
        tail_prob_gate=True,
        kill_tail_prob=KILL_TAIL_PROB,
        kill_anchored_book_gate=True,
        hard_trip_frac=0.12,
        kill_gate_marginal=marginal,
        pre_pnls=(_pnl(pre_frac, 200_000.0),),
        post_pnls=(_pnl(post_frac, 200_000.0),),
        n_samples=1_000,
    )


class TestConfirmGateTwoRegimes:
    def test_over_budget_diversifier_admits_marginal(self) -> None:
        """PRE over budget, candidate leaves the measured P(KILL) untouched
        (or lowers it) => admit; flag off => the level decline (the armed
        form's live freeze, byte-identical)."""
        same = dict(pre_frac=0.05, post_frac=0.05)  # P(KILL) unchanged
        assert _gate(marginal=True, **same) == (True, "")
        assert _gate(marginal=False, **same) == (
            False, "post_kill_tail_prob_over_budget"
        )
        lowers = dict(pre_frac=0.05, post_frac=0.04)  # a KILL-reducing fill
        assert _gate(marginal=True, **lowers) == (True, "")

    def test_over_budget_concentrator_refuses(self) -> None:
        """A candidate that RAISES the measured P(KILL-night) on the shared
        CRN sample is refused — its marginal effect on the ratified anchor
        itself is adverse."""
        confirm, reason = _gate(
            marginal=True, pre_frac=0.05, post_frac=0.08,
            pre_tail=150_000.0, post_tail=155_000.0,  # tail grows too — not
            # a certified reducer (equal tails WOULD certify: post <= pre)
        )
        assert (confirm, reason) == (False, "kill_marginal_raises_p_kill")

    def test_over_budget_certified_reducer_admits(self) -> None:
        """The existing certification measure verbatim (POST unclamped tail
        <= PRE on shared CRN) admits even when the quantized P(KILL) count
        ticks up."""
        confirm, reason = _gate(
            marginal=True, pre_frac=0.05, post_frac=0.06,
            pre_tail=150_000.0, post_tail=149_000.0,  # certified reducer
        )
        assert (confirm, reason) == (True, "")

    def test_under_budget_unchanged(self) -> None:
        """PRE under budget: today's armed rule exactly — a candidate that
        pushes the book over the budget declines, marginal flag or not."""
        for marginal in (True, False):
            confirm, reason = _gate(
                marginal=marginal, pre_frac=0.0, post_frac=0.05
            )
            assert (confirm, reason) == (
                False, "post_kill_tail_prob_over_budget"
            )

    def test_empty_pre_vectors_are_never_a_free_pass(self) -> None:
        """No PRE sample => the regime cannot be measured => the level rule
        stands (UNKNOWN never admits)."""
        confirm, reason = _candidate_gate(
            admission_ev=1_000.0,
            worst_credible_candidate_ev=1_000.0,
            worst_challenger_ev_tolerance=float("-inf"),
            pre=_axes(150_000.0, 150_000.0),
            post=_axes(150_400.0, 150_400.0),
            bankroll_cc=GATE_BANKROLL,
            portfolio_cvar_frac=0.35,
            portfolio_det_max_frac=None,
            portfolio_ruin_prob_budget=None,
            absolute_notional_multiple=None,
            hedge_cost_budget_cc=0,
            allow_negative_ev_hedge=False,
            tail_prob_gate=True,
            kill_tail_prob=KILL_TAIL_PROB,
            kill_anchored_book_gate=True,
            hard_trip_frac=0.12,
            kill_gate_marginal=True,
            pre_pnls=(),
            post_pnls=(_pnl(0.05, 200_000.0),),
            n_samples=1_000,
        )
        assert (confirm, reason) == (False, "post_kill_tail_prob_over_budget")


class TestConfirmGateEndToEnd:
    """The full evaluator on real books: the live freeze shape end to end."""

    def test_inherited_over_budget_book_admits_a_fresh_game_candidate(
        self, over_budget
    ) -> None:
        book, _ = over_budget
        candidate = _pos(99, "GNEW", 4_500, 6_800, tag="c")  # $306, new game
        marginals = {f"LG{i}": OVER["p_hit"] for i in range(OVER["n"])}  # type: ignore[arg-type]
        marginals["LGNEW"] = 0.295
        kw = dict(
            marginals=lambda t: marginals.get(t, 0.5),
            n_samples=20_000,
            seed=7,
            bankroll_cc=BANKROLL,
            portfolio_cvar_frac=0.35,
            portfolio_det_max_frac=float(det_max_backstop_frac()),
            portfolio_ruin_prob_budget=None,
            tail_prob_gate=True,
            kill_tail_prob=KILL_TAIL_PROB,
            kill_anchored_book_gate=True,
            hard_trip_frac=float(KILL_FRAC),
        )
        level = evaluate_candidate_book_risk(
            book, candidate, kill_gate_marginal=False, **kw  # type: ignore[arg-type]
        )
        marginal = evaluate_candidate_book_risk(
            book, candidate, kill_gate_marginal=True, **kw  # type: ignore[arg-type]
        )
        # The level form freezes (the live defect, reproduced at the gate);
        # the marginal form admits the diversifying +EV candidate.
        assert level.confirm is False
        assert level.decline_reason == "post_kill_tail_prob_over_budget"
        assert marginal.candidate_ev_cc > 0
        assert marginal.confirm is True, marginal.decline_reason

    def test_inherited_over_budget_book_still_refuses_a_p_kill_raiser(
        self, over_budget
    ) -> None:
        """A candidate big enough to be its OWN KILL night (premium past the
        KILL line net of the book's win slivers) RAISES the measured P(KILL)
        by ~its hit probability => refused. DEPTH concentration that leaves
        P(KILL) untouched (a same-game depth-doubler) is deliberately NOT
        this gate's job — it is refused at the deterministic §(8a) sites by
        the ALLOCATED dES99 (TestQuoteTimeOverBudget::
        test_concentrator_refuses_with_marginal_detail) and stays bounded by
        the det-max backstop / per-game / entity walls that all still run."""
        book, _ = over_budget
        # $400 premium on a new game at p_hit 0.295: a lone loss crosses the
        # $336.32 KILL line => post P(KILL) jumps ~+28 points.
        candidate = _pos(98, "GBIG", 58_800, 6_800, tag="c")
        assert candidate.max_loss_cc > KILL_LINE_CC
        marginals = {f"LG{i}": OVER["p_hit"] for i in range(OVER["n"])}  # type: ignore[arg-type]
        marginals["LGBIG"] = 0.295
        verdict = evaluate_candidate_book_risk(
            book,
            candidate,
            marginals=lambda t: marginals.get(t, 0.5),
            n_samples=20_000,
            seed=7,
            bankroll_cc=BANKROLL,
            portfolio_cvar_frac=0.35,
            portfolio_det_max_frac=None,
            portfolio_ruin_prob_budget=None,
            tail_prob_gate=True,
            kill_tail_prob=KILL_TAIL_PROB,
            kill_anchored_book_gate=True,
            hard_trip_frac=float(KILL_FRAC),
            kill_gate_marginal=True,
        )
        assert verdict.confirm is False
        assert verdict.decline_reason == "kill_marginal_raises_p_kill"
