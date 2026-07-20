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
from collections.abc import Set as AbstractSet
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
    rx_age_s: float | None,
    *,
    seq_gap: bool,
    max_rx_age_s: float,
    feed_warm: bool = True,
) -> BreakerVerdict:
    """Market-data staleness / sequence gap.

    Fail-closed: a ``None`` rx-age (feed never received, or age unknowable) is
    STALE — we cannot prove the book is fresh. A WS sequence gap flag trips
    regardless of age (a gap means the mirror is provably wrong until re-synced).

    Cold-start exemption (``feed_warm=False``): during warmup — BEFORE the feed
    has produced its first frame — the feed is legitimately not-yet-fresh, and
    tripping here would self-halt the bot before it ever quotes. So while the
    feed is cold we do NOT judge staleness (there is nothing to prove yet). The
    latch is one-way: once warm, ``feed_warm`` is True forever, and a later
    disconnect (rx_age None) or seq gap still fails closed. ``feed_warm``
    defaults True so every existing caller keeps the strict fail-closed contract.
    """
    if not feed_warm:
        # Feed not established yet: no fresh state to judge, and no way for it to
        # be stale-relative-to-a-baseline. Warmup is the ONLY time None is not a
        # trip; the moment the first frame lands the latch flips permanently.
        return BreakerVerdict.clear()
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
    # Trailing window the latency-spike breaker samples over (see quote_app's
    # _sample_breaker_inputs): the breaker judges the worst round-trip in this
    # window, so a single historical slow confirm cannot latch it forever.
    latency_spike_window_s: float = 60.0
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
    # False only during feed warmup (before the first frame). While cold, the
    # data-staleness breaker does not judge (cold-start self-halt exemption).
    # Defaults True: every non-live caller keeps the strict fail-closed contract.
    feed_warm: bool = True
    # ticker -> current P(YES); the coordinator compares against its own
    # last-seen map to detect jumps and unreadable-now legs.
    marginals: Mapping[str, float | None] = field(default_factory=dict)
    # SETTLED-LEG EXEMPTION (2026-07-18 02:17Z live halt: halt_marginal_jump
    # "became unreadable (had 1.000)" on a settled FRAENG leg hard-killed the
    # bot 90s after preflight). Tickers whose market the EXCHANGE confirmed no
    # longer live (a graded settlement fact is cached, or the last status read
    # was closed/determined/disputed/amended/finalized — sampled from
    # ``QuoteLifecycle.settled_watch_exempt``). For these the jump/readability
    # watch is SKIPPED and their baseline purged: a book leaving the feed at
    # close is normal and permanent (not the dead-feed signature), and a
    # live→graded move (0.97 → 1.000) is a settlement, not a mis-mark. Legs
    # NOT in this set keep the full fail-closed watch, so a genuinely dead
    # feed on a live market still trips exactly as before. Default empty ⇒
    # every existing caller keeps the strict pre-fix contract.
    settled_tickers: frozenset[str] = frozenset()
    # IN-PLAY EXEMPTION (2026-07-19: 45 halt_marginal_jump trips through the WC
    # final — every one an in-play ESPARG book going dark mid-game, 8 hard
    # halts). Tickers whose GAME HAS STARTED per the SAME start-time ladder the
    # pregame gate stops quoting on (sampled from
    # ``QuoteLifecycle.inplay_watch_exempt``). An in-play book emptying or a
    # goal moving a marginal > max_jump is NORMAL in-play behaviour, not the
    # dead-feed/mis-mark signature — and by the polarity contract the exemption
    # begins only once quoting on the leg has ended (UNKNOWN start or operator
    # ``allow_inplay_legs`` ⇒ NOT in this set ⇒ full fail-closed watch). Legs
    # not in either exempt set keep the full watch; the whole-feed staleness
    # breaker is untouched. Default empty ⇒ every existing caller keeps the
    # strict pre-fix contract.
    inplay_tickers: frozenset[str] = frozenset()
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

    # Transient/recoverable failure signatures get a GRACE window: the book is NOT
    # hard-killed on a sub-window blip (a WS reconnect, a latency spike aging out
    # of its window, a 429 burst subsiding, a one-off marginal move). During the
    # hold the EXISTING safeguards keep us safe — feed.on_invalidate cancels
    # resting quotes, the WS force-reconnects, and the pricer declines on stale
    # legs — so we hold + re-evaluate; only a condition that stays bad PAST the
    # window escalates to the hard kill. Structural/genuine reasons (metadata/rule
    # change, an unmappable game key that escapes the caps, a breaker that itself
    # raised) are NOT here and hard-halt immediately. Windows are sized in multiples
    # of the 15s status-loop cadence so a reconnect gets ≥1 full tick to heal.
    # (2026-07-13 live: a transient WS reconnect hard-killed the whole book 0.8s in
    # — this is the fix.)
    _GRACE_S: dict[ReasonCode, float] = {
        ReasonCode.HALT_DATA_STALE: 30.0,        # WS reconnect (force_reconnect on drop)
        ReasonCode.HALT_LATENCY_SPIKE: 90.0,     # a spike ages out of the 60s window
        ReasonCode.HALT_RATE_LIMIT_BURST: 30.0,  # 429s subside as the window rolls
        ReasonCode.HALT_MARGINAL_JUMP: 30.0,     # one-off move re-baselines; halt on churn
    }

    # Consecutive fully-clear status ticks required before a transient grace timer
    # is forgiven. A SINGLE clear tick must NOT reset the timer, or a condition
    # that FLAPS (bad/clear/bad/clear around a threshold) would evade escalation
    # forever while effectively broken (review finding, 2026-07-13).
    _RECOVERY_CLEARS = 2

    def __init__(
        self, killswitch: KillSwitch, thresholds: BreakerThresholds, clock: Clock
    ) -> None:
        self._killswitch = killswitch
        self._thr = thresholds
        self._clock = clock
        self._last_marginal: dict[str, float] = {}
        # per-reason first-bad MONOTONIC-ns timestamp (a wall-clock step must never
        # move a safety escalation) + the recovery streak for flap resistance.
        self._bad_since: dict[ReasonCode, int] = {}
        self._clear_streak: int = 0

    def evaluate(self, inputs: BreakerInputs) -> BreakerVerdict:
        """Run all detectors; return the FIRST trip (or clear). Never halts.

        Ordered cheap → structural. A single exception anywhere is a breaker
        failure ⇒ fail-closed trip (HALT_BREAKER_ERROR)."""
        try:
            verdict = detect_data_stale(
                inputs.rx_age_s,
                seq_gap=inputs.seq_gap,
                max_rx_age_s=self._thr.max_rx_age_s,
                feed_warm=inputs.feed_warm,
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
            jump = self._evaluate_marginal_jumps(
                inputs.marginals, inputs.settled_tickers, inputs.inplay_tickers
            )
            if jump.tripped:
                return jump

            return BreakerVerdict.clear()
        except Exception as exc:  # fail-closed: a breaker that raises still trips
            log.exception("circuit_breaker_evaluation_raised")
            return BreakerVerdict.trip(
                ReasonCode.HALT_BREAKER_ERROR, f"breaker evaluation raised: {exc!r}"
            )

    def _evaluate_marginal_jumps(
        self,
        marginals: Mapping[str, float | None],
        settled_tickers: AbstractSet[str] = frozenset(),
        inplay_tickers: AbstractSet[str] = frozenset(),
    ) -> BreakerVerdict:
        tripped: BreakerVerdict | None = None
        for ticker, cur in marginals.items():
            if ticker in settled_tickers or ticker in inplay_tickers:
                # SETTLED-LEG EXEMPTION (2026-07-18 02:17Z live halt): the
                # exchange confirmed this market is no longer live, so there
                # is nothing live to watch — its book leaving the feed is the
                # normal permanent close transition (not a dead feed), a held
                # graded fact is not a move, and the transition INTO the fact
                # (e.g. 0.97 → 1.000 at grading) is a settlement, not a jump.
                # IN-PLAY EXEMPTION (2026-07-19, 45 trips through the final):
                # the leg's game has started and quoting on it has ended — an
                # in-play book going dark or gapping on a goal is normal, not
                # the dead-feed signature. Either way PURGE the baseline so no
                # stale reading can ever fire later; a book that RETURNS
                # readable simply re-baselines (prev None ⇒ clear).
                self._last_marginal.pop(ticker, None)
                continue
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
        """Evaluate; react. A TRANSIENT trip (see ``_GRACE_S``) is HELD for a grace
        window — the book is not hard-killed on a blip (a WS reconnect etc.); the
        existing cancel-all-on-invalidate + WS force-reconnect + pricer leg-freshness
        keep us safe during the hold, and only a condition that stays bad PAST the
        window escalates to the hard kill. A STRUCTURAL/genuine trip hard-halts
        immediately (unchanged). A clear tick resets every grace timer (recovered).
        Returns the verdict so the caller can log/meter it. WIRED call site (status
        loop)."""
        verdict = self.evaluate(inputs)
        now_ns = self._clock.monotonic_ns()
        if not verdict.tripped:
            if self._bad_since:  # holding a timer — forgive only after sustained clear
                self._clear_streak += 1
                if self._clear_streak >= self._RECOVERY_CLEARS:
                    log.info(
                        "circuit_breaker_recovered",
                        held=[str(r) for r in self._bad_since],
                    )
                    self._bad_since = {}
                    self._clear_streak = 0
                # else: HOLD the timer through a brief clear (flap resistance) —
                # a bad/clear/bad flapper must still accumulate toward escalation.
            return verdict

        reason = verdict.reason
        assert reason is not None  # a trip always carries a reason
        self._clear_streak = 0  # any trip breaks a recovery streak
        grace = self._GRACE_S.get(reason)
        if grace is None:
            # structural / genuine → hard-halt immediately (unchanged behaviour)
            log.error(
                "circuit_breaker_tripped", reason=str(reason), detail=verdict.detail
            )
            await self._killswitch.halt(reason, verdict.detail)
            return verdict

        # transient → hold for the grace window; keep only THIS reason's timer so a
        # different reason on the next tick can't inherit a stale start time (a
        # same-reason re-trip preserves the first-bad instant, so a sustained or
        # flapping condition still accumulates toward the hard halt).
        since_ns = self._bad_since.get(reason, now_ns)
        self._bad_since = {reason: since_ns}
        elapsed_s = (now_ns - since_ns) / 1e9
        if elapsed_s < grace:
            log.warning(
                "circuit_breaker_transient_holding",
                reason=str(reason),
                elapsed_s=round(elapsed_s, 1),
                grace_s=grace,
                detail=verdict.detail,
            )
            return verdict  # do NOT hard-halt — give the recovery path time
        log.error(
            "circuit_breaker_tripped_sustained",
            reason=str(reason),
            elapsed_s=round(elapsed_s, 1),
            detail=verdict.detail,
        )
        await self._killswitch.halt(
            reason, f"{verdict.detail} (sustained {elapsed_s:.0f}s > {grace:.0f}s grace)"
        )
        return verdict
