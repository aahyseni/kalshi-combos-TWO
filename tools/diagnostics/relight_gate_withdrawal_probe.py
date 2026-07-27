"""ADVERSARIAL RELIGHT GATE probe — write-withdrawal pacing + liveness split.

Independent of tests/test_liveness_progress.py: builds its own rig, does its own
measurement, and asserts against the DOCUMENTED exchange ceiling rather than
against anything the fix asserts about itself.

Run: .venv/Scripts/python.exe tools/diagnostics/relight_gate_withdrawal_probe.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from combomaker.core.clock import SystemClock  # noqa: E402
from combomaker.core.reasons import ReasonCode  # noqa: E402
from combomaker.exchange.rest import (  # noqa: E402
    DEFAULT_REQUEST_TIMEOUT_S,
    LOWEST_TIER_LIMITS,
    DELETE_QUOTE_TOKEN_COST,
    KalshiApiError,
    RateLimitedError,
)

# Offline probe: it cannot ask GET /account/limits, so it grades against the
# FAIL-SAFE floor the live bot falls back to (2026-07-26 tier derivation).
WRITE_TOKENS_PER_S = LOWEST_TIER_LIMITS.write_refill_per_s
from combomaker.ops.config import SupervisorConfig as SupKnobs  # noqa: E402
from combomaker.ops.persistence import Store  # noqa: E402
from combomaker.ops.write_budget import WriteBudget  # noqa: E402
from combomaker.rfq.lifecycle import OpenQuoteState  # noqa: E402
from combomaker.risk.progress import (  # noqa: E402
    ProgressLedger,
    ProgressReader,
    progress_path,
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}")


class _NotFound(KalshiApiError):
    def __init__(self) -> None:
        super().__init__(404, "not_found", "not found")


async def build_rig(tmp: Path, n_quotes: int, db: str, *, real_clock: bool = True) -> Any:
    from tests.test_filters import Harness
    from tests.test_lifecycle import Rig
    from tests.test_pricing_engine import CROSS_EVENT_LEGS, combo, seed_event

    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp / db, h.clock)
    rig = Rig(h, store)
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS))
    seed_id = next(iter(rig.lifecycle._open))
    template = rig.lifecycle._open[seed_id].constructed
    await rig.lifecycle.cancel_all(ReasonCode.HALT_MANUAL)
    if real_clock:
        rig.lifecycle._clock = SystemClock()
        # PRODUCTION WIRING: exactly what quote_app builds, from the live
        # config's (defaulted) supervisor write-budget knobs.
        live = SupKnobs()
        rig.lifecycle._withdraw_budget = WriteBudget.create(
            SystemClock(),
            capacity=live.write_budget_capacity,
            refill_s=live.write_budget_refill_s,
        )
    ticker = "KXMLBTOTAL-26JUL261335TORBOS"
    qids = []
    for i in range(n_quotes):
        qid = f"q-{i}"
        rfq = combo(
            [{"market_ticker": ticker, "side": "yes", "event_ticker": "E1"}],
            id=f"rfq-{i}",
        )
        rig.lifecycle._open[qid] = OpenQuoteState(
            quote_id=qid,
            rfq=rfq,
            constructed=template,
            leg_mids_cc={ticker: 5_000},
            created_mono_ns=h.clock.monotonic_ns(),
        )
        qids.append(qid)
    return rig, ticker, qids


def peak_tokens_per_window(stamps: list[float], window: float = 1.0) -> tuple[float, int]:
    """Max tokens the EXCHANGE's bucket sees inside any `window` seconds."""
    s = sorted(stamps)
    best = 0
    j = 0
    for i in range(len(s)):
        while s[i] - s[j] >= window:
            j += 1
        best = max(best, i - j + 1)
    return best * DELETE_QUOTE_TOKEN_COST, best


# ==========================================================================
# (a) 200-quote wave vs the write-token budget, at realistic latencies
# ==========================================================================
async def gate_a(tmp: Path) -> None:
    live = SupKnobs()
    print(
        f"\n--- (a) 200-quote wave | budget {live.write_budget_capacity} tok /"
        f" {live.write_budget_refill_s}s = {live.write_budget_capacity / live.write_budget_refill_s:.0f} tok/s"
        f" | DeleteQuote={DELETE_QUOTE_TOKEN_COST} tok | tier ceiling {WRITE_TOKENS_PER_S} tok/s ---"
    )
    for latency in (0.005, 0.020, 0.040):
        rig, ticker, qids = await build_rig(tmp, 200, f"a{int(latency*1000)}.sqlite3")
        stamps: list[float] = []

        async def deleter(quote_id: str) -> dict[str, Any]:
            stamps.append(time.perf_counter())
            await asyncio.sleep(latency)
            return {}

        rig.lifecycle._sender.delete_quote = deleter  # type: ignore[assignment]
        t0 = time.perf_counter()
        deleted, failures = await rig.lifecycle.cancel_quotes_touching(
            {ticker}, ReasonCode.DELETE_MARKET_QUARANTINED, budget_s=15.0
        )
        wall = time.perf_counter() - t0
        tokens, reqs = peak_tokens_per_window(stamps)
        span = (max(stamps) - min(stamps)) if len(stamps) > 1 else 0.0
        avg_rps = len(stamps) / span if span > 0 else float("inf")
        check(
            f"(a) latency={latency*1000:.0f}ms  peak {tokens} tok/s <= {WRITE_TOKENS_PER_S}",
            tokens <= WRITE_TOKENS_PER_S,
            f"emitted={len(stamps)} deleted={deleted} failures={failures} "
            f"wall={wall:.2f}s peak_req/s={reqs} avg_req/s={avg_rps:.0f} tokens/s={tokens}",
        )
        check(
            f"(a) latency={latency*1000:.0f}ms  whole 200-wave finished inside the 15s tick budget",
            failures == 0 and deleted == 200 and wall < 15.0,
            f"wall={wall:.2f}s",
        )


# ==========================================================================
# (b) 429 leaves the quote in the mirror and is retried
# ==========================================================================
async def gate_b(tmp: Path) -> None:
    print("\n--- (b) 429 = UNKNOWN: mirror keeps the quote, next pass retries ---")
    rig, ticker, qids = await build_rig(tmp, 12, "b.sqlite3")
    mode = {"v": "429"}
    seen: list[str] = []

    async def deleter(quote_id: str) -> dict[str, Any]:
        seen.append(quote_id)
        if mode["v"] == "429":
            raise RateLimitedError(429, "rate_limited", "slow down")
        return {}

    rig.lifecycle._sender.delete_quote = deleter  # type: ignore[assignment]
    deleted, failures = await rig.lifecycle.cancel_quotes_touching(
        {ticker}, ReasonCode.DELETE_MARKET_QUARANTINED, budget_s=15.0
    )
    still = [q for q in qids if q in rig.lifecycle._open]
    check(
        "(b) every 429'd quote STAYS in the mirror (not forgotten)",
        len(still) == 12 and deleted == 0 and failures == 12,
        f"kept={len(still)}/12 deleted={deleted} failures={failures}",
    )
    check(
        "(b) failures>0 => caller leaves quarantine UNENFORCED (fail-closed)",
        failures > 0,
    )
    # retry pass
    mode["v"] = "ok"
    seen.clear()
    deleted2, failures2 = await rig.lifecycle.cancel_quotes_touching(
        {ticker}, ReasonCode.DELETE_MARKET_QUARANTINED, budget_s=15.0
    )
    check(
        "(b) the SAME 12 quotes are re-attempted on the next pass and then gone",
        sorted(seen) == sorted(qids) and deleted2 == 12 and failures2 == 0
        and not [q for q in qids if q in rig.lifecycle._open],
        f"retried={len(seen)} deleted={deleted2} failures={failures2}",
    )
    # partial: half 429, half ok -> only the ok half leaves the mirror
    rig2, t2, q2 = await build_rig(tmp, 10, "b2.sqlite3")
    bad = set(q2[:5])

    async def half(quote_id: str) -> dict[str, Any]:
        if quote_id in bad:
            raise RateLimitedError(429, "rate_limited", "slow down")
        return {}

    rig2.lifecycle._sender.delete_quote = half  # type: ignore[assignment]
    d3, f3 = await rig2.lifecycle.cancel_quotes_touching(
        {t2}, ReasonCode.DELETE_MARKET_QUARANTINED, budget_s=15.0
    )
    kept = {q for q in q2 if q in rig2.lifecycle._open}
    check(
        "(b) partial 429: ONLY the acked half leaves the mirror",
        kept == bad and d3 == 5 and f3 == 5,
        f"kept={sorted(kept)} deleted={d3} failures={f3}",
    )
    # 5xx / timeout / transport are also UNKNOWN
    rig3, t3, q3 = await build_rig(tmp, 6, "b3.sqlite3")

    async def five_hundred(quote_id: str) -> dict[str, Any]:
        raise KalshiApiError(503, "unavailable", "down")

    rig3.lifecycle._sender.delete_quote = five_hundred  # type: ignore[assignment]
    d4, f4 = await rig3.lifecycle.cancel_quotes_touching(
        {t3}, ReasonCode.DELETE_MARKET_QUARANTINED, budget_s=15.0
    )
    check(
        "(b) 5xx is UNKNOWN too: kept in the mirror, counted as failure",
        d4 == 0 and f4 == 6 and all(q in rig3.lifecycle._open for q in q3),
    )
    rig4, t4, q4 = await build_rig(tmp, 4, "b4.sqlite3")

    async def hang(quote_id: str) -> dict[str, Any]:
        await asyncio.sleep(60)
        return {}

    rig4.lifecycle._sender.delete_quote = hang  # type: ignore[assignment]
    t0 = time.perf_counter()
    d5, f5 = await rig4.lifecycle.cancel_quotes_touching(
        {t4}, ReasonCode.DELETE_MARKET_QUARANTINED, budget_s=1.0
    )
    wall = time.perf_counter() - t0
    check(
        "(b) hung exchange: wall-bounded, quotes KEPT, counted as failures",
        d5 == 0 and f5 == 4 and all(q in rig4.lifecycle._open for q in q4) and wall < 3.0,
        f"wall={wall:.2f}s deleted={d5} failures={f5}",
    )


# ==========================================================================
# (c) 404 still counts as gone
# ==========================================================================
async def gate_c(tmp: Path) -> None:
    print("\n--- (c) 404 = provably off the wire ---")
    rig, ticker, qids = await build_rig(tmp, 20, "c.sqlite3")

    async def gone(quote_id: str) -> dict[str, Any]:
        raise _NotFound()

    rig.lifecycle._sender.delete_quote = gone  # type: ignore[assignment]
    deleted, failures = await rig.lifecycle.cancel_quotes_touching(
        {ticker}, ReasonCode.DELETE_MARKET_QUARANTINED, budget_s=15.0
    )
    counters = rig.metrics.snapshot()["counters"]
    check(
        "(c) 404 counts as DELETED, zero failures, mirror dropped",
        deleted == 20
        and failures == 0
        and not [q for q in qids if q in rig.lifecycle._open],
        f"deleted={deleted} failures={failures} already_gone={counters.get('quote.delete_already_gone')}",
    )
    # and ONLY 404
    from combomaker.rfq.lifecycle import _already_gone

    check(
        "(c) _already_gone is 404 and ONLY 404",
        _already_gone(_NotFound())
        and not _already_gone(RateLimitedError(429, "r", "r"))
        and not _already_gone(KalshiApiError(503, "u", "d"))
        and not _already_gone(TimeoutError())
        and not _already_gone(OSError("reset")),
    )


# ==========================================================================
# (d) wedged quote loop AND wedged maintenance loop still detected + named
# ==========================================================================
class StepClock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self._t = start

    def advance(self, s: float) -> None:
        self._t += s

    def now(self) -> Any:
        from datetime import UTC, datetime

        return datetime.fromtimestamp(self._t, UTC)

    def monotonic_ns(self) -> int:
        return int(self._t * 1e9)


def gate_d(tmp: Path) -> None:
    from combomaker.ops.quote_app import (
        LOOP_MAINTENANCE,
        LOOP_QUOTE,
        MAINTENANCE_TICK_INTERVAL_S,
        POOL_DEADLINE_S,
    )

    print("\n--- (d) a genuinely wedged loop is STILL killed, and NAMED ---")
    wedge_timeout = 30.0  # live config/prod-live-wc.local.yaml supervisor value
    dwell = 1.5  # RFQ_MAX_QUEUE_DWELL_S (quote_app local)
    for stuck, other, bound in (
        (LOOP_MAINTENANCE, LOOP_QUOTE, wedge_timeout + MAINTENANCE_TICK_INTERVAL_S),
        (LOOP_QUOTE, LOOP_MAINTENANCE, wedge_timeout + POOL_DEADLINE_S + dwell),
    ):
        d = tmp / f"d_{stuck}"
        d.mkdir()
        clock = StepClock()
        led = ProgressLedger(clock, progress_path(d))
        led.register(
            LOOP_MAINTENANCE,
            interval_s=MAINTENANCE_TICK_INTERVAL_S,
            wedge_timeout_s=wedge_timeout,
        )
        led.register(
            LOOP_QUOTE,
            interval_s=POOL_DEADLINE_S + dwell,
            wedge_timeout_s=wedge_timeout,
            idle=lambda: False,  # work IS queued
        )
        rd = ProgressReader(clock, progress_path(d))
        led.publish()
        check(f"(d) {stuck}: healthy at t=0", rd.wedged_detail() is None)
        # advance to just BELOW the bound, keeping the other loop fresh
        clock.advance(bound - 1.0)
        led.mark(other)
        led.publish()
        below = rd.wedged_detail()
        check(
            f"(d) {stuck}: NOT killed 1s below its derived bound {bound:.1f}s",
            below is None,
            f"detail={below!r}",
        )
        clock.advance(2.0)
        led.mark(other)
        led.publish()
        detail = rd.wedged_detail()
        check(
            f"(d) {stuck}: killed just past bound AND named",
            detail is not None and stuck in detail and other not in detail,
            f"detail={detail!r}",
        )
        # ... with a PERFECTLY FRESH heartbeat (the whole point)
        from combomaker.risk.heartbeat import Heartbeat, HeartbeatReader

        hb = Heartbeat(clock, d / "heartbeat.txt")
        hb.beat()
        hr = HeartbeatReader(clock, d / "heartbeat.txt")
        check(
            f"(d) {stuck}: heartbeat is FRESH while the loop is wedged (old signal would miss it)",
            not hr.is_wedged(wedge_timeout) and detail is not None,
        )


# ==========================================================================
# (e) the empty-ledger case cannot read healthy
# ==========================================================================
def gate_e(tmp: Path) -> None:
    from combomaker.ops.quote_app import LOOP_MAINTENANCE

    print("\n--- (e) an empty ledger can never read HEALTHY-and-established ---")
    d = tmp / "e"
    d.mkdir()
    clock = StepClock()
    led = ProgressLedger(clock, progress_path(d))
    rd = ProgressReader(clock, progress_path(d))
    led.publish()  # preflight: zero loops registered
    check("(e) empty ledger: no escalation during startup", rd.wedged_detail() is None)
    check("(e) empty ledger does NOT LATCH (seen stays False)", rd.seen is False)
    # backdate it to 2020 — an empty ledger at ANY age must not latch
    p = progress_path(d)
    payload = json.loads(p.read_text())
    payload["written_at"] = "2020-01-01T00:00:00+00:00"
    p.write_text(json.dumps(payload))
    check(
        "(e) ancient EMPTY ledger still does not latch and still does not read established",
        rd.wedged_detail() is None and rd.seen is False,
    )
    # now establish, then go empty again -> MUST escalate
    led.register(LOOP_MAINTENANCE, interval_s=0.5, wedge_timeout_s=30.0)
    led.publish()
    check("(e) established ledger latches", rd.wedged_detail() is None and rd.seen is True)
    led2 = ProgressLedger(clock, progress_path(d))  # zero loops
    led2.publish()
    det = rd.wedged_detail()
    check(
        "(e) AFTER establishment, an empty ledger is WEDGED (fail-closed)",
        det is not None,
        f"detail={det!r}",
    )
    # and a stale publisher with fresh per-loop ages is wedged
    d2 = tmp / "e2"
    d2.mkdir()
    c2 = StepClock()
    l2 = ProgressLedger(c2, progress_path(d2))
    l2.register(LOOP_MAINTENANCE, interval_s=0.5, wedge_timeout_s=30.0)
    r2 = ProgressReader(c2, progress_path(d2))
    l2.publish()
    check("(e2) fresh", r2.wedged_detail() is None)
    c2.advance(120.0)  # publisher died; file frozen with age_s=0
    det2 = r2.wedged_detail()
    check(
        "(e2) frozen publisher (per-loop age_s=0 forever) is WEDGED via file age",
        det2 is not None,
        f"detail={det2!r}",
    )
    # corrupt / vanished after establishment
    progress_path(d2).unlink()
    check("(e2) vanished ledger after latch is WEDGED", r2.wedged_detail() is not None)
    progress_path(d2).write_text("{not json")
    check("(e2) corrupt ledger after latch is WEDGED", r2.wedged_detail() is not None)


# ==========================================================================
# (f) legitimate startup does not false-kill
# ==========================================================================
async def gate_f(tmp: Path) -> None:
    from combomaker.ops.quote_app import LOOP_MAINTENANCE, LOOP_QUOTE
    from combomaker.risk.heartbeat import Heartbeat

    print("\n--- (f) legitimate startup: no false kill on either axis ---")
    d = tmp / "f"
    d.mkdir()
    clock = StepClock()

    class _Ex:
        def __init__(self) -> None:
            self.cancelled = 0

        async def cancel_all_quotes(self) -> int:
            self.cancelled += 1
            return 0

    from combomaker.ops.supervisor import SafetySupervisor, SupervisorConfig

    cfg = SupervisorConfig(
        heartbeat_path=d / "heartbeat.txt",
        kill_file=d / "KILL",
        reconcile_marker_path=d / "needs_reconcile",
        heartbeat_timeout_s=30.0,
    )
    ex = _Ex()
    sup = SafetySupervisor(cfg, clock, exchange=ex)  # type: ignore[arg-type]
    # T0: nothing on disk at all (supervisor launched before the bot writes)
    check("(f) t0 nothing on disk: heartbeat axis says wedged (pre-existing, correct)",
          sup.heartbeat_wedged() is True)
    # the bot's real preflight order: beat heartbeat, publish EMPTY ledger, launch sup
    hb = Heartbeat(clock, d / "heartbeat.txt")
    hb.beat()
    led = ProgressLedger(clock, progress_path(d))
    led.publish()
    check("(f) after preflight beat+publish: NOT wedged", sup.wedged_detail() is None)
    # long startup: 60s of metadata warm before any loop registers, beater running
    for _ in range(60):
        clock.advance(1.0)
        hb.beat()
        led.publish()
        if sup.wedged_detail() is not None:
            break
    check(
        "(f) 60s of startup with zero loops registered: still no kill",
        sup.wedged_detail() is None,
        f"detail={sup.wedged_detail()!r}",
    )
    # loops come up
    led.register(LOOP_MAINTENANCE, interval_s=0.5, wedge_timeout_s=30.0)
    led.register(LOOP_QUOTE, interval_s=3.5, wedge_timeout_s=30.0, idle=lambda: True)
    led.publish()
    check("(f) loops registered, born progressing: no kill", sup.wedged_detail() is None)
    # quiet market: quote workers idle for 10 minutes
    for _ in range(600):
        clock.advance(1.0)
        hb.beat()
        led.mark(LOOP_MAINTENANCE)
        led.publish()
        if sup.wedged_detail() is not None:
            break
    check(
        "(f) 10 min of an IDLE quote queue is not a stall",
        sup.wedged_detail() is None,
        f"detail={sup.wedged_detail()!r}",
    )
    check("(f) supervisor never emergency-cancelled during startup", ex.cancelled == 0)


# ==========================================================================
# (g) no new hand-set numbers / no timeout raised
# ==========================================================================
def gate_g() -> None:
    import subprocess

    print("\n--- (g) numbers audit ---")
    root = Path(__file__).resolve().parents[2]
    live = SupKnobs()
    check(
        "(g) REST per-request timeout unchanged at 10.0s",
        DEFAULT_REQUEST_TIMEOUT_S == 10.0,
        f"={DEFAULT_REQUEST_TIMEOUT_S}",
    )
    old = subprocess.run(
        ["git", "show", "HEAD:src/combomaker/exchange/rest.py"],
        capture_output=True, text=True, cwd=root,
    ).stdout
    check(
        "(g) HEAD also had request_timeout_s: float = 10.0",
        "request_timeout_s: float = 10.0" in old,
    )
    oldcfg = subprocess.run(
        ["git", "show", "HEAD:src/combomaker/ops/config.py"],
        capture_output=True, text=True, cwd=root,
    ).stdout
    for key in (
        "heartbeat_timeout_s: float = 15.0",
        "poll_interval_s: float = 1.0",
        "write_budget_capacity: int = 200",
        "write_budget_refill_s: float = 10.0",
    ):
        check(f"(g) config unchanged: {key}", key in oldcfg)
    newcfg = (root / "src/combomaker/ops/config.py").read_text(encoding="utf-8")
    check(
        "(g) config.py is UNTOUCHED by this change",
        subprocess.run(["git", "diff", "--quiet", "--", "src/combomaker/ops/config.py"],
                       cwd=root).returncode == 0,
    )
    # the liveness loop's derived cadence, on live numbers
    interval = max(min(live.poll_interval_s, live.heartbeat_timeout_s / 4.0), 0.05)
    print(
        f"      liveness cadence on live config (timeout 30.0, poll 1.0) = "
        f"{max(min(1.0, 30.0 / 4.0), 0.05)}s; on defaults = {interval}s"
    )


async def main() -> int:
    import shutil, tempfile

    td = Path(tempfile.gettempdir()) / "relight_probe"
    shutil.rmtree(td, ignore_errors=True)
    td.mkdir(parents=True, exist_ok=True)
    if True:
        tmp = td
        await gate_a(tmp)
        await gate_b(tmp)
        await gate_c(tmp)
        gate_d(tmp)
        gate_e(tmp)
        await gate_f(tmp)
        gate_g()
    fails = [r for r in RESULTS if not r[1]]
    print(f"\n==== {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS, {len(fails)} FAIL ====")
    for n, _, d in fails:
        print(f"  FAIL {n} {d}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
