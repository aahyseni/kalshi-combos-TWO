"""Fill-velocity governor: a rolling committed-notional + count window on our OWN
acceptances (wire-live 2026-07-13).

The config carried the knobs (``fill_velocity_window_s`` / ``_soft_frac`` /
``_hard_frac`` / ``_max_fills``) and the ``HALT_FILL_VELOCITY`` reason existed,
but NO code computed the velocity — a burst of accepts could commit an unbounded
slice of the book between the ~10s balance polls with nothing watching the RATE.

This governor closes that. Every ACCEPTED fill (where the lifecycle sets
``pending_fill``) records its committed notional (premium at risk = contracts x
bid, the LOSS axis — the capital a fill actually puts at risk) with the monotonic
time it landed. The governor then answers, over the trailing
``fill_velocity_window_s``:

  - ``committed_cc`` — Σ committed notional of the fills inside the window;
  - ``count`` — how many fills landed inside the window.

The lifecycle compares these to the % / count thresholds:

  - committed_cc > soft_frac * bankroll  OR  count > max_fills  ⇒ DECLINE further
    confirms (``DECLINE_FILL_VELOCITY``) + cancel-all resting quotes;
  - committed_cc > hard_frac * bankroll  ⇒ ``killswitch.halt(HALT_FILL_VELOCITY)``.

Fail-closed on a STALE bankroll (hard rule 6): when the bankroll denominator is
unavailable the %-of-bankroll notional thresholds cannot be computed, but the
COUNT limit is bankroll-free and STILL BINDS — a runaway acceptance rate is
capped even in the dark. The notional thresholds simply can't relax on a stale
poll (their branch is skipped, never defaulted to "fine").

Deterministic + clock-injected: the window is evaluated against an injected
monotonic clock, so tests advance time explicitly. Money is integer centi-cents.
Old events outside the window are pruned lazily on each record/evaluate, so the
buffer stays bounded to one window's worth of fills.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from combomaker.core.clock import Clock


@dataclass(frozen=True, slots=True)
class FillVelocityState:
    """The trailing-window view the lifecycle acts on.

    ``committed_cc`` is Σ committed notional (premium at risk) of fills inside the
    window; ``count`` is how many. Both are over exactly the last
    ``window_s`` seconds relative to the evaluation instant."""

    committed_cc: int
    count: int


class FillVelocityTracker:
    """A rolling committed-notional + fill-count window over our acceptances.

    ``record`` on every accepted fill; ``state`` reads the current window. Both
    prune events that have aged out of ``window_s`` (lazy, O(aged) amortized).
    """

    def __init__(self, clock: Clock, *, window_s: float) -> None:
        if window_s <= 0.0:
            raise ValueError(f"window_s must be > 0, got {window_s}")
        self._clock = clock
        self._window_ns = int(window_s * 1e9)
        # (monotonic_ns, committed_cc) in arrival order (monotonic ⇒ sorted).
        self._events: deque[tuple[int, int]] = deque()

    def record(self, committed_cc: int) -> None:
        """Record one accepted fill committing ``committed_cc`` (premium at risk,
        integer centi-cents) at the current monotonic instant. A non-positive
        commit is still recorded for the COUNT (a fill is a fill) but adds 0 to
        the notional sum."""
        now = self._clock.monotonic_ns()
        self._events.append((now, max(0, int(committed_cc))))
        self._prune(now)

    def state(self) -> FillVelocityState:
        """The committed notional + fill count inside the trailing window as of
        now. Prunes aged events first so the window is exact."""
        now = self._clock.monotonic_ns()
        self._prune(now)
        committed = sum(cc for _ts, cc in self._events)
        return FillVelocityState(committed_cc=committed, count=len(self._events))

    def _prune(self, now_ns: int) -> None:
        cutoff = now_ns - self._window_ns
        events = self._events
        while events and events[0][0] < cutoff:
            events.popleft()
