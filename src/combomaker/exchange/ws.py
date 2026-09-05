"""WebSocket manager: authenticated connect, subscribe, reconnect, health.

Message envelope (docs/api-notes/asyncapi-ws.md): ``{"type": ..., "sid": ...,
"seq": ..., "msg": {...}}``. Commands are ``{"id": <unique int>, "cmd":
"subscribe" | ..., "params": {...}}``. Server pings every 10s (aiohttp
auto-pongs); we treat prolonged silence as unhealthy.

Design rule (stale-line protection): on ANY disconnect or gap the downstream
layers must assume their mirrored state is wrong. This manager guarantees the
ordering: ``on_disconnect`` callbacks (cancel-all lives there) fire BEFORE any
reconnect attempt, and every (re)connect gets fresh subscriptions with new
``sid``s, which downstream layers treat as a full invalidation.

READER ISOLATION (2026-09-05, the code-25 day). The socket is read on a
DEDICATED THREAD with its own event loop; handlers, subscriptions and every
downstream consumer stay on the main loop. Derivation in ``_ReaderThread``.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import enum
import json
import random
import threading
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from combomaker.core.clock import Clock
from combomaker.exchange.auth import RequestSigner
from combomaker.ops.logging import get_logger
from combomaker.ops.metrics import Metrics

log = get_logger(__name__)

JsonDict = dict[str, Any]
MessageHandler = Callable[[JsonDict], Awaitable[None]]
LifecycleHandler = Callable[[], Awaitable[None]]

_WS_HANDSHAKE_PATH = "/trade-api/ws/v2"

# Kalshi's SLOW-CONSUMER error (docs/api-notes/communications-ws.md §errors):
# "Subscription buffer overflow — the subscription's outbound buffer was
# exceeded"; terminal, messages were LOST. A protocol fact, not a knob.
SUBSCRIPTION_BUFFER_OVERFLOW_CODE = 25
# Terminal channel errors (asyncapi-ws.md §3.6 table): the subscription is dead
# and must be re-established — 10 "Channel error", 17 "Internal error", 25 the
# slow-consumer overflow above. Protocol facts, not knobs.
TERMINAL_CHANNEL_ERROR_CODES: frozenset[int] = frozenset({10, 17, 25})
# Communications FAN-OUT SHARDING (asyncapi-ws.md §3.2 + error table 19-22,
# communications-ws.md:57-67 "run one connection per shard_key"): subscribe
# params ``shard_factor`` (1..SHARD_FACTOR_MAX) / ``shard_key`` (0..factor-1);
# a malformed pair is refused with codes 19-22. The cap is the documented one.
SHARD_FACTOR_MAX = 100
SHARD_VALIDATION_ERROR_CODES: frozenset[int] = frozenset({19, 20, 21, 22})


SubscribedHandler = Callable[[int], Awaitable[None]]  # receives the new sid
SubscribeErrorHandler = Callable[[int, str], Awaitable[None]]  # (code, text) of a refusal
# Reader-thread frame observer: ``(message, recv_mono_ns, handling_ns)`` —
# called AFTER the lane push, on the reader thread; must never log or block.
FrameObserver = Callable[[JsonDict, int, int], None]

# Transport seam: ``(session, url, headers) -> async context manager yielding
# a socket``. Production binds aiohttp's ``ws_connect``; the replay harness and
# the unit suite bind a recorded/fake socket so the SAME reader thread, lanes
# and dispatcher run against tape frames (rule 8: drive the live module, never
# reimplement it).
Connector = Callable[[aiohttp.ClientSession, str, dict[str, str]], Any]


def _aiohttp_connect(session: aiohttp.ClientSession, url: str, headers: dict[str, str]) -> Any:
    return session.ws_connect(
        # Liveness via receive_timeout, NOT a client heartbeat.
        # `heartbeat=10.0` (tried 2026-07-13) made aiohttp send its
        # own client Pings; Kalshi's response to unsolicited client
        # pings is UNDOCUMENTED (docs/api-notes/asyncapi-ws.md §2 +
        # open-question list) and empirically the socket then died
        # clean every ~22s (11 reconnects/5min) vs ~11min with
        # heartbeat off — so every RFQ closed before our quote POST
        # landed. Reverted to heartbeat=None.
        #
        # receive_timeout=25s replaces it as an ACTIVE half-dead-peer
        # probe that keys off Kalshi's DOCUMENTED server ping (every
        # 10s, §2). Verified in aiohttp 3.14.1 client_ws.receive():
        # the receive_timeout wraps each reader.read() and a PING/PONG
        # frame `continue`s the loop → re-arms a fresh timeout, so any
        # frame (incl. server pings) resets it. It therefore fires
        # ONLY on a genuinely silent peer (no data AND no ping for
        # 25s) → raises TimeoutError → clean logged reconnect, never
        # the 30s-silence→data_stale→halt hang. 25s > 2×10s ping (no
        # false-trip on a quiet market) and < the breaker's 30s
        # data_stale grace (reconnect wins the race, no hard halt).
        # autoping=True still Pongs Kalshi's server pings.
        url,
        headers=headers,
        autoping=True,
        heartbeat=None,
        receive_timeout=25.0,
    )


@dataclass
class _Subscription:
    channels: list[str]
    params_extra: dict[str, Any] = field(default_factory=dict)
    on_subscribed: SubscribedHandler | None = None
    on_subscribe_error: SubscribeErrorHandler | None = None


class Lane(enum.Enum):
    """Frame class, decided by TYPE at read time (registration decides the
    class; the reader only looks it up)."""

    PRIORITY = "priority"  # exchange-deadlined (accept/executed): never dropped
    CONTROL = "control"  # acks, errors, everything unclassified: never dropped
    MARKET = "market"  # ``mark_sheddable``: individually recoverable


class _Verdict(enum.Enum):
    QUEUED = "queued"
    SHED = "shed"  # queued; the OLDEST market frame was dropped to make room
    RUNAWAY = "runaway"  # a never-drop lane is full: genuine runaway, fail closed


class _Lanes:
    """The ONE buffer between the socket reader (its own thread) and the
    main-loop dispatcher. Every operation is O(1) under a plain lock, so
    neither side ever waits on the other for more than a deque op.

    Three lanes, drained PRIORITY → CONTROL → MARKET (2026-07-31 priority lane
    + 2026-08-01 shed class, now made explicit as lanes instead of one FIFO
    with a carry deque):

      * PRIORITY — ``mark_priority`` types (quote_accepted/quote_executed).
        Never dropped. Overflow = protocol breakdown (accepts are tens/day;
        ``capacity`` queued means the exchange is flooding us with order
        events we cannot process) ⇒ fail-closed reconnect, unchanged.
      * CONTROL — everything that is neither priority nor market: subscribe
        acks, ``error`` frames, ``quote_created`` acks, and — on the BOOK
        socket, which marks nothing sheddable — every orderbook frame.
        Never dropped: a full control lane is the original genuine-runaway
        signal ⇒ fail-closed reconnect (seq-dependent deltas must never be
        shed; only reconnect+resnapshot is sound there). Byte-identical
        semantics for the book socket.
      * MARKET — ``mark_sheddable`` types (the rfq_created firehose +
        deletions). Capacity overflow drops the OLDEST market frame and the
        socket STAYS CONNECTED (2026-08-01 derivation, unchanged); the
        dispatcher additionally drops a market frame whose WIRE AGE passed
        its declared stale bound at dequeue (the frame's auction is one the
        worker-side dwell gate would refuse anyway — see ``mark_sheddable``).

    Draining control ahead of market is the SAME reordering the 2026-08-01
    carry lane already declared safe ("control frames like subscribed acks
    carry no seq dependency on market frames"), applied uniformly instead
    of only when a shed displaced them. FIFO is preserved within each lane.

    The reader must never touch ``Metrics`` (its dict counters are not
    thread-safe — ``+=`` is three bytecodes): counts accumulate here under
    the lock and the dispatcher folds them into ``Metrics`` on the main loop.
    """

    __slots__ = (
        "_lock",
        "capacity",
        "priority",
        "control",
        "market",
        "wake_pending",
        "pending_metrics",
    )

    def __init__(self, capacity: int) -> None:
        self._lock = threading.Lock()
        self.capacity = capacity
        self.priority: collections.deque[JsonDict] = collections.deque()
        self.control: collections.deque[JsonDict] = collections.deque()
        self.market: collections.deque[JsonDict] = collections.deque()
        # True while a wake has been scheduled on the main loop and the
        # dispatcher has not yet observed the lanes empty. Coalesces the
        # per-frame cross-thread wakeups into one per burst.
        self.wake_pending = False
        self.pending_metrics: dict[str, int] = {}

    def count(self, name: str, by: int = 1) -> None:
        with self._lock:
            self.pending_metrics[name] = self.pending_metrics.get(name, 0) + by

    def take_metrics(self) -> dict[str, int]:
        with self._lock:
            if not self.pending_metrics:
                return {}
            taken = self.pending_metrics
            self.pending_metrics = {}
            return taken

    def push(self, message: JsonDict, lane: Lane) -> tuple[_Verdict, str, int | None, bool]:
        """Append ``message`` to ``lane``. Returns ``(verdict, shed_type,
        shed_shard, wake_needed)``: ``shed_type`` names the dropped frame's
        type and ``shed_shard`` its ``_shard`` stamp (None unsharded) when the
        verdict is SHED; ``wake_needed`` is True exactly once per burst (the
        caller schedules the main-loop wake)."""
        with self._lock:
            if lane is Lane.MARKET:
                shed_type = ""
                shed_shard: int | None = None
                verdict = _Verdict.QUEUED
                if len(self.market) >= self.capacity:
                    dropped = self.market.popleft()
                    shed_type = str(dropped.get("type", ""))
                    tag = dropped.get("_shard")
                    shed_shard = tag if isinstance(tag, int) else None
                    verdict = _Verdict.SHED
                self.market.append(message)
            else:
                target = self.priority if lane is Lane.PRIORITY else self.control
                if len(target) >= self.capacity:
                    return _Verdict.RUNAWAY, "", None, False
                target.append(message)
                shed_type = ""
                shed_shard = None
                verdict = _Verdict.QUEUED
            wake = not self.wake_pending
            self.wake_pending = True
            return verdict, shed_type, shed_shard, wake

    def purge_market(self, shard: int) -> int:
        """Drop every MARKET frame stamped ``_shard == shard``: a follower
        socket died and its queued auctions are re-dumped by its own
        resubscribe (the 2026-07-14 discard rule, scoped to ONE socket of a
        shared lane set). Priority and control frames are kept — an accept is
        an accept whichever socket carried it, and a stale ack self-drops at
        the shard that owned the command id. O(n) under the lock, once per
        disconnect."""
        with self._lock:
            kept = collections.deque(m for m in self.market if m.get("_shard") != shard)
            dropped = len(self.market) - len(kept)
            self.market = kept
            return dropped

    def pop(self, lane: Lane) -> JsonDict | None:
        target = (
            self.priority
            if lane is Lane.PRIORITY
            else self.control
            if lane is Lane.CONTROL
            else self.market
        )
        with self._lock:
            return target.popleft() if target else None

    def settle_idle(self) -> bool:
        """Under the lock: if every lane is empty, clear ``wake_pending`` and
        report True (the dispatcher may sleep — the next push re-arms a wake).
        A push that landed since the last pop leaves the flag set and returns
        False so the dispatcher keeps draining."""
        with self._lock:
            if self.priority or self.control or self.market:
                return False
            self.wake_pending = False
            return True

    def clear(self) -> int:
        with self._lock:
            dropped = len(self.priority) + len(self.control) + len(self.market)
            self.priority.clear()
            self.control.clear()
            self.market.clear()
            return dropped

    def depths(self) -> dict[str, int]:
        with self._lock:
            return {
                "priority": len(self.priority),
                "control": len(self.control),
                "market": len(self.market),
            }


class WsManager:
    # Per-lane backlog bound. Sized for the BOOT SNAPSHOT: on communications
    # subscribe Kalshi dumps every open RFQ (observed ~650 msgs/s for several
    # seconds, 2026-07-14) while the dispatcher drains at ~300/s (per-RFQ SQLite
    # + REST metadata) — a 2,000 cap overflowed in 3.1s and the close→reconnect
    # →fresh-snapshot cycle looped 107×. 20,000 absorbs the burst (peak backlog
    # ~2-5k, drained in ~10-20s while the reader keeps answering server pings);
    # steady state is single-digit msgs/s. Overflow ⇒ genuine runaway ⇒
    # reconnect (fail-closed), never silently lag the mirrored books — EXCEPT
    # for frames explicitly classed MARKET-DATA via ``mark_sheddable``, which
    # shed oldest-first instead (2026-08-01 pregame-surge deafness; derivation
    # at ``_Lanes``).
    _QUEUE_MAX = 20_000

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
        lanes: _Lanes | None = None,
        wake: Callable[[], None] | None = None,
        dispatch: bool = True,
        reader: bool = True,
        shard_tag: int | None = None,
        shard_gen: int = 0,
        on_frame: FrameObserver | None = None,
    ) -> None:
        """Every keyword after ``connect`` defaults to the single-socket manager
        this class has always been. They exist for COMMUNICATIONS FAN-OUT
        SHARDING (2026-09-05, ``exchange/ws_fanout.py``): one manager per
        exchange shard, each with its OWN reader thread and socket, ALL pushing
        into ONE shared ``_Lanes`` drained by ONE dispatcher — so the priority
        lane is still drained first across every socket and the market lane's
        capacity/age shedding is unchanged.

        * ``lanes`` — share another manager's lanes instead of owning a set.
        * ``wake`` — wake THAT manager's dispatcher (this one runs none).
        * ``dispatch=False`` — FOLLOWER: reader thread only, no dispatch task,
          no metric flush/log lines (the lanes' owner writes them).
        * ``reader=False`` — HOST: dispatcher only, no socket of its own.
        * ``shard_tag``/``shard_gen`` — stamped on every frame as ``_shard`` /
          ``_gen`` so the host can route acks/errors to the socket that owns
          the command id and purge a dead socket's market frames without
          touching its siblings'. ``_gen`` retires frames of a replaced set.
        * ``on_frame`` — reader-thread observer (rate / pipe-lag meter).
        """
        self._url = url
        self._signer = signer
        self._clock = clock
        self._metrics = metrics or Metrics()
        self._name = name
        self._max_silence_s = max_silence_s
        self._backoff_initial_s = backoff_initial_s
        self._backoff_max_s = backoff_max_s
        self._connect = connect
        self._wake_target = wake
        self._dispatch_enabled = dispatch
        self._reader_enabled = reader
        self._shard_tag = shard_tag
        self._shard_gen = shard_gen
        self._on_frame = on_frame
        # Shed metrics are a property of the LANE SET, so a follower counts
        # them under its lanes' owner's name (identical to ``name`` when the
        # manager owns its lanes — today's metric names, byte for byte).
        self._lane_owner_name = name
        self._started = False
        # Reader-thread wall time from receive stamp to lane push, cumulative
        # (parse + classify + push + any GIL wait inside that window). Main
        # loop reads it as a delta per measurement window.
        self._busy_ns = 0

        self._handlers: dict[str, list[MessageHandler]] = {}
        self._on_disconnect: list[LifecycleHandler] = []
        self._on_connect: list[LifecycleHandler] = []
        self._subscriptions: list[_Subscription] = []
        self._pending_sub_acks: dict[int, _Subscription] = {}
        self._live_sub_tasks: set[asyncio.Task[None]] = set()
        self._lifecycle_tasks: set[asyncio.Task[None]] = set()
        self._cmd_id = 0
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        # Main-loop-owned: the socket whose subscription snapshot has been
        # taken (``_after_connect``). Live ``add_subscription`` sends only
        # while this IS the current socket — see ``add_subscription``.
        self._subscribed_ws: aiohttp.ClientWebSocketResponse | None = None
        self._last_rx_mono_ns: int | None = None
        self._frames_read = 0  # reader-thread writes, main-loop reads (diagnostics)
        self._stopping = False
        self._force_reconnecting = False
        # PONG-STARVATION FIX (2026-07-14, root cause of the ~90-150s server-side
        # closes): aiohttp answers Kalshi's 10s server Pings INSIDE receive() —
        # i.e. only while the read loop is actually reading. The old read loop
        # awaited every handler INLINE (incl. RFQ pricing + the REST create_quote
        # round trip), so an RFQ burst stalled receive() for seconds, Pongs went
        # out late, and after ~9-15 missed pings the server closed us (silent,
        # load-correlated, read-alive/write-dead — all four live symptoms; Kalshi
        # docs: connection-keep-alive.md "Clients should respond with Pong").
        # The read loop ONLY reads + enqueues; a single long-lived dispatcher
        # task consumes IN ORDER (seq continuity per sid is preserved by FIFO
        # within a lane).
        #
        # READER ISOLATION (2026-09-05). Read+enqueue-only was necessary but
        # not sufficient: the reader was still a coroutine ON THE MAIN LOOP,
        # so any synchronous stretch of main-loop work (a maintenance pass
        # crossing 60s on the 213 GB store, a reprice sweep, JSON telemetry)
        # stopped it reading at all. TCP flow control then backed the
        # exchange up until ITS per-subscription buffer overflowed — error
        # code 25 "Subscription buffer overflow", measured 2026-09-05 at
        # 0.7/h overnight rising to ~1/min on a fresh boot with nothing else
        # on the box, each one discarding every queued auction AND (on the
        # 3 confirm halts that day) delivering accepts after the taker's
        # window had lapsed. The socket is now read on its OWN THREAD with
        # its own event loop (``_ReaderThread``): frames are parsed there and
        # handed over through ``_Lanes``; the main loop can stall for a
        # minute and the socket is still drained, so an overflow — if any —
        # happens on OUR side, in the lane that can afford it (MARKET, oldest
        # first), never at the exchange. Code 25 can therefore no longer
        # follow from a main-loop stall WHILE THE READER THREAD GETS THE GIL
        # (Python code, SQLite and time.sleep all release it; a C extension
        # holding it for the stall's length would still starve the reader);
        # it is logged CRITICAL if it ever recurs (``_note_server_error``) so
        # the next cause is visible at once.
        self._lanes = lanes if lanes is not None else _Lanes(self._QUEUE_MAX)
        self._wake = asyncio.Event()
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._reader: _ReaderThread | None = None
        self._dispatch_task: asyncio.Task[None] | None = None
        # PRIORITY LANE (2026-07-31, the double confirm-expiry halt). The
        # dispatcher is FIFO within a lane, so a rare-but-deadlined frame
        # (quote_accepted: the exchange gives 3.0s to confirm, a protocol
        # fact — rfq/lifecycle.py EXCHANGE_CONFIRM_WINDOW_S) must never sit
        # behind thousands of firehose frames. Measured 2026-07-31 (report of
        # the same date): on all 12 expired confirms ever, the in-handler time
        # was <= 1.14s of the 3.0s window — >= 1.86s died UPSTREAM, in the
        # backlog, while the comms channel carried ~500 frames/s sustained.
        # Frames whose type is marked priority route to their own lane, which
        # the dispatcher fully drains BEFORE dispatching each normal frame, so
        # a priority frame waits for AT MOST ONE normal dispatch (~ms), never
        # the backlog. The marked types (quote lifecycle events keyed by
        # quote_id) carry no seq dependency on the rfq stream.
        self._priority_types: frozenset[str] = frozenset()
        # MARKET-DATA SHED CLASS (2026-08-01 pregame-surge deafness). The
        # fail-closed overflow⇒reconnect assumed overflow = "genuine
        # runaway", i.e. transient. The 2026-08-01 pregame surge (12:08–13:06
        # ET, the ONLY window this pregame-only bot fills in) broke the
        # assumption: comms inflow exceeded the real-parse drain CONTINUOUSLY
        # for 62+ minutes — measured from the live tape's 14 overflow cycles:
        # the 20k queue refilled connect→overflow in 75s..1480s (net
        # accumulation 14–268 frames/s, worsening into first pitch), 276,644
        # frames discarded, and every reconnect discarded EVERY queued
        # rfq_created then re-entered the same saturated regime — ZERO new-RFQ
        # intake for most of 90 minutes while quoting continued. No finite
        # queue "fits" a sustained inflow>drain regime, so the repair is a
        # POLICY split by frame class, not a bigger number (``_Lanes``):
        # market-data frames are individually recoverable — a dropped
        # rfq_created is ONE missed auction and the next arrives in seconds,
        # while a disconnect drops ALL queued auctions AND the connection.
        # ``_stale_after_ns`` adds the 2026-09-05 age axis: a market frame
        # whose wire age passed the consumer's declared freshness horizon
        # is dropped at dequeue (metrics ``ws.stale_market_frames`` /
        # ``ws.stale.<type>``) — after a main-loop stall the lane collapses
        # to its live tail at subtraction cost instead of dispatching dead
        # auctions through parse + fan-out.
        self._sheddable_types: frozenset[str] = frozenset()
        self._stale_after_ns: dict[str, int] = {}
        # Main-loop-only aggregation state for the shed / stale-drop lines
        # (the reader thread never logs periodically — ``_flush_reader_metrics``).
        self._shed_pending: dict[str, int] = {}
        self._shed_last_log_mono_ns: int | None = None
        self._stale_pending: dict[str, int] = {}
        self._stale_last_log_mono_ns: int | None = None

    # --- registration (all before start) ---

    def on_message(self, msg_type: str, handler: MessageHandler) -> None:
        """Register a handler for a message ``type`` ('*' = every message)."""
        self._handlers.setdefault(msg_type, []).append(handler)

    def mark_priority(self, *msg_types: str) -> None:
        """Route frames of these types around the dispatch backlog.

        For rare frames with an EXCHANGE deadline (accept→confirm). Register
        before ``start()``; handlers are unchanged — only queueing order moves.
        """
        overlap = self._sheddable_types & frozenset(msg_types)
        if overlap:
            raise ValueError(f"sheddable types can never be priority: {sorted(overlap)}")
        self._priority_types = self._priority_types | frozenset(msg_types)

    def mark_sheddable(self, *msg_types: str, stale_after_s: float | None = None) -> None:
        """Declare MARKET-DATA frame types: on a full market lane the OLDEST
        such frame is dropped instead of disconnecting the socket (derivation
        at ``_Lanes``). Register before ``start()``. Never mark seq-dependent
        streams (orderbook deltas) or exchange-deadlined frames (those go to
        ``mark_priority``).

        ``stale_after_s``: the consumer's OWN freshness horizon for these
        frames (the caller passes the constant its worker-side dwell gate
        already refuses on — no new number). A frame whose wire age
        (``_recv_mono_ns``, stamped off the socket) exceeds it at dequeue is
        dropped by the dispatcher without a handler call. ``None`` = ageless
        (deletions: mirror consistency is cheap and never stale)."""
        overlap = self._priority_types & frozenset(msg_types)
        if overlap:
            raise ValueError(f"priority types can never be sheddable: {sorted(overlap)}")
        if stale_after_s is not None and stale_after_s <= 0.0:
            raise ValueError(f"stale_after_s must be > 0, got {stale_after_s}")
        self._sheddable_types = self._sheddable_types | frozenset(msg_types)
        for msg_type in msg_types:
            if stale_after_s is None:
                self._stale_after_ns.pop(msg_type, None)
            else:
                self._stale_after_ns[msg_type] = int(stale_after_s * 1e9)

    def on_disconnect(self, handler: LifecycleHandler) -> None:
        self._on_disconnect.append(handler)

    def on_connect(self, handler: LifecycleHandler) -> None:
        """Fires after (re)connect, BEFORE subscriptions are re-sent."""
        self._on_connect.append(handler)

    def add_subscription(
        self,
        channels: list[str],
        *,
        on_subscribed: SubscribedHandler | None = None,
        on_subscribe_error: SubscribeErrorHandler | None = None,
        **params_extra: Any,
    ) -> None:
        """Declare a desired subscription; sent NOW if connected and re-sent
        on every (re)connect (lazily watched RFQ legs arrive mid-session).

        ``on_subscribed`` fires with the server-assigned sid on every (re)ack —
        sids change across reconnects, so consumers must re-key their state.
        ``on_subscribe_error`` fires with ``(code, text)`` when the exchange
        answers the subscribe command with an ``error`` frame echoing its id
        (the fan-out's sharding fallback hangs off this; unset = today).
        """
        sub = _Subscription(list(channels), dict(params_extra), on_subscribed, on_subscribe_error)
        self._subscriptions.append(sub)
        # LIVE-SEND ONLY ONCE THE CURRENT SOCKET HAS TAKEN ITS SNAPSHOT
        # (review 2026-09-05). The reader publishes ``_ws`` the instant a
        # socket is up, but ``_after_connect`` snapshots ``_subscriptions``
        # one or more main-loop iterations later (after the on_connect
        # handlers). A subscription added inside that window was sent live
        # AND again by the snapshot (Kalshi code 6 "Already subscribed" or a
        # second sid + a leaked pending ack). ``_subscribed_ws`` is set by
        # ``_after_connect`` on the main loop in the same step as its
        # snapshot: a sub added before it rides the snapshot, one added after
        # it is sent live — exactly one subscribe per socket either way.
        ws = self._ws  # reader-thread written: read once
        if ws is not None and not ws.closed and self._subscribed_ws is ws:
            task = asyncio.create_task(
                self._send_subscription_now(sub), name=f"{self._name}-live-subscribe"
            )
            self._live_sub_tasks.add(task)
            task.add_done_callback(self._live_sub_tasks.discard)

    # --- health ---

    @property
    def connected(self) -> bool:
        # ``_ws`` is written by the READER THREAD (socket up / socket died).
        # Read it exactly once: two reads can straddle a socket death and
        # raise AttributeError on the quoting path (``feed.watch`` →
        # ``add_subscription``; the last look's ``rx_age_s``) — review
        # 2026-09-05 must-fix; main's single-loop code had no such race.
        ws = self._ws
        return ws is not None and not ws.closed

    @property
    def healthy(self) -> bool:
        """Connected with traffic inside the silence budget (server pings @10s)."""
        age = self.last_rx_age_s
        return age is not None and age <= self._max_silence_s

    @property
    def last_rx_age_s(self) -> float | None:
        """Seconds since ANY server traffic; the freshness proof for mirrored
        state (a live seq-continuous stream means books are current NOW even
        when quiet). None when disconnected."""
        last = self._last_rx_mono_ns  # reader-thread written: read once
        if last is None or not self.connected:
            return None
        return (self._clock.monotonic_ns() - last) / 1e9

    def lane_depths(self) -> dict[str, int]:
        """Current handoff backlog per lane (diagnostics / the status line)."""
        return self._lanes.depths()

    # --- lifecycle ---

    def start(self) -> None:
        if self._reader is not None or self._dispatch_task is not None or self._started:
            raise RuntimeError("already started")
        self._started = True
        self._stopping = False
        self._main_loop = asyncio.get_running_loop()
        if self._dispatch_enabled:
            self._dispatch_task = asyncio.create_task(
                self._dispatch_loop(), name=f"{self._name}-dispatch"
            )
        if self._reader_enabled:
            self._reader = _ReaderThread(self)
            self._reader.start()

    async def force_reconnect(self) -> None:
        """Close the socket; the reader reconnects and resubscribes.

        For terminal channel errors (codes 10/17/25) where the subscription is
        dead but the connection may look healthy, AND for a WRITE-DEAD socket (a
        ``send`` raising ClientConnectionResetError while the read side is still
        alive, so receive_timeout can't catch it). Reentrancy-guarded: a burst of
        failed writes triggers exactly ONE reconnect; the guard clears when the
        reader establishes the next socket.
        """
        if self._force_reconnecting:
            return
        self._force_reconnecting = True
        ws = self._ws
        if ws is not None and not ws.closed:
            await self._on_socket_loop(ws.close())

    async def stop(self) -> None:
        self._stopping = True
        ws = self._ws
        if ws is not None and not ws.closed:
            with contextlib.suppress(Exception):
                await self._on_socket_loop(ws.close())
        reader = self._reader
        if reader is not None:
            await reader.stop()
            self._reader = None
        for task in list(self._lifecycle_tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._dispatch_task is not None:
            # Drain what's already queued (handlers may hold cleanup state),
            # then cancel. Waiting for an empty lane set would hang if a
            # handler stalls — bound it (same 0.1s the queue join used).
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._lanes_empty(), timeout=0.1)
            self._dispatch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._dispatch_task
            self._dispatch_task = None
        self._flush_reader_metrics()
        self._started = False

    async def _lanes_empty(self) -> None:
        """Resolve once the dispatcher has drained every lane (stop() only)."""
        while any(self._lanes.depths().values()):  # noqa: ASYNC110 — bounded by wait_for
            await asyncio.sleep(0.01)

    # --- cross-loop plumbing ---

    async def _on_socket_loop(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Run ``coro`` on the loop that OWNS the socket. aiohttp objects are
        bound to the loop that created them; the socket lives on the reader
        thread's loop, so every ``send``/``close`` from the main loop is
        marshalled there. When the socket loop IS the current loop (a
        manager never started, or the reader thread itself), run inline."""
        reader = self._reader
        loop = reader.loop if reader is not None else None
        if loop is None or loop is asyncio.get_running_loop():
            return await coro
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()  # the socket loop is shutting down — nothing to send on
            raise
        return await asyncio.wrap_future(fut)

    def _schedule_wake(self) -> None:
        """Reader side (any thread): wake the dispatcher exactly once per burst."""
        if self._wake_target is not None:
            self._wake_target()  # FOLLOWER: the lanes' owner runs the dispatcher
            return
        main = self._main_loop
        if main is None:
            return  # no dispatcher yet — it checks the lanes on entry
        with contextlib.suppress(RuntimeError):  # main loop closed: shutting down
            main.call_soon_threadsafe(self._wake.set)

    def _spawn_lifecycle(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        """Main loop only: run a connect/disconnect follow-up as a tracked task."""
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._lifecycle_tasks.add(task)
        task.add_done_callback(self._lifecycle_tasks.discard)

    def _socket_connected(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Main-loop callback posted by the reader on a fresh socket."""
        self._spawn_lifecycle(self._after_connect(ws), f"{self._name}-after-connect")

    def _socket_disconnected(self, ack: asyncio.Future[None], reader_loop: Any) -> None:
        """Main-loop callback posted by the reader when a socket died. The
        reader waits on ``ack`` before reconnecting (stale-line rule)."""
        self._spawn_lifecycle(
            self._after_disconnect(ack, reader_loop), f"{self._name}-after-disconnect"
        )

    async def _after_connect(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._flush_reader_metrics()
        if ws is not self._ws:
            return  # a stale connect event: that socket already died
        for handler in self._on_connect:
            try:
                await handler()
            except Exception:
                log.exception("ws_connect_handler_failed", name=self._name)
        # From here on live ``add_subscription`` calls send on THIS socket;
        # everything declared before this step is in the snapshot below (the
        # ``list(...)`` copy is taken synchronously in the same step, before
        # the first await) — one subscribe per socket, never two.
        self._subscribed_ws = ws
        try:
            await self._send_subscriptions(ws)
        except Exception as exc:
            # The reader owns recovery: a dead socket comes back as a
            # disconnect event and the next connect re-sends everything.
            log.warning("ws_subscribe_after_connect_failed", name=self._name, error=repr(exc))

    async def _after_disconnect(self, ack: asyncio.Future[None], reader_loop: Any) -> None:
        try:
            # Discard the dead connection's backlog BEFORE reconnect: stale
            # frames would self-drop downstream anyway (dead sids) but they
            # occupy lane slots + dispatcher time, and a still-full lane
            # re-overflows INSTANTLY on the next connect (observed live
            # 2026-07-14: 107-cycle overflow→reconnect loop). The new
            # connection re-snapshots everything, so nothing queued from the
            # old one is load-bearing.
            self._subscribed_ws = None
            self._discard_queued()
            self._flush_reader_metrics()
            # Disconnect: notify (cancel-all etc.) BEFORE any reconnect.
            self._metrics.inc(f"{self._name}.disconnect")
            log.warning("ws_disconnected", name=self._name)
            for handler in self._on_disconnect:
                try:
                    await handler()
                except Exception:
                    log.exception("ws_disconnect_handler_failed", name=self._name)
        finally:
            with contextlib.suppress(RuntimeError):  # reader loop already gone
                reader_loop.call_soon_threadsafe(_resolve_quietly, ack)

    def _flush_reader_metrics(self) -> None:
        """Main loop only: fold the reader thread's counts into ``Metrics``
        and write the aggregated ``ws_shed_market_frames`` line.

        THE READER THREAD NEVER LOGS PERIODICALLY (review 2026-09-05
        must-fix). ``tools/ops/hang_watchdog.py`` treats ANY advance of the
        live log as main-loop work and declares a stall only when the log
        AND the store are both quiet past its threshold (live: 242 s). A
        reader that wrote a line every ``max_silence_s`` while the market
        lane stayed full (~500 frames/s fills it in ~40 s) would keep the
        log axis alive through a PERMANENT main-loop hang — the 45 h-outage
        class the watchdog exists for — and nothing would relight. So shed
        counts cross as metrics (``<name>.shed.<type>``) and the line is
        written HERE by the dispatcher: a stalled loop writes nothing, which
        is exactly what the watchdog must see. Aggregation is unchanged:
        first shed of a burst logs on the first flush, further sheds
        accumulate for at most ``max_silence_s`` (the existing liveness
        window — no new number).

        A FOLLOWER (``dispatch=False``) never flushes: its counts land in the
        shared lanes' pending metrics under the owner's name and the owner's
        dispatcher folds and logs them."""
        if not self._dispatch_enabled:
            return
        shed_prefix = f"{self._name}.shed."
        for name, by in self._lanes.take_metrics().items():
            self._metrics.inc(name, by)
            if name.startswith(shed_prefix):
                msg_type = name[len(shed_prefix) :]
                self._shed_pending[msg_type] = self._shed_pending.get(msg_type, 0) + by
        if not self._shed_pending:
            return
        now = self._clock.monotonic_ns()
        last = self._shed_last_log_mono_ns
        if last is None or (now - last) / 1e9 >= self._max_silence_s:
            log.warning(
                "ws_shed_market_frames",
                name=self._name,
                shed=dict(self._shed_pending),
                depths=self._lanes.depths(),
            )
            self._shed_pending.clear()
            self._shed_last_log_mono_ns = now

    # --- reader side (runs on the socket loop, i.e. the reader thread) ---

    def _lane_for(self, msg_type: str) -> Lane:
        if msg_type in self._priority_types:
            return Lane.PRIORITY
        if msg_type in self._sheddable_types:
            return Lane.MARKET
        return Lane.CONTROL

    async def _read_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        # READ + ENQUEUE ONLY — never await handlers here. Staying inside
        # ws.receive() is what lets aiohttp answer Kalshi's 10s server Pings
        # promptly (autoping replies during receive()); awaiting slow handlers
        # inline starved the Pongs and got us server-closed every ~90-150s.
        # Runs on the socket loop: a main-loop stall cannot reach this loop.
        async for frame in ws:
            self._last_rx_mono_ns = self._clock.monotonic_ns()
            self._frames_read += 1
            if frame.type == aiohttp.WSMsgType.TEXT:
                try:
                    message: JsonDict = json.loads(frame.data)
                except ValueError:
                    log.warning("ws_bad_json", name=self._name, data=frame.data[:200])
                    continue
                # WIRE-RECEIVE STAMP — every frame (2026-08-01, was
                # priority-only since 2026-07-31). The exchange's clock starts
                # BEFORE a frame reaches us, so any downstream freshness or
                # deadline decision must anchor at the earliest instant this
                # process can observe — right here, off the socket — not at
                # handler start. Priority frames feed it to the derived
                # confirm budget; market frames feed the dispatcher's stale
                # drop and the intake's pre-parse staleness gate. The stamp
                # rides the envelope (server fields never start with "_") and
                # reuses the monotonic read taken two lines up.
                recv_ns = self._last_rx_mono_ns
                message["_recv_mono_ns"] = recv_ns
                if self._shard_tag is not None:
                    # FAN-OUT stamps (2026-09-05): the socket that carried the
                    # frame and the shard-set generation, so the host routes
                    # acks/errors to the owning socket and a frame queued by a
                    # retired shard set never resolves a new shard's command.
                    message["_shard"] = self._shard_tag
                    message["_gen"] = self._shard_gen
                lane = self._lane_for(str(message.get("type", "")))
                if lane is Lane.PRIORITY:
                    self._lanes.count(f"{self._name}.priority_frame")
                verdict, shed_type, shed_shard, wake = self._lanes.push(message, lane)
                if verdict is _Verdict.RUNAWAY:
                    # A never-drop lane is full: the genuine-runaway signal
                    # (order events or control/book frames ``capacity`` deep).
                    # Fail closed by reconnecting (books re-snap) rather than
                    # silently falling behind — unchanged since 2026-07-14.
                    self._lanes.count(f"{self._name}.dispatch_queue_overflow")
                    log.error(
                        "ws_dispatch_queue_overflow",
                        name=self._name,
                        lane=lane.value,
                        depths=self._lanes.depths(),
                    )
                    await ws.close()
                    return
                if verdict is _Verdict.SHED:
                    self._record_shed(shed_type, shed_shard)
                if wake:
                    self._schedule_wake()
                # HANDLING TIME (fan-out capacity evidence, 2026-09-05): wall
                # time this thread spent from the receive stamp to the push —
                # parse, classification, push and any GIL wait inside that
                # window. One more monotonic read per frame; the observer (if
                # any) counts on its own lock and never logs.
                done_ns = self._clock.monotonic_ns()
                self._busy_ns += done_ns - recv_ns
                if self._on_frame is not None:
                    self._on_frame(message, recv_ns, done_ns - recv_ns)
            elif frame.type == aiohttp.WSMsgType.ERROR:
                log.warning("ws_frame_error", name=self._name)
                return

    def _record_shed(self, msg_type: str, shard: int | None = None) -> None:
        """Reader side: COUNT a capacity shed, never log it. The counts cross
        to the main loop with the other reader metrics and the dispatcher
        writes one aggregated ``ws_shed_market_frames`` line per
        ``max_silence_s`` (``_flush_reader_metrics``): a surge sheds
        hundreds/s, and a line from THIS thread would keep the hang
        watchdog's log axis alive through a main-loop hang. The reader's
        remaining log lines are one-shot per socket (connect, error,
        runaway-close) or per malformed frame — never periodic.

        Counted under the LANE OWNER's name (== ``name`` unsharded); a
        fan-out additionally attributes the loss to the socket whose frame
        was dropped (``<owner>.s<k>.shed_lost``)."""
        owner = self._lane_owner_name
        self._lanes.count(f"{owner}.shed_market_frames")
        self._lanes.count(f"{owner}.shed.{msg_type}")
        if shard is not None:
            self._lanes.count(f"{owner}.s{shard}.shed_lost")

    # --- dispatcher side (main loop) ---

    def _record_stale_drop(self, msg_type: str, age_s: float) -> None:
        """Count an age drop at dequeue; aggregated like ``_record_shed``."""
        self._metrics.inc(f"{self._name}.stale_market_frames")
        self._metrics.inc(f"{self._name}.stale.{msg_type}")
        self._stale_pending[msg_type] = self._stale_pending.get(msg_type, 0) + 1
        now = self._clock.monotonic_ns()
        last = self._stale_last_log_mono_ns
        if last is None or (now - last) / 1e9 >= self._max_silence_s:
            log.warning(
                "ws_stale_market_frames",
                name=self._name,
                dropped=dict(self._stale_pending),
                oldest_age_s=round(age_s, 3),
                depths=self._lanes.depths(),
            )
            self._stale_pending.clear()
            self._stale_last_log_mono_ns = now

    def _market_frame_stale(self, message: JsonDict) -> float | None:
        """Wire age in seconds if the frame passed its declared stale bound,
        else None. Fail-safe: no bound / no stamp ⇒ never stale."""
        bound_ns = self._stale_after_ns.get(str(message.get("type", "")))
        if bound_ns is None:
            return None
        recv_ns = message.get("_recv_mono_ns")
        if not isinstance(recv_ns, int):
            return None
        age_ns = self._clock.monotonic_ns() - recv_ns
        return age_ns / 1e9 if age_ns > bound_ns else None

    def _discard_queued(self) -> None:
        """Drop every queued-but-unprocessed message (dead-connection backlog).
        A fan-out FOLLOWER drops only ITS OWN market frames from the shared
        lanes (``_Lanes.purge_market``): its siblings' backlog is live."""
        if self._shard_tag is not None:
            dropped = self._lanes.purge_market(self._shard_tag)
        else:
            dropped = self._lanes.clear()
        if dropped:
            self._metrics.inc(f"{self._name}.queue_discarded", dropped)
            log.info("ws_queue_discarded", name=self._name, dropped=dropped)

    async def _dispatch_loop(self) -> None:
        """Single long-lived consumer: handlers run here, IN ORDER within a
        lane (FIFO keeps per-sid seq continuity), off the reader. Messages
        from a dead connection self-drop downstream (sid no longer
        registered). Cancelled only by stop().

        Lane order per dispatch: PRIORITY (fully drained) → CONTROL → one
        MARKET frame, then back to PRIORITY — so a deadlined frame waits for
        at most one normal handler run instead of the whole backlog
        (derivation at ``_Lanes``). Wakeups are coalesced by the reader; a
        spurious wake costs one empty pass.
        """
        self._main_loop = asyncio.get_running_loop()
        while True:
            await self._drain_lanes()
            await self._wake.wait()
            self._wake.clear()

    async def _drain_lanes(self) -> None:
        lanes = self._lanes
        while True:
            self._flush_reader_metrics()
            message = lanes.pop(Lane.PRIORITY)
            if message is None:
                message = lanes.pop(Lane.CONTROL)
            if message is None:
                message = lanes.pop(Lane.MARKET)
                if message is None:
                    if lanes.settle_idle():
                        return
                    continue
                age_s = self._market_frame_stale(message)
                if age_s is not None:
                    self._record_stale_drop(str(message.get("type", "")), age_s)
                    continue
            try:
                await self._dispatch(message)
            except Exception:  # a handler bug must not kill the dispatcher
                log.exception("ws_dispatch_failed", name=self._name)

    async def _dispatch(self, message: JsonDict) -> None:
        msg_type = str(message.get("type", ""))
        self._metrics.inc(f"{self._name}.msg.{msg_type}")
        if not await self._dispatch_control(message, msg_type):
            return
        for handler in self._handlers.get(msg_type, []) + self._handlers.get("*", []):
            try:
                await handler(message)
            except Exception:
                log.exception("ws_handler_failed", name=self._name, msg_type=msg_type)

    async def _dispatch_control(self, message: JsonDict, msg_type: str) -> bool:
        """Transport-level handling of control frames BEFORE the consumer
        handlers run. Returns False to withhold the frame from the handlers —
        the single-socket manager never does (byte-identical to the inline
        code this replaced); the fan-out does for a per-shard channel loss it
        recovers itself (``ws_fanout.CommsFanout._dispatch_control``)."""
        if msg_type == "error":
            log.warning("ws_server_error", name=self._name, message=message)
            self._note_server_error(message)
            await self._resolve_subscribe_error(message)
        elif msg_type == "subscribed":
            await self._resolve_subscribed(message)
        return True

    async def _resolve_subscribe_error(self, message: JsonDict) -> None:
        """An ``error`` frame echoing a pending subscribe's command id IS that
        subscribe's refusal (asyncapi-ws.md §3.6: ``id`` echoes the command).
        The pending ack is released and the subscription's
        ``on_subscribe_error`` (if any) is told the code — the fan-out's
        sharding fallback hangs off this. A subscription without the hook
        keeps exactly today's behaviour (the warning already logged)."""
        try:
            cmd_id = int(message.get("id", 0))
        except (TypeError, ValueError):
            return
        if cmd_id < 1:
            return
        sub = self._pending_sub_acks.pop(cmd_id, None)
        if sub is None or sub.on_subscribe_error is None:
            return
        msg = message.get("msg", {})
        code = 0
        text = ""
        if isinstance(msg, dict):
            try:
                code = int(msg.get("code", 0))
            except (TypeError, ValueError):
                code = 0
            text = str(msg.get("msg", ""))
        try:
            await sub.on_subscribe_error(code, text)
        except Exception:
            log.exception("ws_subscribe_error_handler_failed", name=self._name)

    def _note_server_error(self, message: JsonDict) -> None:
        """Code 25 is the exchange saying WE read too slowly. With the reader
        on its own thread that cannot follow from a main-loop stall any more,
        so a recurrence is a NEW cause (GIL starvation, a dead reader thread,
        the network) and must be impossible to miss: CRITICAL, with the
        reader's own state attached. Recovery (force_reconnect via the
        intake's on_channel_lost) is unchanged — the subscription IS dead."""
        msg = message.get("msg", {})
        try:
            code = int(msg.get("code", 0)) if isinstance(msg, dict) else 0
        except (TypeError, ValueError):
            code = 0
        if code != SUBSCRIPTION_BUFFER_OVERFLOW_CODE:
            return
        self._metrics.inc(f"{self._name}.subscription_buffer_overflow")
        reader = self._reader
        log.critical(
            "ws_subscription_buffer_overflow",
            name=self._name,
            detail=msg.get("msg") if isinstance(msg, dict) else None,
            reader_thread_alive=reader.is_alive() if reader is not None else None,
            frames_read=self._frames_read,
            last_rx_age_s=self.last_rx_age_s,
            depths=self._lanes.depths(),
        )

    async def _resolve_subscribed(self, message: JsonDict) -> None:
        sub = self._pending_sub_acks.pop(int(message.get("id", 0)), None)
        if sub is None or sub.on_subscribed is None:
            return
        msg = message.get("msg", {})
        sid = int(msg.get("sid", 0))
        if sid < 1:
            log.warning("ws_subscribed_without_sid", name=self._name, message=message)
            return
        try:
            await sub.on_subscribed(sid)
        except Exception:
            log.exception("ws_subscribed_handler_failed", name=self._name)

    def _reserve_sub_ack(self, sub: _Subscription) -> int:
        """Register the pending ack BEFORE the subscribe leaves (review fix
        2026-09-05, fan-out build). ``send_command`` takes its id
        synchronously on entry, so the next id is known here without an
        await in between; registering it after the send let an ack that
        arrived within one loop hop (a fast exchange, a fake) be dispatched
        before the registration and silently dropped — ``on_subscribed``
        never fired. Live RTT hid it; correctness must not depend on RTT."""
        cmd_id = self._cmd_id + 1
        self._pending_sub_acks[cmd_id] = sub
        return cmd_id

    async def _send_subscription_now(self, sub: _Subscription) -> None:
        cmd_id = self._reserve_sub_ack(sub)
        try:
            sent = await self.send_command(
                "subscribe", {"channels": sub.channels, **sub.params_extra}
            )
        except Exception as exc:
            self._pending_sub_acks.pop(cmd_id, None)
            # Reconnect resends everything; downstream stays invalid until the
            # subscribe ack + snapshot arrive, so nothing quotes off this gap.
            log.warning("live_subscribe_failed", name=self._name, error=repr(exc))
            return
        if sent != cmd_id:  # pragma: no cover — defensive: ids are taken on entry
            self._pending_sub_acks[sent] = self._pending_sub_acks.pop(cmd_id)

    async def _send_subscriptions(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Re-send every declared subscription on the SPECIFIC socket ``ws``
        (never "whatever is current": a follow-up for a socket that died
        must not subscribe the next socket twice)."""
        if ws is not self._ws:
            return
        self._pending_sub_acks.clear()  # stale acks from a previous connection
        for sub in list(self._subscriptions):
            cmd_id = self._reserve_sub_ack(sub)
            try:
                sent = await self.send_command(
                    "subscribe", {"channels": sub.channels, **sub.params_extra}, ws=ws
                )
            except Exception:
                self._pending_sub_acks.pop(cmd_id, None)
                raise
            if sent != cmd_id:  # pragma: no cover — defensive: ids are taken on entry
                self._pending_sub_acks[sent] = self._pending_sub_acks.pop(cmd_id)

    async def send_command(
        self,
        cmd: str,
        params: dict[str, Any],
        *,
        ws: aiohttp.ClientWebSocketResponse | None = None,
    ) -> int:
        target = self._ws if ws is None else ws
        if target is None or target.closed:
            raise RuntimeError("ws not connected")
        self._cmd_id += 1
        payload = json.dumps({"id": self._cmd_id, "cmd": cmd, "params": params})
        try:
            await self._on_socket_loop(target.send_str(payload))
        except (aiohttp.ClientError, ConnectionError) as exc:
            # WRITE side dead ("Cannot write to closing transport") while the READ
            # side is still alive (server pings + book deltas keep arriving), so
            # receive_timeout never fires and we'd sit half-dead forever, silently
            # failing EVERY new leg-book subscription (2026-07-13 live: 80
            # live_subscribe_failed / only 4 books subscribed → combos on the
            # unsubscribed legs, e.g. KXWCGAME reg-time-win, all decline
            # skip_leg_stale). Force ONE reconnect to rebuild full duplex and
            # re-send every subscription; re-raise so the caller still logs the fail.
            log.warning("ws_write_failed_forcing_reconnect", name=self._name, error=repr(exc))
            await self.force_reconnect()
            raise
        return self._cmd_id


def _resolve_quietly(fut: asyncio.Future[None]) -> None:
    if not fut.done():
        fut.set_result(None)


class _ReaderThread:
    """The socket's home: a daemon thread running its OWN asyncio loop that
    connects, reads, parses and pushes frames into ``_Lanes``. Nothing here
    awaits a handler or touches main-loop state beyond the lanes, the
    ``_ws`` reference and the receive stamp (single attribute writes).

    Why a thread and not "more care on the main loop": a main-loop stall is
    by definition a stretch where NO coroutine there runs — however the
    reader is written, it cannot read while the maintenance pass holds the
    loop. The GIL is not the issue: a CPU-bound main thread yields it every
    switch interval (5 ms) and releases it entirely for I/O and SQLite, so
    the reader thread gets to ``recv`` within milliseconds through a stall
    of any length. The exchange sees a client that always drains.

    Session isolation: the ``aiohttp.ClientSession`` and every socket are
    created and closed on THIS loop; the main loop reaches them only through
    ``WsManager._on_socket_loop`` (``run_coroutine_threadsafe``).

    Ordering preserved from the single-loop design: ``on_disconnect``
    handlers (cancel-all) complete on the main loop BEFORE this thread
    attempts a reconnect — the reader posts the disconnect and awaits an ack
    future the main loop resolves when the handlers are done."""

    def __init__(self, owner: WsManager) -> None:
        self._owner = owner
        self._thread = threading.Thread(
            target=self._main, name=f"{owner._name}-reader", daemon=True
        )
        self.loop: asyncio.AbstractEventLoop | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._started = threading.Event()

    def start(self) -> None:
        self._thread.start()
        # The socket loop must exist before ``start()`` returns so a
        # ``send_command`` racing the first connect marshals to a real loop
        # (it still fails cleanly with "ws not connected" until the socket
        # is up, exactly as before).
        self._started.wait()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    async def stop(self) -> None:
        loop = self.loop
        task = self._run_task
        if loop is not None and task is not None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(task.cancel)
        if self._thread.is_alive():
            # No join timeout on purpose: the thread is a DAEMON (a wedged
            # join cannot hold the interpreter's exit) and this await runs
            # inside the ``ws_stop`` ShutdownStage's wall (BOUNDED SHUTDOWN
            # 2026-07-27), which is the bound that applies.
            await asyncio.to_thread(self._thread.join)

    def _main(self) -> None:
        loop = asyncio.new_event_loop()
        self.loop = loop
        try:
            self._run_task = loop.create_task(self._run(), name=f"{self._owner._name}-run")
            self._started.set()
            with contextlib.suppress(asyncio.CancelledError):
                loop.run_until_complete(self._run_task)
        except BaseException:
            log.exception("ws_reader_thread_died", name=self._owner._name)
        finally:
            self._started.set()
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                # ``loop.close()`` shuts the default executor down WITHOUT
                # waiting (CPython 3.13 BaseEventLoop.close: "shuts down the
                # executor, but does not wait for the executor to finish" →
                # ``executor.shutdown(wait=False)``): aiohttp's getaddrinfo
                # workers exit as soon as their current call returns. We
                # deliberately do NOT ``shutdown_default_executor()`` (join)
                # here: a resolver wedged in getaddrinfo (the 8/27 wake-DNS
                # failure) would hold this thread — and ``stop()`` — for the
                # resolver's own timeout, inside the shutdown wall.
                loop.close()
                self.loop = None
                self._owner._ws = None
                if not self._owner._stopping:
                    # No reconnects can ever happen again from here: the
                    # manager reads as disconnected, data goes stale and the
                    # breaker halts — but say WHY, loudly.
                    log.critical("ws_reader_thread_exited", name=self._owner._name)

    async def _run(self) -> None:
        owner = self._owner
        backoff = owner._backoff_initial_s
        main = owner._main_loop
        assert main is not None  # start() sets it before spawning the thread
        loop = asyncio.get_running_loop()
        async with aiohttp.ClientSession() as session:
            while not owner._stopping:
                try:
                    headers = owner._signer.headers("GET", _WS_HANDSHAKE_PATH)
                    async with owner._connect(session, owner._url, headers) as ws:
                        owner._ws = ws
                        owner._force_reconnecting = False  # fresh socket — clear guard
                        owner._last_rx_mono_ns = owner._clock.monotonic_ns()
                        owner._lanes.count(f"{owner._name}.connect")
                        log.info("ws_connected", name=owner._name)
                        backoff = owner._backoff_initial_s
                        # on_connect handlers + subscriptions run on the MAIN
                        # loop (they are main-loop code); reading starts NOW
                        # so server pings are answered and the subscribe acks
                        # have a reader waiting for them.
                        main.call_soon_threadsafe(owner._socket_connected, ws)
                        await owner._read_loop(ws)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("ws_error", name=owner._name, error=repr(exc))
                finally:
                    owner._ws = None

                if owner._stopping:
                    return
                # Hand the disconnect to the main loop and WAIT for its
                # on_disconnect handlers (cancel-all) before any reconnect.
                ack: asyncio.Future[None] = loop.create_future()
                try:
                    main.call_soon_threadsafe(owner._socket_disconnected, ack, loop)
                except RuntimeError:
                    return  # main loop closed: the process is going away
                await ack
                delay = backoff * (1 + random.random() * 0.25)
                backoff = min(backoff * 2, owner._backoff_max_s)
                await asyncio.sleep(delay)
