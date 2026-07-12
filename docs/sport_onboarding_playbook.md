# Sport Onboarding Playbook — from unknown tickers to zero-hole live pricing

**What this is:** the exact, ordered course of action to take ANY new sport
(NBA, NHL, tennis, a new soccer competition, …) from "we don't even know the
ticker shapes" to "every reachable combo priced with measured or exact math,
zero hand-priors, zero silent fallbacks, adversarially judged, backtest-gated,
ready to quote." It is distilled from how soccer/World Cup and MLB were
actually built (2026-07-05 → 2026-07-12). The blow-by-blow evidence, with every
report filename and commit hash, is in
`docs/reports/` (dated) and the timeline reconstruction that seeded this doc.

**How to use it:** work the 8 stages in order. Each stage has an ENTRY bar
(what must be true to start), the WORK, the EXIT GATE (what must be green to
proceed — never skip), the TOOLS that already exist, and the OPERATOR ASKS to
front-load. The **Failure Firewall** (§F) is the most valuable part: every trap
the first two sports hit, with the tripwire that now prevents it — read it
before you start, not after you're bitten.

**The five laws that override everything** (violate these and the rest is
worthless):
1. **Marginals are ALWAYS live from Kalshi books; history calibrates ONLY the
   joint layer.** The era that changed baseball moved the *marginals*
   (P(HR) 0.089→0.107), not the correlation — which is exactly why we never
   bake a marginal into config.
2. **Source of truth: Kalshi facts (tickers, rules, settlement scopes) come
   from the tape/API/pinned fixtures, never memory or docs-as-assumed; ρ values
   come from real historical corpora, never intuition.**
3. **Fail-closed everywhere: UNKNOWN → widen-or-decline, never a convenient
   default.** A gap you can't measure becomes a *principled decline*, never an
   invented number.
4. **No unexplained residuals: every "N of M" names the M−N.** A "clean"
   summary with a non-empty queue is itself a defect.
5. **Never refit on a P&L window.** Model changes come from measurement or
   structural facts only; P&L is a thermometer.

---

## STAGE 0 — Reconnaissance: the ticker & rules universe

**Entry:** the sport has live or recently-settled markets on Kalshi.
**Exit gate:** a written universe map with ZERO "and others" buckets, every
series prefix classified, every settlement scope pinned to a rule source.

**Work:**
- Enumerate every series prefix and line/rung universe **from the prod tape**
  (`data/combomaker-prod.sqlite3`, READ-ONLY `mode=ro`) AND the public API
  (`GET /series`, `GET /markets`). Do NOT infer ticker shapes from another
  sport — pull real examples. (Trap B8.5: `KXWC1HGAME` was assumed; real is
  `KXWC1H` — the wiring was dead on live data.)
- Pin every **settlement scope** to rule text the operator provides: regulation
  vs extra-time vs pens, includes-overtime totals, postponement/rain scalar,
  DNP definitions. These decide which containments are exact and which farms
  are airtight (trap B8.4: MLB's 48h rain rule scalar-settles every family, so
  MLB farms *nothing*).
- Map the **event/game grouping** convention: on Kalshi `event_ticker` is
  per-market-SERIES, so same-game legs arrive under different event_tickers —
  correlation MUST key on the GAME code, not event_ticker (trap B1.1, the L10
  crisis that silently killed all same-game correlation). Verify the real
  event-ticker segment count from the tape (trap B3.3: props are 2-segment
  `SERIES-GAMECODE`, not 3).

**Tools:** `docs/calibration/containment_probe/` shows the tape+API enumeration
pattern; the `classify_containment_frequency.py` counting approach.
**Operator asks:** rules text for this sport (E.1#6); prod recorder running so
tape exists.

---

## STAGE 1 — Classification: every leg typed, every UNKNOWN explicit

**Entry:** Stage 0 universe map.
**Exit gate:** every combo-eligible leg family classified; every non-modeled
family routes to an explicit UNKNOWN blocker; classification verified against
the FULL series universe (not a sample) with zero false positives.

**Work:**
- Build the exact leg-family list (soccer had ~13, MLB exactly 9). Add
  keywords + **UNKNOWN blockers** for families you will NOT price
  (derbies, futures, leaderboard, series-totals). **Keyword order is
  load-bearing** — blockers and longer substrings must precede shorter ones
  (`MLBHRR` before `MLBHR`; trap B8.5: bare `HR`/`KS` collide with 64/67
  unrelated series including `KXANTHROPICRISK`→player_hr).
- Every classifier has an explicit UNKNOWN branch, and UNKNOWN **cannot reach
  CreateQuote at normal width** (quiet-failure defense #2, property-tested).
- Verify classification against all series (MLB: 11,305-series keyword-collision
  scan, zero false positives) and real tape tickers.

**Tools:** `pricing/legtypes.py` (the family classifier + keyword table);
`pricing/relationships.py` (UNKNOWN/IMPOSSIBLE branches).
**Gate:** adversarial classification judge (MLB: 21 CONFIRMED / 0 REFUTED).

---

## STAGE 2 — Marginals & the pricing grammar

**Entry:** classified legs.
**Exit gate:** every leg's marginal sources live from the book with freshness
gates; frame/orientation/rung conventions declared for this sport.

**Work:**
- Marginals come from the live Kalshi leg book (microprice; spread/thin →
  wider uncertainty; missing/stale → no-quote). Devig applies ONLY to external
  odds adapters, never to Kalshi binaries (they're vig-free by construction).
- Declare the **pricing grammar** for the sport (see §C): orientation
  (`:same`/`:opp`/`:tie` — both measured, never negated), rungs
  (`:rN` = ticker line, exact keys only, no interpolation), frames (which
  cells use which population frame — centralize the frame in ONE place per the
  L1 lesson, trap B2.3).

**Tools:** `pricing/legs.py`, `pricing/normalize.py`, the `shape_in_leg_frame`
convention-owner pattern.

---

## STAGE 3 — The pair-matrix enumeration (do this BEFORE measuring)

**Entry:** grammar declared.
**Exit gate:** the COMPLETE pair matrix — every leg-family × leg-family cell —
with each cell marked, plus the real tape FLOW through each (combos/prints), so
you measure in priority order and can prove nothing is missed.

**Work:**
- Enumerate every pair cell by EXECUTING `legtypes.pair_key` on the pair (never
  hand-write keys — trap B3.1: `rfi|player_ks` sorts to `player_ks|rfi` and a
  hand-copied key silently never matches). Mark each cell:
  MEASURED / EXACT-arithmetic / STRUCTURAL / HAND-PRIOR / FALLBACK.
- Count real tape flow per cell — the biggest MLB flow was in cells no one
  would have listed from intuition; the WC 3–11¢ tail was four unmeasured
  pairs carrying ~190k prints. **Don't trust "top-N" — enumerate the full
  matrix** (trap B3.6: `moneyline|player_tb` and all of spread×props were
  simply missing from every prior list).
- Build the **gap ledger**: one row per hand-prior/fallback cell with its flow,
  the >2¢ families it causes, the fix class (measure / exact-algebra /
  recalibrate), and the data source. ZERO remainder — every hand-prior/fallback
  key in config appears, marked reachable or unreachable-on-tape.

**Tools:** the `gap_ledger.json` builder pattern (`build_gap_ledger.py`,
classification-only over the tape caches).
**This stage is what "no holes" is measured against** — the ledger is the
definition of done for the sport, and the loop runs until it's empty.

---

## STAGE 4 — Measure the joint (the correlations & pairs — the heart of it)

**Entry:** gap ledger.
**Exit gate:** every reachable cell is measured-to-standard, exact-arithmetic,
or a structural output; failing cells are explicit fail-closed declines with
their numbers; the wire list is emitted.

### 4a. Exact beats measured — check arithmetic FIRST
Before measuring anything, find the airtight implications: containments (A⟹B),
windows, mutual exclusions. A third of MLB's cells turned out to be certainties
(hit⟹HRR, TB≥N⟹hit), not statistics — verified ==1.0 on the FULL population
(1,033,852 batter-games), zero counterexamples, before wiring. Exact cells are
*better* than measured and remove uncertainty from quote width.

### 4b. Pick the corpus per pair type
| pair type | corpus | how |
|---|---|---|
| team×team (ml/total/spread/btts) | league match history (football-data, nflverse, Retrosheet) | implied ρ through OUR copula |
| player×team / player×player | player-level data (Understat, Retrosheet batter-games, StatsBomb events) | conditional frequencies |
| **no public corpus joins the two stats** | **Kalshi's own co-settlements** (matches where both markets settled on the tape) | exchange ground truth — the strongest possible source; how corners×scorer was measured |
| derived (e.g. advance = f(result)) | measured base pair + a documented bridge (win⇒advance; draw⇒pens ≈0.5) | bridge uncertainty ADDED to the band; validate the bridge on any direct data |

### 4c. The solver (the ONE copula, inverted)
Invert **the same Gaussian copula the pricer runs** (`implied_rho()` bisection)
so the measured number is a drop-in `pair_rho` that means exactly what the
engine consumes. Tetrachoric estimator, BVN-validated to ~1e-16. (Gold standard
for team pairs: conditional-MLE on each game's own devigged closing lines, then
OOS-gated.)

### 4d. The judge standard (every candidate value)
- **Game/match-CLUSTER bootstrap** CI95 (resample by game, not by row —
  outcomes inside a game co-move; naive CIs are falsely tight).
- **Era/season stability**: split the corpus (e.g. 2005-14 vs 2015-25); the
  shift must fit inside the band.
- **Band = max(0.04, CI95 half-width, |era shift|)** — and the band feeds quote
  width, so uncertainty is literally priced.
- Minimum-n gate (MLB conditionals: n≥50,000). A cell under the gate stays
  UNMEASURED with its numbers shown — **never wire by hope** (trap: a fake zero
  is how "clean board" burned us).
- **Sign-flip alarm**: a measured value whose sign differs from the hand prior
  ⇒ investigate before trusting (likely a frame/label bug — but sometimes the
  prior was just wrong: btts|ml was +0.05 hand, −0.17 measured; trap B2.2).

### 4e. Emit the wire list
`docs/calibration/<sport>_wire_list.txt`: CONVENTION header lines, then one line
per entry: `<pair_key>[:orient][:rN] = <value> band <hw> (<tranche>; n, CI,
era-shift, source)`. Keys EXECUTED via `pair_key`. NOT-WIRED cells flagged
fail-closed. **This file is the canonical handoff** — it survives a spend-limit
death or context wipe (trap B7.1).

**Tools:** `tools/calibrate_pairs_from_history.py` (implied-ρ pipeline), the
Phase-1 measurement scripts, `tools/calibrate_*` per model.
**Operator asks:** corpus access (Retrosheet requires notice; StatsBomb/
Understat open); **spend headroom** for the measurement fleet (trap B7.1).

---

## STAGE 5 — Structural models & the exchange boundary (optional per sport)

**Entry:** measured pair table.
**Exit gate:** any structural pricer passes its OOS gate or stays gated off;
the constructible-vs-blocked matrix is probed on the exchange; impossibility
rules + tripwire cover every semantically-impossible cell.

**Work:**
- If a structural model fits (soccer Dixon-Coles; margin-total BVN for
  basketball/football; NegBin runs for baseball), calibrate it and **OOS-gate**
  it: held-out-season log-loss must BEAT independence AND the incumbent copula.
  Fail-closed (n==0 → INCOMPLETE, not pass — trap B6.2). NFL ml×over FAILED the
  gate and correctly stays 0.00.
- Probe the exchange for **constructibility** on demo (which pairs/side-mixes
  Kalshi's validator allows) — never assume from logic (Kalshi's validator is
  idiosyncratic: it blocks legal combos and allows some impossible ones). Build
  the taxonomy → exchange-matrix → engine-matrix (the containment sweep
  pattern).
- Wire impossibility rules for logically-impossible mixes, and pin the
  semantically-impossible cells in the **taxonomy tripwire** fixture so they
  decline LOUDLY if Kalshi's validator ever loosens (a live match = proof it
  loosened). Farmable=True ONLY on airtight one-official-record tautologies.

**Tools:** `pricing/structural.py`, `pricing/dixon_coles.py`,
`pricing/margin_total.py`, `pricing/mlb_runs.py`, the `validate_*_oos.py`
gates, `pricing/tripwire.py` + `taxonomy_impossible.json`.

---

## STAGE 6 — Wire it in (judged, isolated, bit-exact)

**Entry:** wire list + structural gates green.
**Exit gate:** values wired VERBATIM; the same-cache differential shows
untouched combos bit-identical and every mover on the predicted list; an
adversarial judge finds no defect; the suite is green.

**Work — the discipline that makes wiring safe:**
- Measurement agents NEVER touch the engine; wiring agents NEVER invent values.
  The wire agent transcribes the judged list verbatim and may not add anything
  not on it.
- Wire in an **isolated git worktree** if any long-running pass reads the main
  tree (trap B7.6). Keys generated by executing `pair_key`. Backtest mirrors
  that copy engine dispatch get synced + parity-checked (rule 8c).
- **Judge BEFORE the gate** (operator's ordering): adversarial semantics judge
  (attack sign/orientation/suffix-grammar; re-derive spot cells from the raw
  population) + the **bit-exact differential**: re-price all cached combos,
  everything not touching a changed key must be IDENTICAL (not "close"), every
  mover maps to an enumerated expected-diff class with its predicted direction,
  zero unexplained (proven by set arithmetic). One unexplained bit = FAIL.

**Tools:** the worktree pattern; `verify2/verify3` differential scripts;
`wire_expected_diffs.json`.
**Trap firewall here:** B2.1 (+7.32¢ sign inversion — a judge re-deriving from
the tape caught what the tests, sharing the bug's own fixture, could not);
B3.2 (pass big tables by FILE PATH, never inline-sliced).

---

## STAGE 7 — Gate, scorecard, and the readiness verdict

**Entry:** wired + judged.
**Exit gate — the go-live scorecard, all criteria met or explicitly waived:**
- Backtest gate PASS (promoted config beats flat baseline; strictly-pregame,
  zero-bias split — pricer reads `inputs.pkl`, outcomes in a separate file).
- **No combo family >2¢ median error** — OR the cell is settlement-evidenced as
  maker markup, not our miss (the ruler that separates the two is SETTLEMENT,
  never clearing: MLB's 2.01¢ hrr cell is fine because our fair 33.0% sits on
  the realized 33.3% while the clearing 34.8% is the padding).
- Bias tails healthy: **fair-above-clearing-by->2¢ share low** (we'd never win
  those — MLB 1.6%) and **fair-below-by->5¢ share low** (pickoff risk — the
  number to watch; WC's 9.4% was the red flag).
- Decline-reason histogram printed (silence must be enumerable — trap B5.1).
- Per-print mode + settlement exam where data allows.
- **Residual accounting: the gap ledger is EMPTY** (or every remaining item is
  a documented principled decline).

**If any criterion fails, the loop returns to Stage 4** for that cell — no new
sport, no new track, until the ledger is clean (the zero-gaps mandate).

**Tools:** `tools/backtests/{wc,mlb}_backtest.py` (dual-config, per-print,
zero-bias), the scorecard analyze step, the settlement exam.

---

## §C — The pricing grammar (reference)

- **Orientation** `:same`/`:opp`/`:tie`: `:same` = prop player's team IS the
  ML/spread YES team; batter-stat×player_ks `:opp` = the facing case. **Both
  sides measured directly — NEVER negate :same to get :opp** (asymmetric; the
  gap grows with the line — trap B2.6).
- **Rung** `:rN` = ticker line integer. Rung-keyed families vary per sport
  (MLB: hit/hr/tb/hrr/spread; ks/total/moneyline/rfi never). Chain suffixes in
  pair_key leg order. **No interpolation/extrapolation** — the tb×ks ladder is
  U-shaped, so exact rung keys only, then fall through exact→un-runged-oriented
  →plain→fail-closed (trap B4.6).
- **Windows/containments**: A⟹B priced exactly as P(B)−P(A) (band arithmetic,
  no ρ); conditional super-legs isolated from same-game companions (fail-closed
  — the B2.1 guard).

## §D — The verification ladder (which gate when)

Suite + mypy/ruff (every change) · parity (every rule-8 port) · dual-config
differential (every table change) · bit-exact expected-diff (every "surgical"
claim) · OOS gate (every structural pricer) · backtest gate (before trusting a
promoted table) · per-print (accuracy claims) · zero-bias structural design
(every backtest) · pre-registration (when confirmation bias is a risk) ·
adversarial judges (before promoting a sport) · settlement exam (markup-vs-
mispricing) · live exchange probe (farming/mechanics) · exchange-ledger
promotion (settlement conventions) · tripwire (standing) · scorecard (before
"ready") · residual accounting (every report).

---

## §F — THE FAILURE FIREWALL (read before you start)

Every trap the first two sports hit, and the tripwire that now stops it. Full
detail + report/commit citations in the timeline evidence.

**Quiet failures** — B1.1 same-game correlation keyed on event_ticker (per-
series, not per-game) silently priced everything at independence; tests green,
edge dead. → key correlation on the GAME code; a gate that bypasses the live
grouping can't catch a live-grouping bug. B1.4 the look-ahead artifact: pricing
off the latest snapshot leaked the future and made calibration look *better*;
killed by a structural gather-level pregame filter + pre-registered A/B.

**Sign/frame** — B2.1 +7.32¢ conditional-super-leg sign inversion (caught by a
judge re-deriving from the tape, not by tests that shared the bug's fixture).
B2.2 hand priors had wrong signs (btts|ml). B2.3 the away/home frame flip
(Team.A = game-code prefix = AWAY). → conventions owned in ONE place; sign-flip
⇒ investigate; both orientations measured.

**Keys/data** — B3.1 pair_key sort trap (execute the helper, never hand-write).
B3.2 4000-char prompt-slice silently truncated a 142-cell table (pass data by
FILE PATH). B3.3 poisoned event-ticker fixture hid a real bug (fixtures must
match prod conventions). B3.4 227-vs-230 count conflation (set-arithmetic
residual asserts).

**Invisibility** — B5.1 silent declines produce no error rows (print decline
histograms). B5.2 "clean board" summaries compressed away the open queue
(status summaries carry their ledger). B5.3 crypto flow admitted but unmodeled,
priced at flat 0.6 (the leg-series allowlist — MLB+WC only, per-sport kill
switch). B5.4 impossible mixes priced by copula as if possible (impossibility
rules + tripwire).

**Fetcher/infra** — B6.1 poller silent stall (source clearings from the gap-
free tape). B6.2 fail-OPEN gates (n==0 must be INCOMPLETE, not pass). B6.3
torn-CSV crash-safety.

**Process** — B7.1 spend-limit killed the agent fleet (persist the wire list
in-repo as the handoff; front-load spend). B7.5 per-item fetch over an 85k
universe died (batch/cache). B7.6 isolate risky code in a worktree while a pass
reads the main tree.

**Exchange-mechanics-were-wrong** — B8.1 "quote YES only" was BACKWARDS (parlay
seller quotes NO, yes_bid=0). B8.3 combo NO-payout unverified for months
(promoted only from a real ledger settlement). B8.4 the rain-scalar falsified
"strictly binary" (breaks MLB farm airtightness).

---

## §E — OPERATOR ASKS TO FRONT-LOAD (per new sport)

Queue these so no gate stalls on a human: **two demo Kalshi accounts + demo
funding** (blocks conventions promotion), **prod recorder creds**,
**SportsGameOdds key** (if using external odds), **the fee-schedule PDF**
(behind a bot-check — a human must fetch it), **the sport's rules text**
(settlement windows/scalar/DNP — the one place an unpinned rule turns a farm
into a loss), and **spend headroom** for the fleet. Sign-offs that each gate a
promotion: conventions promotion, staged-config promotion, wiring order, the
markup decision (deferred — pooled multi-week, props-first, never refit on one
window), kill-switch confirmation, prod limits (`prod_limits_configured`),
`--confirm-live`.

**The checklist, per new competition:** classification audit → regime flags
(rule/phase/settlement) → priors review → OOS gate → backtest gate → blind test
→ unblock one YAML prefix. Marginals always live; history only calibrates the
joint; farmable only on airtight one-record tautologies; never refit on P&L.

---

## NEXT STEPS

- Use this doc as the checklist for the next sport (NBA/NHL props at season
  start; a new soccer competition via the unblock path).
- Keep it current: when a new trap is discovered, add it to §F with its
  tripwire; when a gate type is added, add it to §D.
- Companion docs: `CLAUDE.md` (hard rules + defenses), `NOTES.md` (assumption
  audits + exchange mechanics), `docs/reports/README.md` (the dated blow-by-
  blow), `docs/calibration/` (wire-list + probe-matrix artifacts).
