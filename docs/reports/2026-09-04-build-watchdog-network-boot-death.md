# 2026-09-04 — BUILD: watchdog retries NETWORK-class boot deaths (no more latch on a Wi-Fi flap or an exchange 503)

Item C (9/4 list item 5). Branch `build/watchdog-network-boot-death`. Blast
radius: **`tools/ops/` only — `src/` untouched; the pricing/quote path is not
in the diff.** The bot stayed DOWN throughout (nothing started; the live store
was never opened; corpse logs on `D:` were read as files, read-only). Built in
two passes: the first builder landed the mechanism (`735e694`) and died on a
usage limit with the harness un-run; this pass verified it, found four gaps
(below), finished the harness and ran everything.

## WRONG / FIXED / OPEN

| # | Item | Was | Now | Status |
|---|------|-----|-----|--------|
| 1 | Flap guard 1 latched the 8/27 02:13 ET relit boot ("run never reached liveness — relighting would boot-loop") after it died 45 s in on `aiohttp.ClientConnectorDNSError ... [getaddrinfo failed]` (Wi-Fi flap, network re-identifications 01:32–06:27 ET); nothing retried → 8-day outage | **WRONG** — a pre-heartbeat death latched regardless of cause | Corpse log is **classified**: NETWORK → no latch, backoff (threshold × streak, cap 900 s) gated on an exchange reach probe, retry forever; CONFIG_CODE / UNKNOWN → latch exactly as before, class named on the receipt | **FIXED** `735e694` |
| 2 | `_corpse_human_kill_reason` and the classifier each need the corpse tail | duplicated read | one shared `_corpse_tail()`; human-KILL contract pinned by two tests | FIXED `735e694` |
| 3 | **Exchange 5xx at boot.** The two REAL maintenance-window boot deaths on the tape (`live_20260806_0313` / `_0316`: `startup_reconcile_failed` on `KalshiApiError('HTTP 503 ...')` → `REFUSING TO QUOTE ... book_reconciled` → `supervisor_exchange_unreachable` on the same 503) classified **CONFIG_CODE** — the next relight into a Kalshi maintenance window would have latched, i.e. the exact incident behind the 8/6 "restart, that's it" ruling, one guard earlier | WRONG (first pass left it OPEN as "decision owed") | 5xx on a reach event's `error`, or a terminal `KalshiApiError` 5xx traceback, is NETWORK. Derived, not invented: the live code itself files it as unreachable (`supervisor.py` l.273-279 "cannot reach exchange", logged `exchange_reachable: false` on that 503; `rest.py` l.37-40 `KalshiApiError` str `HTTP {status} {code}: {message}`). 4xx (our request) and 429 (ambiguous) stay CODE, fail-closed. The probe already required HTTP 200, so a 503 keeps waiting | **FIXED** `d3a5777` |
| 4 | Refusal rule (`REFUSING TO QUOTE` on `book_reconciled` after a network reconcile failure) only looked at the NEWEST network event — on the real 8/6 corpse the supervisor's own 503 event lands AFTER the refusal print and shadowed the reconcile cause | WRONG (latent; the synthetic test had the events in the convenient order) | any network-caused `startup_reconcile_failed` in THIS boot decides; pinned with the real ordering | FIXED `d3a5777` |
| 5 | `_corpse_tail()` picks the newest `live_*.log` by mtime only. Windows stamps file times off the ~15.6 ms system tick, so logs written in one burst tie — **measured** on the harness's P2 scratch tree: 3 logs, 1 distinct `st_mtime_ns`, old pick = the synthetic 7/30 tape, not the corpse → P2's new class check was RED on the committed tree | WRONG (harness never run by the first pass) | ties broken by name (`live_YYYYMMDD_HHMM.log` sorts chronologically); pinned by `test_corpse_tail_reads_the_newest_log_name_on_an_mtime_tie` | FIXED `d3a5777` |
| 6 | Marker list claimed "the installed aiohttp 3.14.1 `ClientConnectionError` family, enumerated from the library" but missed `ClientSSLError` and `ServerFingerprintMismatch` (re-enumerated: 15 names) | inaccurate | both added; the claim is now exact | FIXED `d3a5777` |
| 7 | `Watchdog()` with no injected probe aimed at a hard-coded prod URL while the CLI path read the live yaml | inconsistent | `__post_init__` uses the same `_read_exchange_anchors(root)`; pinned | FIXED `d3a5777` |
| 8 | `tools/ops/prove_watchdog.py` P4 still pinned the pre-8/6 permanent flap latch and **crashed** (`AttributeError: 'NoneType' ... .get`) on the unmodified tree — the harness had been red since `79d44ba` (8/6) | WRONG (pre-existing) | P4 rewritten to the shipped 8/6 semantics; P2 checks the class; **P8** replays the 8/27 corpse and **P9** the 8/6 corpse through the real CLI | FIXED `111b4bb` |
| 9 | Watchdog "never talks to Kalshi" | true | ONE unauthenticated, read-only `GET /exchange/status` per backoff wait, only while a NETWORK-class boot death keeps the tree down; docstring updated | changed, documented |
| 10 | HTTP 429 (`RateLimitedError`) as the terminal cause of a boot death | latch | still latches (CODE): throttling after rapid relights vs our own budget misconfig is not readable from the corpse — fail-closed on the ambiguous. No such corpse exists on the tape (0 / 271) | **OPEN** — revisit only if it ever latches |
| 11 | `sqlite3.OperationalError: database is locked` instant boot deaths (6 corpses 8/1–8/17, 2.6 KB each; the `WATCHDOG_HALT_20260814_092516` and `_20260815_121104` receipts are exactly this, `healthy_span_s: 0.0`) | latch (CONFIG_CODE) | unchanged — a locked store is a LOCAL transient resource (an analysis session / a lingering writer holding the store), not an exchange-reach failure; outside this item | **OPEN** — own item: a store-lock probe on the same retry path, or the writer's lock discipline |
| 12 | First pass saw `prove_watchdog` P1 flake under load (unit suite running concurrently) | flaky? | two unloaded runs today: P1 PASS both times | closed unless it recurs |

## What changed (mechanism, not numbers)

```
  corpse live_*.log (newest by (mtime, name), last 256 KB)
        │  _corpse_tail()  ── shared with _corpse_human_kill_reason (unchanged contract)
        ▼
  classify_death(tail) — scoped to the last "quote_app_starting" (this boot only)
        │
        ├─ 1. Traceback present → TERMINAL exception line decides
        │       transport class (aiohttp ClientConnectionError family, gaierror,
        │       getaddrinfo failed, Connection*Error)               → NETWORK
        │       bare TimeoutError + frame in combomaker/exchange or aiohttp → NETWORK
        │       KalshiApiError with HTTP 5xx                        → NETWORK   (this pass)
        │       anything else (ImportError, ValidationError, KeyError,
        │       KalshiApiError 4xx, RateLimitedError 429 …)         → CONFIG_CODE
        ├─ 2. CLI refusal print (config error: / REFUSING TO START: /
        │       credentials error: / REFUSING TO QUOTE:)            → CONFIG_CODE
        │       …except REFUSING TO QUOTE on book_reconciled when ANY
        │       network-caused startup_reconcile_failed is in this boot → NETWORK
        ├─ 3. reach event (startup_reconcile_failed, supervisor_exchange_
        │       unreachable, exchange_status_failed) whose error is a
        │       transport / timeout / HTTP 5xx, or kill_switch_halt
        │       reason=halt_data_stale                              → NETWORK
        └─ 4. nothing readable                                      → UNKNOWN

  _escalate():  stop tree ──► classify (logged on EVERY escalation)
      no heartbeat?  ┬─ NETWORK ──► _wait_for_exchange_reach():
                     │     loop: wait = min(threshold × (max(streak,1)+failed_probes), 900)
                     │           probe_exchange_reach(): DNS resolve host → GET /exchange/status
                     │           ok (HTTP 200 JSON) → relight; 5xx / no route → wait again (forever)
                     │           probe CRASH (bug) → ok=None → relight anyway, logged loud
                     └─ CONFIG_CODE / UNKNOWN ──► _latch_halt("…[death class X: marker]")
      guard 2 (8/6 backoff) skipped when guard 1 already waited — never two waits
      human-gated KILL check → latch (unchanged, still last before start)
```

Every marker is derived from the live code and cited in the module comment
block (`tools/ops/hang_watchdog.py`, "Boot-death classification"):
`exchange/rest.py` `_request` l.339-390 (catches only JSON-decode errors, so
aiohttp transport errors propagate; l.388-389 raises `KalshiApiError` for every
non-2xx, 429 → `RateLimitedError`; l.37-40 its str `HTTP {status} {code}:
{message}`) and l.314 (`ClientTimeout(total=…)` → builtin `TimeoutError`);
the aiohttp 3.14.1 `ClientConnectionError` family enumerated from the installed
library (15 names); `quote_app.py` l.3238/3267 (`startup_reconcile_failed`, "a
timeout or a transport error is exactly the 'exchange unreachable' case"),
l.3772-3775 (`startup_reconcile_incomplete`: "exchange unreachable, or …"),
l.4886 (`exchange_status_failed`), l.1884 (`quote_app_starting`);
`supervisor.py` l.273-279 (any listing `Exception` → `supervisor_exchange_
unreachable`, "cannot reach exchange"); `risk/killswitch.py` l.64 +
`core/reasons.py` l.305 + `risk/breakers.py` l.70; `ops/cli.py`
l.168/171/184/190/197; `ops/preflight.py` l.71-72. No numeric knob was added:
the backoff is guard 2's existing `threshold × streak` (threshold is
tape-derived; the 900 s cap is the 8/6 value), the probe timeout is `rest.py`
`DEFAULT_REQUEST_TIMEOUT_S` (10.0, mirrored with citation like the
SupervisorConfig defaults already were), the host comes from the live
`config/*.local.yaml` (`endpoints.rest_base_url` or `env`; per-env templates
are deliberately not consulted; no config ⇒ prod, which is what
`start_all.ps1` launches). The only other literals in the diff are I/O bounds
(256 KB tail read — pre-existing; 64 KB probe body cap; 60 s seam-command
timeout matching the existing `probe_cmd` seam; 200/240-char log truncation),
none of which steers a decision.

Files: `tools/ops/hang_watchdog.py`, `tests/test_hang_watchdog.py`,
`tests/fixtures/watchdog/live_20260827_0207_tail.log` (the 8/27 corpse
verbatim, 110 lines) and `tests/fixtures/watchdog/live_20260806_0313_tail.log`
(the 8/6 corpse verbatim, 37 lines, incl. the raw cp1252 em-dash byte the CLI
printed — the watchdog decodes with `errors="replace"`, and the test proves it
on the real bytes); both force-added past `.gitignore`'s `*.log` and carry
env-var NAMES only, no values; `tools/ops/prove_watchdog.py`.

## Measured evidence

**The incident, from the watchdog's own log + receipt (`D:/kalshi-combos-TWO-data`, read-only):**
02:07:59 ET relight → `live_20260827_0207.log` boots, `needs_reconcile`
marker present, `startup_reconcile_failed` (enumerate, `TimeoutError()`) at
02:08:45, then `_rehydrate_exposure_book` → `rest.get_positions` →
`ClientConnectorDNSError: Cannot connect to host external-api.kalshi.com:443
ssl:default [getaddrinfo failed]` (uncaught; the two interpreters hung alive).
02:12:59 `hung_process` escalation (log quiet 245 s > threshold 242.0 s,
`heartbeat_present: false`, `healthy_span_s: 48.7`, `pids=[20708, 26552]`),
02:13:02 **HALT LATCHED** — "STAYING DOWN" every 5 min until the watchdog
itself died ~13:42 ET. `hang_watchdog.log` shows 176 completed relights over
the watchdog's life; this was the first latch since 8/15.

**Classifier over the WHOLE recorded tape (rule 8: prototype on recorded data,
read-only; every `live_*.log` on `D:`, last 256 KB each — the same read the
watchdog makes):**

| pass | logs | UNKNOWN | CONFIG_CODE | NETWORK |
|---|---|---|---|---|
| `735e694` (first pass) | 271 | 236 | 24 | 11 |
| final (this pass) | 271 | 236 | 22 | 13 |

The two that moved are exactly `live_20260806_0313` and `_0316` (CONFIG_CODE →
NETWORK `startup_reconcile_failed: HTTP 503`). The 13 NETWORK verdicts:

| corpse | marker |
|---|---|
| `live_20260827_0207` (the latch) | **`ClientConnectorDNSError`** |
| `live_20260827_0201` (stall-kill in shutdown, exchange timeout) | `supervisor_exchange_unreachable: TimeoutError` |
| `live_20260826_2243`, `_0144`, `_0155` (DNS lost 01:32 / 01:52 / 01:59 ET) | `halt_data_stale` |
| `live_20260806_0313`, `_0316` (Kalshi maintenance 503s) | `startup_reconcile_failed: HTTP 503` |
| `live_20260818_0519` (19 GB healthy run, died in shutdown on a 503) | `supervisor_exchange_unreachable: HTTP 503` |
| `live_20260803_0558`, `_0805_1601`, `_0805_1647`, `_0806_0205`, `_0813_0241` | `halt_data_stale` |

Of these only the pre-heartbeat ones (`_0207`, `_0313`, `_0316`) ever reach
guard 1; the rest are forensic (their runs had a heartbeat). The 22
CONFIG_CODE verdicts: 16 × `sqlite3.ProgrammingError: Cannot operate on a
closed database` (shutdown-time writer race on long healthy runs — heartbeat
present, never a guard-1 input), 6 × `database is locked` instant boot deaths
(OPEN #11), plus the 7/31 `config error` boot and a `ModuleNotFoundError`
boot — all three genuine boot loops, correctly latched. No healthy corpse
reads NETWORK.

**P2 root cause (mtime tie), measured on the harness scratch tree:** the three
`live_*.log` files written by `build_scratch` share one `st_mtime_ns`
(1 distinct value of 3); old pick `live_20260730_0400.log` (a synthetic tape),
tie-broken pick `live_now.log` (the corpse).

**Reach probe, run once for real from this pass (unauthenticated GET, nothing
else touched):** prod `{"ok": true, "host": "external-api.kalshi.com", "stage":
"http", "status": 200, "elapsed_s": 0.18, "exchange_active": true,
"trading_active": true}`; against `no-such-host.invalid` `{"ok": false, "stage":
"dns", "error": "gaierror(11001, 'getaddrinfo failed')", "elapsed_s": 0.02}` —
the exact 8/27 error string. Anchor reader: main tree (live local yaml, `env:
prod`) → `external-api.kalshi.com`; worktree (templates only) → the same
(prod default; `demo.yaml` is not consulted).

## Tests

| suite | added | passed | failed |
|---|---|---|---|
| `tests/test_hang_watchdog.py` (16 on main) | 38 (26 in `735e694` + 12 this pass) | **54 / 54** | 0 |
| `tools/ops/prove_watchdog.py` P1–P9 (real CLI on scratch trees) | P8, P9 + P4 rewrite + P2 class check | **9 / 9 proofs, 59 / 59 checks** (`ALL PROOFS PASS in 283s`); run 1 on `735e694` + the two small edits was 61 / 62 (P2 class check red = item #5) | 0 |
| full suite (`PYTHONPATH=src … -m pytest -q -p no:cacheprovider`, on `111b4bb`) | — | **3816 passed** (main: 3,778 + the 38 added), 3 deselected, 323 s | 0 |

Unit tests pin: the 8/27 corpse → NETWORK (`ClientConnectorDNSError`); the
8/6 corpse → NETWORK (`startup_reconcile_failed: HTTP 503`); the 8/27 replay
at the real 242 s threshold (hung PIDs 20708/26552, no heartbeat) → `STOP`,
waits **242 / 484 / 726 s**, probes dns-fail / HTTP 503 / 200, `START`, no
receipt, relight record `boot_death=NETWORK, reach_waits=3`; the 8/6 replay →
waits 100 / 200, probe 503 then 200, relit; 12 consecutive NETWORK deaths → 12
relights, waits `100,100,200,300,…` capped at 900, exactly one wait per
escalation (guard 2 never doubles it), never latched; ImportError corpse →
`halt_boot_loop`, receipt "death class CONFIG_CODE: ImportError", probe never
consulted; no log → UNKNOWN latch (pre-build behaviour); probe crash → one wait
then relight, logged "probe crashed — relighting anyway"; human-only-clear
KILL corpse → `halt_human_kill` (unchanged); machine KILL corpse → relit;
`KalshiApiError` 5xx terminal (incl. the multi-line HTML body the real str
carries) → NETWORK, 4xx / 429 → CODE, 401 reconcile failure + refusal → CODE,
the real 8/6 event ordering → NETWORK, a 5xx from an EARLIER boot → UNKNOWN;
mtime-tie corpse pick; probe stage ladder (dns fail / 200 / 503 / URLError)
incl. the 10 s bound and `/exchange/status` URL; live-yaml anchor precedence
for both the CLI reader and `Watchdog()`'s default probe. Nothing existing was
weakened: all 16 main-tree tests untouched and passing (`test_boot_failure_
never_relights` still latches: no corpse ⇒ UNKNOWN).

## Harness (`tools/ops/prove_watchdog.py`, the real CLI against scratch trees)

Run on the final tree (`d3a5777` + `111b4bb`), unloaded box, `ALL PROOFS PASS
in 283s`, rc 0. Each proof drives the real `hang_watchdog.py run` CLI against
a scratch tree (synthetic tape → derived threshold 5 s, fake pid/stop/start
seams, real sqlite decisions store, `--reach-cmd` seam for P8/P9):

| proof | class pinned | checks | result |
|---|---|---|---|
| P1 frozen_log_hang | 7/29: PID alive, log+store frozen → one stop+start, relit run left alone | 5 | PASS |
| P2 boot_loop | 7/31 09:00: instant `config error` exit → stop only, latch, receipt names **`death class CONFIG_CODE: config error`** | 6 | PASS (was red on `735e694`: item #5) |
| P3a/b healthy_lull / store_axis | lull inside threshold; frozen log with live decision flow → zero actions | 3 | PASS |
| P4 flap_backoff | relit runs re-hang → no latch, ≥3 relights, "flap streak 2: backing off 10s" | 5 | PASS (crashed on the unmodified tree since 8/6) |
| P5 start_refused_retry | 7/31 17:34: helpers alive → refused → re-sweep → relight | 6 | PASS |
| P6 kill_still_refuses | human-gated KILL survives re-sweep → refused twice → latch | 4 | PASS |
| P7 ps1_guard_parity | shipped stop/start predicate over the 17:34 table + decoys | 14 | PASS |
| **P8 network_boot_death** | **8/27 corpse (verbatim)** → `stop, reach_fail, reach_fail, reach_ok, start`; waits 5/10/15 s; no latch; record `boot_death=NETWORK, reach_waits=3` | 8 | PASS |
| **P9 maintenance_boot_death** | **8/6 corpse (verbatim)** → `stop, reach_fail, reach_ok, start`; waits 5/10 s; no latch; `reach_waits=2`; log names `startup_reconcile_failed: HTTP 503` | 8 | PASS |

Run 1 (before this pass's fixes, `735e694` + the aiohttp-names/anchor edits):
61 / 62 — the single FAIL was P2 "latch reason names the death class
(CONFIG_CODE)" (the mtime tie, item #5); P1 passed unloaded in both runs.

## Parity

Rule 8 parity here is prototype-vs-live on the same inputs: the prototype ran
`classify_death` — the very function that ships (the watchdog is a tools/ops
module, not a src/ engine) — over all 271 recorded corpses; the unit tests and
P8/P9 re-run the identical fixtures through the identical function and the
real CLI. Verdicts identical in all three (8/27: NETWORK /
`ClientConnectorDNSError`; 8/6: NETWORK / `startup_reconcile_failed: HTTP
503`).

## Quote-ability counterfactual

Not applicable — no pricing, cap or quote-path change; `src/` is not in the
diff. The operational counterfactuals:

* **8/27 replay with this build:** the 02:13 escalation would have waited
  242 s, probed (DNS down until ~06:27 ET → `stage: dns` unreachable), kept
  waiting at 484, 726, 900, 900 … s (≈ 20 probes over the ~4 h flap), and
  relit on the first reachable probe — instead of latching for 8 days.
* **8/6 03:13 replay:** the maintenance-window boot death would have waited
  threshold × 1, probed (HTTP 503 → not ok), backed off to the 900 s cap and
  relit on the first 200 (Kalshi's Thu 03-05 ET window is ~8 capped waits) —
  instead of latching one guard earlier than the latch the 8/6 rework removed.

Every wait and probe result is logged with the class.

## Blast radius

`tools/ops/hang_watchdog.py`, `tools/ops/prove_watchdog.py`,
`tests/test_hang_watchdog.py`, two fixtures. The watchdog imports nothing from
`combomaker`; the pricing hot path, the store writer and the lifecycle are
untouched. New external effect: while a NETWORK-class boot death keeps the tree
down, one unauthenticated `GET /exchange/status` per backoff wait (≥ threshold
apart, ≤ 900 s cap). Wire-up is unchanged (`start_all.ps1` launches the
watchdog; the change is live at the next operator START). Repo hygiene: no
scratch folders left (none existed in the worktree); `config/` holds only the
two templates; both fixtures are LF in the index (`core.autocrlf=true`), the
three Python files are LF in the working tree.

## Operator yaml

None. No new config key; the probe host and timeout are read from / mirrored
off what already exists.

## NEXT STEPS

- **Orchestrator**: merge `build/watchdog-network-boot-death`; the change arms
  itself at the next operator `START_BOT.bat` (which also purges the 8/27
  latch in `watchdog_state.json`).
- **Me, at the relight**: confirm the watchdog's derivation line shows
  `reach_host: external-api.kalshi.com`; on the next NETWORK-class death read
  `hang_watchdog.log` for `corpse death class` / `exchange reach probe` lines.
- **Decision owed (operator)**: OPEN #11 — the `database is locked` instant
  boot deaths (two real latches, 8/14 and 8/15) are a local transient class;
  wants its own item (store-lock probe on the same retry path, or writer lock
  discipline). OPEN #10 (429 at boot) stays fail-closed unless it ever fires.
- If `prove_watchdog` P1 flakes again on an unloaded box, its 5 s scratch
  threshold vs two cold Python spawns needs a measured fix, not a bumped number.
