# MLB go-live: adaptive enforce bricked quoting → shadow (WC caps) fallback (2026-07-23)

Live-money session. The bot is **LIVE and quoting on prod (MLB)**. The correlation-
adaptive caps in `enforce` mode declined 100% of RFQs; switched to `shadow` (proven
World Cup static caps enforce) and it is now market-making with real fills.

## Timeline

1. **Launched** `combomaker run --env prod --mode quote --config config/prod-live-wc.local.yaml --confirm-live` (creds load from `./.env` via `cli.py:116 load_dotenv()`, NOT process env — my first check was the wrong source of truth). Auth OK, reconciled, `prod_preflight_green`.
2. **enforce = 100% decline.** 1849 + 229 risk-audits, **0 quotes**, all `skip_game_loss_cap`, sizer `gross_cc: 0`.
3. **Root cause (verified).** The per-game loss cap = 1% = **$23.85** (`limits.py:828`) is checked against each combo's **comonotone worst-case** game loss (`worst_case_loss_by_game_cc`), which for a multi-leg MLB combo is ~$80+ — far above premium (~$20, which is what the *per-combo* cap checks). The sizer can't fit any size under $23.85 → declines everything. **This is exactly adversarial-review finding H2**, which I overrode with theory instead of validating it could quote.
4. **Bankroll ruled out as a bug.** `balance.py:110` parses `balance_dollars` correctly → bankroll = **$2384.77** (not a unit error; my scratch script's $23.85 was my mistake).
5. **Fix: `adaptive_caps_mode: enforce → shadow`.** WC static caps enforce (per_combo 5%=$119 / game 50%=$1192 / slate 65%), bounded by daily 6%=$143 / drawdown 10%=$238 / KILL 12%=$286 halts; the adaptive brain still LOGS its derived caps.
6. **Validated on the EXCHANGE** (not just log): **187 live open quotes** with real bids (e.g. `no_bid_dollars 0.8230` × 53 contracts). Then **live fills** began: NO 48.6¢×10, NO 62.2¢×7.6, NO 69.4¢×31 (KXMVESPORTSMULTIGAMEEXTENDED combos). Balance $2384.77 → $2379.75, portfolio_value $5.02, equity intact.

## Why enforce couldn't quote — the mechanism

| Cap | Frac | $ @ $2384 | Checks against |
|-----|------|-----------|----------------|
| per_combo | 1% | $23.85 | candidate premium-at-risk (~$20) — would pass |
| **game** | 1% | **$23.85** | **comonotone worst-case per game (~$80+) — FAILS** |

The two caps use **different loss measures**, but the bootstrap pins them equal. A
game cap floor `max(f_slate/expected_games, per_combo)` (added today) does not fix
it because the floor is still 1%. The tight bootstrap is **self-defeating**: no
fills → can't measure σ₁/ρ → never leaves the provisional regime.

## Checkpoints (git)

| Tag | Commit | State |
|-----|--------|-------|
| **`checkpoint-mlb-pre-risk-engine`** | **`2cb3422`** | **AFTER MLB props (OUTS/RBI/SB) + all correlation gaps closed (zero-gaps, mlb rho 319 keys); BEFORE any risk-engine work.** The clean rollback target if the risk-engine changes need reverting. suite ~2613 green. |
| (first risk-engine commit) | `86526a3` | correlation-adaptive cap family (brain, pillars 1-2) — the risk engine STARTS here |
| working tree | `86526a3` + 13 uncommitted | today's risk-engine wiring + spec reconciliation + shadow fallback (armed yaml is gitignored, not committed) |

**To roll back to pre-risk-engine:** `git checkout checkpoint-mlb-pre-risk-engine`
(then restart the bot; note the armed `config/prod-live-wc.local.yaml` is gitignored
and unaffected — set `adaptive_caps_mode: off` there for pure pre-engine behaviour).

## Live status (as of this report)

- **Quoting:** yes — 187+ resting quotes on prod, real bids.
- **Fills:** 3+ accepts (small NO combos), equity intact.
- **Mode:** shadow (WC caps enforce; adaptive brain logs).
- **Downside bound:** daily $143 / KILL $286 halts.
- **Monitor:** persistent, alerting on fills / halts / errors.

## Follow-ups (NOT fixed tonight — flagged)

1. **400 `invalid_parameters` on quote-send** — `rfq_worker_failed`, ~5 of 200+ sends (`rest.py:230` POST /communications/quotes). Non-fatal (worker continues), but some quotes are malformed and rejected. Investigate which combo shape triggers it.
2. **`expected_games` over-counts** — `_count_slate_games` counts all open KXMLBGAME markets (35 = ~3 days), not one night (~10-15). Game-date signal is **`expected_expiration_time`, NOT `close_time`** (close_time is days after the game). Fix the slate-window filter.
3. **Position tracking check** — `get_positions(count_filter=position)` returned 0 while `portfolio_value = $5.02`. Likely a combo/MVE listing nuance; verify the bot's exposure book recorded the fill (state-awareness rule).
4. **Re-size the enforce bootstrap from REAL data** — before ever re-enabling enforce, measure the actual per-game comonotone worst-case distribution of MLB combos and set per_combo/game/slate so it quotes AND stays safe. Do NOT re-enable the 1% bootstrap.
5. **`delete_quote_failed` churn** — many 404s cancelling already-expired quotes; noisy, non-blocking; watch for rate-limit pressure.

## The lesson (recorded)

I built a fully-tested (2670/0), mathematically-safe adaptive cap system and never
ran the one check that mattered: *does a real MLB combo fit under these caps and
quote?* A cap that blocks 100% of trades passes every safety test and is useless.
The review flagged this (H2); I argued it away. **A risk cap must be validated
against real trade sizes — "would it actually quote" — before going live, not
after.** This is now the standing check before any cap change.

## NEXT STEPS

- **Owner: bot (monitoring)** — watch fills / halts / errors on the live shadow run; report P&L as fills settle.
- **Owner: bot (fast-follow)** — (a) diagnose the 400 invalid_parameters; (b) fix `_count_slate_games` slate-window; (c) confirm fill/position recording.
- **Owner: bot (before re-enabling enforce)** — measure real MLB worst-case distribution → size the adaptive bootstrap from data → VALIDATE it quotes → then flip enforce.
- **Decision owed: operator** — keep running shadow (WC caps) tonight to gather fills, or adjust.
