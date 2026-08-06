# 2026-08-06 — DNP scalar pickoff: 3 same-player Marte combos, the rule, what was (and wasn't) built, the fix class

**Trigger (operator, verbatim):** "3 recent same player bets settled fully
scalar. The player didn't play. I thought we had something in place for same
player combos where even if the guy doesn't play, we either profit or don't
lose. We got picked off on 3 combos like that, if we lost money that means the
taker made money. I swear we worked on this in the past."

**Blast radius of this report:** READ-ONLY forensics. Live bot untouched; no
order placed or cancelled; live store read attempted `mode=ro` only (locked
under live load → all numbers below come from the session exchange caches
`night/` (992 settlements, 8/6 pull) + paced public `GET /markets/{ticker}`
rule fetches). No refit; no live module edited.

---

## 60-second verdict

- **What happened:** all three tickets are **Ketel Marte same-player 2-leg
  anti-correlated combos** (the exact structure the 7/23 audit passed as
  CLEAN/+EV *under binary settlement*). Marte was **scratched/not in the
  starting lineup** (exchange truth: all three of his 8/5 leg markets graded
  `result="scalar"`; his 8/4 legs graded binary). Under Kalshi's rule a
  DNP prop **"resolves to the fair market price"**, and the combo multiplies
  leg scalars — so the combo paid the **independence product of the leg
  prices (~17–21¢)** while we had sold YES at 6.8–10.1¢ against a
  correlation-priced fair of 2.4–6.1¢. The taker's payoff had nothing to do
  with baseball: DNP settlement **bypasses the correlation/mutex bound
  entirely** and cashes the independence-vs-correlation gap (~15¢/contract).
- **Cost:** **−$39.75 total** (−$9.22 on 7/23; −$15.05 and −$15.48 on the
  8/5 pair, settled 8/6 ~23:55 ET). Taker made the same +$39.75 (fees ≈ 0).
- **Was protection supposed to exist?** **No guard was ever built — by
  explicit decision.** `docs/dnp_scalar_settlement.md` (7/9–7/10) analyzed
  DNP, concluded "≈EV-neutral, build nothing" (operator decision 7/9;
  MLB re-affirmation flagged 7/10 and again 7/22, never closed). What WAS
  built is scalar-aware **accounting** (NO pays 1−V, commit `435809b`) and a
  **monitoring** decision (AS4, 7/22). The "profit or don't lose" memory is
  the 7/23 audit's mutex/containment bound — real, but it only binds
  **binary** settlements. The EV-neutral theorem is **false for same-player
  combos**: when all legs void at once, V = ∏(leg prices), not the
  correlated joint — a structural blind spot, on tape since 7/23.
- **Fix class:** price the void branch (fair = (1−h)·V_corr + h·⌊∏s⌋, h
  measured 2.35% from our own 978 finalized player legs) **plus** treat the
  computable DNP-sensitivity gap Δ = ∏s − V_corr as an UNKNOWN-taker-type
  width floor in the pregame lineup window (Δ was 14.7–15.5¢ on the 3 sniped
  tickets and ≤3¢ on every other same-player combo era-wide — the detector
  is exact with zero false positives on this corpus). [MEASURED-STRUCTURAL];
  the lineup-feed unlock is a [DECISION].

---

## 1) The 3 tickets (exchange truth, to the cent)

All: we hold NO (parlay seller), maker fills, single print each, pregame.
ET = EDT (UTC−4). "Indep ∏" = independence product of our leg mids at fill.

| # | ticker (tail) | filled (ET) | game start (ET) | lead | legs (all Ketel Marte, AZ) | qty | our NO cost | implied YES sale | our fair (YES) | entry EV | settle | NO pays | **loss** |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| T1 | `KXMVECROSSCATEGORY-…-E8BE6BD67FD` | 7/23 16:37:55 | 7/23 17:15 (AZ@STL) | **38 min** | HRR-2 **no** + TB-2 **yes** | 93.1 | 89.9¢ | 10.1¢ | 6.09¢ | +$3.73 | **scalar 20** (indep ∏ 20.8) | 80¢ | **−$9.22** |
| T2 | `KXMVECROSSCATEGORY-…-97F5BF3F0AC` | 8/5 18:45:43 | 8/5 21:40 (SD@AZ) | 2h54m | HRR-2 **no** + TB-2 **yes** | 136.84 | 91.0¢ | 9.0¢ | 5.95¢ | +$4.17 | **scalar 20** (indep ∏ 21.0) | 80¢ | **−$15.05** |
| T3 | `KXMVECROSSCATEGORY-…-A50F06E3000` | 8/5 18:46:18 | 8/5 21:40 (SD@AZ) | 2h54m | HIT-1 **no** + HRR-2 **yes** | 151.72 | 93.2¢ | 6.8¢ | 2.35¢ | +$6.75 | **scalar 17** (indep ∏ 17.9) | 83¢ | **−$15.48** |

Full tickers: T1 `KXMVECROSSCATEGORY-S202666D375DC4A8-E8BE6BD67FD` (rfq
`d95d14f3`, target_cost $100.00), T2 `KXMVECROSSCATEGORY-S2026F97C96E80E0-
97F5BF3F0AC` (rfq `311d7ebb`, target_cost $131.10), T3 `KXMVECROSSCATEGORY-
S20262E7569D7618-A50F06E3000` (rfq `007f239f`, target_cost $110.00). T2/T3
settled together 2026-08-06 03:55:50Z. Total loss **−$39.75** = taker profit
(settlement fees $0.006 total). These are the ONLY three same-player scalar
settlements in the 992-settlement corpus — the operator's "3" is exact.

**DNP is exchange-proven, not assumed:** `GET /markets` on Marte's 8/5 legs —
`KXMLBHIT/HRR/TB-26AUG052140SDAZ-AZKMARTE4` — all grade `result="scalar"`
(finalized 8/6 03:54Z), which per the rules text below only happens when he
was scratched / not in the starting lineup / started with 0 PA. His 8/4 legs
(same series) graded binary `no` — he played 8/4, not 8/5.

## 2) The rule (docs-first; fetched for the exact tickers)

| fact | text | provenance |
|---|---|---|
| MLB player-prop DNP | "If Ketel Marte is **scratched or not included in the starting lineup**, the market will **resolve to the fair market price**. If [he] starts the game but does not record a plate appearance, the market will resolve to the fair market price. … pinch hit at bats will not count." | doc: `GET /trade-api/v2/markets/KXMLBHIT-26AUG052140SDAZ-AZKMARTE4-1` `rules_secondary`, fetched 8/6 (identical clause on the HRR ticker; matches the 7/10 audit of all 9 MLB contract PDFs) |
| Combo of scalar legs | "Scalar outcomes are **multiplied (rounded down)**" — the collection's V = ⌊∏ vᵢ⌋ on the cent grid | doc: combo `functional_description`, captured live in `docs/dnp_scalar_settlement.md` §6/§7 (agent-verified 2026-07-09; the settled T3 market returns empty rules post-settlement — fetched 8/6) |
| So a same-player combo under DNP | ALL its legs void **simultaneously** → V = ⌊∏ (leg fair prices at freeze)⌋ = the **independence product**. Verified numerically on all three: T1 20.8→20, T2 21.0→20, T3 17.9→17 (our mids vs Kalshi's marks, floor) | this report §1 |
| NOT void/refund, NOT settle-NO | Kalshi player props neither void nor default-NO on DNP — the "we either profit or don't lose" outcome **does not exist** under this rulebook for a seller of anti-correlated same-player YES | doc: rules_secondary above; `docs/dnp_scalar_settlement.md` §7 ("Not voided, not refunded") |

**The mechanism that decides everything:** our edge on these combos IS the
anti-correlation (fair 2.4–6.1¢ vs independence 17.9–21¢). Binary settlement
realizes the correlation (mutex bound holds, audited CLEAN 7/23). Scalar DNP
settlement **prices the legs independently** — the one state of the world
where the correlation credit we sold is marked to zero. E[ΔP&L | DNP] =
V_corr − ∏s ≈ **−15¢/contract**, not the doc's "≈0": the §3–4 EV-neutrality
theorem conditions on *the other legs settling binary*, which is impossible
when the same player's legs void together.

## 3) Pickoff timing + template (F5-shape test)

- **T1 filled 38 minutes before first pitch.** MLB starting lineups are
  public well before that; Marte's leg graded scalar ⇒ he was not in that
  lineup ⇒ the scratch was **public knowledge at fill time**. Pickoff
  CONFIRMED on timing alone.
- **T2/T3 filled 2h54m pregame**, inside the 1–4h lineup-posting window —
  knowability not provable from exchange data alone. But: two RFQs **35
  seconds apart**, same game, same player, same 2-leg template family, and
  the two combos are **jointly near-contradictory under "Marte plays"**
  (T2 wants HRR<2, T3 wants HRR≥2) while **both** pay ~2–3× under DNP.
  No outcome-bettor buys both; a DNP-settlement bettor buys both. Same-actor,
  informed-flow shape.
- **Template continuity:** T1 (7/23) and T2 (8/5) are the *identical*
  structure (HRR-2 no + TB-2 yes, Marte), and the 7/23 audit records "a
  taker repeatedly hit Ketel Marte same-player cross-stat combos … 'no Marte
  1+, yes Marte 2+' went $74→$149 as the taker re-hit it" — T3's template.
  Creator identity is not persisted in our caches and the live store was
  locked (`mode=ro` attempts backed off) — same-account attribution
  UNVERIFIED, but the template/timing/pairing evidence is one actor's shape.
- Quote→fill was 8 s (T2) — our quoting was not slow; we were simply
  pricing a branch (DNP) at zero that the taker priced at ~1.

## 4) What EXISTS vs what NEVER existed (the honest answer)

The operator DID work on this — twice — and both times the decision was
explicitly to not build a guard:

| built / decided | what it does | citation |
|---|---|---|
| **DNP economics doc** (7/9, MLB amendment 7/10) | full scalar math (V=∏vᵢ, NO pays 1−V, branch analysis); concluded "≈EV-neutral, variance-compression, NOT seller edge"; **operator decision 2026-07-09: BUILD NOTHING (reactive)**; MLB re-affirmation flagged | `docs/dnp_scalar_settlement.md` (commits `3e02bdc`, `b93eaa4`, `0a0ab2f`, `402210e`) |
| **Scalar-aware accounting** | NO pays 1−V with floor convention in balance/P&L/reconcile; scalar rows never coerce to 0/1; the 8/5 sweep's "15 scalar settles" seam handled via exchange revenue truth | `risk/balance.py` (§158–230, 813), `rfq/lifecycle.py:8012`, commit `435809b`; `docs/reports/2026-08-05-full-pnl-sweep.md` seam table |
| **Fail-closed fact resolution** | `result=="scalar"` → permanently unresolvable → no receivable/no phony 0/1 fact | `marketdata/settled.py:454–457` |
| **AS4 gating decision** (7/22) | scalar hole in the give-back shield: **ACCEPT-AS-IS + monitor** for MLB arming; classed the scalar surface as an *availability* risk ("false halt, NOT a mis-shielded dollar and NOT a trading loss") | `docs/reports/2026-07-22-scalar-dnp-as4-gating-decision.md` |
| **Same-player pricing audit** (7/23) | the exact Marte structures audited CLEAN and +EV **under binary settlement**; mutex/containment bound verified; family kept quoting by operator choice | `docs/reports/2026-07-23-same-player-hrr-audit-and-player-concentration-gap.md` |
| **NEVER built** | any DNP/scratch **pricing term, width, gate, or lineup feed**. The doc's §8 "adverse-selection guard (widen/cap on high hazard + stale price)" was rated OPTIONAL/low and not built; §8 pricing explicitly said "do NOT add a DNP term to fair" | `docs/dnp_scalar_settlement.md` §8 + Recommendation table ("BUILD NOTHING") |

**Where the analysis was wrong (the blind spot):** every prior document
modeled DNP of ONE leg among independent legs (s ≈ p ⇒ neutral). None
modeled the **same-player combo**, where DNP voids *all* legs at once and
settlement becomes the independence product — precisely nuking the
correlation credit that makes those combos ours. The counterexample was
already on tape (T1, 7/23, −$9.22) but was absorbed into the slate
reconcile as part of "Marte shorts −$18.98, mutex bound HELD" without the
scalar settlement being recognized as the mutex bound being *bypassed*. The
7/22 AS4 call ("not a trading loss") was correct for the shield hole it
analyzed but wrong as a statement about the scalar surface generally.

## 5) The fix class (mechanism only — no hand-set numbers)

The rule (§2) makes the DNP branch **exactly priceable**: on DNP the combo
pays ⌊∏s⌋ where s are the leg marks we already carry. Both quantities in the
correction are computed, not tuned:

- **Δ (DNP-sensitivity) = ∏s − V_corr** per combo, from inputs already in
  the quote path (leg mids + our joint fair). Measured on the era corpus:
  **Δ = 14.7/15.0/15.5¢ on the three sniped tickets; Δ ≤ 3¢ on all 24 other
  same-player combos** — the detector separates the sniper surface exactly,
  zero false positives.
- **h (DNP hazard) = measured** from our own settled-leg corpus: 23 scalar
  / 978 finalized MLB player-prop legs = **2.35%** (HIT 2.8%, HR 4.5%, HRR
  2.1%, KS 1.8%, TB 7.1%) — refreshed continuously, per-family, never
  hand-set.

Options, judged against the rule:

1. **[MEASURED-STRUCTURAL] Void-branch pricing (do first):** fair =
   (1−h)·V_corr + h·⌊∏s⌋. Corrects the model's structural omission (the sim
   already supports per-leg scalar settlement distributions — `sim/engine.py`
   — nothing populates them; `docs/dnp_scalar_settlement.md` §8 sketched the
   mixture). With base-rate h this moves same-player fair by h·Δ ≈ 0.4¢ —
   honest, but it does NOT stop an informed sniper (their h = 1).
2. **[MEASURED-STRUCTURAL] Δ-scaled width under taker-type UNKNOWN:** in the
   pregame lineup window a large-Δ same-player combo faces a taker whose h
   may be 1, and we cannot identify the type ⇒ standing UNKNOWN→widen rule
   applies to the *DNP branch*: quote YES for large-Δ structures no lower
   than the value robust to h∈[h_base,1] — operationally, at/near ∏s until
   the player is confirmed — or decline. This closes the sniper's payoff
   (they'd pay ~the DNP settlement value for the DNP branch) while leaving
   every small-Δ combo (99.7% of era premium) untouched. No knob: Δ and the
   window derive from the quote inputs and the schedule feed already in
   `rfq/pregame.py`.
3. **[DECISION] Lineup-status feed:** external per-player lineup/scratch
   status collapses h to ~0 (confirmed starting) or 1 (scratched); restores
   the ability to sell the correlation credit (the 7/23 audit's +6¢/contract
   on this flow) even pregame. New external dependency + freshness
   discipline — operator call whether the family's volume justifies it.
4. **Rejected: static blocklist of same-player combos** — violates the
   standing no-static-blocklists rule, and the family is +EV when settlement
   is binary (audited 7/23; era same-player realized −$42.78 *including* the
   three pickoffs, i.e. ≈ break-even book despite paying the sniper).

**Era-wide exposure of the class [measured]:** player-prop-carrying combos =
$11,201 of $19,331 era premium (57.9%); filled in the 0–4h pregame window =
**$4,886 over 12 days ≈ $407/day** (the window where scratch information can
exist). The *sniper-exploitable* subset (same-player, large Δ) is tiny and
fully accounted: 3 tickets, $349.62 premium, **100% of the structure's
offered volume was hit, −11.4% ROI realized vs +$14.65 model-claimed**. The
class cost is bounded (~$40 so far) but the hit rate on offered large-Δ
volume is 3-for-3 — it will scale with our size, silently, at every scratch.

## NEXT STEPS

- **Operator (decisions owed):** (1) ratify the fix-class ordering — void
  branch pricing (#1) + Δ-width under UNKNOWN (#2) as the mechanism, lineup
  feed (#3) as an optional later unlock; (2) the long-flagged REACTIVE
  fractional-V re-affirmation (7/10, 7/22) is now MOOT on the accounting
  side (handled) but the *pricing* half of the stance is falsified by this
  incident — retire it.
- **Build session (rule 8: prototype in tools/, parity-check, then port;
  vitals gate before commit):** implement #1+#2 at the joint/quote seam;
  regression: the three tickets' inputs must reprice from 6.8–10.1¢ YES to
  ≥ ~17¢ (T3) / ~20¢ (T1/T2) in the unconfirmed-lineup window; add the
  same-player-scalar case to the vitals corpus.
- **Watch (no action):** further large-Δ RFQs on any single player pregame —
  the fill-prober already surfaces the family; creator attribution when the
  store is next quiescent (same-account question, §3).
- **Bookkeeping:** memory row `project_kct_resume_state` note: "mutex bound
  HELD" on 7/23 must carry the caveat that T1 in that slate was a scalar
  BYPASS of the bound, not a test of it.
