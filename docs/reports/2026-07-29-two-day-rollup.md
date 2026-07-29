# Two-day rollup: 2026-07-27 → 2026-07-29

**Standing rule (operator, 2026-07-29):** "make reports of all the changes for
the past 2 days, it's a standing rule." This is the sweep across the whole
window, including what I got wrong. Per-change detail lives in the individual
dated reports linked below.

---

## 1. Everything that shipped

| commit | date | change | direction |
|---|---|---|---|
| `da29585` | 7/26 | per-pair within-game ρ; tail joint split out | correctness |
| `95588f7` | 7/27 | bounded delete, bounded shutdown, supervisor scope | uptime |
| `e742c83` | 7/27 | launcher auto-clears a MACHINE-written KILL | uptime |
| `9a93925` | 7/27 | confirm-path ρ scoped to same-game pairs | throughput |
| `dd3cdbb` | 7/28 | slate cap stops counting one loss event once per game | correctness |
| `15fa70d` | 7/28 | entity tier rebuild report | docs |
| `27de1e0` | 7/28 | settled legs, hedge credit, value-ranked budget | correctness |
| `eb5bbd8` | 7/28 | portfolio haircut → operator anchor (**loosening**) | capacity |

Detail: [`2026-07-27-confirm-path-b1-pair-scoping-b2-predictor-deleted.md`](2026-07-27-confirm-path-b1-pair-scoping-b2-predictor-deleted.md),
[`2026-07-28-slate-partition-armable-and-failclosed.md`](2026-07-28-slate-partition-armable-and-failclosed.md),
[`2026-07-28-tiered-entity-load-rebuild.md`](2026-07-28-tiered-entity-load-rebuild.md),
[`2026-07-28-settled-legs-hedge-credit-value-ranking.md`](2026-07-28-settled-legs-hedge-credit-value-ranking.md),
[`2026-07-28-portfolio-haircut-anchor.md`](2026-07-28-portfolio-haircut-anchor.md),
[`2026-07-27-vital-signs-gate.md`](2026-07-27-vital-signs-gate.md).

**Seven of eight are corrections — they stop charging for risk that does not
exist.** Exactly one (`eb5bbd8`) genuinely raises what we can lose, and it was
ratified explicitly.

## 2. The measured effect, and the honest verdict

Sizeable defects were real and are fixed: the ρ game-max collapse (p_book 0.37 →
0.83 at peak), the confirm-window discard (96% of the candidate gate's ρ work was
dead; conversion 4/4 then 93%), the slate double-count (1.88–1.98×), settled legs
charged ($80.20), and the haircut feedback loop.

**But the edge itself remains indistinguishable from variance.**
P(excess ≤ 0) = 0.061 on roughly 7–10 independent game-clusters. That is not a
result. The operator called this correctly before I did — see §4.

What IS established by measurement, and is not variance:

| finding | figure |
|---|---|
| our FAIR is good (MLB) | median abs error **0.36¢**, 78.3% inside a tick |
| our ASK is the problem | **+2.70¢** above observed prints |
| cheap NO is where the return is | <50¢ → **+77.9%**; ≥85¢ → **+1.1%** |
| det-max is shape-blind | it IS the premium sum; ratio to comonotone 1.000001 |
| the tail gate has never fired | **0 breaches in 104,803 risk_audit rows** |

## 3. Live wall mix this morning (fresh tape, ~40MB window)

| wall | declines | share |
|---|---|---|
| `skip_entity_loss_cap` | 2,822 | **46.4%** |
| `skip_size_above_max` | 1,370 | 22.5% |
| `skip_utilization_backstop` | 1,153 | 19.0% |
| `skip_per_combo_loss_cap` | 594 | 9.8% |
| `skip_max_open_quotes` | 2 | 0.03% |
| **portfolio CVaR / det-max** | **0** | **0.00%** |

Send rate **25.4%** (2,023 sent / 7,964 decisions). det-max **$408.84** against a
**$1,058.50** wall.

Read that carefully: **the book is 38.6% deployed and we still refuse three
quarters of flow.** The portfolio risk measure — the thing that is supposed to
be the judge — refused *nothing*. Dollar counts and per-entity walls are doing
all the governing, and the operator's ratified risk anchor (P(KILL-night) ≤ 2%
at a 12% KILL line) has been decorative since it was armed.

`risk_starvation_watchdog` fired at 20 consecutive risk-driven declines. The
watchdog is correct; the caps are what it is complaining about.

## 4. What I claimed and then had to retract

Recorded in full because the pattern matters more than any single error: **every
one of these was me over-reading a small sample, and in three of them the
operator was right and I was wrong.**

| claim | reality | who called it |
|---|---|---|
| "6.1× edge" | variance; one 27% shot was 40% of P&L | **operator: "we got lucky"** |
| "diversifiers quoted 3× wider" | my sign-frame error — both figures were rebates; diversifiers were already 0.13¢ *tighter* | me, on re-check |
| "a hidden extra 2¢ on markup" | 4.05¢ pooled across WC eras; 98.2% was the operator's own tier | **operator: "So there was nothing wrong going on fix 1"** |
| "the entity fix unlocks 32%" | it admits ZERO of 26,416 — strictly tightening | me, on re-check |
| "56/66 losing together is damning" | 1-in-200,000 at worst; real inflation 1.3–1.9× | **operator: "probability of 55/66 hitting on 1 given night is probably extremely low"** |
| in-play-game count | I echoed the operator's guess back as established fact | **operator: "never assume what i say is right"** |

Standing correction to my own behaviour, now in memory: a number gets reported
as a *finding* only after cluster-bootstrap over game-components. Before that it
is a hypothesis, and it gets labelled as one.

## 5. Uptime

Two outages in the window — 8h36m (518 unread alerts, unnoticed) and 6.7h (clean
shutdown). Root cause of the second: a bot launched via `Start-Process` from my
session does not survive session cleanup. **`START_BOT.bat` is the durable
path.** Operator's response is the correct standard to hold:

> "We keep having to harden this bot over and over again on stuff that should've
> happened already."

That is why `tools/vitals/` now exists and is CLAUDE.md hard rule 9: eight
changes in 48h produced seven regressions with 3,081 unit tests green throughout.
The suite asserts the mechanism the author had in mind; the gate varies the
discriminating variable against degraded state.

## 6. In flight as of this report

1. **Re-anchor the tail gate** to the ratified 12% KILL line and 2% probability
   budget, so P(KILL-night) governs instead of a dollar count. Projected on the
   live book: 25 → 46 tickets, $789 → $2,145 premium, EV $29.73 → $93.56 (+215%),
   ES/premium 0.472 → 0.217, N_eff 15.65 → 25.98, P(KILL-night) 1.50% (inside
   budget). det-max is **retained** as a model-failure backstop, not removed.
2. **Esports markup → flat 3¢** (operator-directed; reverses the prior freeze).
3. This rollup.

## 7. Cross-game correlation — operator ruling, and the one caveat

Ratified 2026-07-29:

> "cross games are independent for sure, one mlb game doesnt affect another
> game, and that goes for any sport, same with esports."

`DEFAULT_CROSS_EVENT_RHO = 0.0` stands; no family is quarantined for lack of a
measured cross-game ρ.

**The caveat, recorded so it stays answerable.** The risk here is not causal
linkage between games — the operator is right that there is none. It is
**correlated model error**: if our strikeout model runs rich, it is rich on every
pitcher simultaneously, which makes genuinely independent games behave like one
bet in the tail. The games are independent; our mistake is not.

Measured sensitivity, and it is not small:

| cross-ρ | ES99 | modeled EV |
|---|---|---|
| 0.00 | $221.64 | **+$10.84** |
| 0.25 | $280.65 | **−$4.25** |

A cross-ρ of 0.25 is the entire edge. So a per-slate diagnostic estimating
realised cross-game residual correlation **from settlements** ships with the
re-anchor — **instrumentation only, never a gate**. This converts the assumption
from something argued into something measured, without overriding the ruling.

---

## NEXT STEPS

- **Runs next:** re-anchor build + adversarial gate (workflow in flight); esports
  markup to flat 3¢ with sha256 proof that no other tier moved; suite + vitals
  8/8; then one operator restart carrying everything.
- **Owner:** me for build, gate, arming read-out and the first-hour watch.
- **Binding wall after the re-anchor lands — flagged now:** entity, at 46.4% of
  declines, becomes the dominant refusal. `skip_utilization_backstop` (19.0%) is
  a *clearing* constraint (gross settlement notional > 3× bankroll), not a risk
  proxy — if it starts binding it needs its own decision, not a cap bump.
- **Decisions owed by operator:**
  1. Resume posture after the restart.
  2. Whether entity tiers get re-derived once the portfolio measure governs —
     they were sized against a regime where the tail gate never fired.
  3. Nothing on cross-ρ; ruling stands, diagnostic is read-only.
