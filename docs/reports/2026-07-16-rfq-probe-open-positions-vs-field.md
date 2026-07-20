# 2026-07-16 — RFQ probe: our two open ESP-ARG-legged fills vs the current maker field

**What:** re-created RFQs (target-cost $25, never accepted, deleted after ~22s) on the two
combos we filled today at 15:47–15:48 UTC, and read every competing maker quote
(`get_quotes(rfq_id, rfq_user_filter="self")` — the 2026-07-14 probe technique, now persisted
at `tools/diagnostics/rfq_price_probe.py`). Probe ran 16:26–16:27 UTC, ~40 min after the fills.

## Fill 1 — KXMVECROSSCATEGORY…3F55FA29427 (yes:FRAENG-BTTS + yes:ESPARG-BTTS)

Our fill: **NO @ 64.00¢ × 39.87ct** (taker paid 36.00¢ YES; premium at risk $25.52).

Field now (23 makers responded): best competing NO bid **63.90**, then 63.60 / 63.50 / 63.40 /
63.20 / 63.10 — six makers within 0.8¢. Deep, calibrated, liquid combo. Two-sided makers show
YES bid up to 34.40 → market spread roughly 34.4 / 36.1.

**Verdict: we filled 0.10¢ inside today's best competitor** — the same thin-sharp-win pattern as
the 2026-07-14 price-discovery report (we win mains by 0.2–0.8¢). Mark vs best bid ≈ −0.1¢/ct
(−$0.04 total): filled at market.

## Fill 2 — KXMVESPORTSMULTIGAMEEXTENDED…04EA5F03582 (ESPARG corners 9+ & Messi 1+ & ARG 4+ tcorners)

Our fill: **NO @ 83.30¢ × 9.16ct** (taker paid 16.70¢ YES; premium at risk $7.63).
NOTE: this fill was AFTER the corners +3¢ edge-floor went live (deployed 14:58 UTC, `1e2e14c`)
— the floor is already in this price.

Field now (15 makers): best competing NO bid **80.70**, then 78.2 / 78.0 / 77.5 / 77.2 / 76.6 /
76.2; serious-cluster median ≈ 77.

**Verdict: we filled 2.6¢ inside the best competitor and ~6¢ inside the median maker** — even
WITH the +3¢ corners floor, the field prices corners-carrying parlays materially richer than we
do. Consistent with (and extends) the #37 corners-richness finding: the floor closed part of the
gap, not all of it. Mark vs best bid ≈ −2.6¢/ct (−$0.24 total) — trivial dollars at this size,
but a real signal on floor sizing.

## Bonus live finding — our own bot declined to re-quote both probes

The live bot saw both probe RFQs, priced them (positive candidate EV), and **declined**:
`skip_game_loss_cap` with concentration util **0.94** on the FRAENG/ESPARG book (fill 1) and
`skip_max_open_quotes` / `skip_game_loss_cap` at util 0.82 (fill 2). The widen-vs-decline shadow
agrees ("near cap on concentrating flow"). I.e. the risk engine considers us capped-out on these
games and is refusing further same-direction flow — the quote-time analytic caps working as
designed. (The in-flight Problem-A waiver is CONFIRM-path only per the E2 invariant, so these
quote-time declines are intended to remain.)

## Caveats

- 40-minute gap between fill and probe; the tight 6-maker cluster on fill 1 argues prices are
  stable, but this is one snapshot, not a pooled measurement.
- Field NO bids on a probe RFQ are indicative maker quotes, not clearing prints — winner's-curse
  logic says the eventual clearing sits at/above the best bid shown.
- Never refit markup on this — pooled multi-week evidence only (standing rule).

## NEXT STEPS

- **Me:** Problem-A continuation workflow (`wf_a220ac69`) is running — review on completion,
  then the dated Problem-A report + full suite.
- **DECIDED (operator, ~17:20 UTC same day): corners edge-floor 3¢ → 4.5¢** — armed in
  `config/prod-live-wc.local.yaml` (`series_adders_cc: KXWCCORNERS/KXWCTCORNERS 450`), loader
  validated. Takes effect at the next bot restart (held for the Problem-A review). Note this is a
  measurement-driven move (field position from this probe + the 7/14 backtest + the 7/15 ρ
  measurement), not a P&L refit.
- **Standing:** bot restart waits for the Problem-A review (one restart on fully-reviewed code,
  tiers + waiver together).
