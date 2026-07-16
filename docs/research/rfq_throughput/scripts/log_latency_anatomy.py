"""LENS 2 — latency anatomy from live logs (READ-ONLY).

Parses the 2026-07-16 live logs for:
  * pricing_stats series -> pool call/timeout rates, memo hit rate, status-tick gaps
  * periodic_report deltas -> per-run decision/skip mix (store counters are cumulative)
  * ws_disconnected/ws_connected -> reconnect anatomy
  * supervisor events, risk_starvation_watchdog
  * global log-timestamp gaps (event-loop stall proxy)
  * inventory_skew_shadow per-second rate (priced-RFQ throughput)

Writes nothing anywhere except stdout. Never touches the DBs.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

LOGS = [
    Path("data/live_logs/live_20260716_fixed.log"),
    Path("data/live_logs/live_20260716_caps100_dir40_corners3.log"),
    Path("data/live_logs/live_20260716_waiver_tiers_corners45.log"),
]

TS_RE = re.compile(r'"ts": "([^"]+)"')
EVENT_RE = re.compile(r'"event": "([^"]+)"')


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def pct(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    i = min(len(sorted_vals) - 1, int(q * len(sorted_vals)))
    return sorted_vals[i]


def analyze(path: Path) -> None:
    print(f"\n{'='*80}\nLOG: {path.name}\n{'='*80}")
    pricing_stats: list[dict] = []
    reports: list[tuple[datetime, dict, dict, int]] = []  # ts, decisions, skips, rfqs_seen
    ws_events: list[tuple[datetime, str]] = []
    supervisor: list[str] = []
    watchdog = 0
    skew_per_sec: Counter[str] = Counter()   # second bucket -> priced RFQs (post-filter+price)
    audit_per_sec: Counter[str] = Counter()
    first_ts = last_ts = None
    prev_ts = None
    max_gaps: list[tuple[float, str, str]] = []  # (gap_s, at, prev_line_event)
    prev_event = ""

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = TS_RE.search(line)
            if not m:
                continue
            ts = parse_ts(m.group(1))
            me = EVENT_RE.search(line)
            event = me.group(1) if me else "?"
            if first_ts is None:
                first_ts = ts
            if prev_ts is not None:
                gap = (ts - prev_ts).total_seconds()
                if gap > 2.0:
                    max_gaps.append((gap, prev_ts.isoformat(), f"{prev_event}->{event}"))
            prev_ts = ts
            prev_event = event
            last_ts = ts

            if event == "pricing_stats":
                d = json.loads(line)
                pricing_stats.append(d)
            elif event == "periodic_report":
                d = json.loads(line)
                rep = d["report"]
                mdec = re.search(r"decisions: (\{[^}]*\})", rep)
                decisions = eval(mdec.group(1)) if mdec else {}  # noqa: S307 - our own log
                mseen = re.search(r"RFQs seen: (\d+)", rep)
                seen = int(mseen.group(1)) if mseen else 0
                skips = dict(re.findall(r"    ([a-z_0-9]+): (\d+)", rep))
                reports.append((ts, decisions, {k: int(v) for k, v in skips.items()}, seen))
            elif event in ("ws_disconnected", "ws_connected", "ws_error", "book_invalidated"):
                ws_events.append((ts, event))
            elif event.startswith("supervisor_") or event == "kill_switch_halt":
                supervisor.append(f"{ts.isoformat()} {event}")
            elif event == "risk_starvation_watchdog":
                watchdog += 1
            elif event == "inventory_skew_shadow":
                skew_per_sec[m.group(1)[:19]] += 1
            elif event == "risk_audit":
                audit_per_sec[m.group(1)[:19]] += 1

    dur = (last_ts - first_ts).total_seconds() if first_ts and last_ts else 0
    print(f"span: {first_ts} -> {last_ts}  ({dur/60:.1f} min)")

    # ---- pricing_stats anatomy ----
    if pricing_stats:
        f0, fN = pricing_stats[0], pricing_stats[-1]
        print(f"\npricing_stats ticks: {len(pricing_stats)} (15s cadence)")
        print(f"  final: memo_hits={fN['memo_hits']} memo_misses={fN['memo_misses']} "
              f"hit_rate={fN['memo_hit_rate']} pool_calls={fN['pool_calls']} "
              f"pool_timeouts={fN['pool_timeouts']} pool_errors={fN['pool_errors']}")
        to_total = fN["pool_timeouts"] - f0["pool_timeouts"]
        calls_total = fN["pool_calls"] - f0["pool_calls"]
        print(f"  run delta: pool_calls={calls_total} pool_timeouts={to_total} "
              f"timeout_rate={to_total/calls_total*100 if calls_total else 0:.1f}%")
        # per-tick deltas + worst 6-min (24-tick) window
        deltas = []
        for a, b in zip(pricing_stats, pricing_stats[1:]):
            dt = (parse_ts(b["ts"]) - parse_ts(a["ts"])).total_seconds()
            deltas.append({
                "ts": b["ts"], "dt": dt,
                "calls": b["pool_calls"] - a["pool_calls"],
                "timeouts": b["pool_timeouts"] - a["pool_timeouts"],
                "hits": b["memo_hits"] - a["memo_hits"],
                "misses": b["memo_misses"] - a["memo_misses"],
            })
        # status-tick gap distribution (loop-stall proxy: task sleeps 15s between ticks)
        tick_gaps = sorted(d["dt"] for d in deltas)
        print(f"  status-tick gap (nominal 15s+work): p50={pct(tick_gaps,0.5):.1f}s "
              f"p90={pct(tick_gaps,0.9):.1f}s p99={pct(tick_gaps,0.99):.1f}s max={tick_gaps[-1]:.1f}s")
        big = [d for d in deltas if d["dt"] > 25]
        for d in big[:8]:
            print(f"    tick gap {d['dt']:.1f}s ending {d['ts']}")
        # worst 6-min window of timeouts
        best = None
        for i in range(len(deltas)):
            j = i
            acc_t, acc_c, acc_dt = 0, 0, 0.0
            while j < len(deltas) and acc_dt < 360:
                acc_dt += deltas[j]["dt"]
                acc_t += deltas[j]["timeouts"]
                acc_c += deltas[j]["calls"]
                j += 1
            if best is None or acc_t > best[0]:
                best = (acc_t, acc_c, acc_dt, deltas[i]["ts"])
        if best:
            print(f"  worst ~6min window: {best[0]} timeouts / {best[1]} pool calls "
                  f"({best[2]:.0f}s starting {best[3]})")
        # busiest single tick
        worst_tick = max(deltas, key=lambda d: d["timeouts"], default=None)
        if worst_tick:
            print(f"  worst 15s tick: {worst_tick['timeouts']} timeouts, {worst_tick['calls']} calls, "
                  f"{worst_tick['hits']}h/{worst_tick['misses']}m at {worst_tick['ts']}")

    # ---- periodic_report deltas ----
    if len(reports) >= 2:
        (t0, d0, s0, seen0), (t1, d1, s1, seen1) = reports[0], reports[-1]
        span = (t1 - t0).total_seconds()
        print(f"\nperiodic_report delta over {span/60:.1f} min "
              f"({t0.strftime('%H:%M')} -> {t1.strftime('%H:%M')}):")
        print(f"  RFQs recorded (allowlist tape): +{seen1 - seen0}  "
              f"({(seen1-seen0)/span:.1f}/s avg)")
        dd = {k: d1.get(k, 0) - d0.get(k, 0) for k in d1}
        print(f"  decisions delta: {dd}")
        ds = {k: s1.get(k, 0) - s0.get(k, 0) for k in s1}
        top = sorted(ds.items(), key=lambda kv: -kv[1])[:20]
        print("  skip-reason deltas (top 20):")
        for k, v in top:
            print(f"    {k:40s} {v:>8d}  ({v/span:.2f}/s)")

    # ---- throughput ----
    if audit_per_sec:
        vals = sorted(audit_per_sec.values())
        print(f"\nrisk_audit lines/sec (post-price decisions incl declines): "
              f"peak={vals[-1]} p99={pct(vals,0.99)} p50={pct(vals,0.5)} "
              f"active_secs={len(vals)}")
    if skew_per_sec:
        vals = sorted(skew_per_sec.values())
        print(f"inventory_skew_shadow lines/sec (RFQs that PRICED): "
              f"peak={vals[-1]} p99={pct(vals,0.99)} p50={pct(vals,0.5)} "
              f"active_secs={len(vals)}")

    # ---- WS churn ----
    if ws_events:
        cnt = Counter(e for _, e in ws_events)
        print(f"\nws events: {dict(cnt)}")
        # reconnect durations: ws_disconnected -> next ws_connected
        last_disc = None
        for ts, e in ws_events:
            if e == "ws_disconnected" and last_disc is None:
                last_disc = ts
            elif e == "ws_connected" and last_disc is not None:
                print(f"  reconnect: down {(ts - last_disc).total_seconds():.1f}s "
                      f"(disc {last_disc.strftime('%H:%M:%S')})")
                last_disc = None
        for ts, e in ws_events[:12]:
            print(f"    {ts.strftime('%H:%M:%S.%f')[:-3]} {e}")

    # ---- supervisor / watchdog ----
    print(f"\nsupervisor/halt events: {len(supervisor)}")
    for s in supervisor:
        print(f"  {s}")
    print(f"risk_starvation_watchdog count: {watchdog}")

    # ---- global log gaps ----
    max_gaps.sort(reverse=True)
    print(f"\nglobal log-timestamp gaps >2s: {len(max_gaps)} (top 10):")
    for g, at, ev in max_gaps[:10]:
        print(f"  {g:7.1f}s after {at}  [{ev}]")


if __name__ == "__main__":
    for p in LOGS:
        if p.exists():
            analyze(p)
        else:
            print(f"MISSING: {p}", file=sys.stderr)
