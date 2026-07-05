"""Last look: the confirm/decline decision as a PURE function.

Everything is precomputed by the caller from warm in-memory state — this
function does no I/O, no clock reads, no network. Target < 1 ms; the 3-second
HVM window is spent on the confirm round-trip, not on thinking.

Declines are data: every decision returns the single reason that killed it
(checks are ordered by severity) and the caller logs the full input set.
"""

from __future__ import annotations

from dataclasses import dataclass

from combomaker.core.reasons import ReasonCode


@dataclass(frozen=True, slots=True)
class LastLookPolicy:
    leg_move_tolerance_cc: int = 150      # any leg mid moving further ⇒ decline
    joint_move_tolerance_cc: int = 200    # recomputed fair vs quote-time fair
    max_leg_age_s: float = 2.0


@dataclass(frozen=True, slots=True)
class LastLookInputs:
    """Snapshot assembled at quote_accepted time, all in-memory."""

    quote_time_fair_cc: int
    current_fair_cc: int | None           # None ⇒ repricing failed ⇒ decline
    max_leg_move_cc: int | None           # worst |leg mid now − at quote|; None ⇒ unknown
    max_leg_age_s: float | None           # stalest leg book age; None ⇒ unknown
    ws_healthy: bool
    seq_ok: bool                          # no unresolved gaps
    any_leg_in_play: bool
    velocity_anomaly: bool
    exchange_active: bool
    killswitch_halted: bool
    risk_breaches: tuple[str, ...]        # from LimitChecker on the post-fill book


@dataclass(frozen=True, slots=True)
class LastLookDecision:
    confirm: bool
    reason: ReasonCode
    detail: str = ""


def decide_confirm(inputs: LastLookInputs, policy: LastLookPolicy) -> LastLookDecision:
    if inputs.killswitch_halted:
        return LastLookDecision(False, ReasonCode.DECLINE_KILL_SWITCH)
    if not inputs.exchange_active:
        return LastLookDecision(False, ReasonCode.DECLINE_EXCHANGE_INACTIVE)
    if not inputs.ws_healthy or not inputs.seq_ok:
        return LastLookDecision(
            False,
            ReasonCode.DECLINE_WS_UNHEALTHY,
            f"healthy={inputs.ws_healthy} seq={inputs.seq_ok}",
        )
    if inputs.any_leg_in_play:
        return LastLookDecision(False, ReasonCode.DECLINE_IN_PLAY)
    if inputs.velocity_anomaly:
        return LastLookDecision(False, ReasonCode.DECLINE_VELOCITY_ANOMALY)
    if inputs.max_leg_age_s is None or inputs.max_leg_age_s > policy.max_leg_age_s:
        return LastLookDecision(
            False, ReasonCode.DECLINE_LEG_STALE, f"stalest leg {inputs.max_leg_age_s}s"
        )
    if inputs.max_leg_move_cc is None or inputs.max_leg_move_cc > policy.leg_move_tolerance_cc:
        return LastLookDecision(
            False, ReasonCode.DECLINE_FAIR_MOVED_LEG, f"leg moved {inputs.max_leg_move_cc}cc"
        )
    if (
        inputs.current_fair_cc is None
        or abs(inputs.current_fair_cc - inputs.quote_time_fair_cc)
        > policy.joint_move_tolerance_cc
    ):
        moved = (
            None
            if inputs.current_fair_cc is None
            else inputs.current_fair_cc - inputs.quote_time_fair_cc
        )
        return LastLookDecision(
            False, ReasonCode.DECLINE_FAIR_MOVED_JOINT, f"joint moved {moved}cc"
        )
    if inputs.risk_breaches:
        return LastLookDecision(
            False, ReasonCode.DECLINE_RISK_LIMIT, "; ".join(inputs.risk_breaches[:3])
        )
    return LastLookDecision(True, ReasonCode.CONFIRM_OK)
