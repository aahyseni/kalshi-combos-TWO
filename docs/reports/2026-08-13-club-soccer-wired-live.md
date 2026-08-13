# 2026-08-13 — CLUB SOCCER LIVE: La Liga + MLS (GAME/TOTAL/SPREAD/BTTS) + UECL (all four + ADVANCE)

**Operator direction:** wire the three leagues onto the WC machinery with the
existing correlation numbers, UECL with total/spread/btts/advance included,
restart once working. **DONE — bot relit 09:28 ET on `67579a5`, preflight
green, and club RFQs are QUOTING (three UECL-legged quotes on the wire within
minutes; UECL qualifying plays today, La Liga opens 8/15).**

## What shipped (commit `67579a5`, merged to main, pushed)

| seam | change |
|---|---|
| `pricing/legtypes.py` | `("UECL", SOCCER)` sport keyword ("UCL" is NOT a substring of "UECL" — UECL classified UNKNOWN-sport before); `LALIGAADVANCE` UNKNOWN blocker ahead of `ADVANCE` (it's a season series, not a tie market) |
| `pricing/markup.py` | club prefixes → the `soccer` markup tier (they tagged `other` = ZERO markup — the exact KXMENWORLDCUP live-incident class) |
| `pricing/relationships.py` | **farmable is now SERIES-scoped** (`_farm_certain`, KXWC only, all legs must qualify): club "impossible" combos never farm — the 48h cancel/reschedule rule scalar-settles them at fair value (pin doc), the same money hole as MLB's rain rule |
| `pricing/sgp.py` | **two-legged-tie ADVANCE regime guard**: same-game UECL advance pairs price UNTYPED (flat + widened band) — the measured advance\|* values are single-match (KXWC) regime; a team can lose leg 2 and still advance |
| `marketdata/metadata.py` + `rfq/pregame.py` | `occurrence_datetime` = third fail-closed estimate anchor: Kalshi creates club line rungs ON GAME DAY with creation-relative `expected_expiration` (live-verified `KXUECLTOTAL-26AUG12SCRPAI-7` would have quoted ~1.3h in-play); min() over three anchors keeps it pregame |
| structural / DC | **ZERO changes** — parser handles no-HHMM club codes, line conventions identical to WC (`-6` = over 5.5; `TEAM3` = by >2.5), GROUP format is exactly club 90'-regulation semantics, and `knockout_series` stays `[KXWC]` so UECL ADVANCE **auto-falls to copula** (the single-match DC Advance spec never touches it). Dixon-Coles has NO home-advantage parameter (rates inverted per game from live prices — venue is embedded in the marginals), so the home/away frame question turned out moot for pricing |
| local yaml | allowlist +13 exact-family series (trailing-hyphen style), `decline_two_legged_tie: false` (the blanket predated the wiring; allowlist is now the only UCL/UEL gate — re-pin before ever admitting those), per-league pregame offsets (GAME/ADVANCE 3.0167h, others 4.0167h — measured kickoff+3h/+4h anchors, cross-checked vs independent kickoffs), 48h max-pregame horizons |

**The "WC coefficients" were club-calibrated all along:** dc_ρ = −0.05 was
fitted on 8,980 club games and the soccer pair table on top-5-EU club matches
— they fit these leagues *better* than they fit the WC.

## Proof chain

- Suite **3,643/0** (34 new tests: classification ×13 exemplars, sport ×13,
  blockers, markup map, club farm-gates vs KXWC farm-kept, advance regime
  guard, occurrence-anchor in-play defense) — re-proven in a fresh scratch
  worktree of the PUSHED commit + vitals fast **8/8 GREEN** there (52.7s).
- mypy clean; vitals fast **8/8 GREEN** pre-commit too.
- **Validate-can-quote (live): 14/14 sampled pure-club open RFQs QUOTED**
  through the real engine at live mids with the armed config
  (`tools/backtests/club_soccer_quote_probe.py`) — including target-cost
  sizing (the majority RFQ mode) and the same-game correlation credit
  visibly applied (MLS BTTS×TOTAL fair 54.4¢ vs 43.5¢ naive product).
- **MLB no-drift differential: byte-identical** quotes main-vs-branch on
  fixed-mid MLB combos (`--mlb` mode).
- Post-relight: preflight green, 88 quotes in the first minutes, club RFQs
  passing the firehose (344 UECL leg references), **3 UECL-legged RFQs
  cross-referenced to `quote_sent`**.

## Pre-ship tier: the KNOWN standing RED, recorded not argued away

The pre-ship vitals check (quiet machine, 515s) failed 0/1 — the **inherited
5×-book confirm-window projection**, the SAME standing RED adjudicated on 8/6
("identical at origin/main; live size clears with 62% margin; belongs to the
utilization/P1 queue") that the bot ran with all week. This wiring touches
neither `compute_book_risk` nor the confirm path (differential + suite prove
it); the RED is a property of live BOOK SIZE at a 5× synthetic projection.
It stays on the deferred ledger (frozen to 8/31) — surfaced, not silently
waived.

## Why it took hours (operator asked)

The ticker wiring itself was ~30 minutes. The rest: (1) the repo's own
gates — full suite ~4.5 min/run, vitals fast ~1 min, pre-ship ~9 min, run
multiple times per the hard rules for any pricing-path change; (2) this box's
disk is saturated by the live bot (149.6 GB store), so every gate crawled —
the first pre-ship attempt ran 30+ min against live I/O before being killed
and re-run in the quiet window; (3) three real money traps that "just add the
tickers" would have shipped: **zero markup on every club combo**, **farming
club impossibilities that scalar-settle under the 48h rule**, and **in-play
quoting via game-day-created rungs**. Each was found by the research pass,
not by luck. The store-rotation item on the deferred ledger is what makes
future wires faster.

## Watch items (first club slates)

- UECL ADVANCE first-leg blindness: a FUTURE round's ADVANCE market listed
  before leg 1 would estimate off leg-2's anchor (this round is leg-2-only —
  correct). Evidence n=1 says Kalshi lists ADVANCE after leg 1; verify at the
  play-off round (~8/20) and add `pregame_scheduled_starts` entries if not.
- La Liga offsets are UNVERIFIED against a finalized game until 8/15 —
  re-measure exp−kickoff after the first slate.
- Cross-sport combos (club × MLB) are newly reachable → the `mixed` markup
  bucket under independence; watch composition in the next readout.
- An 11-leg La Liga NO-basket declined `skip_classifier_unknown`
  ("band×neighbour not structural-representable") — fail-closed, correct;
  if that shape carries real flow, it's a coverage item, not a defect.
- First club settlements: reconcile to the cent (defense #3) — first natural
  canary is tonight's UECL slate.

## NEXT STEPS

- **Me (watch):** first-hour post-relight readout (club quote share, decline
  mix, no halts); first UECL settlements reconcile; weekly census cadence.
- **Operator:** none owed — wiring was pre-authorized incl. restart. FYI: the
  worktrees `kct-clubsoccer` + `kct-scratch-clubsoccer` retire at next
  cleanup; `.env`/PEMs were copied into `kct-clubsoccer` (gitignored, local).
- **Deferred ledger unchanged** (engine freeze to 8/31); the standing
  pre-ship RED stays on the P1/utilization queue.
