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
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
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


SubscribedHandler = Callable[[int], Awaitable[None]]  # receives the new sid


@dataclass
class _Subscription:
    channels: list[str]
    params_extra: dict[str, Any] = field(default_factory=dict)
    on_subscribed: SubscribedHandler | None = None


class WsManager:
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
    ) -> None:
        self._url = url
        self._signer = signer
        self._clock = clock
        self._metrics = metrics or Metrics()
        self._name = name
        self._max_silence_s = max_silence_s
        self._backoff_initial_s = backoff_initial_s
        self._backoff_max_s = backoff_max_s

        self._handlers: dict[str, list[MessageHandler]] = {}
        self._on_disconnect: list[LifecycleHandler] = []
        self._on_connect: list[LifecycleHandler] = []
        self._subscriptions: list[_Subscription] = []
        self._pending_sub_acks: dict[int, _Subscription] = {}
        self._cmd_id = 0
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._last_rx_mono_ns: int | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._stopping = False

    # --- registration (all before start) ---

    def on_message(self, msg_type: str, handler: MessageHandler) -> None:
        """Register a handler for a message ``type`` ('*' = every message)."""
        self._handlers.setdefault(msg_type, []).append(handler)

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
        **params_extra: Any,
    ) -> None:
        """Declare a desired subscription; (re)sent on every (re)connect.

        ``on_subscribed`` fires with the server-assigned sid on every (re)ack —
        sids change across reconnects, so consumers must re-key their state.
        """
        self._subscriptions.append(
            _Subscription(list(channels), dict(params_extra), on_subscribed)
        )

    # --- health ---

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    @property
    def healthy(self) -> bool:
        """Connected with traffic inside the silence budget (server pings @10s)."""
        if not self.connected or self._last_rx_mono_ns is None:
            return False
        age_s = (self._clock.monotonic_ns() - self._last_rx_mono_ns) / 1e9
        return age_s <= self._max_silence_s

    # --- lifecycle ---

    def start(self) -> None:
        if self._run_task is not None:
            raise RuntimeError("already started")
        self._stopping = False
        self._run_task = asyncio.create_task(self._run(), name=f"{self._name}-run")

    async def stop(self) -> None:
        self._stopping = True
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._run_task is not None:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
            self._run_task = None

    async def _run(self) -> None:
        backoff = self._backoff_initial_s
        async with aiohttp.ClientSession() as session:
            while not self._stopping:
                try:
                    headers = self._signer.headers("GET", _WS_HANDSHAKE_PATH)
                    async with session.ws_connect(
                        self._url, headers=headers, autoping=True, heartbeat=None
                    ) as ws:
                        self._ws = ws
                        self._last_rx_mono_ns = self._clock.monotonic_ns()
                        self._metrics.inc(f"{self._name}.connect")
                        log.info("ws_connected", name=self._name)
                        backoff = self._backoff_initial_s
                        for handler in self._on_connect:
                            await handler()
                        await self._send_subscriptions()
                        await self._read_loop(ws)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("ws_error", name=self._name, error=repr(exc))
                finally:
                    self._ws = None

                if self._stopping:
                    return
                # Disconnect: notify (cancel-all etc.) BEFORE any reconnect.
                self._metrics.inc(f"{self._name}.disconnect")
                log.warning("ws_disconnected", name=self._name)
                for handler in self._on_disconnect:
                    try:
                        await handler()
                    except Exception:
                        log.exception("ws_disconnect_handler_failed", name=self._name)
                delay = backoff * (1 + random.random() * 0.25)
                backoff = min(backoff * 2, self._backoff_max_s)
                await asyncio.sleep(delay)

    async def _read_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for frame in ws:
            self._last_rx_mono_ns = self._clock.monotonic_ns()
            if frame.type == aiohttp.WSMsgType.TEXT:
                try:
                    message: JsonDict = json.loads(frame.data)
                except ValueError:
                    log.warning("ws_bad_json", name=self._name, data=frame.data[:200])
                    continue
                await self._dispatch(message)
            elif frame.type == aiohttp.WSMsgType.ERROR:
                log.warning("ws_frame_error", name=self._name)
                return

    async def _dispatch(self, message: JsonDict) -> None:
        msg_type = str(message.get("type", ""))
        self._metrics.inc(f"{self._name}.msg.{msg_type}")
        if msg_type == "error":
            log.warning("ws_server_error", name=self._name, message=message)
        if msg_type == "subscribed":
            await self._resolve_subscribed(message)
        for handler in self._handlers.get(msg_type, []) + self._handlers.get("*", []):
            try:
                await handler(message)
            except Exception:
                log.exception("ws_handler_failed", name=self._name, msg_type=msg_type)

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

    async def _send_subscriptions(self) -> None:
        self._pending_sub_acks.clear()  # stale acks from a previous connection
        for sub in self._subscriptions:
            cmd_id = await self.send_command(
                "subscribe", {"channels": sub.channels, **sub.params_extra}
            )
            self._pending_sub_acks[cmd_id] = sub

    async def send_command(self, cmd: str, params: dict[str, Any]) -> int:
        if self._ws is None or self._ws.closed:
            raise RuntimeError("ws not connected")
        self._cmd_id += 1
        await self._ws.send_str(json.dumps({"id": self._cmd_id, "cmd": cmd, "params": params}))
        return self._cmd_id
