"""Phase 6 prod go-live gates: the static prod guard (whitelist gate) + the
runtime preflight. Prod refuses to quote with any gate red; demo is unaffected."""

from __future__ import annotations

import dataclasses

import pytest

from combomaker.ops.config import (
    AppConfig,
    EndpointsConfig,
    Env,
    FiltersConfig,
    Mode,
    ProdGuardError,
    SafetyConfig,
)
from combomaker.ops.preflight import (
    PreflightConditions,
    evaluate_preflight,
)


def _config(
    env: Env,
    mode: Mode,
    *,
    confirm: bool,
    limits: bool,
    whitelist: list[str] | None = None,
    require_whitelist: bool = True,
) -> AppConfig:
    if whitelist is None:
        whitelist = ["KXWC", "KXMLB"]
    return AppConfig(
        env=env,
        mode=mode,
        endpoints=EndpointsConfig.for_env(env),
        safety=SafetyConfig(
            prod_limits_configured=limits,
            prod_require_series_whitelist=require_whitelist,
        ),
        filters=FiltersConfig(allowed_leg_series_prefixes=whitelist),
        confirm_live=confirm,
    )


# --------------------------------------------------------------------------- #
# Static prod guard — the whitelist go-live gate (extends the existing guard).
# --------------------------------------------------------------------------- #


def test_prod_quote_with_all_static_gates_green() -> None:
    _config(Env.PROD, Mode.QUOTE, confirm=True, limits=True).assert_safe_to_run()


def test_prod_quote_blocked_on_empty_leg_whitelist() -> None:
    with pytest.raises(ProdGuardError, match="allowed_leg_series_prefixes"):
        _config(
            Env.PROD, Mode.QUOTE, confirm=True, limits=True, whitelist=[]
        ).assert_safe_to_run()


def test_prod_quote_blocked_on_null_leg_whitelist() -> None:
    # A null allowlist DISABLES the per-leg gate — unsafe on real money, so the
    # prod whitelist gate must still refuse.
    cfg = _config(Env.PROD, Mode.QUOTE, confirm=True, limits=True)
    cfg = cfg.model_copy(
        update={"filters": FiltersConfig(allowed_leg_series_prefixes=None)}
    )
    with pytest.raises(ProdGuardError, match="allowed_leg_series_prefixes"):
        cfg.assert_safe_to_run()


def test_prod_whitelist_gate_can_be_disabled() -> None:
    # An operator can deliberately turn the leg-series gate off — the other
    # gates still bind (this only removes the whitelist requirement).
    _config(
        Env.PROD,
        Mode.QUOTE,
        confirm=True,
        limits=True,
        whitelist=[],
        require_whitelist=False,
    ).assert_safe_to_run()


def test_prod_still_blocks_without_confirm_or_limits() -> None:
    with pytest.raises(ProdGuardError, match="confirm-live"):
        _config(Env.PROD, Mode.QUOTE, confirm=False, limits=True).assert_safe_to_run()
    with pytest.raises(ProdGuardError, match="limits"):
        _config(Env.PROD, Mode.QUOTE, confirm=True, limits=False).assert_safe_to_run()


def test_demo_unaffected_by_go_live_gates() -> None:
    # Demo quote with NO whitelist, NO confirm, NO limits still runs — the gates
    # are prod-only (nothing to protect on demo).
    _config(
        Env.DEMO, Mode.QUOTE, confirm=False, limits=False, whitelist=[]
    ).assert_safe_to_run()


# --------------------------------------------------------------------------- #
# Runtime preflight — every live gate must be green before the first quote.
# --------------------------------------------------------------------------- #


def _all_green() -> PreflightConditions:
    return PreflightConditions(
        limits_configured=True,
        whitelist_non_empty=True,
        supervisor_heartbeat_established=True,
        external_kill_reachable=True,
        book_reconciled=True,
    )


def test_preflight_green_when_all_conditions_met() -> None:
    result = evaluate_preflight(_all_green(), require_supervisor=True)
    assert result.green is True
    assert result.red_gates == ()


def test_preflight_defaults_are_all_red() -> None:
    # FAIL-CLOSED: an unset PreflightConditions is entirely red.
    result = evaluate_preflight(PreflightConditions(), require_supervisor=True)
    assert result.green is False
    assert "limits_configured" in result.red_gates
    assert "whitelist_non_empty" in result.red_gates
    assert "supervisor_heartbeat_established" in result.red_gates
    assert "external_kill_reachable" in result.red_gates
    assert "book_reconciled" in result.red_gates


@pytest.mark.parametrize(
    "field",
    [
        "limits_configured",
        "whitelist_non_empty",
        "supervisor_heartbeat_established",
        "external_kill_reachable",
        "book_reconciled",
    ],
)
def test_preflight_any_single_red_gate_blocks(field: str) -> None:
    conditions = dataclasses.replace(_all_green(), **{field: False})
    result = evaluate_preflight(conditions, require_supervisor=True)
    assert result.green is False
    assert field in result.red_gates


def test_preflight_unreconciled_book_blocks() -> None:
    conditions = PreflightConditions(
        limits_configured=True,
        whitelist_non_empty=True,
        supervisor_heartbeat_established=True,
        external_kill_reachable=True,
        book_reconciled=False,  # block-restart-until-reconciled not satisfied
    )
    result = evaluate_preflight(conditions, require_supervisor=True)
    assert result.green is False
    assert result.red_gates == ("book_reconciled",)


def test_preflight_supervisor_optional_when_not_required() -> None:
    # With require_supervisor False, the two supervisor gates are not load-bearing
    # (the other three still are).
    conditions = PreflightConditions(
        limits_configured=True,
        whitelist_non_empty=True,
        supervisor_heartbeat_established=False,
        external_kill_reachable=False,
        book_reconciled=True,
    )
    result = evaluate_preflight(conditions, require_supervisor=False)
    assert result.green is True
