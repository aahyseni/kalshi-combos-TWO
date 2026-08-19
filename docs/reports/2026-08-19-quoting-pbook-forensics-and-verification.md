# 2026-08-19 — Quoting/p_book forensics (6 finders + 3 adversarial verifiers): quoting is NOT down, the STORE is; p_book calc CONFIRMED correct (my 8/16 projection retracted); shard-1 cash critical; ~$2k of unbooked wins found in stale ledger rows

Operator questions (≈07:40 ET): "another losing day"; "quoting a LOT less than
before"; "how are we not filling the 70% cap"; "if p_book can't reach 0.6 our
risk book is wrong or the calc is wrong." Nine read-only agents (six finders,
three adversarial verifiers), receipts throughout. All times ET.

## WRONG / FIXED / OPEN

| # | claim | verdict | truth |
|---|---|---|---|
| 1 | "Another losing day" (8/19) | WRONG (so far) | TD 8/19 realized **$0.00 / 0 groups** (nothing reconciled since 00:47 ET); equity flat since 05:30 ($4,154.12 vs $4,158.92) |
| 2 | My 8/18 "final −$156.51 + overnight −$121.96" | **RETRACTED — boundary artifact** | position_ledger timestamps are UTC (verified two ways); my 04:00 filters cut at midnight ET. True TD 8/18 (04:00→04:00 ET) = **−$296.94 / 132 groups**, verified to the cent |
| 3 | "Quoting a LOT less than before" | WRONG on the wire | TD 8/18 sent **437,608** quotes = within 0.8% of the all-time max (8/9: 441,081), above era mean ~391k; today 04:00–07:22 mid-pack (−9% vs era mean). Log = truth |
| 4 | — but something IS collapsing | CONFIRMED: **the store** | WAL checkpoint failing all boot (3,120 failures, `database table is locked`); last successful fold 8/18 22:44 ET; WAL 5.63GB growing 0.64GB/h; writer 2h35m behind; ~75–80% of tape rows silently dropped today (capture 20–25%). Every store-reading view shows a phantom ~50% quoting collapse |
| 5 | "Risk book wrong or p_book calc wrong" | **NEITHER — audited + independently re-derived** | Book model = exchange truth (in-memory ExposureBook, exchange-first rehydrate lifecycle.py:1802/quote_app.py:3413; settled legs fact-resolved 0/1 lifecycle.py:9718; stale ledger rows provably cannot enter). p_book = P(book P&L>0), 20k MC, seed 7 |
| 6 | My 8/16 projection "p_book 0.60–0.70 at ≥60% favorite share, +1.5¢" | **RETRACTED — the math was wrong** | Verifier reproduced exactly: at +1.5¢ retained on ~15-game slates the ceiling is **~0.55–0.578 regardless of band share** (0.60 needs k≥58 indep. games, 0.62 k≥85, 0.70 k≥249). Composition target itself already MET (58.7% favorite-band premium vs ≥60% goal) |
| 7 | 8/18 overnight "2 gross-notional reneges" | CORRECTED | Boot-log census: exactly **1** (missed by $11.47 = 0.086%, EV foregone $0.60). My monitor over-counted |
| 8 | Stale-open ledger rows | **NEW: they hide ~+$2,038** | 272 rows/$3,482.36 cost, all games ≤8/18; sibling-group analysis: 242/244 estimable rows sit in WINNING groups ≈ **+$2,037.60 of future realized P&L** not yet booked (28 rows/$889.09 unknown). Reconciled day-P&L is biased PESSIMISTIC; a future day will print phantom wins |

## Answers with receipts

### Q: quoting less?
No — wire truth (risk_audit `quote_sent`, one per CreateQuote POST):
8/9: 441,081 · 8/10: 431,086 · 8/11: 353,585 · 8/12: 415,025 · 8/13: 314,672 ·
8/18: **437,608** · 8/19 by 07:22: 61,338 (era same-window 54,304–80,045).
Real deltas: peak-hour bursts down ~35% (current boot ceilings ~22k/h vs era
26–35k/h nights — follow-up); this morning's genuine flow ≈70–83% of
yesterday's (thin midweek AM; tonight is 15 MLB incl. 5 day games from 12:35
ET + 15 MLS + UCL playoff). The perceived collapse is #4 above: anyone
reading the store sees 40%→20% capture. NOTE: repo-root live_*.log are stale
7/15 decoys; real logs = D:\kalshi-combos-TWO-data\ per CURRENT_LOG.txt.

### Q: why not 70% deployed?
Book-level walls are NOT the ceiling: det $1,688 = 58% of the $2,908
backstop; slate/directional/KILL/ruin/cash refused ~0 today; utilization
(gross-notional) refusals 3,094 = 100% the ×4.0–5.6 double-count artifact
(same-moment true gross $2,303 vs $12,499 limit). The real limiters:
(1) per-candidate caps from the operator's 1% ruling — per_combo $83.32 /
structure $41.66 / entity $124.99 / flat $500 size cap refuse **74.7% of
risk-stage RFQs** (188,448 of 252,271 today), overwhelmingly giant-payout
lottery shapes (typical $10-premium 10-leg parlays with $180–280 det loss);
per_combo was binding-first on 90,374 but NEVER the sole tripped wall
(0/26,403) — candidates trip 3+ caps at once, so no single-cap tweak frees
much; (2) auction win share ~0.9% in the band where auctions actually clear;
(3) all caps auto-scale off equity, down $5.1k→$4.15k. The 8/12–13 "3k
days": mean ticket $24–28/max $150–200 (the whale era the 1% ruling ended)
+ 5.8–7.1h settlement recycling. **The 70% goal and the 1-vs-2% verdict are
the same decision.** 8/18 under 1%: 153 combos, median $10.56, max $42.95,
$2,399 premium, zero fills above the 2% line.

### Q: p_book — where 0.6 actually comes from
Verifier's independent MC/exact-binomial (vrfy_pbook_ceiling_mc.py)
reproduced every number: per $1 favorite premium (p=0.70, m=+1.5¢) EV
+$0.0214 / sd $0.645; 15 games ⇒ P(book>0) 0.567; current book + favorites
peaks 0.5787; **+3.5¢ retained on ~$1.2k favorites ⇒ 0.620**; **85 indep.
games at +1.5¢ ⇒ 0.626**; cheapest realistic blend: **+2.5¢ retained ×
k≈30 (a full MLB+MLS day) ⇒ 0.62**. Correlation does NOT lower p_book
(slightly raises it; it fattens the loss TAIL instead — ES/det problem).
Current book EV +2.31¢/$1 ≈ +1.5–1.6¢ retained — internally coherent with
p_book 0.53–0.56 and implied σ≈$257, k_eff≈17.8.
Measured opening for the margin route: **≥65¢-band auctions mostly EXPIRE
unfilled (88% no print within 30min; 35–65¢: 80%)** — competition is thin
exactly where the margin lever works; razor band is the opposite (81%
clear, winner at our-fair +0.11¢, 30+ makers).

### Auction win rate (context)
Accepts/10k sends: 8/12: 5.23 · 8/13: 7.25 · 8/18: 4.41 · today: 4.78.
This morning vs 8/18 same window: sends −9%, accepts −53% (Poisson-real) —
razor conversion cooled (12→4) with NO field re-tightening (clearing level
vs our fair unchanged +0.11¢; our won margin +0.1¢ steady; lost-gap
0.6–0.8¢ = width stacking on the 0.3¢ tier). Razor verdict stays on the
pre-registered ≥2-week clock; no refit.

### Slate-autopsy bias note (favorite-band check)
The morning's 57%-vs-74% favorite-band read used reconciled groups only;
wholly-unreconciled groups skew WINS (242/244) ⇒ the measured win rate is
biased LOW. One-slate clustering conclusion stands; magnitude softer than
reported. The ~9/1 pre-registered check must include late-reconciled groups.

## Store failure — mechanism (V2, PARTIAL: onset + lock cause corrected)
Single shared aiosqlite connection (persistence.py:252) serves writer
batches, checkpoints, AND maintenance/settlement reads; `database table is
locked` = SQLITE_LOCKED = **the connection blocks ITSELF** (cursors held
open across awaits on a 191GB DB). External readers are NOT the cause
(overnight natural experiment: zero successes at minimum load). No
in-process recovery exists. Fill/settlement writes share this connection
(synchronous; working, latency risk grows). Disk fine (1.57TB free).
**Remediation ranked:** (1) quiesced restart — clean stop, close-time WAL
fold, verify `-wal`≈0, restart; ~3–12 min; pre-noon lull acceptable,
waiting until tonight folds ~2× the WAL; (2) durable fix (small, isolated
to persistence.py — needs freeze blessing): checkpoint on a DEDICATED
second connection + fetchall-materialize maintenance cursors + log
`_dropped_writes` (today's 75% tape loss was silent by design).

## SHARD 1 CASH — CRITICAL
$228.45 at 08:30 ET (was $907.62 at 05:30) vs **94.8% of today's flow**
(63.6% 8/17 → 90.1% 8/18 → 94.8% — migration nearly complete). 5 MLB day
games start 12:35 ET. Transfer script re-derived to 0.90 flow share
(~$1,990 move); assistant execution BLOCKED by permissions — **operator
must run `TRANSFER_SHARD1.bat`** (or `!` the command). Zero
insufficient-balance 400s so far today; that changes when the slate opens.

## NEXT STEPS
- **Operator, ranked:** (1) run TRANSFER_SHARD1.bat before ~12:00 ET;
  (2) bless the pre-noon quiesced-checkpoint restart (~3–12 min downtime);
  (3) bless the two freeze-exception fixes for daylight ship: gross-notional
  double-count (ratified 8/12; 3,094 artifact refusals today + 1 renege
  last boot) and the persistence.py checkpoint-connection fix; (4) 1-vs-2%
  verdict with the 70%-deployment framing above; (5) optional margin lever:
  +1¢ retained on favorite-band mains (88% of those auctions currently
  expire unfilled — thin competition; moves p_book toward 0.6 per V1 math).
- **Me:** ledger stale-row backfill sweep design (272 rows hiding +$2k;
  also biases every day-P&L report); peak-hour throughput ceiling follow-up
  (~22k/h vs era 35k/h); include late-reconciled groups in the 9/1
  pre-registered checks; keep the anomaly watcher on log-truth only (never
  store counters).
- **Standing:** no refit on any of today's P&L; razor verdict waits its
  2-week window; engine freeze holds except items blessed above.
