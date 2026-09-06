"""COMMUNICATIONS FAN-OUT SHARDING — the fills lever (2026-09-05).

What was wrong
--------------
After the reader-isolation merge the 17:59 ET boot showed ZERO exchange-side
"Subscription buffer overflow" drops (was ~1/min) — and BOTH accepts still
expired: ``confirm_expired_by_exchange`` with ``accept_to_confirm_ms`` 547 and
666, ``dispatch_delay_ms`` 8 and 137, ``confirm_rtt_ms`` 55-60, against the
exchange's 3,000 ms window. The accept frame was late BEFORE it reached the
process. Exchange pipe lag (an RFQ's ``created_ts`` → the instant it left our
socket) on that boot: p50 6.4 s, p90 9.0 s, 85 % > 3 s (8/26: p50 1.5 s;
overnight 9/5: 2.2 s). Inbound on the ONE unsharded ``communications``
subscription ran ~1,100-1,340 frames/s (the ``seq`` on the code-25 error
frames: 72,694 frames in the 60.5 s between two overflows on the 15:00 boot).
One connection's drain — a reader thread sharing the GIL with a saturated
main loop, one TCP window, one server-side per-subscription buffer — cannot
keep pace, so TCP back-pressure makes the exchange deliver our frames seconds
late, accepts included.

The mechanism
-------------
Kalshi's communications channel supports FAN-OUT SHARDING (docs/api-notes/
asyncapi-ws.md §3.2, communications-ws.md:57-67 "run one connection per
shard_key"): subscribe with ``shard_factor`` N (1..100) and ``shard_key`` k,
one connection per k, and every frame lands on exactly one of the N sockets
(subscribing to ALL keys 0..N-1 makes the open routing question — hash of
rfq id? market? do our own quote events follow the RFQ's shard? — moot: we
hold every shard). Each shard carries 1/N of the flow; the resubscribe
snapshot dump divides by N too.

``CommsFanout`` is a ``WsManager`` HOST (the shared ``_Lanes`` + the ONE
dispatcher + the consumer handlers, no socket of its own) over N follower
``WsManager`` sockets (own reader thread each, ``dispatch=False``, pushing
into the host's lanes). Nothing downstream changes: the intake registers on
the host exactly as on a single manager; the priority lane is drained first
across all sockets; market-lane capacity/age shedding is the host's, once;
wire stamps are the followers'.

Recovery rule (the exact rule, per the UPTIME #3 finding)
-----------------------------------------------------------
* A TERMINAL channel error (10/17/25) on ONE shard reconnects THAT shard only
  (its own ``force_reconnect`` → resubscribe with its shard params; its
  queued market frames are purged, its priority frames are kept). The frame
  is NOT forwarded to the intake, so no ``cancel_all``: open quotes stand,
  the siblings are untouched, the loss is a WARNING + metric. Accepts routed
  to that shard during its ~1 s gap are lost auctions (the taker's window
  lapses; no position can result without our confirm) — the price of not
  pulling the whole book for one socket.
* When EVERY shard is in the lost state at once (each has reported a
  terminal error and not yet re-acked its subscribe), the loss is a genuine
  whole-channel loss: the frame IS forwarded → intake ``on_channel_lost`` →
  the app's ``cancel_all`` + ``force_reconnect`` (all shards) — today's rule.
  With N = 1 this is always the case: byte-identical to today.
* A shard's ordinary socket death (EOF, timeout) reconnects on its own reader
  as today. The consumer's ``on_disconnect`` (the intake's registry reset)
  fires ONCE per loss epoch. The epoch stays open until EVERY shard whose
  socket dropped has RE-ACKED its subscribe (review fix 2026-09-05: closing
  it on the first re-CONNECT let an exchange-side rolling close that reached
  the N sockets in sequence fire N times, rotating the intake's two
  stale-liveness generations N times). A death that lands after every
  casualty has re-acked is a new epoch — without shard-scoping the intake's
  registry (out of this build's radius) there is nothing to tie it to.
* Any non-ack answer to a SHARDED subscribe that is not a terminal channel
  code (10/17/25 reconnect that shard on their own path) is a REFUSAL —
  the documented sharding-validation codes 19-22, 11 "Invalid parameter",
  and every unlisted one (6 "Already subscribed" is the open question of
  whether N connections under one key may each hold a communications
  subscription): it falls back LOUDLY to the single unsharded subscription
  — fail-safe = today's behaviour — never to a shard left CONNECTED but
  UNSUBSCRIBED with every health reading green (review must-fix 2026-09-05).
* A live re-shard purges the retired generation's queued MARKET frames from
  the shared lanes before the new set opens (the 2026-07-14 discard rule:
  the new sockets re-dump every open RFQ, so nothing queued from the old
  ones is load-bearing — up to 20,000 stale parses otherwise ran ahead of
  the re-dump). Priority and control frames are kept.
* Whether Kalshi PARTITIONS our own quote events (accept / execute) by the
  RFQ's shard or BROADCASTS them to every subscription of ours is
  undocumented. Under N > 1 the host admits each priority frame ONCE per
  ``(type, quote_id)`` (``ws_fanout_duplicate_priority_frame`` WARNING +
  metric on a duplicate): a duplicate accept would otherwise re-run the
  confirm against a quote already confirmed and count a confirm failure of
  ours toward ``HALT_CONFIRM_TIMEOUTS``. N = 1 admits everything (one
  socket cannot duplicate; byte-identical to today).

Deriving N (constitution: no hand-set numbers)
-----------------------------------------------
Measured per shard per window, on the reader thread (counts only, never a
log line — the hang watchdog's log axis stays main-loop work):

* ``frames`` read, and the reader's HANDLING wall time (receive stamp → lane
  push: parse, classify, push, any GIL wait inside that window);
* the PIPE LAG of every ``rfq_created`` (server ``created_ts`` → receive) and
  of every ``quote_created`` — our own quote acks, server-stamped: the
  quote-event-path proxy, because ``quote_accepted`` carries NO timestamp
  (SUMMARY.md:24). Judged against the exchange confirm window (3,000 ms —
  a protocol fact, ``EXCHANGE_CONFIRM_WINDOW_S``, passed in by the caller).

Evidence per window (a (re)subscribe marks its own and the following window
as SNAPSHOT — the open-RFQ re-dump is a one-off, not a rate — excluded):

    inbound_total_fps   = Σ_k frames_k / elapsed
    capacity_k          = fps_k                    if the shard VIOLATED
                                                   (rfq pipe-lag p50 > window:
                                                   at that rate it was not
                                                   keeping up, whatever the
                                                   cause — the rate it read at
                                                   IS its demonstrated ceiling)
                        = fps_k / utilization_k    otherwise (utilisation =
                                                   handling time / elapsed:
                                                   the reader's own measured
                                                   service time extrapolated)

Pooled over the retained boots (retention = the oldest ``live_*.log`` on
disk, the gap tape's rule) plus this boot:

    N = clamp( ceil( Q_hi(inbound) × HEADROOM / Q_lo(capacity) ), 1, 100 )

* ``Q_hi`` / ``Q_lo`` are the upper / lower quantiles at the policy z
  ladder's DAILY rung (z = 3, daily 3 / weekly 4 / KILL 5): N is re-derived
  per boot, an intra-day unit (``risk/confirm_expired_rate.py``'s argument),
  and sizing for the surge quantile rather than the mean is the 2026-08-01
  lesson (the mean-sized queue overflowed for 62 minutes). For samples under
  ~740 windows these are the max and the min.
* ``HEADROOM`` is the hang watchdog's / stall wall's ``_MARGIN`` (2.0,
  ``risk/stall_wall.py``): the codebase's one existing "measured × margin"
  rule. Here it covers the estimator's KNOWN optimism — handling time
  excludes aiohttp's TLS/WebSocket parse and GIL waits outside the handling
  window — by running each connection at half its measured ceiling.
* Floor 1; cap 100 (the documented ``shard_factor`` maximum).

SHRINK GATE (review fix 2026-09-05). The healthy-window estimate
``fps / utilisation`` measures OUR reader's handling time, not the bound that
actually bit (the server-side per-subscription buffer / TCP window): at N = 3
a shard reading ~430 fps at ~2 % utilisation reports a ~20,000 fps "ceiling",
a ~10× mismatch HEADROOM = 2 does not cover. N held at 3 only while the
pooled unsharded VIOLATING windows (capacity = read rate ≈ 1,100) stayed in
the tape; once pruned (retention = log rotation) Q_lo jumped, N derived 1,
the next boot ran unsharded, violated, and re-grew — a 1→2→…→1 oscillation
across boots. So a third evidence class is pooled: the SUSTAINED rate — a
healthy window's per-connection read rate ``fps_k`` (the connection
demonstrably kept up at that rate: a LOWER bound on its ceiling, never an
extrapolation). A SHRINK from the current N is applied only down to

    N_sustained = ceil( Q_hi(inbound) × HEADROOM / Q_hi(sustained) )

i.e. only as far as some connection has demonstrably carried the load each
would carry after the shrink, with the same headroom; no sustained evidence ⇒
hold. Growth never consults it (at N shards each connection carries 1/N of the
flow, so the demonstrated rate alone would always argue for 2N — a ratchet).

Bootstrap: an empty tape derives N = 1 (source ``bootstrap`` — exactly
today's unsharded subscribe, no params on the wire); the first two refresh
windows measure (the first holds the snapshot); the derivation then applies.
GROWTH is applied LIVE (fail-safe direction, and urgent — accepts are
expiring) — but never while an accept is in flight (the app's
``AcceptPriorityGate.holding``): a re-shard closes every comms socket for
~1 s, and a ``quote_executed`` for a quote confirmed just before would be
lost to that gap (recovered only by the sweep's REST poll); the apply defers
to the next tick (``apply_deferred=accept_in_flight``). A SHRINK is deferred
to the next boot (an optimisation, applied at the natural rebuild point,
gated as above against the previous boot's factor; logged
``deferred_shrink``). An explicit override
(``endpoints.comms_shard_factor_override``) is applied and logged as
``source=override`` — never a silent number. A sharding refusal is sticky
for the boot and RECORDED in the tape (``boots_refused`` in every
derivation line); the next boot re-probes — at boot no quote stands and the
registry is empty, so the fallback costs one ~1 s reconnect, while honouring
a persisted refusal would let one transient exchange error park the fills
lever until a human moved a knob.

Known failure mode, disclosed: if pipe lag stays above the window at N shards
for a cause sharding cannot fix, violating windows keep contributing
``capacity = read rate`` and N grows again next boot (≈ ×2 per boot toward
the cap) with ``pipe_lag_exceeds_confirm_window`` firing throughout — the
alarm and ``ws_fanout_derivation`` make it visible; the cap bounds it.

Blast radius: the communications transport (sockets, subscriptions, the
per-shard recovery rule, telemetry). Pricing and risk read nothing from here;
the intake's interface is unchanged.
"""

from __future__ import annotations

import asyncio
import math
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from combomaker.core.clock import Clock
from combomaker.exchange.auth import RequestSigner
from combomaker.exchange.ws import (
    SHARD_FACTOR_MAX,
    SHARD_VALIDATION_ERROR_CODES,
    TERMINAL_CHANNEL_ERROR_CODES,
    Connector,
    LifecycleHandler,
    SubscribedHandler,
    SubscribeErrorHandler,
    WsManager,
    _aiohttp_connect,
)
from combomaker.ops.logging import get_logger
from combomaker.ops.metrics import Metrics
from combomaker.risk.confirm_expired_rate import EXPIRED_RATE_ALARM_Z
from combomaker.risk.heartbeat import _atomic_write
from combomaker.risk.stall_wall import (
    STALL_WALL_MARGIN,
    normal_upper_tail_p,
    oldest_live_log_mtime,
)

log = get_logger(__name__)

JsonDict = dict[str, Any]

# The hang watchdog's / stall wall's margin — the codebase's existing
# "measured × margin" headroom rule (derivation in the module doc).
FANOUT_HEADROOM = STALL_WALL_MARGIN
# The policy z ladder's DAILY rung (daily 3 / weekly 4 / KILL 5): N is
# re-derived per boot, an intra-day unit; an under-provisioned N is an alarm
# condition, not a kill. IMPORTED, not duplicated, so the anchor cannot drift.
FANOUT_Z = EXPIRED_RATE_ALARM_Z
FANOUT_TAPE_FILENAME = "ws_fanout_tape.json"
# v2 (2026-09-05 review fixes): + ``sustained`` histogram, + ``refused`` per
# boot. A v1 record loads with empty sustained evidence and refused = False.
_SCHEMA_VERSION = 2
# The DOCUMENTED sharding refusals — 19-22 (the validation table) and 11
# "Invalid parameter" when it answers a subscribe that carried shard params.
# The fallback rule is wider (module doc): ANY non-terminal answer to a
# sharded subscribe is a refusal; this set only labels the log line
# ``documented=True/False`` so an unlisted code (6 "Already subscribed") is
# recognisable as the per-key open question rather than a docs error.
SHARDING_REFUSED_CODES: frozenset[int] = SHARD_VALIDATION_ERROR_CODES | frozenset({11})
COMMUNICATIONS_CHANNEL = "communications"
_LAG_TYPES: frozenset[str] = frozenset({"rfq_created", "quote_created"})


def fanout_tape_path(data_dir: Path) -> Path:
    return data_dir / FANOUT_TAPE_FILENAME


def _parse_rfc3339_epoch(text: str) -> float | None:
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _error_code(message: JsonDict) -> int:
    msg = message.get("msg", {})
    if not isinstance(msg, dict):
        return 0
    try:
        return int(msg.get("code", 0))
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# Measurement: pipe-lag histogram, per-shard meter, windows
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class LagSummary:
    n: int
    p50_ms: float
    p90_ms: float
    max_ms: float
    over_window: int

    @property
    def share_over_window(self) -> float:
        return self.over_window / self.n if self.n else 0.0

    def as_log(self) -> dict[str, object]:
        return {
            "n": self.n,
            "p50_ms": round(self.p50_ms, 1),
            "p90_ms": round(self.p90_ms, 1),
            "max_ms": round(self.max_ms, 1),
            "share_over_window": round(self.share_over_window, 4),
        }


class LagHistogram:
    """Integer-millisecond buckets of pipe lag (the confirm window is
    expressed in ms). Sparse; reset every window."""

    __slots__ = ("counts", "n", "max_ms")

    def __init__(self) -> None:
        self.counts: dict[int, int] = {}
        self.n = 0
        self.max_ms = -math.inf

    def observe(self, lag_ms: float) -> None:
        if math.isnan(lag_ms) or math.isinf(lag_ms):
            return
        idx = int(math.floor(lag_ms))
        self.counts[idx] = self.counts.get(idx, 0) + 1
        self.n += 1
        if lag_ms > self.max_ms:
            self.max_ms = lag_ms

    def merge(self, other: LagHistogram) -> None:
        for k, c in other.counts.items():
            self.counts[k] = self.counts.get(k, 0) + c
        self.n += other.n
        if other.max_ms > self.max_ms:
            self.max_ms = other.max_ms

    def quantile(self, p: float) -> float:
        if self.n == 0:
            return 0.0
        target = p * self.n
        seen = 0
        for idx in sorted(self.counts):
            seen += self.counts[idx]
            if seen >= target:
                return float(idx)
        return self.max_ms

    def summary(self, window_ms: float) -> LagSummary | None:
        if self.n == 0:
            return None
        over = sum(c for k, c in self.counts.items() if k > window_ms)
        return LagSummary(
            n=self.n,
            p50_ms=self.quantile(0.50),
            p90_ms=self.quantile(0.90),
            max_ms=self.max_ms,
            over_window=over,
        )


@dataclass(frozen=True, slots=True)
class ShardWindow:
    """One measurement window of one shard socket (main-loop view)."""

    shard: int
    elapsed_s: float
    frames: int
    busy_ns: int
    lag_rfq: LagHistogram
    lag_quote: LagHistogram
    snapshot: bool
    shed_lost: int

    @property
    def fps(self) -> float:
        return self.frames / self.elapsed_s if self.elapsed_s > 0 else 0.0

    @property
    def utilization(self) -> float:
        if self.elapsed_s <= 0:
            return 0.0
        return min(1.0, max(0.0, self.busy_ns / (self.elapsed_s * 1e9)))

    def violating(self, window_ms: float) -> bool:
        return self.lag_rfq.n > 0 and self.lag_rfq.quantile(0.50) > window_ms

    def capacity_fps(self, window_ms: float) -> float | None:
        """The connection's measured ceiling this window (module doc): the
        read rate when it violated the window, else the read rate
        extrapolated by the reader's own utilisation. None = no evidence."""
        if self.frames == 0 or self.elapsed_s <= 0:
            return None
        rate = self.fps
        if self.violating(window_ms):
            return rate
        util = self.utilization
        if util <= 0.0:
            return None
        return rate / util


class ShardMeter:
    """Per-shard counters written by the reader thread (``observe``), read and
    reset by the main loop (``take``). Never logs."""

    def __init__(self, shard: int, clock: Clock) -> None:
        self.shard = shard
        self._clock = clock
        self._lock = threading.Lock()
        self._frames = 0
        self._busy_ns = 0
        self._lag: dict[str, LagHistogram] = {t: LagHistogram() for t in _LAG_TYPES}
        self._subscribed_current = False
        self._subscribed_prev = False
        self._window_start_ns = clock.monotonic_ns()
        self._shed_lost_seen = 0

    def seed_shed_lost(self, total: int) -> None:
        self._shed_lost_seen = total

    def observe(self, message: JsonDict, recv_mono_ns: int, handling_ns: int) -> None:
        """Reader thread. ``rfq_created`` / ``quote_created`` frames carry a
        server ``created_ts``; its distance to the receive instant is the
        pipe lag. Everything else only counts toward rate and busy time."""
        msg_type = str(message.get("type", ""))
        lag_ms: float | None = None
        if msg_type in _LAG_TYPES:
            msg = message.get("msg")
            created = msg.get("created_ts") if isinstance(msg, dict) else None
            if isinstance(created, str):
                created_s = _parse_rfc3339_epoch(created)
                if created_s is not None:
                    # The receive stamp is monotonic; wall time is read here,
                    # microseconds after it, on the same thread.
                    lag_ms = (self._clock.now().timestamp() - created_s) * 1e3
        with self._lock:
            self._frames += 1
            self._busy_ns += handling_ns
            if lag_ms is not None:
                self._lag[msg_type].observe(lag_ms)

    def mark_subscribed(self) -> None:
        """Main loop (subscribe ack): this window and the next carry the
        exchange's open-RFQ re-dump — a one-off, not a rate."""
        with self._lock:
            self._subscribed_current = True

    def take(self, now_mono_ns: int, shed_lost_total: int) -> ShardWindow:
        with self._lock:
            elapsed_s = max(0.0, (now_mono_ns - self._window_start_ns) / 1e9)
            window = ShardWindow(
                shard=self.shard,
                elapsed_s=elapsed_s,
                frames=self._frames,
                busy_ns=self._busy_ns,
                lag_rfq=self._lag["rfq_created"],
                lag_quote=self._lag["quote_created"],
                snapshot=self._subscribed_current or self._subscribed_prev,
                shed_lost=max(0, shed_lost_total - self._shed_lost_seen),
            )
            self._subscribed_prev = self._subscribed_current
            self._subscribed_current = False
            self._frames = 0
            self._busy_ns = 0
            self._lag = {t: LagHistogram() for t in _LAG_TYPES}
            self._window_start_ns = now_mono_ns
            self._shed_lost_seen = shed_lost_total
            return window


# --------------------------------------------------------------------------- #
# Evidence: log-spaced rate histogram, per-boot tape, derivation
# --------------------------------------------------------------------------- #


class RateHistogram:
    """Frames/s observations in LOG-SPACED buckets whose relative width is the
    finest resolution an integer N ≤ 100 can express (ratio 1 + 1/100): a
    rate spanning 1..10^9 fps costs ≤ ~2,100 keys, so the per-boot tape stays
    small however many windows a boot records. Upper quantiles resolve to a
    bucket's upper edge (conservative-high), lower quantiles to its lower
    edge (conservative-low), both clipped to the observed extremes."""

    RATIO = 1.0 + 1.0 / SHARD_FACTOR_MAX
    __slots__ = ("counts", "n", "max_fps", "min_fps")

    def __init__(self) -> None:
        self.counts: dict[int, int] = {}
        self.n = 0
        self.max_fps = 0.0
        self.min_fps = math.inf

    @classmethod
    def _index(cls, fps: float) -> int:
        return int(math.floor(math.log(fps) / math.log(cls.RATIO)))

    def observe(self, fps: float) -> None:
        if not (fps > 0.0) or math.isinf(fps):
            return
        idx = self._index(fps)
        self.counts[idx] = self.counts.get(idx, 0) + 1
        self.n += 1
        if fps > self.max_fps:
            self.max_fps = fps
        if fps < self.min_fps:
            self.min_fps = fps

    def merge(self, other: RateHistogram) -> None:
        for idx, c in other.counts.items():
            self.counts[idx] = self.counts.get(idx, 0) + c
        self.n += other.n
        if other.max_fps > self.max_fps:
            self.max_fps = other.max_fps
        if other.min_fps < self.min_fps:
            self.min_fps = other.min_fps

    def quantile_upper(self, p: float) -> float:
        if not 0.0 < p <= 1.0:
            raise ValueError(f"quantile out of range: {p}")
        if self.n == 0:
            return 0.0
        target = p * self.n
        seen = 0
        for idx in sorted(self.counts):
            seen += self.counts[idx]
            if seen >= target:
                return min(self.RATIO ** (idx + 1), self.max_fps)
        return self.max_fps

    def quantile_lower(self, p: float) -> float:
        if not 0.0 < p <= 1.0:
            raise ValueError(f"quantile out of range: {p}")
        if self.n == 0:
            return 0.0
        target = p * self.n
        seen = 0
        for idx in sorted(self.counts):
            seen += self.counts[idx]
            if seen >= target:
                return max(self.RATIO**idx, self.min_fps)
        return self.max_fps

    def copy(self) -> RateHistogram:
        h = RateHistogram()
        h.counts = dict(self.counts)
        h.n = self.n
        h.max_fps = self.max_fps
        h.min_fps = self.min_fps
        return h

    def to_json(self) -> dict[str, object]:
        return {
            "n": self.n,
            "max_fps": self.max_fps,
            "min_fps": None if math.isinf(self.min_fps) else self.min_fps,
            "counts": {str(k): v for k, v in sorted(self.counts.items())},
        }

    @classmethod
    def from_json(cls, payload: object) -> RateHistogram | None:
        """None on any malformed payload — dropped from the pool, never
        raised into the derivation."""
        if not isinstance(payload, dict):
            return None
        try:
            h = cls()
            raw = payload.get("counts", {})
            if not isinstance(raw, dict):
                return None
            for k, v in raw.items():
                idx = int(k)
                c = int(v)
                if c < 0:
                    return None
                if c:
                    h.counts[idx] = c
            h.n = int(payload.get("n", sum(h.counts.values())))
            h.max_fps = float(payload.get("max_fps", 0.0))
            raw_min = payload.get("min_fps")
            h.min_fps = math.inf if raw_min is None else float(raw_min)
        except (KeyError, TypeError, ValueError):
            return None
        if h.n != sum(h.counts.values()) or h.max_fps < 0.0:
            return None
        return h


@dataclass(frozen=True, slots=True)
class PooledEvidence:
    inbound: RateHistogram
    capacity: RateHistogram
    n_windows: int
    n_violating: int
    boots: int
    # Healthy windows' per-connection read rates (the shrink gate's evidence).
    sustained: RateHistogram = field(default_factory=RateHistogram)
    # Retained boots whose sharded subscribe the exchange refused.
    boots_refused: int = 0
    # The factor the most recent retained boot (with any applied) ran at last
    # — the shrink gate's ``current`` at the next boot.
    last_shard_factor: int | None = None


class FanoutTape:
    """Per-boot evidence on disk: ``<data_dir>/ws_fanout_tape.json``. Load →
    fold this boot → prune → pool → save. Every failure degrades to "no
    tape" (N = 1), never to a raise into the transport."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.boots: dict[str, dict[str, Any]] = {}

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        self.boots = {}
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return
        try:
            import json

            payload = json.loads(raw)
        except ValueError:
            log.warning("ws_fanout_tape_unreadable", path=str(self._path))
            return
        boots = payload.get("boots") if isinstance(payload, dict) else None
        if not isinstance(boots, dict):
            return
        for key, rec in boots.items():
            if not isinstance(rec, dict):
                continue
            inbound = RateHistogram.from_json(rec.get("inbound"))
            capacity = RateHistogram.from_json(rec.get("capacity"))
            if inbound is None or capacity is None:
                continue
            # v1 records carry no ``sustained`` (⇒ no demonstrated rate: the
            # shrink gate holds) and no ``refused`` (⇒ False).
            sustained = RateHistogram.from_json(rec.get("sustained"))
            if sustained is None:
                sustained = RateHistogram()
            started = rec.get("started_at_ts")
            self.boots[str(key)] = {
                "started_at_ts": float(started) if isinstance(started, int | float) else None,
                "inbound": inbound,
                "capacity": capacity,
                "sustained": sustained,
                "n_windows": int(rec.get("n_windows", 0) or 0),
                "n_snapshot": int(rec.get("n_snapshot", 0) or 0),
                "n_violating": int(rec.get("n_violating", 0) or 0),
                "shard_factors": [int(x) for x in rec.get("shard_factors", []) or []],
                "refused": bool(rec.get("refused", False)),
            }

    def fold(
        self,
        boot_key: str,
        *,
        started_at_ts: float,
        inbound: RateHistogram,
        capacity: RateHistogram,
        n_windows: int,
        n_snapshot: int,
        n_violating: int,
        shard_factors: list[int],
        sustained: RateHistogram | None = None,
        refused: bool = False,
    ) -> None:
        """Replace this boot's record with its CURRENT (cumulative) evidence."""
        self.boots[boot_key] = {
            "started_at_ts": started_at_ts,
            "inbound": inbound.copy(),
            "capacity": capacity.copy(),
            "sustained": sustained.copy() if sustained is not None else RateHistogram(),
            "n_windows": n_windows,
            "n_snapshot": n_snapshot,
            "n_violating": n_violating,
            "shard_factors": list(shard_factors),
            "refused": bool(refused),
        }

    def prune(self, *, retain_since_ts: float | None) -> int:
        if retain_since_ts is None:
            return 0
        doomed = [
            k
            for k, rec in self.boots.items()
            if isinstance(rec.get("started_at_ts"), float)
            and float(rec["started_at_ts"]) < retain_since_ts
        ]
        for k in doomed:
            del self.boots[k]
        return len(doomed)

    def pooled(self) -> PooledEvidence:
        inbound = RateHistogram()
        capacity = RateHistogram()
        sustained = RateHistogram()
        n_windows = 0
        n_violating = 0
        boots_refused = 0
        last_factor: int | None = None
        last_started = -math.inf
        for rec in self.boots.values():
            inb = rec.get("inbound")
            cap = rec.get("capacity")
            if isinstance(inb, RateHistogram) and isinstance(cap, RateHistogram):
                inbound.merge(inb)
                capacity.merge(cap)
                sus = rec.get("sustained")
                if isinstance(sus, RateHistogram):
                    sustained.merge(sus)
                n_windows += int(rec.get("n_windows", 0))
                n_violating += int(rec.get("n_violating", 0))
                if rec.get("refused"):
                    boots_refused += 1
                factors = rec.get("shard_factors") or []
                started = rec.get("started_at_ts")
                if factors and isinstance(started, float) and started > last_started:
                    last_started = started
                    last_factor = int(factors[-1])
        return PooledEvidence(
            inbound,
            capacity,
            n_windows,
            n_violating,
            len(self.boots),
            sustained=sustained,
            boots_refused=boots_refused,
            last_shard_factor=last_factor,
        )

    def save(self) -> None:
        import json

        payload = {
            "schema_version": _SCHEMA_VERSION,
            "boots": {
                key: {
                    "started_at_ts": rec.get("started_at_ts"),
                    "inbound": rec["inbound"].to_json(),
                    "capacity": rec["capacity"].to_json(),
                    "sustained": (
                        rec["sustained"].to_json()
                        if isinstance(rec.get("sustained"), RateHistogram)
                        else RateHistogram().to_json()
                    ),
                    "n_windows": rec.get("n_windows", 0),
                    "n_snapshot": rec.get("n_snapshot", 0),
                    "n_violating": rec.get("n_violating", 0),
                    "shard_factors": rec.get("shard_factors", []),
                    "refused": bool(rec.get("refused", False)),
                }
                for key, rec in self.boots.items()
            },
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(self._path, json.dumps(payload))
        except OSError as exc:  # pragma: no cover - disk failure path
            log.warning("ws_fanout_tape_write_failed", path=str(self._path), error=repr(exc))


def refresh_fanout_tape(
    *,
    tape_path: Path,
    data_dir: Path,
    boot_key: str,
    boot_started_at_ts: float,
    boot_inbound: RateHistogram,
    boot_capacity: RateHistogram,
    boot_n_windows: int,
    boot_n_snapshot: int,
    boot_n_violating: int,
    boot_shard_factors: list[int],
    boot_sustained: RateHistogram | None = None,
    boot_refused: bool = False,
) -> PooledEvidence:
    """Synchronous file I/O + arithmetic on COPIES — call it off the loop."""
    tape = FanoutTape(tape_path)
    tape.load()
    tape.fold(
        boot_key,
        started_at_ts=boot_started_at_ts,
        inbound=boot_inbound,
        capacity=boot_capacity,
        n_windows=boot_n_windows,
        n_snapshot=boot_n_snapshot,
        n_violating=boot_n_violating,
        shard_factors=boot_shard_factors,
        sustained=boot_sustained,
        refused=boot_refused,
    )
    tape.prune(retain_since_ts=oldest_live_log_mtime(data_dir))
    tape.save()
    return tape.pooled()


@dataclass(frozen=True, slots=True)
class ShardFactorDerivation:
    """One derivation of N — everything the log line and the report need."""

    n: int
    source: str  # "bootstrap" | "measured" | "override" | "fallback_unsharded"
    inbound_fps: float
    inbound_max_fps: float
    capacity_fps: float
    capacity_min_fps: float
    n_windows: int
    n_violating: int
    boots_pooled: int
    margin: float
    z: float
    p_hi: float
    p_lo: float
    floor: int = 1
    cap: int = SHARD_FACTOR_MAX
    override: int | None = None
    n_measured: int | None = None
    # Shrink gate (module doc): the highest demonstrably sustained
    # per-connection rate, the N it supports, the N the gate judged against,
    # and whether it held a shrink back.
    sustained_fps: float = 0.0
    n_sustained: int | None = None
    current: int | None = None
    shrink_held: bool = False
    boots_refused: int = 0

    def as_log(self) -> dict[str, object]:
        return {
            "shard_factor_derived": self.n,
            "source": self.source,
            "inbound_q_hi_fps": round(self.inbound_fps, 1),
            "inbound_max_fps": round(self.inbound_max_fps, 1),
            "capacity_q_lo_fps": round(self.capacity_fps, 1),
            "capacity_min_fps": round(self.capacity_min_fps, 1),
            "sustained_q_hi_fps": round(self.sustained_fps, 1),
            "n_windows": self.n_windows,
            "n_violating": self.n_violating,
            "boots_pooled": self.boots_pooled,
            "boots_refused": self.boots_refused,
            "margin": self.margin,
            "z": self.z,
            "p_hi": self.p_hi,
            "p_lo": self.p_lo,
            "floor": self.floor,
            "cap": self.cap,
            "override": self.override,
            "shard_factor_measured": self.n_measured,
            "shard_factor_sustained": self.n_sustained,
            "shrink_gate_current": self.current,
            "shrink_held": self.shrink_held,
        }


def derive_shard_factor(
    inbound: RateHistogram | None,
    capacity: RateHistogram | None,
    *,
    n_windows: int,
    n_violating: int,
    boots_pooled: int,
    override: int | None = None,
    refused: bool = False,
    margin: float = FANOUT_HEADROOM,
    z: float = FANOUT_Z,
    sustained: RateHistogram | None = None,
    current: int | None = None,
    boots_refused: int = 0,
) -> ShardFactorDerivation:
    """``clamp(ceil(Q_hi(inbound) × margin / Q_lo(capacity)), 1, 100)`` —
    see the module doc. ``margin`` must be ≥ 1 (below one would size a
    connection INSIDE its measured ceiling — the defect this removes).

    SHRINK GATE: when ``current`` is given and the measured N is below it,
    the shrink is applied only down to ``N_sustained = ceil(Q_hi(inbound) ×
    margin / Q_hi(sustained))`` — as far as a connection has demonstrably
    carried the post-shrink load with the same headroom; no sustained
    evidence ⇒ hold at ``current``. Growth never consults it."""
    if not (margin >= 1.0) or math.isinf(margin):
        raise ValueError(f"margin must be >= 1 and finite, got {margin}")
    p_hi = normal_upper_tail_p(z)
    p_lo = 1.0 - p_hi
    inbound_q = inbound.quantile_upper(p_hi) if inbound is not None and inbound.n else 0.0
    inbound_max = inbound.max_fps if inbound is not None and inbound.n else 0.0
    cap_q = capacity.quantile_lower(p_lo) if capacity is not None and capacity.n else 0.0
    cap_min = (
        capacity.min_fps
        if capacity is not None and capacity.n and not math.isinf(capacity.min_fps)
        else 0.0
    )
    sus_q = sustained.quantile_upper(p_hi) if sustained is not None and sustained.n else 0.0
    n_measured: int | None = None
    if inbound_q > 0.0 and cap_q > 0.0:
        n_measured = max(1, min(SHARD_FACTOR_MAX, math.ceil(inbound_q * margin / cap_q)))
    n_sustained: int | None = None
    if inbound_q > 0.0 and sus_q > 0.0:
        n_sustained = max(1, min(SHARD_FACTOR_MAX, math.ceil(inbound_q * margin / sus_q)))
    shrink_held = False
    if override is not None:
        n = max(1, min(SHARD_FACTOR_MAX, int(override)))
        source = "override"
    elif refused:
        n = 1
        source = "fallback_unsharded"
    elif n_measured is None:
        n = 1
        source = "bootstrap"
    else:
        n = n_measured
        source = "measured"
        if current is not None and n_measured < current:
            gate_floor = current if n_sustained is None else n_sustained
            n = min(current, max(n_measured, gate_floor))
            shrink_held = n > n_measured
    return ShardFactorDerivation(
        n=n,
        source=source,
        inbound_fps=inbound_q,
        inbound_max_fps=inbound_max,
        capacity_fps=cap_q,
        capacity_min_fps=cap_min,
        n_windows=n_windows,
        n_violating=n_violating,
        boots_pooled=boots_pooled,
        margin=margin,
        z=z,
        p_hi=p_hi,
        p_lo=p_lo,
        override=override,
        n_measured=n_measured,
        sustained_fps=sus_q,
        n_sustained=n_sustained,
        current=current,
        shrink_held=shrink_held,
        boots_refused=boots_refused,
    )


# --------------------------------------------------------------------------- #
# The transport: host + N follower sockets
# --------------------------------------------------------------------------- #


@dataclass
class _BaseSub:
    channels: list[str]
    params_extra: dict[str, Any] = field(default_factory=dict)
    on_subscribed: SubscribedHandler | None = None
    on_subscribe_error: SubscribeErrorHandler | None = None

    @property
    def shardable(self) -> bool:
        return self.channels == [COMMUNICATIONS_CHANNEL]


class CommsFanout(WsManager):
    """N communications sockets, one shared lane set, one dispatcher.

    Presents the ``WsManager`` surface the intake and the app use
    (``on_message`` / ``on_disconnect`` / ``add_subscription`` /
    ``mark_priority`` / ``mark_sheddable`` / ``start`` / ``stop`` /
    ``force_reconnect`` / ``lane_depths`` / health). Derivation of N lives in
    ``FanoutGovernor``; this class only applies a factor
    (``apply_shard_factor``) and measures (``take_windows``)."""

    def __init__(
        self,
        url: str,
        signer: RequestSigner,
        clock: Clock,
        metrics: Metrics | None = None,
        *,
        name: str = "ws",
        max_silence_s: float = 30.0,
        backoff_initial_s: float = 0.5,
        backoff_max_s: float = 30.0,
        connect: Connector = _aiohttp_connect,
    ) -> None:
        super().__init__(
            url,
            signer,
            clock,
            metrics,
            name=name,
            max_silence_s=max_silence_s,
            backoff_initial_s=backoff_initial_s,
            backoff_max_s=backoff_max_s,
            connect=connect,
            reader=False,  # HOST: lanes + dispatcher + handlers, no socket
        )
        self._shards: list[WsManager] = []
        self._meters: list[ShardMeter] = []
        self._generation = 0
        self._shard_factor = 1
        self._sharding_refused = False
        self._lost: set[int] = set()
        self._base_subs: list[_BaseSub] = []
        self._user_on_disconnect: list[LifecycleHandler] = []
        self._user_on_connect: list[LifecycleHandler] = []
        # LOSS EPOCH (module doc): ``_epoch_open`` = the consumer's
        # ``on_disconnect`` has fired and not every shard whose socket dropped
        # since has re-ACKED its subscribe (``_reacking``). Closed by the ack.
        self._epoch_open = False
        self._reacking: set[int] = set()
        # Priority frames admitted under N > 1, keyed (type, quote_id) →
        # first shard; insertion-ordered, bounded by the lane capacity (an
        # existing transport bound — tens of accepts a day never reach it).
        self._priority_seen: dict[tuple[str, str], int | None] = {}
        self._reshard_lock = asyncio.Lock()

    # --- surface ---

    @property
    def shard_factor(self) -> int:
        return self._shard_factor

    @property
    def shard_count(self) -> int:
        return len(self._shards)

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def sharding_refused(self) -> bool:
        return self._sharding_refused

    @property
    def connected(self) -> bool:
        return bool(self._shards) and all(s.connected for s in self._shards)

    @property
    def healthy(self) -> bool:
        return bool(self._shards) and all(s.healthy for s in self._shards)

    @property
    def last_rx_age_s(self) -> float | None:
        """The OLDEST shard's receive age; None if any shard is disconnected
        (the conservative reading for a freshness proof)."""
        ages: list[float] = []
        for s in self._shards:
            age = s.last_rx_age_s
            if age is None:
                return None
            ages.append(age)
        return max(ages) if ages else None

    def shard_states(self) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for k, s in enumerate(self._shards):
            age = s.last_rx_age_s
            out.append(
                {
                    "shard": k,
                    "connected": s.connected,
                    "rx_age_s": None if age is None else round(age, 3),
                    "frames_read": s._frames_read,
                    "lost": k in self._lost,
                }
            )
        return out

    def on_disconnect(self, handler: LifecycleHandler) -> None:
        self._user_on_disconnect.append(handler)

    def on_connect(self, handler: LifecycleHandler) -> None:
        self._user_on_connect.append(handler)

    def mark_priority(self, *msg_types: str) -> None:
        super().mark_priority(*msg_types)
        for s in self._shards:
            s._priority_types = self._priority_types

    def mark_sheddable(self, *msg_types: str, stale_after_s: float | None = None) -> None:
        super().mark_sheddable(*msg_types, stale_after_s=stale_after_s)
        for s in self._shards:
            s._sheddable_types = self._sheddable_types
            s._stale_after_ns = dict(self._stale_after_ns)

    def add_subscription(
        self,
        channels: list[str],
        *,
        on_subscribed: SubscribedHandler | None = None,
        on_subscribe_error: SubscribeErrorHandler | None = None,
        **params_extra: Any,
    ) -> None:
        """A ``["communications"]`` subscription is materialised on EVERY
        shard with that shard's ``shard_factor``/``shard_key`` (no params at
        all when N = 1 — today's wire bytes); any other channel goes to shard
        0 only. Declared subs are re-materialised on every re-shard."""
        base = _BaseSub(list(channels), dict(params_extra), on_subscribed, on_subscribe_error)
        self._base_subs.append(base)
        for k, shard in enumerate(self._shards):
            self._materialize(shard, k, self._meters[k], base)

    async def send_command(self, cmd: str, params: dict[str, Any], *, ws: Any = None) -> int:
        if not self._shards:
            raise RuntimeError("ws not connected")
        return await self._shards[0].send_command(cmd, params, ws=ws)

    # --- lifecycle ---

    def start(self) -> None:
        super().start()  # dispatcher only (reader=False)
        self._build_shards(self._shard_factor, epoch_open=False)

    async def stop(self) -> None:
        self._stopping = True
        shards, self._shards, self._meters = self._shards, [], []
        await self._stop_shards(shards)
        await super().stop()

    async def force_reconnect(self, shard: int | None = None) -> None:
        """All shards (the app's whole-channel path) or one (the per-shard
        rule). Each shard keeps its own reentrancy guard."""
        targets = (
            list(self._shards)
            if shard is None
            else [self._shards[shard]]
            if 0 <= shard < len(self._shards)
            else []
        )
        for s in targets:
            try:
                await s.force_reconnect()
            except Exception:
                log.exception("ws_shard_force_reconnect_failed", name=s._name)

    async def apply_shard_factor(self, n: int, *, reason: str) -> int:
        """Rebuild the shard set at ``n`` (clamped to [1, 100]; forced to 1
        once sharding was refused). Before ``start()`` it only records the
        factor. Live: fires the consumer's ``on_disconnect`` ONCE (mirrored
        state is invalid across the rebuild), stops the old sockets, PURGES
        their queued market frames from the shared lanes (the new sockets
        re-dump every open RFQ — review fix 2026-09-05: up to 20,000 stale
        parses otherwise ran ahead of the re-dump), builds the new set — old
        first, then new, so no frame is ever delivered twice (an overlap
        would double every RFQ and every accept). The loss epoch stays open
        until every new shard has acked."""
        n = max(1, min(SHARD_FACTOR_MAX, int(n)))
        if self._sharding_refused:
            n = 1
        async with self._reshard_lock:
            if not self._started:
                self._shard_factor = n
                return n
            if n == self._shard_factor and self._shards:
                return n
            log.info(
                "ws_fanout_resharding",
                name=self._name,
                shard_factor_from=self._shard_factor,
                shard_factor_to=n,
                reason=reason,
                generation=self._generation + 1,
            )
            self._metrics.inc(f"{self._name}.reshard")
            await self._fire_user_disconnect()
            shards, self._shards, self._meters = self._shards, [], []
            await self._stop_shards(shards)
            # Every retired reader has exited (stop joins the thread), so a
            # purge by shard tag is unambiguous here: no new set exists yet.
            purged = sum(self._lanes.purge_market(k) for k in range(len(shards)))
            if purged:
                self._metrics.inc(f"{self._name}.reshard_purged", purged)
                log.info(
                    "ws_fanout_reshard_purged",
                    name=self._name,
                    dropped=purged,
                    generation_retired=self._generation,
                )
            self._build_shards(n, epoch_open=True)
            return n

    # --- measurement ---

    def take_windows(self, now_mono_ns: int) -> list[ShardWindow]:
        """Main loop: every shard's window since the last take, reset."""
        return [
            meter.take(now_mono_ns, self._metrics.counter(f"{self._name}.s{k}.shed_lost"))
            for k, meter in enumerate(self._meters)
        ]

    # --- internals ---

    def _build_shards(self, n: int, *, epoch_open: bool) -> None:
        """``epoch_open``: True after a live re-shard (the consumer's
        ``on_disconnect`` just fired; the epoch closes when every new shard
        has acked), False on ``start()`` (nothing fired; a death before the
        first ack opens an epoch as a plain manager would)."""
        self._generation += 1
        gen = self._generation
        shards: list[WsManager] = []
        meters: list[ShardMeter] = []
        for k in range(n):
            meter = ShardMeter(k, self._clock)
            meter.seed_shed_lost(self._metrics.counter(f"{self._name}.s{k}.shed_lost"))
            shard = WsManager(
                self._url,
                self._signer,
                self._clock,
                self._metrics,
                name=f"{self._name}.s{k}",
                max_silence_s=self._max_silence_s,
                backoff_initial_s=self._backoff_initial_s,
                backoff_max_s=self._backoff_max_s,
                connect=self._connect,
                lanes=self._lanes,
                wake=self._schedule_wake,
                dispatch=False,
                shard_tag=k,
                shard_gen=gen,
                on_frame=meter.observe,
            )
            shard._priority_types = self._priority_types
            shard._sheddable_types = self._sheddable_types
            shard._stale_after_ns = dict(self._stale_after_ns)
            shard._lane_owner_name = self._name
            shard.on_connect(self._shard_connected_hook(k))
            shard.on_disconnect(self._shard_disconnected_hook(k))
            shards.append(shard)
            meters.append(meter)
        self._shards = shards
        self._meters = meters
        self._shard_factor = n
        self._lost = set()
        self._epoch_open = epoch_open
        self._reacking = set(range(n)) if epoch_open else set()
        for k, shard in enumerate(shards):
            for base in self._base_subs:
                self._materialize(shard, k, meters[k], base)
        for shard in shards:
            shard.start()
        log.info(
            "ws_fanout_started",
            name=self._name,
            shard_factor=n,
            generation=gen,
            sharded=n > 1,
        )

    async def _stop_shards(self, shards: list[WsManager]) -> None:
        results = await asyncio.gather(*(s.stop() for s in shards), return_exceptions=True)
        for s, r in zip(shards, results, strict=True):
            if isinstance(r, BaseException):
                log.warning("ws_shard_stop_failed", name=s._name, error=repr(r))

    def _materialize(self, shard: WsManager, k: int, meter: ShardMeter, base: _BaseSub) -> None:
        if not base.shardable:
            if k == 0:
                shard.add_subscription(
                    base.channels,
                    on_subscribed=base.on_subscribed,
                    on_subscribe_error=base.on_subscribe_error,
                    **base.params_extra,
                )
            return
        params = dict(base.params_extra)
        sharded = self._shard_factor > 1
        if sharded:
            params["shard_factor"] = self._shard_factor
            params["shard_key"] = k
        shard.add_subscription(
            base.channels,
            on_subscribed=self._ack_hook(k, meter, base),
            on_subscribe_error=self._subscribe_error_hook(k, base, sharded=sharded),
            **params,
        )

    def _ack_hook(self, k: int, meter: ShardMeter, base: _BaseSub) -> SubscribedHandler:
        async def hook(sid: int) -> None:
            self._lost.discard(k)
            self._shard_reacked(k)
            meter.mark_subscribed()
            log.info(
                "ws_shard_subscribed",
                name=self._name,
                shard=k,
                sid=sid,
                shard_factor=self._shard_factor,
            )
            if base.on_subscribed is not None:
                await base.on_subscribed(sid)

        return hook

    def _subscribe_error_hook(
        self, k: int, base: _BaseSub, *, sharded: bool
    ) -> SubscribeErrorHandler:
        async def hook(code: int, text: str) -> None:
            self._metrics.inc(f"{self._name}.shard_subscribe_error.{code}")
            if sharded and code not in TERMINAL_CHANNEL_ERROR_CODES:
                # REVIEW MUST-FIX 2026-09-05: EVERY non-ack answer to a
                # sharded subscribe is a refusal — not only the documented
                # validation codes. An unlisted code (6 "Already subscribed":
                # the per-key open question, SUMMARY.md) used to leave this
                # shard CONNECTED but UNSUBSCRIBED for the whole boot with
                # ``connected``/``healthy`` True and 1/N of every RFQ and
                # accept silently invisible. Terminal codes (10/17/25) are
                # excluded: ``_dispatch_control`` reconnects that shard.
                documented = code in SHARDING_REFUSED_CODES
                self._sharding_refused = True
                self._metrics.inc(f"{self._name}.sharding_refused")
                log.warning(
                    "ws_fanout_sharding_refused",
                    name=self._name,
                    shard=k,
                    code=code,
                    error=text,
                    documented=documented,
                    shard_factor=self._shard_factor,
                    detail="the exchange refused a SHARDED communications subscribe "
                    + (
                        "(a documented sharding validation code, 19-22 / 11 — the "
                        "docs, asyncapi-ws.md §3.2, are wrong about something)"
                        if documented
                        else "(an UNLISTED code — 6 'Already subscribed' would mean "
                        "one key cannot hold N communications subscriptions)"
                    )
                    + "; falling back to the single UNSHARDED subscription for the "
                    "rest of this boot — today's behaviour, never a connected-but-"
                    "unsubscribed shard. Investigate before re-arming.",
                )
                self._spawn_lifecycle(
                    self._fallback_unsharded(f"sharding_refused_code_{code}"),
                    f"{self._name}-fallback-unsharded",
                )
            else:
                # Unsharded (today's behaviour: the warning is the whole
                # response) or a terminal code (that shard reconnects).
                log.warning(
                    "ws_shard_subscribe_error",
                    name=self._name,
                    shard=k,
                    code=code,
                    error=text,
                    sharded=sharded,
                    terminal=code in TERMINAL_CHANNEL_ERROR_CODES,
                )
            if base.on_subscribe_error is not None:
                await base.on_subscribe_error(code, text)

        return hook

    async def _fallback_unsharded(self, reason: str) -> None:
        await self.apply_shard_factor(1, reason=reason)

    def _shard_connected_hook(self, k: int) -> LifecycleHandler:
        async def hook() -> None:
            if not any(b.shardable for b in self._base_subs):
                # Nothing to ack on this socket: the connect is the re-ack.
                self._shard_reacked(k)
            for handler in self._user_on_connect:
                try:
                    await handler()
                except Exception:
                    log.exception("ws_connect_handler_failed", name=self._name, shard=k)

        return hook

    def _shard_disconnected_hook(self, k: int) -> LifecycleHandler:
        async def hook() -> None:
            self._metrics.inc(f"{self._name}.shard_disconnect")
            self._reacking.add(k)
            if self._epoch_open:
                # Same loss epoch: some casualty has not re-acked yet, so the
                # consumer's mirror is already invalidated — one rotation.
                self._metrics.inc(f"{self._name}.shard_disconnect_coalesced")
                return
            await self._fire_user_disconnect()

        return hook

    def _shard_reacked(self, k: int) -> None:
        """A shard's subscribe was (re-)acked: it is no longer a casualty. The
        epoch closes when no casualty remains."""
        self._reacking.discard(k)
        if not self._reacking and self._epoch_open:
            self._epoch_open = False
            self._metrics.inc(f"{self._name}.loss_epoch_closed")

    async def _fire_user_disconnect(self) -> None:
        self._epoch_open = True
        for handler in self._user_on_disconnect:
            try:
                await handler()
            except Exception:
                log.exception("ws_disconnect_handler_failed", name=self._name)

    def _shard_of(self, message: JsonDict) -> tuple[WsManager | None, int | None]:
        tag = message.get("_shard")
        if (
            isinstance(tag, int)
            and message.get("_gen") == self._generation
            and 0 <= tag < len(self._shards)
        ):
            return self._shards[tag], tag
        return None, None

    async def _dispatch_control(self, message: JsonDict, msg_type: str) -> bool:
        """Route acks/errors to the socket that owns the command id; apply the
        per-shard recovery rule (module doc). Control frames from a retired
        shard set are dropped: they can neither resolve a live command nor
        report a live channel."""
        if msg_type not in ("subscribed", "error"):
            if msg_type in self._priority_types and len(self._shards) > 1:
                return self._admit_priority_once(message, msg_type)
            return True
        shard, k = self._shard_of(message)
        if shard is None and message.get("_gen") is not None:
            self._metrics.inc(f"{self._name}.retired_control_frame")
            return False
        if msg_type == "subscribed":
            if shard is not None:
                await shard._resolve_subscribed(message)
            return True
        # msg_type == "error"
        log.warning("ws_server_error", name=self._name, shard=k, message=message)
        code = _error_code(message)
        if shard is not None:
            shard._note_server_error(message)
            await shard._resolve_subscribe_error(message)
        if code in TERMINAL_CHANNEL_ERROR_CODES and shard is not None and k is not None:
            return self._shard_channel_lost(k, shard, code, message)
        return True

    def _admit_priority_once(self, message: JsonDict, msg_type: str) -> bool:
        """N > 1 only (module doc): the same quote event carried by two shard
        sockets is dispatched once. Frames without a ``quote_id`` are
        admitted as-is (nothing to key on)."""
        msg = message.get("msg")
        quote_id = msg.get("quote_id") if isinstance(msg, dict) else None
        if not isinstance(quote_id, str) or not quote_id:
            return True
        key = (msg_type, quote_id)
        tag = message.get("_shard")
        shard = tag if isinstance(tag, int) else None
        if key in self._priority_seen:
            self._metrics.inc(f"{self._name}.dup_priority_frame")
            self._metrics.inc(f"{self._name}.dup_priority_frame.{msg_type}")
            log.warning(
                "ws_fanout_duplicate_priority_frame",
                name=self._name,
                msg_type=msg_type,
                quote_id=quote_id,
                shard=shard,
                first_shard=self._priority_seen[key],
                shard_factor=self._shard_factor,
                detail="the exchange delivered the same quote event on more than one "
                "shard socket (BROADCAST, not partition — the SUMMARY.md open "
                "question, answered); dropped here so the confirm path runs once.",
            )
            return False
        self._priority_seen[key] = shard
        capacity = self._lanes.capacity
        while len(self._priority_seen) > capacity:
            self._priority_seen.pop(next(iter(self._priority_seen)))
        return True

    def _shard_channel_lost(self, k: int, shard: WsManager, code: int, message: JsonDict) -> bool:
        """True = forward to the consumer (whole-channel loss → the app's
        cancel_all + force_reconnect); False = recovered here (this shard
        reconnects, the book stands)."""
        self._lost.add(k)
        self._metrics.inc(f"{self._name}.shard_channel_lost.{code}")
        msg = message.get("msg", {})
        detail = msg.get("msg") if isinstance(msg, dict) else None
        if len(self._lost) >= len(self._shards):
            log.error(
                "ws_fanout_channel_lost_all",
                name=self._name,
                code=code,
                detail=detail,
                shard_factor=self._shard_factor,
                shards_lost=sorted(self._lost),
                action="every shard lost its subscription: forwarded to the intake "
                "(cancel_all + reconnect all — the whole-channel rule)",
            )
            return True
        log.warning(
            "ws_shard_channel_lost",
            name=self._name,
            shard=k,
            code=code,
            detail=detail,
            shard_factor=self._shard_factor,
            shards_lost=sorted(self._lost),
            action="reconnect this shard only; open quotes stand; siblings unaffected; "
            "accepts routed here during the gap are lost auctions, never positions",
        )
        self._spawn_lifecycle(shard.force_reconnect(), f"{self._name}-s{k}-reconnect")
        return False


# --------------------------------------------------------------------------- #
# The governor: telemetry, tape, derivation, apply
# --------------------------------------------------------------------------- #


class FanoutGovernor:
    """Owns this boot's evidence and the tape I/O; drives the fan-out.

    ``tick("boot")`` before ``fanout.start()`` derives the initial N from the
    retained tape; ``tick("refresh")`` on the app's slow cadence folds the
    windows, writes telemetry (``ws_inbound_rate``, ``ws_pipe_lag`` + the
    ``pipe_lag_exceeds_confirm_window`` alarm), refreshes the tape off-loop,
    re-derives and applies growth live (shrink deferred to the next boot).
    Never raises into the caller."""

    def __init__(
        self,
        fanout: CommsFanout,
        clock: Clock,
        metrics: Metrics,
        *,
        tape_path: Path,
        data_dir: Path,
        boot_key: str,
        boot_started_at_ts: float,
        confirm_window_s: float,
        override: int | None = None,
        io: Callable[..., Awaitable[PooledEvidence]] | None = None,
        apply_ok: Callable[[], bool] | None = None,
    ) -> None:
        """``apply_ok`` (module doc, the apply gate): a live re-shard is
        applied only while it returns True — the app passes "no accept in
        flight" (``not AcceptPriorityGate.holding()``). None = always."""
        if not (confirm_window_s > 0.0):
            raise ValueError(f"confirm_window_s must be > 0, got {confirm_window_s}")
        self._fanout = fanout
        self._clock = clock
        self._metrics = metrics
        self._tape_path = tape_path
        self._data_dir = data_dir
        self._boot_key = boot_key
        self._boot_started_at_ts = boot_started_at_ts
        self._window_ms = confirm_window_s * 1e3
        self._override = override
        self._io = io
        self._apply_ok = apply_ok
        self.inbound = RateHistogram()
        self.capacity = RateHistogram()
        self.sustained = RateHistogram()
        self.n_windows = 0
        self.n_snapshot = 0
        self.n_violating = 0
        self.shard_factors_used: list[int] = []
        self.last: ShardFactorDerivation | None = None
        self.last_pooled: PooledEvidence | None = None

    async def tick(self, *, reason: str) -> ShardFactorDerivation | None:
        try:
            if reason != "boot":
                self._observe_windows(self._fanout.take_windows(self._clock.monotonic_ns()))
            pooled = await self._refresh_tape()
            self.last_pooled = pooled
            current = self._fanout.shard_factor
            # The shrink gate judges against the factor actually RUNNING: this
            # boot's live set on a refresh, the previous boot's last applied
            # factor at boot (nothing runs yet; ``shard_factor`` is the default).
            gate_current = pooled.last_shard_factor if reason == "boot" else current
            derivation = derive_shard_factor(
                pooled.inbound,
                pooled.capacity,
                n_windows=pooled.n_windows,
                n_violating=pooled.n_violating,
                boots_pooled=pooled.boots,
                override=self._override,
                refused=self._fanout.sharding_refused,
                sustained=pooled.sustained,
                current=gate_current,
                boots_refused=pooled.boots_refused,
            )
            self.last = derivation
            deferred_shrink = False
            apply_deferred: str | None = None
            urgent = self._fanout.sharding_refused and current != 1
            want_apply = (
                reason == "boot"
                or derivation.n > current
                or (self._override is not None and derivation.n != current)
                or urgent
            )
            if (
                want_apply
                and reason != "boot"
                and not urgent
                and self._apply_ok is not None
                and not self._apply_ok()
            ):
                # APPLY GATE (module doc): an accept is in flight — a re-shard
                # would blind the comms path for ~1 s exactly when its
                # ``quote_executed`` is due. Next tick.
                want_apply = False
                apply_deferred = "accept_in_flight"
                self._metrics.inc(f"{self._fanout._name}.apply_deferred")
            if want_apply:
                applied = await self._fanout.apply_shard_factor(
                    derivation.n, reason=f"{reason}:{derivation.source}"
                )
            else:
                applied = current
                deferred_shrink = derivation.n < current
            if applied not in self.shard_factors_used:
                self.shard_factors_used.append(applied)
            self._metrics.inc(f"{self._fanout._name}.fanout_derivation")
            log.info(
                "ws_fanout_derivation",
                reason=reason,
                shard_factor_current=current,
                shard_factor_previous_boot=pooled.last_shard_factor,
                shard_factor_applied=applied,
                deferred_shrink=deferred_shrink,
                apply_deferred=apply_deferred,
                boot_windows=self.n_windows,
                boot_snapshot_windows=self.n_snapshot,
                boot_violating_windows=self.n_violating,
                window_ms=self._window_ms,
                shards=self._fanout.shard_states(),
                **derivation.as_log(),
            )
            return derivation
        except Exception:
            log.exception("ws_fanout_refresh_failed", reason=reason)
            return None

    async def _refresh_tape(self) -> PooledEvidence:
        kwargs: dict[str, Any] = {
            "tape_path": self._tape_path,
            "data_dir": self._data_dir,
            "boot_key": self._boot_key,
            "boot_started_at_ts": self._boot_started_at_ts,
            "boot_inbound": self.inbound.copy(),
            "boot_capacity": self.capacity.copy(),
            "boot_n_windows": self.n_windows,
            "boot_n_snapshot": self.n_snapshot,
            "boot_n_violating": self.n_violating,
            "boot_shard_factors": list(self.shard_factors_used),
            "boot_sustained": self.sustained.copy(),
            "boot_refused": self._fanout.sharding_refused,
        }
        if self._io is not None:
            return await self._io(**kwargs)
        return await asyncio.to_thread(refresh_fanout_tape, **kwargs)

    def _observe_windows(self, windows: list[ShardWindow]) -> None:
        if not windows:
            return
        elapsed = max(w.elapsed_s for w in windows)
        total_frames = sum(w.frames for w in windows)
        total_fps = total_frames / elapsed if elapsed > 0 else 0.0
        snapshot = any(w.snapshot for w in windows)
        rfq = LagHistogram()
        quote = LagHistogram()
        for w in windows:
            rfq.merge(w.lag_rfq)
            quote.merge(w.lag_quote)
        rfq_summary = rfq.summary(self._window_ms)
        quote_summary = quote.summary(self._window_ms)
        name = self._fanout._name
        log.info(
            "ws_inbound_rate",
            name=name,
            total_fps=round(total_fps, 1),
            per_shard_fps=[round(w.fps, 1) for w in windows],
            per_shard_utilization=[round(w.utilization, 4) for w in windows],
            per_shard_shed_lost=[w.shed_lost for w in windows],
            shard_factor=self._fanout.shard_factor,
            elapsed_s=round(elapsed, 1),
            snapshot_window=snapshot,
            depths=self._fanout.lane_depths(),
        )
        log.info(
            "ws_pipe_lag",
            name=name,
            window_ms=self._window_ms,
            rfq_created=None if rfq_summary is None else rfq_summary.as_log(),
            quote_created=None if quote_summary is None else quote_summary.as_log(),
            per_shard_rfq_p50_ms=[
                round(w.lag_rfq.quantile(0.50), 1) if w.lag_rfq.n else None for w in windows
            ],
            shard_factor=self._fanout.shard_factor,
            snapshot_window=snapshot,
            proxy="quote_accepted carries no server timestamp (SUMMARY.md:24); "
            "quote_created (our own quote acks, server created_ts) is the "
            "quote-event-path proxy; rfq_created is the firehose path",
        )
        if rfq_summary is not None:
            self._metrics.observe_ms(f"{name}.pipe_lag.rfq_created.p50_ms", rfq_summary.p50_ms)
        if quote_summary is not None:
            self._metrics.observe_ms(f"{name}.pipe_lag.quote_created.p50_ms", quote_summary.p50_ms)
        if not snapshot and rfq_summary is not None and rfq_summary.p50_ms > self._window_ms:
            self._metrics.inc(f"{name}.pipe_lag.exceeds_confirm_window")
            log.warning(
                "pipe_lag_exceeds_confirm_window",
                name=name,
                p50_ms=round(rfq_summary.p50_ms, 1),
                p90_ms=round(rfq_summary.p90_ms, 1),
                share_over_window=round(rfq_summary.share_over_window, 4),
                window_ms=self._window_ms,
                shard_factor=self._fanout.shard_factor,
                detail="frames reach this process later than the exchange's confirm "
                "window: an accept on this path expires before we can see it. "
                "The lever is more shards; the next derivation carries this window.",
            )
        if snapshot:
            self.n_snapshot += 1
            return
        self.n_windows += 1
        self.inbound.observe(total_fps)
        for w in windows:
            violating = w.violating(self._window_ms)
            if violating:
                self.n_violating += 1
            elif w.frames > 0 and w.elapsed_s > 0:
                # SUSTAINED (the shrink gate's evidence): this connection kept
                # up at this rate — a demonstrated lower bound on its ceiling.
                self.sustained.observe(w.fps)
            cap = w.capacity_fps(self._window_ms)
            if cap is not None:
                self.capacity.observe(cap)
