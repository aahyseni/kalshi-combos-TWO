# 2026-07-31 — Boot-warmup quote gate (Task B) + the tape's verdict on today's two reneges

## WRONG / FIXED / OPEN

| # | Item | State |
|---|------|-------|
| 1 | **WRONG (task premise, refuted by the tape):** "at boot+72s we sent quotes before the gate could possibly pass." Both reneged quotes were SENT under a **usable, generation-MATCHED** snapshot (gen 2 == 2, ages 5.2s / 8.5s, `fallback_reason=""` on their quote-time `risk_audit` lines at 10:11:04.747Z and 10:11:08.021Z). The renege cause is **mid-run generation staleness**, not boot warmup. | Documented below; boot gate built anyway (it closes a real, adjacent hole) |
| 2 | **FIXED (built):** boot-warmup quote gate — quote SENDING held at startup until the first evaluation on which the confirm path's own usability predicate could pass; one loud `quote_warmup_open` line with measured warmup duration; loud `quote_warmup_holding` warning throttled to the snapshot freshness window; dedicated `skip_warmup_book_risk` reason; one-way latch (mid-run behaviour untouched). | SHIPPED in tree; suite green; vitals 8/8 GREEN |
| 3 | **OPEN (the actual owner of today's 2 reneges):** arming `book_risk_stale_decay` (FIX 5, 2026-07-28 — currently SHADOW). Its shadow readout on both declines already carried the charged tail the confirm needed: `stale_decay_es_99_cc = 1,996,602cc = $199.66`, det the same — a level later confirms passed at ~$288 governing ES. Armed, both fills would have been evaluated (and by the tape's own levels, confirmed). | **Operator decision owed** |

## What happened today (0609 boot, `data/live_20260731_0609.log`)

Boot 10:09:40Z (06:09:40 ET), flat book (no `exposure_rehydrated` — nothing held).
`book_reconciled` 10:10:28.23; startup sync snapshot 10:10:28.28; first `quote_sent` 10:10:29.24.

| ts (Z) | event |
|--------|-------|
| 10:10:31.1 | accept #1 → confirm_ok (book was empty at check ⇒ predicate None ⇒ nothing to measure) |
| 10:10:48.5 | accept #2 → confirm_ok |
| 10:10:59.53 | snapshot gen 2 published |
| 10:11:04.7 / 10:11:08.0 | the two later-reneged quotes SENT — snapshot **usable, gen 2 == live 2** |
| 10:11:08.2 | accept #3 → confirm_ok → **fill bumps live generation 2 → 3** |
| 10:11:12.70 / .71 | accepts #4/#5 (target-cost $10 / $4) → reservation check reads gen-stale ⇒ unarmed decay ⇒ discard ⇒ "portfolio book-risk snapshot unusable … fails closed" ⇒ **both reneged** (`decline_risk_limit`) |
| 10:11:14.96 | snapshot gen 3 published — the stale window was **6.75s**; accepts 2.3s earlier than the cure |

The 0906 boot (6 rehydrated positions): startup snapshot published usable at 13:06:48.34,
first `quote_sent` 13:06:49.43 — zero quotes pre-verdict there too. **0 confirm declines in the 0906 run.**

## The build

`src/combomaker/rfq/lifecycle.py` — `QuoteLifecycle.quote_warmup_open()`:

```
              ┌─────────────────────────────────────────────────────┐
   RFQ ──────►│ handle_rfq: rfq_gone → [WARMUP GATE] → filter → …   │
              └───────────────┬─────────────────────────────────────┘
                              │ not open ⇒ skip_warmup_book_risk (pre-pricing,
                              │            durably recorded, metric quote.warmup_held)
              ┌───────────────▼─────────────────────────────────────┐
              │ open iff ANY of (evaluated lazily, LATCHED one-way): │
              │  • _book_risk_for_check() is None   (empty book)     │
              │  • …or .usable                      (measured tail)  │
              │  • caps_shadow_mode                 (confirm can't   │
              │                                      renege — parity)│
              │  • no bankroll layer                (limits.py       │
              │                                      do-not-brick)   │
              └─────────────────────────────────────────────────────┘
```

- **Predicate REUSED, not duplicated:** `_book_risk_for_check()` — the exact view every
  `limits.check(book_risk=…)` call site (quote, reserve, confirm, maintenance) consumes.
- **Enforcement parity** (no-double-risk-layers): the fail-closed portfolio breaches carry
  `shadow=caps_shadow_mode` and the confirm path drops shadow breaches; with no bankroll
  source the whole %-cap layer is inactive (`limits.py` "do-not-brick path"). The gate
  mirrors both — it can hold **only** where a confirm would actually renege. Both flags are
  read live off the checker/lifecycle, so a cap-mode change needs no second switch.
- **Everything else proceeds during warmup:** the hold sits inside `handle_rfq`;
  `_ensure_watched` (leg subscription + metadata) runs before it in quote_app, and
  settlement/feeds/balance loops are untouched.
- **Observability:** `quote_warmup_open` info line (measured `warmup_s`, book size, which
  stand-down applied); `quote_warmup_holding` warning throttled to `book_risk_stale_after_s`
  (30s — the system's existing staleness horizon, no new knob); also evaluated on the 0.5s
  `maintenance_tick`, so quoting opens (and the warning fires) with zero RFQ flow.
- **Nothing derived, nothing hand-set:** the open condition is the confirm predicate; the
  warn cadence is the existing freshness window; the reason code is new taxonomy, not policy.

Files: `rfq/lifecycle.py` (state + method + handle_rfq hold + maintenance hook),
`core/reasons.py` (`SKIP_WARMUP_BOOK_RISK`), `tests/test_quote_warmup_gate.py` (5 tests),
`tests/test_startup_first_snapshot.py` (2 assertions moved to the new reason — same
no-quote outcome, now pre-pricing with the boot window named).

## Proof (all in `tests/test_quote_warmup_gate.py`, plus suite/vitals)

| Obligation | Test | Result |
|---|---|---|
| Zero `quote_sent` before first usable snapshot | `test_boot_hold_zero_quotes_before_first_usable_snapshot` | GREEN |
| Opens automatically after | `test_quoting_opens_automatically_on_first_usable_snapshot` | GREEN |
| Never-usable ⇒ silent + loud periodic warning (throttled, re-fires past the window) | `test_never_usable_book_stays_silent_with_throttled_warning` | GREEN |
| Mid-run staleness UNCHANGED; latch never re-holds | `test_midrun_generation_staleness_behaviour_unchanged` | GREEN |
| Empty-book boot opens instantly | `test_empty_book_boot_opens_instantly` | GREEN |

Suite: **3,430 passed, 0 failed** (13 first-pass failures were all the gate being STRICTER
than confirm in shadow-cap / no-bankroll rigs — fixed by enforcement parity, not by test
edits; the only 2 test edits are the startup-snapshot boot-scenario reason codes).
Vitals fast tier: **8/8 GREEN** (first run was 7/8 — V7 RED from the same parity defect).

## Measured: warmup duration and cost, from today's tape

| Boot | Book at boot | First usable verdict | First quote_sent | Gate-measured warmup | Quotes forgone | Reneges avoided |
|------|--------------|----------------------|------------------|----------------------|----------------|-----------------|
| 0609 | empty | instant (predicate None) | 10:10:29.2 | ~0s | 0 | 0 |
| 0906 | 6 positions | 13:06:48.34 (startup sync snapshot) | 13:06:49.43 | ~2–4s (lifecycle init → publish) | 0 | 0 |

**Honest reading:** on today's realistic states the gate costs nothing and saves nothing —
the 2026-07-16 startup synchronous snapshot already lands a usable verdict before quote
processing when it succeeds. **Today's 2 reneges (≈$14 premium won-then-reneged) are out of
the gate's reach by its own spec** ("mid-run staleness unchanged"): they are the post-fill
generation-stale window (6.75s here; bounded by the ~15–17s MC publish cadence), the exact
branch FIX 5's decay was built for and measures in shadow on both decline lines. The gate's
real value is the boot where the startup snapshot FAILS or times out on a non-empty book
(the 2026-07-16 shape: 69 post-pricing `skip_portfolio_cvar` declines in ~40s — expected
warmup ≈ the first maintenance publish, order ~1min): those RFQs now hold pre-pricing
(no pool spend), under one named reason, with one loud open line — and the invariant
"zero quote_sent before a usable verdict" is structural, not incidental.

**Throughput:** latched-open cost = one bool read per RFQ (unmeasurable at ~550 quotes/min);
while holding it REMOVES pricing work. No change to markups, caps, skew, or any policy number.

## Blast radius

Quote-path only, boot-window only: pre-pricing hold in `handle_rfq` + a read-only probe on
`maintenance_tick`. No pricing/fair change, no cap fraction moved, no confirm-path change,
monitoring/P&L/settlement untouched. Live process untouched — lands on the operator's ONE
approved restart.

## NEXT STEPS

- **Operator:** decide on arming `book_risk_stale_decay` (FIX 5) — it is the mechanism that
  owns today's two reneges; its shadow numbers on the decline lines were exactly the charged
  view a confirm needed. (Separate, already-open decisions: resume posture, kill_anchor %,
  C1/C3/C5.)
- **Operator:** the ONE approved restart picks this up; watch for the single
  `quote_warmup_open` line (expect `warmup_s` ≈ 0–4s on a healthy boot) and any
  `quote_warmup_holding` warnings (should not appear unless the startup snapshot failed).
- **Next session:** after the restart, read the first boot's `quote_warmup_open` line and
  record measured `warmup_s` + `quote.warmup_held` count here (expected 0 on a healthy boot).
- **Me (done this session):** suite 3,430/0, vitals fast 8/8 GREEN, report + README row.
