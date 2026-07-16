# 2026-07-16 — Quarantine of the external-LLM continuation + restore to checkpoint `a0f8e0f`

## What happened

The operator handed the repo to an external LLM at checkpoint `a0f8e0f`
(the 2026-07-15 handoff commit on `risk-audit-overnight`). That continuation
went off on tangents and left the tree in an unknown state; the operator asked
to quarantine everything it did and return to the checkpoint.

## What the external LLM left behind (all UNCOMMITTED, zero commits made)

- 53 modified tracked files (+8,567/−1,122) across `risk/`, `ops/`, `rfq/`,
  `exchange/`, `marketdata/`, tests, and docs.
- 14 new untracked files: `rfq/issuance.py`, `rfq/retry.py`,
  `risk/structural_certificate.py`, 4 test files, `tools/windows/` (a C#
  launcher + build script), a compiled `ComboMaker-Live.exe` (22KB launcher),
  and 4 reports (2026-07-15/16 resume states, heartbeat/throughput/structural-
  relief implementation, smooth-quote-flow timeout analysis).
- Its own claims (unreviewed, NOT live-validated): suite 2,213/0 (3 deselected),
  ruff+mypy clean; work on Problems B (supervisor config/run identity,
  run-scoped heartbeat), C (smooth quote flow: terminal `quote_timed_out`,
  POST pacing 3/sec, issuance fenced on comms ACK), and an A-variant
  ("accepted-last-look same-game structural relief", default-off).
- It also edited the GITIGNORED armed config: `max_open_quotes` 60→80,
  conditioned on its own (now-quarantined) supervisor re-enumeration.
- It had launched a prod run via `ComboMaker-Live.exe` at 09:19 ET 2026-07-16;
  that process is DEAD (PID 25484 gone). No ComboMaker process is running.

## Actions taken

| Step | Result |
|------|--------|
| Quarantine branch | `llm-b-continuation` @ `16e34f7` — the ENTIRE working tree committed (67 files), **pushed to origin** (this also backs up the whole `risk-audit-overnight` history, previously local-only) |
| Restore | `git checkout risk-audit-overnight` → clean tree at `a0f8e0f` (code `0f5d6c8`), byte-identical to the handoff checkpoint |
| Armed config | `config/prod-live-wc.local.yaml` verified against handoff §1 (markup 1¢; game/slate 0.30; directional 0.15; `KXWCGOAL: 3.0167`; heartbeat 30s) — one deviation found and REVERTED: `max_open_quotes` 80→60 (handoff: do NOT raise until live headroom measured post-fanout). The LLM's version is preserved at `config/prod-live-wc.llm-b-bak.local.yaml` (gitignored) |
| Suite re-run at checkpoint | **2047 passed / 0 failed** (3 deselected = credential-gated integration), 92s — matches the handoff stamp exactly |
| Live processes | None running; bot DOWN (as the handoff left it) |

## State after this report

- Branch `risk-audit-overnight`, tree clean, suite 2047/0. Baseline restore
  remains `git reset --hard 45164f1`. NOT merged to main.
- The open-problem list is UNCHANGED from the handoff
  (`2026-07-15-HANDOFF-for-llm-review.md` §4): A (ME-overstated caps, HIGH),
  B (heartbeat kill, HIGH), C (throughput ceiling, HIGH), D (DB lock),
  E (pregame offsets), F (caps pin 1-game book), G (candidate gate unvalidated).
- The quarantined branch MAY contain salvageable B/C work — treat it as an
  unreviewed external patch: adversarial review against the handoff constraints
  (E2 monotonicity for anything touching caps; rule-8 prototype→port→parity)
  before cherry-picking anything. Do NOT merge it wholesale.

## NEXT STEPS

1. **Fix B (heartbeat)** — pass `--config` to the supervisor subprocess
   (`quote_app.py:1238-1244`) and/or thread-based beat. Owner: agent.
2. **Fix C (throughput)** — cache per-game structural fit, shed load pre-pricer.
   Owner: agent.
3. **A durable fix** — last-look MC worst-case gate (prototypes:
   `proto_structural_book_mc.py`, `proto_mutex_game_cap.py`,
   `proto_mutex_directional.py`). Owner: agent + operator sign-off.
4. **Triage `llm-b-continuation`** — adversarial review of its B/C/A-relief
   diffs for salvage vs. redo. Owner: agent; operator decides what lands.
5. **Decisions owed by operator:** corners +3¢ edge-floor deploy; relaunch go;
   merge-to-main (blocked until A+B fixed and live-re-validated).
