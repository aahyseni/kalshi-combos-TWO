# Steps 4-5 WIRED: same-player conditional containment + ml|spread family (+ the truncation catch)

**Date:** 2026-07-10 ~11:30 UTC · **Commits:** `3d1e499` (wiring) + `ab6cfd0`
(full-table restoration) · Verify agent: ALL 5 CHECKS PASS · Suite **1095/0**.

## What shipped

- **DO-2 (the [D]-regression fix):** same-game same-player batter pairs now
  route via `conditionals_mlb.py` — 33 EXACT arithmetic containments (all
  empirically verified == 1.0, pooled AND 2021-25) price as containment;
  109 measured conditional cells (n up to 588k) price exactly via
  P(A∧B) = P(A)·P(B|A); out-of-grid/buried-partial → UNKNOWN (fail-closed).
  P(HRR≥5|HR≥1)=0.5294 checkpoint reproduced.
- **DO-3:** ml|spread same-team = exact containment (cover⇒win, 0/98,980);
  opposite teams (both END-ANCHORED, never inferred from inequality) →
  IMPOSSIBLE **farmable=False** (MLB 48h rain-scalar breaks the airtight bar);
  unresolvable → plain 0.00 ± 0.95 (retires the documented-badly-wrong +0.6).
- **Differential (cached replay):** ONLY the two new families moved — 526
  same-player pairs → 8 containment / 66 conditional / rest fail-closed;
  843 ml|spread flat hits → 0. Gate still PASS (0.96¢ vs 2.21¢ legacy).

## The catch worth remembering

The workflow passed the conditional table to the wiring agent through a
4000-char prompt slice — **silently truncated to 60 of 142 cells**. The wiring
agent flagged it (fail-closed on the missing cells — correct), the verify agent
quantified it, and the orchestrator restored the full table from the
measurement artifact. Side-discovery from the restored reverse-direction
cells: **TB≥2 ⇒ HIT≥1 is exact** (total bases only come from hits), so some
pairs upgraded from conditional to pure containment. Lesson codified: pass
large data BY FILE PATH between agents, never inline-sliced.

## Config state: mlb table now 68 entries (43 + 22 oriented + 3 ml|spread)

## NEXT STEPS
- Rerun fleet (WC/MLB/mixed × fixed/lookahead + pre-registration scoring) in
  flight — the 100%-certainty test the operator demanded.
- Remaining queue: DO-5 rung keys · DO-6 basket width · DO-8 measurements ·
  mlb_runs grid calibration · buried-exact super-leg analogue · same-player
  same-family rung guard.
