"""Machine-readable reason codes.

Every decision the system takes — quote, no-quote, reprice, delete, confirm,
decline, halt — carries exactly one of these. They are stable identifiers:
persisted, aggregated in the daily report, and used to tune parameters. Rename
nothing casually; add new codes freely.
"""

from __future__ import annotations

from enum import StrEnum


class ReasonCode(StrEnum):
    # --- RFQ filter (no-quote) ---
    SKIP_NOT_WHITELISTED = "skip_not_whitelisted"
    SKIP_SERIES_NOT_ALLOWED = "skip_series_not_allowed"
    SKIP_TOO_MANY_LEGS = "skip_too_many_legs"
    SKIP_SIZE_BELOW_MIN = "skip_size_below_min"
    SKIP_SIZE_ABOVE_MAX = "skip_size_above_max"
    SKIP_LEG_UNKNOWN = "skip_leg_unknown"
    SKIP_LEG_STALE = "skip_leg_stale"
    SKIP_LEG_SPREAD_TOO_WIDE = "skip_leg_spread_too_wide"
    SKIP_LEG_BOOK_THIN = "skip_leg_book_thin"
    SKIP_EVENT_TOO_SOON = "skip_event_too_soon"
    SKIP_IN_PLAY = "skip_in_play"
    # Pregame-only gate (Phase 3, operator directive 2026-07-10): a leg's game
    # has already STARTED per its schedule (verified ticker-embedded start, or
    # the conservative expiry-minus-offset estimate). Applies to ALL sports.
    SKIP_INPLAY_LEG = "skip_inplay_leg"
    # Pregame-only gate: no usable start-time source for a leg. UNKNOWN means
    # decline, never "probably pregame" (quiet-failure defense #2).
    SKIP_START_TIME_UNKNOWN = "skip_start_time_unknown"
    # Too-far horizon gate: a leg's game is beyond the per-prefix max horizon
    # (uninformed far-out book ⇒ adverse-selection risk). Per max_pregame_hours.
    SKIP_GAME_TOO_FAR = "skip_game_too_far"
    SKIP_EXCHANGE_INACTIVE = "skip_exchange_inactive"
    SKIP_RISK_HEADROOM = "skip_risk_headroom"
    SKIP_MASS_ACCEPTANCE_BREACH = "skip_mass_acceptance_breach"
    SKIP_MAX_OPEN_QUOTES = "skip_max_open_quotes"

    # --- R2 %-of-bankroll cap hierarchy (Phase 2; SHADOW by default) ---
    # Each of these binds a candidate/book aggregate against a threshold derived
    # AT CHECK TIME from the live risk bankroll:
    #   thr_cc = frac.numerator * bankroll_cc // frac.denominator (integer-exact).
    # They run in PARALLEL with the existing enforced hard-dollar caps; in shadow
    # mode they are emitted with Breach.shadow=True (log-only, zero quote impact).
    # Axes are strictly the LOSS axis (premium at risk) except the utilization
    # backstop, which is the ONLY new cap on the gross-settlement-notional axis
    # (R1/R2 invariant #2: the two money axes are NEVER summed).
    SKIP_GAME_LOSS_CAP = "skip_game_loss_cap"            # %-of-GAME correlated loss
    SKIP_PER_COMBO_LOSS_CAP = "skip_per_combo_loss_cap"  # single position max_loss
    SKIP_DIRECTIONAL_CAP = "skip_directional_cap"        # net one-directional theme
    SKIP_SLATE_CAP = "skip_slate_cap"                    # Σ game loss over a slate
    # Loose backstop ABOVE the % caps on the gross-settlement-notional axis
    # (multiple × bankroll). A stale bankroll fails closed (SKIP_BANKROLL_
    # UNAVAILABLE) instead — a stricter block than a loose multiple.
    SKIP_UTILIZATION_BACKSTOP = "skip_utilization_backstop"
    # Portfolio joint-tail cap (Phase 4 / M1 §5): the book's GOVERNING MODEL
    # ES_0.99 (max of production-copula ES at the corr-high band and the
    # correlation-inflated challenger ES — the worst SAMPLED CVaR) exceeds its
    # %-of-bankroll ceiling. P0-3: this is the SAMPLED tail ONLY; the deterministic
    # all-hit maximum is a SEPARATE axis (SKIP_PORTFOLIO_DET_MAX) so it can no
    # longer dominate and silence this gate. Read off the LATEST full-MC
    # BookRiskSnapshot (never re-run in check); a stale/UNKNOWN snapshot fails
    # closed. SHADOW in Phase 4.
    SKIP_PORTFOLIO_CVAR = "skip_portfolio_cvar"
    # Portfolio deterministic maximum-loss cap (P0-3): the exact all-hit
    # premium-at-risk (+ reserved holdings) — an unconditional upper bound the
    # sampled ES can never exceed — exceeds its %-of-bankroll ceiling. Gated
    # INDEPENDENTLY of the sampled-ES cap so the deterministic maximum is a hard
    # premium-at-risk backstop, not folded into the ES axis. Fails closed on a
    # stale/UNKNOWN snapshot via the shared ``usable`` guard.
    SKIP_PORTFOLIO_DET_MAX = "skip_portfolio_det_max"
    # A2: P(this settlement wave drops equity below the ruin floor) exceeds the
    # probability budget (structural-MC book-risk snapshot).
    SKIP_PORTFOLIO_RUIN = "skip_portfolio_ruin"
    # The %-of-bankroll caps cannot be computed because the live bankroll reading
    # is unavailable/stale (BalanceTracker fails closed → None). UNKNOWN bankroll
    # ⇒ fail-closed (widen-or-no-quote), NEVER a convenient default. In shadow
    # mode this is log-only; enforced later it blocks for real.
    SKIP_BANKROLL_UNAVAILABLE = "skip_bankroll_unavailable"
    # Widen-vs-DECLINE (Phase 5, R3 Part R2): near a per-game cap on
    # NORMAL/uncertain flow we DECLINE rather than post a wide quote — widening
    # a thin book near a limit only attracts hitters (our own P&L-sweep finding).
    # SHADOW by default (logged, zero live impact) until an operator enables the
    # policy; the widen-attracts-toxic-flow decision is graded on markouts, never
    # a P&L window. Distinct from the enforced delta/loss caps: this fires BELOW
    # the hard cap, in the low-headroom band where a quote would have to be wide.
    SKIP_WIDEN_AVOIDED = "skip_widen_avoided"
    SKIP_HALTED = "skip_halted"
    SKIP_PRICING_FAILED = "skip_pricing_failed"
    # The off-loop joint pricing exceeded its latency DEADLINE — we DELIBERATELY
    # dropped a combo too slow to price-and-POST inside its RFQ window (the
    # throughput/wedge guard). NOT a pricer failure; distinct so "pricing failed"
    # only ever means a genuine error.
    SKIP_PRICE_DEADLINE = "skip_price_deadline"
    # The RFQ's window closed before our quote POST landed — a normal taker-race
    # loss (we were not first to the taker), not an error.
    SKIP_RFQ_CLOSED = "skip_rfq_closed"
    SKIP_NEGATIVE_MARGINAL_EV = "skip_negative_marginal_ev"
    SKIP_SOURCES_DISAGREE = "skip_sources_disagree"
    SKIP_NO_FREE_MONEY_CHECK = "skip_no_free_money_check"
    SKIP_WS_UNHEALTHY = "skip_ws_unhealthy"
    # A classifier (leg relationship, settlement rules, market family) returned
    # UNKNOWN. UNKNOWN always means widen-or-no-quote, never a convenient default.
    # NOTE (2026-07-14): this MUST mean a genuine relationship-UNKNOWN only. The
    # three below used to share this code (no combo grid, unresolvable size,
    # malformed combo), inflating the "classifier unknown" tally with non-classifier
    # causes — now split out so the count is honest.
    SKIP_CLASSIFIER_UNKNOWN = "skip_classifier_unknown"
    # We have no PRICE GRID for the combo market (metadata not fetched / no grid on
    # an RFQ-generated multi-game market). Missing data ⇒ no-quote (rule 6); NOT a
    # classifier failure.
    SKIP_NO_COMBO_GRID = "skip_no_combo_grid"
    # The RFQ size could not be resolved (target-cost→contracts conversion). NOT a
    # classifier failure.
    SKIP_SIZE_UNRESOLVABLE = "skip_size_unresolvable"
    # The RFQ is not a well-formed combo (not a combo, or a leg side is unknown).
    SKIP_MALFORMED_COMBO = "skip_malformed_combo"
    # The combo is logically impossible (e.g. two YES legs of a mutually
    # exclusive event). v1 policy: no-quote, don't try to arb it.
    SKIP_LOGICALLY_IMPOSSIBLE = "skip_logically_impossible"
    # P0-5 exact exchange-quantity reconciliation: the exchange's authoritative
    # position (ticker/side/position_fp for the pinned subaccount) disagrees with
    # our reconstructed local book — a size mismatch, an opposite side, or a
    # holding with no local fill record (a manual/external trade). The exchange
    # ledger is ground truth (defense #3): we reserve the LARGER exposure (never a
    # convenient smaller default) and tag it with this code so the caps bind on the
    # conservative quantity and the mismatch is diagnosable, never silently
    # papered over by the local number.
    SKIP_RECONCILE_QUANTITY_MISMATCH = "skip_reconcile_quantity_mismatch"
    # An unmodeled market regime we deliberately decline (e.g. two-legged-tie
    # UCL/UEL/UECL knockouts, where "advance" != a single-match win and the
    # single-match soccer priors do not apply). Gated off until its own regime
    # is built, rather than mispriced on the wrong model.
    SKIP_UNMODELED_REGIME = "skip_unmodeled_regime"

    # --- Quote lifecycle ---
    QUOTE_SENT = "quote_sent"
    REPRICE_FAIR_MOVED = "reprice_fair_moved"
    DELETE_TTL_EXPIRED = "delete_ttl_expired"
    DELETE_LEG_MOVED = "delete_leg_moved"
    DELETE_LEG_STALE = "delete_leg_stale"
    DELETE_IN_PLAY_TRIGGER = "delete_in_play_trigger"
    DELETE_RISK_BREACH = "delete_risk_breach"
    DELETE_KILL_SWITCH = "delete_kill_switch"
    DELETE_WS_GAP = "delete_ws_gap"
    DELETE_RFQ_GONE = "delete_rfq_gone"

    # --- Last look (confirm decision) ---
    CONFIRM_OK = "confirm_ok"
    DECLINE_FAIR_MOVED_LEG = "decline_fair_moved_leg"
    DECLINE_FAIR_MOVED_JOINT = "decline_fair_moved_joint"
    DECLINE_LEG_STALE = "decline_leg_stale"
    DECLINE_WS_UNHEALTHY = "decline_ws_unhealthy"
    DECLINE_IN_PLAY = "decline_in_play"
    # Pregame-only gate re-checked at confirm time (straddle safety): the
    # leg's game started between quote and accept, or its start time became
    # unknowable — either way we never confirm a possibly-in-play fill.
    DECLINE_INPLAY_LEG = "decline_inplay_leg"
    DECLINE_START_TIME_UNKNOWN = "decline_start_time_unknown"
    DECLINE_VELOCITY_ANOMALY = "decline_velocity_anomaly"
    # Fill-velocity governor (wire-live 2026-07-13): committed notional or fill
    # COUNT in the rolling window exceeded the soft limit — DECLINE further
    # confirms + cancel-all resting quotes so a burst of accepts cannot run the
    # book away between balance polls. The COUNT limit still binds when the
    # bankroll is stale (fail-closed). Distinct from DECLINE_VELOCITY_ANOMALY
    # (a per-leg market-motion signal); this is our OWN acceptance rate.
    DECLINE_FILL_VELOCITY = "decline_fill_velocity"
    DECLINE_RISK_LIMIT = "decline_risk_limit"
    # P0-1 candidate-aware portfolio-risk gate (last look). AFTER the existing
    # analytic/gross/burst gates ADMIT a confirm, a candidate-aware ~20k-sample
    # portfolio MC scores the PRE (committed + reservations) and POST (+ this fill)
    # books on COMMON sampled states: confirm ONLY when the candidate's marginal EV
    # is positive, the POST joint-tail / ruin / deterministic / gross budgets pass,
    # and no fail-closed condition tripped. STRICTLY ADDITIVE — it can only DECLINE
    # a fill the other gates admit, never turn a decline into an admit. An UNKNOWN
    # merged marginal, an over-budget POST book, or ANY error in the off-loop eval
    # DECLINES here (fail-closed; an unmeasured/errored joint tail is never safe).
    DECLINE_CANDIDATE_RISK = "decline_candidate_risk"
    DECLINE_MASS_ACCEPTANCE = "decline_mass_acceptance"
    DECLINE_KILL_SWITCH = "decline_kill_switch"
    DECLINE_EXCHANGE_INACTIVE = "decline_exchange_inactive"
    # Accepted quantity or settlement convention unknowable at confirm time —
    # confirming a fill of unknown size/payout is never an option.
    DECLINE_SIZE_UNKNOWN = "decline_size_unknown"
    DECLINE_CONVENTION_UNKNOWN = "decline_convention_unknown"
    # An accept landed on a side we did NOT quote (bid = 0 = declined). For a
    # farmed impossible combo this is the hard guard that we can never be filled
    # long the worthless YES side. Deliberate lapse, never confirm.
    DECLINE_SIDE_NOT_QUOTED = "decline_side_not_quoted"

    # --- Kill switches / halts ---
    HALT_DAILY_LOSS = "halt_daily_loss"
    # R2 give-back / rate halts (Phase 2; SHADOW by default). Drawdown = give-back
    # from intraday peak equity; hard-trip = the deeper give-back that KILLs
    # (human-only clear); fill-velocity = committed notional per rolling window.
    HALT_DRAWDOWN = "halt_drawdown"
    HALT_HARD_TRIP = "halt_hard_trip"
    HALT_FILL_VELOCITY = "halt_fill_velocity"
    HALT_ERROR_RATE = "halt_error_rate"
    HALT_CONFIRM_TIMEOUTS = "halt_confirm_timeouts"
    HALT_EXCHANGE_STATUS = "halt_exchange_status"
    HALT_CLOCK_SKEW = "halt_clock_skew"
    HALT_WS_UNHEALTHY = "halt_ws_unhealthy"
    HALT_KILL_FILE = "halt_kill_file"
    HALT_MANUAL = "halt_manual"
    # Predicted vs exchange-ledger mismatch (fees, position signs, balance,
    # settlement values). The ledger is ground truth; a mismatch means our model
    # of the exchange is wrong somewhere — stop quoting, don't just log.
    HALT_RECONCILIATION_MISMATCH = "halt_reconciliation_mismatch"

    # --- Phase 6 circuit breakers (in-process detectors ⇒ KillSwitch.halt) ---
    # Each fires on a known failure SIGNATURE and is fail-closed: a detector
    # that cannot evaluate its input TRIPS (UNKNOWN is never safe). Grouped and
    # distinct so the halt stream is diagnosable (feedback_enumerate_buckets).
    # Market data STALE / sequence gap: the feed rx-age exceeded the threshold,
    # or a WS sequence gap left the book unusable — pricing off a stale book is
    # a courtsider gift; halt rather than quote on a possibly-wrong marginal.
    HALT_DATA_STALE = "halt_data_stale"
    # LATENCY spike: confirm / round-trip milliseconds over threshold — the
    # exchange (or our link) is slow enough that last-look freshness can't be
    # trusted and a stale accept could land.
    HALT_LATENCY_SPIKE = "halt_latency_spike"
    # 429 BURST: rate-limit responses over threshold in a rolling window — we
    # are being throttled hard enough that quotes/cancels may not land; stand
    # down so the reserved supervisor budget can still act.
    HALT_RATE_LIMIT_BURST = "halt_rate_limit_burst"
    # MARGINAL JUMP: a leg marginal moved more than the threshold between ticks
    # — a bad-data print or a real event (goal / injury / suspension). Either
    # way the book we priced against is no longer the book, so halt.
    HALT_MARGINAL_JUMP = "halt_marginal_jump"
    # Rule / metadata CHANGE: the taxonomy tripwire matched (a pinned
    # exchange-blocked impossible shape became constructible ⇒ the validator
    # changed) OR a market's settlement-relevant metadata changed under us
    # (close_time / rules_primary / settlement source). Our model of the market
    # is stale; halt before pricing on the wrong rules.
    HALT_METADATA_CHANGE = "halt_metadata_change"
    # UNMAPPED GAME KEY: a leg whose game_key can't be resolved to a real game
    # reached the risk path (it would key on its own whole-ticker singleton and
    # escape every game/slate cluster cap). UNKNOWN cluster membership is never
    # safe for a correlation book — halt rather than let it slip the caps.
    HALT_UNMAPPED_GAME = "halt_unmapped_game"
    # A circuit-breaker detector itself raised while evaluating. A breaker that
    # can't run is a breaker that can't protect — fail closed to a halt, never
    # swallow the error and continue quoting.
    HALT_BREAKER_ERROR = "halt_breaker_error"
    # The external safety supervisor tripped: a missed heartbeat (bot presumed
    # wedged) or an explicit external trigger. Written into the KILL file by the
    # supervisor so a revived bot halts immediately on restart.
    HALT_SUPERVISOR = "halt_supervisor"
    # Block-restart-until-reconciled: a `needs_reconcile` marker (left by a
    # prior hard halt / supervisor kill) is present at startup. The bot refuses
    # to quote until it reconciles its local book against the exchange and the
    # marker clears — a restarted bot must never resume quoting blind.
    HALT_NEEDS_RECONCILE = "halt_needs_reconcile"
