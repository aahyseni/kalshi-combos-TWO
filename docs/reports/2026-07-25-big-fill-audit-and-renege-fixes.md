# 2026-07-25 (night) — Big-fill audit: why the $20–50 combos die, and the four renege fixes

## The operator's question

> "we're missing the $20-50 combos, the 4 team ML parlays, the HR HR
> parlays… get to the bottom of this."

## The audit (16-agent workflow, every claim adversarially re-verified against the store + tape + code)

**Headline: we WIN the $20–50 auctions and then renege.** Tonight through
8:47p log time: **49 auctions won → 15 filled; $355 of taker premium
won-then-declined vs $84 filled**, including the exact archetypes: a $50
3-leg (premium $47.28), a $20 HRR+HRR+OUTS, six $14.50 GAME/SPREAD/TOTAL
combos. Confirmed root causes:

1. **Sizing defect (HIGH)**: `_risk_qty` converted target-cost RFQs at OUR
   cheapest bid (the NO bid, ~80¢ on longshots); the exchange awards at the
   TAKER's price (~19¢) — quote-time risk saw fills **3.6–4.7× smaller**
   than the accept that arrived, so caps passed a phantom-small quote the
   confirm layer correctly declined at true size.
2. **Model-disagreement renege**: same-game GAME+SPREAD+TOTAL combos priced
   +EV by the calibrated pricing fair but scored −EV by the gate's
   band-high risk copula → 20 `negative_ev_not_risk_reducing` declines of
   won auctions.
3. **Double-count (review HIGH)**: even at true size, the accepted quote's
   OWN resting entry stayed in the book at confirm — the fill counted twice
   (resting hypothetical + candidate) → confirm demanded ~2× what
   quote-time admitted on the summing axes.
4. **Waiver peak-flow gap**: the rescue waiver's stability key used GLOBAL
   generation/version counters — any concurrent fill anywhere invalidated
   certificates for unrelated games ("book moved during every enumeration").
5. Context findings: `max_open_quotes: 200` (hand-bumped 60→120→200) eats
   ~$3.2M/day of flow arrival-order-blind; the pregame-only gate kills
   ~$1.58M/day by mid-evening (policy — path = measured in-play shadow);
   slate figure ~6-7× inflated by full-loss-per-game attribution
   (mechanism confirmed; zero fills lost to slate-alone tonight).
   **Refuted**: markup uncompetitiveness; `skip_size_above_max` (only
   $500+ whales). Also: the persistence writer ran ~40 min behind the log
   (measurement-blindness debt).

## The fixes (`fdc9f6d`, suite 2759/0, 3-lens adversarial review — all findings addressed)

| flag (all default OFF) | fix |
|---|---|
| `risk_qty_award_sizing` | contracts = target / ($1 − bid): a fee-free strict UPPER bound on the award; >99¢ bids unresolvable → no-quote |
| `gate_ev_from_pricing_fair` | admission EV = the calibrated fair's edge, **re-priced FRESH at confirm** (pickoffs still caught); fallback to MC EV is metered + logged; `admission_ev_cc`/`admission_ev_source` now in every gate log + decline detail |
| `waiver_game_scoped_stability` | stability compares the breached games' position/reservation CONTENT — unrelated-game fills stop killing certificates |
| `release_accepted_quote_exposure` | the accepted quote's economically-dead resting entry is removed before confirm checks (no-double-risk-layers) |

Arm all four together (comment block ready in the armed YAML) at a
**pregame restart** — per the validate-caps-can-quote rule, the arm
validation is: replay tonight's reneged sizes and confirm non-zero quoting
plus zero renege-class declines on the same shapes.

Also shipped: **`DECLINES.bat`** — plain-English per-decline lines + wall
tally (operator: "so hard to read what for").

## NEXT STEPS

- **Operator**: arm the 4-flag renege bundle at the next pregame restart
  (uncomment 4 YAML lines; restart via STOP/START bat); watch
  `admission_ev_source` + renege-class declines → should go to ~0.
- **Claude (queued builds, from the audit's fix directions)**: derived
  open-quote capacity + EV-based eviction (dissolve max_open_quotes 200) +
  phantom-slot release at exchange-death; slate cap committed-only backstop
  + governor-derived resting floor; persistence writer backlog; in-play
  shadow measurement design; entity-axis BOUNDS; leg_axis arming read-out.
- Post-slate P&L + settlement reconciliation.
