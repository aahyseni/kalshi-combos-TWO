"""Tests for combomaker.risk.lastlook — the pure confirm/decline decision.

decide_confirm is a pure function over precomputed inputs: no I/O, no clock.
We pin (a) every decline branch, (b) the severity order of the checks,
(c) the exact boundary semantics (strict > everywhere), and (d) the
confirm ⟺ all-gates-pass equivalence as a hypothesis property.
"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from combomaker.core.reasons import ReasonCode
from combomaker.risk.lastlook import (
    LastLookDecision,
    LastLookInputs,
    LastLookPolicy,
    decide_confirm,
)

POLICY = LastLookPolicy()  # leg tol 150cc, joint tol 200cc, max age 2.0s

ALL_CLEAR = LastLookInputs(
    quote_time_fair_cc=5_000,
    current_fair_cc=5_000,
    max_leg_move_cc=0,
    max_leg_age_s=0.5,
    ws_healthy=True,
    seq_ok=True,
    any_leg_in_play=False,
    any_leg_started=False,
    leg_start_unknown=False,
    velocity_anomaly=False,
    exchange_active=True,
    killswitch_halted=False,
    risk_breaches=(),
)


# ---------------------------------------------------------------- happy path


def test_all_clear_confirms_with_confirm_ok() -> None:
    decision = decide_confirm(ALL_CLEAR, POLICY)
    assert decision == LastLookDecision(True, ReasonCode.CONFIRM_OK)
    assert decision.confirm is True
    assert decision.detail == ""


# ------------------------------------------------- each decline branch alone

DECLINE_CASES: list[tuple[dict[str, object], ReasonCode]] = [
    ({"killswitch_halted": True}, ReasonCode.DECLINE_KILL_SWITCH),
    ({"exchange_active": False}, ReasonCode.DECLINE_EXCHANGE_INACTIVE),
    ({"ws_healthy": False}, ReasonCode.DECLINE_WS_UNHEALTHY),
    ({"seq_ok": False}, ReasonCode.DECLINE_WS_UNHEALTHY),
    ({"any_leg_in_play": True}, ReasonCode.DECLINE_IN_PLAY),
    ({"any_leg_started": True}, ReasonCode.DECLINE_INPLAY_LEG),
    ({"leg_start_unknown": True}, ReasonCode.DECLINE_START_TIME_UNKNOWN),
    ({"velocity_anomaly": True}, ReasonCode.DECLINE_VELOCITY_ANOMALY),
    ({"max_leg_age_s": 2.0001}, ReasonCode.DECLINE_LEG_STALE),
    ({"max_leg_age_s": None}, ReasonCode.DECLINE_LEG_STALE),
    ({"max_leg_move_cc": 151}, ReasonCode.DECLINE_FAIR_MOVED_LEG),
    ({"max_leg_move_cc": None}, ReasonCode.DECLINE_FAIR_MOVED_LEG),
    ({"current_fair_cc": 5_000 + 201}, ReasonCode.DECLINE_FAIR_MOVED_JOINT),
    ({"current_fair_cc": 5_000 - 201}, ReasonCode.DECLINE_FAIR_MOVED_JOINT),
    ({"current_fair_cc": None}, ReasonCode.DECLINE_FAIR_MOVED_JOINT),
    ({"risk_breaches": ("max_position",)}, ReasonCode.DECLINE_RISK_LIMIT),
]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    DECLINE_CASES,
    ids=[f"{list(kw)[0]}->{reason.value}" for kw, reason in DECLINE_CASES],
)
def test_single_bad_field_declines_with_its_reason(
    overrides: dict[str, object], expected: ReasonCode
) -> None:
    inputs = replace(ALL_CLEAR, **overrides)  # type: ignore[arg-type]
    decision = decide_confirm(inputs, POLICY)
    assert decision.confirm is False
    assert decision.reason is expected


# --------------------------------------------------------- severity ordering

# Everything wrong at once — used to walk the severity ladder top to bottom.
ALL_BAD = LastLookInputs(
    quote_time_fair_cc=5_000,
    current_fair_cc=None,
    max_leg_move_cc=None,
    max_leg_age_s=None,
    ws_healthy=False,
    seq_ok=False,
    any_leg_in_play=True,
    any_leg_started=True,
    leg_start_unknown=True,
    velocity_anomaly=True,
    exchange_active=False,
    killswitch_halted=True,
    risk_breaches=("max_position",),
)

# The severity order pinned from the source (top = most severe): fixing each
# failure in turn must surface exactly the next reason down the ladder.
def test_severity_ladder_fix_one_reveal_next() -> None:
    inputs = ALL_BAD
    expected_reasons = [
        ReasonCode.DECLINE_KILL_SWITCH,
        ReasonCode.DECLINE_EXCHANGE_INACTIVE,
        ReasonCode.DECLINE_WS_UNHEALTHY,
        ReasonCode.DECLINE_IN_PLAY,
        ReasonCode.DECLINE_INPLAY_LEG,
        ReasonCode.DECLINE_START_TIME_UNKNOWN,
        ReasonCode.DECLINE_VELOCITY_ANOMALY,
        ReasonCode.DECLINE_LEG_STALE,
        ReasonCode.DECLINE_FAIR_MOVED_LEG,
        ReasonCode.DECLINE_FAIR_MOVED_JOINT,
        ReasonCode.DECLINE_RISK_LIMIT,
    ]
    fixes: list[dict[str, object]] = [
        {},
        {"killswitch_halted": False},
        {"exchange_active": True},
        {"ws_healthy": True, "seq_ok": True},
        {"any_leg_in_play": False},
        {"any_leg_started": False},
        {"leg_start_unknown": False},
        {"velocity_anomaly": False},
        {"max_leg_age_s": 0.5},
        {"max_leg_move_cc": 0},
        {"current_fair_cc": 5_000},
    ]
    for fix, expected in zip(fixes, expected_reasons, strict=True):
        inputs = replace(inputs, **fix)  # type: ignore[arg-type]
        decision = decide_confirm(inputs, POLICY)
        assert decision.confirm is False
        assert decision.reason is expected, f"after fixing {fix}"
    # Clearing the final gate (risk breaches) confirms.
    final = decide_confirm(replace(inputs, risk_breaches=()), POLICY)
    assert final == LastLookDecision(True, ReasonCode.CONFIRM_OK)


def test_killswitch_beats_stale_leg() -> None:
    inputs = replace(ALL_CLEAR, killswitch_halted=True, max_leg_age_s=None)
    assert decide_confirm(inputs, POLICY).reason is ReasonCode.DECLINE_KILL_SWITCH


def test_ws_unhealthy_beats_joint_moved() -> None:
    inputs = replace(ALL_CLEAR, ws_healthy=False, current_fair_cc=None)
    assert decide_confirm(inputs, POLICY).reason is ReasonCode.DECLINE_WS_UNHEALTHY


# --------------------------------------------------------- boundary semantics


def test_age_exactly_at_max_passes() -> None:
    inputs = replace(ALL_CLEAR, max_leg_age_s=POLICY.max_leg_age_s)  # == 2.0
    assert decide_confirm(inputs, POLICY).confirm is True


def test_leg_move_exactly_at_tolerance_passes() -> None:
    inputs = replace(ALL_CLEAR, max_leg_move_cc=POLICY.leg_move_tolerance_cc)  # == 150
    assert decide_confirm(inputs, POLICY).confirm is True


@pytest.mark.parametrize("direction", [+1, -1])
def test_joint_move_exactly_at_tolerance_passes(direction: int) -> None:
    inputs = replace(
        ALL_CLEAR,
        current_fair_cc=ALL_CLEAR.quote_time_fair_cc + direction * POLICY.joint_move_tolerance_cc,
    )
    assert decide_confirm(inputs, POLICY).confirm is True


# --------------------------------------------------------------------- purity


def test_same_inputs_same_output() -> None:
    for inputs in (ALL_CLEAR, ALL_BAD, replace(ALL_CLEAR, max_leg_age_s=None)):
        first = decide_confirm(inputs, POLICY)
        second = decide_confirm(inputs, POLICY)
        assert first == second


def test_perf_sanity_10k_calls_under_1ms_each() -> None:
    # Soft sanity: the docstring targets < 1 ms per decision; assert the
    # average over 10k calls stays under that (generous — real cost is ~µs).
    n = 10_000
    start = time.perf_counter()
    for _ in range(n):
        decide_confirm(ALL_CLEAR, POLICY)
    elapsed = time.perf_counter() - start
    assert elapsed / n < 0.001, f"{elapsed / n * 1e6:.1f}µs per call"


# ---------------------------------------------------------- hypothesis property


@settings(derandomize=True, max_examples=300)
@given(
    quote_fair=st.integers(min_value=0, max_value=10_000),
    current_fair=st.one_of(st.none(), st.integers(min_value=0, max_value=10_000)),
    leg_move=st.one_of(st.none(), st.integers(min_value=0, max_value=500)),
    leg_age=st.one_of(
        st.none(), st.floats(min_value=0.0, max_value=10.0, allow_nan=False)
    ),
    ws_healthy=st.booleans(),
    seq_ok=st.booleans(),
    in_play=st.booleans(),
    leg_started=st.booleans(),
    start_unknown=st.booleans(),
    velocity=st.booleans(),
    exchange_active=st.booleans(),
    killswitch=st.booleans(),
    breaches=st.lists(
        st.sampled_from(["max_position", "notional_cap", "family_cap"]), max_size=3
    ).map(tuple),
)
def test_confirm_iff_all_gates_pass(
    quote_fair: int,
    current_fair: int | None,
    leg_move: int | None,
    leg_age: float | None,
    ws_healthy: bool,
    seq_ok: bool,
    in_play: bool,
    leg_started: bool,
    start_unknown: bool,
    velocity: bool,
    exchange_active: bool,
    killswitch: bool,
    breaches: tuple[str, ...],
) -> None:
    inputs = LastLookInputs(
        quote_time_fair_cc=quote_fair,
        current_fair_cc=current_fair,
        max_leg_move_cc=leg_move,
        max_leg_age_s=leg_age,
        ws_healthy=ws_healthy,
        seq_ok=seq_ok,
        any_leg_in_play=in_play,
        any_leg_started=leg_started,
        leg_start_unknown=start_unknown,
        velocity_anomaly=velocity,
        exchange_active=exchange_active,
        killswitch_halted=killswitch,
        risk_breaches=breaches,
    )
    decision = decide_confirm(inputs, POLICY)

    # Independently re-derived gate conjunction (strict-> semantics: values
    # exactly at tolerance pass; None always fails closed).
    expected_confirm = (
        not killswitch
        and exchange_active
        and ws_healthy
        and seq_ok
        and not in_play
        and not leg_started
        and not start_unknown
        and not velocity
        and leg_age is not None
        and leg_age <= POLICY.max_leg_age_s
        and leg_move is not None
        and leg_move <= POLICY.leg_move_tolerance_cc
        and current_fair is not None
        and abs(current_fair - quote_fair) <= POLICY.joint_move_tolerance_cc
        and not breaches
    )
    assert decision.confirm is expected_confirm
    # CONFIRM_OK appears exactly when confirming; declines always carry a
    # decline reason (reason codes are data, never a convenient default).
    assert (decision.reason is ReasonCode.CONFIRM_OK) == decision.confirm
