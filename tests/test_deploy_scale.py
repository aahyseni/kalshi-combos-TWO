"""Tests for combomaker.risk.deploy_scale — the SOLVED deployment scale.

The safety contract these pin (operator, non-negotiable):
  * ``s`` is SOLVED, never configured, and every failure path lands on 1.0 —
    never on a larger size;
  * ``s`` breathes ONLY the deploy-side budgets; the ENVELOPE it was solved
    against (portfolio CVaR / det-max / ruin budget / tail-prob anchor), the
    HALTS and every absolute backstop are invariant;
  * ``deploy_scale=1.0`` through ``LimitChecker.check`` is byte-identical to
    the pre-existing behaviour (the whole feature is inert while disarmed).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields
from fractions import Fraction

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.risk.deploy_scale import (
    DEPLOY_BUDGET_FIELDS,
    ENVELOPE_INVARIANT_FIELDS,
    FAILSAFE,
    DeployScaleResult,
    as_exact,
    scale_deploy_budgets,
    scale_grid,
    solve_deployment_scale,
)
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.limits import DailyPnl, LimitChecker, RiskLimits

CC = CentiCents
Q = CentiContracts

CONVENTIONS = Conventions(
    verified=True,
    source="test",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)


def provider(mapping: dict[str, float]) -> Callable[[str], float | None]:
    return lambda ticker: mapping.get(ticker)


MARG = provider({"A": 0.5, "B": 0.5})
LEG_A = (LegRef("A", "EV1", "yes"),)

ARMED = RiskLimits(
    game_loss_frac=Fraction(8, 100),
    per_combo_loss_frac=Fraction(1, 100),
    entity_loss_frac=Fraction(3, 100),
    directional_frac=Fraction(10, 100),
    slate_loss_frac=Fraction(8, 100),
)


def make_position(pid: str, *, contracts: int = 100) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=f"COMBO-{pid}",
        collection=None,
        our_side=Side.YES,
        contracts=Q(contracts),
        entry_price_cc=CC(5_000),
        legs=LEG_A,
    )


# --------------------------------------------------------------- scale_grid


class TestScaleGrid:
    def test_descending_and_bounded(self) -> None:
        grid = scale_grid(3.0, 4)
        assert grid == (3.0, 2.5, 2.0, 1.5)
        assert list(grid) == sorted(grid, reverse=True)
        assert len(grid) == 4                    # the MC budget IS the point count

    def test_never_includes_one(self) -> None:
        # 1.0 is the solver's own precondition probe, not a ladder rung.
        assert 1.0 not in scale_grid(2.0, 8)

    def test_degenerate_inputs_are_empty(self) -> None:
        assert scale_grid(1.0, 8) == ()
        assert scale_grid(0.5, 8) == ()
        assert scale_grid(3.0, 0) == ()


# ------------------------------------------------------- scale_deploy_budgets


class TestScaleDeployBudgets:
    def test_scale_one_returns_the_same_object(self) -> None:
        # Byte-identical default: not "an equal copy" — literally the same object,
        # so a disarmed run cannot differ from the pre-feature build at all.
        assert scale_deploy_budgets(ARMED, 1.0) is ARMED
        assert scale_deploy_budgets(ARMED, 0.5) is ARMED

    def test_deploy_budgets_scale_exactly(self) -> None:
        out = scale_deploy_budgets(ARMED, 1.5)
        assert out.per_combo_loss_frac == Fraction(1, 100) * Fraction(3, 2)
        assert out.entity_loss_frac == Fraction(3, 100) * Fraction(3, 2)
        assert out.game_loss_frac == Fraction(8, 100) * Fraction(3, 2)
        assert out.slate_loss_frac == Fraction(8, 100) * Fraction(3, 2)
        assert out.directional_frac == Fraction(10, 100) * Fraction(3, 2)

    def test_thresholds_stay_exact_fractions(self) -> None:
        out = scale_deploy_budgets(ARMED, 1.4953)
        for name in DEPLOY_BUDGET_FIELDS:
            val = getattr(out, name)
            assert isinstance(val, Fraction)     # floats are never live thresholds

    def test_envelope_and_halts_are_invariant(self) -> None:
        out = scale_deploy_budgets(ARMED, 2.75)
        for name in ENVELOPE_INVARIANT_FIELDS:
            assert getattr(out, name) == getattr(ARMED, name), name

    def test_only_deploy_fields_move_at_all(self) -> None:
        """The exhaustive version of the above: EVERY field of RiskLimits is
        unchanged except the five named deploy budgets. A future field added to
        RiskLimits can never be silently swept into the scale."""
        out = scale_deploy_budgets(ARMED, 2.0)
        moved = {
            f.name
            for f in fields(ARMED)
            if getattr(out, f.name) != getattr(ARMED, f.name)
        }
        assert moved == set(DEPLOY_BUDGET_FIELDS)

    def test_unarmed_axis_is_never_invented(self) -> None:
        base = RiskLimits(entity_loss_frac=None)
        assert scale_deploy_budgets(base, 2.0).entity_loss_frac is None

    def test_quantization_rounds_down(self) -> None:
        # A solved float becomes a live threshold only through as_exact, which
        # truncates — quantization can only ever make the scale SMALLER.
        assert as_exact(1.4953) == Fraction(1_495_300, 1_000_000)
        assert as_exact(1.9999999) <= Fraction(2)


# --------------------------------------------------- solve_deployment_scale


def graded(**kw: tuple[bool, tuple[str, ...]]) -> dict[float, tuple[bool, tuple[str, ...]]]:
    return {float(k.lstrip("s").replace("_", ".")): v for k, v in kw.items()}


class TestSolve:
    def test_picks_the_largest_feasible_rung(self) -> None:
        g = {1.0: (True, ()), 3.0: (False, ("det",)), 2.5: (False, ("det",)),
             2.0: (True, ())}
        res = solve_deployment_scale(g, s_max=3.0, points=4)
        assert res.scale == 2.0
        assert res.solved
        assert res.binding == ("det",)           # the first wall walking DOWN

    def test_current_book_infeasible_falls_back_to_one(self) -> None:
        g = {1.0: (False, ("skip_portfolio_det_max",)), 2.0: (True, ())}
        res = solve_deployment_scale(g, s_max=3.0, points=4)
        assert res.scale == 1.0
        assert res.binding == ("skip_portfolio_det_max",)
        assert "already refusing" in res.reason

    def test_no_rung_clears_the_envelope(self) -> None:
        g = {1.0: (True, ()), 3.0: (False, ("a",)), 2.5: (False, ("a",)),
             2.0: (False, ("a",)), 1.5: (False, ("a",))}
        res = solve_deployment_scale(g, s_max=3.0, points=4)
        assert res.scale == 1.0
        assert res.solved

    def test_missing_base_probe_is_failsafe(self) -> None:
        res = solve_deployment_scale({2.0: (True, ())}, s_max=3.0, points=4)
        assert res is FAILSAFE
        assert res.scale == 1.0
        assert not res.solved

    def test_ungraded_rung_is_skipped_never_assumed_feasible(self) -> None:
        # A probe whose MC never arrived must not be treated as headroom.
        g = {1.0: (True, ()), 2.0: (True, ())}     # 3.0 and 2.5 never graded
        res = solve_deployment_scale(g, s_max=3.0, points=4)
        assert res.scale == 2.0

    def test_scale_is_never_above_s_max(self) -> None:
        g = {1.0: (True, ()), 3.0: (True, ())}
        res = solve_deployment_scale(g, s_max=3.0, points=4)
        assert res.scale <= 3.0

    def test_never_raises(self) -> None:
        class Exploding(dict):  # type: ignore[type-arg]
            def get(self, *a: object, **k: object) -> None:
                raise RuntimeError("boom")

        res = solve_deployment_scale(Exploding(), s_max=3.0, points=4)
        assert res is FAILSAFE

    def test_failsafe_is_never_larger_than_today(self) -> None:
        assert FAILSAFE.scale == 1.0
        assert FAILSAFE.book_generation == -1     # never matches a live generation


# ------------------------------------------------------- check() integration


class TestCheckIntegration:
    def _book(self, contracts: int) -> ExposureBook:
        book = ExposureBook(CONVENTIONS)
        book.add_position(make_position("p1", contracts=contracts))
        return book

    def test_default_is_byte_identical(self) -> None:
        checker = LimitChecker(ARMED)
        book = self._book(4_000)
        bank = 2_000_0000
        kw = dict(risk_bankroll_cc=bank)
        a = checker.check(book, MARG, DailyPnl(0, 0), **kw)              # type: ignore[arg-type]
        b = checker.check(book, MARG, DailyPnl(0, 0), deploy_scale=1.0, **kw)  # type: ignore[arg-type]
        assert [(x.reason, x.detail) for x in a] == [(x.reason, x.detail) for x in b]

    def test_a_scale_can_only_relax_deploy_caps(self) -> None:
        """Monotone in the safe direction: the breach set at a scale > 1 is a
        SUBSET of the breach set at 1.0 — a scale can never CREATE a breach."""
        checker = LimitChecker(ARMED)
        book = self._book(4_000)
        bank = 2_000_0000
        base = {
            b.reason
            for b in checker.check(book, MARG, DailyPnl(0, 0), risk_bankroll_cc=bank)
        }
        scaled = {
            b.reason
            for b in checker.check(
                book, MARG, DailyPnl(0, 0), risk_bankroll_cc=bank, deploy_scale=2.5
            )
        }
        assert scaled <= base

    def test_scale_below_one_is_ignored(self) -> None:
        checker = LimitChecker(ARMED)
        book = self._book(4_000)
        bank = 2_000_0000
        a = checker.check(book, MARG, DailyPnl(0, 0), risk_bankroll_cc=bank)
        b = checker.check(
            book, MARG, DailyPnl(0, 0), risk_bankroll_cc=bank, deploy_scale=0.4
        )
        assert [x.reason for x in a] == [x.reason for x in b]


class TestResultStamp:
    def test_exact_is_a_fraction(self) -> None:
        res = DeployScaleResult(
            scale=1.4953, solved=True, binding=(), reason="", evaluations=3
        )
        assert isinstance(res.exact, Fraction)
        assert float(res.exact) <= 1.4953
