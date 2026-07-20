"""CERTIFIED-HEDGE EV BUDGET (2026-07-18) — sim/book_risk._candidate_gate.

``allow_negative_ev_hedge`` used to admit ANY negative-EV fill within
``hedge_cost_budget_cc`` with NO verification that the fill hedges — arming it
would have paid the sniper tax on every stale quote. The gate now requires the
candidate be CERTIFIED risk-reducing: POST governing model UNCLAMPED expected
tail loss <= PRE, both scored on the SAME common-random-numbers sample.
UNCLAMPED (2026-07-18 verify fix): the clamped governing ES_0.99 is exactly
0.0 on any book whose worst-1% sampled outcome is still net-profitable (a
fresh post-settlement book, any small early book of +EV fills), so a
clamped-ES comparison degenerated to 0 <= 0 there and re-admitted the sniper
tax the certification exists to exclude; the unclamped number makes eroding
the tail profit cushion count against the candidate, equals the clamped ES in
the genuine-loss regime, and still admits a hedge that GROWS the cushion.
(The spec's second clause — post det-max <= pre det-max — is provably degenerate
on a sell-only book: the deterministic all-hit maximum is comonotone-ADDITIVE
(P0-3), so post det == pre det + candidate premium + fee on every real fill;
det-max stays enforced against its ABSOLUTE budget instead. Flagged in the gate
comment + the session report.)

Mandated tests: certified-reducing negative-EV within budget => confirm;
non-reducing negative-EV => decline REGARDLESS of budget; positive-EV
unaffected; the PROFIT-CLAMPED-TAIL regression (an independent-game negative-EV
snipe on a book whose 1% tail is a profit must decline, and a true tail-cushion
hedge there must still certify). Plus the lifecycle/config wiring (RiskConfig ->
build_lifecycle_config -> LifecycleConfig -> CandidateBookRiskInputs) with the
default-disabled safety preserved, and validator coverage.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from combomaker.core.conventions import Side
from combomaker.core.quantity import CentiContracts
from combomaker.ops.config import RiskConfig
from combomaker.ops.quote_app import build_lifecycle_config
from combomaker.rfq.lifecycle import LifecycleConfig
from combomaker.risk.exposure import LegRef, OpenPosition
from combomaker.sim.book_risk import evaluate_candidate_book_risk


def _pos(
    position_id: str,
    legs: tuple[LegRef, ...],
    *,
    our_side: Side = Side.NO,
    contracts: int = 100,
    price_cc: int = 5_000,
) -> OpenPosition:
    return OpenPosition(
        position_id=position_id,
        combo_ticker=f"COMBO-{position_id}",
        collection=None,
        our_side=our_side,
        contracts=CentiContracts(contracts),
        entry_price_cc=price_cc,  # type: ignore[arg-type]
        legs=legs,
    )


def _leg(ticker: str, event: str, side: str = "yes") -> LegRef:
    return LegRef(market_ticker=ticker, event_ticker=event, side=side)


def _reducing_hedge() -> tuple[list[OpenPosition], OpenPosition]:
    """Committed NO on a rarely-hitting parlay + a negative-EV LONG-YES on the
    SAME leg bought expensively: it pays exactly when the committed NO loses,
    so the POST tail is measurably below the PRE tail (certified reducing) —
    while its EV is negative (the price of the insurance)."""
    legs = (_leg("A", "KXWCGAME-G1"),)
    committed = [_pos("c1", legs, our_side=Side.NO, contracts=200, price_cc=2_000)]
    hedge = _pos("hedge", legs, our_side=Side.YES, contracts=200, price_cc=9_500)
    return committed, hedge


def _non_reducing_sniper() -> tuple[list[OpenPosition], OpenPosition]:
    """Committed NO + a candidate NO on the SAME leg bought ABOVE its fair
    miss-value (the stale-quote sniper shape — our resting no_bid overpays a
    moved book): negative EV AND it CONCENTRATES the tail — post ES > pre ES,
    so no budget may admit it."""
    legs = (_leg("A", "KXWCGAME-G1"),)
    committed = [_pos("c1", legs, our_side=Side.NO, contracts=200, price_cc=2_000)]
    sniper = _pos("snipe", legs, our_side=Side.NO, contracts=200, price_cc=9_000)
    return committed, sniper


class TestCertifiedHedgeGate:
    def test_certified_reducing_negative_ev_within_budget_confirms(self) -> None:
        committed, hedge = _reducing_hedge()
        probe = evaluate_candidate_book_risk(
            committed, hedge, marginals=lambda t: 0.05, n_samples=40_000, seed=2
        )
        assert probe.candidate_ev_cc < 0.0
        # CERTIFIED: the hedge measurably shrinks the tail on common randoms.
        assert (
            probe.post.governing_model_es_99_cc
            <= probe.pre.governing_model_es_99_cc
        )
        r = evaluate_candidate_book_risk(
            committed,
            hedge,
            marginals=lambda t: 0.05,
            n_samples=40_000,
            seed=2,
            allow_negative_ev_hedge=True,
            hedge_cost_budget_cc=int(-probe.candidate_ev_cc) + 1,
        )
        assert r.confirm
        assert r.decline_reason == ""

    def test_non_reducing_negative_ev_declines_regardless_of_budget(self) -> None:
        """THE fix: a negative-EV fill that does NOT shrink the tail (the
        sniper shape) declines even with an effectively unlimited budget."""
        committed, sniper = _non_reducing_sniper()
        r = evaluate_candidate_book_risk(
            committed,
            sniper,
            marginals=lambda t: 0.5,   # NO fair miss-value 5000cc, sold at 900
            n_samples=40_000,
            seed=6,
            allow_negative_ev_hedge=True,
            hedge_cost_budget_cc=10**9,     # no budget can buy admission
        )
        assert r.candidate_ev_cc < 0.0
        assert (
            r.post.governing_model_es_99_cc > r.pre.governing_model_es_99_cc
        )
        assert not r.confirm
        assert r.decline_reason == "negative_ev_not_risk_reducing"

    def test_certified_but_over_budget_still_declines(self) -> None:
        # The budget check survives AFTER certification (ordering pinned).
        committed, hedge = _reducing_hedge()
        r = evaluate_candidate_book_risk(
            committed,
            hedge,
            marginals=lambda t: 0.05,
            n_samples=40_000,
            seed=2,
            allow_negative_ev_hedge=True,
            hedge_cost_budget_cc=1,
        )
        assert not r.confirm
        assert r.decline_reason == "negative_ev_exceeds_hedge_budget"

    def test_disabled_budget_unchanged(self) -> None:
        committed, hedge = _reducing_hedge()
        r = evaluate_candidate_book_risk(
            committed, hedge, marginals=lambda t: 0.05, n_samples=40_000, seed=2
        )
        assert not r.confirm
        assert r.decline_reason == "negative_ev_no_hedge_budget"

    def test_positive_ev_unaffected_by_hedge_knobs(self) -> None:
        # A +EV candidate never enters the exception: identical verdict with
        # the knobs on or off (same seed => same numbers).
        committed = [_pos("c1", (_leg("A", "KXWCGAME-G1"),), our_side=Side.NO)]
        cand = _pos(
            "cand", (_leg("B", "KXWCGAME-G2"),), our_side=Side.NO, price_cc=1_000
        )
        base = evaluate_candidate_book_risk(
            committed, cand, marginals=lambda t: 0.10, n_samples=40_000, seed=7
        )
        armed = evaluate_candidate_book_risk(
            committed,
            cand,
            marginals=lambda t: 0.10,
            n_samples=40_000,
            seed=7,
            allow_negative_ev_hedge=True,
            hedge_cost_budget_cc=10**9,
        )
        assert base.candidate_ev_cc > 0.0
        assert base.confirm and armed.confirm
        assert base.candidate_ev_cc == armed.candidate_ev_cc


def _profitable_tail_book() -> list[OpenPosition]:
    """40 independent-game +EV NO fills at p=0.01: book P&L per scenario is
    320_000 − 10_000×hits cc with hits ~ Binomial(40, 0.01), so even the
    worst-1% sampled outcome is a large net PROFIT — the profit-clamped regime
    where the clamped governing ES is exactly 0.0 (a fresh post-settlement or
    small early book)."""
    return [
        _pos(
            f"b{i}",
            (_leg(f"L{i}", f"KXWCGAME-B{i}"),),
            our_side=Side.NO,
            contracts=100,
            price_cc=2_000,
        )
        for i in range(40)
    ]


class TestProfitClampedTailRegression:
    """2026-07-18 verify fix: the certification must NOT be vacuous when the
    sampled 1% tail is still net-profitable (clamped ES 0 <= 0 admitted every
    candidate there, including fills that hedge nothing)."""

    def test_independent_snipe_on_profitable_book_declines(self) -> None:
        """THE regression: profitable book + an INDEPENDENT-game negative-EV
        snipe (p=0.5 leg overpaid), budget effectively unlimited. Old clamped
        comparison: pre == post == 0.0 => 0 <= 0 ADMITTED. Unclamped: the
        snipe erodes the tail profit cushion => DECLINED."""
        committed = _profitable_tail_book()
        marginals = lambda t: 0.5 if t == "X" else 0.01  # noqa: E731
        # NO on a p=0.5 leg at 7_500cc: EV = 0.5×10_000 − 7_500 = −2_500cc per
        # contract — a pure pickoff on an INDEPENDENT game (hedges nothing).
        snipe = _pos(
            "snipe",
            (_leg("X", "KXWCGAME-SNIPE"),),
            our_side=Side.NO,
            contracts=100,
            price_cc=7_500,
        )
        r = evaluate_candidate_book_risk(
            committed,
            snipe,
            marginals=marginals,
            n_samples=40_000,
            seed=11,
            allow_negative_ev_hedge=True,
            hedge_cost_budget_cc=10**9,     # no budget may buy admission
        )
        assert r.candidate_ev_cc < 0.0
        # Pin the vacuous regime: BOTH clamped governing ES are exactly 0.0 —
        # the old comparison (post_es <= pre_es) would have ADMITTED this.
        assert r.pre.governing_model_es_99_cc == 0.0
        assert r.post.governing_model_es_99_cc == 0.0
        # The unclamped tail is a PROFIT cushion (negative loss) pre and post…
        assert r.pre.governing_model_tail_loss_cc < 0.0
        # …and the snipe ERODES it, so certification fails.
        assert (
            r.post.governing_model_tail_loss_cc
            > r.pre.governing_model_tail_loss_cc
        )
        assert not r.confirm
        assert r.decline_reason == "negative_ev_not_risk_reducing"

    def test_true_hedge_on_profitable_book_still_certifies(self) -> None:
        """The reason the fix compares the UNCLAMPED tail instead of requiring
        pre ES > 0: a genuine insurance fill on a profit-clamped book (one that
        GROWS the tail cushion) must still be admittable within budget."""
        # Profitable base book + one BIG committed NO on a p=0.5 leg X: the 1%
        # tail is the X-hit scenarios, still a net profit (clamped ES 0.0).
        committed = [
            *_profitable_tail_book(),
            _pos(
                "big",
                (_leg("X", "KXWCGAME-BIG"),),
                our_side=Side.NO,
                contracts=1_000,
                price_cc=2_000,
            ),
        ]
        marginals = lambda t: 0.5 if t == "X" else 0.01  # noqa: E731
        # Insurance: LONG-YES on the SAME leg X at 5_500cc — pays exactly when
        # the big NO loses (flattens the X exposure), EV = −5_000cc.
        hedge = _pos(
            "ins",
            (_leg("X", "KXWCGAME-BIG"),),
            our_side=Side.YES,
            contracts=1_000,
            price_cc=5_500,
        )
        r = evaluate_candidate_book_risk(
            committed,
            hedge,
            marginals=marginals,
            n_samples=40_000,
            seed=11,
            allow_negative_ev_hedge=True,
            hedge_cost_budget_cc=20_000,
        )
        assert r.candidate_ev_cc < 0.0
        # Still the profit-clamped regime (the minimal `pre ES > 0`
        # precondition would have BLOCKED this genuine hedge)…
        assert r.pre.governing_model_es_99_cc == 0.0
        assert r.pre.governing_model_tail_loss_cc < 0.0
        # …and the hedge GROWS the tail cushion => certified => admitted.
        assert (
            r.post.governing_model_tail_loss_cc
            <= r.pre.governing_model_tail_loss_cc
        )
        assert r.confirm
        assert r.decline_reason == ""


class TestConfigWiring:
    def test_risk_config_defaults_disabled(self) -> None:
        cfg = RiskConfig()
        assert cfg.allow_negative_ev_hedge is False
        assert cfg.hedge_cost_budget_cc == 0

    def test_lifecycle_config_defaults_disabled(self) -> None:
        cfg = LifecycleConfig()
        assert cfg.allow_negative_ev_hedge is False
        assert cfg.hedge_cost_budget_cc == 0

    def test_budget_validator_rejects_negative(self) -> None:
        assert RiskConfig(hedge_cost_budget_cc=0).hedge_cost_budget_cc == 0
        assert RiskConfig(hedge_cost_budget_cc=5_000).hedge_cost_budget_cc == 5_000
        with pytest.raises(ValidationError):
            RiskConfig(hedge_cost_budget_cc=-1)

    def test_pass_through_reaches_lifecycle_config(self) -> None:
        cfg = build_lifecycle_config(
            RiskConfig(allow_negative_ev_hedge=True, hedge_cost_budget_cc=5_000)
        )
        assert cfg.allow_negative_ev_hedge is True
        assert cfg.hedge_cost_budget_cc == 5_000
        default = build_lifecycle_config(RiskConfig())
        assert default.allow_negative_ev_hedge is False
        assert default.hedge_cost_budget_cc == 0


async def test_gate_inputs_carry_the_armed_budget(tmp_path: Path) -> None:
    """The lifecycle seam: ``_build_candidate_gate_inputs`` ships the config's
    hedge knobs into the off-loop ``CandidateBookRiskInputs`` (the old code
    hardcoded disabled/0 — a YAML knob that stopped there would be dead)."""
    from dataclasses import replace

    from combomaker.core.conventions import Side as _Side
    from tests.test_candidate_gate_wiring import _make_rig
    from tests.test_lifecycle import rfq as _rfq

    rig = await _make_rig(tmp_path)
    lifecycle = rig.lifecycle
    lifecycle._config = replace(  # noqa: SLF001 — test seam
        lifecycle._config,  # noqa: SLF001
        allow_negative_ev_hedge=True,
        hedge_cost_budget_cc=1_234,
    )
    await lifecycle.handle_rfq(_rfq())
    open_quotes = lifecycle._open  # noqa: SLF001
    assert len(open_quotes) == 1
    quote_id, state = next(iter(open_quotes.items()))
    state.pending_fill = (_Side.YES, state.constructed.yes_bid_cc, state.risk_qty)
    inputs = lifecycle._build_candidate_gate_inputs(quote_id, state)  # noqa: SLF001
    assert inputs.allow_negative_ev_hedge is True
    assert inputs.hedge_cost_budget_cc == 1_234
    # And at defaults the shipped inputs stay disabled (byte-identical safety).
    lifecycle._config = replace(  # noqa: SLF001
        lifecycle._config,  # noqa: SLF001
        allow_negative_ev_hedge=False,
        hedge_cost_budget_cc=0,
    )
    inputs_default = lifecycle._build_candidate_gate_inputs(  # noqa: SLF001
        quote_id, state
    )
    assert inputs_default.allow_negative_ev_hedge is False
    assert inputs_default.hedge_cost_budget_cc == 0
