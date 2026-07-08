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
    SKIP_TOO_MANY_LEGS = "skip_too_many_legs"
    SKIP_SIZE_BELOW_MIN = "skip_size_below_min"
    SKIP_SIZE_ABOVE_MAX = "skip_size_above_max"
    SKIP_LEG_UNKNOWN = "skip_leg_unknown"
    SKIP_LEG_STALE = "skip_leg_stale"
    SKIP_LEG_SPREAD_TOO_WIDE = "skip_leg_spread_too_wide"
    SKIP_LEG_BOOK_THIN = "skip_leg_book_thin"
    SKIP_EVENT_TOO_SOON = "skip_event_too_soon"
    SKIP_IN_PLAY = "skip_in_play"
    SKIP_EXCHANGE_INACTIVE = "skip_exchange_inactive"
    SKIP_RISK_HEADROOM = "skip_risk_headroom"
    SKIP_MASS_ACCEPTANCE_BREACH = "skip_mass_acceptance_breach"
    SKIP_MAX_OPEN_QUOTES = "skip_max_open_quotes"
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
