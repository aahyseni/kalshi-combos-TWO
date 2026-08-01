# F5 (first-5-innings) launch: unknown-family legs quoted, one taker picked us off 3× — gate closed

**Date:** 2026-07-31 ~23:15Z (operator flagged it, going to sleep; executed under standing instruction)
**Scope / blast radius:** `config/prod-live-wc.local.yaml` `filters.allowed_leg_series_prefixes` ONLY. No code, no markup, no cap moved. One restart (19:04 ET boot).

## What happened

Kalshi launched first-5-innings series under the KXMLB umbrella. The allowlist's
bare `KXMLB` prefix admitted them the moment they appeared:

| series | legs in recent tape | classify_leg |
|---|---|---|
| KXMLBF5 | 668 | **unknown** |
| KXMLBF5TOTAL | 1,056 | **unknown** |
| KXMLBF5SPREAD | 213 | **unknown** |

No pair-rho entries exist for any of them. The UNKNOWN band widens, but no
width covers an F5-winner × same-game-spread correlation that is realistically
~0.7 — the parlay's true hit probability is far above what near-independent
pricing implies, so our NO bid systematically overpays.

**The operator noticed from the app before any monitor did:** "they added this
new 5 innings market… we don't have a correlation for that ticker I think? Yet
the bot is quoting it… I'm suspicious we're getting picked off."

## The pickoff, measured

One taker, one template — **F5 winner + same-team −3 spread + opposing-pitcher
Ks**, three different games in 18 minutes, each sized at the entity ceiling:

| time (UTC) | game | NO entry | premium |
|---|---|---|---|
| 22:24:50 | DET/ATH | 86.6¢ | $86.60 |
| 22:29:51 | BOS/LAD | 87.5¢ | $87.50 |
| 22:42:21 | WSH/ATL | 85.8¢ | $85.80 |
| 22:54:27 | (F5TOTAL in 6-series combo) | 76.6¢ | $15.53 |

**$275.43 of premium on a family with no correlation model.** These positions
are held to settlement (sell-only book; no unwind). Settlement watch owed.

## The fix (operator's designated mechanism, not a blocklist)

Bare `KXMLB` replaced with the 12 series enumerated from the tape that classify
to real families (GAME, TOTAL, SPREAD, KS, HIT, HR, HRR, RFI, TB, SB, OUTS,
RBI). Verified: none is a prefix of `KXMLBF5*`; `KXMLBHR` intentionally admits
`KXMLBHRR`. This is the allowlist's designed role — "doubles as per-sport kill
switch" — distinct from the retired risk blocklists.

Verified live post-restart (19:04 boot): **zero new F5 quotes**; held F5
positions still tracked in exposure.

## The structural lesson

A prefix allowlist admits every FUTURE series a venue launches under that
prefix, with whatever correlation model happens to exist — usually none. The
durable fix is a **quote-time family gate**: a leg whose `classify_leg` is
`unknown` (or whose same-game pairs have no measured/derived rho) must
widen-to-refusal on COMBOS, not just widen. That is a pricing-path change and
needs vitals + adversarial gating — queued, not shipped tonight.

## Context that night (for the record)

- Fills today: 24 / **$1,060.58 — best day ever** (prior best 7/28 $969.32),
  through two confirm-expiry halts, one hard freeze, and this pickoff.
- Book now at det-max $1,099.55 vs the $1,058.50 wall → quoting throttled until
  settlements or the ratified demotion arms (fleet gating in flight).

## NEXT STEPS

- **Runs next:** fleets gate → arm demotion + protective fixes → final restart
  (operator standing instruction, he is asleep).
- **Owner:** me — settlement watch on the three F5 combos ($259.90); measure
  realized loss vs our entry when they settle; that number decides how loud the
  markout lesson is.
- **Decisions owed by operator (morning):** (1) re-admit KXMLBF5* once the F5
  family is classified + carries measured rhos — build queued; (2) ratify the
  quote-time unknown-family refusal as a pricing-path change.
