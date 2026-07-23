"""Live wiring: config flag -> DerivedCapEngine -> LimitChecker swap. The startup
derivation + each periodic tick call `_refresh_adaptive_caps_once` (which touches
no `self`), so we exercise it directly with self=None. The `_adaptive_caps_loop`
is verified only for its off-mode no-op (its periodic body is the same helper)."""
from __future__ import annotations

import asyncio
from fractions import Fraction

from combomaker.ops.config import RiskConfig
from combomaker.ops.quote_app import QuoteApp
from combomaker.risk.derived_cap_engine import DerivedCapEngine
from combomaker.risk.limits import LimitChecker


def _base():
    # Derived caps are base-independent (fully adaptive), so any base works — the
    # enforced slate/trip/per_combo are the policy anchors regardless.
    return RiskConfig().to_risk_limits()


def _refresh(checker, engine, mode) -> None:
    QuoteApp._refresh_adaptive_caps_once(None, checker, engine, mode, 12)  # self unused


def test_config_defaults_are_off_and_conservative() -> None:
    cfg = RiskConfig()
    assert cfg.adaptive_caps_mode == "off"
    assert cfg.adaptive_caps_expected_games == 12


def test_off_mode_is_a_noop_engine_none() -> None:
    base = _base()
    checker = LimitChecker(base)
    # off => cap_engine is None at the call site; loop returns immediately, no swap
    # (rest/game_series unused on that path -> None/() are safe).
    asyncio.run(QuoteApp._adaptive_caps_loop(None, None, checker, None, "off", 12, ()))
    assert checker.limits is base


class _FakeRest:
    def __init__(self, pages):
        self._pages = pages
        self._i = 0

    async def get_markets(self, **_params):
        page = self._pages[min(self._i, len(self._pages) - 1)]
        self._i += 1
        return page


def _count(pages, series=("KXMLBGAME",)):
    return asyncio.run(QuoteApp._count_slate_games(None, _FakeRest(pages), series))


def test_slate_count_is_distinct_game_keys() -> None:
    n = _count([{"markets": [
        {"event_ticker": "KXMLBGAME-26JUL22BOSNYM"},
        {"event_ticker": "KXMLBGAME-26JUL22LADSFG"},
        {"event_ticker": "KXMLBGAME-26JUL22BOSNYM"},   # dup game (2nd market family)
        {"event_ticker": "KXMLBGAME-26JUL22CHCMIL"},
    ], "cursor": ""}])
    assert n == 3


def test_slate_count_paginates() -> None:
    n = _count([
        {"markets": [{"event_ticker": "KXMLBGAME-26JUL22AAA"}], "cursor": "c1"},
        {"markets": [{"event_ticker": "KXMLBGAME-26JUL22BBB"}], "cursor": ""},
    ])
    assert n == 2


def test_slate_count_empty_returns_none_for_fallback() -> None:
    assert _count([{"markets": [], "cursor": ""}]) is None


def test_slate_count_error_returns_none_for_fallback() -> None:
    class _Boom:
        async def get_markets(self, **_):
            raise RuntimeError("markets api down")

    assert asyncio.run(QuoteApp._count_slate_games(None, _Boom(), ("KXMLBGAME",))) is None


def test_enforce_swaps_derived_caps_onto_the_checker() -> None:
    base = _base()
    checker = LimitChecker(base)
    _refresh(checker, DerivedCapEngine(base), "enforce")
    assert checker.limits is not base
    assert checker.limits.slate_loss_frac == Fraction(15, 100)   # bootstrap slate
    assert checker.limits.hard_trip_frac == Fraction(12, 100)    # KILL anchor
    assert checker.limits.per_combo_loss_frac == Fraction(1, 100)


def test_shadow_mode_logs_but_never_swaps() -> None:
    base = _base()
    checker = LimitChecker(base)
    _refresh(checker, DerivedCapEngine(base), "shadow")
    assert checker.limits is base                                # enforcement untouched


def test_refresh_error_keeps_current_limits_fail_safe() -> None:
    base = _base()
    checker = LimitChecker(base)

    class _Boom(DerivedCapEngine):
        def refresh(self, **_):  # type: ignore[override]
            raise RuntimeError("sensor blew up")

    _refresh(checker, _Boom(base), "enforce")
    assert checker.limits is base                                # never widened on a bug
