"""P1-2 (RISK_ENGINE_AUDIT_ACTION_PLAN.txt P1 item 2): confidence bounds /
adaptive sample counts near the ruin budget, plus common random numbers for
candidate comparisons.

``p_ruin`` is a Monte-Carlo estimate ``p̂ = k/n`` of a binomial proportion, so
near the ruin budget it carries sampling error: p̂ may sit just under the budget
while the TRUE ruin probability is over it. Gating on the point estimate would
then admit a fill whose ruin risk is only statistically-indistinguishable-from-
safe. The fail-closed fix (hard rule 6) gates on the one-sided Wilson UPPER
confidence bound instead. These tests pin:

  * the pure Wilson-upper / adaptive-sample-count math (monotonicity, the z==0
    identity, small-p behaviour, the closed-form value);
  * that ``compute_book_risk`` reports ``p_ruin_upper`` == p̂ at z==0 and STRICTLY
    above it at z>0 (the interval genuinely widens);
  * that ``evaluate_candidate_book_risk`` DECLINES a candidate whose ruin p̂ only
    just clears the budget once a positive confidence level is required — while
    the SAME candidate confirms on the point estimate (z==0);
  * that common random numbers still make the pre/post comparison exact (the P1-2
    change did not disturb the CRN determinism the candidate evaluator relies on).
"""

from __future__ import annotations

import math

from combomaker.core.conventions import Side
from combomaker.core.quantity import CentiContracts
from combomaker.risk.exposure import LegRef, OpenPosition
from combomaker.sim.book_model import build_book_model
from combomaker.sim.book_risk import (
    compute_book_risk,
    evaluate_candidate_book_risk,
    ruin_samples_for_precision,
    wilson_upper_bound,
)


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


class TestWilsonUpperBound:
    def test_z_zero_is_identity(self) -> None:
        # No confidence widening ⇒ the bound is exactly the point estimate. This is
        # the default everywhere, so the whole risk engine's behaviour is unchanged
        # unless an operator opts into a positive z.
        for p in (0.0, 0.01, 0.05, 0.5, 0.99, 1.0):
            assert wilson_upper_bound(p, 20_000, 0.0) == p

    def test_zero_or_negative_n_is_identity(self) -> None:
        # Nothing sampled ⇒ no interval; the ruin cap does not evaluate anyway.
        assert wilson_upper_bound(0.03, 0, 1.645) == 0.03
        assert wilson_upper_bound(0.03, -5, 1.645) == 0.03

    def test_upper_bound_strictly_above_phat_for_positive_z(self) -> None:
        # A genuine one-sided upper confidence bound is strictly above p̂ (finite n,
        # positive z) — even at p̂ == 0, where a Wald interval would collapse to 0.
        assert wilson_upper_bound(0.0, 10_000, 1.645) > 0.0
        assert wilson_upper_bound(0.04, 10_000, 1.645) > 0.04

    def test_bound_tightens_toward_phat_as_n_grows(self) -> None:
        # More samples ⇒ tighter interval ⇒ the upper bound descends monotonically
        # toward p̂ (the adaptive-sample lever: sample more to trust a near-budget
        # estimate).
        p = 0.04
        widths = [
            wilson_upper_bound(p, n, 1.645) - p for n in (1_000, 10_000, 100_000)
        ]
        assert widths[0] > widths[1] > widths[2] > 0.0

    def test_bound_widens_with_z(self) -> None:
        # A higher confidence level ⇒ a wider (more conservative) upper bound.
        p, n = 0.04, 10_000
        assert (
            wilson_upper_bound(p, n, 2.326)  # 99% one-sided
            > wilson_upper_bound(p, n, 1.645)  # 95% one-sided
            > wilson_upper_bound(p, n, 1.0)
            > p
        )

    def test_closed_form_value(self) -> None:
        # Pin the exact Wilson one-sided upper bound at a known point so a future
        # refactor cannot silently change the formula.
        p, n, z = 0.05, 400, 1.645
        z2 = z * z
        denom = 1.0 + z2 / n
        centre = (p + z2 / (2.0 * n)) / denom
        half = (z / denom) * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
        assert wilson_upper_bound(p, n, z) == min(1.0, centre + half)

    def test_bound_capped_at_one(self) -> None:
        # A tiny n with p̂ == 1 saturates the centre+halfwidth above 1 ⇒ clamped.
        assert wilson_upper_bound(1.0, 3, 3.0) == 1.0
        # And it is never allowed to exceed 1 for any input.
        assert wilson_upper_bound(0.999, 50, 3.0) <= 1.0


class TestAdaptiveSampleCount:
    def test_no_widening_requested_returns_zero(self) -> None:
        assert ruin_samples_for_precision(0.05, 0.01, 0.0) == 0
        assert ruin_samples_for_precision(0.05, 0.0, 1.645) == 0

    def test_matches_wald_formula(self) -> None:
        p, target, z = 0.05, 0.005, 1.645
        expected = math.ceil((z * z * p * (1.0 - p)) / (target * target))
        assert ruin_samples_for_precision(p, target, z) == expected

    def test_tighter_target_demands_more_samples(self) -> None:
        # Halving the target half-width roughly quadruples the required n.
        loose = ruin_samples_for_precision(0.05, 0.01, 1.645)
        tight = ruin_samples_for_precision(0.05, 0.005, 1.645)
        assert tight > 3.5 * loose

    def test_achieved_bound_meets_target(self) -> None:
        # The n it prescribes delivers a Wilson half-width CLOSE to target — the
        # count solves the large-n Wald approximation, which the exact Wilson width
        # converges to from slightly above (the extra z²/4n² term), so the achieved
        # width is target plus a small O(1/n) slack, not a gross overshoot. This
        # pins that the count is the right ORDER (a conservative guide, not exact).
        p, target, z = 0.05, 0.004, 1.645
        n = ruin_samples_for_precision(p, target, z)
        achieved = wilson_upper_bound(p, n, z) - p
        assert target <= achieved <= 1.1 * target


class TestComputeBookRiskReportsUpperBound:
    def _snapshot(self, z: float):
        # A NO-seller book on a frequently-hitting parlay so p_ruin is materially
        # above zero (the interval is meaningful).
        pos = _pos(
            "p1",
            (_leg("A", "KXWCGAME-G1"),),
            our_side=Side.NO,
            contracts=200,
            price_cc=8_000,
        )
        model = build_book_model([pos], marginals=lambda t: 0.9)
        return compute_book_risk(
            model,
            n_samples=20_000,
            seed=3,
            bankroll_cc=200_000,
            current_equity_cc=145_000,  # floor 140_000 ⇒ ruin evaluates
            ruin_floor_frac=0.70,
            ruin_prob_ci_z=z,
        )

    def test_z_zero_upper_equals_point_estimate(self) -> None:
        snap = self._snapshot(0.0)
        assert snap.p_ruin > 0.0
        assert snap.p_ruin_upper == snap.p_ruin

    def test_positive_z_widens_reported_upper(self) -> None:
        snap = self._snapshot(1.645)
        assert snap.p_ruin_upper > snap.p_ruin
        # p̂ itself (the reported point estimate) is untouched by the z knob — only
        # the gated upper bound moves.
        assert self._snapshot(0.0).p_ruin == snap.p_ruin


class TestCandidateRuinConfidenceGate:
    def _near_budget_inputs(self):
        # A committed NO plus a same-game +EV-but-concentrating candidate whose POST
        # ruin p̂ lands JUST above / around a chosen budget. We size it so the point
        # estimate clears the budget but the confidence bound does not.
        leg = (_leg("A", "KXWCGAME-G1"),)
        committed = [_pos("c1", leg, our_side=Side.NO, contracts=200, price_cc=900)]
        cand = _pos("cand", leg, our_side=Side.NO, contracts=400, price_cc=900)
        return committed, cand

    def test_point_estimate_passes_confidence_bound_declines(self) -> None:
        committed, cand = self._near_budget_inputs()
        common = dict(
            marginals=lambda t: 0.90,
            n_samples=40_000,
            seed=3,
            bankroll_cc=200_000,
            current_equity_cc=145_000,  # floor 140_000 ⇒ gap 5_000
            ruin_floor_frac=0.70,
        )
        # Measure the POST ruin p̂ under the point estimate, then set a budget a hair
        # ABOVE it so the point-estimate gate passes but the upper bound crosses.
        probe = evaluate_candidate_book_risk(
            committed, cand, ruin_prob_ci_z=0.0, **common
        )
        assert probe.post.p_ruin > 0.0
        budget = probe.post.p_ruin + 1e-4  # just above the point estimate
        # z == 0: the point estimate is under budget ⇒ the ruin gate does NOT fire
        # (any decline must be for another reason; this book's EV is positive and
        # no other budget is supplied, so it confirms).
        passes = evaluate_candidate_book_risk(
            committed,
            cand,
            ruin_prob_ci_z=0.0,
            portfolio_ruin_prob_budget=budget,
            **common,
        )
        assert passes.confirm
        # z > 0: the UPPER confidence bound exceeds the same budget ⇒ fail-closed
        # decline on the ruin axis (the near-budget estimate is not trusted).
        declines = evaluate_candidate_book_risk(
            committed,
            cand,
            ruin_prob_ci_z=1.645,
            portfolio_ruin_prob_budget=budget,
            **common,
        )
        assert not declines.confirm
        assert declines.decline_reason == "post_ruin_prob_over_budget"
        # And the upper bound the gate used is strictly above the point estimate.
        assert declines.post.p_ruin_upper > declines.post.p_ruin

    def test_generous_budget_confirms_even_with_confidence(self) -> None:
        # When there is real headroom, the confidence bound is still under budget ⇒
        # the fill confirms; the widening is not a blanket tightening, only a
        # near-budget guard.
        committed, cand = self._near_budget_inputs()
        r = evaluate_candidate_book_risk(
            committed,
            cand,
            marginals=lambda t: 0.90,
            n_samples=40_000,
            seed=3,
            bankroll_cc=200_000,
            current_equity_cc=145_000,
            ruin_floor_frac=0.70,
            ruin_prob_ci_z=1.645,
            portfolio_ruin_prob_budget=0.95,  # enormous headroom
        )
        assert r.confirm


class TestCommonRandomNumbersPreserved:
    def test_crn_determinism_unchanged(self) -> None:
        # The P1-2 change must not disturb the common-random-number determinism the
        # candidate evaluator relies on: same seed ⇒ identical verdict, and the
        # upper bound is deterministic too.
        committed = [_pos("c1", (_leg("A", "KXWCGAME-G1"),), our_side=Side.NO)]
        cand = _pos("cand", (_leg("B", "KXWCGAME-G2"),), our_side=Side.NO)
        common = dict(
            marginals=lambda t: 0.6,
            n_samples=20_000,
            seed=5,
            bankroll_cc=200_000,
            current_equity_cc=145_000,
            ruin_prob_ci_z=1.645,
        )
        a = evaluate_candidate_book_risk(committed, cand, **common)
        b = evaluate_candidate_book_risk(committed, cand, **common)
        assert a.post.p_ruin == b.post.p_ruin
        assert a.post.p_ruin_upper == b.post.p_ruin_upper
        assert a.confirm == b.confirm
