"""SHADOW-TELEMETRY SAMPLING UNDER LOAD (2026-09-05) — unit proofs.

1. Unbound (no lag probe): every line passes, nothing is annotated.
2. Bound and the loop keeps up (ratio <= 1): every shadow line passes.
3. Bound and behind (ratio 2.5 -> N = 3): exactly 1 in 3 shadow lines per
   event name is kept, annotated ``sampled_1_in=3``; the change is logged.
4. ``risk_audit`` and every other decision line are NEVER sampled — at any
   ratio — and the registry pins it (no overlap, no unknown shadow names).
5. ``configure_logging`` installs the sampler first in the chain.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog
import structlog.testing

from combomaker.ops import telemetry_sampling as ts
from combomaker.ops.telemetry_sampling import (
    NEVER_SAMPLED,
    SHADOW_EVENTS,
    ShadowTelemetrySampler,
)

LIFECYCLE = Path(__file__).resolve().parents[1] / "src" / "combomaker" / "rfq" / "lifecycle.py"


def _run(sampler: ShadowTelemetrySampler, event: str) -> dict[str, object] | None:
    try:
        return dict(sampler(None, "info", {"event": event, "x": 1}))
    except structlog.DropEvent:
        return None


def test_unbound_sampler_is_a_pass_through() -> None:
    s = ShadowTelemetrySampler()
    for _ in range(10):
        out = _run(s, "entity_tier_admission")
        assert out == {"event": "entity_tier_admission", "x": 1}
    assert s.dropped == 0
    assert s.current_n() == 1


def test_bound_but_keeping_up_samples_nothing() -> None:
    s = ShadowTelemetrySampler()
    s.bind(lambda: 0.4)
    assert s.current_n() == 1
    assert all(_run(s, "inventory_skew_shadow") is not None for _ in range(20))
    s.bind(lambda: 1.0)  # exactly one period late is NOT behind
    assert s.current_n() == 1
    assert s.dropped == 0


def test_behind_samples_one_in_ceil_ratio_and_annotates() -> None:
    s = ShadowTelemetrySampler()
    s.bind(lambda: 2.5)  # 2.5 probe periods late -> keep 1 in 3
    assert s.current_n() == 3
    with structlog.testing.capture_logs() as logs:
        kept = [_run(s, "entity_tier_admission") for _ in range(9)]
    assert [k is not None for k in kept] == [False, False, True] * 3
    assert all(k["sampled_1_in"] == 3 for k in kept if k is not None)
    assert s.dropped == 6
    # Counters are per event name: another shadow event has its own 1-in-3.
    kept2 = [_run(s, "slate_partition_shadow") for _ in range(3)]
    assert [k is not None for k in kept2] == [False, False, True]
    change = [e for e in logs if e["event"] == "shadow_telemetry_sampling"]
    assert len(change) == 1 and change[0]["sample_1_in"] == 3
    # Back to keeping up: the change is logged once more, everything passes.
    s.bind(lambda: 0.1)
    with structlog.testing.capture_logs() as logs:
        assert all(_run(s, "entity_tier_admission") is not None for _ in range(5))
    change = [e for e in logs if e["event"] == "shadow_telemetry_sampling"]
    assert len(change) == 1 and change[0]["sample_1_in"] == 1


def test_non_finite_or_broken_ratio_source_fails_open() -> None:
    s = ShadowTelemetrySampler()
    s.bind(lambda: float("inf"))
    assert s.current_n() == 1

    def broken() -> float:
        raise RuntimeError("probe gone")

    s.bind(broken)
    assert s.current_n() == 1
    assert _run(s, "entity_tier_admission") is not None


@pytest.mark.parametrize("ratio", [0.0, 1.0, 2.5, 50.0, 1e6])
def test_risk_audit_and_decisions_are_never_sampled(ratio: float) -> None:
    s = ShadowTelemetrySampler()
    s.bind(lambda: ratio)
    for event in sorted(NEVER_SAMPLED) + ["quote_sent", "rfq_seen", "ws_connected"]:
        for _ in range(7):
            out = _run(s, event)
            assert out == {"event": event, "x": 1}  # untouched: no annotation
    assert s.dropped == 0


def test_registry_pins_shadow_names_against_the_source() -> None:
    assert "risk_audit" not in SHADOW_EVENTS
    assert not (SHADOW_EVENTS & NEVER_SAMPLED)
    src = LIFECYCLE.read_text(encoding="utf-8")
    for name in SHADOW_EVENTS:
        # Every registered shadow event is a literal lifecycle.py telemetry
        # line; a renamed or removed read-out must update the registry.
        assert f'"{name}"' in src, name
    assert '"risk_audit"' in src


def test_configure_logging_installs_the_sampler_first() -> None:
    from combomaker.ops.logging import configure_logging

    try:
        configure_logging(json_output=True, level="INFO")
        processors = structlog.get_config()["processors"]
        assert processors[0] is ts.SAMPLER
        assert isinstance(ts.SAMPLER, ShadowTelemetrySampler)
    finally:
        structlog.reset_defaults()
