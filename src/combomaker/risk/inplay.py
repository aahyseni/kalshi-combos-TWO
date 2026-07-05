"""In-play / courtside protection: market-based liveness triggers.

Adversaries at the venue are seconds ahead of any data feed — the only robust
defenses are width, size, and refusal. This detector supplies the refusal
trigger: sudden mid velocity or update-rate spikes on a leg mark it live for a
cooldown window, during which quotes are pulled and confirms declined.
Event-schedule liveness (close-time gates) lives in the filter layer; this is
the purely market-based backstop.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from combomaker.core.clock import Clock
from combomaker.core.money import CentiCents


@dataclass(frozen=True, slots=True)
class InPlayPolicy:
    velocity_window_s: float = 5.0
    velocity_threshold_cc: int = 300      # mid range within window ⇒ anomalous
    update_count_threshold: int = 25      # updates within window ⇒ anomalous
    cooldown_s: float = 30.0


@dataclass
class _MarketState:
    mids: deque[tuple[int, int]] = field(default_factory=deque)  # (mono_ns, mid_cc)
    anomalous_until_ns: int | None = None


class InPlayDetector:
    def __init__(self, clock: Clock, policy: InPlayPolicy | None = None) -> None:
        self._clock = clock
        self._policy = policy or InPlayPolicy()
        self._markets: dict[str, _MarketState] = {}

    def note_mid(self, market_ticker: str, mid_cc: CentiCents) -> None:
        """Feed every mid change (call from the orderbook delta path)."""
        now = self._clock.monotonic_ns()
        state = self._markets.setdefault(market_ticker, _MarketState())
        state.mids.append((now, int(mid_cc)))
        self._trim(state, now)
        mids = [m for _, m in state.mids]
        if (
            len(mids) >= 2
            and (max(mids) - min(mids)) > self._policy.velocity_threshold_cc
        ) or len(mids) > self._policy.update_count_threshold:
            state.anomalous_until_ns = now + int(self._policy.cooldown_s * 1e9)

    def velocity_anomaly(self, market_ticker: str) -> bool:
        state = self._markets.get(market_ticker)
        if state is None or state.anomalous_until_ns is None:
            return False
        return self._clock.monotonic_ns() < state.anomalous_until_ns

    def any_anomalous(self, market_tickers: list[str]) -> bool:
        return any(self.velocity_anomaly(t) for t in market_tickers)

    def _trim(self, state: _MarketState, now_ns: int) -> None:
        horizon = now_ns - int(self._policy.velocity_window_s * 1e9)
        while state.mids and state.mids[0][0] < horizon:
            state.mids.popleft()
