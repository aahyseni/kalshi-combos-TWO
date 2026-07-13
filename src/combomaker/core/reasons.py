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
    # Portfolio joint-tail cap (Phase 4 / M1 §5): the book's operative ES_0.99
    # (max of production-copula ES at the corr-high band, the correlation-inflated
    # challenger ES, and the exact all-hit deterministic stress) exceeds its
    # %-of-bankroll ceiling. Read off the LATEST full-MC BookRiskSnapshot (never
    # re-run in check); a stale/UNKNOWN snapshot fails closed. SHADOW in Phase 4.
    SKIP_PORTFOLIO_CVAR = "skip_portfolio_cvar"
    # The %-of-bankroll caps cannot be computed because the live bankroll reading
    # is unavailable/stale (BalanceTracker fails closed → None). UNKNOWN bankroll
    # ⇒ fail-closed (widen-or-no-quote), NEVER a convenient default. In shadow
    # mode this is log-only; enforced later it blocks for real.
    SKIP_BANKROLL_UNAVAILABLE = "skip_bankroll_unavailable"
    SKIP_HALTED = "skip_halted"
    SKIP_PRICING_FAILED = "skip_pricing_failed"
    SKIP_NEGATIVE_MARGINAL_EV = "skip_negative_marginal_ev"
    SKIP_SOURCES_DISAGREE = "skip_sources_disagree"
    SKIP_NO_FREE_MONEY_CHECK = "skip_no_free_money_check"
    SKIP_WS_UNHEALTHY = "skip_ws_unhealthy"
    # A classifier (leg relationship, settlement rules, market family) returned
    # UNKNOWN. UNKNOWN always means widen-or-no-quote, never a convenient default.
    SKIP_CLASSIFIER_UNKNOWN = "skip_classifier_unknown"
    # The combo is logically impossible (e.g. two YES legs of a mutually
    # exclusive event). v1 policy: no-quote, don't try to arb it.
    SKIP_LOGICALLY_IMPOSSIBLE = "skip_logically_impossible"
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
    DECLINE_RISK_LIMIT = "decline_risk_limit"
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
