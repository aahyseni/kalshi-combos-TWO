# Go-live runbook — tiny, validation-first ($2,000)

**Date:** 2026-07-13. Every command/flag/config key below is verified against the
merged code at `main` (`463a147`): `ops/cli.py`, `config/{demo,prod}.yaml`,
`config.py::assert_safe_to_run`, `ops/supervisor.py`, `ops/preflight.py`,
`risk/settlement.py`. **Framing: this is a VALIDATION run on capital you can
afford to lose, not "it's proven, size up."** The risk engine is enforced-when-
armed and correct-in-code, but (a) the settlement/reconcile path has never run
against a real Kalshi settlement, and (b) the markup/edge is unvalidated. The goal
of the first live is to prove the plumbing + conventions on tiny size and start
the multi-week data that will decide the edge.

## Kill switches (know these BEFORE you start)
- **KILL file:** `touch data/KILL` (the `kill_file: KILL` in the config, watched
  every 1s + checked synchronously at startup). Halts + cancels all.
- **CLI halt:** `combomaker halt --kill-file KILL`.
- **Cancel all quotes:** `combomaker cancel --env {demo,prod}`.
- **The external supervisor** cancels-all + writes KILL on a missed heartbeat.

---

## Phase 0 — Credentials + cap sign-off (off-exchange, do first)

- [ ] **Bot credential** (env only, never committed): `KALSHI_API_KEY_ID` +
  `KALSHI_PRIVATE_KEY_PATH` (or `KALSHI_PRIVATE_KEY_PEM`).
- [ ] **SEPARATE supervisor credential** — a DISTINCT Kalshi API key:
  `KALSHI_SUPERVISOR_API_KEY_ID` + `KALSHI_SUPERVISOR_PRIVATE_KEY_PATH` (or
  `_PEM`). It MUST be a different key from the bot's, so a throttled/compromised
  bot key can't disable the kill path. Without it the supervisor runs KILL-only
  (no cancel) and the prod preflight `external_kill_reachable` gate fails.
- [ ] **Review + freeze the cap values.** The $2k caps (game 8%/$160, per-combo
  1%/$20, directional 10%, slate 8%, daily 6%, drawdown 10%, hard-trip 12%,
  utilization 3×, portfolio-CVaR 15%) are research-derived defaults, now ENFORCED
  by default in quote mode. You own risk appetite — confirm or adjust them under
  `risk:` in the config before real money. (Conservative first-live tightening —
  e.g. game 3-5%, per-combo 0.5-1%, hard-trip 8-10% — is in `RISK_BUILD_PLAN`.)

## Phase 1 — Convention + settlement dry-run on DEMO (the never-run-live path)

The settlement poller → `apply_settlement` → reconcile-to-the-cent-or-HALT chain
has only ever run in tests. Prove it on demo BEFORE prod.

- [ ] Set the **demo** credentials; `env: demo`.
- [ ] **Round-trip a convention check:**
  `combomaker ground-truth --market <a liquid open demo combo market> --contracts 1.00`
  — creates/quotes/accepts/confirms on demo; confirm who ends up long what, the
  fee side, and the position signs match the fixture.
- [ ] **Let a real demo combo fill SETTLE**, with the bot running
  `combomaker run --env demo --mode quote` so the settlement poller reconciles it.
  CONFIRM in the logs: (1) NO spurious `HALT_RECONCILIATION_MISMATCH` on a
  legitimate settlement, (2) realized P&L booked correctly (NO-miss credit / NO-hit
  debit / fee), (3) the position is pruned from the exposure book after settling.
  **A HALT here is a real convention/sign/fee bug — fix it before prod, do not
  suppress it.**

## Phase 2 — Demo quote-mode shakedown

- [ ] Run a full session: `combomaker run --env demo --mode quote` (try
  `--mode paper` first if you want zero order sends). Watch through a game slate:
  - the SafetySupervisor subprocess launches and its heartbeat beats (preflight
    `supervisor_heartbeat_established` + `external_kill_reachable` green);
  - the balance poll anchors `risk_bankroll_cc` (a fresh start no-quotes for ~one
    poll then resumes — expected, fail-closed);
  - caps enforce on real demo RFQs (look for `skip_game_loss_cap`,
    `skip_per_combo_loss_cap`, etc. as breaches, not just logs);
  - no spurious halts; the reconcile/settlement loop is quiet-or-correct.

## Phase 3 — Arm PROD, tiny

- [ ] **Deposit $2,000** to the Kalshi (prod) account.
- [ ] Edit `config/prod.yaml`: set `mode: quote`, `safety.prod_limits_configured: true`
  (only after the Phase-0 cap review), keep `filters.allowed_leg_series_prefixes`
  **non-empty** (e.g. `[KXWC, KXMLB]`), and your reviewed `risk:` caps.
- [ ] Set the **prod** bot + supervisor credentials in env.
- [ ] Launch: `combomaker run --env prod --mode quote --confirm-live`.
  The **static prod guard** requires `--confirm-live` + `prod_limits_configured` +
  the non-empty whitelist, and the **runtime preflight** requires all 5 gates green
  (limits, whitelist, supervisor heartbeat, external kill reachable, book
  reconciled) — else it **refuses to quote (exit 3)**. If it refuses, read the red
  gate; do not force past it.

## Phase 4 — Validation under fire (the first real fills)

- [ ] Keep size **tiny**. Watch the first real fills + settlements: does the
  reconcile hold to the cent? do conventions match reality?
- [ ] **KILL immediately on any `HALT_RECONCILIATION_MISMATCH`** — that is a real
  predicted-vs-ledger bug, not noise.
- [ ] Do **NOT** scale on a good day or two. The caps limit the bleed; they don't
  make the book profitable.

## Phase 5 — Only then, the edge (the real graduation)

- [ ] Accumulate **pooled, multi-week, game-clustered** settlement data.
- [ ] Re-derive the **markup** AND the caps from that data — never one window.
      The markup is what makes the book +EV; it is currently unset/unvalidated.
- [ ] Enable inventory skew / widen-vs-decline **only after** the pooled
      shadow-markout study says they help. Scale bankroll only on this evidence.

---

## What stays OFF at go-live (by design, not a gap)
- **Inventory skew / widen** (`pricing.skew.enabled` / `pricing.widen.enabled`
  False) — enable only after the markout study.
- **Schedule-feed pregame precision** (`ScheduleCache` empty) — no verified feed
  yet; the conservative 4.5h estimate is the safe fallback.
- **In-play quoting** (`filters.allow_inplay_legs` absent) — pregame-only.

## NEXT STEPS
- **Owner: operator** — Phase 0→4 in order; treat the first live as validation.
- **Owner: eng** — a standing (not just pre-trade) portfolio-ES halt would close
  the "already-over-tail book doesn't self-halt" gap; the schedule feed unblocks
  pregame precision.
- **Owner: measurement** — the pooled-multi-week markup study (Phase 5) is the
  actual profitability gate.
