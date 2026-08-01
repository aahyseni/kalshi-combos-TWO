# 2026-08-01 — READING B ratified: kill-anchor flag re-landed clean + det-max demotion to the 0.70B ruin-anchor backstop (flag-off DARK)

## THE RATIFICATION (operator, 2026-08-01, verbatim): "Reading b"

Context he ratified against, stated to him three times with the numbers:
**READING B = the "ruin floor 30%" anchor means an EQUITY FLOOR (always keep
>= 30% of bankroll), so the model-free det-max backstop sits at
(1 - 0.30) x risk_bankroll = 0.70B (~$2,100 at today's ~$3,000 bankroll), NOT
the 30%-drawdown reading (0.30B).** Day-to-day the KILL gate governs:
P(night loss >= 12% of bankroll) <= 2% (`portfolio_kill_tail_prob` 0.02,
`hard_trip_frac` 0.12 — both existing ratified anchors). Caveat he accepted:
at cross-rho 0.25 the target 64-ticket book measures P(KILL) 20.8%; the KILL
gate + book diversity is the management mechanism; det-max@0.70B is the
model-free floor guaranteeing equity >= 30% under ANY copula.

**ON RECORD WITH IT:** Reading A (30% drawdown) is the convention enforced
elsewhere in the codebase (`cap_family.RUIN_FLOOR_FRAC = 0.30` as drawdown
distance; `book_risk` p_ruin at the 0.70 surviving-equity floor with the 5%
budget) and the operator explicitly chose B for the det-max backstop axis,
**knowing the collision**. The p_ruin MC gate (drawdown convention, 5%
budget) STAYS ARMED unchanged — it is the probabilistic guard on the
30%-drawdown event; Reading B repositions only the MODEL-FREE det-max axis.

## What landed (branch `readingb-detmax`, flag-off DARK)

| item | desc | status |
|------|------|--------|
| kill-anchor flag re-land | `7162a4d` was reverted from main (`d121e46`) because it bundled imports of the missing `rfq/eviction_value` (ships with the other fleet). Re-landed CLEAN: cherry-pick minus the eviction-only lifecycle hunks (prior fleet's split patch, reverse-applied) minus the two eviction config fields + validator entries. `grep eviction_value\|eviction_diversity\|open_quote_capacity_derived` over src = 0 hits | DONE |
| golden byte-identity | fixture regenerated digest `b136e16e76ce78da...` == the pre-change capture (20,000 limit cases + 2,000 candidate-gate cases); old test file 20/20 at the re-landed tree BEFORE the demotion | RE-PROVEN |
| det-max demotion | staged `apply_demotion.py` applied 16/16 edits (two import anchors re-pointed: the reanchor tree anchored them on the withdrawn burst-floor import, absent from main). `cap_family.det_max_backstop_frac()` = `1 - RUIN_FLOOR_FRAC` via exact Decimal→Fraction (`7/10`, never 0.2999…), `@cache`d (pure function of a module-constant anchor) | DONE |
| both-site guard | demotion applies ONLY with `kill_anchored_book_gate` AND `portfolio_tail_prob_gate` armed (quote-time cap in `risk/limits.py` AND confirm gate in `sim/book_risk.py`, `hard_trip_frac` threaded so the sites can never disagree); breach detail names the backstop wall actually tested | DONE |
| lifecycle readout | `det_max_backstop_cc` emitted on every snapshot so the operator sees the armed wall next to book premium before arming | DONE |
| canary UPDATE | the old canary `test_det_max_ceiling_is_unmoved_by_the_arming_flag` refused ANY re-landing. Updated (not deleted) for the ratification: `test_armed_ceiling_is_the_backstop_disarmed_is_todays_wall` accepts exactly the reading-B form and still goes red on an UNRATIFIED shape (revert the demotion, widen the DISARMED wall) + `test_demotion_never_applies_without_its_governor` (both sites) refuses an UNGUARDED landing. Why recorded in the test-file docstring with the verbatim ratification | DONE |
| capacity proof tool | `tools/diagnostics/demotion_capacity_proof.py` ported — source was lost with the reanchor cleanup; faithfully reconstructed from its compiled bytecode (`.cpython-313.pyc`, 2026-07-31 18:27), same functions/constants/format; runs clean read-only | PORTED |
| staged test fix | staged `_gate` helper passed `tail_prob_gate=True` hard, colliding with the governor-off override test (`TypeError`) — made it `setdefault` | FIXED |

## EXECUTE verdicts (all at the live measurement bankroll $2,940.28 unless noted)

| check | result |
|-------|--------|
| boundary books land on the anchors | n=46/$1,840 book, measured P(KILL) 1.1% / 1.9% ADMITTED; 2.3% / 4.9% / 10.4% REFUSED on the kill-tail axis — the flip is exactly (measured P(KILL) > 2% budget), det-max silent inside the backstop. PASS |
| ratified capacity case | 64-ticket spread book $1,983.64 premium, P(KILL) 0.6%: REFUSED disarmed (dollar wall $1,058.50), ADMITTED armed on BOTH sites ($2,058.19 backstop). PASS |
| backstop still enforces | n=160/$2,400 refused armed on det-max; 300-game "every pair diversifying" book refused — NO diversifier/certified-chain bypass across 0.70B (det-max is GLOBAL premium sum; certification is pairwise-local and never touches the det-max axis) |
| no governor, no demotion | armed flag + tail form OFF ⇒ band book ($1,500) still refused at today's 0.36 wall, both sites; armed + `hard_trip_frac=None` ⇒ today's verdicts, no crash, no free pass |
| staleness fail-closed | unusable snapshot refuses BOTH axes armed or disarmed; legacy no-envelope snapshot falls back to the ES form on `portfolio_cvar_frac` |
| shadow byte-identical | golden (captured at pre-change HEAD) reproduces at the FULL demoted tree — flag off = today exactly |
| confirm/cap coherence | same flag, same governor guard, same walls at both sites (`TestConfirmPathMovesWithTheCap`, 6 tests) |
| throughput | `LimitChecker.check` non-breaching hot path: main 35.09us, flag-off 35.17us (identical within noise — and flag-off is byte-identical by golden). Armed 40.5us (+15% on the per-RFQ admission check only, after caching the backstop derivation; the pricing/quoting path does not run this) — armed delta re-measured at the arming pre-ship |

## CAPACITY PROOF on the live book (read-only, boot equity 2026-08-01 12:00Z $2,808.37)

```
bankroll $2,808.37 | KILL line $337.00 | today's wall $1,011.01 (0.36) | backstop $1,965.86 (7/10)
LIVE BOOK: n=39 premium $1,117.74 P(KILL)=0.0020
    today: ['skip_portfolio_det_max'] | armed: ADMITTED
    headroom to next ticket: today $0.00 | armed $848.12 (if P(KILL) stays inside budget)
SYNTHETIC DIVERSIFIED (equal tickets, independent games; armed-stop P(KILL))
   n=8  50c  today $1,010.63  armed $449.09   ratio 0.44   (0.0050)
   n=20 50c  today $1,010.63  armed $673.98   ratio 0.67   (0.0070)
   n=46 50c  today $1,010.63  armed $1,107.31 ratio 1.10   (0.0140)
CHEAP-NO RE-EXAM (n=46): 25c ratio 0.70 | 40c 0.96 | 50c 1.10 | 70c 1.73 | 85c 1.95 | 92c 1.95
```

**Today's regime is refusing the LIVE book right now** (premium $1,117.74 >
the $1,011.01 dollar wall; measured P(KILL) 0.2% — a tenth of the budget) —
the exact wrong-direction refusal the ratification opens. **Small-book
honesty:** below n ~ 15 the KILL-line hit count is lattice-quantized (at n=8
one ticket flips P(KILL) across the whole 2% budget), so on tiny CONCENTRATED
books the armed gate binds TIGHTER than today's wall (n=8 ratio 0.44, n=20
0.67) — that is the gate working, and the capacity the ratification buys
arrives only with DIVERSE books (n=46 ratio 1.10, and up to 1.95x on high-P
cheap-premium shapes, always fenced by the 0.70B backstop).

## Verdicts (hard rule 9)

* full suite: **3,496 passed / 0 failed** (3 deselected) at the demoted tree
* `tests/test_kill_anchored_book_gate.py`: **24/24** (incl. in-test copula sweep, collision pin, golden)
* vitals fast tier: **8/8 GREEN (GATE PASS, 23.7s)** at this tree — V1 cap-scope
  (entity wall 0.03 x $2,050 = $61.51, disjoint admitted / touching refused),
  V2 quoting liveness over-wall + jammed-slot recovery, V3-V9 all inside bounds
* self-containment proof: fresh scratch worktree of main+branch — import `quote_app`, full suite, vitals fast (see commit message)

### Worktree-gate operational lessons (cost ~1h of red herrings today)

1. **A worktree gate run NEEDS the gitignored `config/prod-live-wc.local.yaml`
   copied in.** Without it `derive.live_config()` falls back to `prod.yaml`
   whose `entity_loss_frac` is empty ⇒ V1/V2 ERR on `Fraction('')`, and V2's
   mid-setup crash leaves the rig store open so the tempdir cleanup throws a
   misleading `WinError 32` — the visible error is downstream of the real one.
2. **The gate rescans the ENTIRE 30GB tape every run while the live bot
   writes** (`tape_facts` manifest includes the growing log ⇒ cache always
   invalid ⇒ ~20min derive under I/O contention). Fix used here, read-only and
   honest: snapshot dir `D:\kct-vdata` — hardlinks for the 69 closed logs +
   the store, a frozen COPY of the growing log — and `VITALS_DATA_DIR` at the
   snapshot: fresh scan 30.68GB once, then cache hits. Same mechanism the
   skew-fix report flagged as "structurally blocked while the live log grows";
   a durable fix (per-file manifest so closed logs never rescan) is owed to
   tools/vitals.

## Arming (DEFERRED — quiet-machine ritual, skew-fix precedent)

Ships DARK (`kill_anchored_book_gate` defaults False = byte-identical). The
arming stanza is STAGED as comments at the end of the gitignored
`config/prod-live-wc.local.yaml` with the ratification quote. To arm: quiet
machine, run `tools.vitals.gate --tier pre-ship` at the live tree, set
`risk.kill_anchored_book_gate: true` (governs only because
`portfolio_tail_prob_gate` is already true), restart at a pregame window,
watch the first `det_max_backstop_cc` readouts against the live premium.

## NEXT STEPS

* **operator**: arm decision — the flag is dark until the quiet-machine
  pre-ship ritual; the live book is ALREADY pinned by the disarmed wall
  (today headroom $0.00), so arming is the capacity unlock.
* **next session**: after arming, watch the first armed declines name the
  backstop / KILL line correctly on the tape; re-measure armed check
  throughput at pre-ship.
* **other fleet**: eviction/capacity branch rebases on this (whoever pushes
  second rebases + re-runs the self-containment proof).

---

## ADVERSARIAL GATE (2026-08-01, second fleet) — re-executed in a fresh scratch worktree at `cdfdcc8`: VERIFIED, exactly the ratified shape, nothing more

Every check by EXECUTION in an independent worktree (`kct-gate-rb`, not the build tree), interpreter = live venv, frozen-tape snapshot `D:\kct-vdata` for anything touching data. Live bot untouched throughout (GET/read-only; tape read = `tail` on the log copy stream only).

| gate item | how verified | result |
|---|---|---|
| (a) backstop DERIVED, never typed | `det_max_backstop_frac()` = `Fraction(1) - Fraction(Decimal(str(RUIN_FLOOR_FRAC)))` = exactly `7/10`, `@cache`d; grep: the only `0.70` literals near the axis are the PRE-EXISTING Reading-A surviving-equity defaults in `book_risk` (`ruin_floor_frac: float = 0.70`, untouched by the diff and pinned by the collision test). Ratification verbatim (incl. Reading-A collision + copula caveat + "operator chose B knowing it") present in `cap_family` comment, test-file header, `config.py`, the staged yaml stanza, and this report | PASS |
| (b) armed capacity re-produced | `demotion_capacity_proof.py` at boot equity $2,808.37: live book n=39, premium $1,117.74, P(KILL) 0.20% — today `skip_portfolio_det_max` (live tape tail corroborates: 860 det-max skips, 0 cvar), armed ADMITTED, headroom $848.12 to the $1,965.86 backstop; synthetic n=8/20/46 ratios 0.44/0.67/1.10; cheap-NO sweep 25c 0.70 … 92c 1.95 — all identical to the build's table | PASS |
| (b) boundary flip on the anchors | boundary probe re-run against gate-tree src: P(KILL) 1.1%/1.9% ADMITTED, 2.3%/4.9%/10.4% REFUSED — flip exactly at the ratified 2% budget, det-max silent inside the backstop | PASS |
| (c) wrong-model pricing | copula test RE-MEASURES (200k paths/rho, not pinned constants): rho 0 inside budget, rho 0.25 > 8× the 2% budget (≈20.8%), rho 1.0 → 29.5% 30%-drawdown event — matches the pinned sweep the operator ratified over | PASS |
| (c) p_ruin MC gate still armed | three-wiring probe (disarmed / armed-no-governor / fully armed): a p_ruin 6.01% > 5%-budget candidate REFUSED in all three; plus the depleted-equity case (equity 0.75B, loss crosses the 0.70B floor but not the KILL line) where fully-armed the RUIN axis itself fires (`post_ruin_prob_over_budget`) — drawdown convention, 5% budget, unchanged | PASS |
| (d) no bypass / fail-closed / coherence | n=160/$2,400 refused at BOTH sites; unusable-snapshot fail-closed parametrized armed+disarmed; quote/confirm same flag + same governor guard (half-wired states degrade only in the DECLINE direction, never the renege direction) — all in the 24/24 | PASS |
| (d) shadow byte-identity, NON-circular | golden EMITTED FRESH at pre-change main `022e47f` (which does not even have the flag) → digest `b136e16e76ce78da…` == fixture == HEAD flag-off regeneration (20,000 + 2,000 cases) | PASS |
| (d) throughput | `LimitChecker.check` best-round: HEAD flag-off 32.4µs == main 32.4µs (medians noise-dominated by the live bot; logical identity already proven by the golden); armed 36.7µs — admission check only | PASS |
| (d) canary refuses unratified landings | MUTATION-TESTED, 3 shapes: (1) demotion reverted → ratified-capacity test RED; (2) governor guard dropped → `test_demotion_never_applies_without_its_governor` RED; (3) DISARMED wall silently widened to the backstop → updated canary + golden BOTH RED. Tree restored clean after each | PASS |
| (e) self-containment | fresh scratch worktree at `origin/main` = `cdfdcc8`: `import quote_app` OK, FULL suite **3,496 passed / 0 failed** (3 deselected, 298s), vitals fast **8/8 GREEN** 21.9s (frozen snapshot + local yaml copied in). Capacity/eviction branch (`7e20dc2`) NOT landed on main → no merge to prove against; they still owe the rebase + re-proof when they push | PASS |
| (f) zero `eviction_value` in src | `git grep eviction_value -- src` = 0 hits (docs history mentions only) | PASS |

**Verdict: what shipped is exactly the ratification — Reading B on the det-max axis only, both sites, governor-guarded, dark by default, byte-identical off.** No findings. The residual risk remains the RATIFIED trade (comonotone collapse 2.33× the enforced ruin distance; copula-dependent KILL + p_ruin gates day-to-day; copula-free 0.70B floor), on record above.

### ONE-restart arming plan (staged, NOT executed — operator owns the go)

One pregame restart arms everything pending together (quiet-machine ritual, skew-fix precedent):

1. **Quiet machine**: no games in the next ~1h window, `tools.vitals.gate --tier pre-ship` at the LIVE tree must be GREEN first.
2. **Flags at that restart** (all currently dark): `risk.kill_anchored_book_gate: true` (stanza already staged commented at the end of the gitignored live yaml with the ratification quote; governs because `portfolio_tail_prob_gate` is already true) **+ the capacity/eviction fleet's flags iff their branch has landed and re-proven by then** (they rebase onto `cdfdcc8` and re-run their self-containment proof first — one restart, not two).
3. **First-hour metrics to watch on the tape**: (i) first armed declines must name the KILL line / `ruin-anchor backstop` in the breach detail (never the old `0.36 bankroll` string); (ii) `det_max_backstop_cc` + `p_kill_night` readouts on every snapshot vs live premium; (iii) armed `check` latency at pre-ship (~+15% budgeted, admission check only); (iv) quotes-per-min vs the pre-restart hour (throughput never regresses); (v) declines on tiny books (n<15) are EXPECTED to rise (lattice regime — the gate binding tighter than the dollar wall on concentrated books is the design, not a defect).
4. **Abort criteria** (any one → set the flag false, restart at the next quiet window, no other change): armed declines NOT naming the backstop/KILL anchors; P(book) or quotes-per-min degradation beyond the pre-restart hour's band; any breach string claiming a wall that wasn't tested; `det_max_backstop_cc` readout absent or ≠ 0.70×bankroll; any renege (confirm-time decline of a won auction on the det-max/tail axes).
5. **Stale-bankroll caveat carried** (pre-existing, not this change): the gate bankroll input reads the stale `daily_ruin_anchors` row — verify the boot-equity row is current at the arming restart.

NEXT STEPS: operator — arm decision per the plan above; eviction fleet — rebase onto `cdfdcc8` + re-prove; next session — watch the first armed hour per §3.
