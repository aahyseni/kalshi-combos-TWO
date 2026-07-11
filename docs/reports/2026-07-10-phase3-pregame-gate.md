# Phase 3 — pregame-only quote gate: SHIPPED (active by default)

**Date:** 2026-07-10 · **Suite:** 1173 passed / 0 failed (baseline 1133 + 40 new)
· mypy strict clean on all changed files · ruff clean.

**Operator directive (verbatim intent):** "I do not currently want to quote any
combos where a leg is currently in play — that goes for ALL sports. Keep it OFF
for now, but code it in a way so it can be turned on later."

## What shipped

```
                       rfq_created
                            │
              ┌─────────────▼──────────────┐
              │  RfqFilter.evaluate()      │
              │  … existing gates …        │
              │  ┌───────────────────────┐ │
              │  │ PregameGate (NEW)     │ │   per leg, fail-closed chain:
              │  │ rfq/pregame.py        │ │   (a) KXMLB embedded ET start
              │  │ any started → SKIP_   │ │   (b) min(close,exp_exp) − offset
              │  │   INPLAY_LEG          │ │   (c) neither → UNKNOWN
              │  │ any UNKNOWN → SKIP_   │ │       → decline
              │  │   START_TIME_UNKNOWN  │ │
              │  └───────────────────────┘ │
              └─────────────┬──────────────┘
                            │ pass
                     price → risk → CreateQuote
                            │
                     quote_accepted
                            │
              ┌─────────────▼──────────────┐
              │ last look (STRADDLE RE-    │  same PregameGate re-run:
              │ CHECK): any_leg_started →  │  a leg that went in-play
              │ DECLINE_INPLAY_LEG;        │  between quote and accept
              │ unknown → DECLINE_START_   │  is a deliberate lapse.
              │ TIME_UNKNOWN               │
              └────────────────────────────┘
```

| item | description | status |
|------|-------------|--------|
| Gate semantics | any leg with `now >= start` ⇒ decline whole RFQ; boundary start==now is IN-PLAY; ALL sports | SHIPPED |
| Config flag | `FiltersConfig.allow_inplay_legs: bool = False` (gate ACTIVE by default); flip to true re-enables in-play quoting with NO code change; motion detector + `min_time_to_close_s` stay active regardless | SHIPPED |
| Source (a) | `rfq/pregame.py: embedded_start_time` — KXMLB* game-code `YYMMMDDHHMM` token parsed as **US/Eastern** (zoneinfo, tzdata dep added); only series in the verified allowlist (`KXMLB`) may use this path | SHIPPED, API-VERIFIED |
| Source (b) | estimate = `min(close_time, expected_expiration_time)` − `pregame_start_offset_hours` (default **4.5h**, per-prefix overrides `{"KXMLB": 4.0}`) | SHIPPED |
| Source (c) | no usable source ⇒ UNKNOWN ⇒ `skip_start_time_unknown` (defense #2, never a default) | SHIPPED |
| Straddle safety | pregame gate RE-CHECKED at last look via two new `LastLookInputs` fields; sits in the severity ladder right after the motion-detector `DECLINE_IN_PLAY` | SHIPPED (no gap) |
| risk/inplay.py | market-motion detector UNTOUCHED (zero diff); coexistence pinned by test | UNCHANGED |
| Property test | gated RFQ (started / boundary / unknown / missing-meta) literally cannot reach `create_quote` — asserted at the QuoteSender seam with the gate's reason confirmed as cause; baseline non-vacuous | SHIPPED |

## MLB embedded-time VERIFICATION EVIDENCE (hard rule 5)

Tickers pulled from the READ-ONLY prod tape (rfqs near rowid 15,964,157, seen_at
2026-07-10T23:4xZ); metadata fetched LIVE via public
`GET https://external-api.kalshi.com/trade-api/v2/markets/{ticker}` (no auth).

**18/18 markets** across `KXMLBGAME/HIT/KS/TB/RFI/TOTAL/SPREAD` and ET/CT/PT
venues, day + night games: `expected_expiration_time` = (embedded token read as
**America/New_York**) + **exactly 3.00h**, spread 0.00h. Competing hypotheses
refuted: *venue-local* scatters the gap 0.00–3.00h by venue timezone;
*UTC* gives a constant 7h but implies impossible game facts (Sat day games at
9–10am local; SF night game at 3:15pm local). Sanity: tokens read as ET are
exactly typical MLB local starts (ET venues 1840–1915, CT 1940–2015, PT
2140–2215, day games 1410/1605).

Sample rows (gapET = exp_exp − token-as-ET):

| ticker | venue | token | exp_exp (UTC) | gapET |
|---|---|---|---|---|
| KXMLBGAME-26JUL101845NYYWSH-NYY | DC (ET) | 1845 | 07-11T01:45 | 3.00 |
| KXMLBGAME-26JUL102015ATLSTL-ATL | St. Louis (CT) | 2015 | 07-11T03:15 | 3.00 |
| KXMLBGAME-26JUL102215COLSF-COL | SF (PT) | 2215 | 07-11T05:15 | 3.00 |
| KXMLBGAME-26JUL111410ATHCWS-ATH | CHI day (CT) | 1410 | 07-11T21:10 | 3.00 |
| KXMLBKS-26JUL091235ATLPIT-ATLBELDER55-2 | prop, day | 1235 | 07-09T19:35 | 3.00 |
| KXMLBTOTAL-26JUL092145COLSF-11 | prop, UTC-midnight cross | 2145 | 07-10T04:45 | 3.00 |

Also learned: MLB `close_time` is game+3 days (= `expiration_time`), NOT a game
anchor — the old close-time proximity gate never fired for MLB; the new gate
closes that hole. `expected_expiration_time` = scheduled start + 3h exactly
(Kalshi computes it FROM the ET start).

## Soccer estimate params — DEVIATION from the sketch, with evidence

The sketch said "default consistent with the harnesses' 2.5h". **Measured live
(same API, real World Cup markets, kickoff bracketed by 1H-market settle
times), `expected_expiration_time` lands 2.95–3.95h AFTER kickoff** depending
on series (scheduled end rounded up to the hour + settlement buffer; e.g.
FRA-MAR Jul 9 kickoff ≈20:03Z: KXWCGAME/ADVANCE/CORNERS exp 23:00Z = +2.95h,
KXWCTOTAL/SPREAD/1HTOTAL exp 00:00Z = +3.95h). A 2.5h offset would therefore
say "not started" for up to ~1.5h of live play — fine for the backtest's
purpose (drop-borderline-data), NOT fine for a live gate. Shipped:

- `pregame_start_offset_hours: 4.5` (default, all unverified families) —
  conservative side: too-large only costs late-pregame quoting; too-small
  quotes in-play.
- `pregame_start_offset_hours_by_prefix: {"KXMLB": 4.0}` (harness-validated;
  fallback only — the embedded ET start normally wins for KXMLB*).
- Anchor = **earliest** of close_time / expected_expiration_time (soccer
  active-market close_time can be event-level far-future, MLB's is +3d; min
  is conservative by construction).
- Also observed: active soccer `close_time` (KXWC1H ARGSUI) = Jul 26 for a
  Jul 11 game — close_time alone is NOT a usable start anchor in either
  direction; the min() handles it.

## Reason codes added

`skip_inplay_leg`, `skip_start_time_unknown` (quote-time filter);
`decline_inplay_leg`, `decline_start_time_unknown` (last-look straddle
re-check).

## Files changed

- `src/combomaker/rfq/pregame.py` — NEW (embedded-ET parse + PregameGate)
- `src/combomaker/rfq/filters.py` — gate wired into evaluate(); `pregame_status()` seam for last look
- `src/combomaker/rfq/lifecycle.py` — straddle re-check feeds LastLookInputs
- `src/combomaker/risk/lastlook.py` — 2 inputs + 2 ladder checks (after DECLINE_IN_PLAY)
- `src/combomaker/core/reasons.py` — 4 codes
- `src/combomaker/ops/config.py` — 3 FiltersConfig fields (documented)
- `pyproject.toml` / `uv.lock` — tzdata (Windows zoneinfo data)
- tests: `tests/test_pregame_gate.py` NEW (38); `test_lastlook.py` (+2 cases, ladder + hypothesis extended); `test_filters.py` harness default meta close 2h→6h; `test_lifecycle.py` Rig accepts a FiltersConfig
- `config/demo.yaml` / `config/prod.yaml` — operator-facing comment for the flag

Pre-existing (NOT mine, NOT touched): repo-wide mypy has 2 errors in
`src/combomaker/pricing/ising_amm.py` (numpy Any-return) — present at HEAD
64de97c before this work.

## NEXT STEPS

- **Owner (operator):** none required to stay safe — gate ships ACTIVE. To
  re-enable in-play quoting later: `filters.allow_inplay_legs: true` in the
  env YAML (no code change). Decide if/when per-series offsets should be
  tightened below 4.5h (needs per-series exp−kickoff measurement like the WC
  one above).
- **Next per resume plan:** Phase 4 capstone re-backtest (per-print + full new
  config), then 14 demo fill e2e → 15 weekly sweep cadence → 16 MLB blind test
  → E decisions.
- **Standing:** recorder through Jul 11; WC backtest after Jul 11 settlements;
  weekly P&L sweep + calibration ledger.
