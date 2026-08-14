# 2026-08-14 — Sportsbook parlay markups vs our combo ladder (verified research)

Operator ask: *"research markups professional sports books use for parlays…
to see if we have a decent markup."* Answered by a 3-researcher web fleet +
adversarial number-verifier + synthesis (450k tokens, 145 tool calls); every
load-bearing figure re-derived arithmetically this session and cross-checked
against regulator filings, operator earnings letters, and the Kalshi fee
schedule. Live-YAML ladder re-verified against `config/prod-live-wc.local.yaml`
before comparison (matches: soccer 1/2/4¢, MLB 1/2/2.5/3/4¢, esports flat 3¢,
racing 3/4/5¢, cross-sport 4/5/6¢).

## TL;DR — one line per band

| our band | verdict vs the industry |
|---|---|
| Mains (fair ≥35¢, 1¢) | **~8–11× cheaper than book parlays; below even straight-bet vig.** The Kalshi taker FEE (~1.75¢ at midprice) exceeds our markup here. |
| Mid (15–35¢, 2–2.5¢) | Cheap-to-par: prices like a parlay book charging single-bet juice; less than half the realized book rate. |
| Longshots (<15¢, 3–4¢) | Above posted-book *rates* (% of fair) but **below the observed Kalshi field** (rival medians 3–9¢ above us) and far below realized Kalshi retail longshot losses. |
| Esports (flat 3¢) | Cheap vs books everywhere, but the FLAT shape is inverted vs every industry curve and our own other ladders (rich at mains, thinnest at longshots). |
| Cross-sport (4–6¢) | Our richest tier; par with realized book rate at fair ≥20¢, rich below ~18¢ — reads as model-risk pad + thin competition, not extraction. |

## The benchmarks (verified)

**Units.** Kalshi combos settle binary at $1, so our markup in cents over
model fair IS the sportsbook quantity (parlay price − fair). Book "hold" is
% of stake: markup per $1 of fair = h/(1−h).

**Theoretical −110 compounding** H(N) = 1−(21/22)^N (exact, tier A):

| legs | hold (% of stake) | binary frame: price vs fair | markup % of fair |
|---|---|---|---|
| 2 | 8.9% | 27.44¢ vs 25¢ (+2.44¢) | 9.8% |
| 3 | 13.0% | 14.37¢ vs 12.5¢ (+1.87¢) | 15.0% |
| 4 | 17.0% | 7.53¢ vs 6.25¢ (+1.28¢) | 20.5% |
| 5 | 20.8% | 3.94¢ vs 3.13¢ (+0.82¢) | 26.2% |
| 6 | 24.4% | 2.07¢ vs 1.56¢ (+0.50¢) | 32.2% |

Structural fact: books' absolute *cents* markup SHRINKS with legs while
%-of-fair GROWS — the exact opposite shape most people assume.

**Realized holds** (regulator/operator, division-verified): IL FY2023 parlay
bucket **17.74%** of handle vs straights 4.9%; NJ FY2025 ~**18.7%**; the
parlay/straight multiple is a stable **3.6–3.7×** across states and years.
Realized book-average markup ≈ **21.6¢ per $1 of fair**. SGP estimates run
15–25%+ (no regulator publishes SGP-only hold — all tier-C). DK/FanDuel
blended structural holds 10.5–15.5%; NET of promos both run roughly half.
Operator dispersion is huge (IL FY2023: FanDuel 21.3% vs BetMGM 12.3% —
pricing choice, not necessity).

**Kalshi venue:** taker fee = ceil(0.07·P·(1−P)) charged once on the combo
(3.5% of stake at 50¢, →~7% for longshots). Kalshi retail combo all-in
extraction ≈ **14.6% of stake** (Sportico: $117M lost / $800M staked, of
which only $35M = 4.4% is exchange fees — the rest is maker margin + leg
mispricing). Sub-10¢ combo buyers lose >60% of stake; sub-2¢ combos lose
92¢/$1. An arXiv study finds Kalshi combos price only ~0–3%/leg over the
*product of leg prices* — i.e. the venue's parlay-structure premium is tiny;
the retail loss lives in longshot leg prices and maker margin.

## Ours vs theirs at matched fair

(our % of stake = markup/(fair+markup); "all-in" adds the Kalshi taker fee,
which a book's posted price already internalizes)

| fair | ours (soccer/MLB) | ours % of stake | all-in over fair | book theoretical | book realized-avg (21.6% of fair) | verdict |
|---|---|---|---|---|---|---|
| 50¢ | +1¢ | 2.0% | +2.75¢ (5.5%) | straight vig 4.76% | +10.8¢ | **FAR CHEAP** — under straight-bet vig |
| 35¢ | +1¢ | 2.8% | +2.6¢ | — | +7.6¢ | FAR CHEAP |
| 25¢ | +2/2.5¢ | 7.4–9.3% | +3.4¢ (13.5%) | 2-leg +2.44¢ (9.8%) | +5.4¢ | PAR with theory, under realized |
| 15¢ | +2/3¢ | 12–17% | +2.9–3.9¢ | ~3-leg +1.9¢ (15%) | +3.2¢ | PAR to slightly rich |
| 8¢ | +4¢ | 33–43% | +4.6¢ | 4-leg +1.28¢ (17%) | +1.7¢ | RICH vs books; still under Kalshi field + retail losses |
| 5¢ | +4¢ | 44% | +4.5¢ | 5-leg +0.82¢ (21%) | +1.1¢ | RICH vs books; under the 92¢/$1 realized loss band |

Crossover fairs where our rate meets the realized book-average rate: ~18.5¢
for the 4¢ tiers, ~13.9¢ for 3¢, ~9.3¢ for 2¢, ~4.6¢ for 1¢ — everything
above those fairs, we are cheaper than an average US book's parlay.

## What it means for us

1. **"Do we have a decent markup?" — we are the cheapest parlay product in
   the comparison set at every fair ≥15¢**, cheaper than theoretical
   compounding, realized state holds, SGPs, and (at mains) straight-bet vig.
   Below ~10–15¢ fair we flip above posted-book *rates* while staying below
   the field we actually compete against.
2. **Realized capture confirms it:** our measured edge ≈ +2.5¢ per $1 of
   premium vs the industry's 17.7–18.7¢ gross parlay hold — we capture
   ~1/7th of a book's take. That is what "cheapest maker in a competitive
   auction" should look like, and it means the schedule survives adverse
   selection (charged 2–10% of price, realized ~2.5% of premium).
3. **The industry ceiling is NOT our binding constraint — the best rival RFQ
   quote is.** Book holds bound what retail *will* pay, not what the auction
   *lets us* charge. Internal receipts: we win at fair+1–2¢ while field
   medians sit 3–9¢ above us (7/14 report), and at mains we cleared 0.75¢
   UNDER the winning price on 17/25 (7/16 report) — ≥0.75¢/contract of pure
   surplus left on mains wins.
4. **Headroom read (informational — markup moves are operator-ratified,
   never automatic):** mains 1¢→2¢ would still be under the measured
   clearing median, under straight-bet vig, and ~5× under book parlay rates;
   the mid band has ~+1¢ of similar slack. Longshots/cross-sport show
   nominal room by field medians, but that's the band where we already
   exceed posted-book rates and where our own model risk, not competitors,
   binds. **The decisive missing measurement is per-auction gap-to-BEST-rival
   (not median) from the RFQ tape** — that number sets safe headroom per
   band; winner's-curse selection means realized edge will not scale 1:1
   with markup.
5. **Esports flat-3¢ shape is the one anomaly** — charges 3× our mains rate
   at high fairs while being our thinnest longshot tier; every industry
   curve and our own other ladders slope the other way. (Known context: the
   7/29 flat was an operator price-floor to buy fills/information, 0/196
   accepts — not a calibration.)

## Caveats that survive verification

- Book parlay hold is monopoly posted-odds pricing to captive retail; ours
  is a competitive RFQ auction. "8× cheaper than FanDuel" ≠ 8× headroom.
- Our markup is vs OUR fair; book hold is vs their own price-implied fair.
  The +2.5¢/$1 realized and 59% win rate are ground truth; the ladder is
  intent.
- Realized book holds are GROSS of promos (net ≈ half). State parlay buckets
  blend SGP + cross-game with unpublished leg mixes. Monthly holds swing
  6–24% on outcome noise — only full-year anchors are load-bearing (and per
  the standing rule, no refit on a P&L window on our side either).
- Kalshi-side realized figures (Sportico, 92¢-loss, ~5 combo makers) are
  single secondary sources; our own fills ledger overrides them where they
  differ.
- Verifier killed four defective claims from the raw research (a garbled IL
  transcription, a mislabeled FanDuel "actual hold," a WoO mis-extraction,
  an LSR "~1% Kalshi fee" phrasing) — none load-bearing for the table above.

## NEXT STEPS

- **Operator:** none required — no markup change is proposed. If the mains
  1¢→2¢ question interests you, the prerequisite is the gap-to-best-rival
  extraction below, not more industry data.
- **Me (on request):** extract per-auction gap-to-BEST-rival by fair band
  from the RFQ tape (the one measurement that turns "headroom exists" into a
  number); fold the esports-shape observation into the next pooled esports
  evidence read.
- **Standing:** armed-bot monitoring continues (separate report:
  `2026-08-14-variance-levers-armed.md`).
