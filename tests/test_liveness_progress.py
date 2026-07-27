"""LIVENESS ("alive") vs PROGRESS ("working") + the bounded quarantine burst.

Rebuild of 2026-07-26, after the supervisor emergency-cancelled 27 resting
quotes on a HEALTHY bot at 20:12:55.991Z (``data/live_20260726_1606.log``):

  20:12:25.647  11x market_quarantined in ONE maintenance tick — an end-of-game
                lifecycle wave (inactive->determined, active->inactive,
                active->finalized) across the TORBOS and CHCPIT markets.
  20:12:27.762  the first of 63 ``delete_quote_failed`` warnings, ALL
                ``HTTP 404 not_found`` — quotes the exchange had already dropped
                with their finished markets — walked ONE AT A TIME to 20:12:54.
  20:12:54.489  supervisor_heartbeat_wedged age=30.1s > 30.0s
  20:12:55.991  supervisor_emergency_kill, cancelled 27, KILL written.

The bot was never wedged: the largest gap between two log lines anywhere in that
window is 0.70s, 559 RFQs were priced inside the 30 "wedged" seconds, and 3
fills were confirmed on the run. What actually happened is that the ONE loop
that owned the liveness file also owned the work, so "this pass is slow" and
"this process is dead" were the same signal.

The fix has two halves and these tests pin both:

  1. DECOUPLE. The heartbeat is beaten by a dedicated task whose only job is to
     prove the event loop schedules, and every loop that matters publishes a
     separate last-progress age. Removing detection would be a NO-SHIP, so the
     tests below prove a genuinely stalled loop is STILL killed — with a fresh
     heartbeat — and at the same age the old single signal died at.
  2. BOUND THE BURST. Withdrawal is concurrent under a bounded fan-out, a 404 is
     SUCCESS (the quote is gone), a hung call cannot pin a slot, and a pass that
     cannot finish inside its caller's tick defers instead of stalling it.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from combomaker.core.clock import SystemClock
from combomaker.core.reasons import ReasonCode
from combomaker.exchange.rest import (
    DELETE_QUOTE_TOKEN_COST,
    LOWEST_TIER_LIMITS,
    ApiTierLimits,
    KalshiApiError,
    RateLimitedError,
)
from combomaker.ops.config import SupervisorConfig as SupervisorKnobs
from combomaker.ops.persistence import Store
from combomaker.ops.quote_app import (
    LOOP_MAINTENANCE,
    LOOP_QUOTE,
    MAINTENANCE_TICK_INTERVAL_S,
    STATUS_TICK_INTERVAL_S,
)
from combomaker.ops.supervisor import SafetySupervisor, SupervisorConfig
from combomaker.ops.write_budget import WriteBudget
from combomaker.rfq.lifecycle import OpenQuoteState, _already_gone
from combomaker.risk.heartbeat import Heartbeat, HeartbeatReader
from combomaker.risk.progress import ProgressLedger, ProgressReader, progress_path
from tests.test_filters import Harness
from tests.test_lifecycle import Rig
from tests.test_metadata_change_scope import _armed_app
from tests.test_pricing_engine import combo, seed_event

# --------------------------------------------------------------------------
# clocks
# --------------------------------------------------------------------------


class StepClock:
    """Deterministic wall+monotonic clock the tests advance by hand."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self._t = start

    def advance(self, seconds: float) -> None:
        self._t += seconds

    def now(self) -> Any:
        from datetime import UTC, datetime

        return datetime.fromtimestamp(self._t, UTC)

    def monotonic_ns(self) -> int:
        return int(self._t * 1e9)


def _ledger(tmp_path: Path, clock: StepClock) -> ProgressLedger:
    return ProgressLedger(clock, progress_path(tmp_path))  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# a lifecycle rig with N markets and M resting quotes on them
# --------------------------------------------------------------------------


async def _rig_with_quotes(
    tmp_path: Path,
    *,
    markets: int,
    quotes_per_market: int,
    delete: Any,
    db: str = "liveness.sqlite3",
    real_clock: bool = False,
    budget: WriteBudget | None = None,
) -> tuple[Rig, list[str], list[str]]:
    """A REAL ``QuoteLifecycle`` holding ``markets * quotes_per_market`` resting
    quotes, one market per quote, with a scripted ``delete_quote``.

    The quotes are injected as ``OpenQuoteState`` rather than round-tripped
    through ``handle_rfq`` so the test exercises the WITHDRAWAL path (the thing
    the incident stressed) at realistic width without also paying for 300 MC
    pricings. The state carries a REAL ``ConstructedQuote`` taken from a real
    priced quote, so nothing here is a hand-built stand-in."""
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / db, h.clock)
    rig = Rig(h, store)
    from tests.test_pricing_engine import CROSS_EVENT_LEGS

    # One real priced quote supplies a genuine ConstructedQuote to clone.
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS))
    seed_id = next(iter(rig.lifecycle._open))  # noqa: SLF001 (test seam)
    template = rig.lifecycle._open[seed_id].constructed  # noqa: SLF001
    await rig.lifecycle.cancel_all(ReasonCode.HALT_MANUAL)

    if real_clock:
        # The withdrawal WALL BUDGET is measured on the injected clock (repo
        # convention: every deadline is testable). The shared Harness clock is a
        # FakeClock that never advances, which would make any wall budget
        # infinite — so budget tests run the lifecycle on the real clock, the
        # same SystemClock production wires.
        rig.lifecycle._clock = SystemClock()  # noqa: SLF001 (test seam)
        # The WRITE-TOKEN bucket must run on the SAME clock the pass measures
        # its deadline on (production passes one clock to both). Rebuild the
        # DEFAULT bucket — same capacity/refill the live config gives it — on
        # the real clock, or it could never refill and every wave past the
        # first burst would defer.
        live = SupervisorKnobs()
        rig.lifecycle._withdraw_budget = WriteBudget.create(  # noqa: SLF001
            SystemClock(),
            capacity=live.write_budget_capacity,
            refill_s=live.write_budget_refill_s,
        )
    if budget is not None:
        # The WRITE-TOKEN budget the withdrawal paces on. Production injects it
        # from the operator's single knob (supervisor.write_budget_*); a test
        # that wants to observe pacing injects a bucket on a REAL clock (the
        # shared Harness FakeClock never advances, so a bucket on it could never
        # refill) — sometimes scaled, always with the arithmetic shown at the
        # call site.
        rig.lifecycle._withdraw_budget = budget  # noqa: SLF001 (test seam)
    rig.sender.delete_quote = delete  # type: ignore[assignment]
    tickers: list[str] = []
    quote_ids: list[str] = []
    for m in range(markets):
        ticker = f"KXMLBTOTAL-26JUL261335TORBOS-{m}"
        tickers.append(ticker)
        for q in range(quotes_per_market):
            quote_id = f"q-{m}-{q}"
            rfq = combo(
                [{"market_ticker": ticker, "side": "yes", "event_ticker": f"E{m}"}],
                id=f"rfq-{m}-{q}",
            )
            rig.lifecycle._open[quote_id] = OpenQuoteState(  # noqa: SLF001
                quote_id=quote_id,
                rfq=rfq,
                constructed=template,
                leg_mids_cc={ticker: 5_000},
                created_mono_ns=h.clock.monotonic_ns(),
            )
            quote_ids.append(quote_id)
    return rig, tickers, quote_ids


class _NotFound(KalshiApiError):
    def __init__(self) -> None:
        super().__init__(404, "not_found", "not found")


# ==========================================================================
# TEST 1 — the exact incident: 12 markets in one tick, slow 404ing deletes,
#          and the heartbeat NEVER goes stale.
# ==========================================================================


async def test_incident_twelve_market_wave_never_stales_the_heartbeat(
    tmp_path: Path,
) -> None:
    """THE INCIDENT. 12 markets quarantine in a single tick; every delete is
    slow AND answers 404, exactly as the exchange did at 20:12Z. The dedicated
    liveness task must keep the heartbeat fresh throughout, and the whole
    enforcement pass must finish far inside the sequential cost it replaced."""
    per_delete_s = 0.05
    markets, per_market = 12, 5  # 60 withdrawals — the incident's order of size

    async def slow_404(quote_id: str) -> dict[str, Any]:
        await asyncio.sleep(per_delete_s)
        raise _NotFound()

    rig, tickers, quote_ids = await _rig_with_quotes(
        tmp_path, markets=markets, quotes_per_market=per_market, delete=slow_404
    )
    app = _armed_app(tmp_path)
    for ticker in tickers:
        app._market_quarantine.quarantine(ticker, "lifecycle change")  # noqa: SLF001
    assert len(app._market_quarantine.unenforced()) == markets  # noqa: SLF001

    # The bot's REAL dedicated liveness task and REAL heartbeat file.
    app._progress.register(  # noqa: SLF001
        LOOP_MAINTENANCE,
        interval_s=MAINTENANCE_TICK_INTERVAL_S,
        wedge_timeout_s=app._config.supervisor.heartbeat_timeout_s,  # noqa: SLF001
    )
    liveness = asyncio.create_task(app._liveness_loop())  # noqa: SLF001
    reader = HeartbeatReader(app._clock, app._heartbeat.path)  # noqa: SLF001
    interval_s = app._config.supervisor.poll_interval_s  # noqa: SLF001

    ages: list[float] = []
    stop = asyncio.Event()

    async def watch() -> None:
        while not stop.is_set():
            age = reader.read_age_s()
            # Unreadable is fail-closed for the supervisor, so it must never
            # happen here either — record it as an infinite age.
            ages.append(float("inf") if age is None else age)
            await asyncio.sleep(0.01)

    await asyncio.sleep(interval_s)  # let the first beat land
    watcher = asyncio.create_task(watch())
    started = time.monotonic()
    await app._enforce_market_quarantine(rig.lifecycle)  # noqa: SLF001
    elapsed = time.monotonic() - started
    stop.set()
    await watcher
    liveness.cancel()

    # 1. Every market enforced — a 404 is provably gone, so nothing escalates.
    assert app._market_quarantine.unenforced() == ()  # noqa: SLF001
    assert rig.lifecycle.open_quote_count == 0

    # 2. The heartbeat never aged past even a couple of its own intervals, let
    #    alone the supervisor's 30s wedge tolerance.
    assert ages, "watcher never sampled"
    worst = max(ages)
    assert worst < app._config.supervisor.heartbeat_timeout_s  # noqa: SLF001
    assert worst <= interval_s * 3, f"heartbeat aged {worst:.2f}s (interval {interval_s}s)"

    # 3. Concurrency actually happened: sequential would cost 60 x 0.05 = 3.0s.
    sequential_s = markets * per_market * per_delete_s
    assert elapsed < sequential_s / 3, f"{elapsed:.2f}s vs sequential {sequential_s:.2f}s"


# ==========================================================================
# TEST 2 — detection was NOT removed.
# ==========================================================================


async def test_wedged_quote_loop_is_still_detected_with_a_fresh_heartbeat(
    tmp_path: Path,
) -> None:
    """THE NO-SHIP GUARD. A dedicated beater keeps the heartbeat fresh forever,
    so if that were the only signal a deadlocked quote loop would be invisible.
    With work QUEUED and the quote loop not advancing, the supervisor must still
    fire — and name the loop."""
    clock = StepClock()
    ledger = _ledger(tmp_path, clock)
    ledger.register(
        LOOP_QUOTE,
        interval_s=3.5,
        wedge_timeout_s=30.0,
        idle=lambda: False,  # work IS queued — idleness cannot excuse this
    )
    # A perfectly healthy-looking process: the beater keeps beating.
    heartbeat = Heartbeat(clock, tmp_path / "heartbeat.txt")  # type: ignore[arg-type]

    killed: list[str] = []

    class _Exchange:
        async def list_open_quote_ids(self) -> list[str]:
            return ["q1", "q2"]

        async def cancel_quote(self, quote_id: str) -> None:
            killed.append(quote_id)

    supervisor = SafetySupervisor(
        SupervisorConfig(
            heartbeat_path=tmp_path / "heartbeat.txt",
            kill_file=tmp_path / "KILL",
            reconcile_marker_path=tmp_path / "needs_reconcile",
            heartbeat_timeout_s=30.0,
            progress_path_=progress_path(tmp_path),
        ),
        clock,  # type: ignore[arg-type]
        exchange=_Exchange(),
    )

    # Healthy for a while: the loop marks, the beater beats, no kill.
    for _ in range(5):
        clock.advance(1.0)
        ledger.mark(LOOP_QUOTE)
        heartbeat.beat()
        ledger.publish()
        assert await supervisor.check_once() is None

    # Now the quote loop wedges. The PROCESS is still fine — the beater keeps
    # the heartbeat fresh the whole time — but no iteration ever completes.
    result = None
    for _ in range(40):
        clock.advance(1.0)
        heartbeat.beat()
        ledger.publish()
        result = await supervisor.check_once()
        if result is not None:
            break

    assert result is not None, "a wedged quote loop was NOT detected"
    # The heartbeat was FRESH the entire time — proof this came from the
    # progress axis and not from the old, now-decoupled liveness signal.
    assert supervisor.heartbeat_wedged() is False
    assert killed == ["q1", "q2"]
    assert result.kill_written is True
    assert result.marker_written is True
    assert (tmp_path / "KILL").exists()


async def test_stalled_maintenance_loop_dies_at_the_same_age_as_the_old_signal(
    tmp_path: Path,
) -> None:
    """The loop that USED to carry the beat is still killed at the age it always
    was: its bound is DERIVED as the operator's wedge tolerance plus the loop's
    own cadence, so nothing was loosened to make the incident stop hurting."""
    clock = StepClock()
    ledger = _ledger(tmp_path, clock)
    ledger.register(
        LOOP_MAINTENANCE,
        interval_s=MAINTENANCE_TICK_INTERVAL_S,
        wedge_timeout_s=30.0,
    )
    reader = ProgressReader(clock, progress_path(tmp_path))  # type: ignore[arg-type]
    bound = 30.0 + MAINTENANCE_TICK_INTERVAL_S

    ledger.mark(LOOP_MAINTENANCE)
    clock.advance(bound - 0.1)
    ledger.publish()
    assert reader.wedged_detail() is None  # just inside the bound

    clock.advance(0.2)
    ledger.publish()
    detail = reader.wedged_detail()
    assert detail is not None and LOOP_MAINTENANCE in detail


async def test_progress_ledger_is_fail_closed_once_established(tmp_path: Path) -> None:
    """LATCH. Before the bot has ever published, an absent ledger is 'starting
    up' (no escalation). Once it HAS published, a ledger that vanishes or
    corrupts is WEDGED — a stalled bot must not be able to hide by deleting the
    evidence."""
    clock = StepClock()
    ledger = _ledger(tmp_path, clock)
    ledger.register(LOOP_QUOTE, interval_s=1.0, wedge_timeout_s=30.0)
    reader = ProgressReader(clock, progress_path(tmp_path))  # type: ignore[arg-type]

    assert reader.wedged_detail() is None  # never seen ⇒ not yet established
    assert reader.seen is False

    ledger.publish()
    assert reader.wedged_detail() is None
    assert reader.seen is True

    progress_path(tmp_path).unlink()
    assert reader.wedged_detail() == "loop progress ledger unreadable"

    progress_path(tmp_path).write_text("{not json", encoding="utf-8")
    assert reader.wedged_detail() == "loop progress ledger unreadable"


async def test_a_stale_publisher_is_wedged_even_with_fresh_loop_ages(
    tmp_path: Path,
) -> None:
    """The publisher is part of the process it certifies: a ledger whose ages
    all read 0 but which stopped being REWRITTEN is stale, because the reader
    adds the file's own wall age."""
    clock = StepClock()
    ledger = _ledger(tmp_path, clock)
    ledger.register(LOOP_QUOTE, interval_s=1.0, wedge_timeout_s=30.0)
    ledger.mark(LOOP_QUOTE)
    ledger.publish()
    reader = ProgressReader(clock, progress_path(tmp_path))  # type: ignore[arg-type]
    assert reader.wedged_detail() is None

    clock.advance(31.5)  # nobody republished
    detail = reader.wedged_detail()
    assert detail is not None and LOOP_QUOTE in detail


async def test_an_empty_ledger_is_never_readable_as_healthy(tmp_path: Path) -> None:
    """B2 (2026-07-26 adversarial gate) — THE BLIND SPOT. Staleness is judged
    PER LOOP, so a ledger with ZERO registered loops cannot read stalled at ANY
    age. Before the fix it still LATCHED the reader (``seen=True``), which would
    have left the progress axis permanently blind while looking established —
    and quote_app publishes exactly this empty ledger at preflight, before any
    register() and again right after the supervisor is launched."""
    clock = StepClock()
    ledger = _ledger(tmp_path, clock)  # NO register() — the preflight publish
    ledger.publish()
    reader = ProgressReader(clock, progress_path(tmp_path))  # type: ignore[arg-type]

    payload = json.loads(progress_path(tmp_path).read_text(encoding="utf-8"))
    assert payload["loops"] == {}

    # Not established: it must not latch, at any age.
    assert reader.stalled() is None
    assert reader.seen is False
    assert reader.wedged_detail() is None  # startup ⇒ no escalation, no kill

    clock.advance(60 * 60 * 24 * 365)  # backdate it by a year
    ledger.publish()
    assert reader.wedged_detail() is None
    assert reader.seen is False, "an un-stallable ledger must never latch"

    # Once real loops appear the axis establishes and works normally…
    ledger.register(LOOP_QUOTE, interval_s=1.0, wedge_timeout_s=30.0)
    ledger.publish()
    assert reader.wedged_detail() is None
    assert reader.seen is True

    # …and going BACK to empty is now fail-closed: an established axis that
    # loses its loops is unreadable, not healthy.
    ProgressLedger(clock, progress_path(tmp_path)).publish()  # type: ignore[arg-type]
    assert reader.wedged_detail() == "loop progress ledger unreadable"


async def test_legitimate_startup_does_not_false_kill(tmp_path: Path) -> None:
    """The other side of B2: the real startup ORDER (publish an empty ledger →
    launch the supervisor → the supervisor polls for the whole of preflight →
    loops register → the liveness task publishes) must produce NO kill. The
    progress axis simply is not established yet, and since 2026-07-27 it is the
    ONLY kill axis — so an un-established ledger means no consequence at all."""
    clock = StepClock()
    ledger = _ledger(tmp_path, clock)
    heartbeat = Heartbeat(clock, tmp_path / "heartbeat.txt")  # type: ignore[arg-type]
    supervisor = SafetySupervisor(
        SupervisorConfig(
            heartbeat_path=tmp_path / "heartbeat.txt",
            kill_file=tmp_path / "KILL",
            reconcile_marker_path=tmp_path / "needs_reconcile",
            heartbeat_timeout_s=30.0,
            progress_path_=progress_path(tmp_path),
        ),
        clock,  # type: ignore[arg-type]
        exchange=None,
    )

    # PREFLIGHT: heartbeat + the empty ledger, then ~40 supervisor polls while
    # the bot works through startup (reconcile, book snapshot, gates).
    heartbeat.beat()
    ledger.publish()
    for _ in range(40):
        clock.advance(1.0)
        heartbeat.beat()
        ledger.publish()  # still empty — nothing has registered yet
        assert await supervisor.check_once() is None

    # LOOPS REGISTER, and normal operation continues without a kill.
    ledger.register(LOOP_MAINTENANCE, interval_s=MAINTENANCE_TICK_INTERVAL_S,
                    wedge_timeout_s=30.0)
    for _ in range(20):
        clock.advance(1.0)
        heartbeat.beat()
        ledger.mark(LOOP_MAINTENANCE)
        ledger.publish()
        assert await supervisor.check_once() is None
    assert not (tmp_path / "KILL").exists()


async def test_idle_queue_is_not_a_stall(tmp_path: Path) -> None:
    """A quiet market blocks the RFQ workers on an empty queue by design. That
    must never read as a wedge, or the bot dies every time the slate is quiet."""
    clock = StepClock()
    ledger = _ledger(tmp_path, clock)
    ledger.register(LOOP_QUOTE, interval_s=1.0, wedge_timeout_s=30.0, idle=lambda: True)
    reader = ProgressReader(clock, progress_path(tmp_path))  # type: ignore[arg-type]
    for _ in range(20):
        clock.advance(60.0)  # 20 minutes of total silence
        ledger.publish()
        assert reader.wedged_detail() is None


# ==========================================================================
# TEST 3 — a 404 on DELETE is "already gone", not an error path.
# ==========================================================================


def test_already_gone_is_404_and_only_404() -> None:
    assert _already_gone(KalshiApiError(404, "not_found", "not found")) is True
    # Everything else leaves a quote whose state we do NOT know ⇒ must escalate.
    assert _already_gone(RateLimitedError(429, "rate_limited", "slow down")) is False
    assert _already_gone(KalshiApiError(500, "internal", "boom")) is False
    assert _already_gone(TimeoutError()) is False
    assert _already_gone(ConnectionResetError()) is False
    assert _already_gone(RuntimeError("sender down")) is False


async def test_404_deletes_count_as_enforced_not_as_failures(tmp_path: Path) -> None:
    """The incident's 63 404s each counted as a FAILURE, which leaves the
    quarantine unenforced — and an unenforced quarantine escalates to a
    WHOLE-BOT HALT on the next tick. A quote the exchange has already dropped is
    gone; that is success."""

    async def always_404(quote_id: str) -> dict[str, Any]:
        raise _NotFound()

    rig, tickers, quote_ids = await _rig_with_quotes(
        tmp_path, markets=3, quotes_per_market=4, delete=always_404, db="gone.sqlite3"
    )
    deleted, failures = await rig.lifecycle.cancel_quotes_touching(
        set(tickers), ReasonCode.DELETE_MARKET_QUARANTINED
    )
    assert failures == 0
    assert deleted == len(quote_ids)
    assert rig.lifecycle.open_quote_count == 0
    assert rig.metrics.snapshot()["counters"]["quote.delete_already_gone"] == len(
        quote_ids
    )


async def test_non_404_delete_errors_still_fail_closed(tmp_path: Path) -> None:
    """The other half: a 429/5xx/timeout leaves a quote we cannot account for.
    Those still count as failures, so the quarantine stays unenforced and the
    existing escalation-to-halt still fires."""

    async def rate_limited(quote_id: str) -> dict[str, Any]:
        raise RateLimitedError(429, "rate_limited", "slow down")

    rig, tickers, quote_ids = await _rig_with_quotes(
        tmp_path, markets=2, quotes_per_market=3, delete=rate_limited, db="429.sqlite3"
    )
    deleted, failures = await rig.lifecycle.cancel_quotes_touching(
        set(tickers), ReasonCode.DELETE_MARKET_QUARANTINED
    )
    assert deleted == 0
    assert failures == len(quote_ids)


async def test_a_429_keeps_the_quote_in_the_mirror_and_retries_it(
    tmp_path: Path,
) -> None:
    """B1 (2026-07-26 adversarial gate), the WORST link in the 429 chain: a
    rate-limited DELETE never reached the book, so the quote is UNKNOWN — it may
    still be RESTING and may still FILL. It must stay in our mirror (or we stop
    repricing/cancelling/counting a live quote), and the next pass must ask
    again. A 404, by contrast, is proof it is gone."""
    answers: list[Any] = []

    async def scripted(quote_id: str) -> dict[str, Any]:
        raise answers.pop(0)

    rig, tickers, quote_ids = await _rig_with_quotes(
        tmp_path, markets=2, quotes_per_market=3, delete=scripted, db="429mirror.sqlite3"
    )
    # PASS 1 — every delete is rate-limited.
    answers.extend(RateLimitedError(429, "rate_limited", "slow down") for _ in quote_ids)
    deleted, failures = await rig.lifecycle.cancel_quotes_touching(
        set(tickers), ReasonCode.DELETE_MARKET_QUARANTINED
    )
    assert (deleted, failures) == (0, len(quote_ids))
    # THE FINDING: the mirror must NOT have forgotten them.
    assert rig.lifecycle.open_quote_count == len(quote_ids)
    assert rig.metrics.snapshot()["counters"]["quote.delete_rate_limited"] == len(
        quote_ids
    )

    # PASS 2 — the same markets are still quarantined, so the retry happens; the
    # exchange has since dropped the quotes with their finished markets (404).
    answers.extend(_NotFound() for _ in quote_ids)
    deleted, failures = await rig.lifecycle.cancel_quotes_touching(
        set(tickers), ReasonCode.DELETE_MARKET_QUARANTINED
    )
    assert (deleted, failures) == (len(quote_ids), 0)
    assert rig.lifecycle.open_quote_count == 0  # 404 IS proof ⇒ now it leaves
    assert not answers  # every quote really was asked about twice


# ==========================================================================
# TEST 4 — the fan-out is bounded.
# ==========================================================================


def _window_peak_tokens(spend_times: list[float], window_s: float) -> int:
    """Most tokens spent inside any ``window_s``-wide sliding window."""
    peak = 0
    for start in spend_times:
        n = sum(1 for t in spend_times if start <= t < start + window_s)
        peak = max(peak, n * DELETE_QUOTE_TOKEN_COST)
    return peak


async def test_a_two_hundred_quote_wave_stays_inside_the_write_token_budget(
    tmp_path: Path,
) -> None:
    """B1 (2026-07-26 adversarial gate). The withdrawal wave is paced in the
    exchange's OWN unit — write TOKENS — not in concurrent coroutines.

    Why a fan-out literal could not do this job (the shipped-then-rejected
    version used 8): a fan-out is a count, and what the exchange meters is a
    RATE. 8 in flight at a measured 5-20 ms DELETE latency is 400-1,600 req/s
    = 800-3,200 tokens/s; at 40 ms it is 200 req/s = 400 tokens/s. Every one of
    those is over the 300 tokens/s Advanced ceiling, and the number moves with
    the EXCHANGE's latency, not with our code.

    ARITHMETIC (live config, SupervisorConfig defaults):
        capacity  = 200 tokens per refill window
        refill_s  = 10.0 s          ⇒ sustained rate = 20 tokens/s
        DeleteQuote = 2 tokens      ⇒ 200 quotes = 400 tokens
        bucket guarantee: tokens spent in ANY window T <= capacity + rate*T
                          ⇒ worst 1 s = 200 + 20 = 220 <= 300 (tier ceiling)
    This test runs the SAME shape with the wall clock compressed (same capacity,
    a shorter refill window) so the invariant is checked in ~1 s instead of 10;
    the assertion is written against whatever bucket is configured, so it is
    scale-free. ``test_live_write_budget_clears_a_full_book_without_breaching``
    pins the LIVE numbers.
    """
    live = SupervisorKnobs()
    # Same capacity (so the burst shape is identical), 50x faster window.
    budget = WriteBudget.create(
        SystemClock(),
        capacity=live.write_budget_capacity,
        refill_s=live.write_budget_refill_s / 50.0,
    )
    rate_per_s = budget.rate_per_s
    spends: list[float] = []
    inflight = 0
    peak_inflight = 0
    max_latency_s = 0.0

    async def tracked_delete(quote_id: str) -> dict[str, Any]:
        nonlocal inflight, peak_inflight, max_latency_s
        sent_at = time.monotonic()
        spends.append(sent_at)
        inflight += 1
        peak_inflight = max(peak_inflight, inflight)
        try:
            await asyncio.sleep(0.005)  # realistic per-request latency
            raise _NotFound()
        finally:
            inflight -= 1
            max_latency_s = max(max_latency_s, time.monotonic() - sent_at)

    # 200 = the LIVE max_open_quotes, i.e. the biggest wave the book can make.
    rig, tickers, quote_ids = await _rig_with_quotes(
        tmp_path,
        markets=100,
        quotes_per_market=2,
        delete=tracked_delete,
        db="tokens.sqlite3",
        real_clock=True,
        budget=budget,
    )
    assert len(quote_ids) == 200
    started = time.monotonic()
    deleted, failures = await rig.lifecycle.cancel_quotes_touching(
        set(tickers), ReasonCode.DELETE_MARKET_QUARANTINED
    )
    elapsed = time.monotonic() - started

    assert (deleted, failures) == (200, 0)
    assert len(spends) == 200
    total_tokens = len(spends) * DELETE_QUOTE_TOKEN_COST
    assert total_tokens == 400

    # 1. THE BUDGET INVARIANT: never more tokens in a window than the bucket
    #    could have paid for. Checked on the bucket's own window and on the
    #    exchange's 1-second bucket.
    for window_s in (budget.refill_s, 1.0):
        allowed = budget.capacity + rate_per_s * window_s
        got = _window_peak_tokens(spends, window_s)
        assert got <= allowed, (
            f"{got} tokens in a {window_s}s window > budget "
            f"{budget.capacity} + {rate_per_s:.1f}/s * {window_s}s = {allowed:.0f}"
        )
    # 2. Average rate over the pass is the bucket's rate plus its one burst.
    assert total_tokens <= budget.capacity + rate_per_s * elapsed

    # 3. Concurrency is a CONSEQUENCE of the budget, not a separate literal:
    #    nothing can be in flight that was not paid for, so in-flight is capped
    #    by what the bucket can emit within one request's own latency —
    #    (capacity + rate * latency) / cost. At the LIVE rate (20 tokens/s) and
    #    a 40 ms DELETE that is 100 + 0.4 ⇒ 100 in flight; the compressed clock
    #    in this test just makes the second term visible.
    inflight_bound = (
        budget.capacity + rate_per_s * max_latency_s
    ) / DELETE_QUOTE_TOKEN_COST
    assert peak_inflight <= inflight_bound, (
        f"{peak_inflight} in flight > ({budget.capacity} + {rate_per_s:.0f}/s * "
        f"{max_latency_s:.3f}s) / {DELETE_QUOTE_TOKEN_COST} = {inflight_bound:.1f}"
    )
    assert peak_inflight > 1, "not concurrent at all — this is the sequential incident"


def test_live_write_budget_clears_a_full_book_without_breaching() -> None:
    """The LIVE numbers, as arithmetic — no rig, no timing.

    (a) The worst second the bucket can emit stays under the tier ceiling, so a
        withdrawal wave can never 429 (which would trip HALT_RATE_LIMIT_BURST,
        leave the quarantine unenforced, and mark live quotes UNKNOWN).
    (b) A full-book wave still finishes INSIDE the enforcement pass's own budget
        (the status tick), because a deferral is not free: it leaves the
        quarantine unenforced and escalates to a halt on the next tick.
    """
    live = SupervisorKnobs()
    # The tier is OBSERVED at boot (2026-07-26), so the budget the bot actually
    # builds is the CONFIGURED one clamped to the account's real bucket. Graded
    # here against the live prod observation AND against the fail-safe floor,
    # because the whole point is that both must hold.
    observed_prod = ApiTierLimits(
        usage_tier="advanced",
        read_refill_per_s=300,
        read_capacity=600,
        write_refill_per_s=300,
        write_capacity=600,
        observed=True,
    )
    for tier in (observed_prod, LOWEST_TIER_LIMITS):
        capacity, refill_s = tier.clamp_write_budget(
            live.write_budget_capacity, live.write_budget_refill_s
        )
        rate_per_s = capacity / refill_s
        # (a) token-bucket guarantee over any window T: capacity + rate*T.
        worst_second = capacity + rate_per_s * 1.0
        assert worst_second <= tier.write_capacity + tier.write_refill_per_s, (
            f"{tier.usage_tier}: {worst_second} tokens in one second exceeds "
            f"bucket {tier.write_capacity} + refill {tier.write_refill_per_s}"
        )

    # The rest of the arithmetic uses the OBSERVED prod tier (nothing clamps).
    capacity, refill_s = observed_prod.clamp_write_budget(
        live.write_budget_capacity, live.write_budget_refill_s
    )
    assert (capacity, refill_s) == (
        live.write_budget_capacity,
        live.write_budget_refill_s,
    ), "the shipped knob fits the observed prod bucket unchanged"
    rate_per_s = capacity / refill_s          # 20 tokens/s

    # (b) live max_open_quotes = 200 (config/prod-live-wc.local.yaml), so the
    #     biggest possible wave is 200 * 2 = 400 tokens. Time to spend it:
    #     the first `capacity` are free (bucket starts full), the rest accrue.
    live_max_open_quotes = 200
    wave_tokens = live_max_open_quotes * DELETE_QUOTE_TOKEN_COST
    seconds_needed = max(wave_tokens - capacity, 0) / rate_per_s
    assert seconds_needed <= STATUS_TICK_INTERVAL_S, (
        f"a full-book wave needs {seconds_needed:.1f}s > the enforcement budget "
        f"{STATUS_TICK_INTERVAL_S}s ⇒ it would defer and escalate to a halt"
    )


async def test_a_hung_exchange_never_stalls_the_caller(tmp_path: Path) -> None:
    """A slow/failing exchange must never hold the calling loop open. Every
    hung DELETE is cut at the caller's own wall budget and comes back UNKNOWN:
    those quotes stay resting in our mirror, are reported as
    not-provably-withdrawn (so the quarantine stays unenforced and the existing
    escalation still applies), and are retried on the next tick."""

    budget_s = 0.5

    async def hangs(quote_id: str) -> dict[str, Any]:
        await asyncio.sleep(30.0)  # a delete that never comes back
        return {}

    rig, tickers, quote_ids = await _rig_with_quotes(
        tmp_path,
        markets=8,
        quotes_per_market=8,
        delete=hangs,
        db="hang.sqlite3",
        real_clock=True,
    )
    assert len(quote_ids) == 64
    started = time.monotonic()
    deleted, failures = await rig.lifecycle.cancel_quotes_touching(
        set(tickers), ReasonCode.DELETE_MARKET_QUARANTINED, budget_s=budget_s
    )
    elapsed = time.monotonic() - started

    # The caller is held for its OWN budget, not for 64 x 30s (and not for the
    # per-call timeout an admission-gate-only budget would have cost).
    assert elapsed < budget_s * 4, f"the pass held the caller for {elapsed:.2f}s"
    assert deleted == 0
    assert failures == len(quote_ids)  # nothing provably withdrawn ⇒ fail-closed
    # NOTHING was provably withdrawn, so NOTHING leaves the mirror: every one of
    # these quotes may still be resting and may still fill.
    assert rig.lifecycle.open_quote_count == len(quote_ids)


async def test_out_of_tokens_defers_rather_than_storming_the_exchange(
    tmp_path: Path,
) -> None:
    """When the bucket cannot pay for the whole wave inside the caller's tick,
    the leftovers are DEFERRED — never sent anyway. Deferred quotes were never
    asked about, so they stay in the mirror, count as not-provably-withdrawn,
    and are retried next tick."""
    sent: list[str] = []

    async def counted(quote_id: str) -> dict[str, Any]:
        sent.append(quote_id)
        raise _NotFound()

    # A bucket that can pay for exactly 3 deletes in this pass's lifetime.
    budget = WriteBudget.create(
        SystemClock(), capacity=3 * DELETE_QUOTE_TOKEN_COST, refill_s=60.0
    )
    rig, tickers, quote_ids = await _rig_with_quotes(
        tmp_path,
        markets=5,
        quotes_per_market=4,
        delete=counted,
        db="defer.sqlite3",
        real_clock=True,
        budget=budget,
    )
    deleted, failures = await rig.lifecycle.cancel_quotes_touching(
        set(tickers), ReasonCode.DELETE_MARKET_QUARANTINED, budget_s=0.2
    )
    assert len(sent) == 3, f"sent {len(sent)} deletes on a 3-delete budget"
    assert deleted == 3
    assert failures == len(quote_ids) - 3
    assert rig.lifecycle.open_quote_count == len(quote_ids) - 3
    counters = rig.metrics.snapshot()["counters"]
    assert counters["quote.delete_deferred"] == len(quote_ids) - 3


# ==========================================================================
# wiring: the app really does register the loops it claims to
# ==========================================================================


async def test_liveness_task_publishes_both_files(tmp_path: Path) -> None:
    """End-to-end on the real app object: the dedicated task writes BOTH the
    heartbeat (alive) and the progress ledger (working)."""
    app = _armed_app(tmp_path)
    app._progress.register(  # noqa: SLF001
        LOOP_MAINTENANCE,
        interval_s=MAINTENANCE_TICK_INTERVAL_S,
        wedge_timeout_s=app._config.supervisor.heartbeat_timeout_s,  # noqa: SLF001
    )
    task = asyncio.create_task(app._liveness_loop())  # noqa: SLF001
    await asyncio.sleep(app._config.supervisor.poll_interval_s * 1.5)  # noqa: SLF001
    task.cancel()
    assert app._heartbeat.path.exists()  # noqa: SLF001
    payload = json.loads(progress_path(tmp_path).read_text(encoding="utf-8"))
    assert LOOP_MAINTENANCE in payload["loops"]
    assert payload["loops"][LOOP_MAINTENANCE]["stall_after_s"] == pytest.approx(
        app._config.supervisor.heartbeat_timeout_s + MAINTENANCE_TICK_INTERVAL_S  # noqa: SLF001
    )


def test_status_tick_interval_is_the_enforcement_budget() -> None:
    """The enforcement pass's budget is the STATUS LOOP's own cadence — derived
    from the loop that owns it, never a separate hand-set number."""
    assert STATUS_TICK_INTERVAL_S == 15.0
