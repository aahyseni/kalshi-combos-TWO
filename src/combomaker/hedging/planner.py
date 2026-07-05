"""Hedging scaffold — PHASE-GATED OFF until after Phase 7.

After a fill, per-leg deltas (risk/exposure.py, or the conditional-MC version
in sim/engine.py) can be laid off in the single-leg markets via V2 limit/IOC
orders when |delta| exceeds a threshold, accounting for the crossed spread and
fees on the hedge leg.

Strategic note (CLAUDE.md): hedging converts outright event risk into
correlation risk — the residual P&L IS the correlation position, which is the
actual book a combo maker runs. Activating this changes what business we're
in; it is a deliberate later decision, not a switch to flip.
"""

from __future__ import annotations

from dataclasses import dataclass

from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts


@dataclass(frozen=True, slots=True)
class HedgeOrder:
    """A planned (never yet sent) single-leg hedge."""

    market_ticker: str
    side: str                    # "yes"|"no" of the leg market
    contracts: CentiContracts
    limit_price_cc: CentiCents
    reason: str


class HedgePlanner:
    """Plans hedges from exposure deltas. Sending is NOT implemented — the
    executor arrives with its own phase, tests, and limits."""

    def __init__(self, *, delta_threshold_contracts: float = 50.0) -> None:
        self._threshold = delta_threshold_contracts

    def plan(self, delta_by_market: dict[str, float]) -> list[HedgeOrder]:
        raise NotImplementedError(
            "hedging is phase-gated off (see module docstring); "
            "activate only after the top-down maker is profitable over a real sample"
        )
