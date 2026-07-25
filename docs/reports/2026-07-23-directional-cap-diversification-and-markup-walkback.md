# 2026-07-23 — Under-fill investigation: directional-cap diversification + markup walk-back

**Operator thread:** "we're one-sided on DET, fine — but we CAN still quote the
other team / overs / props / anything diversifying" → then "we're not filling
nearly enough, only ~$270 spent of $2,380; the risk blocks might be net-negative
by disallowing +EV bets that hedge / raise P(book)" → decision: **"walk back
markup 1c."**

Live context: prod MLB, adaptive caps in **shadow** (WC static caps enforce),
3-game afternoon slate (TB@TOR 3:07p done, AZ@STL 5:15p, KC@DET 6:40p pregame).

## What we found (evidence, not theory)

**1. Directional-cap mechanism** (`risk/exposure.py:570–580`, design note). The
enforced directional cap binds on the P0-9 mutex-aware bound that nets **exactly
ONE game-result ME event** (moneyline / 1X2 / advance) via max-over-branches, and
**fails closed to the comonotone SUM on 0 or ≥2 ME events**. Binary markets
(over/under, BTTS) and props "REFINE the partition" → deliberately NOT credited in
the enforced fold (would break the mass-acceptance monotone-dominance proof E2);
that richer all-legs hedge credit lives ONLY in the candidate-aware MC. So:
- **Opposite team (KC-win)** → different moneyline branch → **NETS → flows.** ✓
- **Overs / props** → ride as COMMON → **add** to the directional magnitude → can
  be vetoed by the coarse cap once a game is near its limit. (This matches the
  prior shipped decision — `2026-07-18-mutex-detmax-waiver-fingerprint.md` item 3:
  a one-sided book frees opposite-branch flow, concentrating flow declines.)

**2. Held concentration.** 11 combos, $229 total; **82% ($192.63) on KC@DET**, all
the same stacked wager — short "Detroit wins a low-scoring game" (NO on
"yes Detroit, no Over 8.5/9.5"). Positively correlated: a DET pitchers'-duel win
sinks the $93+$59+$29+$9 together (~$190 = **8.4% of $2,260 bank**, within the 12%
KILL). The other two games are small (TB@TOR $25, AZ@STL $16).

**3. The narrative correction (open-quote decomp).** We ARE resting diversifiers on
KC@DET — overs (`yes Over 2.5, no Over 11.5`; `yes Detroit, yes Over 12.5`),
**KC-win** (`yes Arizona, yes Kansas City`, twice), player props (both yes/no),
unders. **Diversification and hedges already flow.** The coarse cap is NOT a
category blockade — it declines the *marginal* quote that tips a game over its cap.
My earlier "the cap blocks the diversifying flow" framing was **overstated**;
corrected here.

**4. Directional declines are DEEP, not marginal.** n=2,000: median `direction_cc`
= **1.76× cap** (min 1.34×, 90th pct 2.12×, 24% >2×), **0% within 1.2× of the cap.**
Loosening frees **deep mass-acceptance concentration** (all ~112 resting quotes
filling at once), not edge-case diversifiers.

**5. `skip_portfolio_cvar` decoded** (the monitor's spiking "other", up to 646/win):
the MC-based tail cap. It's the RICH cap that DOES credit diversification (a
diversifier lowers the tail → CVaR passes). The ~388 gap between directional (948)
and CVaR (560) ≈ the flow the coarse cap eats but the tail cap would allow —
i.e. CVaR is the real backstop, so a future cap fix wouldn't remove a safety layer.

**6. The real under-fill driver is WIN RATE, not caps.** 35,695 quote messages →
**15 fills → $276 ($18/fill).** To deploy $2,380 at $18/fill needs ~130 fills. The
`+1¢` markup added earlier this session (vs the winner's-curse probe) made us less
competitive — fill-prober walked +2.3¢ → +0.3–0.9¢, i.e. "less rich" and "fewer
fills" are the SAME lever pulled the same way. Markup trades fills ⇄ curse
directly, and is a far bigger dial on fills than the risk caps (which correctly
refuse deep concentration).

## Decision + action

Operator chose **prioritize fills** → **walk back the +1¢ markup.**

- **Config** (`config/prod-live-wc.local.yaml`, gitignored — NOT committed):
  MLB markup ladder reverted to the pre-+1¢ values —
  `markup_cc 200→100`; tiers `500→400 / 400→300 / 350→250 / 300→200`
  (mains 1¢; `<10¢→4¢ | 10–20¢→3¢ | 20–25¢→2.5¢ | 25–35¢→2¢`). Soccer untouched.
  Config-load verified (base 100, tiers `[(1000,400),(2000,300),(2500,250),(3500,200)]`).
- **Restart** (documented 2026-07-17 procedure): killed whole bot tree (wrapper→bot
  →supervisor+workers, monitors/prober left alive), `cancel-all` (72 quotes, 1
  already-gone 404), purged `heartbeat.txt`/`supervisor_heartbeat.txt` (no
  KILL/needs_reconcile existed), relaunched detached → `data/live_20260723_v4.log`.
  Verified: `quote_app_starting`, `pricing_aliases_active`, supervisor respawned as
  child, **quoting live, zero halt/KILL/preflight-fail.**
- **Walk-back confirmed live**: fill-prober first post-restart fill **+2.5¢ rich**
  (NO 68.1¢ vs ext 65.6¢) — up from +0.3–0.9¢, exactly the expected direction
  (lower markup → more aggressive bid → win richer, and should win *more*).
- **Blast radius**: pricing-only config value + a full bot cycle. No risk/
  settlement/monitoring logic touched.

## Deferred — option A (NOT built)

Cross-dimension directional credit: net the diverse resting book in the
mass-acceptance directional fold so a self-hedged book (overs+unders / both teams)
stops over-stating. Real but **modest/secondary** (diversification already largely
flows; declines are deep, not marginal). Must be prototyped off-line with the
mass-acceptance monotone-dominance proof preserved, parity-checked, then ported —
NOT a live hot-patch (validate-caps lesson). Only worth it if fills stay
constrained *after* the markup change proves out.

## NEXT STEPS

- **Me (now):** watch v4 fill rate + prober richness — did the walk-back lift fills?
  (owner: me; the read that decides whether option A is even needed.)
- **Me (~10p ET):** pull KC@DET settlement P&L — the +EV ruler on whether the rich
  fills were winner's curse or genuine edge. (owner: me.)
- **Operator:** the standing fills ⇄ curse trade — if +2.5¢ rich feels too rich for
  the fills gained, we trim markup back up; if fills still too few, option A or a
  deeper markup cut. (owner: operator, on v4 evidence.)
- **Parked:** 400 `invalid_parameters` on ~0.3% of sends; `_count_slate_games`
  over-count (`expected_games=47` multi-day; shadow-mode harmless). (owner: me,
  post-slate.)
