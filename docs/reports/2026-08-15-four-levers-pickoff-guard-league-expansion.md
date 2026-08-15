# 2026-08-15 — Four levers SHIPPED + soccer pickoff CONFIRMED & GUARDED + league expansion (operator: "Do those 4… look over soccer combos… wire more leagues")

Everything ordered this morning is live as of the **12:25 ET restart**
(commits `e5f2793` + `958961f`). One confirmed pickoff defect (root-caused
to the centicent), one surgical guard, one false-start (the known boot race
— now mechanism-fixed), one adjudicated pre-ship RED shipped through with
precedent + a live-RFQ quote proof.

## The four levers (all live)

| # | lever | implementation | proof |
|---|---|---|---|
| 1 | **Continuous equity** (operator: "equity should be read continuously") | `risk_bankroll_cc` = cash + haircut×PV on EVERY poll (`balance.py`) — the SOD min() that ratcheted DOWN on relights is gone (it pinned $4,181.81 vs ~$5k equity and fired a FALSE hard-trip halt 21:57 ET 8/14). SOD stays for the give-back halts. | First post-restart snapshot: backstop $3,432 = 0.70 × **$4,903**, kill line $588 = 0.12 × $4,903 — walls read live equity, **+17% capacity** on every wall vs the ratcheted anchor. |
| 2 | **Ticket-size restoration to the top of your 1–2% band** | `structure_loss_frac` 0.01→**0.02** (max ~$98/combo market at ~$4.9k), `per_combo_loss_frac` 0.05→**0.02** (the belt beneath it). | Validation set: 94 overnight fills, 0 reneges, 0 cap breaches; fill RATE held while size was the collapsed variable ($13.99 vs 8/13's $22.75 risk/fill). |
| 3 | **Mains markup 1¢→2¢** (soccer + MLB base; ladders unchanged) | yaml `markup_cc` 100→200 both sports. | 8/14 verified research (mains 1¢ = 2-2.9% of fair vs book 21.6¢/$1; ≥0.75¢/ct left on wins) + overnight capture +0.25¢/ct ABOVE tier with clean markouts. Noted interaction: the skew rebate can hand back up to ~0.9¢ — watch capture, not sticker. Club-soccer caveat below. |
| 4 | **Cash-gate sender** (the 400-storm fix) | `CashGateSender` decorator: after an exchange `insufficient_balance`, creates refuse LOCALLY until a NEW balance-poll reading (one probe per reading — derived cadence, no timer); delete/confirm NEVER gated. `CashGatedError` is local (never feeds the 429 breaker). | Kills the 181–225k/day discovery-by-400 storm (7.2/s peak). 6 unit pins. |

## ⚠ SOCCER PICKOFF — CONFIRMED, root-caused, guarded

**Your read was right end-to-end, and worse than you thought.** The 8
same-game **YES-tie + NO-over-2.5** fills since the club wire (ORLCIN ×3,
ALAGET ×2, NSHMIA ×2, SEVRVC — $263.05 at risk / $43.95 premium, settling
tonight) were taken by exactly **two sharp taker ids** who only ever hit the
quote-states where our model had silently degraded:

- Normally the shape prices on **Dixon-Coles scorelines** (correct: 3,194
  of 3,208 tie×total quotes; those took ZERO fills).
- On draw-rich matches the DC inversion residual crosses its 0.005 bar
  (`dixon_coles.py:641-644`) → **silent fallback to the copula**, whose
  `soccer:moneyline|total = +0.28` is TEAM-calibrated and **wrong-signed
  for the draw outcome** (truth ≈ −0.45) → fair lands **below
  independence** (correlation backwards). NSHMIA: our 9.3¢ vs the 22-maker
  field's 14.0¢ — your "10x vs 6x" verbatim.
- The ALAGET natural experiment: identical legs priced 30.4¢ structurally
  on 8/13; a 0.35¢ mid move flipped the residual over the bar → 20.5¢
  fallback; the taker accepted ONLY the 2 flipped quotes of 1,390.
- Prober receipts: all 8 fills +2.7 to +6.7¢ richer than the best of 12–22
  makers; every healthy fill class sits +0.0 to +1.0¢. The bot booked its
  LARGEST "expected edges" on exactly these — apparent edge was model error.

**GUARD SHIPPED (live now):** a same-game tie×total combo whose DC fit
rejected now **DECLINES** (`skip_structural_fallback_tie_total`) instead of
copula-pricing. Blast radius: the 14-in-3,208 fallback states that produced
all 8 pickoff fills and zero healthy ones. Live-RFQ probe: 13/14 club RFQs
QUOTED, exactly 1 guarded. **Full repair (post-freeze recipe):** tie-oriented
rho split (`moneyline|total:tie ≈ −0.42`, mirroring the measured 1H value) +
small sgp routing branch; CHALLENGE-band semantics on the residual reject
(price-at-best-fit + widen, `fit_challenge.py` has the band with zero
callers); wire `record_structural_fit` (0 rows ever — the audit trail that
would have caught this day one). Secondary leak logged: cross-game
same-game-pair stacks (BTTS×over per game) price ~2¢/pair cheap on the
copula — 4 fills 8/15 morning, bounded, next-build item.

## League expansion (census-driven, live)

Demand census (REST, open RFQs): **Liga MX** (596 + 697 in MLS cross-combos),
**EFL Championship** (478+), **Club Friendlies (5,676!)**, UCL, CHNSL — plus
NFL 2,059 and UFC 4,760 for the record. Wired the competitive leagues:
**LIGAMX + EFLCHAMPIONSHIP + UCL + EPL** (4 families each; EPL entries are
ready-state for when Kalshi lists the season). **CLUBF/CHNSL/ENGCS are
classified + markup-mapped but NOT allowlisted** — friendlies carry
rotation noise the competitive-club DC fit never saw; **operator decision
owed** (biggest demand pool on the board if you want it). Found + fixed a
classifier trap: "CHAMPION**SH**IP" contains the second-half period token —
league names can no longer read as period markets. The pickoff guard makes
the expansion safe: the wrong-sign fallback now declines in every league.

Club-mains note (from the investigation): our mains FAIR is clean (field
clears 0.1–0.9¢ ABOVE it) — we lose club mains because the WC-calibrated
+4¢ longshot tier over-prices the fair<15¢ bucket vs a field clearing at
fair+<1¢. Do NOT tighten while the ev_ledger holds the 8 poisoned fills;
after tonight's settles + the fix, club longshot mains at ~1¢ is the
share play. (The ordered 2¢ mains raise stands — it's the ≥35¢ band.)

## Ship gates + the two incidents

- Suite **3,708/0**, ruff clean, mypy clean on touched files, vitals fast
  **8/8 GREEN**, markup SHA pin healed at commit.
- **Pre-ship 0/1 RED (V6 confirm-window)** — the KNOWN adjudicated class
  (8/6): confirm MC no longer fits 3s at the measured 335-row "open" book —
  which the ledger defect inflates ~2× (195 stale phantom opens). None of
  today's changes touch that path; the designed degradation (deterministic
  caps resolve confirms in-window, 3.96ms) is green; shipped through per
  the 8/13 precedent with this documentation. **The real fix is the ledger
  stale-row repair + store rotation — now priority-1 on the 9/1 list.**
- **Boot race bit again** (12:06 start died `database is locked`, 25 min
  down): mechanism FIXED — `busy_timeout` now set BEFORE the WAL pragma in
  `Store.open` (`958961f`). Clean 12:25 boot through the same window.

## First-minutes verification (12:25 boot)

Preflight green; **493 quotes in the first 2 min**; walls on live equity
($4,903); p_ruin 0.0; 70 positions; day realized +$452.86 carried; guard
fires 0 (correct — rare state); zero halts. Persistent monitor armed
(halts, reneges, cash-gate armings, executions).

## NEXT STEPS

- **Operator decisions owed:** (1) Club Friendlies — allowlist the 5,676-RFQ
  demand pool or hold (model-risk trade-off stated above)? (2) the tie-rho
  full repair is engine-freeze work — ship it this weekend or ledger to 9/1
  (the guard holds either way)?
- **Me (tonight):** watch tonight's La Liga/MLS slate — the 8 pickoff
  positions settle (expect roughly −$17 to −$28 model-truth on them);
  confirm the guard blanks the two sharp takers; evening readout with
  fills/capture at the 2¢ mains + $98 cap.
- **Me (this weekend, freeze-compatible):** gap-to-best-rival extraction;
  direction-net shadow ARM value; ledger stale-row repair recipe (now
  gating V6); ev_ledger re-baseline excluding the 8 poisoned fills.
