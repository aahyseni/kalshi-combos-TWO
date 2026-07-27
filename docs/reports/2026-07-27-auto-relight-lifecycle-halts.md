# AUTO-RELIGHT FOR LIFECYCLE-CLASS HALTS — BUILT, VERIFIED, READY FOR THE ONE RESTART

**Date:** 2026-07-27
**Scope:** `src/combomaker/ops/relight.py` (new), `ops/quote_app.py`, `rfq/lifecycle.py`,
`tools/ops/{start_all,stop_all,watch_main}.ps1`, `tests/test_auto_relight.py` (new)
**Status:** **SHIPPED to the working tree. Suite 3047/0 (+37). Throughput NEUTRAL.
Bot untouched — nothing here takes effect until the operator's one restart.**
**Blast radius:** the 15 s status loop, the terminal `on_halt` path, and a new
out-of-process parent. **Zero** files changed under `pricing/`, `marketdata/`, or
`exchange/`. The quote hot path is not on the diff.

---

## 1. What was built, in one picture

```
 BEFORE (measured)                         AFTER (this build)
 ────────────────                          ──────────────────
 cmd /k   (dumb shell, survives)           cmd /k   (dumb shell, survives)
  └─ python -m combomaker.ops.cli run       └─ python -m combomaker.ops.relight   ← OUTLIVES THE
      └─ python (venv launcher SHIM)            │  (no credential, no socket)        HALT BY BEING
          └─ python -m ...supervisor            └─ python -m combomaker.ops.cli run  THE PARENT
               ^ dies 32 ms after the halt          └─ python (venv SHIM)
 ⇒ NOTHING outlives a halt ⇒ a human                    └─ python -m ...supervisor
   must clear it before quoting resumes                      ^ still dies with the bot
```

The relighter restarts the bot for **exactly one** halt class and escalates
terminally for everything else. It never reads a KILL file for attribution and
never deletes one.

---

## 2. The load-bearing invariant: auto-relight never touches a KILL file

A lifecycle-class halt **provably writes no KILL**. `QuoteApp.on_halt` drops the
`needs_reconcile` marker, cancels all, and stops; nothing in `quote_app.py` writes
KILL. Every KILL writer in the tree is therefore *outside* the relightable class
by construction:

| KILL writer | Where |
|---|---|
| supervisor stall / wedge kill | `supervisor.py:207 _write_kill_file` |
| `combomaker halt` (CLI) | `cli.py:120` |
| a human, by hand | — |

So the rule is **"relight only when there is NO KILL" (G5)**, not "clear the KILL
I can attribute". There is nothing to attribute and no attribution bug available.
This is strictly stronger than the KILL-parsing approach and has zero surface.

---

## 3. The halt receipt — evidence, plus a claim the evidence re-derives

`data/halt_receipt.json`, written **atomically** (the existing
`risk/heartbeat.py::_atomic_write`) by the halting bot, as the **first statement**
of `on_halt` — before `cancel_all`, before `_stop.set()`, fully wrapped so a
receipt failure can never block the cure.

Every field is already computed at the sites it comes from. The only genuinely
new datum is `withdraw_failure_kinds`, derived at the failure site by
`lifecycle._withdraw_failure_kind` (an HTTP status names itself; anything else
names its exception class; a never-asked quote is `budget_deferred`).

### The 8 read gates — default deny

| # | Gate | Refuses |
|---|---|---|
| G1 | receipt present + parses + schema v1 (relighter `unlink`s before every launch) | STOP_BOT, crash, window closed |
| G2 | `run_id` == the nonce minted for THIS child, passed by **env only** | stale receipt, second stack, hand-written file |
| G3 | spawn identity: `expected_pid ∈ {pid, ppid}` | an unrelated process's receipt |
| G4 | child exit code == 0 | crash, red preflight (3), credentials (2) |
| G5 | **KILL absent** | supervisor kill, `combomaker halt`, human KILL |
| G6 | `needs_reconcile` present **and** body == `halt_metadata_change` | a halt that armed no reconcile; a disagreeing writer |
| G7 | **claim re-derived from evidence**: reason ∧ `tripwire_hit is null` ∧ every ticker in `verdict_detail` has evidence ∧ `settlement_fp_prior == settlement_fp_new` ∧ `¬regraded` ∧ `status_class=="lifecycle"` ∧ `quarantine_armed` ∧ `origin=="quarantine_unenforced"` | settlement/payoff moves, re-grades, disputes, the taxonomy tripwire, unarmed quarantines, unmodelled statuses |
| G8 | `halt_class ∈ _RELIGHTABLE_HALT_CLASSES` (a frozenset in **code**, not YAML) | any new class, by default |

G6 is the one that makes hazard 3 structural: the relighter **requires** the
reconcile marker to be present, so it can only ever relight *into* a mandatory
exchange reconcile.

**Three ReasonCodes are indistinguishable without G7.** `HALT_METADATA_CHANGE` is
returned by `detect_metadata_change` for the taxonomy tripwire, for settlement
moves, **and** for the unenforced-quarantine promotion. The reason code alone can
never attribute — only the re-derivation can, and it works off the evidence, never
the label. Test `test_settlement_class_halt_does_not_relight` includes a receipt
that **lies** (claims the lifecycle class and origin while carrying a moved
settlement fingerprint); it is rejected by its own contents.

---

## 4. 🔴 A REAL BUG, FOUND ONLY BY RUNNING REAL PROCESSES

Every fake-child test passed. Then the relighter was run against **actual OS
subprocesses** and refused its own receipt:

```
[error] relight_refused  refusal_gate=G3
        detail='pid mismatch: receipt=31712 expected=34312'
```

**Cause.** On Windows `.venv\Scripts\python.exe` is a **launcher shim** that
re-spawns the real interpreter as a child with an identical command line — the
very fact `start_all.ps1` already compensates for when it counts bot ROOTS ("one
sleeper launch = 2 processes"). So `os.getpid()` inside the bot is the shim's
child, **not** the pid the relighter spawned. G3 as first written could never
match in production.

**Confirmed on the LIVE process tree** (bot running while this was built):

```
PID 25828  .venv\Scripts\python.exe -m combomaker.ops.cli run   ← what a parent spawns
PID 30444  .venv\Scripts\python.exe -m combomaker.ops.cli run   ← the real interpreter = os.getpid()
PID 23212  ...supervisor                                        ← the bot's child
```

**Impact had it shipped:** every lifecycle halt would have refused at G3. The
feature would have been a **silent no-op** that passed its entire test suite —
precisely the failure mode the operator memory warns about ("100%-decline passes
every safety test + is useless"). It fails safe, but it fails *useless*.

**Fix.** The bot records **both** `pid` and `ppid`; G3 accepts either. Exactly one
shim level; an unrelated process still refuses, so the gate is loosened by a
*measured fact*, not blunted. Pinned by `test_g3_accepts_the_venv_launcher_shim_level`
and `test_the_bot_records_both_pids`.

---

## 5. The crash-loop bound — derived, two independent layers

Neither is a number a human can move.

**B2 — NOVELTY.** One relight per distinct **root-cause signature** per relighter
session. The signature is `origin:<sorted distinct withdrawal failure kinds>`,
computed at the failure site. A repeat means the bot has *demonstrated* it cannot
self-cure that mode ⇒ human. Session total is bounded by the number of distinct
exchange failure modes actually observed. "Session" = the relighter's own lifetime
= the operator's launch→stop span, an existing boundary. A human restart resets it,
which is correct: a human looked.

**B3 — AMORTIZATION.** Grant relight *i+1* iff `work_i ≥ cost_i`. "A relight must
be paid for by observed productive work" has exactly **one** scale-free breakeven;
any multiple (1.5×, 3×) would be a tuned number. Consequences: a crash loop has
`work → 0` while `cost` stays at boot time ⇒ refused on the **first** repeat, so
**the mechanism can emit at most one unpaid relight, ever**; duty cycle can never
fall below 50%.

**PRODUCTIVE** ≡ `needs_reconcile` absent ∧ heartbeat not wedged ∧ no loop stalled.
Because "productive" *is* "the reconcile provably cleared", amortization and
reconcile-proof are the **same measurement**.

**No boot timeout constant.** After the child launches its supervisor, the
supervisor's own derived bound kills a hung bot and writes KILL ⇒ G5 ⇒ terminal.
Before that, the relighter applies the **same single operator anchor**
`supervisor.heartbeat_timeout_s` (**verified = 30.0** loaded from the live
`config/prod-live-wc.local.yaml`) to the bot's heartbeat, with `ProgressReader`'s
latch shape. One anchor, already owned by the operator, reused.

---

## 6. REQUIRED TESTS — actual numbers

Full suite **3047 passed, 3 deselected in 188.93s** (baseline at HEAD: 3010 passed,
3 deselected). `ruff` clean on every changed file, `mypy` clean on all three modules.

### 1. Lifecycle halt auto-relights, and the reconcile RUNS before quoting resumes

Driven through the **real** three-tick sequence on the **real** Kalshi CLETB
payload (seed → `status: active→inactive` scoped quarantine → enforcement fails →
promotion):

```
receipt.halt_class          = lifecycle_quarantine_unenforced
receipt.root_cause_signature= quarantine_unenforced:429,TimeoutError
evidence.settlement_fp_prior== settlement_fp_new   (payoff byte-identical)
evidence.status_class       = lifecycle    regraded=False  settlement_moved=False
evidence.quarantine_armed   = True
evidence.withdraw_failure_kinds = {'429': 3, 'TimeoutError': 1}
needs_reconcile body        = "halt_metadata_change"   (written by the OTHER path)
DECISION                    = GRANT
```

Reconcile proven to run **before** quoting:

```
prod preflight BEFORE reconcile -> PreflightError: book_reconciled   (RED, refuses to quote)
_block_restart_until_reconciled -> rest.deleted == ['leftover-1','leftover-2']   (exchange round-trip RAN)
_book_reconciled                -> True     (only after the proof)
needs_reconcile                 -> cleared by the PROOF, never by the relighter
```

### 2. Everything else refuses, and the KILL is left for a human

| Halt | Gate | Result |
|---|---|---|
| PAYOFF/settlement (strike 5.5→6.5, rule rewritten) | **G7** | refuse |
| re-grade (`result` no→yes) | **G7** | refuse |
| `status → disputed` | **G7** | refuse |
| taxonomy tripwire (same ReasonCode) | **G7** | refuse |
| reconciliation mismatch | G6/G7 | refuse |
| rate-limit burst | G6/G7 | refuse |
| drawdown / fill-velocity / hard-trip / data-stale / unmapped-game | G6/G7 | refuse |
| **supervisor heartbeat kill** | **G5** | refuse, **KILL left on disk** |
| **human KILL of unknown provenance** | **G5** | refuse, **KILL left on disk** |
| STOP_BOT / crash | G1 / G4 | refuse |
| unarmed quarantine (wiring bug) | **G7** | refuse |
| unmodelled status string | **G7** | refuse |

All **five historical metadata halts** (7/25 18:50, 7/25 20:06, 7/26 00:30,
7/26 14:15, 7/26 18:15) replayed as receipts → **5/5 refuse** (all settlement-lane).

### 3. Crash loop is BOUNDED — real processes, real timings

A real child that boots, becomes productive, then halts immediately with the
**same** signature:

```
attempt 1: exit=0 cost=0.233s work=0.601s duty=0.721  grant=True   gate=grant  sig=quarantine_unenforced:429
attempt 2: exit=0 cost=None  work=None   duty=None    grant=False  gate=B2     sig=quarantine_unenforced:429
=== RELIGHTER EXIT 1 after 1.7s ===   session relights=1
```

**Exactly one relight, then terminal exit 1.** The bound engaged is **B2
(novelty)** — the signature came from the withdrawal path's own failure
classification, not from any threshold. Under the fake-clock unit test the
**B3** bound engages independently on the same shape (`work_i=1s < cost_i=2s`),
so either alone terminates the loop.

Where the bound came from: **B2** = the set of distinct exchange failure kinds
observed at `lifecycle._withdraw_batch`; **B3** = `work_i ≥ cost_i`, both measured
by the relighter from `HeartbeatReader` / `ProgressReader` / `ReconcileMarker`.
Neither is configurable.

### 4. A relight cannot proceed if the reconcile fails

```
FakeRest(fail=True) -> _block_restart_until_reconciled
  _book_reconciled            = False    (never unblocked)
  needs_reconcile             = PRESENT  (stays in force)
  rest.deleted                = []       (nothing assumed gone)
  prod preflight              -> RED -> PreflightError -> cli exit 3
  relight decision(exit_code=3) = REFUSE at G4  -> terminal escalation
```

Also pinned: a KILL outranks even a *successful* reconcile (marker stays,
`_book_reconciled` False), so the bot's own gate and G5 agree.

### 5. Flapping vs a normal slate — real processes

Four end-of-game waves, each separated by proven productive work, each a
**different** exchange failure mode:

```
attempt 1: cost=0.233s work=3.608s duty=0.939  GRANT  sig=quarantine_unenforced:429
attempt 2: cost=0.814s work=3.006s duty=0.787  GRANT  sig=quarantine_unenforced:503
attempt 3: cost=0.815s work=3.006s duty=0.787  GRANT  sig=quarantine_unenforced:TimeoutError
attempt 4: cost=0.813s work=3.006s duty=0.787  GRANT  sig=quarantine_unenforced:ConnectionResetError
attempt 5: cost=0.812s work=3.007s duty=0.787  REFUSE gate=B2 (signature repeats)
=== 4 relights, dead=3.49s productive=15.63s, every duty >= 0.5 ===
```

Budget is **not** exhausted by having many games: B2 keys on the *root cause*, not
the halt count, and B3 keys on *observed productive work*, not elapsed time. The
unit test adds the mirror case — a novel mode arriving with 0.5 s of work behind it
is refused by **B3**.

### 6. Observability

Asserted on captured structlog events, not on substrings:

| Event | Level | Key fields |
|---|---|---|
| `relight_granted` | **error** (deliberately loud) | `halt_class`, `tickers`, `signature`, `cost_s`, `work_s`, `session_relights` |
| `relight_refused` | **error** | `refusal_gate`, full receipt echo |
| `relight_terminal_escalation` | **error** | `refusal_gate`, `why`, `human_must_check`, `last_log` |
| `relight_child_launched` / `_exited` / `_productive` | info / warning / info | `run_id`, `pid`, `exit_code`, `work_s`, `duty_cycle` |
| `halt_receipt_written` | error | `halt_class`, `signature`, `markets` |

Plus append-only `data/relight_ledger.jsonl` (one record per decision carrying
**all** inputs, so any decision is reconstructible offline) and
`data/relight_status.json`.

"Quietly relights 20 times overnight" is structurally impossible: 20 grants needs
20 **distinct** exchange failure signatures, each **paid for** by ≥ its own boot
cost of proven quoting, each emitting an error-level line and a ledger record.

### 7. Existing suites all green

```
tests/test_metadata_change_scope.py  tests/test_liveness_progress.py
tests/test_supervisor.py             tests/test_maintenance_stall_and_read_budget.py
tests/test_withdraw_resolution.py    tests/test_withdraw_accept_race.py
                                              -> 177 passed in 28.59s
```

One existing double (`_FailingLifecycle`) needed the new read-only property. The
production read was **also** made defensive (`try/except → {}`): attribution sits
on the **escalation path of a fail-closed halt**, and an observability field must
never be able to break the cure it describes.

### Write-set proof (hazard 3, measured not asserted)

A full grant→refuse cycle against a real directory diff:

```
created by the relighter = {relight_ledger.jsonl, relight_status.json}
needs_reconcile          = untouched ("halt_metadata_change")
KILL                     = never created
.unlink( call sites in ops/relight.py = 1  (the receipt, G1's premise)
```

---

## 7. THROUGHPUT PARITY (hard rule) — before/after

"Before" is `git archive HEAD` extracted to a scratch tree. **The working tree was
never stashed** — a live bot with a `multiprocessing spawn` pricing pool was
running, and mutating source under it could have had a new worker import mixed code.

**A. Quote hot path** — `tools/profile_pricer.py`, 180 real tape combos, fixed seed:

| Trial | BEFORE | AFTER |
|---|---|---|
| 1 | 28,110 ms | 28,564 ms |
| 2 | 29,109 ms | **27,799 ms** |
| | ≈6 combos/s | ≈6 combos/s |

AFTER is **faster** in one of two paired trials ⇒ noise (the live bot competes for
CPU). An early unpaired BEFORE read 8 combos/s purely because the machine was
briefly quiet — which is exactly why the trials were alternated.

**B. Status loop** — `_sample_breaker_inputs → _metadata_changes`, 200 markets,
300 ticks, half the book walking `active→inactive` every other tick (so the new
evidence recording is exercised, not skipped):

| Arm | median (4 runs) | mean |
|---|---|---|
| BEFORE | 11.549 / 11.967 / 12.307 / 11.690 ms | **11.878 ms** |
| AFTER | 11.724 / 12.463 / 11.868 / 11.811 ms | **11.967 ms** |

**+0.089 ms (+0.7%), with the ordering flipping between trials.** Against the
15,000 ms status tick that is **0.079% of the budget**.

**Structural check:** `git diff --stat` touches `ops/quote_app.py`,
`rfq/lifecycle.py`, one test, and three `.ps1` files. **No `pricing/`,
`marketdata/`, or `exchange/` module changed** — the quote hot path is not on the
diff at all.

---

## 8. Launcher wiring — verified against the REAL regexes

Done **last**, so the operator's one restart is the first launch under the relighter.

| Script | Change | Verified |
|---|---|---|
| `start_all.ps1` | bot invocation → `-m combomaker.ops.relight --env prod --mode quote --confirm-live --config config\prod-live-wc.local.yaml`. **KILL y/n prompt untouched** — it is the human gate | relighter vs `combomaker\.ops\.cli run` → **False** ⇒ duplicate check still counts exactly 1 bot root; vs `combomaker\|fill_prober` → **True** ⇒ a second START_BOT still refuses |
| `stop_all.ps1` | classification `'supervisor'` → `'supervisor\|relight'` | relighter killed in the **first** group ⇒ no spurious escalation line on the way out |
| `watch_main.ps1` | `relight_` + `halt_receipt_written` added; grants/refusals/escalations **RED**, lifecycle events white | — |

The relighter deliberately does **not** carry the bot argv on its own command line
(it builds it via `build_child_argv`), which is what keeps `bots.Count == 1`.

Paths are taken from the **bot's own config**, not from independent CLI defaults,
so the relighter can never watch a different `data_dir`/KILL than the bot it
supervises: loaded live → `heartbeat_timeout_s=30.0`, `data_dir=data`,
`kill_file=KILL`.

---

## 9. Hazard → prevention

| Hazard | Prevented by | Tuned number? |
|---|---|---|
| 1. Crash loop | B2 (novelty) **and** B3 (`work ≥ cost`), independently sufficient — demonstrated terminating in 1.7 s with real processes | none |
| 2. Clearing the wrong KILL | The relighter never reads a KILL for attribution and never deletes one; a KILL on disk is an unconditional refusal (G5). Attribution runs off a receipt whose claim is re-derived from its own evidence (G7) | none — policy is a code frozenset |
| 3. Skipping reconcile | Identical argv; **G6 requires the marker present**; "productive" *is* marker-cleared; reconcile failure ⇒ preflight red ⇒ exit 3 ⇒ G4 terminal; write set measured | none |
| 4. Silent degradation | Every grant error-level + ledgered; refusal is terminal (non-zero exit, `cmd /k` window stays open); duty ≥ 50%; repeats structurally impossible | none |
| 5. Wrong owner | The relighter is the bot's **parent** — it cannot be reaped by its child | n/a |

---

## NEXT STEPS

**Runs next (no action needed from me):**
1. The operator's single restart before 14:35 ET first pitch launches
   `START_BOT.bat` → the relighter → the bot. Expect `relight_supervisor_starting`
   then `relight_child_launched` in the MONITOR window, followed by the usual boot
   lines. That restart also checkpoints the growing WAL, as planned.
2. First real exercise is whichever end-of-game wave first leaves a quarantine
   unenforced. Watch for `halt_receipt_written` → `relight_granted` (RED).

**Owner: operator — decisions owed.**
- **D1 — scope, unchanged from the design and still open.** The class this
  auto-relights has fired **zero** times since `aeed109`. The class that actually
  fired three times in 30 h is the **supervisor stall kill** (`KILL` = "loop
  stalled: maintenance age=30.9s > 30.5s"), and auto-relight refuses every one of
  those at G5, **correctly**. This build is what was ordered and it is safe, but it
  will sit idle if the recurrent outage stays the stall kill. Re-aim at that class
  next, or wait for evidence?
- **D2 — relightable set.** Confirm `_RELIGHTABLE_HALT_CLASSES =
  {"lifecycle_quarantine_unenforced"}` only, with `unarmed` and unmodelled-status
  staying human. Currently implemented exactly that way.
- **D3 — root fix in parallel.** The 7/26 20:12Z unenforced wave was 63 deletes
  returning UNKNOWN over 27 s during an end-of-game burst. Fixing the withdrawal
  path *removes* the halt rather than recovering from it. Schedule alongside, or
  after?

**Owner: next build session.**
- If D1 says re-aim: the supervisor stall kill writes a KILL by design, so it needs
  a *different* mechanism than this one — do **not** weaken G5 to reach it.
- Consider promoting the two scratch benches (status-loop parity, real-process
  relight smoke) into `tools/` if this pattern recurs; they are currently one-off
  scratchpad scripts referenced in §6–§7.

**Do not touch:** `data/` runtime files, the running bot, or
`config/prod-live-wc.local.yaml`. The relighter's `data/` files are new names only;
no existing runtime file changed format.
