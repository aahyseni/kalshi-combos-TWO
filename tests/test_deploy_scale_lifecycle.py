"""Deployment-scale WIRING against the real lifecycle (operator LEVER #1).

What the pure-solver tests cannot prove, proved here on the live wiring:

  1. DISARMED is inert — the solve never runs and every check sees 1.0.
  2. The solve runs OFF the quote path (the maintenance tick launches it; the
     hot path only READS a float) and lands on a scale the LIVE checker
     actually graded clean against the LIVE MC.
  3. BOOK-GROWTH DECAY — a fill/settlement between solves is CHARGED against
     the measured headroom (scale x solved_premium / live_premium), reaching
     exactly 1.0 once the book has grown into the whole solved envelope, and
     never inflating above the solved value when the book shrinks.
  4. Every failure path (no positions, no bankroll, an exploding solve) lands on
     1.0 — never larger.
  5. The scale a solve returns is one the checker graded CLEAN, so arming it
     cannot brick quoting (the operator's standing SHIP rule, in miniature).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from combomaker.ops.persistence import Store
from combomaker.rfq.lifecycle import LifecycleConfig, QuoteLifecycle
from combomaker.risk.limits import LimitChecker, RiskLimits
from tests.test_book_risk_offloop import _build, _FakePool, _position
from tests.test_filters import Harness


@pytest.fixture()
async def harness(tmp_path: Path) -> tuple[Harness, Store]:
    from tests.test_pricing_engine import seed_event

    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / "t.sqlite3", h.clock)
    return h, store


def _arm(lifecycle: QuoteLifecycle, **kw: object) -> None:
    """Arm the feature on an already-built lifecycle (LifecycleConfig is a plain
    dataclass; the harness builds it with defaults)."""
    cfg = lifecycle._config
    for k, v in {
        "deploy_scale_enabled": True,
        "deploy_scale_s_max": 3.0,
        "deploy_scale_grid_points": 4,
        "deploy_scale_mc_samples": 2_000,
        **kw,
    }.items():
        object.__setattr__(cfg, k, v)


# --------------------------------------------------------------- 1. disarmed


async def test_disarmed_is_inert(harness: tuple[Harness, Store]) -> None:
    h, store = harness
    lifecycle, exposure = _build(
        h, store, bankroll_cc=100_000_000_000, book_risk_pool=None
    )
    exposure.add_position(_position("held"))
    assert lifecycle._config.deploy_scale_enabled is False
    lifecycle.solve_deploy_scale()                  # explicit call: still a no-op
    assert lifecycle.deploy_scale_for_check() == 1.0
    assert lifecycle._deploy_scale.evaluations == 0  # not one MC was spent


async def test_disarmed_maintenance_hook_never_touches_the_throttle(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    lifecycle, exposure = _build(
        h, store, bankroll_cc=100_000_000_000, book_risk_pool=None
    )
    exposure.add_position(_position("held"))
    lifecycle._maybe_solve_deploy_scale()
    assert lifecycle._deploy_scale_refresh_mono_ns is None


# ----------------------------------------------------------------- 2. solves


async def test_solve_lands_on_a_scale_the_live_checker_graded_clean(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    # A bankroll big enough that a 1x book is comfortably inside every cap, so
    # there IS real headroom to find.
    lifecycle, exposure = _build(
        h, store, bankroll_cc=100_000_000_000, book_risk_pool=None
    )
    _arm(lifecycle)
    exposure.add_position(_position("held"))
    lifecycle.solve_deploy_scale()
    res = lifecycle._deploy_scale
    assert res.solved
    assert res.scale >= 1.0
    assert res.evaluations >= 1
    assert res.book_generation == exposure.position_generation
    # The answer is a rung the checker itself graded clean — re-grade it.
    positions = list(exposure.positions.values())
    ok, why = lifecycle._deploy_scale_feasible(
        res.scale, positions, 100_000_000_000, exposure.position_generation
    )
    assert ok, why
    assert lifecycle.deploy_scale_for_check() == res.scale


async def test_tight_bankroll_cannot_manufacture_headroom(
    harness: tuple[Harness, Store],
) -> None:
    # A book already pressing its caps must NOT produce a scale > 1: the walls
    # are what bound the solve, so a full book solves to exactly 1.0.
    h, store = harness
    lifecycle, exposure = _build(h, store, bankroll_cc=1_000, book_risk_pool=None)
    _arm(lifecycle)
    exposure.add_position(_position("held", contracts=100_000))
    lifecycle.solve_deploy_scale()
    assert lifecycle.deploy_scale_for_check() == 1.0


async def test_offloop_solve_uses_the_pool(harness: tuple[Harness, Store]) -> None:
    h, store = harness
    pool = _FakePool()
    lifecycle, exposure = _build(
        h, store, bankroll_cc=100_000_000_000, book_risk_pool=pool
    )
    _arm(lifecycle)
    exposure.add_position(_position("held"))
    await lifecycle.solve_deploy_scale_offloop()
    assert pool.calls >= 1                       # every MC ran in the worker
    assert lifecycle._deploy_scale.solved
    assert lifecycle.deploy_scale_for_check() >= 1.0


# ------------------------------------------------------ 3. generation safety


async def test_book_growth_decays_the_scale(harness: tuple[Harness, Store]) -> None:
    """A fill between solves is CHARGED against the measured headroom: the book
    doubling halves the remaining scale. (The first cut invalidated the scale on
    any generation change — but a RESERVATION bumps that counter, so on live
    flow the scale collapsed within one accept and the feature measured headroom
    it could never spend. Found by the live ship gate 2026-07-27.)"""
    h, store = harness
    lifecycle, exposure = _build(
        h, store, bankroll_cc=100_000_000_000, book_risk_pool=None
    )
    _arm(lifecycle)
    exposure.add_position(_position("held"))
    lifecycle.solve_deploy_scale()
    solved = lifecycle.deploy_scale_for_check()
    assert solved > 1.0
    exposure.add_position(_position("new_fill"))   # identical size ⇒ book x2
    assert lifecycle.deploy_scale_for_check() == pytest.approx(solved / 2.0)


async def test_growth_into_the_whole_envelope_reaches_exactly_one(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    lifecycle, exposure = _build(
        h, store, bankroll_cc=100_000_000_000, book_risk_pool=None
    )
    _arm(lifecycle)
    exposure.add_position(_position("held"))
    lifecycle.solve_deploy_scale()
    s = lifecycle._deploy_scale.scale
    # Grow the book PAST the solved multiple: the remaining room is exactly 1.0
    # (never below — the enforced caps own refusal from there).
    for i in range(int(s) + 2):
        exposure.add_position(_position(f"grow{i}"))
    assert lifecycle.deploy_scale_for_check() == 1.0


async def test_unstamped_premium_is_failsafe(harness: tuple[Harness, Store]) -> None:
    from dataclasses import replace as dc_replace

    h, store = harness
    lifecycle, exposure = _build(
        h, store, bankroll_cc=100_000_000_000, book_risk_pool=None
    )
    _arm(lifecycle)
    exposure.add_position(_position("held"))
    lifecycle.solve_deploy_scale()
    lifecycle._deploy_scale = dc_replace(
        lifecycle._deploy_scale, solved_premium_cc=0
    )
    assert lifecycle.deploy_scale_for_check() == 1.0


async def test_shrinking_book_never_inflates_the_scale(
    harness: tuple[Harness, Store],
) -> None:
    """A settlement SHRINKS the book. The decay must never turn that into a
    LARGER scale than was solved (the ratio is clamped at the solved value)."""
    h, store = harness
    lifecycle, exposure = _build(
        h, store, bankroll_cc=100_000_000_000, book_risk_pool=None
    )
    _arm(lifecycle)
    exposure.add_position(_position("held"))
    exposure.add_position(_position("settles"))
    lifecycle.solve_deploy_scale()
    s = lifecycle._deploy_scale.scale
    exposure.remove_position("settles")
    assert lifecycle.deploy_scale_for_check() == s


# ------------------------------------------------------------ 4. fail-safes


async def test_empty_book_is_failsafe(harness: tuple[Harness, Store]) -> None:
    h, store = harness
    lifecycle, _ = _build(h, store, bankroll_cc=100_000_000_000, book_risk_pool=None)
    _arm(lifecycle)
    lifecycle.solve_deploy_scale()
    assert lifecycle.deploy_scale_for_check() == 1.0
    assert not lifecycle._deploy_scale.solved


async def test_no_bankroll_is_failsafe(harness: tuple[Harness, Store]) -> None:
    h, store = harness
    lifecycle, exposure = _build(h, store, bankroll_cc=0, book_risk_pool=None)
    _arm(lifecycle)
    exposure.add_position(_position("held"))
    lifecycle.solve_deploy_scale()
    assert lifecycle.deploy_scale_for_check() == 1.0


async def test_exploding_solve_is_failsafe(harness: tuple[Harness, Store]) -> None:
    h, store = harness
    lifecycle, exposure = _build(
        h, store, bankroll_cc=100_000_000_000, book_risk_pool=None
    )
    _arm(lifecycle)
    exposure.add_position(_position("held"))
    lifecycle.solve_deploy_scale()
    assert lifecycle.deploy_scale_for_check() > 1.0     # a good scale is in force

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("MC exploded")

    lifecycle._deploy_scale_feasible = boom            # type: ignore[assignment]
    lifecycle.solve_deploy_scale()
    assert lifecycle.deploy_scale_for_check() == 1.0   # collapses, never larger


async def test_offloop_exploding_pool_is_failsafe(
    harness: tuple[Harness, Store],
) -> None:
    class _Boom:
        calls = 0

        async def run(self, inputs: object) -> object:
            raise RuntimeError("worker died")

    h, store = harness
    lifecycle, exposure = _build(
        h, store, bankroll_cc=100_000_000_000, book_risk_pool=_Boom()
    )
    _arm(lifecycle)
    exposure.add_position(_position("held"))
    await lifecycle.solve_deploy_scale_offloop()
    assert lifecycle.deploy_scale_for_check() == 1.0


# --------------------------------------------- 5. the scale cannot brick caps


async def test_armed_scale_never_creates_a_decline(
    harness: tuple[Harness, Store],
) -> None:
    """The operator's SHIP rule in miniature: whatever the solve returns, the
    breach set the checker produces at that scale is a SUBSET of the breach set
    at 1.0 — arming the scale can only ever REMOVE a decline, never add one."""
    from combomaker.risk.limits import DailyPnl

    h, store = harness
    lifecycle, exposure = _build(
        h, store, bankroll_cc=100_000_000_000, book_risk_pool=None
    )
    _arm(lifecycle)
    exposure.add_position(_position("held"))
    lifecycle.solve_deploy_scale()
    s = lifecycle.deploy_scale_for_check()
    checker: LimitChecker = lifecycle._limits
    base = {
        b.reason
        for b in checker.check(
            exposure, lifecycle._marginals, DailyPnl(0, 0),
            risk_bankroll_cc=100_000_000_000,
        )
    }
    scaled = {
        b.reason
        for b in checker.check(
            exposure, lifecycle._marginals, DailyPnl(0, 0),
            risk_bankroll_cc=100_000_000_000, deploy_scale=s,
        )
    }
    assert scaled <= base


def test_lifecycle_config_defaults_are_off() -> None:
    cfg = LifecycleConfig()
    assert cfg.deploy_scale_enabled is False
    assert cfg.deploy_scale_s_max > 1.0          # a SEARCH ceiling, not a target
    assert RiskLimits().portfolio_kill_tail_prob == 0.02   # the ratified anchor
