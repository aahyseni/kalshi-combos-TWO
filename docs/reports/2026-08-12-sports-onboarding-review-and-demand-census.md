# 2026-08-12 — Sports onboarding review: playbook refresher, demand census, and the wiring order

**Operator direction (2026-08-12):** "we are going to code in some more sports
and tickers to further help diversification… review an old report we made on
how to wire sports, so we can efficiently wire and start quoting new ones."

**The playbook exists and is battle-tested:** `docs/sport_onboarding_playbook.md`
(distilled 7/12 from the soccer/WC and MLB builds) — 8 gated stages
(recon → classification → marginals/grammar → pair matrix → measure the joint →
structural models/exchange boundary → wire bit-exact → scorecard), the five
overriding laws (marginals always live, source-of-truth, fail-closed UNKNOWN,
no unexplained residuals, never refit on P&L), and the §F failure firewall
(every trap the first two sports hit). This report does NOT restate it — it
answers "which sport, in what order, and what's already on the shelf."

**Why this is the right month for it:** diversification is the operator's
chosen lever for P(book) — and the 8/12 forensics showed capital alone cannot
buy it on a one-slate book (8/11: 180 tickets, ONE game-cluster). Every new
independent sport adds real clusters. Sports wiring is pricing-side and
allowlist-gated per sport, compatible with the 8/31 engine freeze (the risk
engine is untouched; a new sport is classification + tables + one YAML prefix
at the very end, with the bit-exact differential proving untouched sports
byte-identical).

---

## 1) Demand census — what takers actually request TODAY

New tool: **`tools/diagnostics/rfq_series_census.py`** (read-only, `mode=ro`;
verified against the recording seam: `record_rfq` fires BEFORE any filter or
pricing at `quote_app.py:2493–2499`, so absence = no taker demand, not our
filter). Run 8/12 over the last ~8M RFQs (8/8 → 8/12):

| leg series (sampled 200k RFQs) | share | status |
|---|---|---|
| KXMLBGAME / TOTAL / KS / SPREAD / HIT / HR / HRR / RFI / TB / OUTS / RBI / SB | ~97.5% | allowlisted, quoting |
| KXLOLGAME + KXCS2GAME (esports) | ~2.5% | allowlisted, quoting |
| **anything else** | **0 in 200k sampled (~8M window)** | — |

**Finding: there is ZERO latent non-allowlisted RFQ demand right now.** Nobody
is requesting WNBA/soccer/NFL combos today — so wiring order is driven by the
**season calendar** (demand appears when Kalshi's app offers the parlays and
takers arrive), not by the current tape. Historical proof the channel does
carry new prefixes when demand exists: the June crypto/Oscars parlay flow and
the 7/31 KXMLBF5 launch both showed up on this exact axis.

**Standing cadence:** run the census weekly (and at every league opener). A
non-allowlisted prefix with real flow = the trigger to start the playbook for
that sport. This is the F5 lesson inverted — see it on the tape before it
matters, this time as opportunity instead of pickoff.

## 2) Shelf inventory — what's already built per candidate sport

| sport | machinery on the shelf | what's missing | season timing (2026) |
|---|---|---|---|
| **Club soccer (EPL / top leagues)** | The ENTIRE soccer engine is club-trained: Dixon-Coles OOS-gated on 8,980 held-out CLUB games; pair tables measured largely on club corpora (football-data, Understat); classification, containments, bands, farming rules, calibration tools all exist from WC | Stage-0 recon of the club series prefixes (never assume ticker shapes — the KXWC1H trap); settlement scopes per league (simpler than WC: no extra time in league play); per-league priors review (the WC advance/pens machinery drops out); allowlist prefix | **EPL opens mid-August — imminent**; other top leagues within weeks |
| **NFL** | `pricing/margin_total.py` BVN calibrated through 2025, **OOS gate PASSED 7/6** (biggest win hw×cover), was briefly ENABLED; nflverse corpus open | Spread legs blocked until in-season ticker line-sign verification (the L1 frame trap — deliberate); prop families (if Kalshi offers NFL props) need Stages 1–4; freshness re-check of the 2025 calibration | Preseason NOW; opener early Sep — **likely the biggest parlay-demand onboard of the year** |
| **WNBA** | margin_total calibrated (data refreshed 7/5) | Gated pending an odds source or prod-shadow settlements; census shows ZERO current RFQ demand | mid-season now — wire only if the detector fires |
| **CFB (college football)** | margin_total family fits; corpora open | full Stages 0–4 (new classification, huge team universe) | late August openers |
| **NBA / NHL** | NBA margin_total calibrated; NHL not built | NBA: same as WNBA path; NHL: full playbook | October |
| **More esports (Dota2, Valorant, …)** | LOL/CS2 live; esports flat 3¢ markup floor exists | Stage-0 recon of what Kalshi lists; map-winner series still unquotable pending match-map ρ | year-round; current esports flow is only ~2.5% of RFQs |
| **KXMLBF5 (first-5-innings)** | Removed from allowlist 7/31 after the $275 pickoff | classification + measured pair ρ (Retrosheet innings 1–5 — same corpus we already use) | in-season now; re-admit = a measured mini-onboard, not a config flip |
| **UCL/UEL/UECL** | soccer engine | two-legged-tie leg handling (filters decline them today) | group stage Sep |
| **Racing (F1/NASCAR)** | — | HELD on start-time provenance (pregame gate needs verified starts) | ongoing |

## 3) The wiring order (recommendation)

1. **Club soccer, EPL first** — highest shelf-reuse (the engine is literally a
   club-soccer engine we aimed at the WC), demand arrives mid-August, and it
   directly adds non-MLB game-clusters on MLB nights (evening EU kickoffs
   overlap afternoon slates; weekend mornings are pure additive coverage).
   Playbook pass is mostly Stages 0–1 + priors review + backtest gate — the
   heavy Stage-4/5 work is already done and OOS-gated.
2. **NFL in parallel at recon depth** — start Stage 0 (series/rules universe)
   and the line-sign verification plan NOW so the sport is wire-ready before
   the September opener; the demand spike will be large and immediate.
3. **Weekly demand census** — anything new that fires (WNBA, Dota2, a Kalshi
   launch like F5) jumps the queue at detector strength.
4. **KXMLBF5 re-admit** — small measured onboard using Retrosheet; recovers
   flow we currently decline by prefix. (It IS current-season demand when the
   requester returns.)
5. UCL tie-handling before September group stage; NBA/NHL at season approach.

**Not recommended now:** WNBA (zero demand on tape despite being mid-season —
the detector decides); racing (blocked on start-time provenance).

## 4) Operator asks to front-load (playbook §E, per sport)

- **Club soccer:** rules text per league (settlement scopes; league games are
  regulation-only — confirm per Kalshi rulebook); confirm demo accounts still
  funded for constructibility probes; spend headroom for the measurement/judge
  fleet at wiring time.
- **NFL:** rules text (OT totals conventions, prop DNP definitions); the
  spread line-sign verification needs one in-season slate of real tickers
  (preseason may suffice — verify against preseason tape when prefixes appear).
- Both: no new data purchases needed (football-data/Understat/nflverse open).

## NEXT STEPS

- **Me:** start Club-soccer Stage 0 (series/rules recon from tape + public
  API) next session; NFL Stage-0 recon behind it; weekly
  `rfq_series_census.py` runs (first standing run ~8/19 or at EPL opener).
- **Operator:** provide/confirm league rules text when Stage 0 surfaces the
  series list; demo-account funding status for the constructibility probe;
  spend headroom at the measurement stage.
- **Standing:** every stage exits through its playbook gate; the allowlist
  prefix is the LAST step per sport; the engine freeze (8/31 ledger) is
  untouched by any of this.
