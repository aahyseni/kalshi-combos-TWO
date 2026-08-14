# 2026-08-14 — Arming-day fill collapse: forensics (operator: "we've filled basically nothing — look further, never assume")

Operator was right. Full-day dissection: 5 read-only measurement agents over the
9.35 GB live log + the store (mode=ro) + the risk code, 3 adversarial verifiers,
1 synthesis; key mechanisms confirmed by direct code read. Nothing edited,
nothing restarted — this is the assessment + decision menu.

## Verdict table

| claim | verdict | truth |
|---|---|---|
| "We've filled basically nothing" | **TRUE** | 4 fills / $52.76 premium since the 09:33 arming restart (vs 58 / $2,286.75 overnight pre-restart; 8/13: 192 / ~$4,369). 8h43m fill drought 06:06→14:49 ET. |
| "The bot hasn't quoted almost all day" | **TRUE** | 1,307 quotes all day post-restart, ZERO for the first 4h52m (first quote 14:25:53 ET). Overnight un-armed bot: 17,840 at ~109/min sustained. ~93% throughput drop. |
| "We still have headroom for soccer, esports" | **TRUE** | 8,923 soccer + 10,380 esports RFQs arrived post-restart, every hour. Soccer got 13 store-visible quotes, esports 0. Refused by two DEFECTS + intended walls (below), not by lack of flow. |
| "We're still negative today" | **FALSE on exchange truth, directionally right** | 8/14 realized +$128.88 — but ALL of it is overnight settles; everything graded post-restart is net **−$140.04**. (Also: 8/13 final was **−$215.34** exchange-truth, worse than the −$183.52 I reported from the log tracker.) |
| "Bets >1-2% of balance are bleeding us" | **TRUE + already enforced** | Today's two big red settles: $141/$143 tickets (~3.2% of bankroll each) — both opened PRE-restart. The armed 1% structure cap ($44.79/combo market) refuses that class since 09:33; biggest post-arm fill $22.76. |

## The causal chain (all ET)

```
09:33  restart, levers armed. Book boots PINNED: det $3,144.68 vs 0.70B
       backstop $3,135.71 (headroom −$8.97), p_ruin 0.1017 > 0.05 budget,
       P(KILL-night) Wilson-upper 0.446 > 0.02 budget FROM MINUTE ONE
       → the "marginal" regime was permanent, never transient.
09:34  entity wall starts: skip_entity_loss_cap ~2,600/min ALL DAY
       (1,154,750 refusals = the single biggest wall).
12:41  settle frees det (+$61.90 headroom) — still ZERO quotes for 1h44m
       (entity/structure/directional walls hold).
14:25  first quote. skip_portfolio_cvar appears same minute (the armed
       marginal tail gate) → 163,261 refusals through 17:34.
15:04  book RE-PINS (det $3,136.76) → 16:00 hour: 0 quotes, 54k cvar skips.
17:01  settle wave (−$163.78 red) frees $548 → third burst (636 quotes).
       Of 10 accepts all day, 6 DECLINED at last-look:
       kill_marginal_raises_p_kill — 3 with POSITIVE EV. We reneged.
```

**Over-determination (the asterisk that matters):** the UN-armed pre-restart
bot had ALREADY quoted zero for its final 2h41m (06:07–08:47) and the fill
drought began 06:06 — 3.5h BEFORE arming — behind the OLD walls
(entity 88k / directional 49k / utilization 32k in its last 49 min). At this
book saturation the old wall set bricks on its own; arming changed WHICH
walls and removed the recovery. Fills collapsed purely downstream of quote
volume — per-quote fill rate was UNCHANGED (0.33% overnight vs 0.26% today);
0.33% × 1,307 predicts ~4.3 fills. Exactly 4 happened.

## The three defects (vs two working-as-intended walls)

| # | mechanism | class | evidence |
|---|---|---|---|
| D1 | **Directional cap iterates ALL book games — no candidate-game scoping, no no-worsen early-out** (`risk/limits.py:1699-1712`) | **Constitution violation** (8/1 sunk-book: a book-carried MLB direction refuses a pure MLS parlay) | Store decision 111700015, 10:03:45 ET: pure 7-leg next-day MLS parlay refused because "game 26AUG141420STLCHC [MLB] mutex-aware directional 18271024cc > 17915902cc". 7,213 soccer + 1,249 esports refusals today. The adjacent check's own comment (:1719-1722) names this "the 8/1 freeze shape". |
| D2 | **Marginal KILL/ruin EV-credit asymmetry**: EV credited at `ev × p_accept_lower` (CP-lower ~2-3e-4 from the seeded tape) vs **100%** of the conditional tail charge (`limits.py:426-441`, per its ratified 8/1 text) | Mis-ratified formula — a validate-caps-can-quote failure (safe but useless) | Store rows 111867377/111867380 (14:38 ET): refusals at **174,000–292,000× over** the threshold. 163,261 cvar refusals 14:25–17:34. Only dES99=0 flow (fresh-game diversifiers, `eviction_value.py:400-408`) can pass — which is how the club-soccer quotes got out. |
| D3 | **Entity accumulator folds concurrent in-flight CANDIDATES into prior_cc** (`entity_admission.py:246-269`) + warm keys can never certify (:497-503) | Candidate-storm self-loading (refusals ≠ committed exposure) | Soccer's max entity prior all day was $21.57 — arithmetically cannot trip the $134.39 wall — yet 11,853 soccer refusals attributed to entity, 100% over_size_ceiling on batch attribution. (LOL warm keys are REAL sunk book though: $258.61 open LOL cost incl. one $154.45 position.) |
| — | Confirm-site kill gate declines any accept that raises p_kill by even one MC path (`sim/book_risk.py:3218-3235`) | Same D2 family, confirm flavor | 6/10 accepts declined (`kill_marginal_raises_p_kill`), 3 with positive EV ($0.36–$0.38). **We reneged on 60% of accepted flow.** |
| W1 | Structure cap 1% = $44.79/combo market (`limits.py:1641-1676`) | **Working as the operator ordered** | All 4 post-arm fills ≤$22.76; the 8/13 whale shapes refuse; today's $141-143 losers were pre-arm inventory. KEEP. |
| W2 | `skip_game_too_far` 48h pregame window (6,067 soccer refusals of Aug 16-17 weekend games) | Working as configured | Weekend La Liga/MLS opens quote from Friday evening onward. |

## P&L truth (exchange-reconstructed, ledger corrected at group level)

| day | exchange truth | what I'd reported | note |
|---|---|---|---|
| 8/13 final | **−$215.34** | −$183.52 (log tracker) | ledger double-count class (−$25.30, −$13.88 rows) |
| 8/14 thru ~17:04 | **+$128.88** | +$95.69 (log) | log understates by exactly the −$33.23 double-count the boot seed inherited; split: pre-boot +$268.92 / post-boot **−$140.04** |

What went red 17:01–17:04: two ~240-contract MLB tickets (−$143.04,
−$141.25) + one KS group (−$61.74) vs +$152.64 of offsets — again the
pre-cap whale class. Ledger defects found (report-only): WIN groups strand
siblings "open" forever (195 stale rows / $2,010.92); LOSS groups sometimes
double-book. Genuinely-open at-risk book: $3,404.38 (88 rows; 21 rows/$750.67
dating to 8/02 deserve an exchange liveness check).

## The 1-2% directive — status

**Enforced since 09:33 by the structure cap at 1% ($44.79).** No other gate
sits in the band (per-combo fallback is 5% = $224; entity 3% = $134/key; the
$500 notional prong is a hand-set absolute that never binds first). Holes:
(a) same logical structure under a DIFFERENT exchange ticker gets a fresh
$44.79 bucket — no single ticket can exceed 1%, but same-structure exposure
can stack to ~$134 before the entity wall binds (mechanism fix = key on
structure hash, post-freeze); (b) optional belt-and-braces: per_combo 5%→2%
(yaml:571) so the fallback ceiling is $89.59 if structure is ever disarmed.

## Decision menu (nothing edited; recommendation marked)

| option | what | effect | needs |
|---|---|---|---|
| **A (recommended)** | Repair D2: symmetric p_accept discounting (charge `des99 × p_accept_lower` too) or floor p_accept at the MEASURED overnight fill rate (0.33%) instead of sparse-bucket CP-lower; same repair at the confirm site (noise-tolerant comparison instead of one-MC-path strictness) | Converts the bulk of 163k/afternoon cvar refusals into live candidates (~100× afternoon quote volume) while still refusing tail-concentrators; stops the renege class | Operator ratification (the armed formula matches its ratified 8/1 text — the text was wrong, not the code) |
| **A′ (fallback, 2 yaml lines)** | Disarm `kill_gate_marginal` + confirm-site flavor only (keep level triggers, keep structure 1%, keep seed, keep backstop) until A is built | Restores ~overnight quoting behavior minus whales; the 2% tail budget goes back to telemetry | Operator ruling; one restart |
| **B** | Repair D1: candidate-game scoping / sunk-baseline early-out for the directional cap | Unblocks 7,213+1,249 soccer/esports refusals; pure defect repair | None under the 8/1 ruling (it's a constitution violation); code change = freeze carve-out ack |
| **C** | Repair D3: exclude in-flight candidates from entity prior_cc + allow warm-key certification for net-diversifying small adds | Removes most of the ~1.15M/day artifact refusals | Partial ratification (7/28 tier table is operator-ratified) |
| **D** | per_combo 5%→2% belt-and-braces | Hard $89.59 fallback ceiling | One yaml line, same restart |

Also surfaced: recorder ~3h behind (3.1 GB un-checkpointed WAL; all 30
post-restart quoted rfq_ids not yet visible in rfqs — recheck after catch-up);
allowlist drift (KXCSGOGAME- listed; live prefixes KXCS2GAME/KXLOLGAME);
~62% of esports flow dies pre-risk (arrives in-play / thin books) — esports
diversification has a structural ceiling regardless of gates.

## Process failure (mine, on record)

This is the second validate-caps-can-quote incident (first: 7/23 MLB 1%
bootstrap). The pre-arm gates proved the levers REFUSE whales but never
proved the armed config would still QUOTE AND CONFIRM normal flow against
the seeded book state — which booted pinned AND over both tail budgets from
minute one, making the marginal regime permanent. The standing rule exists
precisely for this; the arming checklist must add a quote-production
counterfactual (replay N hours of tape through the armed config, require
non-zero sends+confirms) before any future arm.

## NEXT STEPS

- **Operator (decisions owed):** pick from the menu — A (repair the marginal
  formula) vs A′ (disarm marginal flavors until repaired); whether B (the
  directional-scoping defect repair) proceeds now under the constitution
  carve-out or waits for 8/31; C and D dispositions.
- **Me on ruling:** implement + gate (suite, vitals fast+pre-ship, scratch
  proof, and the NEW quote-production counterfactual) + one restart + live
  watch with fill/quote milestones.
- **Me regardless:** recheck the recorder WAL backlog tonight; exchange
  /portfolio cross-check of the 88 open rows ($3,404.38, esp. the 21 from
  8/02); direction-net shadow read still owed after tonight's slate.
