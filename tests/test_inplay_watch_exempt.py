"""IN-PLAY WATCH EXEMPTION for the marginal-jump breaker (2026-07-19 live).

The failure this pins: through the WC final, in-play ESPARG books went dark
mid-game (traders pull quotes / the book empties in-play) and the marginal
watch tripped ``halt_marginal_jump`` "became unreadable" 45 times — 8 hard
halts through the final — on markets that were LIVE (not settled), so the
settled-watch exemption could not cover them. Nothing behind those halts was
actionable: the pregame gate had already stopped quoting the game, resting
quotes die via cancel-on-invalidate, and confirms via last-look freshness. On
an MLB/WNBA nightly slate the first game going in-play would halt quoting on
every OTHER pregame game, every night.

The fix under test:

1. ``BreakerInputs.inplay_tickers``: legs in the set are skipped by the
   jump/readability watch (baseline purged) — an in-play book going dark or
   gapping on a goal is normal, not the dead-feed signature. Legs NOT in the
   set keep the exact pre-fix fail-closed watch (load-bearing tests).
2. ``RfqFilter.leg_inplay_watch_exempt``: True iff the leg's game has STARTED
   per the SAME start-time ladder the pregame gate stops quoting on. The
   polarity contract: UNKNOWN start ⇒ False (keep the watch);
   ``allow_inplay_legs`` ⇒ False (never blind a leg we can still quote).
3. ``QuoteLifecycle.inplay_watch_exempt`` delegates to the filter; the breaker
   sampler (``_book_leg_signals``) carries the set for exactly the book legs.
"""

from __future__ import annotations

from pathlib import Path

from combomaker.core.clock import FakeClock
from combomaker.core.reasons import ReasonCode
from combomaker.ops.config import FiltersConfig
from combomaker.risk.breakers import (
    BreakerInputs,
    BreakerThresholds,
    CircuitBreakers,
)
from combomaker.risk.exposure import ExposureBook
from combomaker.risk.killswitch import KillSwitch
from tests.test_filters import Harness
from tests.test_lifecycle import TEST_CONVENTIONS
from tests.test_settled_marginals import (
    FRA_WIN,
    _build,
    _cross_game_position,
    harness,  # noqa: F401 — pytest fixture import
)

# Embedded-ET precise start (verified KXMLB path): 2026-07-10 19:15 ET.
MLB_TICKER = "KXMLBGAME-26JUL101915BOSNYM-BOS"

# Harness NOW = 2026-07-05 12:00 UTC; with_meta close = NOW+6h, no expiration ⇒
# the pregame ESTIMATE start = close − 4.5h = NOW + 1.5h.
ESTIMATE_START_S = 1.5 * 3600.0


def _breaker_rig(clock: FakeClock) -> tuple[CircuitBreakers, KillSwitch]:
    killswitch = KillSwitch(clock)
    return CircuitBreakers(killswitch, BreakerThresholds(), clock), killswitch


def _tick(
    marginals: dict[str, float | None],
    inplay: frozenset[str] = frozenset(),
) -> BreakerInputs:
    return BreakerInputs(rx_age_s=0.1, marginals=marginals, inplay_tickers=inplay)


IP = "KXWCGAME-26JUL19ESPARG-TIE"  # the live shape: in-play leg going dark


# --------------------------------------------------------------------------- #
# 1. Breaker-level exemption.                                                  #
# --------------------------------------------------------------------------- #


class TestBreakerInplayExemption:
    async def test_inplay_book_going_dark_never_halts(self) -> None:
        # The exact 2026-07-19 shape: a live echo seeds the baseline (0.919),
        # the game goes in-play, the book leaves the feed — held far past the
        # 30s sustained grace, never a trip, never a halt, baseline purged.
        clock = FakeClock()
        breakers, killswitch = _breaker_rig(clock)
        v1 = await breakers.evaluate_and_halt(_tick({IP: 0.919}))
        assert v1.tripped is False
        exempt = frozenset({IP})
        for _ in range(4):
            verdict = await breakers.evaluate_and_halt(_tick({IP: None}, exempt))
            assert verdict.tripped is False
            clock.advance(31.0)
        assert killswitch.halted is False
        assert IP not in breakers._last_marginal  # noqa: SLF001 — purged

    async def test_inplay_goal_jump_is_not_a_trip(self) -> None:
        # A goal moves a marginal 0.60 → 0.95 (> max_jump 0.25) — normal
        # in-play behaviour, not a mis-mark.
        clock = FakeClock()
        breakers, killswitch = _breaker_rig(clock)
        assert not (await breakers.evaluate_and_halt(_tick({IP: 0.60}))).tripped
        verdict = await breakers.evaluate_and_halt(
            _tick({IP: 0.95}, frozenset({IP}))
        )
        assert verdict.tripped is False
        assert killswitch.halted is False

    async def test_same_sequences_without_exemption_still_trip(self) -> None:
        # LOAD-BEARING: identical sequences WITHOUT the in-play set keep the
        # exact pre-fix fail-closed contract (the fix is the exemption, not a
        # loosened watch).
        clock = FakeClock()
        breakers, _ks = _breaker_rig(clock)
        assert not (await breakers.evaluate_and_halt(_tick({IP: 0.60}))).tripped
        v_jump = await breakers.evaluate_and_halt(_tick({IP: 0.95}))
        assert v_jump.tripped is True
        assert v_jump.reason is ReasonCode.HALT_MARGINAL_JUMP

        clock2 = FakeClock()
        breakers2, killswitch2 = _breaker_rig(clock2)
        assert not (await breakers2.evaluate_and_halt(_tick({IP: 0.919}))).tripped
        v_hold = await breakers2.evaluate_and_halt(_tick({IP: None}))
        assert v_hold.tripped is True  # grace hold starts
        clock2.advance(31.0)
        await breakers2.evaluate_and_halt(_tick({IP: None}))
        assert killswitch2.halted is True  # sustained ⇒ hard halt (pre-fix)

    async def test_non_exempt_leg_beside_exempt_keeps_full_watch(self) -> None:
        # A pregame leg's dead book still halts even while an in-play leg is
        # exempt beside it — the exemption is per-ticker, never blanket.
        clock = FakeClock()
        breakers, killswitch = _breaker_rig(clock)
        seed = {IP: 0.60, "PREGAME-M": 0.40}
        assert not (await breakers.evaluate_and_halt(_tick(seed))).tripped
        exempt = frozenset({IP})
        v = await breakers.evaluate_and_halt(
            _tick({IP: None, "PREGAME-M": None}, exempt)
        )
        assert v.tripped is True  # PREGAME-M unreadable — grace hold begins
        clock.advance(31.0)
        await breakers.evaluate_and_halt(
            _tick({IP: None, "PREGAME-M": None}, exempt)
        )
        assert killswitch.halted is True

    async def test_returning_book_rebaselines_cleanly(self) -> None:
        # A game leaves the exemption... conservatively: if the set stops
        # carrying the ticker while the book is readable again, the first
        # reading re-seeds the baseline (prev None ⇒ clear), never a phantom
        # jump off the purged pre-game baseline.
        clock = FakeClock()
        breakers, killswitch = _breaker_rig(clock)
        assert not (await breakers.evaluate_and_halt(_tick({IP: 0.10}))).tripped
        await breakers.evaluate_and_halt(_tick({IP: None}, frozenset({IP})))
        v = await breakers.evaluate_and_halt(_tick({IP: 0.90}))  # set no longer carries it
        assert v.tripped is False  # re-baseline, not a 0.10→0.90 jump
        assert killswitch.halted is False


# --------------------------------------------------------------------------- #
# 2. Filter predicate: the start-time ladder + polarity contract.              #
# --------------------------------------------------------------------------- #


class TestFilterPredicate:
    def test_estimate_path_flips_at_estimated_start(self) -> None:
        h = Harness()
        h.with_meta("M1")  # estimate start = NOW + 1.5h
        assert h.filter.leg_inplay_watch_exempt("M1") is False
        h.clock.advance(ESTIMATE_START_S + 1.0)
        assert h.filter.leg_inplay_watch_exempt("M1") is True

    def test_precise_embedded_path_flips_at_start(self) -> None:
        h = Harness()  # NOW = 2026-07-05; MLB game 2026-07-10 19:15 ET
        assert h.filter.leg_inplay_watch_exempt(MLB_TICKER) is False
        h.clock.advance(6 * 24 * 3600.0)  # → 2026-07-11, game started
        assert h.filter.leg_inplay_watch_exempt(MLB_TICKER) is True

    def test_unknown_start_keeps_watch(self) -> None:
        # No metadata, no embedded start ⇒ UNKNOWN ⇒ never exempt (fail-closed).
        h = Harness()
        h.clock.advance(30 * 24 * 3600.0)
        assert h.filter.leg_inplay_watch_exempt("KXWCX-NOMETA") is False

    def test_allow_inplay_legs_disables_exemption(self) -> None:
        # Operator re-enabled in-play quoting: a leg we can still QUOTE must
        # keep the full fail-closed watch even long after its game started.
        h = Harness(FiltersConfig(allow_inplay_legs=True))
        h.with_meta("M1")
        h.clock.advance(ESTIMATE_START_S + 3600.0)
        assert h.filter.leg_inplay_watch_exempt("M1") is False


# --------------------------------------------------------------------------- #
# 3. Lifecycle delegate + sampler wiring.                                      #
# --------------------------------------------------------------------------- #


class TestWiring:
    async def test_lifecycle_delegates_to_filter(
        self, harness: tuple[Harness, object]  # noqa: F811
    ) -> None:
        h, store = harness
        lifecycle, _s, _e = _build(h, store, bankroll_cc=10**11, settled=None)  # type: ignore[arg-type]
        assert lifecycle.inplay_watch_exempt("M1") is False  # pregame
        h.clock.advance(ESTIMATE_START_S + 1.0)
        assert lifecycle.inplay_watch_exempt("M1") is True  # started
        # UNKNOWN start stays un-exempt forever.
        assert lifecycle.inplay_watch_exempt("KXWCX-NOMETA") is False

    def test_sampler_surfaces_inplay_set(self, tmp_path: Path) -> None:
        from tests.test_quote_app_phase6 import (
            FakeFeed,
            FakeLifecycle,
            FakeMetadata,
            _demo_app,
        )

        app = _demo_app(tmp_path)
        exposure = ExposureBook(TEST_CONVENTIONS)
        exposure.add_position(_cross_game_position("held"))
        # FRA_WIN reports BOTH settled and in-play: settled wins (elif) — the
        # sets stay disjoint so telemetry can name which exemption applied.
        lifecycle = FakeLifecycle(
            marginals={FRA_WIN: 1.0, "KXWCTOTAL-26JUL18FRAENG-5": None, "M1": 0.35},
            settled={FRA_WIN},
            inplay={FRA_WIN, "KXWCTOTAL-26JUL18FRAENG-5"},
        )
        feed = FakeFeed(rx_age_s=0.1, warm=True, seq_gap=False)
        inputs = app._sample_breaker_inputs(  # noqa: SLF001 — sampler seam
            feed,  # type: ignore[arg-type]
            lifecycle,  # type: ignore[arg-type]
            exposure,
            FakeMetadata(),  # type: ignore[arg-type]
        )
        assert inputs.settled_tickers == frozenset({FRA_WIN})
        assert inputs.inplay_tickers == frozenset({"KXWCTOTAL-26JUL18FRAENG-5"})
