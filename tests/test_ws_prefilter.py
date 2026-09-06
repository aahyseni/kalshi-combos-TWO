"""READER-SIDE RAW PRE-FILTER + WALL-TIME GOVERNOR CADENCE (2026-09-05).

Property under test (``exchange/ws_prefilter.py`` module doc): the set of RFQs
that reach the intake's fan-out after FULL parsing is IDENTICAL with and
without the pre-filter, over a corpus of REAL frames
(``tests/fixtures/ws_rfq_frames_corpus_20260905.jsonl``: 400 frames from the
9/5 archive tail — today's allowlisted series — and 200 real July World-Cup
frames whose series are NOT on today's allowlist) plus layout / escaping /
foreign-series / mixed-legs variants generated from them. The pre-filter may
only remove work; every frame it cannot identify passes to the full parser.

Cadence under test (``WallTimeCadence``): the governor refresh fires on
ELAPSED MONOTONIC TIME, at most once per check, regardless of how long a
maintenance pass takes.
"""

from __future__ import annotations

import asyncio
import collections
import json
import random
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import structlog.testing

from combomaker.core.clock import FakeClock, SystemClock
from combomaker.exchange.ws import WsManager
from combomaker.exchange.ws_fanout import (
    FanoutGovernor,
    ShardMeter,
    WallTimeCadence,
    fanout_tape_path,
)
from combomaker.exchange.ws_prefilter import (
    RawSeriesPrefilter,
    Verdict,
    raw_created_ts,
    raw_frame_type,
)
from combomaker.ops.metrics import Metrics
from combomaker.ops.quote_app import MAINTENANCE_TICK_INTERVAL_S, QuoteApp
from combomaker.rfq.intake import RfqIntake
from combomaker.rfq.models import Rfq
from combomaker.risk.progress import ProgressLedger, progress_path
from tests.test_feed import FakeWs
from tests.test_ws_fanout import _FakeExchange, _fanout, _settled, _until

JsonDict = dict[str, Any]

CORPUS = Path(__file__).parent / "fixtures" / "ws_rfq_frames_corpus_20260905.jsonl"

# The 2026-09-05 live allowlist (config/prod-live-wc.local.yaml
# ``filters.allowed_leg_series_prefixes``), copied here ONLY so the corpus test
# runs against the real families; production reads the config object.
LIVE_ALLOWLIST: tuple[str, ...] = (
    "KXMLBGAME", "KXMLBTOTAL", "KXMLBSPREAD", "KXMLBKS", "KXMLBHIT", "KXMLBHR",
    "KXMLBHRR", "KXMLBRFI", "KXMLBTB", "KXMLBSB", "KXMLBOUTS", "KXMLBRBI",
    "KXLOLGAME-", "KXCS2GAME-", "KXCSGOGAME-",
    "KXLALIGAGAME-", "KXLALIGATOTAL-", "KXLALIGASPREAD-", "KXLALIGABTTS-",
    "KXMLSGAME-", "KXMLSTOTAL-", "KXMLSSPREAD-", "KXMLSBTTS-",
    "KXUECLGAME-", "KXUECLTOTAL-", "KXUECLSPREAD-", "KXUECLBTTS-", "KXUECLADVANCE-",
    "KXLIGAMXGAME-", "KXLIGAMXTOTAL-", "KXLIGAMXSPREAD-", "KXLIGAMXBTTS-",
    "KXEFLCHAMPIONSHIPGAME-", "KXEFLCHAMPIONSHIPTOTAL-",
    "KXEFLCHAMPIONSHIPSPREAD-", "KXEFLCHAMPIONSHIPBTTS-",
    "KXUCLGAME-", "KXUCLTOTAL-", "KXUCLSPREAD-", "KXUCLBTTS-",
    "KXEPLGAME-", "KXEPLTOTAL-", "KXEPLSPREAD-", "KXEPLBTTS-",
    "KXSERIEAGAME-", "KXSERIEATOTAL-", "KXSERIEASPREAD-", "KXSERIEABTTS-",
    "KXLIGUE1GAME-", "KXLIGUE1TOTAL-", "KXLIGUE1SPREAD-", "KXLIGUE1BTTS-",
    "KXUCLADVANCE-",
    "KXBUNDESLIGAGAME-", "KXBUNDESLIGATOTAL-", "KXBUNDESLIGASPREAD-", "KXBUNDESLIGABTTS-",
    "KXSAUDIPLGAME-", "KXSAUDIPLTOTAL-", "KXSAUDIPLSPREAD-", "KXSAUDIPLBTTS-",
)  # fmt: skip

# Series observed on the exchange that are NOT allowlisted (docs/reports
# 2026-08-12 census: club friendlies, Leagues Cup, MLB first-five, UEFA Super
# Cup, UFC; 2026-08-26: the Bundesliga-2 shared-prefix trap).
FOREIGN_SERIES: tuple[str, ...] = (
    "KXCLUBFGAME",
    "KXLEAGUESCUPGAME",
    "KXLEAGUESCUPTOTAL",
    "KXMLBF5",
    "KXMLBF5TOTAL",
    "KXUEFASCGAME",
    "KXUFCFIGHT",
    "KXBUNDESLIGA2GAME",
    "KXNFLGAME",
)


def _load_corpus() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with CORPUS.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    assert len(out) == 600
    return out


def _envelope(msg: JsonDict, *, sid: int = 15, msg_type: str = "rfq_created") -> JsonDict:
    return {"type": msg_type, "sid": sid, "msg": msg}


def _foreign_variant(msg: JsonDict, rng: random.Random, *, legs_to_swap: int | None) -> JsonDict:
    """Replace the series of ``legs_to_swap`` legs (None = all) with foreign
    series, keeping the frame's structure byte-for-byte otherwise."""
    out = json.loads(json.dumps(msg))
    legs = out.get("mve_selected_legs") or []
    idx = list(range(len(legs)))
    if legs_to_swap is not None:
        idx = idx[:legs_to_swap]
    for i in idx:
        leg = legs[i]
        series = rng.choice(FOREIGN_SERIES)
        tail_m = str(leg.get("market_ticker", "")).split("-", 1)
        tail_e = str(leg.get("event_ticker", "")).split("-", 1)
        leg["market_ticker"] = f"{series}-{tail_m[1] if len(tail_m) > 1 else 'X'}"
        leg["event_ticker"] = f"{series}-{tail_e[1] if len(tail_e) > 1 else 'X'}"
    return out


def _msg_first(payload: dict[str, Any]) -> dict[str, Any]:
    """The same envelope with ``type`` serialized LAST — after ``msg`` (the
    wire's key order is unverifiable from this box; review fix 2026-09-05)."""
    reordered = {k: v for k, v in payload.items() if k != "type"}
    reordered["type"] = payload["type"]
    return reordered


def _layouts(env: JsonDict) -> list[str]:
    """The same envelope in every serialization the identifier must handle:
    compact, spaced, pretty-printed, sid-first (type before msg), msg-first
    (type AFTER msg) spaced and compact."""
    compact = json.dumps(env, separators=(",", ":"))
    spaced = json.dumps(env)
    pretty = json.dumps(env, indent=2)
    sid_first = json.dumps({"sid": env["sid"], "type": env["type"], "msg": env["msg"]})
    msg_first = json.dumps(_msg_first(env))
    msg_first_compact = json.dumps(_msg_first(env), separators=(",", ":"))
    return [compact, spaced, pretty, sid_first, msg_first, msg_first_compact]


N_LAYOUTS = len(_layouts(_envelope({"id": "probe"})))


async def _intake_reached(
    frames: list[str], *, prefilter: RawSeriesPrefilter | None
) -> tuple[list[str], list[str], int]:
    """Drive the REAL intake over serialized frames. With a pre-filter, a
    frame it DROPs never reaches the intake (exactly the reader's rule);
    everything else is parsed and delivered. Returns (reached rfq ids,
    parse-error ids, dropped count)."""
    ws = FakeWs()
    metrics = Metrics()
    intake = RfqIntake(ws, metrics, series_prefixes=LIVE_ALLOWLIST)
    seen: list[str] = []

    async def on_rfq(rfq: Rfq) -> None:
        seen.append(rfq.rfq_id)

    intake.on_rfq(on_rfq)
    dropped = 0
    for raw in frames:
        if prefilter is not None and prefilter.judge(raw) is Verdict.DROP:
            dropped += 1
            continue
        await ws.deliver(json.loads(raw))
    return seen, [], dropped


# --------------------------------------------------------------------------- #
# 1. The identifier
# --------------------------------------------------------------------------- #


def _raw(payload: dict[str, Any]) -> str:
    return json.dumps(payload)


# Documented envelope examples (asyncapi-ws.md §3 / communications-ws.md §6),
# serialized in the docs' own layout (type before msg).
DOCS_FRAMES: dict[str, dict[str, Any]] = {
    "subscribed": {"id": 1, "type": "subscribed", "msg": {"channel": "communications", "sid": 1}},
    "error": {
        "id": 123,
        "type": "error",
        "msg": {"code": 25, "msg": "Subscription buffer overflow"},
    },
    "quote_created": {
        "type": "quote_created",
        "sid": 15,
        "msg": {
            "quote_id": "q",
            "rfq_id": "r",
            "market_ticker": "KXNFLGAME-1",
            "created_ts": "2024-12-01T10:02:00Z",
        },
    },
    "quote_accepted": {
        "type": "quote_accepted",
        "sid": 15,
        "msg": {
            "quote_id": "q",
            "rfq_id": "r",
            "market_ticker": "KXNFLGAME-1",
            "accepted_side": "yes",
        },
    },
    "quote_executed": {
        "type": "quote_executed",
        "sid": 15,
        "msg": {"quote_id": "q", "rfq_id": "r", "order_id": "o", "market_ticker": "KXNFLGAME-1"},
    },
    "rfq_deleted": {
        "type": "rfq_deleted",
        "sid": 15,
        "msg": {"id": "r", "creator_id": "c", "market_ticker": "KXNFLGAME-1", "deleted_ts": "x"},
    },
    "unsubscribed": {"id": 102, "sid": 2, "seq": 7, "type": "unsubscribed"},
}


def _created(legs: list[str], *, own: str = "KXMVE-1") -> str:
    return _raw(
        {
            "type": "rfq_created",
            "sid": 1,
            "msg": {
                "id": "x",
                "market_ticker": own,
                "mve_selected_legs": [{"market_ticker": t, "side": "yes"} for t in legs],
            },
        }
    )


def test_raw_frame_type_reads_the_envelope_type_in_every_layout() -> None:
    env = _envelope(
        {"id": "r1", "market_ticker": "KXMLBGAME-1", "created_ts": "2026-09-05T22:00:00Z"}
    )
    for raw in _layouts(env):
        assert raw_frame_type(raw) == "rfq_created", raw
    for kind, payload in DOCS_FRAMES.items():
        assert raw_frame_type(_raw(payload)) == kind
        assert raw_frame_type(json.dumps(payload, separators=(",", ":"))) == kind
        # msg-first serializations of the same documented frames.
        assert raw_frame_type(_raw(_msg_first(payload))) == kind
        assert raw_frame_type(json.dumps(_msg_first(payload), separators=(",", ":"))) == kind
        assert raw_frame_type(json.dumps(_msg_first(payload), indent=2)) == kind


def test_raw_frame_type_reads_msg_first_and_fails_open_only_when_uncertain() -> None:
    # msg BEFORE type (review fix 2026-09-05): the key sits AFTER the last
    # nested container closes — the envelope's own region — and is read.
    assert raw_frame_type('{"sid": 1, "msg": {"id": "x"}, "type": "rfq_created"}') == "rfq_created"
    assert raw_frame_type('{"sid":1,"msg":{"id":"x"},"type":"rfq_created"}') == "rfq_created"
    assert raw_frame_type('{"sid": 1, "msg": {"id": "x"}, "type": "rfq_created", "seq": 7}') == (
        "rfq_created"
    )
    # A nested object's own "type" key is never consulted: the envelope's wins.
    assert raw_frame_type('{"msg": {"type": "rfq_created"}, "type": "error"}') == "error"
    assert raw_frame_type('{"type": "error", "msg": {"type": "rfq_created"}}') == "error"
    # Braces inside msg strings can only SHRINK the tail region, never let a
    # nested key through.
    brace_in_msg = '{"sid": 1, "msg": {"note": "}", "type": "rfq_created"}, "type": "error"}'
    assert raw_frame_type(brace_in_msg) == "error"
    close_in_msg = '{"sid": 1, "msg": {"type": "rfq_created", "n": "]}"}, "type": "error"}'
    assert raw_frame_type(close_in_msg) == "error"
    # A brace inside a TAIL string ends the region early → fail-open, never a misread.
    assert raw_frame_type('{"sid": 1, "msg": {"a": 1}, "type": "error", "note": "}"}') is None
    # "type" as a VALUE is skipped (before or after msg), the real key is read.
    assert raw_frame_type('{"sid": "type", "type": "rfq_created", "msg": {}}') == "rfq_created"
    assert raw_frame_type('{"sid": 1, "msg": {}, "note": "type", "type": "rfq_created"}') == (
        "rfq_created"
    )
    # Arrays are containers too (the tail starts after the LAST close of either kind).
    assert raw_frame_type('{"a": [{"type": "rfq_created"}], "type": "error"}') == "error"
    assert raw_frame_type('{"a": ["type", 5], "type": "error"}') == "error"
    # No envelope type key at all; a non-object; truncated / unclosed text.
    assert raw_frame_type('{"sid": 1, "msg": {"type": "rfq_created"}}') is None
    assert raw_frame_type('["type", "rfq_created"]') is None
    assert raw_frame_type('{"type": "rfq_cre') is None
    assert raw_frame_type('{"sid": 1, "msg": {"id": "x", "type": "rfq_created"') is None
    assert raw_frame_type("") is None
    # A type key BETWEEN two nested containers (a shape the envelope never
    # has) is not read: fail-open, by design — the full depth walk that would
    # read it costs more than the parse it exists to save (module doc).
    assert raw_frame_type('{"a": {"x": 1}, "type": "error", "b": {"type": "rfq_created"}}') is None


def test_raw_created_ts_reads_the_first_created_ts() -> None:
    stamp = "2026-09-05T22:03:30.623372Z"
    env = _envelope({"id": "r1", "market_ticker": "KXX-1", "created_ts": stamp})
    for raw in _layouts(env):
        assert raw_created_ts(raw) == stamp
    assert raw_created_ts(_raw(DOCS_FRAMES["rfq_deleted"])) is None
    # The first occurrence is a VALUE (an id that reads "created_ts"): skipped
    # for the key that follows (review fix 2026-09-05).
    value_first = _raw(_envelope({"id": "created_ts", "market_ticker": "KXX", "created_ts": stamp}))
    assert value_first.index('"created_ts"') < value_first.index('"created_ts": "')
    assert raw_created_ts(value_first) == stamp
    assert raw_created_ts(_raw(_envelope({"id": "created_ts"}))) is None


# --------------------------------------------------------------------------- #
# 2. Decision neutrality over the real-frame corpus
# --------------------------------------------------------------------------- #


async def test_prefilter_drop_set_is_a_subset_of_the_intakes_drop_set_on_real_frames() -> None:
    """THE PROPERTY: over real frames (+ variants) the RFQs reaching the
    intake's fan-out are identical with and without the pre-filter; the
    pre-filter never drops a frame the intake would have fanned out."""
    rng = random.Random(20260905)
    corpus = _load_corpus()
    prefilter = RawSeriesPrefilter(LIVE_ALLOWLIST)
    frames: list[str] = []
    classes: collections.Counter[str] = collections.Counter()
    for rec in corpus:
        msg = rec["msg"]
        for raw in _layouts(_envelope(msg)):
            frames.append(raw)
            classes[rec["era"]] += 1
        # Every leg foreign (the firehose's dominant shape): must DROP — in
        # the type-first AND the msg-first serialization (review fix).
        foreign = _envelope(_foreign_variant(msg, rng, legs_to_swap=None))
        frames.append(json.dumps(foreign))
        frames.append(json.dumps(_msg_first(foreign)))
        classes["foreign"] += 2
        # ONE leg foreign, the rest allowlisted: PASSES here, intake drops it.
        legs = msg.get("mve_selected_legs") or []
        if len(legs) >= 2:
            frames.append(json.dumps(_envelope(_foreign_variant(msg, rng, legs_to_swap=1))))
            classes["mixed"] += 1
    # Escaped text (a \u escape in a value) → must FAIL OPEN, never DROP.
    escaped = json.dumps(_envelope(_foreign_variant(corpus[0]["msg"], rng, legs_to_swap=None)))
    escaped = escaped.replace('"side": "yes"', '"side": "\\u0079es"', 1)
    assert "\\" in escaped
    frames.append(escaped)
    rng.shuffle(frames)

    verdicts = collections.Counter(prefilter.judge(raw) for raw in frames)
    assert verdicts[Verdict.OTHER] == 0  # every frame is an rfq_created
    assert verdicts[Verdict.DROP] >= classes["foreign"] + N_LAYOUTS * 200  # foreign + July
    assert verdicts[Verdict.PASS] >= N_LAYOUTS * 400  # today's allowlisted frames, every layout
    assert verdicts[Verdict.FAIL_OPEN] >= 1  # the escaped frame

    with_filter, _, dropped = await _intake_reached(frames, prefilter=prefilter)
    without_filter, _, _ = await _intake_reached(frames, prefilter=None)
    assert with_filter == without_filter  # same RFQs, same order
    assert dropped == verdicts[Verdict.DROP] > 0
    # The intake fanned out exactly today's allowlisted frames (each layout is
    # a separate delivery; the registry dedupes nothing at this layer).
    assert len(without_filter) == N_LAYOUTS * 400

    # And every frame the pre-filter dropped satisfies the INTAKE's own drop
    # rule (proof step 3 → the intake's ``any(not startswith)``), and a
    # frame's verdict does not depend on its serialization layout.
    def identity(raw: str) -> tuple[str, str]:
        env = json.loads(raw)
        return env["msg"]["id"], json.dumps(env["msg"].get("mve_selected_legs"), sort_keys=True)

    verdict_by_identity: dict[tuple[str, str], set[Verdict]] = collections.defaultdict(set)
    for raw in frames:
        verdict = prefilter.judge(raw)
        verdict_by_identity[identity(raw)].add(verdict)
        if verdict is Verdict.DROP:
            legs = json.loads(raw)["msg"].get("mve_selected_legs") or []
            assert not legs or any(
                not str(leg.get("market_ticker", "")).startswith(LIVE_ALLOWLIST)
                for leg in legs
                if isinstance(leg, dict)
            ), raw[:200]
    for ident, verdicts_seen in verdict_by_identity.items():
        # Across layouts an identity is judged uniformly (FAIL_OPEN aside —
        # the escaped variant's only verdict, by construction).
        assert len(verdicts_seen - {Verdict.FAIL_OPEN}) <= 1, (ident, verdicts_seen)


def test_prefilter_never_drops_priority_or_control_frames() -> None:
    prefilter = RawSeriesPrefilter(LIVE_ALLOWLIST)
    for payload in DOCS_FRAMES.values():
        assert prefilter.judge(_raw(payload)) is Verdict.OTHER, payload
        assert prefilter.judge(json.dumps(payload, separators=(",", ":"))) is Verdict.OTHER
        assert prefilter.judge(_raw(_msg_first(payload))) is Verdict.OTHER, payload
    # An error whose free text mentions an rfq_created envelope — escaped
    # quotes ⇒ fail-open, and even unescaped it is inside msg ⇒ OTHER.
    hostile = _raw(
        {"id": 1, "type": "error", "msg": {"code": 10, "msg": 'saw "type": "rfq_created"'}}
    )
    assert "\\" in hostile and prefilter.judge(hostile) is Verdict.FAIL_OPEN
    hostile2 = _raw(
        {
            "id": 1,
            "type": "error",
            "msg": {"code": 10, "type": "rfq_created", "market_ticker": "KXNFLGAME-1"},
        }
    )
    assert prefilter.judge(hostile2) is Verdict.OTHER


def test_prefilter_judges_msg_first_frames_and_fails_open_on_shapeless_text() -> None:
    prefilter = RawSeriesPrefilter(LIVE_ALLOWLIST)
    # msg BEFORE type (review fix 2026-09-05): identified and judged like any
    # other layout — a foreign rfq_created DROPs, an allowlisted one PASSes.
    msg_first = _raw(
        {"sid": 1, "msg": {"id": "x", "market_ticker": "KXNFLGAME-1"}, "type": "rfq_created"}
    )
    assert prefilter.judge(msg_first) is Verdict.DROP
    assert prefilter.judge(_raw(_msg_first(json.loads(_created(["KXNFLGAME-1"]))))) is Verdict.DROP
    assert (
        prefilter.judge(_raw(_msg_first(json.loads(_created(["KXMLBGAME-26SEP05-NYY"])))))
        is Verdict.PASS
    )
    assert prefilter.judge("not json at all") is Verdict.FAIL_OPEN
    # Truncated inside the only ticker value: not JSON at all. The fast path
    # DROPs it (no allowlisted value opens), the parser would reject it as
    # ``ws_bad_json`` — no RFQ reaches the intake either way. Never PASS
    # into a fan-out: the same text with the separator broken takes the slow
    # path and fail-opens to that same rejecting parser.
    full = _created(["KXNFLGAME-1"])
    truncated = full[: full.index('"KXNFLGAME-1"') + 6]  # ends inside the leg's ticker value
    assert truncated.endswith('"market_ticker": "KXNFL')
    assert prefilter.judge(truncated) is Verdict.DROP
    with pytest.raises(ValueError):
        json.loads(truncated)
    odd_ws = truncated.replace('"market_ticker": "KXNFL', '"market_ticker" : "KXNFL')
    assert prefilter.judge(odd_ws) is Verdict.PASS
    # No market_ticker field anywhere: nothing to judge → PASS (fail-open).
    assert (
        prefilter.judge(_raw({"type": "rfq_created", "sid": 1, "msg": {"id": "x"}})) is Verdict.PASS
    )
    # An unreadable ticker occurrence (non-string value) → PASS.
    numeric = _raw({"type": "rfq_created", "sid": 1, "msg": {"id": "x", "market_ticker": 5}})
    assert prefilter.judge(numeric) is Verdict.PASS
    # A single allowlisted ticker anywhere → PASS; none → DROP.
    assert prefilter.judge(_created(["KXMLBGAME-26SEP05-NYY"])) is Verdict.PASS
    assert prefilter.judge(_created(["KXNFLGAME-26SEP07-KC"])) is Verdict.DROP
    assert (
        prefilter.judge(_created(["KXNFLGAME-26SEP07-KC", "KXMLBGAME-26SEP05-NYY"])) is Verdict.PASS
    )
    # The Bundesliga-2 trap: a shared character prefix is NOT the family.
    assert prefilter.judge(_created(["KXBUNDESLIGA2GAME-26SEP05-HSV"])) is Verdict.DROP
    assert prefilter.judge(_created(["KXBUNDESLIGAGAME-26SEP05-FCB"])) is Verdict.PASS


def test_prefilter_rejects_prefixes_json_could_escape() -> None:
    with pytest.raises(ValueError):
        RawSeriesPrefilter(())
    with pytest.raises(ValueError):
        RawSeriesPrefilter(("KXMLB", ""))
    with pytest.raises(ValueError):
        RawSeriesPrefilter(('KX"MLB',))
    with pytest.raises(ValueError):
        RawSeriesPrefilter(("KX\\MLB",))
    with pytest.raises(ValueError):
        RawSeriesPrefilter(("KXMLBé",))
    p = RawSeriesPrefilter(["KXMLBGAME", "KXMLBGAME", "KXMLSGAME-"])
    assert p.prefixes == ("KXMLBGAME", "KXMLSGAME-") and p.msg_type == "rfq_created"


# --------------------------------------------------------------------------- #
# 3. Through the REAL transport: WsManager reader → lanes → dispatcher → intake
# --------------------------------------------------------------------------- #


class _TextFrame:
    def __init__(self, raw: str) -> None:
        self.type = aiohttp.WSMsgType.TEXT
        self.data = raw


class _TextSocket:
    """Socket double fed with PRE-SERIALIZED text (the reader sees bytes as
    the exchange sent them, not a re-serialization)."""

    def __init__(self, frames: list[str]) -> None:
        self._frames: collections.deque[_TextFrame] = collections.deque(
            _TextFrame(r) for r in frames
        )
        self._lock = threading.Lock()
        self.closed = False
        self.sent: list[str] = []

    def __aiter__(self) -> _TextSocket:
        return self

    async def __anext__(self) -> _TextFrame:
        while True:
            if self.closed:
                raise StopAsyncIteration
            with self._lock:
                frame = self._frames.popleft() if self._frames else None
            if frame is not None:
                return frame
            await asyncio.sleep(0.001)

    async def close(self) -> None:
        self.closed = True

    async def send_str(self, data: str) -> None:
        self.sent.append(data)


class _Ctx:
    def __init__(self, sock: _TextSocket) -> None:
        self._sock = sock

    async def __aenter__(self) -> _TextSocket:
        return self._sock

    async def __aexit__(self, *exc: object) -> bool:
        self._sock.closed = True
        return False


class _Signer:
    def headers(self, method: str, path: str) -> dict[str, str]:
        return {}


def _manager(frames: list[str], metrics: Metrics) -> tuple[WsManager, _TextSocket]:
    sock = _TextSocket(frames)
    sockets = [sock]

    def connect(session: Any, url: str, headers: dict[str, str]) -> _Ctx:
        if not sockets:
            raise ConnectionError("no more sockets")
        return _Ctx(sockets.pop(0))

    m = WsManager(
        "wss://example/ws",
        _Signer(),  # type: ignore[arg-type]
        SystemClock(),
        metrics,
        name="t",
        backoff_initial_s=0.01,
        backoff_max_s=0.02,
        connect=connect,
    )
    m.mark_priority("quote_accepted", "quote_executed")
    m.mark_sheddable("rfq_created", stale_after_s=30.0)
    m.mark_sheddable("rfq_deleted")
    return m, sock


def _wire_mix(corpus: list[dict[str, Any]], rng: random.Random) -> tuple[list[str], dict[str, int]]:
    """A realistic slice of the wire: allowlisted + foreign rfq_created,
    rfq_deleted, one accept and one executed, in random order."""
    now = datetime.now(UTC)
    frames: list[str] = []
    counts = {"allowlisted": 0, "foreign": 0, "deleted": 0, "priority": 0}
    for rec in corpus[:60]:  # today's frames: allowlisted
        msg = dict(rec["msg"])
        msg["created_ts"] = now.isoformat()
        frames.append(json.dumps(_envelope(msg)))
        counts["allowlisted"] += 1
    for rec in corpus[400:460]:  # July frames: foreign today
        msg = dict(rec["msg"])
        msg["created_ts"] = now.isoformat()
        frames.append(json.dumps(_envelope(msg)))
        counts["foreign"] += 1
    for i in range(120):
        deleted = {"id": f"del-{i}", "creator_id": "c", "market_ticker": "KXX-1", "deleted_ts": "x"}
        frames.append(json.dumps(_envelope(deleted, msg_type="rfq_deleted")))
        counts["deleted"] += 1
    quote = {"quote_id": "q1", "rfq_id": "r1"}
    frames.append(json.dumps(_envelope(quote, msg_type="quote_accepted")))
    frames.append(json.dumps(_envelope(quote, msg_type="quote_executed")))
    counts["priority"] = 2
    rng.shuffle(frames)
    return frames, counts


async def _run_transport(
    frames: list[str], *, prefilter: RawSeriesPrefilter | None
) -> tuple[Metrics, list[str], list[str], list[str], WsManager]:
    metrics = Metrics()
    m, sock = _manager(frames, metrics)
    if prefilter is not None:
        m.set_raw_prefilter(prefilter)
    intake = RfqIntake(m, metrics, series_prefixes=LIVE_ALLOWLIST)
    seen: list[str] = []
    deleted: list[str] = []
    events: list[str] = []

    async def on_rfq(rfq: Rfq) -> None:
        seen.append(rfq.rfq_id)

    async def on_deleted(rfq_id: str, msg: JsonDict) -> None:
        deleted.append(rfq_id)

    async def on_event(kind: str, msg: JsonDict) -> None:
        events.append(kind)

    intake.on_rfq(on_rfq)
    intake.on_rfq_deleted(on_deleted)
    intake.on_quote_event(on_event)
    m.start()
    try:
        await _until(lambda: m._frames_read >= len(frames), within_s=10.0)
        await _until(
            lambda: not any(m.lane_depths().values()) and not m._lanes.wake_pending,
            within_s=10.0,
        )
        await asyncio.sleep(0.02)
    finally:
        await m.stop()
    return metrics, seen, deleted, events, m


async def test_real_transport_delivers_identical_intake_with_and_without_the_prefilter() -> None:
    rng = random.Random(7)
    corpus = _load_corpus()
    frames, counts = _wire_mix(corpus, rng)
    prefilter = RawSeriesPrefilter(LIVE_ALLOWLIST)

    m_off, seen_off, del_off, ev_off, _ = await _run_transport(frames, prefilter=None)
    m_on, seen_on, del_on, ev_on, mgr = await _run_transport(frames, prefilter=prefilter)

    # Identical outcomes at the intake.
    assert seen_on == seen_off and len(seen_on) == counts["allowlisted"]
    assert del_on == del_off and len(del_on) == counts["deleted"]
    assert ev_on == ev_off and sorted(ev_on) == ["quote_accepted", "quote_executed"]
    # Without the pre-filter the intake did the dropping…
    assert m_off.counter("rfq.dropped_series_fastpath") == counts["foreign"]
    assert m_off.counter("t.msg.rfq_created") == counts["allowlisted"] + counts["foreign"]
    assert m_off.counter("t.prefiltered") == 0
    # …with it the reader did, BEFORE the parse: the dispatcher never saw them.
    assert m_on.counter("t.prefiltered") == counts["foreign"]
    assert m_on.counter("t.prefiltered.rfq_created") == counts["foreign"]
    assert m_on.counter("t.prefilter_passed") == counts["allowlisted"]
    assert m_on.counter("t.prefilter_fail_open") == 0
    assert m_on.counter("t.msg.rfq_created") == counts["allowlisted"]
    assert m_on.counter("rfq.dropped_series_fastpath") == 0
    # Deletions and priority frames took the unchanged path.
    assert m_on.counter("t.msg.rfq_deleted") == counts["deleted"]
    assert m_on.counter("t.msg.quote_accepted") == 1 and m_on.counter("t.msg.quote_executed") == 1
    assert m_on.counter("t.priority_frame") == 2
    # Every frame was READ (health / rate accounting unchanged).
    assert mgr._frames_read == len(frames)
    assert m_on.counter("t.shed_market_frames") == 0
    assert m_on.counter("t.dispatch_queue_overflow") == 0


async def test_start_refuses_a_prefilter_that_targets_a_never_drop_type() -> None:
    metrics = Metrics()
    m, _ = _manager([], metrics)
    m.set_raw_prefilter(RawSeriesPrefilter(LIVE_ALLOWLIST, msg_type="quote_accepted"))
    with pytest.raises(ValueError, match="never-drop lane"):
        m.start()
    m2, _ = _manager([], metrics)
    m2.set_raw_prefilter(RawSeriesPrefilter(LIVE_ALLOWLIST, msg_type="subscribed"))
    with pytest.raises(ValueError):
        m2.start()
    # Clearing it restores today's path.
    m3, _ = _manager([], metrics)
    m3.set_raw_prefilter(RawSeriesPrefilter(LIVE_ALLOWLIST))
    m3.set_raw_prefilter(None)
    m3.start()
    await m3.stop()


async def test_set_raw_prefilter_after_start_is_refused() -> None:
    """The reader binds the pre-filter once per connection and ``start()``
    validates its target, so a late install would silently no-op until the
    next reconnect and then run unvalidated (review fix 2026-09-05): loud."""
    metrics = Metrics()
    m, _ = _manager([], metrics)
    m.start()
    try:
        with pytest.raises(RuntimeError, match="after start"):
            m.set_raw_prefilter(RawSeriesPrefilter(LIVE_ALLOWLIST))
        with pytest.raises(RuntimeError, match="after start"):
            m.set_raw_prefilter(None)
        assert m._raw_prefilter is None
    finally:
        await m.stop()
    # After stop() the manager may be re-armed before a restart.
    m.set_raw_prefilter(RawSeriesPrefilter(LIVE_ALLOWLIST))
    assert m._raw_prefilter is not None


# --------------------------------------------------------------------------- #
# 4. Fan-out: every shard's reader pre-filters; the meter keeps its coverage
# --------------------------------------------------------------------------- #


def test_shard_meter_counts_prefiltered_frames_toward_rate_service_time_and_lag() -> None:
    clock = FakeClock(datetime(2026, 9, 5, 22, 0, tzinfo=UTC))
    meter = ShardMeter(0, clock)
    t0 = clock.monotonic_ns()
    created = (clock.now() - timedelta(seconds=6.0)).isoformat()
    parsed = {"type": "rfq_created", "msg": {"id": "x", "created_ts": created}}
    for _ in range(10):
        meter.observe(parsed, t0, 1_000_000)
    for _ in range(30):
        meter.observe_prefiltered("rfq_created", created, t0, 100_000)  # 0.1 ms each
    meter.observe_prefiltered("rfq_created", None, t0, 100_000)  # unreadable ts: rate only
    clock.advance(1.0)
    w = meter.take(clock.monotonic_ns(), shed_lost_total=0)
    assert w.frames == 41 and w.prefiltered == 31
    assert w.fps == pytest.approx(41.0)
    assert w.utilization == pytest.approx(0.0131)  # 10 ms + 3.1 ms of 1 s
    assert w.lag_rfq.n == 40  # the lag histogram still covers every rfq_created it could read
    assert w.violating(3000.0) and w.capacity_fps(3000.0) == pytest.approx(41.0)
    # Reset per window.
    clock.advance(1.0)
    w2 = meter.take(clock.monotonic_ns(), shed_lost_total=0)
    assert w2.frames == 0 and w2.prefiltered == 0


async def test_fanout_prefilters_on_every_shard_and_reports_it_per_window(tmp_path: Path) -> None:
    exchange = _FakeExchange()
    f, metrics = _fanout(exchange)
    seen: list[str] = []

    async def rec(msg: JsonDict) -> None:
        seen.append(f"{msg['type']}:{msg['msg'].get('id')}:{msg['_shard']}")

    f.on_message("rfq_created", rec)
    f.add_subscription(["communications"])
    # Installed on the HOST before start (the contract): every shard built —
    # now and on any later re-shard — carries the same immutable object.
    f.set_raw_prefilter(RawSeriesPrefilter(("KXT",)))
    await f.apply_shard_factor(3, reason="test")
    f.start()
    await _until(lambda: exchange.live_keys() == set(range(3)))
    await _until(lambda: metrics.counter("t.msg.subscribed") >= 3 and not f._lost)
    try:
        assert all(s._raw_prefilter is f._raw_prefilter for s in f._shards)
        gov = FanoutGovernor(
            f,
            SystemClock(),
            metrics,
            tape_path=fanout_tape_path(tmp_path),
            data_dir=tmp_path,
            boot_key="boot-1",
            boot_started_at_ts=1.0,
            confirm_window_s=3.0,
            refresh_interval_s=60.5,
        )
        now = datetime.now(UTC).isoformat()
        before = sum(s._frames_read for s in f._shards)
        n_allow, n_foreign = 0, 0
        for i in range(90):
            rfq_id = f"r{i}"
            # ``i % 4`` is independent of the fake exchange's digit-sum-mod-3
            # routing, so every shard carries both classes.
            allowlisted = i % 4 == 0
            ticker = "KXT-1" if allowlisted else "KXNFLGAME-1"
            payload = {
                "type": "rfq_created",
                "sid": 1,
                "msg": {"id": rfq_id, "market_ticker": ticker, "created_ts": now},
            }
            if allowlisted:
                n_allow += 1
            else:
                n_foreign += 1
            exchange.route(payload, rfq_id)
        await _until(lambda: sum(s._frames_read for s in f._shards) >= before + 90)
        await _settled(f)
        assert len([s for s in seen if s.startswith("rfq_created:")]) == n_allow
        # A pre-filtered frame never wakes the dispatcher, so its Metrics
        # counts fold on the next dispatched frame, at stop — and on the
        # governor tick (review fix 2026-09-05), so the relight checklist's
        # counters agree with ``ws_inbound_rate`` within a window even on a
        # quiet dispatcher. Plant a pending count and let the TICK fold it.
        f._lanes.count("t.prefilter_probe")
        assert f._lanes.pending_metrics.get("t.prefilter_probe") == 1
        assert metrics.counter("t.prefilter_probe") == 0
        with structlog.testing.capture_logs() as logs:
            d = await gov.tick(reason="refresh")
        assert d is not None
        assert metrics.counter("t.prefilter_probe") == 1 and not f._lanes.pending_metrics
        assert metrics.counter("t.prefiltered") == n_foreign
        assert metrics.counter("t.prefilter_passed") == n_allow
        rate = [e for e in logs if e["event"] == "ws_inbound_rate"]
        assert len(rate) == 1
        line = rate[0]
        assert line["prefiltered"] == n_foreign
        per_shard = line["per_shard_prefiltered"]
        assert sum(per_shard) == n_foreign and len(per_shard) == 3 and all(p > 0 for p in per_shard)
        # The window holds the 90 rfq frames plus the 3 subscribe acks.
        expected_share = n_foreign / (n_foreign + n_allow + 3)
        assert line["prefiltered_share"] == pytest.approx(expected_share, abs=0.02)
        assert line["refresh_interval_s"] == 60.5
        # The log rounds to 0.1 s (a fast test's window can read 0.0); the
        # governor keeps the unrounded window and the log is its rounding.
        assert gov.last_window_elapsed_s is not None and gov.last_window_elapsed_s > 0
        assert line["elapsed_s"] == round(gov.last_window_elapsed_s, 1)
        lag = [e for e in logs if e["event"] == "ws_pipe_lag"][0]
        assert lag["rfq_created"]["n"] == n_allow + n_foreign  # coverage unchanged
        deriv = [e for e in logs if e["event"] == "ws_fanout_derivation"][0]
        assert deriv["refresh_interval_s"] == 60.5
        assert deriv["window_elapsed_s"] == line["elapsed_s"]
    finally:
        await f.stop()


# --------------------------------------------------------------------------- #
# 5. Wall-time governor cadence
# --------------------------------------------------------------------------- #


def test_wall_time_cadence_fires_on_elapsed_time_at_most_once_per_check() -> None:
    clock = FakeClock()
    cadence = WallTimeCadence(clock, 60.5)
    assert cadence.interval_s == 60.5
    assert cadence.elapsed_s() is None
    assert cadence.due() is False  # first call stamps the baseline
    clock.advance(60.4)
    assert cadence.due() is False
    clock.advance(0.1)
    assert cadence.due() is True
    assert cadence.due() is False  # not twice without the interval elapsing
    clock.advance(300.0)  # a pass that overran five intervals…
    assert cadence.due() is True  # …fires ONCE
    assert cadence.due() is False
    assert cadence.elapsed_s() == pytest.approx(0.0)
    cadence.stamp()
    clock.advance(60.5)
    assert cadence.due() is True
    with pytest.raises(ValueError):
        WallTimeCadence(clock, 0.0)


def test_wall_time_cadence_vs_tick_counter_under_slow_passes() -> None:
    """The live defect, replayed: passes of 3 s. 120 ticks = 360 s between
    tick-counted refreshes; the wall-time cadence fires every ~60.5 s."""
    clock = FakeClock()
    cadence = WallTimeCadence(clock, 60.5)
    cadence.stamp()
    ticks = 0
    tick_fires = 0
    wall_fires = 0
    for _ in range(240):  # 240 passes × 3 s = 720 s
        clock.advance(3.0)
        ticks += 1
        if ticks >= 120:
            ticks = 0
            tick_fires += 1
        if cadence.due():
            wall_fires += 1
    assert tick_fires == 2
    assert wall_fires == 11  # floor(720 / 63): each fire lands on the first 3 s pass past 60.5 s
    # Fast passes (0.5 s): both agree on ~60 s.
    clock2 = FakeClock()
    cadence2 = WallTimeCadence(clock2, 60.5)
    cadence2.stamp()
    fires = 0
    for _ in range(121):
        clock2.advance(0.5)
        if cadence2.due():
            fires += 1
    assert fires == 1


class _StubLifecycle:
    """Each maintenance pass advances the FAKE clock by the pass's own
    duration, controlled by the test: ``pass_s[i]`` for pass ``i`` (the last
    entry repeats)."""

    def __init__(self, clock: FakeClock, pass_s: list[float]) -> None:
        self._clock = clock
        self._pass_s = pass_s
        self.passes = 0

    async def maintenance_tick(self) -> None:
        self._clock.advance(self._pass_s[min(self.passes, len(self._pass_s) - 1)])
        self.passes += 1


class _StubGovernor:
    """Stands in for ``FanoutGovernor`` under the PRODUCTION
    ``_refresh_ws_fanout`` (so its ``finally: stamp()`` runs): records the
    reason and takes ``tick_s`` of fake time (a slow tape refresh)."""

    def __init__(self, clock: FakeClock, tick_s: float) -> None:
        self._clock = clock
        self._tick_s = tick_s
        self.reasons: list[str] = []

    async def tick(self, *, reason: str) -> None:
        self.reasons.append(reason)
        self._clock.advance(self._tick_s)


def _floor_s(tmp_path: Path) -> float:
    """The cadence anchor as the app derives it — ``_demo_app`` builds a
    default ``AppConfig`` (heartbeat 15 s ⇒ 15.5 s), NOT the live 60.5 s."""
    from tests.test_quote_app_phase6 import _demo_app

    app = _demo_app(tmp_path / "probe")
    floor = app._stall_wall_floor_s()  # noqa: SLF001
    assert floor == float(app._config.supervisor.heartbeat_timeout_s) + MAINTENANCE_TICK_INTERVAL_S  # noqa: SLF001
    return floor


async def _drive_maintenance(
    tmp_path: Path, *, pass_s: list[float], passes: int, tick_s: float = 0.0
) -> tuple[int, list[str]]:
    from tests.test_quote_app_phase6 import _demo_app

    app: QuoteApp = _demo_app(tmp_path)
    clock = FakeClock()
    app._clock = clock  # noqa: SLF001 (test seam)
    app._progress = ProgressLedger(clock, progress_path(tmp_path))  # noqa: SLF001
    app._fanout_cadence = WallTimeCadence(clock, app._stall_wall_floor_s())  # noqa: SLF001
    app._fanout_cadence.stamp()  # noqa: SLF001 — the boot derivation stamps
    governor = _StubGovernor(clock, tick_s)
    app._fanout_governor = governor  # type: ignore[assignment]  # noqa: SLF001
    lifecycle = _StubLifecycle(clock, pass_s)
    task = asyncio.create_task(app._maintenance_loop(lifecycle))  # type: ignore[arg-type]  # noqa: SLF001
    try:
        await asyncio.wait_for(_until(lambda: lifecycle.passes >= passes, within_s=30.0), 30.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    return lifecycle.passes, governor.reasons


async def test_maintenance_loop_refreshes_the_fanout_on_elapsed_time_once_per_pass(
    tmp_path: Path,
) -> None:
    """The REAL ``_maintenance_loop`` + the REAL ``_refresh_ws_fanout``: a
    pass that takes longer than the interval fires the governor exactly once
    per pass; passes shorter than the interval do not fire it at all —
    whatever the tick counter says. The interval is the app's OWN derived
    floor (review fix 2026-09-05: it was mislabelled 60.5 s here; the demo
    config's is 15.5 s)."""
    floor_s = _floor_s(tmp_path)
    slow_passes, slow_reasons = await _drive_maintenance(
        tmp_path / "slow", pass_s=[floor_s + 1.0], passes=4
    )
    assert slow_passes >= 4
    assert set(slow_reasons) == {"refresh"}
    # One refresh per completed pass (the check runs once per pass; the loop
    # may have completed one more pass than we waited for).
    assert slow_passes - 1 <= len(slow_reasons) <= slow_passes + 1
    fast_passes, fast_reasons = await _drive_maintenance(tmp_path / "fast", pass_s=[1.0], passes=4)
    assert fast_passes >= 4 and fast_reasons == []


async def test_refresh_restamps_the_cadence_after_the_governor_tick(tmp_path: Path) -> None:
    """``_refresh_ws_fanout``'s ``finally: stamp()`` (production code) starts
    the next window when the derivation is DONE: a first slow pass fires the
    governor, whose tick itself takes longer than the interval; the fast
    passes after it must NOT fire again (elapsed since the re-stamp is one
    fast pass). Without the re-stamp every following pass would fire."""
    floor_s = _floor_s(tmp_path)
    passes, reasons = await _drive_maintenance(
        tmp_path / "restamp",
        pass_s=[floor_s + 1.0, 1.0],
        passes=5,
        tick_s=floor_s + 1.0,
    )
    assert passes >= 5
    assert reasons == ["refresh"]


def test_stall_wall_floor_is_the_wedge_anchor_plus_the_loop_cadence(tmp_path: Path) -> None:
    from tests.test_quote_app_phase6 import _demo_app

    app = _demo_app(tmp_path)
    expected = float(app._config.supervisor.heartbeat_timeout_s) + MAINTENANCE_TICK_INTERVAL_S  # noqa: SLF001
    assert app._stall_wall_floor_s() == expected  # noqa: SLF001
    assert app._fanout_cadence.interval_s == pytest.approx(expected)  # noqa: SLF001


# --------------------------------------------------------------------------- #
# 6. App wiring: install fail-open — a refused allowlist entry never bricks a boot
# --------------------------------------------------------------------------- #


async def test_app_runs_without_the_prefilter_when_an_allowlist_entry_could_be_escaped(
    tmp_path: Path,
) -> None:
    """``RawSeriesPrefilter`` refuses an entry JSON could escape; the intake
    and the config accept it. The app must log + count and run today's path
    (review fix 2026-09-05), never die inside the run path."""
    from tests.test_quote_app_phase6 import _demo_app

    app = _demo_app(tmp_path)
    prefixes = ("KXMLBGAME", "KXMLBé")  # non-ASCII: JSON may escape it
    with pytest.raises(ValueError):
        RawSeriesPrefilter(prefixes)
    m, _ = _manager([], Metrics())
    with structlog.testing.capture_logs() as logs:
        assert app._install_raw_prefilter(m, prefixes) is False  # noqa: SLF001
    assert m._raw_prefilter is None
    assert app._metrics.counter("ws.prefilter_not_installed") == 1  # noqa: SLF001
    assert [e["event"] for e in logs] == ["ws_prefilter_not_installed"]
    assert "not plain JSON text" in logs[0]["reason"]
    m.start()  # today's transport, no pre-filter
    await m.stop()
    # The intake filters on the SAME tuple exactly as before.
    ws = FakeWs()
    metrics = Metrics()
    intake = RfqIntake(ws, metrics, series_prefixes=prefixes)
    seen: list[str] = []

    async def on_rfq(rfq: Rfq) -> None:
        seen.append(rfq.rfq_id)

    intake.on_rfq(on_rfq)
    allow = _load_corpus()[0]["msg"]
    assert all(leg["market_ticker"].startswith("KXMLBGAME") for leg in allow["mve_selected_legs"])
    await ws.deliver(_envelope(allow))
    await ws.deliver(_envelope(_foreign_variant(allow, random.Random(1), legs_to_swap=None)))
    assert seen == [allow["id"]] and metrics.counter("rfq.dropped_series_fastpath") == 1
    # A plain-ASCII allowlist installs.
    m2, _ = _manager([], Metrics())
    assert app._install_raw_prefilter(m2, LIVE_ALLOWLIST) is True  # noqa: SLF001
    assert m2._raw_prefilter is not None and m2._raw_prefilter.prefixes == LIVE_ALLOWLIST
    assert app._metrics.counter("ws.prefilter_not_installed") == 1  # noqa: SLF001
