"""candidate_gate_deadline_s YAML wiring (game-day knob, 2026-07-16).

The P0-2 candidate gate's confirm-window wall budget was previously only the
LifecycleConfig default (2.0s) — a dead knob from YAML. Covered here:

- default parity: RiskConfig default == LifecycleConfig default == the prior
  hardcoded 2.0, and build_lifecycle_config(RiskConfig()) is BIT-IDENTICAL to
  the pre-wiring LifecycleConfig (dataclass equality over every field);
- field validation: (0, 3] with NaN rejected (each budget must fit the
  exchange's 3s confirm window on its own);
- model validation: with the last-look MC waiver ENABLED the SUM of the waiver
  and gate budgets must fit the 3s window (both run inside the same confirm);
  with it disabled only the per-field bound applies;
- pass-through: the YAML value reaches the LifecycleConfig the lifecycle is
  built from (build_lifecycle_config — the ONE builder quote_app uses).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from combomaker.ops.config import RiskConfig
from combomaker.ops.quote_app import QUOTE_TTL_S, build_lifecycle_config
from combomaker.rfq.lifecycle import LifecycleConfig


def test_defaults_parity() -> None:
    assert RiskConfig().candidate_gate_deadline_s == 2.0
    assert LifecycleConfig().candidate_gate_deadline_s == 2.0


def test_default_lifecycle_config_bit_identical_to_prior_wiring() -> None:
    # The builder at RiskConfig defaults must equal EXACTLY what quote_app
    # constructed before the wiring (quote_ttl_s override, everything else the
    # dataclass defaults) — frozen-dataclass equality covers every field, so a
    # future knob silently changing a default fails here.
    assert build_lifecycle_config(RiskConfig()) == LifecycleConfig(
        quote_ttl_s=QUOTE_TTL_S
    )


@pytest.mark.parametrize("good", [0.5, 1.0, 2.0, 3.0])
def test_field_validator_accepts_confirm_window_budgets(good: float) -> None:
    assert RiskConfig(candidate_gate_deadline_s=good).candidate_gate_deadline_s == good


@pytest.mark.parametrize("bad", [0.0, -1.0, 3.5, float("nan")])
def test_field_validator_rejects_bad_budgets(bad: float) -> None:
    with pytest.raises(ValidationError):
        RiskConfig(candidate_gate_deadline_s=bad)


def test_joint_sum_rejected_when_waiver_enabled() -> None:
    # Each budget is individually valid, but together they cannot fit the 3s
    # confirm window while the waiver is armed.
    with pytest.raises(ValidationError, match="3s confirm window"):
        RiskConfig(
            lastlook_mc_waiver_enabled=True,
            lastlook_mc_waiver_deadline_s=1.5,
            candidate_gate_deadline_s=2.0,
        )


def test_joint_sum_at_exactly_three_seconds_ok() -> None:
    cfg = RiskConfig(
        lastlook_mc_waiver_enabled=True,
        lastlook_mc_waiver_deadline_s=1.0,
        candidate_gate_deadline_s=2.0,
    )
    assert cfg.lastlook_mc_waiver_deadline_s + cfg.candidate_gate_deadline_s == 3.0


def test_joint_sum_not_checked_when_waiver_disabled() -> None:
    # With the waiver OFF the gate runs alone: the same over-3s pair is fine.
    cfg = RiskConfig(
        lastlook_mc_waiver_enabled=False,
        lastlook_mc_waiver_deadline_s=1.5,
        candidate_gate_deadline_s=2.0,
    )
    assert cfg.candidate_gate_deadline_s == 2.0


def test_pass_through_reaches_lifecycle_config() -> None:
    cfg = build_lifecycle_config(RiskConfig(candidate_gate_deadline_s=1.25))
    assert cfg.candidate_gate_deadline_s == 1.25
    # And the neighbours still thread (no field swap in the builder).
    cfg2 = build_lifecycle_config(
        RiskConfig(
            candidate_gate_enabled=False,
            candidate_gate_deadline_s=0.75,
            lastlook_mc_waiver_enabled=True,
            lastlook_mc_waiver_deadline_s=0.5,
        )
    )
    assert cfg2.candidate_gate_enabled is False
    assert cfg2.candidate_gate_deadline_s == 0.75
    assert cfg2.lastlook_mc_waiver_enabled is True
    assert cfg2.lastlook_mc_waiver_deadline_s == 0.5
