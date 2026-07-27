"""V3/V4 — LOOP LIVENESS UNDER FAN-OUT, and the OBSERVED-TIER RATE BUDGET.

  R2 (2026-07-26 20:12:55Z) quarantine enforcement ran on the loop that also beat
     the heartbeat. An end-of-game lifecycle wave quarantined 11 markets in ONE
     tick; 63 slow 404ing DELETEs walked one at a time for 29 s; the supervisor
     emergency-cancelled 27 resting quotes on a bot that had priced 559 RFQs
     inside the "wedged" window. The suite fired ONE market at a time — the
     discriminating variable is the FAN-OUT N, and N must come from the tape.

  R3 (2026-07-26) the withdrawal retry was unmetered: 400 write tok/s against a
     300 tok/s ceiling, so a transient 429 sustained its own 429s forever. The
     ceiling is the account's OBSERVED bucket, never a requests/s literal — N
     calls in flight at latency L is 2N/L tokens/s, a property of the EXCHANGE's
     speed that a concurrency literal cannot express (rest.py:111).
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from typing import Any

from combomaker.exchange.rest import DELETE_QUOTE_TOKEN_COST
from tools.vitals.result import FAST, Probe, Result, Vital

V3 = Vital(
    key="V3",
    incident="R2 breaker rebuild coupled liveness to work (2026-07-26 20:12Z)",
    name="LOOP LIVENESS UNDER FAN-OUT — no loop stalls past its derived bound",
    degraded="N markets quarantined in ONE tick, every DELETE slow AND 404ing "
    "(N = the largest wave ever recorded on the tape)",
    money="the supervisor emergency-cancels a HEALTHY bot: 27 resting quotes "
    "killed, the whole book withdrawn, quoting down until a human restarts it",
    tier=FAST,
)

V4 = Vital(
    key="V4",
    incident="R3 unmetered retry driver (2026-07-26)",
    name="OBSERVED-TIER RATE BUDGET — no path outruns the account's own bucket",
    degraded="max_open_quotes withdraw-pending, every DELETE 429s forever, the "
    "exchange lists all of them as STILL RESTING",
    money="a transient rate limit becomes permanent: withdrawals never resolve, "
    "the book sits at max_open_quotes and every new RFQ is refused",
    tier=FAST,
)


async def _v3(probe: Probe, d: Any) -> tuple[bool, str, str, str]:
    from combomaker.ops.quote_app import LOOP_MAINTENANCE, MAINTENANCE_TICK_INTERVAL_S
    from combomaker.risk.heartbeat import HeartbeatReader
    from tests.test_liveness_progress import _NotFound, _rig_with_quotes
    from tests.test_metadata_change_scope import _armed_app

    n_markets = d.fanout_n
    per_market = 5           # the incident's 63 withdrawals over 11 markets
    per_delete_s = 0.05      # the measured 404 round trip on the incident tape

    async def slow_404(quote_id: str) -> dict[str, Any]:
        await asyncio.sleep(per_delete_s)
        raise _NotFound()

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        rig, tickers, _ = await _rig_with_quotes(
            tmp,
            markets=n_markets,
            quotes_per_market=per_market,
            delete=slow_404,
            db="vitals-fanout.sqlite3",
        )
        app = _armed_app(tmp)
        for ticker in tickers:
            app._market_quarantine.quarantine(ticker, "lifecycle change")  # noqa: SLF001

        cadence = MAINTENANCE_TICK_INTERVAL_S
        # THE BOUND, and it is the operator's ONE ratified wedge anchor plus the
        # loop's own registered cadence — nothing invented here.
        bound_s = d.heartbeat_timeout_s + cadence
        app._progress.register(  # noqa: SLF001
            LOOP_MAINTENANCE, interval_s=cadence, wedge_timeout_s=d.heartbeat_timeout_s
        )
        liveness = asyncio.create_task(app._liveness_loop())  # noqa: SLF001
        reader = HeartbeatReader(app._clock, app._heartbeat.path)  # noqa: SLF001
        poll_s = app._config.supervisor.poll_interval_s  # noqa: SLF001

        ages: list[float] = []
        stop = asyncio.Event()

        async def watch() -> None:
            while not stop.is_set():
                age = reader.read_age_s()
                ages.append(float("inf") if age is None else age)
                await asyncio.sleep(0.01)

        await asyncio.sleep(poll_s)
        watcher = asyncio.create_task(watch())
        t0 = time.monotonic()
        await app._enforce_market_quarantine(rig.lifecycle)  # noqa: SLF001
        elapsed = time.monotonic() - t0
        stop.set()
        await watcher
        liveness.cancel()

        worst = max(ages) if ages else float("inf")
        unenforced = len(app._market_quarantine.unenforced())  # noqa: SLF001
        sequential_s = n_markets * per_market * per_delete_s
        await rig.lifecycle._store.close()  # noqa: SLF001

    ok = worst < bound_s and unenforced == 0
    probe.note(
        f"wave: {n_markets} markets x {per_market} quotes = "
        f"{n_markets * per_market} withdrawals, each a {per_delete_s * 1000:.0f}ms 404"
    )
    probe.note(
        f"heartbeat worst age {worst:.2f}s over {len(ages)} samples; "
        f"enforcement {elapsed:.2f}s vs {sequential_s:.2f}s if walked one at a time"
    )
    probe.note(f"markets left unenforced: {unenforced}")
    return (
        ok,
        f"worst loop age {worst:.2f}s (enforcement {elapsed:.2f}s)",
        f"< heartbeat_timeout_s {d.heartbeat_timeout_s:.1f}s + maintenance cadence "
        f"{cadence:.1f}s = {bound_s:.1f}s",
        ""
        if ok
        else (
            "a work wave stalled the liveness signal — the supervisor kills a "
            "healthy bot"
            if worst >= bound_s
            else f"{unenforced} quarantined markets never enforced"
        ),
    )


async def _v4(probe: Probe, d: Any) -> tuple[bool, str, str, str]:
    from combomaker.ops.write_budget import WriteBudget
    from combomaker.rfq import lifecycle as lifecycle_mod
    from tests.test_withdraw_resolution import _429, _FakeLister, _rig, _tick, _wire_lister

    pending_n = d.max_open_quotes
    real_sleep = asyncio.sleep
    holder: dict[str, Any] = {}

    async def fake_sleep(delay: float, *a: Any, **k: Any) -> Any:
        clock = holder.get("clock")
        if clock is not None and delay > 0:
            clock.advance(delay)
        return await real_sleep(0)

    asyncio.sleep = fake_sleep  # type: ignore[assignment]
    lifecycle_mod._SIMULATED_CLOCK_HOLDER = holder  # type: ignore[attr-defined]
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rig = await _rig(
                tmp,
                "vitals-storm.sqlite3",
                max_open_quotes=2 * pending_n,  # capacity is NOT the axis here
                quotes=pending_n,
            )
            holder["clock"] = rig.h.clock
            # The bucket production hands the lifecycle: the operator's own knob,
            # CLAMPED BY THE OBSERVED TIER. Both numbers are read, not chosen.
            capacity = min(d.write_budget_capacity, d.write_capacity)
            rate = min(
                d.write_budget_capacity / d.write_budget_refill_s, d.write_refill_per_s
            )
            budget = WriteBudget.create(
                rig.h.clock, capacity=capacity, refill_s=capacity / rate
            )
            rig.lifecycle._withdraw_budget = budget  # noqa: SLF001
            resting = set(rig.lifecycle._open)  # noqa: SLF001
            lister = _FakeLister(lambda: resting)
            _wire_lister(rig, lister)

            emitted: list[float] = []

            async def refuses(quote_id: str) -> dict[str, Any]:
                emitted.append(rig.h.clock.now().timestamp())
                raise _429()

            rig.sender.delete_quote = refuses  # type: ignore[assignment]
            for rfq_id in list(rig.lifecycle._by_rfq):  # noqa: SLF001
                await rig.lifecycle.on_rfq_deleted(rfq_id, {})
            emitted.clear()

            t0 = rig.h.clock.now().timestamp()
            for _ in range(12):
                await _tick(rig)
            elapsed = rig.h.clock.now().timestamp() - t0
            tokens = len(emitted) * DELETE_QUOTE_TOKEN_COST
            peak_1s = (
                max(
                    sum(1 for t in emitted if s <= t < s + 1.0) * DELETE_QUOTE_TOKEN_COST
                    for s in emitted
                )
                if emitted
                else 0
            )
            reads = lister.calls
            ticks = 12
            await rig.lifecycle._store.close()  # noqa: SLF001
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]
        delattr(lifecycle_mod, "_SIMULATED_CLOCK_HOLDER")

    observed_ceiling = d.write_refill_per_s
    bucket_bound = budget.capacity + budget.rate_per_s * elapsed
    burst_bound = budget.capacity + budget.rate_per_s
    ok = (
        tokens <= bucket_bound
        and peak_1s <= burst_bound
        and peak_1s <= d.write_capacity
        and reads <= ticks  # the prover is ONE read per tick, O(1) in pending
    )
    probe.note(
        f"{pending_n} pending, every DELETE 429: emitted {len(emitted)} DELETEs = "
        f"{tokens} write tokens over {elapsed:.1f}s simulated"
    )
    probe.note(
        f"bucket = min(operator {d.write_budget_capacity}tok/"
        f"{d.write_budget_refill_s}s, observed {d.write_capacity}cap/"
        f"{observed_ceiling}per s) = {budget.capacity}tok @ {budget.rate_per_s:.1f}tok/s"
    )
    probe.note(f"open-quote READS (the prover): {reads} over {ticks} ticks — O(1)/tick")
    return (
        ok,
        f"peak {peak_1s} tok in any 1s, {tokens} tok total, {reads} reads",
        f"peak <= capacity+rate = {burst_bound:.0f} tok and <= observed cap "
        f"{d.write_capacity}; total <= {bucket_bound:.0f} tok; reads <= {ticks}",
        ""
        if ok
        else "the retry outruns the account's own bucket — the 429 sustains itself",
    )


def check_fanout(probe: Probe, d: Any) -> Result:
    ok, measured, bound, detail = asyncio.run(_v3(probe, d))
    return probe.grade(ok, measured=measured, bound=bound, detail=detail)


def check_rate_budget(probe: Probe, d: Any) -> Result:
    ok, measured, bound, detail = asyncio.run(_v4(probe, d))
    return probe.grade(ok, measured=measured, bound=bound, detail=detail)


CHECKS = [(V3, check_fanout), (V4, check_rate_budget)]
