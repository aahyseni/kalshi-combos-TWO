# SportsGameOdds API — external odds source notes (fetched 2026-07-05)

Sources: `sportsgameodds.com/llms-full.txt`, `/docs/info/ai-vibe-coding`,
`/docs`, `/docs/data-types/odds`, `/docs/guides/handling-odds`.
OpenAPI spec exists at `sportsgameodds.com/SportsGameOdds_OpenAPI_Spec.json`
(not yet digested).

## Auth + base

- Base URL: `https://api.sportsgameodds.com/v2`
- Auth: header `x-api-key: <key>` (query `?apiKey=` also works — do not use;
  keys don't belong in URLs/logs)
- Usage endpoint: `GET /account/usage`

## Free tier ("Amateur") — CONFIRMED by user's plan + live probe 2026-07-05

- **2,500 objects/month**, **10 requests/minute**, 10-min update frequency,
  **no historical data** (user-confirmed).
- Leagues (8): NFL, NBA, MLB, NHL, College Football, College Basketball,
  Champions League, MLS — good overlap with Kalshi sports combos.
- Bookmakers (9): FanDuel, DraftKings, BetMGM, Caesars, ESPN BET, Bovada,
  Unibet, PointsBet, William Hill.
- Data on plan: pregame AND live odds, spreads/moneylines/over-unders,
  partials (1st half etc.), player/team props, alt lines, **fairOdds** +
  **bookOdds** consensus, results/live scores/box scores.
- Live-probe facts: `GET /account/usage` works (tier "amateur",
  per-minute max-requests 10 visible in `rateLimits`); events envelope is
  `{"success": true, "data": [...]}`; a real MLB event carries ~1,000–1,300
  odds entries (~16 moneyline entries across periods).
- An "object" ≈ a returned entity — every polled event costs budget
  (~80/day at 2,500/month). Poller floor: 10-minute interval (matches their
  update frequency); ≤1 request per league per cycle keeps us far under
  10 req/min.

## Events endpoint

`GET /events?leagueID=NBA&oddsAvailable=true&limit=...&cursor=...`

Event shape (from docs):

```json
{
  "data": [
    {
      "eventID": "...", "sportID": "...", "leagueID": "NBA",
      "teams": {"home": {"teamID": "..."}, "away": {"teamID": "..."}},
      "status": {"startsAt": "date", "started": false, "ended": false},
      "odds": {
        "<oddID>": {
          "oddID": "points-home-game-ml-home",
          "statID": "points", "statEntityID": "home", "periodID": "game",
          "betTypeID": "ml", "sideID": "home",
          "fairOdds": "-115",   // consensus, juice REMOVED (their devig)
          "bookOdds": "-125",   // consensus across books, WITH juice
          "byBookmaker": {"<bookmakerID>": {"odds": "-110", "available": true}}
        }
      }
    }
  ]
}
```

- `oddID` = `{statID}-{statEntityID}-{periodID}-{betTypeID}-{sideID}`
- Moneyline game-winner: `points-home-game-ml-home` / `points-away-game-ml-away`
- `sideID` ∈ {home, away, over, under}; `betTypeID` ∈ {ml, sp, ou}
- Odds are **American odds as strings** ("-110", "+150")
- Spread/OU lines: `fairSpread`/`bookSpread`, `fairOverUnder`/`bookOverUnder`

## Integration decisions (see `pricing/sources/sportsgameodds.py`)

- We devig `bookOdds` OURSELVES (two-sided pair → configured devig method)
  rather than trusting their `fairOdds` (method opaque). Their `fairOdds` is
  used as a cross-check: |ours − theirs| feeds the belief's uncertainty.
- Poller (budget-aware) → in-memory cache → sync `OddsSource.marginal()`.
  Entries expire (default 15 min) → None → Kalshi-book-only pricing.
- Kalshi ticker → (eventID, oddID) mapping is EXPLICIT (static table);
  unmapped ⇒ None, never guessed (quiet-failure defense #2). Automated
  mapping (team names + start times) is Phase 6 work with live tickers.

## Critical facts (must get right)

- 2,500 objects/month free tier — the poller MUST be budget-gated.
- American odds are strings and can be positive or negative; implied
  probability: -o → o/(o+100), +o → 100/(o+100).
- `fairOdds` = juice-removed consensus; `bookOdds` = juiced consensus.
- No historical on free tier (user's historical pulls likely predate a plan
  change or used a different allowance — verify before relying on it).

## Open questions

- Exact pagination envelope (cursor field name in the response) — code reads
  `nextCursor` defensively; verify on first live pull.
- Rate-limit (429) body/headers; object accounting granularity (is an event
  with 50 odds 1 object or 51?). Check `GET /account/usage` after one pull.
- Whether `oddsAvailable=true` filters to pregame-quotable events only.
- Terms-of-use for trading-bot consumption of the free tier — user should
  confirm their plan allows this use.
