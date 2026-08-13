# 2026-08-13 — VARIANCE LEVERS BUILT DARK: P1 Stage-1 bounds + acceptance-tape boot seed (`7a742e5`)

**Operator ruling (same day):** "Build both now" — carving the two variance
levers out of the 8/31 freeze after the drawdown dissection (NO bleed; whale
seam 6th sighting; entry EV positive every measured day). Fallback snapshot
stands: tag `fallback-pre-variance-levers-20260813` + local yaml backup.

**Everything ships DARK (all defaults OFF = byte-identical).** Arming is ONE
operator restart after the pre-arm gates below.

## What was built

### Lever 1 — P1 Stage-1 accumulated bounds (`risk/limits.py` checks 3d + 4b)

| axis | what it bounds | key | waivable | arming value |
|---|---|---|---|---|
| **STRUCTURE** (`SKIP_STRUCTURE_LOSS_CAP`) | accumulated committed+reserved+candidate premium (`loss_by_combo_cc`, the existing fold) on ONE combo market | candidate structures only (7/26 bricking lesson); no lone-candidate guard — a whale alone must trip | **NO** (a same-structure re-hit is never a hedge) | `structure_loss_frac: "0.01"` — the ratified per-combo ANCHOR (cap_family.PER_COMBO_FRAC), never a new number; derived-caps join tracks the derived anchor |
| **GAME-DIRECTION NET** (`SKIP_GAME_DIRECTION_NET_CAP`) | NEW `directional_net_by_game_cc` census — the mutex-aware branch-max fold over committed+reserved+candidate ONLY (never resting quotes), judged against the SUNK committed baseline (the 8/1 constitutional: a book already over the line never blocks flow that doesn't worsen it) | candidate games only | **YES** (certified hedges always fill; game-keyed breach; evictable-on-fill) | derived (`book(directional_frac)` join) — arm ENABLED (shadow) first, read the distribution |

Both have the enabled/armed observer split (`structure_bound_shadow` /
`game_direction_net_shadow` log events — the readout the operator arms from).
The census is built only when the lever asks (`want_directional_net`, the
`want_loss_units` pattern — throughput never regresses).

What the whale evidence maps to: the 8/13 −$176.69 (271ct) and −$137.16
(282ct) tickets each carried max_loss ≈ 2.9–3.7% of bankroll — at the 1%
anchor (~$47 today) both REFUSE at the reservation path. NOTE the honest UX:
RFQ size is taker-set, so an armed structure bound refuses the WHOLE whale
ticket (no down-sizing mechanism exists) — decline reports will carry the
exact combo/size/legs per the standing rule.

### Lever 2 — acceptance-tape boot seed (the 8/1 empty-tape brick)

- Sizing arithmetic extracted to ONE implementation
  (`eviction_value.det_consumed_cc` / `risk_qty_from_terms`); the lifecycle
  delegates; parity pinned to the cent (incl. the audited 279.33ct example).
- `AcceptanceCounters.seed_counts` — additive (race-free vs intraday
  increments), validated, clamped.
- `ops/acceptance_seed.py` — reconstructs the last **24h** (the per-night
  anchor horizon; keeps every bucket ~30× inside the CP exact regime) of
  (quoted, accepted) per size bucket from the store's own decisions+rfqs
  tape. Sync stdlib sqlite3 on a second `mode=ro` connection (never the
  writer thread), id-BISECTED window (no `at` scan on 100M rows), 25k-id
  chunks (never pins the WAL). Unjoinable rows counted + skipped, never
  guessed. ANY failure ⇒ None ⇒ empty tape ⇒ today.
- `quote_app` boots it OFF-THREAD behind `risk.acceptance_seed_from_store`
  (default False = zero store reads, zero tasks; ~77s measured cold — boot
  never delayed; cancelled at shutdown).
- Live-shaped seed (the 8/13 24h probe: 238k sent / 82 matched accepts)
  flips `discriminating()` TRUE from boot with every bucket's CP-lower > 0 —
  pinned by test. Note the blast radius: the ALREADY-ARMED
  `eviction_diversity_key: "on"` becomes decisive from boot too (same tape,
  same measurement — intended, watch eviction metrics first hour).

## Proof chain (all at the pushed commit)

Suite **3,667/0** (24 new tests: 16 bound tests incl. sunk-baseline,
hedge-pass, waiver, off-state identity, monotonicity, resting-quote
exclusion; 8 seed tests incl. the store round trip through the REAL reader
and the discriminating-from-boot arming property); mypy clean; vitals fast
**8/8 GREEN** (55.1s); scratch self-containment at `7a742e5` (import + suite
3,667/0). Merged to main + pushed.

## ARMING (one restart — the pre-arm checklist)

Pre-arm gates (run in the restart window, bot stopped):
1. `tools/diagnostics/marginal_kill_gate_counterfactual.py` +
   `ruin_gate_marginal_counterfactual.py` re-run with the SEEDED tape
   (validate-caps-can-quote: projected admit fraction must beat the 8/1
   day-one 0.6%). *(--seed-from-store mode: small tool addition at the
   arming session.)*
2. P1 whale replay: re-price the 8/13/8/9/8/5 whale tickets under
   `structure_loss_frac=0.01` at the bankroll-at-fill — the −$177/−$137
   shapes must REFUSE while ordinary flow (median ticket $9.19, 5× under
   the $47 wall) is untouched.
3. Vitals pre-ship tier on the quiet machine (the standing inherited 5×-book
   RED is recorded, not this build's).

Then the yaml lines (gitignored live config — edit at the restart only):
```yaml
risk:
  kill_anchored_book_gate: true      # line ~689: false -> true
  # kill_gate_marginal / ruin_gate_marginal already true (armed 8/1, inert)
  acceptance_seed_from_store: true   # NEW line under ruin_gate_marginal
  portfolio_ruin_prob_budget: "0.05" # line ~702: "1.0" -> "0.05"
  portfolio_det_max_frac: "0.36"     # line ~975: "0.70" -> "0.36" (per its own comment)
  structure_loss_frac: "0.01"        # NEW: the ratified per-combo anchor
  structure_bound_armed: true        # NEW (enabled implied by armed path)
  game_direction_net_frac: "0.10"    # NEW: = the enforced directional_frac, shadow first
  game_direction_net_enabled: true   # NEW: SHADOW readout; arm after the distribution read
```
First-hour watch (merged from the 8/1 checklists): `acceptance_tape_seed_result`
log line with NON-ZERO buckets (seed failed + over-budget book + quote_sent=0
= the 8/1 brick — rollback); armed declines NAME the KILL/backstop anchors;
`det_max_backstop_cc` = 0.70×bankroll; quotes-per-min ≥ pre-restart band;
`structure_bound_shadow`/`game_direction_net_shadow` events sane; eviction
metrics not thrashing; any renege = abort. Rollback = config-only revert;
full fallback = the tag + yaml backup.

## Deferred/noted

- `det_cc` forward-recording into decision contexts (seed plan EDIT 9) —
  small forward-compat item, next build session.
- Direction-net ARMED value: read the shadow distribution first, then arm at
  the derived fraction (never a hand number).
- kill_anchor % / C1/C3/C5 remain open operator decisions (not consumed here).

## NEXT STEPS

- **Me:** arming session in the next quiet window (pre-arm gates 1–3 → yaml →
  restart → first-hour watch → arming readout report).
- **Operator:** the restart go (or tell me to arm at tonight's natural
  post-slate window); accept the whale-refusal UX noted above.
