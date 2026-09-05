"""SHADOW-TELEMETRY SAMPLING UNDER LOAD (2026-09-05 reader isolation, item 3).

The measured load (14:46 ET boot, 13 min, 3,316 sends): 105,970 of 123,461 log
lines — 85.8% — were the six measurement-only SHADOW read-outs below, 32 per
quote sent (``entity_tier_admission`` alone 15/send). Each is built, JSON-
rendered and flushed to disk ON THE EVENT LOOP: 22-31 µs per line through the
real pipeline (bench 2026-09-05: render 5.5-13.5 µs, structlog + flushed
``print`` the rest), i.e. ~0.7-1.0 ms of loop time per send, 136 lines/s at
that boot's rate. Small in steady state; under a burst it is paid exactly
when the loop is already behind, on the same drive the store writer is
saturating.

Mechanism, not knob: a structlog PROCESSOR that keeps every shadow line while
the loop keeps up and samples 1-in-N when the ``LoopLagProbe`` says it is
behind — N = ceil(lag / period), the measured ratio itself (a loop running
two probe periods late keeps every second line, five periods late every
fifth). Kept lines carry ``sampled_1_in=N`` so any tape reader can re-weight
counts; N changes are logged as ``shadow_telemetry_sampling``. Everything
outside ``SHADOW_EVENTS`` — every risk decision, ``risk_audit``, every
lifecycle event — passes through untouched at ANY lag: sampling is a
telemetry-only lever by construction (the set is an explicit registry of
the read-outs lifecycle.py itself labels "SHADOW READ-OUT ... Telemetry
only", and the suite pins that ``risk_audit`` is never in it).

Why sampling and not an off-loop writer: the writer thread saves the same
~25 µs/line at every load level but delays every line, including the last
ones before a death — the hang watchdog classifies corpses from the log's
tail, and the KILL/halt receipts are read beside it. Losing the final
buffered lines of a dying process is a forensic cost this repo has already
paid for once (the 45 h frozen-log outage); a sampler drops nothing when
the loop is healthy and never touches decision lines.
"""

from __future__ import annotations

import math
from collections.abc import Callable, MutableMapping
from typing import Any

import structlog

# structlog directly (not ops.logging.get_logger): ops.logging installs this
# module's SAMPLER into its processor chain, so importing it back would cycle.
log = structlog.get_logger(__name__)

# The measurement-only read-outs (rfq/lifecycle.py ``_log_*`` shadow helpers +
# the skew/widen shadow lines). Registry, not derivation: which events are
# shadow is a classification, exactly like ``mark_sheddable``.
SHADOW_EVENTS: frozenset[str] = frozenset(
    {
        "entity_tier_admission",
        "slate_partition_shadow",
        "structure_bound_shadow",
        "game_direction_net_shadow",
        "inventory_skew_shadow",
        "widen_vs_decline_shadow",
    }
)

# Decision lines that must NEVER be sampled — pinned by the suite against the
# registry above so a future edit cannot quietly move one across.
NEVER_SAMPLED: frozenset[str] = frozenset(
    {
        "risk_audit",
        "candidate_gate_ev",
        "candidate_gate_confirm",
        "quote_accepted",
        "confirm_failed",
        "cancel_all",
        "halt",
        "decline",
    }
)


class ShadowTelemetrySampler:
    """structlog processor. Bind a ``ratio_source`` (``LoopLagProbe.behind_ratio``)
    to arm it; unbound (tests, observe mode, tools) it is a pass-through."""

    def __init__(self) -> None:
        self._ratio_source: Callable[[], float] | None = None
        self._counters: dict[str, int] = {}
        self._last_n = 1
        self.dropped = 0

    def bind(self, ratio_source: Callable[[], float]) -> None:
        self._ratio_source = ratio_source

    def unbind(self) -> None:
        self._ratio_source = None
        self._counters.clear()
        self._last_n = 1

    @property
    def bound(self) -> bool:
        return self._ratio_source is not None

    def current_n(self) -> int:
        """1 while the loop keeps up (ratio ≤ 1); otherwise ceil(ratio)."""
        source = self._ratio_source
        if source is None:
            return 1
        try:
            ratio = float(source())
        except Exception:  # a broken probe must never break logging
            return 1
        if not math.isfinite(ratio) or ratio <= 1.0:
            return 1
        return math.ceil(ratio)

    def __call__(
        self, logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        event = event_dict.get("event")
        if event not in SHADOW_EVENTS or self._ratio_source is None:
            return event_dict
        n = self.current_n()
        if n != self._last_n:
            self._last_n = n
            # Re-entrant into structlog with a NON-shadow event: passes this
            # processor untouched, so no recursion.
            log.info("shadow_telemetry_sampling", sample_1_in=n)
        if n <= 1:
            return event_dict
        count = self._counters.get(event, 0) + 1
        if count >= n:
            self._counters[event] = 0
            event_dict["sampled_1_in"] = n
            return event_dict
        self._counters[event] = count
        self.dropped += 1
        raise structlog.DropEvent


# The process-wide instance ``configure_logging`` installs; QuoteApp binds the
# lag probe to it at boot.
SAMPLER = ShadowTelemetrySampler()
