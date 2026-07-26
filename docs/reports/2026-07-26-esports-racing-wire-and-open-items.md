# 2026-07-26 — Esports + racing wire (esports ENABLED, racing held) + the running open-items ledger

## What went live

**Esports winners are ENABLED** in the armed config (allowlist
`KXMLB, KXLOLGAME-, KXCS2GAME-, KXCSGOGAME-`). Racing is wired in code but
NOT admitted — see §3.

Ticker grammar is TAPE- and API-VERIFIED, never guessed (operator rule
[[feedback_kalshi_source_of_truth]]): a 400k-RFQ scan of the observe-mode
store plus `GET /series?category=Sports` (3,005 series) and per-market
`GET /markets/{ticker}`.

| family | verified winner series | evidence |
|---|---|---|
| League of Legends | `KXLOLGAME-26JUL180700KCT1-T1` | 3,332 tape hits |
| Counter-Strike | `KXCS2GAME`, `KXCSGOGAME` ("Counter-Strike 2 Game") | series catalog; operator sees it live; 0 combo flow in the sampled tape |
| F1 | `KXF1RACE-BELGP26-ANT` | 326 tape hits |
| NASCAR | `KXNASCARRACE-WINW26-DEHA` | 24 tape hits |

Markup ladders (operator-set price floors, NOT settlement-calibrated — no
esports/racing corpus exists yet; the self-selection argument carries them:
a taker only fills at ≥ fair+markup, so a rich markup declines competitive
flow rather than buying it):

| combo | fair <10¢ | 10–20¢ | mains |
|---|---|---|---|
| esports | 5¢ | 4¢ | 3¢ |
| racing | 5¢ | 4¢ | 3¢ |
| **cross-sport ("mixed")** | **6¢** | **5¢** | **4¢** |

## §1 Two traps found and closed while wiring

1. **A "RACE" substring rule would have been a money hole.** The exchange
   also lists `KXF1RACETOP5 / TOP10 / PODIUM / TOPX / SPRINT` and
   `KXNASCARTOP3/5/10/20` — "finish top N" markets that are NOT one-of-N
   winners and ARE correlated with the winner. Loose matching would have
   priced a top-5 leg as a win leg. Motorsport winners now classify from an
   EXACT-series map; every neighbour stays `UNKNOWN` ⇒ no-quote, pinned by
   test. Map-winner series (`KXCS2MAP`, `KXLOLMAP`) are likewise UNKNOWN:
   a map and its own match are correlated and that ρ is unmeasured.
2. **Cross-sport combos were quoting at ZERO markup.** `sport_of` tags any
   multi-sport combo `'other'` ⇒ markup 0 — a systematic underprice on
   exactly the niche flow we want to be rich on (the same species as the
   2026-07-16 KXMENWORLDCUP zero-markup incident). Fixed with a dedicated
   `mixed` config; fail-safe unchanged (any unknown/dark-sport leg ⇒ 0).

**Allowlist hygiene:** the trailing hyphen in `"KXCS2GAME-"` is
load-bearing — prefixes match with `startswith`, so it admits
`KXCS2GAME-<match>-<team>` while excluding the plural `KXCS2GAMES-` series.

## §2 Correlation stance (operator: "these can't have any correlation")

Correct as implemented, with one caveat worth stating: legs from DIFFERENT
events carry no same-game pair entry, so they price as an independent
product. Legs within ONE event (two drivers in a race, two teams in a match)
are mutually exclusive — and that comes from the event's OWN
`mutually_exclusive` metadata, never from an assumption here. The
UNMEASURED pair is match×map, which is why map series stay unquotable.

## §3 Why racing is held back (the one real blocker)

F1/NASCAR tickers embed **no start token**, and the pregame gate's estimate
tier is `earliest(close, expiry) − offset`. Live-verified:
`KXF1RACE-BELGP26-ANT` has `expected_expiration 19:00Z` against a ~13:00Z
race start — the default 4.5h offset lands at 14:30Z, i.e. **1.5h into the
race**. Enabling racing without fixing this admits in-play quoting, the one
thing that gate exists to prevent.

**Fix options (next session):** a per-prefix offset ≥8h (NASCAR looked ~9h
from its single sample) via `pregame_start_offset_hours_by_prefix`, or a
small explicit `ScheduleCache` table of race start times (precise tier), plus
`max_pregame_hours_by_prefix` so week-out uninformed books can't be picked
off (the 2026-07-14 lesson). Sample size so far is ONE race per series —
verify against 3+ before trusting either number.

## §4 OPEN ITEMS LEDGER (everything noticed, in priority order)

**Correctness / money**
1. **Entity-axis BOUNDS** (top ask): leg-axis PRICING is armed, but there is
   no hard per-player/per-entity wall. Operator 7/25: "I wouldn't prefer
   having our book rely on 1 leg like that" (Hunter Greene 4+ K).
2. **p_night restart-roll is a no-op**: `Store.day_realized_pnl_cc` reads
   `position_ledger`, which NO live code writes (`record_position_open` /
   `record_position_settled` have zero production callers; the settlement
   handler is built without a store). Also settlements occurring while the
   bot is DOWN can never be booked (the poller matches only in-memory
   positions) — needs a startup reconcile pass.
3. **Racing start-time policy** (§3) before racing is enabled.
4. Re-run the two adversarial-review lenses that aborted on a spend limit
   (EV-slot-eviction, v2 doctrine gate).
5. **All-positions p_book invariant + alarm**: p_book covers all committed
   positions incl. in-play, but EXCLUDES `risk_modeled=False` reserved
   holdings — operator wants a rolling whole-book guarantee.

**Throughput / flow**
6. **Dissolve `max_open_quotes: 200`** into measured capacity (sweep-tick
   latency + write-budget headroom). EV eviction is only half the fix;
   ~$3.2M/day of flow died at this cap arrival-order-blind.
7. **In-play adverse-selection read-out** after ≥1 slate of
   `would_quotes_inplay` rows joined to settlements → decides whether the
   pregame-only gate (~$1.15M/evening) ever opens.
8. **Persistence writer backlog** (~40 min behind at peak) — blinds any live
   flow-loss measurement, since skips are DB-only.
9. Map-winner families (`KXCS2MAP`, `KXLOLMAP`) need a match×map ρ before
   they can quote; same for the racing top-N families.

**Ops**
10. The metadata-change breaker caused FOUR halts on 7/25 (3:40p, 6:47p,
    7:32p, 8:37p). Three root causes fixed (terminal-status exemption,
    earliest-horizon, expected_expiration excluded from the fingerprint).
    It remains the most halt-prone component — check it first after any
    restart.
11. Watch out: the venv `python.exe` is a SHIM that spawns an identical
    child, so process listings always show PAIRS. Never diagnose "duplicate
    stacks" from a raw count (this misdiagnosis killed a healthy bot).
12. `tools/ops/*.ps1` must stay pure ASCII (PS 5.1 ANSI parse).
13. 7/25 settlement reconciliation to the cent is still owed.

## NEXT STEPS

- **Now**: restart to pick up esports + the day-rollover fix + the breaker
  fix; watch for `KXLOLGAME`/`KXCS2GAME` quotes and confirm the mixed-tier
  markup appears on any cross-sport combo.
- **Next session**: §4 items 1–3 in order.

---

## §5 ADDENDUM (2026-07-26 morning) — the steer rebates are economically dead

**Measured on 997,581 live skew events (current run):**

| component | rebate (median) | widen (median) | widen (max) |
|---|---|---|---|
| P(book) | **−0.02¢** | +0.01¢ | +0.17¢ |
| leg-family | −0.04¢ | +0.02¢ | +1.42¢ |
| entity | −0.03¢ | +0.03¢ | +0.16¢ |

The classifier WORKS — 2,390,157 `pbook_diversifying` classifications, i.e. it
recognises diverse flow on nearly every quote — and then prices that
recognition at a fiftieth of a cent against a 1–4¢ markup ladder. A taker
cannot see 0.02¢, so **we are not rebating diversity at all**; we identify it
and do nothing. Operator (2026-07-26): "why are we not giving rebates to more
diverse [bets]" — correct diagnosis.

**Root cause (mechanism, not a number):** each component is a product of
sub-1 fractions — `deficit × need × onset^gamma` — and `onset` divides the
concentrated tail by the ENFORCED cap (~$240). At the current book size that
term is a few percent, so three fractions against a ~1.5¢ ceiling leave
hundredths of a cent. It self-scaled to irrelevance. Note the ASYMMETRY: the
widen side reaches 1.42¢ (concentration IS large today) while the rebate side
never can — we effectively armed the penalty without the reward.

**Fix (build #1, this session):** price the rebate against the VALUE it
creates — the candidate's measured ΔP(book) converted into cents through the
same EV frame the gate already computes — so the reward carries the same
units as the markup, still fully derived (no hand-set constant). Shadow the
new magnitudes first; arm only after the cent-distribution is eyeballed.

**CORRECTION (operator, verified):** an earlier draft of this note blamed the
−$49 EV on in-progress games marking the book down. That was ASSUMED, not
checked, and it is WRONG. Verified against the source of truth at 08:51 ET:
every held game starts 13:35 ET or later — **ZERO legs were live**. So a
book of 71 positions, each sold at a positive edge, shows expected P&L of
−$48.67 and wins only 37% of nights BEFORE anything has happened. That is a
design failure, not variance: the positions are the SAME BET REPEATED
($282 short K-overs, four pitchers carrying ~$420), i.e. we have been
QUOTING DIRECTIONAL. Directional quoting → correlated book → low p_book, and
the mechanism meant to prevent it was rebating at 0.02¢. Lesson recorded:
never assume an error's cause — check the source of truth.
