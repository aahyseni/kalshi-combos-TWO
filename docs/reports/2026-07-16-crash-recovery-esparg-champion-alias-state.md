# 2026-07-16 — PC hard-crash mid-session: state as recovered + the ESPARG champion-leg pricing-alias plan (half-wired)

**Scope:** the operator's PC crashed at **20:28:08 UTC** (heartbeat file mtimes; bot log
`live_20260716_no_menwc.log` and recorder `observe_20260716_restart3.log` both last wrote
20:28:08Z). This report reconstructs everything that happened after the last memory write
(~20:30Z stamp, actually ~19:55Z content) and freezes the in-flight work so any session can
resume it. Nothing was lost except in-conversation context: **all commits are pushed**, and the
in-flight edit survives on disk (2 uncommitted files).

## Timeline after the last recorded state

| When (UTC) | What |
|------------|------|
| 19:50:30 | **First live Problem-A waiver shot** (quote `b0d6696e`): a pure `skip_game_loss_cap` breach deferred correctly, attempted the waiver — and **timed out at the 1.0s deadline**. Measured cause: NOT the enumeration (87ms warm) but **BookRiskPool queue-wait** — 1 worker served the ~seconds maintenance snapshot MC + candidate gate + waiver (throughput research F10, lens 3). |
| 19:55:44 | **`ff250da` committed + pushed** — `fix(F10): BookRiskPool workers 1 → 2` (confirm-window calls get a free lane; correctness rests on P0-2 generation/version stamps, not worker exclusivity; 77 tests green on affected surfaces). |
| 19:56 | Relaunch on `ff250da` → `live_20260716_f10.log`. |
| ~19:00 (earlier) → 20:11 | **KXMENWORLDCUP live window.** Series had been added to the allowlist ~19:00Z. Live observation: a champion leg (`KXMENWORLDCUP-26-AR`) classifies `LegType.UNKNOWN` and `markup._leg_sport` tags it `'other'` → the MarkupPolicy fail-safe (mixed ⇒ other ⇒ 0) quoted the **whole combo at ZERO markup** — bare fair, no edge, on real flow. |
| 20:11:16 | Series **pulled from the allowlist** (armed yaml comment: "added ~19:00Z, REMOVED ~20:30Z" — effective at this relaunch) → relaunch as `live_20260716_no_menwc.log`. BookRiskPool `workers: 2` confirmed in startup log (F10 live). Last champion leg recorded 20:11:13Z. |
| 20:11 → 20:28 | Normal operation: binding cap `skip_game_loss_cap` on the pinned FRAENG/ESPARG book (by design), known `delete_quote_failed` 404 TTL-churn noise. No wedge, no kill. |
| 20:27:59 / 20:28:10 | The session was **wiring the pricing-alias fix**: `legtypes.py` written 20:27:59Z, `markup.py` written 20:28:10Z — **two seconds after the bot's last log line**. The PC died between edits. |

## The in-flight work: ESPARG champion-leg PRICING ALIAS (operator-directed)

**Problem.** Kalshi did **not** list a `KXWCADVANCE` series for the 7/19 ESP-ARG final
(tape-verified: zero `KXWCADVANCE-*ESPARG*` legs; advance exists only through the semis, e.g.
`KXWCADVANCE-26JUL15ENGARG-ARG`). The final's "who lifts the trophy" flow arrives instead on the
**championship series** `KXMENWORLDCUP-26-{AR,ES}` ("Argentina/Spain wins the World Cup"). At
finals time those are **settlement-identical to an advance leg on the final** (win incl. ET +
pens) — but our engine sees an unknown series: UNKNOWN leg type (flat fallback prior, no DC
netting against Messi/BTTS/corners legs on the same game) and `'other'` markup sport (zero
markup, observed live).

**Fix (the plan of record).** A config-driven **exact-ticker pricing alias**, applied ONLY in
the pricing-classification layer (`classify_leg` / `classify_sport` / `is_period_leg` /
structural parsing / markup sport-tagging):

```
KXMENWORLDCUP-26-AR  →  KXWCADVANCE-26JUL19ESPARG-ARG
KXMENWORLDCUP-26-ES  →  KXWCADVANCE-26JUL19ESPARG-ESP
```

The exchange-facing identity (order-book subscription, marginal source, quoting, settlement,
metadata, freshness) keeps the REAL ticker. The champion leg becomes STRUCTURAL — the
Dixon-Coles engine nets it exactly against every other final leg — and markup sees soccer.
Config: `PricingConfig.leg_pricing_aliases` (committed default `{}`), installed by
`PricingEngine.__init__`, validated so **only UNKNOWN-classifying tickers may be aliased** (an
alias can never override a modeled family).

**Source-of-truth verification (done this session, live-DB tape `mode=ro`):**
- Champion tickers, exhaustive: `KXMENWORLDCUP-26` (event), `-26-AR`, `-26-ES` — note **ES not
  ESP**; the champion series uses 2-letter codes while KXWC uses 3-letter. 113,943 rfq tape rows
  (re-quotes included) carry a champion leg; last seen 20:11:13Z (recording stopped when the
  series left the allowlist).
- Final game code: `26JUL19ESPARG` (e.g. `KXWCBTTS-26JUL19ESPARG`, `KXWCGOAL-26JUL19ESPARG`).
- Real advance format: `KXWCADVANCE-26JUL15ENGARG-ARG` ⇒ the synthetic targets above are
  format-correct.
- No real advance series for the final ⇒ the alias is genuinely needed (nothing to subscribe
  to instead).

## Wiring status (exact)

| Piece | Where | Status |
|-------|-------|--------|
| Alias registry `_PRICING_ALIASES` + `set_pricing_aliases()` + `resolve_pricing_alias()` | `pricing/legtypes.py:29-44` | ✅ on disk (uncommitted) |
| Applied in `is_period_leg` / `classify_leg` / `classify_sport` | `legtypes.py:174,179,246` | ✅ on disk |
| Applied in markup sport tagging | `markup.py:33` (`_leg_sport`) | ⚠️ **BROKEN — calls `resolve_pricing_alias` with NO import** (crash hit between edits) ⇒ NameError on every `_leg_sport` call; suite will fail; do NOT relaunch on this tree as-is |
| `PricingConfig.leg_pricing_aliases` + loader validation (only-UNKNOWN rule) | `ops/config.py` | ❌ not started |
| `PricingEngine.__init__` installs aliases | `pricing/engine.py` | ❌ not started |
| Same-game grouping | `relationships.py:485` — `classify_legs` groups on **`leg.event_ticker`** | ❌ champion leg's event is `KXMENWORLDCUP-26` ⇒ never joins the `26JUL19ESPARG` game block; needs alias-aware grouping or the whole point (DC netting) is lost |
| Structural adapter | `structural.py:314,355,369,542` parse **raw** `leg.market_ticker` | ❌ needs alias resolution (the "structural parsing" the design comment promises) |
| sgp team-code resolvers (`_advance_player_prior` etc.) | `sgp.py:266,287,389` | ❌ verify they see the aliased ticker (team code ARG/ESP comes from the synthetic suffix) |
| Tests | — | ❌ none |
| Armed yaml: alias entries + re-add `KXMENWORLDCUP` to `allowed_leg_series_prefixes` | `config/prod-live-wc.local.yaml:140` (currently `[KXWC]`) | ❌ arm LAST, after suite-green + review |

## Relaunch readiness

- **No processes running** (bot, supervisor, recorder all died with the OS).
- `KILL` and `data/needs_reconcile` **absent** (OS death, not a supervisor kill).
- `data/heartbeat.txt` + `data/supervisor_heartbeat.txt` **stale (20:28Z)** — purge before
  relaunch per the documented procedure.
- Tree is mid-edit BROKEN (markup import). Either finish the wire (preferred; small) or
  `git stash` the two pricing files to relaunch on `ff250da` clean.
- Branch `risk-audit-overnight` @ `ff250da`, in sync with origin.

## NEXT STEPS

- **Me (next session):** finish the alias wire (import fix → config key + only-UNKNOWN
  validation → engine install → relationships/structural/sgp alias-awareness → tests incl.
  a mutex check that aliased -AR/-ES legs land in the final's game block and net as the SAME
  advance ME event) → full suite → commit + push → THEN arm yaml (aliases + re-add
  KXMENWORLDCUP) → relaunch once, watch classification mix + markup on champion legs.
- **Owed (unchanged from memory):** agent adversarial re-verify of `2bfae72` + `1af2953` (+ now
  `ff250da` and the alias work) when API capacity returns — tonight's verify fleet 529'd again
  (3/3 agents); Throughput Batch-1 remainder (F1/F2/fast-lane/F5); 7/18-19 game-day waiver
  metrics → game-cap decision; llm-b ancestry check before any merge to main.
- **Operator:** none owed for the alias itself (plan already operator-directed); relaunch go
  after the wire lands.
