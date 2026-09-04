# 2026-09-04 — BUILD: watchdog retries NETWORK-class boot deaths (no more latch on a Wi-Fi flap; exchange 5xx classified NETWORK defensively)

Item C (9/4 list item 5). Branch `build/watchdog-network-boot-death`. Blast
radius: **`tools/ops/` only — `src/` untouched; the pricing/quote path is not
in the diff.** The bot stayed DOWN throughout (nothing started; the live store
was never opened; corpse logs on `D:` were read as files, read-only). Built in
two passes: the first builder landed the mechanism (`735e694`) and died on a
usage limit with the harness un-run; this pass verified it, found four gaps
(below), finished the harness and ran everything. A third pass (the review
fix pass, same day) applied the adversarial review: one **retraction** (row 3 —
the 8/6 corpses were post-heartbeat), three classifier / probe hardening
fixes, two count nits and one new OPEN item — see "Review fixes" at the end.

## WRONG / FIXED / OPEN

| # | Item | Was | Now | Status |
|---|------|-----|-----|--------|
| 1 | Flap guard 1 latched the 8/27 02:13 ET relit boot ("run never reached liveness — relighting would boot-loop") after it died 45 s in on `aiohttp.ClientConnectorDNSError ... [getaddrinfo failed]` (Wi-Fi flap, network re-identifications 01:32–06:27 ET); nothing retried → 8-day outage | **WRONG** — a pre-heartbeat death latched regardless of cause | Corpse log is **classified**: NETWORK → no latch, backoff (threshold × streak, cap 900 s) gated on an exchange reach probe, retry forever; CONFIG_CODE / UNKNOWN → latch exactly as before, class named on the receipt | **FIXED** `735e694` |
| 2 | `_corpse_human_kill_reason` and the classifier each need the corpse tail | duplicated read | one shared `_corpse_tail()`; human-KILL contract pinned by two tests | FIXED `735e694` |
| 3 | **Exchange 5xx.** The two REAL maintenance-window deaths on the tape (`live_20260806_0313` / `_0316`: `startup_reconcile_failed` on `KalshiApiError('HTTP 503 ...')` → `REFUSING TO QUOTE ... book_reconciled` → `supervisor_exchange_unreachable` on the same 503) classified **CONFIG_CODE** after the first pass. **RETRACTION (review fix pass):** the earlier text of this row said they "would have latched … one guard earlier" — FALSE. Both were **POST-heartbeat** deaths: the watchdog's own escalations (`D:/kalshi-combos-TWO-data/hang_watchdog.log` 8/6 03:16:35 and 03:20:05 ET) read `heartbeat_present: true, healthy_span_s: 74.8 / 74.7`, and the 03:16:45 relight auto-cleared a machine KILL, which `start_all.ps1` does only when the previous run reached liveness (`$prevRunReachedLiveness`, l.82 / l.130). They were flap guard 2's class, already retried by the 8/6 rework; guard 1 never saw them and structurally cannot: `quote_app.py` `run()` beats the heartbeat (l.2608) BEFORE launching the supervisor (l.2617) and running the preflight (l.2621), and every pre-beat exchange call catches `KalshiApiError` (slate count l.4621, startup reconcile l.3232 / l.3265, risk snapshot l.3731 catch `Exception`; rehydrate l.3414 catches `KalshiApiError` only) | classifier read CONFIG_CODE — a forensic mislabel; no latch was ever at stake | 5xx on a reach event's `error`, or a terminal `KalshiApiError` 5xx traceback, is NETWORK — **forensic** (the class is logged on every escalation) and **defensive** (a future pre-beat call that lets a 5xx escape). Still derived, not invented (`supervisor.py` l.273-279 "cannot reach exchange", `exchange_reachable: false` on that 503; `rest.py` l.37-40 str). 4xx / 429 stay CODE. **On the live code today the ONLY guard-1-reachable NETWORK class is rule 1 — an uncaught transport traceback, the 8/27 shape — and that is what this build fixes.** | FIXED `d3a5777` (mechanism); premise **RETRACTED** (fix pass) |
| 4 | Refusal rule (`REFUSING TO QUOTE` on `book_reconciled` after a network reconcile failure — forensic: the preflight runs after the heartbeat beat, so this print is always a guard-2 death) only looked at the NEWEST network event — on the real 8/6 corpse the supervisor's own 503 event lands AFTER the refusal print and shadowed the reconcile cause | WRONG (latent; the synthetic test had the events in the convenient order) | any network-caused `startup_reconcile_failed` in THIS boot decides; pinned with the real ordering | FIXED `d3a5777` |
| 5 | `_corpse_tail()` picks the newest `live_*.log` by mtime only. Windows stamps file times off the ~15.6 ms system tick, so logs written in one burst tie — **measured** on the harness's P2 scratch tree: 3 logs, 1 distinct `st_mtime_ns`, old pick = the synthetic 7/30 tape, not the corpse → P2's new class check was RED on the committed tree | WRONG (harness never run by the first pass) | ties broken by name (`live_YYYYMMDD_HHMM.log` sorts chronologically); pinned by `test_corpse_tail_reads_the_newest_log_name_on_an_mtime_tie` | FIXED `d3a5777` |
| 6 | Marker list claimed "the installed aiohttp 3.14.1 `ClientConnectionError` family, enumerated from the library" but missed `ClientSSLError` and `ServerFingerprintMismatch` (re-enumerated: **16** names — the first text of this row said 15, a count nit the review caught) | inaccurate | both added; the claim is now exact | FIXED `d3a5777` |
| 7 | `Watchdog()` with no injected probe aimed at a hard-coded prod URL while the CLI path read the live yaml | inconsistent | `__post_init__` uses the same `_read_exchange_anchors(root)`; pinned | FIXED `d3a5777` |
| 8 | `tools/ops/prove_watchdog.py` P4 still pinned the pre-8/6 permanent flap latch and **crashed** (`AttributeError: 'NoneType' ... .get`) on the unmodified tree — the harness had been red since `79d44ba` (8/6) | WRONG (pre-existing) | P4 rewritten to the shipped 8/6 semantics; P2 checks the class; **P8** replays the 8/27 corpse and **P9** the 8/6 corpse through the real CLI | FIXED `111b4bb` |
| 9 | Watchdog "never talks to Kalshi" | true | ONE unauthenticated, read-only `GET /exchange/status` per backoff wait, only while a NETWORK-class boot death keeps the tree down; docstring updated | changed, documented |
| 10 | HTTP 429 (`RateLimitedError`) as the terminal cause of a boot death | latch | still latches (CODE): throttling after rapid relights vs our own budget misconfig is not readable from the corpse — fail-closed on the ambiguous. No such corpse exists on the tape (0 / 271) | **OPEN** — revisit only if it ever latches |
| 11 | `sqlite3.OperationalError: database is locked` instant boot deaths (6 corpses 8/1–8/17, 2.6 KB each; the `WATCHDOG_HALT_20260814_092516` and `_20260815_121104` receipts are exactly this, `healthy_span_s: 0.0`) | latch (CONFIG_CODE) | unchanged — a locked store is a LOCAL transient resource (an analysis session / a lingering writer holding the store), not an exchange-reach failure; outside this item | **OPEN** — own item: a store-lock probe on the same retry path, or the writer's lock discipline |
| 12 | First pass saw `prove_watchdog` P1 flake under load (unit suite running concurrently) | flaky? | two unloaded runs today: P1 PASS both times | closed unless it recurs |
| 13 | **Bot-side root cause of the 8/27 death** (review finding): `src/combomaker/ops/quote_app.py` l.3414 `_rehydrate_exposure_book` catches only `KalshiApiError`, so an aiohttp transport error / `TimeoutError` from `get_positions_paged` escapes and kills the boot BEFORE the heartbeat — the only pre-beat call that does (`_startup_reconcile` l.3232/3265, slate count l.4621, risk snapshot l.3731 all catch `Exception`). The watchdog now compensates; the bot should not die there at all | uncaught | unchanged (`src/` is out of this item's blast radius) | **OPEN** — own build: catch the transport class like `_startup_reconcile` does, log `rehydrate_positions_failed`, continue fail-closed |
| 14 | Reach probe: ANY HTTP 4xx read `ok: False` — a moved path (404) or a WAF on the watchdog's UA (403) would hold a healthy network down forever (STOP only; waits 242/484/726/900/900…; no START ever issued) | WRONG (review) | a 4xx is the exchange ANSWERING about our request: `ok: True`, `stage: http-4xx`; the relight (the bot's own authenticated boot) is the real test. 5xx / no route / non-JSON 200 stay not-ok | FIXED (fix pass) |
| 15 | A bare stdlib `ConnectionResetError` / `ConnectionRefusedError` / `ConnectionAbortedError` read NETWORK with no exchange frame — on Windows a `ProcessPoolExecutor` worker dying at boot (`ops/pricing_pool.py` l.148 / l.670, a code bug) raises `ConnectionResetError: [WinError 10054]` on the pool pipe → would retry forever | WRONG (review) | those three names are frame-gated exactly like the bare `TimeoutError` rule (a `combomaker/exchange/` or `aiohttp/` frame in the traceback); the aiohttp family and `gaierror` stay transport-by-name; reach-event `error` fields keep the full list (exchange calls by construction) | FIXED (fix pass) |
| 16 | Rule precedence: the LAST traceback always won, so a non-fatal `Task exception was never retrieved` transport traceback followed by a genuine CLI refusal (`config error:` / `credentials error:` / `REFUSING TO QUOTE` on a non-network gate) read NETWORK → retry loop on a real refusal | WRONG (review) | whichever of {last traceback, last refusal print} comes LAST in the tail decides — the refusal is `ops/cli.py`'s last word after catching the exception; a traceback after the refusal is still the newer word | FIXED (fix pass) |

## What changed (mechanism, not numbers)

```
  corpse live_*.log (newest by (mtime, name), last 256 KB)
        │  _corpse_tail()  ── shared with _corpse_human_kill_reason (unchanged contract)
        ▼
  classify_death(tail) — scoped to the last "quote_app_starting" (this boot only)
        │
        ├─ 1. Traceback present → TERMINAL exception line decides
        │       transport BY NAME (aiohttp ClientConnectionError family ×16,
        │       gaierror, getaddrinfo failed)                       → NETWORK
        │       bare Connection{Reset,Refused,Aborted}Error or TimeoutError
        │         + a frame in combomaker/exchange or aiohttp        → NETWORK
        │         (no such frame: a pool pipe / gate timeout       → CONFIG_CODE)
        │       …unless a CLI refusal print comes AFTER this traceback → rule 2
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
                     │           ok (HTTP 200 JSON, or ANY 4xx = the exchange answered) → relight
                     │           5xx / no route / non-JSON 200 → wait again (forever)
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
library (16 names); `quote_app.py` l.3238/3267 (`startup_reconcile_failed`, "a
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

Of these **only `_0207` ever reached guard 1** (no heartbeat). The other
twelve — `_0313` / `_0316` included (`heartbeat_present: true` on their
escalations; an earlier version of this paragraph wrongly listed them as
pre-heartbeat — retracted) — are forensic: their runs had a heartbeat, so
they were guard 2's. The 22 CONFIG_CODE verdicts: **14** × `sqlite3.
ProgrammingError: Cannot operate on a closed database` (an earlier version
said 16; the census is 14 + 6 + 1 + 1 = 22 — shutdown-time writer race on
long healthy runs, heartbeat present, never a guard-1 input), 6 × `database
is locked` instant boot deaths (OPEN #11), plus the 7/31 `config error` boot
and a `ModuleNotFoundError` boot — all three genuine boot loops, correctly
latched. No healthy corpse reads NETWORK.

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
| `tests/test_hang_watchdog.py` (16 on main) | 52 (26 in `735e694` + 12 in `d3a5777` + 14 in the review fix pass) | **68 / 68** | 0 |
| `tools/ops/prove_watchdog.py` P1–P9 (real CLI on scratch trees) | P8, P9 + P4 rewrite + P2 class check | **9 / 9 proofs, 59 / 59 checks** (`ALL PROOFS PASS in 283s`); run 1 on `735e694` + the two small edits was 61 / 62 (P2 class check red = item #5) | 0 |
| full suite (`PYTHONPATH=src … -m pytest -q -p no:cacheprovider`) | — | **3830 passed** on the review-fix tree (main: 3,778 + the 52 added), 3 deselected, 262.7 s; `111b4bb` was 3816 / 323 s | 0 |

Unit tests pin: the 8/27 corpse → NETWORK (`ClientConnectorDNSError`); the
8/6 corpse → NETWORK (`startup_reconcile_failed: HTTP 503`); the 8/27 replay
at the real 242 s threshold (hung PIDs 20708/26552, no heartbeat) → `STOP`,
waits **242 / 484 / 726 s**, probes dns-fail / HTTP 503 / 200, `START`, no
receipt, relight record `boot_death=NETWORK, reach_waits=3`; the 8/6 corpse
planted under a SYNTHETIC no-heartbeat escalation (not a replay — the real
8/6 deaths had a heartbeat) → waits 100 / 200, probe 503 then 200, relit; 12 consecutive NETWORK deaths → 12
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
| **P9 maintenance_boot_death** | **8/6 corpse (verbatim)** under a synthetic pre-heartbeat premise (defensive path, not a replay) → `stop, reach_fail, reach_ok, start`; waits 5/10 s; no latch; `reach_waits=2`; log names `startup_reconcile_failed: HTTP 503` | 8 | PASS |

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
* **8/6 03:13 replay: RETRACTED.** The real 8/6 deaths had a heartbeat
  (guard 2) and the 8/6 rework already retried them — this build changes
  nothing about how 8/6 would have gone. The unit test and P9 that plant the
  8/6 corpse under a no-heartbeat escalation prove the DEFENSIVE guard-1 path
  for a hypothetical pre-beat 5xx, not a replay.
* **403 / 404 from `/exchange/status` while a NETWORK-class death keeps the
  tree down (review scenario, measured on `ca558d5` vs this tree):** before —
  `probe_exchange_reach` → `ok: False` forever: STOP only, waits 242 / 484 /
  726 / 900 / 900 … (the review counted 25 probes = 21,252 s and unbounded),
  no START ever issued; after — `ok: True, stage: http-4xx` on the first
  probe → one wait, relight (pinned end-to-end with the real probe function).

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

## Review fixes (2026-09-04, fix pass — adversarial review `SHIP_WITH_FIXES`)

| review item | kind | what changed | evidence |
|---|---|---|---|
| **MUST-FIX — the 8/6 premise was false** (post-heartbeat, guard 2; "would have latched one guard earlier" / "the 8/27 boot had it survived rehydrate" / "only the pre-heartbeat ones (_0207, _0313, _0316)" / counterfactual (b)) | honesty, no code change | retracted in this report (row 3, evidence paragraph, counterfactual (b), tests paragraph, P9 row); `tools/ops/hang_watchdog.py` marker comments (5xx bullet, refusal bullet) now state the post-heartbeat fact with the `hang_watchdog.log` and `quote_app.py` l.2608/2617/2621 citations and say plainly that rule 1 is the only guard-1-reachable NETWORK class; `tools/ops/prove_watchdog.py` header + P9 docstring/print and `tests/test_hang_watchdog.py` 8/6 header, the state-machine docstring and the "had it survived rehydrate" case name all say "synthetic pre-heartbeat premise" | independently re-verified this pass: `hang_watchdog.log` l.1348 / l.1372 `heartbeat_present: true, healthy_span_s: 74.8 / 74.7`; KILL auto-clear at 03:16:45; `quote_app.py` `run()` l.2504 rehydrate → l.2608 beat → l.2617 supervisor → l.2621 preflight; catch clauses l.3232 / l.3265 / l.3414 / l.3731 / l.4621 |
| should-fix: 4xx probe answer held the network down forever | probe | `probe_exchange_reach`: `HTTPError` 4xx → `ok: True, stage: http-4xx`; 5xx / URLError / non-JSON 200 unchanged | HEAD vs tree on a 403 opener: `{ok: False, stage: http}` → `{ok: True, stage: http-4xx, status: 403}`; new `test_probe_exchange_reach_4xx_is_an_answer` (403/404/401 ok, 502 not) + `test_network_boot_death_relights_on_a_4xx_probe_answer` (real probe fn, 8/27 corpse: STOP, one wait, START) |
| should-fix: bare `Connection*Error` read NETWORK without an exchange frame | classifier | `_NETWORK_EXC_NAMES` split into `_AIOHTTP_EXC_NAMES` (16) + `_STDLIB_DNS_NAMES` + `_STDLIB_CONN_NAMES`; traceback rule uses `_TRANSPORT_EXC_RE` (by name) and `_STDLIB_CONN_RE` (`\b`-bounded, frame-gated on `_EXCHANGE_FRAME_RE`, marker `"<name> on exchange call"`); reach events keep the full `_NETWORK_EXC_RE` | HEAD vs tree on the review's E10 (pool-pipe `ConnectionResetError: [WinError 10054]`, no exchange frame): NETWORK → CONFIG_CODE; 6-case `test_classify_death_bare_connection_errors_need_an_exchange_frame` (with a rest.py / aiohttp frame → NETWORK; `ClientConnectionResetError` and `gaierror` need no frame) |
| should-fix: last traceback outranked a later refusal print | classifier | the refusal index is found first; rule 1 runs only when the last traceback is AFTER the last refusal (`tb_at > refusal_at`); docstring precedence updated | HEAD vs tree on the review's E2 (non-fatal DNS traceback then `config error:`): NETWORK → CONFIG_CODE; 6-case `test_classify_death_refusal_after_a_traceback_is_the_last_word` (E2, E3, credentials; refusal then a LATER ImportError / transport traceback → the traceback; the 8/6 shape with a stale traceback keeps its reconcile verdict) |
| nits: "16 × ProgrammingError" (14), "15 names" (16) | report | corrected in place, with the census | tape replay this pass: CONFIG_CODE = 14 `sqlite3.ProgrammingError` + 6 `sqlite3.OperationalError` + 1 `config error` + 1 `ModuleNotFoundError` = 22; aiohttp 3.14.1 family re-enumerated from the venv = 16 |
| add the bot-side root cause as OPEN | report | row 13 + NEXT STEPS | `quote_app.py` l.3414 `except KalshiApiError` is the only pre-beat exchange call that lets a transport error through |
| note: `_wait_for_exchange_reach` calls `time.sleep` directly | none | no change — mirrors guard 2's own wait; the `Watchdog.sleep` seam is the poll cadence (`self.sleep(self.poll_s)`, one call site); tests patch `hw.time.sleep`. Docstring says so now | consistent with main |

**Tape replay after the fixes (read-only, all 271 `live_*.log` tails on
`D:`, HEAD `ca558d5` vs this tree): UNKNOWN 236 / CONFIG_CODE 22 / NETWORK 13
on both sides; NOT ONE corpse changed class or marker.** No recorded corpse
has a bare `Connection*Error` traceback or a refusal-after-traceback shape;
the three hardening fixes bite only on the review's synthetic shapes, all of
which are now pinned.

**Tests (fix pass):** `tests/test_hang_watchdog.py` 54 → **68 passed** (14
added: 6 + 6 parametrized cases + 2 probe / state-machine tests), 0 failed,
1.3 s; ruff check clean on all three files; `hang_watchdog.py` ruff-format
clean. Harness (`tools/ops/prove_watchdog.py`, real CLI on scratch trees, run unloaded BEFORE the suite): **ALL PROOFS PASS in 282s**, rc 0, 9 / 9 proofs, 59 / 59 checks, 0 FAIL — P8 and P9 identical to the previous pass (P9 now prints "synthetic pre-heartbeat premise"). Full suite (`PYTHONPATH=src … -m pytest -q -p no:cacheprovider`, project addopts, run AFTER the harness): **3830 passed**, 3 deselected, **0 failed**, 262.7 s — main's 3,778 + 38 (earlier passes) + 14 (this pass).

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
- **Next build (src/, separate item)**: OPEN #13 — `quote_app.py` l.3414
  `_rehydrate_exposure_book` must catch the transport class (mirror
  `_startup_reconcile`'s `except Exception` + `startup_reconcile_failed`
  logging) so a DNS flap during rehydrate no longer kills the boot
  pre-heartbeat; the watchdog retry is the compensation, not the cure.
- If `prove_watchdog` P1 flakes again on an unloaded box, its 5 s scratch
  threshold vs two cold Python spawns needs a measured fix, not a bumped number.
