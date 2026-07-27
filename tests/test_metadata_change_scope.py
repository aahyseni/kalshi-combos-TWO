"""Metadata-change breaker: SETTLEMENT halt vs LIFECYCLE quarantine.

Rebuild of 2026-07-26, after the bot self-halted at 18:15:45Z
(`circuit_breaker_tripped / halt_metadata_change`) on
`KXMLBTOTAL-26JUL261215CLETB-6` for a single field move: `status`
`"active" -> "inactive"` — Kalshi's `deactivated` event, an in-play trading
PAUSE at game end. `close_time`, `event_ticker`, `rules_primary`,
`floor_strike`, `strike_type`, `expiration_time` and `latest_expiration_time`
were all untouched, and the market went on to settle `result="no"` on the same
rule six minutes later. A whole-book kill for one market's lifecycle transition
is the defect; the payoff-changing case must still hard-halt.

Every payload here is a REAL Kalshi market body. `_CLETB_BEFORE` is the exact
`KXMLBTOTAL-26JUL261215CLETB-6` entry from `data/metadata_cache.json`, the
snapshot written 56 seconds before the halt.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from combomaker.core.reasons import ReasonCode
from combomaker.marketdata.metadata import MarketMeta
from combomaker.ops.quote_app import QuoteApp
from combomaker.rfq.filters import RfqFilter
from combomaker.risk.exposure import LegRef
from combomaker.risk.killswitch import HaltEvent, KillSwitch
from combomaker.risk.quarantine import MarketQuarantine
from tests.test_quote_app_phase6 import (
    FakeFeed,
    FakeLifecycle,
    FakeMetadata,
    _book_with_quote_legs,
    _breakers,
    _demo_app,
    _sample,
)

CLETB = "KXMLBTOTAL-26JUL261215CLETB-6"

# The exact cached payload for the market that halted the bot (verbatim from
# data/metadata_cache.json, mtime 18:14:49.534815Z — 56 s pre-halt).
_CLETB_BEFORE: dict[str, Any] = {
    "can_close_early": True,
    "close_time": "2026-07-29T16:15:00Z",
    "created_time": "2026-07-25T20:20:47.946299Z",
    "early_close_condition": "This market will close and expire early if the event occurs.",
    "event_ticker": "KXMLBTOTAL-26JUL261215CLETB",
    "expected_expiration_time": "2026-07-26T19:15:00Z",
    "expiration_time": "2026-07-29T16:15:00Z",
    "expiration_value": "",
    "floor_strike": 5.5,
    "latest_expiration_time": "2026-07-29T16:15:00Z",
    "market_type": "binary",
    "notional_value_dollars": "1.0000",
    "result": "",
    "rules_primary": (
        "If Cleveland and Tampa Bay collectively score more 5.5 runs in the "
        "Cleveland vs Tampa Bay professional baseball game originally scheduled "
        "for Jul 26, 2026 at 12:15 PM EDT, then the market resolves to Yes."
    ),
    "rules_secondary": (
        "Kalshi is not affiliated, associated, authorized, endorsed by, or in any "
        "way officially connected with the Governing League."
    ),
    "settlement_timer_seconds": 60,
    "status": "active",
    "strike_type": "greater",
    "ticker": CLETB,
    "updated_time": "2026-07-25T20:30:00.730149Z",
}

_CLOSE = datetime(2026, 7, 29, 16, 15, tzinfo=UTC)
_EXPECTED_EXPIRY = datetime(2026, 7, 26, 19, 15, tzinfo=UTC)


def _meta(raw: dict[str, Any]) -> MarketMeta:
    """Build a MarketMeta the way MetadataCache.refresh does, from a raw body."""

    def _t(key: str) -> datetime | None:
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    return MarketMeta(
        ticker=str(raw["ticker"]),
        status=str(raw.get("status", "")),
        grid=None,
        event_ticker=raw.get("event_ticker"),
        close_time=_t("close_time"),
        expected_expiration_time=_t("expected_expiration_time"),
        raw=raw,
        fetched_mono_ns=0,
    )


def _mutated(**fields: Any) -> dict[str, Any]:
    raw = copy.deepcopy(_CLETB_BEFORE)
    raw.update(fields)
    return raw


class _FeedStub:
    feed_healthy = True
    rx_age_s = 0.1

    def book(self, ticker: str) -> Any:
        raise KeyError(ticker)  # no book ⇒ SKIP_LEG_UNKNOWN, irrelevant here


def _armed_app(tmp_path: Path) -> QuoteApp:
    """A QuoteApp with the scoped lane ARMED exactly as production arms it —
    by constructing the quote-refusal consumer (RfqFilter) over the app's
    quarantine object."""
    app = _demo_app(tmp_path)
    RfqFilter(
        app._config.filters,
        _FeedStub(),  # type: ignore[arg-type]
        FakeMetadata(),  # type: ignore[arg-type]
        KillSwitch(app._clock, kill_file=app._config.kill_file),
        app._clock,
        None,
        app._market_quarantine,
    )
    assert app._market_quarantine.armed is True
    return app


def _two_samples(
    app: QuoteApp, before: dict[str, Any], after: dict[str, Any]
) -> tuple[Any, Any]:
    """Seed the baseline off ``before``, then sample ``after``. Returns both
    BreakerInputs. The book holds one resting quote on the market."""
    legs = (LegRef(CLETB, "KXMLBTOTAL-26JUL261215CLETB", "yes"),)
    book = _book_with_quote_legs(legs)
    feed = FakeFeed(rx_age_s=0.1, warm=True, seq_gap=False)
    first = _sample(
        app,
        feed,
        lifecycle=FakeLifecycle({CLETB: 0.02}),
        exposure=book,
        metadata=FakeMetadata({CLETB: _meta(before)}),
    )
    second = _sample(
        app,
        feed,
        lifecycle=FakeLifecycle({CLETB: 0.02}),
        exposure=book,
        metadata=FakeMetadata({CLETB: _meta(after)}),
    )
    return first, second


# --------------------------------------------------------------------------- #
# (1) The exact live transition that killed the bot.
# --------------------------------------------------------------------------- #


def test_cletb_inplay_pause_does_not_halt(tmp_path: Path) -> None:
    """THE regression: status active -> inactive on the real CLETB payload, with
    the settlement horizon still 59 minutes in the FUTURE (so no end-of-life
    exemption could have fired), must NOT halt the bot."""
    app = _armed_app(tmp_path)
    breakers = _breakers(app)
    first, second = _two_samples(app, _CLETB_BEFORE, _mutated(status="inactive"))
    assert first.changed_markets == ()  # first sighting seeds the baseline
    assert breakers.evaluate(first).tripped is False
    assert second.changed_markets == ()  # <- the halt that shouldn't have been
    assert breakers.evaluate(second).tripped is False
    # ...and the SCOPED response fired instead.
    assert app._market_quarantine.is_quarantined(CLETB) is True


def test_cletb_pause_quarantine_refuses_new_quotes(tmp_path: Path) -> None:
    """Scoped half 1: while quarantined, the filter refuses every RFQ whose
    legs touch the market — side-blind (an exchange-paused market can neither be
    entered nor hedged on)."""
    from tests.test_filters import combo_rfq

    quarantine = MarketQuarantine()
    app = _demo_app(tmp_path)
    rfq_filter = RfqFilter(
        app._config.filters,
        _FeedStub(),  # type: ignore[arg-type]
        FakeMetadata(),  # type: ignore[arg-type]
        KillSwitch(app._clock, kill_file=app._config.kill_file),
        app._clock,
        None,
        quarantine,
    )
    rfq = combo_rfq(
        mve_selected_legs=[
            {"market_ticker": CLETB, "side": "yes"},
            {"market_ticker": "KXMLBGAME-26JUL261215CLETB-TB", "side": "no"},
        ]
    )
    assert ReasonCode.SKIP_MARKET_QUARANTINED not in rfq_filter.evaluate(rfq)
    quarantine.quarantine(CLETB, "lifecycle change active->inactive")
    assert ReasonCode.SKIP_MARKET_QUARANTINED in rfq_filter.evaluate(rfq)
    # The "no" (hedge) side is refused too — unlike the operator blocklist.
    rfq_no = combo_rfq(
        mve_selected_legs=[
            {"market_ticker": CLETB, "side": "no"},
            {"market_ticker": "KXMLBGAME-26JUL261215CLETB-TB", "side": "no"},
        ]
    )
    assert ReasonCode.SKIP_MARKET_QUARANTINED in rfq_filter.evaluate(rfq_no)
    quarantine.release(CLETB)
    assert ReasonCode.SKIP_MARKET_QUARANTINED not in rfq_filter.evaluate(rfq)


# --------------------------------------------------------------------------- #
# (2) A genuine settlement change STILL halts and STILL drops needs_reconcile.
# --------------------------------------------------------------------------- #


def test_settlement_change_halts_and_drops_needs_reconcile(tmp_path: Path) -> None:
    """The strike moves under an open position (total 5.5 -> 6.5): a different
    bet on the same game. Halts, and the hard-halt path drops the
    needs_reconcile marker so a bare restart is blocked."""
    app = _armed_app(tmp_path)
    breakers = _breakers(app)
    _, second = _two_samples(
        app,
        _CLETB_BEFORE,
        _mutated(
            floor_strike=6.5,
            rules_primary=_CLETB_BEFORE["rules_primary"].replace("5.5", "6.5"),
        ),
    )
    assert second.changed_markets == (CLETB,)
    verdict = breakers.evaluate(second)
    assert verdict.tripped is True
    assert verdict.reason is ReasonCode.HALT_METADATA_CHANGE
    # The scoped lane was NOT taken.
    assert app._market_quarantine.is_quarantined(CLETB) is False
    # needs_reconcile is dropped on this halt class (fail-closed restart block).
    marker = tmp_path / "needs_reconcile"
    assert marker.exists() is False
    app.mark_reconcile_on_hard_halt(
        HaltEvent(
            reason=ReasonCode.HALT_METADATA_CHANGE,
            detail=str(verdict.detail),
            at_iso="2026-07-26T18:15:45+00:00",
        )
    )
    assert marker.exists() is True
    assert app._book_reconciled is False


def test_settlement_change_halts_even_after_the_horizon_passed(
    tmp_path: Path,
) -> None:
    """The end-of-life exemption must NOT cover a payoff change: a strike/rule
    move matters MOST after the game is over. (Pre-rebuild, the horizon
    exemption swallowed any change on a market past its close.)"""
    app = _armed_app(tmp_path)
    breakers = _breakers(app)
    past = {"close_time": "2020-07-26T16:15:00Z", "expected_expiration_time": ""}
    _, second = _two_samples(
        app,
        _mutated(**past),
        _mutated(**past, rules_primary="If Tampa Bay wins, the market resolves to Yes."),
    )
    assert second.changed_markets == (CLETB,)
    assert breakers.evaluate(second).reason is ReasonCode.HALT_METADATA_CHANGE


# --------------------------------------------------------------------------- #
# (3) Every LIFECYCLE-only field / transition is proven NON-halting.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("label", "after"),
    [
        # Kalshi `deactivated` — the 2026-07-26 halt.
        ("status_active_to_inactive", {"status": "inactive"}),
        # Kalshi `activated` (unpause). Also auto-cancels resting orders
        # exchange-side, which is exactly what the quarantine mirrors.
        ("status_inactive_to_active", {"status": "active"}),
        # Implicit transition at close_time.
        ("status_to_closed", {"status": "closed"}),
        # Early determination mid-game (2026-07-25 6:47p halt class).
        ("status_to_determined", {"status": "determined"}),
        ("status_to_finalized", {"status": "finalized"}),
        # close_time REWRITTEN backdated at the close — measured on
        # KXMLBRFI-26JUL261435SEATEX at 18:55:23Z, bundled with the
        # determination write, while expiration_time did not move.
        ("close_time_backdated", {"close_time": "2026-07-26T18:19:39Z"}),
        # A postponement INSIDE the listed window: close moves, the settlement
        # deadline (expiration_time) does not, so the payoff is unchanged.
        ("close_time_pushed_out", {"close_time": "2026-07-27T16:15:00Z"}),
        ("can_close_early_flips", {"can_close_early": False}),
    ],
)
def test_lifecycle_only_changes_never_halt(
    tmp_path: Path, label: str, after: dict[str, Any]
) -> None:
    app = _armed_app(tmp_path)
    breakers = _breakers(app)
    before = _CLETB_BEFORE
    if label == "status_inactive_to_active":
        before = _mutated(status="inactive")
    _, second = _two_samples(app, before, _mutated(**after))
    assert second.changed_markets == (), label
    assert breakers.evaluate(second).tripped is False, label
    assert app._market_quarantine.is_quarantined(CLETB) is True, label


@pytest.mark.parametrize(
    ("label", "after"),
    [
        # Kalshi's ESTIMATE of expiry drifts as a game runs long (2026-07-25
        # fourth-halt fix). In NEITHER lane: it moves on nearly every in-play
        # market and would otherwise quarantine the whole slate.
        ("expected_expiration_drift", {"expected_expiration_time": "2026-07-26T19:55:00Z"}),
        # Tick noise / bookkeeping.
        ("updated_time", {"updated_time": "2026-07-26T18:15:45.000000Z"}),
        ("prices_and_volume", {"volume_fp": "999999.0", "yes_bid_dollars": "0.4200"}),
        # The FIRST grading write is the settlement machinery's normal event.
        ("first_grading", {"result": "no", "expiration_value": "No"}),
    ],
)
def test_noise_changes_neither_halt_nor_quarantine(
    tmp_path: Path, label: str, after: dict[str, Any]
) -> None:
    app = _armed_app(tmp_path)
    breakers = _breakers(app)
    _, second = _two_samples(app, _CLETB_BEFORE, _mutated(**after))
    assert second.changed_markets == (), label
    assert breakers.evaluate(second).tripped is False, label
    assert app._market_quarantine.is_quarantined(CLETB) is False, label


# --------------------------------------------------------------------------- #
# (4) Every SETTLEMENT-relevant field / transition is proven halting.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("label", "after"),
    [
        # A different parent event is a different settlement source.
        ("event_ticker", {"event_ticker": "KXMLBTOTAL-26JUL261215CLETB-ALT"}),
        # The settlement rule verbatim.
        ("rules_primary", {"rules_primary": "If Tampa Bay wins, this resolves Yes."}),
        # The postponement policy — decides pays-0/1 vs resolves-to-fair-price.
        ("rules_secondary", {"rules_secondary": "Postponed games resolve to a fair price."}),
        # The line, and the entity/comparison the rule is evaluated against.
        ("floor_strike", {"floor_strike": 6.5}),
        ("strike_type", {"strike_type": "less"}),
        ("cap_strike_appears", {"cap_strike": 9.5}),
        ("custom_strike_appears", {"custom_strike": {"baseball_team": "uuid-x"}}),
        # The real settlement DEADLINE + the outer postponement bound. Measured
        # STABLE across the CLETB market's whole life and across the SEATEX
        # determination write that backdated close_time in the same step.
        ("expiration_time", {"expiration_time": "2026-08-02T16:15:00Z"}),
        ("latest_expiration_time", {"latest_expiration_time": "2026-08-02T16:15:00Z"}),
        # The payoff SHAPE and per-contract notional.
        ("market_type", {"market_type": "scalar"}),
        ("notional_value_dollars", {"notional_value_dollars": "10.0000"}),
        # A settlement field VANISHING from the payload is a change, not a match.
        ("rules_primary_absent", {}),
        # The grade is being contested / changed.
        ("status_disputed", {"status": "disputed"}),
        ("status_amended", {"status": "amended"}),
        # An enum value we do not model can never be called benign.
        ("status_unmodelled", {"status": "quarantined_by_exchange"}),
    ],
)
def test_settlement_relevant_changes_always_halt(
    tmp_path: Path, label: str, after: dict[str, Any]
) -> None:
    app = _armed_app(tmp_path)
    breakers = _breakers(app)
    mutated = _mutated(**after)
    if label == "rules_primary_absent":
        del mutated["rules_primary"]
    _, second = _two_samples(app, _CLETB_BEFORE, mutated)
    assert second.changed_markets == (CLETB,), label
    verdict = breakers.evaluate(second)
    assert verdict.tripped is True, label
    assert verdict.reason is ReasonCode.HALT_METADATA_CHANGE, label
    assert app._market_quarantine.is_quarantined(CLETB) is False, label


def test_regrade_halts_but_first_grading_does_not(tmp_path: Path) -> None:
    """`result` "" -> "no" is the determination write (normal). "no" -> "yes"
    is the graded outcome changing under a booked position."""
    app = _armed_app(tmp_path)
    breakers = _breakers(app)
    _, second = _two_samples(
        app, _mutated(status="determined", result="no"),
        _mutated(status="determined", result="yes"),
    )
    assert second.changed_markets == (CLETB,)
    assert breakers.evaluate(second).reason is ReasonCode.HALT_METADATA_CHANGE


def test_settlement_change_bundled_with_a_lifecycle_change_still_halts(
    tmp_path: Path,
) -> None:
    """Lane selection is by FIELD: a settlement move hidden inside a pause +
    close_time rewrite (the exact shape of a real Kalshi bundled write) cannot
    reach the scoped lane."""
    app = _armed_app(tmp_path)
    breakers = _breakers(app)
    _, second = _two_samples(
        app,
        _CLETB_BEFORE,
        _mutated(
            status="inactive",
            close_time="2026-07-26T18:19:39Z",
            floor_strike=6.5,
        ),
    )
    assert second.changed_markets == (CLETB,)
    assert breakers.evaluate(second).reason is ReasonCode.HALT_METADATA_CHANGE
    assert app._market_quarantine.is_quarantined(CLETB) is False


# --------------------------------------------------------------------------- #
# Fail-closed properties of the scoped lane.
# --------------------------------------------------------------------------- #


def test_unarmed_quarantine_fails_closed_to_a_halt(tmp_path: Path) -> None:
    """Property 2: with no quote-refusal consumer wired, the scoped lane would
    be a silent pass — so it is not used at all. The lifecycle change hard-halts
    exactly as it did before the rebuild."""
    app = _demo_app(tmp_path)  # deliberately NOT armed
    assert app._market_quarantine.armed is False
    breakers = _breakers(app)
    _, second = _two_samples(app, _CLETB_BEFORE, _mutated(status="inactive"))
    assert second.changed_markets == (CLETB,)
    assert breakers.evaluate(second).reason is ReasonCode.HALT_METADATA_CHANGE


async def test_enforcement_pulls_resting_quotes_then_marks_enforced(
    tmp_path: Path,
) -> None:
    """Scoped half 2: the enforcement pass pulls every deletable resting quote
    touching the quarantined market, and only THEN marks it enforced."""

    class _Lifecycle:
        def __init__(self, failures: int = 0) -> None:
            self.calls: list[tuple[set[str], Any]] = []
            self._failures = failures

        async def cancel_quotes_touching(
            self, tickers: set[str], reason: Any, **_: object
        ) -> tuple[int, int]:
            self.calls.append((set(tickers), reason))
            return (2, self._failures)

    app = _armed_app(tmp_path)
    app._market_quarantine.quarantine(CLETB, "pause")
    lifecycle = _Lifecycle()
    await app._enforce_market_quarantine(lifecycle)  # type: ignore[arg-type]
    assert lifecycle.calls[0][0] == {CLETB}
    assert lifecycle.calls[0][1] is ReasonCode.DELETE_MARKET_QUARANTINED
    assert app._market_quarantine.unenforced() == ()
    # Already enforced ⇒ no repeat network work on later ticks.
    await app._enforce_market_quarantine(lifecycle)  # type: ignore[arg-type]
    assert len(lifecycle.calls) == 1


async def test_unenforceable_quarantine_escalates_to_halt(tmp_path: Path) -> None:
    """Property 3: a scoped response we could not carry out is not a scoped
    response. A failed pull leaves the quarantine unenforced, and the NEXT
    sample promotes it to the whole-bot halt."""

    class _FailingLifecycle:
        async def cancel_quotes_touching(
            self, tickers: set[str], reason: Any, **_: object
        ) -> tuple[int, int]:
            return (1, 1)  # one delete the exchange did not acknowledge

        @property
        def last_withdraw_failure_kinds(self) -> Any:
            # Attribution for the halt receipt (2026-07-27 auto-relight): which
            # failure MODE left the quarantine unenforced. Read-only, additive —
            # the escalation below is unchanged by it.
            from collections import Counter

            return Counter({"429": 1})

    app = _armed_app(tmp_path)
    breakers = _breakers(app)
    _, second = _two_samples(app, _CLETB_BEFORE, _mutated(status="inactive"))
    assert second.changed_markets == ()  # tick N: scoped, no halt
    await app._enforce_market_quarantine(_FailingLifecycle())  # type: ignore[arg-type]
    assert app._market_quarantine.unenforced() == (CLETB,)
    third = _sample(
        app,
        FakeFeed(rx_age_s=0.1, warm=True, seq_gap=False),
        lifecycle=FakeLifecycle({CLETB: 0.02}),
        exposure=_book_with_quote_legs(
            (LegRef(CLETB, "KXMLBTOTAL-26JUL261215CLETB", "yes"),)
        ),
        metadata=FakeMetadata({CLETB: _meta(_mutated(status="inactive"))}),
    )
    assert third.changed_markets == (CLETB,)  # tick N+1: escalated
    assert breakers.evaluate(third).reason is ReasonCode.HALT_METADATA_CHANGE


async def test_enforcement_exception_leaves_quarantine_unenforced(
    tmp_path: Path,
) -> None:
    """An enforcement that RAISES must not silently count as enforced (and must
    not propagate — the escalation is the error path)."""

    class _RaisingLifecycle:
        async def cancel_quotes_touching(
            self, tickers: set[str], reason: Any, **_: object
        ) -> tuple[int, int]:
            raise RuntimeError("sender down")

    app = _armed_app(tmp_path)
    app._market_quarantine.quarantine(CLETB, "pause")
    await app._enforce_market_quarantine(_RaisingLifecycle())  # type: ignore[arg-type]
    assert app._market_quarantine.unenforced() == (CLETB,)


def test_release_requires_tradable_and_stable(tmp_path: Path) -> None:
    """Release is STRUCTURAL, never a timer: the market must read `active`
    again AND its lifecycle fingerprint must have stopped moving. A market
    that stays paused (a permanent deactivation / VOID) never leaves."""
    app = _armed_app(tmp_path)
    paused = _mutated(status="inactive")
    _, _ = _two_samples(app, _CLETB_BEFORE, paused)
    assert app._market_quarantine.is_quarantined(CLETB) is True
    legs = (LegRef(CLETB, "KXMLBTOTAL-26JUL261215CLETB", "yes"),)
    book = _book_with_quote_legs(legs)
    feed = FakeFeed(rx_age_s=0.1, warm=True, seq_gap=False)

    def tick(raw: dict[str, Any]) -> Any:
        return _sample(
            app, feed, lifecycle=FakeLifecycle({CLETB: 0.02}), exposure=book,
            metadata=FakeMetadata({CLETB: _meta(raw)}),
        )

    # Still paused and stable ⇒ still held (not tradable).
    tick(paused)
    assert app._market_quarantine.is_quarantined(CLETB) is True
    # Unpause: the fingerprint MOVED this tick ⇒ re-armed, still held.
    tick(_CLETB_BEFORE)
    assert app._market_quarantine.is_quarantined(CLETB) is True
    # Active AND stable for a full tick ⇒ released.
    tick(_CLETB_BEFORE)
    assert app._market_quarantine.is_quarantined(CLETB) is False


def test_quarantined_market_leaving_the_book_can_still_be_released(
    tmp_path: Path,
) -> None:
    """A market quarantined while in the book, then no longer on the risk path,
    must still be re-evaluated — otherwise it could never be released and would
    block that market for the rest of the run."""
    app = _armed_app(tmp_path)
    _two_samples(app, _CLETB_BEFORE, _mutated(status="inactive"))
    assert app._market_quarantine.is_quarantined(CLETB) is True
    feed = FakeFeed(rx_age_s=0.1, warm=True, seq_gap=False)
    for _ in range(3):  # empty book: no legs at all
        _sample(
            app, feed, lifecycle=FakeLifecycle(),
            metadata=FakeMetadata({CLETB: _meta(_CLETB_BEFORE)}),
        )
    assert app._market_quarantine.is_quarantined(CLETB) is False


def test_combo_grid_stub_is_never_fingerprinted(tmp_path: Path) -> None:
    """`MetadataCache.put_combo_grid` injects a grid-only stub with `raw={}`.
    It carries no exchange metadata, so it must seed no baseline — otherwise
    the stub -> real-payload transition would read as a settlement change."""
    app = _armed_app(tmp_path)
    breakers = _breakers(app)
    stub = MarketMeta(
        ticker=CLETB, status="active", grid=None, event_ticker=None,
        close_time=None, expected_expiration_time=None, raw={}, fetched_mono_ns=0,
    )
    legs = (LegRef(CLETB, "KXMLBTOTAL-26JUL261215CLETB", "yes"),)
    book = _book_with_quote_legs(legs)
    feed = FakeFeed(rx_age_s=0.1, warm=True, seq_gap=False)
    first = _sample(
        app, feed, lifecycle=FakeLifecycle({CLETB: 0.02}), exposure=book,
        metadata=FakeMetadata({CLETB: stub}),
    )
    assert first.changed_markets == ()
    second = _sample(
        app, feed, lifecycle=FakeLifecycle({CLETB: 0.02}), exposure=book,
        metadata=FakeMetadata({CLETB: _meta(_CLETB_BEFORE)}),
    )
    assert second.changed_markets == ()  # real payload SEEDS, never trips
    assert breakers.evaluate(second).tripped is False
    assert app._market_quarantine.is_quarantined(CLETB) is False
