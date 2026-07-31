# Derived open-quote capacity + diversity-aware eviction key (Task C)

**Date:** 2026-07-31 · **Scope:** quote_app / eviction path / config ONLY
(Task A owns limits/exposure/lifecycle risk internals) · **Default state:
everything OFF — byte-identical to today.**

Operator ruling (verbatim): *"we can bump the 200 up, but 5 $1 EV quotes
shouldn't lose to 1 $5 EV quote, especially if the 5 quotes are diverse."*

## What shipped (worktree, uncommitted until the fleet's C-commit)

```
                       quote path (unchanged)
  RFQ ──▶ price ──▶ LimitChecker.check ──▶ [SKIP_MAX_OPEN_QUOTES only?]
                                               │
                          ┌────────────────────┘
                          ▼
              _try_axis_eviction (slot axis)
                          │
        eviction_diversity_key: off ──▶ absolute-EV key (today)
                          │ shadow ──▶ log diversity verdict, absolute rules
                          │ on ──▶ MEASURABLE? ──no──▶ absolute-EV (fail closed)
                          │          │yes
                          ▼          ▼
              key(q) = dEV(q) × P(accept|size-bucket, measured CP)
                        − dES99(q)  [per-game CVaR share, same MC]
              candidate @ CP-LOWER, incumbent @ CP-UPPER
              (the confidence gap IS the churn hysteresis — derived)
```

| piece | where | state |
|---|---|---|
| `rfq/eviction_value.py` (NEW) — buckets, exact CP lower bound, acceptance tape, dES99 allocation, capacity derivation | pure module, no I/O | tested 32/32 |
| In-process acceptance tape (sent/accepted per premium-at-risk bucket) | `lifecycle` sent + accept hooks | always recorded, O(1), 214 ns/incr |
| Diversity slot key + separate anti-thrash ledger | `lifecycle._slot_diversity_decision` / `_try_axis_eviction` | `risk.eviction_diversity_key: off/shadow/on`, default **off** |
| Derived capacity = (observed tier write rate − kill reserve − measured flow) × TTL / (create+delete=4) | `quote_app._derived_capacity_tick`, ~60 s cadence | `risk.open_quote_capacity_derived: off/shadow/on`, default **off** |
| Counterfactual replay tool | `tools/diagnostics/eviction_diversity_replay.py` | run vs live store+tape, numbers below |
| Dangling `burst_floor_derived` kwarg in `build_lifecycle_config` (feature withdrawn by the parallel task; kwarg would crash boot) | `quote_app` | REMOVED (coordination fix) |

## Exchange-doc verification (Kalshi-docs-first rule)

Kalshi documents **no maker open-quote cap** — `create-quote` absence,
recorded in `docs/research/rfq_throughput/04-exchange-constraints.md`; the
only exchange-side constraint is the WRITE token bucket
(`docs/api-notes/limits-account.md`: CreateQuote/DeleteQuote = 2 tokens
each; tier ceiling OBSERVED live via `GET /account/limits`). The 200 was
ours alone (hand-bumped 20→60→120→200).

## Capacity derivation — proven to quote (2026-07-23 rule)

A standing quote must be re-posted every TTL (reprice or expiry-relight =
one delete + one create = 4 tokens, protocol facts), so the bucket bounds
the standing book:

```
capacity = (tier 300 t/s − withdraw/kill reserve 20 t/s − measured flow) × 20 s / 4
```

Measured on today's tape (store-side, per-minute sent windows):

| window | sent/min | flow tok/s | derived capacity |
|---|---|---|---|
| median | 384 | 25.6 | **1,272** |
| worst | 605 | 40.3 | **1,198** |

≈6× today's 200, non-zero at every window — the cap can actually quote.
Flow term double-counts the standing book's own refresh (reprices re-register
as sent) — strictly conservative. **Fail-closed arms:** first ~60 s after
boot (no rate window), unreadable tier, or headroom < 1 quote ⇒ keep today's
configured 200.

**Mass-acceptance guard (why capacity is throughput, not risk):** resting
quotes admit zero additional risk — every fill still passes the exact
confirm-path enforcement (reservations + candidate MC + waiver), the
fill-velocity governor and the mass-acceptance worst-case caps, which bind
on the ADMITTED book regardless of how many quotes rest. Measured worst
accept bursts ever: 25 accepts / $400 per 60 s (7/28–29). Failure direction
of a bigger standing book under a wave = RENEGES, never uncapped loss.

## Measured acceptance table (store-side since 7/26, CP at α=0.02)

| bucket | quoted | accepted | per 1k | CP lo/1k | CP hi/1k |
|---|---|---|---|---|---|
| <$5 | 64,920 | 87 | 1.34 | 1.06 | 1.67 |
| $5–15 | 169,092 | 140 | 0.83 | 0.69 | 0.98 |
| $15–50 | 11,396 | 53 | 4.65 | 3.44 | 6.15 |
| $50–150 | 3,966 | 44 | 11.09 | 7.95 | 15.05 |
| >$150 | 55 | 0 | 0.00 | 0.000 | 68.66 |

Table **discriminates** ($50–150 CP-lower 7.95/1k > $5–15 CP-upper 0.98/1k
— disjoint intervals, the derived criterion).

**HONEST FINDING (measure before you assert):** acceptance per quoted RFQ
**rises** with size — the dEV×P(accept) factor alone REINFORCES large
quotes. In today's eviction replay (6,626 `open_quote_evicted` events,
935 joined to store sizes; 857 small <$15 victims) the degraded
dEV×P(accept) key (dES99=0 offline) reverses only **35/935** evictions.
The operator's small diverse book is protected by the OTHER two mechanisms:

1. **derived capacity** — at ≥1,198 slots the slot cap simply stops binding
   at a ~200-quote book: **all 6,626 of today's evictions are
   counterfactually moot** (every one of the 7/26–28 mornings' 53–85 small
   resting quotes survives on capacity grounds alone);
2. **dES99** — when capacity genuinely binds, the concentration charge (the
   book-risk MC's additive per-game CVaR decomposition, hedge games
   NEGATIVE) is the discriminator that makes a diverse cheap quote
   near-unevictable and a fifth hot-game quote pay its concentration.

## Proofs

| proof | result |
|---|---|
| Shadow byte-identical | flags default off; "shadow" test asserts the absolute-EV outcome is unchanged and only logs/metrics appear; suite (see below) |
| Operator sentence as a test | `test_operator_sentence_small_diverse_book_holds` — 5×$1-EV diverse quotes HOLD against a $5-EV candidate under a measured tape; control test proves absolute-EV would have evicted |
| Fail-closed | thin/undiscriminating tape ⇒ absolute-EV key exactly (tested); capacity unmeasured ⇒ configured 200 (tested); degenerate CP inputs ⇒ 0.0 credit (tested) |
| CP lower bound exact | `P(X ≥ x; n, L) = α` to 1e-9 at n=1,000 and n=50,000; the textbook duality form UNDERFLOWS at large n and returns a zero-width bound (would-be anti-conservative lie) — found and avoided by direct bisection |
| Throughput | hot-path additions: 214 ns/quote (tape increment) + 520 ns (det arithmetic) on the SEND path (a ~40 ms REST call) ≈ 0.002%; eviction-path work bounded by the breach rate; capacity tick off-loop every 60 s. No pricing-path change at all with flags off |
| New tests | `tests/test_eviction_diversity_capacity.py` 32/32 |
| Full suite | 3,515 passed at the interleave re-run; the ONLY failures (5) are `tests/test_burst_floor_derived.py` — the parallel task's WITHDRAWN burst-floor feature's orphaned test file (its config/lifecycle/limits wiring was removed this session; the file references knobs that no longer exist). Zero failures touch this diff; the orphaned file is the withdrawal owner's cleanup debt |
| Vitals | fast tier **8/8 GREEN** (19.3 s) + pre-ship tier **1/1 GREEN** (8.6 s) |
| Decoupling | `eviction_value.py` inlines the exact binomial tail + CP-upper (sync-noted copies of `risk/burst_floor.py`) so the C diff commits independently of the withdrawn D feature |
| mypy/ruff | clean on all touched files (4 pre-existing `pricing/engine.py` mypy type-arg errors are at HEAD, untouched) |

## Blast radius

Flags off (committed default): the ONLY live-path changes are two O(1)
counter increments on the send/accept paths and a per-tick integer bump in
the maintenance loop. No pricing, no risk-cap, no confirm-path change.
Errors in the capacity tick log and keep current limits (fix isolation).

## NEXT STEPS

- **Operator:** arm order when ready — (1) `open_quote_capacity_derived:
  shadow` one session, read the `open_quote_capacity` lines against the
  enforced 200; (2) `shadow` → `on` (capacity swaps via `set_limits`,
  every other field preserved); (3) `eviction_diversity_key: shadow` while
  the in-process tape grows (it discriminates within a session at live
  volumes), then `on`. Independent levers; either can arm alone.
- **Task A interleave:** suite re-run at Task C end covers the shared-file
  interleave; the withdrawn burst-floor kwarg was removed from
  `build_lifecycle_config` here — Task A/D should confirm no other dangling
  reference on their side.
- **Follow-up (owed):** live dES99 read-out in shadow logs once the
  book-risk snapshot carries per-game tails on a busy slate; re-run the
  replay with capacity armed to measure the realized resting mix vs the
  7/29 baseline the operator wants back.
- **Decision owed by operator:** none for the ship (defaults off); arming
  order above when ready.
