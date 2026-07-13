"""Circuit breakers: in-process detectors that trip the kill switch on the
known failure signatures (RISK_BUILD_PLAN Phase 6).

Each breaker is a small PURE detector — ``(input) -> BreakerVerdict`` — with no
I/O and no clock of its own, so it is deterministic and testable with plain
values. The ``CircuitBreakers`` coordinator holds thresholds, runs the detectors
against inputs sampled from the live loops (feed rx-age, WS seq-gap flag, confirm
latency, 429 counts, leg marginals, tripwire, resolved game keys), and on the
FIRST trip calls ``killswitch.halt(reason, detail)``. It is wired into the
maintenance / status loops and the confirm path.

The whole doctrine is FAIL-CLOSED (CLAUDE.md hard rule 6 + quiet-failure defense
#2): a detector that cannot evaluate its input TRIPS. UNKNOWN is never safe.
Concretely:

- a missing / ``None`` feed rx-age ⇒ STALE (we can't prove the feed is fresh);
- a ``None`` latency sample where one was expected ⇒ SPIKE;
- an unresolvable game key ⇒ UNMAPPED (never keyed to its own singleton and
  waved through the caps);
- a detector that RAISES ⇒ the coordinator halts with ``HALT_BREAKER_ERROR``
  (a breaker that can't run can't protect).

Thresholds are the "fires AT the threshold, NOT just under" contract: the
comparison is ``>`` for age/latency/jump (strictly over) and ``>=`` for the 429
burst COUNT (at-or-over a count is a burst), matching the tests.

Money/prob note: marginals are probabilities (floats are fine in probability
space, hard rule 5); the jump threshold is a probability delta.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from combomaker.core.clock import Clock
from combomaker.core.reasons import ReasonCode
from combomaker.ops.logging import get_logger
from combomaker.risk.killswitch import KillSwitch

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BreakerVerdict:
    """A detector's decision. ``tripped`` with the reason + human detail, or the
    all-clear (``tripped=False``). A verdict is never ambiguous — a detector that
    cannot decide returns ``tripped=True`` (fail-closed), never a clear."""

    tripped: bool
    reason: ReasonCode | None = None
    detail: str = ""

    @classmethod
    def clear(cls) -> BreakerVerdict:
        return cls(tripped=False)

    @classmethod
    def trip(cls, reason: ReasonCode, detail: str) -> BreakerVerdict:
        return cls(tripped=True, reason=reason, detail=detail)


# --------------------------------------------------------------------------- #
# Pure detectors. Each takes only plain values and thresholds — no clock, no I/O.
# --------------------------------------------------------------------------- #


def detect_data_stale(
    rx_age_s: float | None, *, seq_gap: bool, max_rx_age_s: float
) -> BreakerVerdict:
    """Market-data staleness / sequence gap.

    Fail-closed: a ``None`` rx-age (feed never received, or age unknowable) is
    STALE — we cannot prove the book is fresh. A WS sequence gap flag trips
    regardless of age (a gap means the mirror is provably wrong until re-synced).
    """
    if seq_gap:
        return BreakerVerdict.trip(
            ReasonCode.HALT_DATA_STALE, "ws sequence gap — orderbook mirror unreliable"
        )
    if rx_age_s is None:
        return BreakerVerdict.trip(
            ReasonCode.HALT_DATA_STALE, "feed rx-age unknown — cannot prove freshness"
        )
    if rx_age_s > max_rx_age_s:
        return BreakerVerdict.trip(
            ReasonCode.HALT_DATA_STALE,
            f"feed rx-age {rx_age_s:.2f}s > {max_rx_age_s:.2f}s",
        )
    return BreakerVerdict.clear()


def detect_latency_spike(
    latency_ms: float | None, *, max_latency_ms: float
) -> BreakerVerdict:
    """Confirm / round-trip latency spike.

    ``None`` means NO round-trip has been measured yet (e.g. at startup, before
    the first confirm) — there is nothing to judge, so it CLEARS (a spike needs a
    measured sample). This mirrors the marginal-jump "no baseline yet" case: the
    breaker only fires on an ACTUAL over-threshold measurement, never on the
    absence of one. (The staleness breaker, not this one, catches a dead link.)
    """
    if latency_ms is None:
        return BreakerVerdict.clear()
    if latency_ms > max_latency_ms:
        return BreakerVerdict.trip(
            ReasonCode.HALT_LATENCY_SPIKE,
            f"round-trip {latency_ms:.0f}ms > {max_latency_ms:.0f}ms",
        )
    return BreakerVerdict.clear()


def detect_rate_limit_burst(count_in_window: int, *, max_in_window: int) -> BreakerVerdict:
    """429 burst: at-or-over ``max_in_window`` rate-limit responses in the rolling
    window is a burst. (``>=``: a COUNT reaching the limit IS the burst.)"""
    if count_in_window >= max_in_window:
        return BreakerVerdict.trip(
            ReasonCode.HALT_RATE_LIMIT_BURST,
            f"{count_in_window} 429s in window >= {max_in_window}",
        )
    return BreakerVerdict.clear()


def detect_marginal_jump(
    prev: float | None, cur: float | None, *, ticker: str, max_jump: float
) -> BreakerVerdict:
    """A leg marginal moving more than ``max_jump`` (probability) between ticks.

    Fail-closed: if we HAD a previous marginal and now can't read the current one
    (``cur is None``) that is itself a data failure and trips (the leg we priced
    against vanished). A first-ever reading (``prev is None``) is NOT a jump —
    there is no baseline to compare, so it clears and becomes the baseline.
    """
    if prev is None:
        return BreakerVerdict.clear()
    if cur is None:
        return BreakerVerdict.trip(
            ReasonCode.HALT_MARGINAL_JUMP,
            f"{ticker}: marginal became unreadable (had {prev:.3f})",
        )
    jump = abs(cur - prev)
    if jump > max_jump:
        return BreakerVerdict.trip(
            ReasonCode.HALT_MARGINAL_JUMP,
            f"{ticker}: marginal jumped {jump:.3f} ({prev:.3f}->{cur:.3f}) > {max_jump:.3f}",
        )
    return BreakerVerdict.clear()


def detect_unmapped_game(game_key: str | None, *, ticker: str) -> BreakerVerdict:
    """A leg whose game_key can't be resolved. ``None`` (unresolvable) trips: an
    unmapped leg would key on its own whole-ticker singleton and escape every
    game/slate cluster cap. UNKNOWN cluster membership is never safe."""
    if game_key is None or not game_key:
        return BreakerVerdict.trip(
            ReasonCode.HALT_UNMAPPED_GAME,
            f"{ticker}: game_key unresolvable — leg escapes cluster caps",
        )
    return BreakerVerdict.clear()


def detect_metadata_change(
    tripwire_hit: tuple[str, str] | None,
    changed_markets: Sequence[str] = (),
) -> BreakerVerdict:
    """Rule / market-metadata change.

    ``tripwire_hit`` is the ``(shape, detail)`` from ``pricing.tripwire`` (a
    pinned exchange-blocked impossible shape became constructible ⇒ the validator
    changed). ``changed_markets`` are markets whose settlement-relevant metadata
    (close_time / rules / settlement source) changed under us. Either trips —
    our model of the market is stale.
    """
    if tripwire_hit is not None:
        shape, detail = tripwire_hit
        return BreakerVerdict.trip(
            ReasonCode.HALT_METADATA_CHANGE,
            f"taxonomy tripwire {shape}: {detail}",
        )
    if changed_markets:
        return BreakerVerdict.trip(
            ReasonCode.HALT_METADATA_CHANGE,
            f"settlement-relevant metadata changed: {', '.join(changed_markets)}",
        )
    return BreakerVerdict.clear()


# --------------------------------------------------------------------------- #
# Rolling 429 counter (the one breaker that needs a small stateful window).
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class RateLimitWindow:
    """Counts 429 responses in a rolling wall-time window. The bot records each
    429 via ``record`` (from the REST error path); ``count`` prunes and returns
    the live count. Deterministic under a fake clock."""

    clock: Clock
    window_s: float
    _events: deque[float] = field(default_factory=deque)

    def record(self) -> None:
        self._events.append(self.clock.now().timestamp())

    def count(self) -> int:
        cutoff = self.clock.now().timestamp() - self.window_s
        while self._events and self._events[0] < cutoff:
            self._events.popleft()
        return len(self._events)


# --------------------------------------------------------------------------- #
# Coordinator: runs the detectors, trips the killswitch on the first trip.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BreakerThresholds:
    """All Phase-6 breaker thresholds in one place (config-sourced). Chosen
    conservative for the shadow/first-live posture; the report tables the values.
    """

    max_rx_age_s: float = 5.0
    max_latency_ms: float = 2_000.0
    rate_limit_window_s: float = 10.0
    max_rate_limit_in_window: int = 10
    max_marginal_jump: float = 0.25


@dataclass(frozen=True, slots=True)
class BreakerInputs:
    """A snapshot of everything the breakers evaluate on a tick. Sampled by the
    coordinator's caller (the maintenance/status loops); the breakers never do
    I/O themselves — they read this immutable snapshot."""

    rx_age_s: float | None
    seq_gap: bool = False
    latency_ms: float | None = None
    rate_limit_count: int = 0
    # ticker -> current P(YES); the coordinator compares against its own
    # last-seen map to detect jumps and unreadable-now legs.
    marginals: Mapping[str, float | None] = field(default_factory=dict)
    # ticker -> resolved game key (None = unresolvable). Only legs actually on
    # the risk path are here.
    game_keys: Mapping[str, str | None] = field(default_factory=dict)
    tripwire_hit: tuple[str, str] | None = None
    changed_markets: Sequence[str] = ()


HaltFn = Callable[[ReasonCode, str], object]  # matches KillSwitch.halt (awaitable)


class CircuitBreakers:
    """Runs the Phase-6 breakers and halts on the first trip.

    Stateless except for the per-leg last-marginal baseline (needed for the jump
    detector) — the coordinator owns that map so the pure detector stays pure.
    ``evaluate`` returns the first tripping verdict (or a clear) WITHOUT halting,
    so a shadow caller can log it; ``evaluate_and_halt`` additionally trips the
    kill switch, and is what the live loops call. Any exception inside a detector
    is caught and converted into a ``HALT_BREAKER_ERROR`` trip — a breaker that
    can't run must fail closed, never silently pass.
    """

    def __init__(self, killswitch: KillSwitch, thresholds: BreakerThresholds) -> None:
        self._killswitch = killswitch
        self._thr = thresholds
        self._last_marginal: dict[str, float] = {}

    def evaluate(self, inputs: BreakerInputs) -> BreakerVerdict:
        """Run all detectors; return the FIRST trip (or clear). Never halts.

        Ordered cheap → structural. A single exception anywhere is a breaker
        failure ⇒ fail-closed trip (HALT_BREAKER_ERROR)."""
        try:
            verdict = detect_data_stale(
                inputs.rx_age_s, seq_gap=inputs.seq_gap, max_rx_age_s=self._thr.max_rx_age_s
            )
            if verdict.tripped:
                return verdict

            verdict = detect_latency_spike(
                inputs.latency_ms, max_latency_ms=self._thr.max_latency_ms
            )
            if verdict.tripped:
                return verdict

            verdict = detect_rate_limit_burst(
                inputs.rate_limit_count, max_in_window=self._thr.max_rate_limit_in_window
            )
            if verdict.tripped:
                return verdict

            verdict = detect_metadata_change(inputs.tripwire_hit, inputs.changed_markets)
            if verdict.tripped:
                return verdict

            for ticker, game_key in inputs.game_keys.items():
                verdict = detect_unmapped_game(game_key, ticker=ticker)
                if verdict.tripped:
                    return verdict

            # Marginal jump last: it also UPDATES the baseline, so run it after
            # the others (a trip elsewhere shouldn't leave the baseline half-way).
            jump = self._evaluate_marginal_jumps(inputs.marginals)
            if jump.tripped:
                return jump

            return BreakerVerdict.clear()
        except Exception as exc:  # fail-closed: a breaker that raises still trips
            log.exception("circuit_breaker_evaluation_raised")
            return BreakerVerdict.trip(
                ReasonCode.HALT_BREAKER_ERROR, f"breaker evaluation raised: {exc!r}"
            )

    def _evaluate_marginal_jumps(
        self, marginals: Mapping[str, float | None]
    ) -> BreakerVerdict:
        tripped: BreakerVerdict | None = None
        for ticker, cur in marginals.items():
            prev = self._last_marginal.get(ticker)
            verdict = detect_marginal_jump(
                prev, cur, ticker=ticker, max_jump=self._thr.max_marginal_jump
            )
            if verdict.tripped and tripped is None:
                tripped = verdict
            # Update baseline to the readable current value (a None current keeps
            # the old baseline so a recovered feed compares against the last good
            # reading, not a phantom).
            if cur is not None:
                self._last_marginal[ticker] = cur
        return tripped or BreakerVerdict.clear()

    async def evaluate_and_halt(self, inputs: BreakerInputs) -> BreakerVerdict:
        """Evaluate; on a trip, halt the kill switch (idempotent). Returns the
        verdict so the caller can log/meter it. This is the WIRED call site the
        maintenance/status loops use."""
        verdict = self.evaluate(inputs)
        if verdict.tripped:
            assert verdict.reason is not None  # a trip always carries a reason
            log.error(
                "circuit_breaker_tripped", reason=str(verdict.reason), detail=verdict.detail
            )
            await self._killswitch.halt(verdict.reason, verdict.detail)
        return verdict
