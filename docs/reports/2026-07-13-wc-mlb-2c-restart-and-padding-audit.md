# WC + MLB live @ 2¢ + per-family padding audit (2026-07-13)

Operator directive: "include all World Cup and all MLB, at a 2¢ edge … remember some
combos had extra padding (HRR baseball, corners soccer), add those, then restart."

## What changed (config only — no code)

`config/prod-live-wc.local.yaml` (gitignored armed config), suite unchanged at 1720/0:

| Knob | Before | After |
|---|---|---|
| `pricing.markup.soccer.markup_cc` | 300 (3¢) | **200 (2¢)** |
| `pricing.markup.mlb.enabled` / `markup_cc` | false / 0 | **true / 200 (2¢)** |
| `filters.allowed_leg_series_prefixes` | `[KXWC]` | **`[KXWC, KXMLB]`** |

Config parses + `assert_safe_to_run()` passes; relaunched 23:46 UTC (one bot, verified
by PID tree). **Caveat on 2¢:** the re-grade's min robustly-+EV markup was 2.2¢
*same-game*; 2¢ is at/just-below that floor, and multi-game + MLB are unvalidated
(see `2026-07-13-samegame-vs-multigame-edge-split.md` + the re-grade's MLB confound).
Going live at 2¢ is a **gather-live-data** decision — treat early P&L on multi-game /
MLB as data, not proof. Downside bounded by sell-only + caps + the fair/width below.

## Per-family padding audit — it's ALREADY in the fair, not a markup knob

3-agent discovery workflow (`padding-discovery`, wf_5fb70120-2c0) mapped every flagged
family against the code. **Finding: the "extra padding" for HRR (baseball) and corners
(soccer) is FAIR CALIBRATION, already live in `config.py`** — not a separate markup:

- **corners**: `corners|corners_team` ρ**0.62** (measured 8,981 matches, `config.py:472`),
  corners|total, corners|player_goal, oriented resolvers — all `yes_in_code`.
- **HRR**: HRR|total ρ0.40, HRR|spread ρ0.40 (tightest MLB pair, ±0.05 band), HRR|KS,
  TB⟹HRR exact containment — all `yes_in_code` (`config.py:531,419,492`).
- **`btts|first_half_total`** ρ0.55 (the other RFQ-flagged pair) — `config.py:389`.
- **Explicit extra WIDTH** already active: **+2.5¢ on 8+-leg all-NO MLB prop baskets**
  (DO-6, `config.py:1638`, default 250cc, not overridden) — the HRR-overbid defense.

Mechanism: `margin = max(defensive_half_width, markup_cc)` (`quote.py:166`). So
uncertain families already get ≥ their uncertainty width regardless of markup, and
dropping markup 3¢→2¢ removes **none** of the per-family protection.

**Decision: did NOT add a per-family markup knob.** No such mechanism exists today
(markup is per-sport only), and adding one would (a) double-charge families whose fair
is already calibrated → **less** competitive → *worse* fills (the operator's active
concern), and (b) be un-validated hand-tuning against the measure-first doctrine. The
clean 3-edit seam to add per-family markup padding IS documented (config field →
MarkupPolicy method → engine call, reuse `classify_leg`) for a future phase **if**
settlement-graded per-family evidence ever justifies it.

## Live state (as of ~00:03 UTC 2026-07-14)

- **One bot** (PID tree verified: bash→main-launcher→main-worker→supervisor×2). The
  "2× quote процессы" in tasklist is the Windows console-script launcher+worker, not
  two bots. No double-quoting.
- WS **stable** (receive_timeout fix, `2776013`): ~1 disconnect / 10–13 min, 0
  WS-caused halts. Quoting steadily (quote_sent ~386 and climbing).
- **0 fills** — expected (maker + 2¢ on the no-edge multi-game pond; the "6/20 contracts"
  the operator saw are RFQ *requested sizes*, 86% of RFQs are target-cost-mode showing
  0 in the contracts column — NOT fills).
- ⚠️ **Mild HTTP 429** (metadata fetches) from the added MLB volume — watching.

## Follow-on: false `data_stale` halt on a quiet pregame market (fixed)

~00:07 UTC the bot hard-halted on `halt_data_stale` ("feed rx-age 5.92s > 5.00s
sustained 35s"). **Not** the WS (stable) and **not** 429s (0 in-window). Root cause:
on a quiet pregame market the leg books don't tick every 5s, so feed rx-age sat at
5–6s all session (flapping holds at 5.04/5.07/5.56/6.27s); a 5.92s gap sustained
through a WS reconnect crossed the breaker's 30s grace → FATAL halt = a false
"dead feed" on a merely quiet one. Same "stop-and-go" class the operator dislikes.

**Fix (config only, in the local armed yaml):** decouple *decline* from *halt* and
size both for quiet pregame:
- `breakers.max_rx_age_s` 5 → **45s** (HALT only on a genuinely dead feed; 45s ≫ the
  ~6s quiet cadence, and the WS proves liveness via 10s server pings).
- `filters.max_feed_age_s` 5 → **12s** (DECLINE to POST on books >12s; pregame fair
  moves slowly so ≤12s is fresh). 12 < 45 ⇒ we are already declining well before the
  breaker could ever hold — between 12–45s the bot DECLINES and STAYS UP, no halt.

Corrected a backwards convention comment in `config.py` (the real invariant is
freshness-gate ≤ breaker, not ≥). Relaunched 00:15 UTC; 0 data_stale, 0 halts on boot.
Deeper option if halts ever recur on an ultra-quiet market: make `data_stale`
non-fatal entirely (decline + auto-recover, never hard-halt) — a breaker code change,
deferred pending operator sign-off.

## Shadow recorder — DOWN

`data/combomaker-prod.sqlite3` last written **2026-07-12 19:28** (>1 day), no
`--mode observe` process. We are **not** accumulating weeks 2–4 backtest data. Decision
pending (operator): stopgap in-session restart (may worsen 429s alongside the live bot)
vs the durable server (the real fix for multi-week accumulation).

## NEXT STEPS

1. **Owner: operator (decision owed).** Shadow recorder: stopgap now vs durable server.
2. **Owner: bot.** Keep supervising WC+MLB @2¢; watch 429 rate (if it starves quoting,
   consider a metadata-fetch backoff or dropping the observe recorder if co-running).
3. **Owner: bot (recommended, pending operator).** Same-game-only gate — the +EV pond is
   same-game (multi-game −1.2pp). Prototype-in-test → port → parity (hard rule 8).
4. **Owner: bot (deferred).** Live-engine MLB re-grade to de-confound the MLB verdict.
5. **Owner: measurement.** Pooled multi-week is the markup gate — never a P&L refit.
