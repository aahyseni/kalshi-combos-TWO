# 2026-07-17 — Throughput Batch-1 remainder LANDED (F2 liveness, F1 pre-gate, record-after-price fast-lane, F5) + relaunch

**Scope:** the Batch-1 remainder from the throughput synthesis
(`docs/research/rfq_throughput/00-SYNTHESIS-throughput-plan.md`), built via a
build → 3-lens adversarial verify → fix workflow (`wf_9d96072e`, 5 agents); all
four CONFIRMED verify findings fixed in-tree before commit. **Commit `15ebe40`
(pushed). Suite 2263/0 independently re-run; ruff 13 / mypy 6 = baseline.**
Restart activates it together with `max_open_quotes` 60→120 and the armed F1
gate (both operator-directed 2026-07-17).

## What landed

| Item | Mechanism | Live effect |
|---|---|---|
| **F2 mid-pipeline liveness** | `rfq_alive` probes at pre-price / post-price / pre-POST in `handle_rfq`; **positive-deletion-only** (`RfqIntake.rfq_alive` + two-generation stale store — a disconnect-cleared registry is UNKNOWN ⇒ alive); probe errors proceed-as-today | stops spending pool/risk/POST work on RFQs deleted mid-flight (~97% of POSTs hit dead RFQs today); `skip_rfq_deleted_midflight` + `rfq.liveness_skip.{pre_price,post_price,pre_post}` + `rfq.registry_reset` |
| **F1 monotone pre-pricing gate** | candidate-free `LimitChecker` check → shadow split → proven-monotone allowlist filter (`max_open_quotes`, `game_loss_cap`, `utilization_backstop`, `bankroll_unavailable`); cached per (book generation, bankroll, 0.5s TTL); identical reason codes, `stage=pre_pricing` | skips the pricing pool entirely for RFQs that would be declined post-pricing anyway — **48.2% of the 811k no-quotes on the 7/16–17 tape (UPPER bound; ~21% candidate-invariant floor)**; `pre_gate.{check,cache_hit,declined}` |
| **Record-after-price fast-lane** | `record_rfq` moved into a `finally` after the pipeline (exactly-once incl. raise/cancel); **`seen_at` keeps pickup-time semantics** (captured at worker entry, new optional param) | DB serialize+enqueue off the pre-POST path; latency instruments stay comparable — no window split needed |
| **F5 snapshot count** | `len(book.open_quotes)` replaces a full snapshot decomposition (value-identical by construction) | one of three per-RFQ decompositions removed from the loop thread |

**Rule-8 discipline (F1):** `tools/proto_pre_pricing_gate.py` fuzzed the gate
against the LIVE `LimitChecker` (never a reimplementation): 25k cases build +
30k more across two independent verifier re-runs → **0 monotonicity violations**
(gate fires ⇒ the full with-candidate check declines with the same reasons =
no false skip possible). Negative controls prove the exclusions load-bearing:
an opposite-side candidate CLEARS a mass-acceptance breach and a candidate leg
re-buckets a slate — so those reasons are provably NOT pre-declinable.
`skip_directional_cap` excluded on conservatism (documented monotone; add only
after its own fuzz pass).

## Verify findings (all fixed pre-commit)

1. **SERIOUS — `seen_at` semantics corruption:** the fast-lane moved the stamp
   from worker-pickup to post-pipeline (+0.1–2.5s), silently corrupting
   `latency_seen_to_sent` / `db_latency_anatomy` / lens-4 intake-lag — the
   instruments that gate the WS-sharding decision. Fixed: pickup time captured
   at entry and passed through; semantics unchanged, ships before the fast-lane
   ever runs live.
2. **MINOR — F2 disconnect false-deletion:** the intake clears its registry on
   every WS drop, and absence was read as deletion → live RFQs near a reconnect
   would be skipped as "deleted". Fixed: positive-deletion-only semantics
   (UNKNOWN ⇒ alive), two-generation stale store, `rfq.registry_reset` metric,
   7 new tests.
3. **MINOR/INFO — false build-report claims corrected:** F2 is ACTIVE in paper
   mode too (not inert — paper denominators shift on the next paper run); Part
   C's 48.2% is an upper bound (tape reasons are with-candidate).

## Re-basing notes for tape analyses (the relaunch is the watermark)

- `SKIP_RFQ_CLOSED` + `quote.rfq_closed_before_post` **cliff** — that cohort
  moves to `skip_rfq_deleted_midflight`/`pre_post` (continuity bridge:
  compare `rfq.liveness_skip.pre_post` against the old series).
- F2 site-1 runs before the filter → filter-reason tallies shrink.
- Armed-F1 pre-declined rows carry only the pre-breach reason subset (no
  candidate-dependent reasons, no risk-audit line — nothing was priced).
- Paper runs are liveness-gated from now on.

## Armed at the accompanying relaunch (operator-directed)

- `max_open_quotes: 60 → 120` (asked 100–200, stepped to 120; go 200 for the
  final if Saturday is clean — self-declined wins + waiver metrics decide).
- `pre_pricing_gate_enabled: true` (decline-only; `: false` reverts).

## NEXT STEPS

- **Me (now):** restart → live-verify (pre-gate/liveness metrics, 120 slots,
  champion quoting unchanged) → then build the QUEUED **quote-time
  resting-quote HAIRCUT** (operator 2026-07-17, `[[feedback_no_double_risk_layers]]`:
  resting quotes at 40% weight + 3-largest-at-100% floor in the QUOTE-TIME caps
  only; last-look untouched; event-driven post-fill pull; E2 property test
  rewritten to "no accept sequence can commit past the budget") — prototype →
  property tests → adversarial verify → restart 2 pre-Saturday as canary.
- **Operator:** none owed for this batch. Game-cap + 200-slot decisions after
  the 7/18–19 games; maker-fee residuals stand.
- **Later:** `skip_directional_cap` F1-allowlist fuzz pass; F5 second half
  (candidate-free decomposition cache); Batch-2 (WS sharding, event-driven
  reprice) after the latency instruments re-measure post-relaunch.
