"""Phase 6 external safety supervisor + the kill-drill.

Deterministic: a fake exchange (no network), a fake clock, and a bot heartbeat
we age out by advancing the supervisor's clock.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from combomaker.core.clock import FakeClock
from combomaker.ops.supervisor import (
    SUPERVISOR_HEARTBEAT_FILENAME,
    KalshiSupervisorExchange,
    SafetySupervisor,
    SupervisorConfig,
    WriteBudget,
    supervisor_credential_configured,
    supervisor_heartbeat_path,
    supervisor_heartbeat_reachable,
)
from combomaker.risk.heartbeat import Heartbeat, HeartbeatReader, ReconcileMarker


class FakeExchange:
    """A stand-in SupervisorExchange. ``fail`` makes listing raise (unreachable);
    ``fail_cancel`` makes individual cancels raise."""

    def __init__(
        self, quote_ids: list[str], *, fail: bool = False, fail_cancel: bool = False
    ) -> None:
        self._ids = quote_ids
        self._fail = fail
        self._fail_cancel = fail_cancel
        self.cancelled: list[str] = []

    async def list_open_quote_ids(self) -> list[str]:
        if self._fail:
            raise ConnectionError("exchange unreachable")
        return list(self._ids)

    async def cancel_quote(self, quote_id: str) -> None:
        if self._fail_cancel:
            raise ConnectionError("cancel failed")
        self.cancelled.append(quote_id)


def _config(tmp_path: Path, **overrides: object) -> SupervisorConfig:
    kwargs: dict[str, object] = dict(
        heartbeat_path=tmp_path / "heartbeat.txt",
        kill_file=tmp_path / "KILL",
        reconcile_marker_path=tmp_path / "needs_reconcile",
        heartbeat_timeout_s=15.0,
        poll_interval_s=1.0,
        write_budget_capacity=200,
        write_budget_refill_s=10.0,
    )
    kwargs.update(overrides)
    return SupervisorConfig(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Write budget.
# --------------------------------------------------------------------------- #


def test_write_budget_spends_and_refills() -> None:
    clock = FakeClock()
    budget = WriteBudget.create(clock, capacity=3, refill_s=10.0)
    assert budget.try_spend() and budget.try_spend() and budget.try_spend()
    assert budget.try_spend() is False  # exhausted
    clock.advance(10.0)
    assert budget.try_spend() is True   # refilled at the window boundary


def test_write_budget_rejects_bad_params() -> None:
    clock = FakeClock()
    for cap, refill in ((0, 10.0), (1, 0.0)):
        try:
            WriteBudget.create(clock, capacity=cap, refill_s=refill)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for cap={cap} refill={refill}")


# --------------------------------------------------------------------------- #
# Kill-drill: missed heartbeat ⇒ cancel-all + KILL + marker.
# --------------------------------------------------------------------------- #


async def test_kill_drill_missed_heartbeat_cancels_and_kills(tmp_path: Path) -> None:
    # Bot beats once, then "dies". The supervisor's clock advances past the
    # timeout ⇒ it must cancel every resting quote AND write KILL + the marker.
    bot_clock = FakeClock()
    Heartbeat(bot_clock, tmp_path / "heartbeat.txt").beat()

    sup_clock = FakeClock()
    exchange = FakeExchange(["q1", "q2", "q3"])
    supervisor = SafetySupervisor(_config(tmp_path), sup_clock, exchange=exchange)

    assert supervisor.heartbeat_wedged() is False  # fresh at t=0
    sup_clock.advance(16.0)  # bot went silent past the 15s timeout
    assert supervisor.heartbeat_wedged() is True

    result = await supervisor.check_once()
    assert result is not None
    assert result.cancelled == 3
    assert result.failed == 0
    assert result.exchange_reachable is True
    assert result.kill_written is True
    assert result.marker_written is True
    assert sorted(exchange.cancelled) == ["q1", "q2", "q3"]
    assert (tmp_path / "KILL").exists()
    assert ReconcileMarker(tmp_path / "needs_reconcile").is_set()


async def test_live_bot_is_not_killed(tmp_path: Path) -> None:
    bot_clock = FakeClock()
    hb = Heartbeat(bot_clock, tmp_path / "heartbeat.txt")
    hb.beat()
    sup_clock = FakeClock()
    exchange = FakeExchange(["q1"])
    supervisor = SafetySupervisor(_config(tmp_path), sup_clock, exchange=exchange)
    # Bot keeps beating in lockstep with the supervisor's advancing clock.
    for _ in range(5):
        sup_clock.advance(5.0)
        bot_clock.advance(5.0)
        hb.beat()
        assert await supervisor.check_once() is None
    assert not (tmp_path / "KILL").exists()
    assert exchange.cancelled == []


# --------------------------------------------------------------------------- #
# Fail-closed: exchange unreachable ⇒ still KILL + marker + alarm.
# --------------------------------------------------------------------------- #


async def test_unreachable_exchange_still_writes_kill(tmp_path: Path) -> None:
    sup_clock = FakeClock()
    exchange = FakeExchange(["q1"], fail=True)  # listing raises
    supervisor = SafetySupervisor(_config(tmp_path), sup_clock, exchange=exchange)
    result = await supervisor.emergency_cancel_all("drill")
    assert result.exchange_reachable is False
    assert result.cancelled == 0
    assert result.kill_written is True   # fail-closed — KILL still lands
    assert result.marker_written is True
    assert (tmp_path / "KILL").exists()


async def test_no_credential_still_writes_kill(tmp_path: Path) -> None:
    # No dedicated supervisor credential ⇒ no cancel path, but KILL still lands.
    sup_clock = FakeClock()
    supervisor = SafetySupervisor(_config(tmp_path), sup_clock, exchange=None)
    assert supervisor.has_kill_credential is False
    result = await supervisor.emergency_cancel_all("no-cred drill")
    assert result.cancelled == 0
    assert result.exchange_reachable is False
    assert result.kill_written is True
    assert result.marker_written is True


async def test_partial_cancel_failures_counted(tmp_path: Path) -> None:
    sup_clock = FakeClock()
    exchange = FakeExchange(["q1", "q2"], fail_cancel=True)
    supervisor = SafetySupervisor(_config(tmp_path), sup_clock, exchange=exchange)
    result = await supervisor.emergency_cancel_all("drill")
    assert result.exchange_reachable is True
    assert result.cancelled == 0
    assert result.failed == 2
    assert result.kill_written is True  # KILL lands even when every cancel failed


# --------------------------------------------------------------------------- #
# Reserved write budget respected under a simulated 429 storm.
# --------------------------------------------------------------------------- #


async def test_reserved_budget_bounds_cancels_under_429_storm(tmp_path: Path) -> None:
    # 100 resting quotes but a reserved budget of only 5 for this window: the
    # supervisor spends exactly its reserved budget (never more), alarms that it
    # was exhausted, and STILL writes KILL so the bot can't add more.
    sup_clock = FakeClock()
    quote_ids = [f"q{i}" for i in range(100)]
    exchange = FakeExchange(quote_ids)
    config = _config(tmp_path, write_budget_capacity=5, write_budget_refill_s=10.0)
    supervisor = SafetySupervisor(config, sup_clock, exchange=exchange)
    result = await supervisor.emergency_cancel_all("429 storm")
    assert result.cancelled == 5          # exactly the reserved budget
    assert result.budget_exhausted is True
    assert result.kill_written is True
    assert len(exchange.cancelled) == 5


# --------------------------------------------------------------------------- #
# Idempotence + run loop.
# --------------------------------------------------------------------------- #


async def test_check_once_idempotent_after_kill(tmp_path: Path) -> None:
    bot_clock = FakeClock()
    Heartbeat(bot_clock, tmp_path / "heartbeat.txt").beat()
    sup_clock = FakeClock()
    exchange = FakeExchange(["q1", "q2"])
    supervisor = SafetySupervisor(_config(tmp_path), sup_clock, exchange=exchange)
    sup_clock.advance(20.0)
    first = await supervisor.check_once()
    assert first is not None and first.cancelled == 2
    # Once killed, a second check is a no-op (no double-cancel).
    assert await supervisor.check_once() is None
    assert len(exchange.cancelled) == 2


async def test_credential_presence_helper(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    assert supervisor_credential_configured() is False  # conftest strips env
    monkeypatch.setenv("KALSHI_SUPERVISOR_API_KEY_ID", "sup-key")
    assert supervisor_credential_configured() is False   # id but no key material
    monkeypatch.setenv("KALSHI_SUPERVISOR_PRIVATE_KEY_PEM", "-----PEM-----")
    assert supervisor_credential_configured() is True


# --------------------------------------------------------------------------- #
# Full kill-drill through the run() loop: a dying bot ⇒ the loop kills it.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Production adapter: list_open_quote_ids must paginate to exhaustion so an
# emergency cancel-all never leaves resting quotes beyond the first page.
# --------------------------------------------------------------------------- #


class PagingRest:
    """Fake KalshiRestClient serving /communications/quotes in cursor pages of
    ``page_size``. Records the cursors it was asked for so we can assert the
    adapter looped."""

    def __init__(self, quote_ids: list[str], *, page_size: int) -> None:
        self._ids = quote_ids
        self._page_size = page_size
        self.cursors_seen: list[str] = []

    async def get_quotes(self, **params: Any) -> dict[str, Any]:
        # /communications/quotes caps limit at 500 (docs/api-notes). Reject an
        # over-range limit the way Kalshi would (HTTP 400) so a regression that
        # sends 1000 on the emergency-cancel enumerate path fails this test
        # instead of silently relying on undocumented lenient clamping.
        limit = int(params.get("limit", 500))
        if limit > 500:
            raise ValueError(f"/communications/quotes limit max 500, got {limit}")
        cursor = str(params.get("cursor", ""))
        self.cursors_seen.append(cursor)
        start = int(cursor) if cursor else 0
        page = self._ids[start : start + self._page_size]
        next_start = start + self._page_size
        next_cursor = str(next_start) if next_start < len(self._ids) else ""
        return {
            "quotes": [{"id": qid} for qid in page],
            "cursor": next_cursor,
        }


async def test_list_open_quote_ids_paginates_to_exhaustion() -> None:
    # 25 open quotes across pages of 10: the adapter must return ALL of them, not
    # just the first page (regression for the single-page cancel-all miss).
    ids = [f"q{i}" for i in range(25)]
    rest = PagingRest(ids, page_size=10)
    adapter = KalshiSupervisorExchange(rest)
    got = await adapter.list_open_quote_ids()
    assert got == ids
    assert len(rest.cursors_seen) == 3  # looped: page 1 (""), page 2, page 3


async def test_list_open_quote_ids_single_page_when_no_cursor() -> None:
    ids = ["q1", "q2"]
    rest = PagingRest(ids, page_size=100)  # everything fits ⇒ empty next cursor
    adapter = KalshiSupervisorExchange(rest)
    assert await adapter.list_open_quote_ids() == ids
    assert rest.cursors_seen == [""]  # one request, no follow-up page


async def test_run_loop_kills_dying_bot(tmp_path: Path) -> None:
    import asyncio

    class DrivenClock(FakeClock):
        """A clock the loop advances itself: every ``now()`` jumps forward so the
        heartbeat ages out within a couple of poll cycles (deterministic without
        real sleeps)."""

        def now(self):  # type: ignore[no-untyped-def]
            self.advance(10.0)
            return super().now()

    # Bot beats once (at a fixed earlier instant) then dies.
    Heartbeat(FakeClock(), tmp_path / "heartbeat.txt").beat()
    exchange = FakeExchange(["q1", "q2"])
    supervisor = SafetySupervisor(
        _config(tmp_path, poll_interval_s=0.001), DrivenClock(), exchange=exchange
    )

    async def stop_after_kill() -> None:
        # Poll until the KILL file appears, then stop the loop.
        for _ in range(2000):
            if (tmp_path / "KILL").exists():
                supervisor.request_stop()
                return
            await asyncio.sleep(0.001)
        supervisor.request_stop()

    await asyncio.gather(supervisor.run(), stop_after_kill())
    assert (tmp_path / "KILL").exists()
    assert sorted(exchange.cancelled) == ["q1", "q2"]
    assert ReconcileMarker(tmp_path / "needs_reconcile").is_set()


# --------------------------------------------------------------------------- #
# The supervisor beats its OWN heartbeat so the bot's preflight can verify a
# RUNNING, RECENTLY-BEATING watcher (not just a configured credential).
# --------------------------------------------------------------------------- #


def test_supervisor_beats_own_heartbeat_on_check(tmp_path: Path) -> None:
    # A bot heartbeat exists and is fresh (no kill); check_once must still beat
    # the supervisor's OWN heartbeat every cycle so the preflight sees it alive.
    bot_clock = FakeClock()
    Heartbeat(bot_clock, tmp_path / "heartbeat.txt").beat()
    sup_clock = FakeClock()
    supervisor = SafetySupervisor(_config(tmp_path), sup_clock, exchange=None)
    own = supervisor_heartbeat_path(tmp_path)
    assert not own.exists()
    import asyncio

    asyncio.run(supervisor.check_once())
    assert own.exists()  # the supervisor proved its OWN liveness
    # And it is fresh against the supervisor's clock (age ~0).
    reader = HeartbeatReader(sup_clock, own)
    assert reader.is_wedged(15.0) is False


def test_supervisor_beats_own_heartbeat_even_after_kill(tmp_path: Path) -> None:
    # After a kill the supervisor stays up as the latch — a LIVE latch must keep
    # proving it's alive, so it keeps beating its own heartbeat.
    Heartbeat(FakeClock(), tmp_path / "heartbeat.txt").beat()
    sup_clock = FakeClock()
    supervisor = SafetySupervisor(_config(tmp_path), sup_clock, exchange=None)
    import asyncio

    sup_clock.advance(20.0)  # bot went silent ⇒ first check kills
    asyncio.run(supervisor.check_once())
    own = supervisor_heartbeat_path(tmp_path)
    # Wipe the own-heartbeat to prove the NEXT check re-beats it post-kill.
    own.unlink()
    asyncio.run(supervisor.check_once())  # idempotent no-op EXCEPT the beat
    assert own.exists()


# --------------------------------------------------------------------------- #
# supervisor_heartbeat_reachable: the preflight gate. Stronger than mere
# credential presence — requires a LIVE, recently-beating watcher.
# --------------------------------------------------------------------------- #


def test_reachable_false_without_credential(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A beating supervisor but NO credential ⇒ KILL-only, not a reachable CANCEL
    # path ⇒ False (conftest strips the credential env).
    clock = FakeClock()
    Heartbeat(clock, supervisor_heartbeat_path(tmp_path)).beat()
    assert supervisor_heartbeat_reachable(tmp_path, clock, max_age_s=15.0) is False


def test_reachable_false_when_no_supervisor_beating(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    # Credential present but NO supervisor heartbeat on disk ⇒ dead kill path.
    monkeypatch.setenv("KALSHI_SUPERVISOR_API_KEY_ID", "sup")
    monkeypatch.setenv("KALSHI_SUPERVISOR_PRIVATE_KEY_PEM", "-----PEM-----")
    clock = FakeClock()
    assert supervisor_heartbeat_reachable(tmp_path, clock, max_age_s=15.0) is False


def test_reachable_false_when_supervisor_heartbeat_stale(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    # Credential present, supervisor beat once but then went stale ⇒ False.
    monkeypatch.setenv("KALSHI_SUPERVISOR_API_KEY_ID", "sup")
    monkeypatch.setenv("KALSHI_SUPERVISOR_PRIVATE_KEY_PEM", "-----PEM-----")
    beat_clock = FakeClock()
    Heartbeat(beat_clock, supervisor_heartbeat_path(tmp_path)).beat()
    read_clock = FakeClock()
    read_clock.advance(20.0)  # 20s > 15s timeout ⇒ stale
    assert supervisor_heartbeat_reachable(tmp_path, read_clock, max_age_s=15.0) is False


def test_reachable_true_when_credential_and_beating(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setenv("KALSHI_SUPERVISOR_API_KEY_ID", "sup")
    monkeypatch.setenv("KALSHI_SUPERVISOR_PRIVATE_KEY_PEM", "-----PEM-----")
    clock = FakeClock()
    Heartbeat(clock, supervisor_heartbeat_path(tmp_path)).beat()
    assert supervisor_heartbeat_reachable(tmp_path, clock, max_age_s=15.0) is True


def test_supervisor_heartbeat_path_is_under_data_dir(tmp_path: Path) -> None:
    assert supervisor_heartbeat_path(tmp_path) == tmp_path / SUPERVISOR_HEARTBEAT_FILENAME
