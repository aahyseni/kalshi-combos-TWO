# 2026-08-26 — Bundesliga + Saudi Pro League wired, markup reset, WIP shipped, bot RELIT

Operator directives (8/26 morning): (1) "all of the soccer leagues wired in" —
UCL/EPL/Serie A/Bundesliga/EFL Championship/Ligue 1/MLS/Liga MX/UECL/Saudi
Premier League, all four club families; (2) markup — final form after a
mid-morning refinement: "for ML parlays make the markup 0.6c **across all
sports**, for the rest make it **1-3c tiered** like before" (ruled ALL
sports on a direct question — racing/mixed included); (3) gate the 8/19
WIP + relight. Operator also confirmed THEY stopped the bot on 8/20 (closes
the outage forensics question — no unknown killer). La Liga was not on the
list but stays wired (flagged, no objection).

## Shipped (commits `d5b121a`, `e866fec`; restart 08:26 ET)

| item | detail | state |
|------|--------|-------|
| 8/19 WIP committed | gross-notional once-counted fix (unconditional; ratified 8/12) + persistence 2nd-conn checkpoint fix + thin-auction lever (DARK, yaml key absent). Gates: WIP tests 94/94, suite 3,755/1*, vitals fast 8/8 GREEN | `d5b121a`, pushed |
| Bundesliga wire | `KXBUNDESLIGA{GAME,TOTAL,SPREAD,BTTS}-` — code pre-wired 8/16; allowlist+offsets+48h in yaml. 4,867 open RFQs (8th-largest prefix exchange-wide) | LIVE |
| Saudi Pro League wire | `KXSAUDIPL{GAME,TOTAL,SPREAD,BTTS}-` — Kalshi's ONLY Saudi base (all other tokens 404). New `SAUDIPL` sport keyword + markup prefix. 1,300 open RFQs | LIVE, `e866fec` |
| ML-parlay markup | ml_parlay_cc 30 → **60** (0.6¢) AND the MLB/soccer-only sport restriction REMOVED (markup.py) — cross-game ML-only parlays ride the razor in EVERY active sport bucket incl. esports, racing, and cross-sport 'mixed' (which already requires every leg known+active; dark/unknown legs still zero out). Shape guards unchanged (prop leg / same-game / unparseable ⇒ full tiers) | LIVE (code `1934f8f`-class + yaml) |
| General ladders → 1–3¢ | soccer + MLB mains 200 → **100** (reverts the 8/15 raise); soccer <15¢ tier 4¢→**3¢** (ladder 1/2/3¢); MLB <10¢ tier 4¢→**3¢** (ladder 1/2/2.5/3¢); racing 3/4/5¢ → **flat 3¢**; cross-sport mixed 4/5/6¢ → **flat 3¢** (operator's explicit all-sports ruling); esports already flat 3¢ | LIVE (yaml) |
| Yaml backup | `config/prod-live-wc.local.yaml.fallback-20260826` | taken pre-edit |
| WAL checkpoint | quiesced TRUNCATE, 0.5s, WAL removed (was 38 MB — the persistence fix had held it down from 8/20) | done |

*suite sole failure both runs = the markup-module-frozen sha tripwire, which
passes on commit (its purpose: no markup change without operator sign-off —
the 8/26 markup directives ARE the sign-off). Pre-ship V6 RED = the standing
stale-ledger class (380 ledger opens vs 0 real positions today), 5th
ship-through adjudication; ledger repair stays the 9/1 P1.

## Recon facts pinned (authed fleet, live API 8/26)

- Both leagues: settlement rules VERBATIM-identical to the 12-series club pin
  (90'+stoppage regulation-only, BTTS own-goals, 48h cancel/reschedule scalar
  ⇒ farmable=False inherits). Anchors GAME kickoff+3h / others +4h (measured
  same-fixture deltas). Full scrapes in `docs/calibration/club_soccer_rules_pin.md`
  §8/26; assumption audit NOTES.md BS1–BS7.
- Traps test-pinned: `KXBUNDESLIGA2*` = Bundesliga 2 (different league, shared
  prefix — the exact-family-dash allowlist style is the gate); `KXBBLGAME` =
  Bundesliga basketball (UNKNOWN); Bundesliga futures stay UNKNOWN.
- Saudi collection membership tape-proven (live RFQ carries a KXSAUDIPLGAME
  leg in KXMVECROSSCATEGORY-SHARD1-R); reverse probe lags ~1wk pre-fixture.
- **Account truth**: $4,827.44 all cash (shard1 $4,537.65 / shard0 $289.79 —
  the SHARD1 transfer executed while the bot was down; **that decision is
  CLOSED**), zero positions, zero open quotes. The 8/20 marked equity
  $6,677.71 settled down to $4,827.44 (78 positions returned $1,570.56 vs
  $3,420.83 mark). All-time realized: +$2,827.44 on the $2,000 deposit.
- `balance_breakdown` now has FOUR shards (2 & 3 empty, new); the whole-book
  merge is dynamic (quote_app.py:1035-46) — handled. `mvec_eligibility_scan`
  still assumes 2 collections (8/12 flag, still open).

## First-window verification (boot 08:26 ET, window ~14 min)

- **655 sends/min** in the trailing 3 min (benchmark 300–460 — throughput
  ABOVE benchmark; 2,945 sends total in the first 10 min).
- **0 insufficient_balance** (shard1 funded), **0 halts**, preflight green.
- Walls on the fresh equity: entity ceiling $96.55 = exactly 2% × $4,827.44.
- New leagues flowing: Saudi legs reaching entity admission (full
  classify→price→risk path); Bundesliga metadata warming.
- Boot transient (5 min): 461 metadata 429s + 97 starvation warnings while
  the 6-day-stale metadata cache refilled a fresh slate — both stopped by
  08:32 ET, none since; known cold-cache pattern, fail-closed as designed.
- Live-loader proof (final scheme): ml_parlay 60cc riding on esports ML
  parlays ('esports', 60), cross-sport MLB×LOL ('mixed', 60), Saudi×
  Bundesliga soccer ('soccer', 60), racing winners ('racing', 60); soccer
  100cc + (1500→300, 3500→200); MLB 100cc + (1000→300, 2000→300, 2500→250,
  3500→200); esports/racing/mixed flat 300; non-ML shapes keep tiers
  (soccer longshot @8¢ → 300); thin_auction_bonus_cc 0 (dark); 8 new
  prefixes + offsets + 48h caps resolved. NOTE: the first boot (08:26 ET)
  ran ~40 min on the interim 1-2¢/MLB-soccer-razor reading before the
  operator's refinement landed; second restart loaded the final scheme.

## NEXT STEPS

- **Me (watch items)**: first Bundesliga/Saudi quote_sent + first fills
  (metadata now warm; Saudi matchday 8/28, Bundesliga 8/29-30); first
  settlements reconcile per league + offset re-verify (BS3); razor capture at
  0.6¢ vs the 0.3¢ era (pre-registered ≥2-week pooled read, first ~9/1);
  favorite-band pooled check ~9/1 (on truncated ~8-day data — say so).
- **Operator, still open**: 1% vs 2% cap verdict; friendlies allowlist
  (CLUBF 3.3k+ RFQs); thin-auction +1¢ lever (coded dark, one yaml key);
  boot persistence re-raise (two 6-day outages in a month — the stack still
  survives neither reboot nor sleep-stop without a manual START_BOT).
- **9/1 list unchanged**: store rotation P0 (201.7 GB), ledger stale-row
  repair P1 (dissolves the V6 RED), shard-aware cash gate, telemetry-anchor
  split, composition-aware KILL budget, mvec scan baseline, tie-rho repair.
