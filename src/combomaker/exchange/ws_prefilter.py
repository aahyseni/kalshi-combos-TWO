"""READER-SIDE RAW PRE-FILTER for the communications firehose (2026-09-05).

What was wrong
--------------
After fan-out sharding (N = 3) the exchange pipe lag fell to p50 1.0 s, and
the binding constraint moved INSIDE the process: the dispatcher was still
dropping 1-4k ``rfq_created`` per ~30 s window as STALE at dequeue (407,500
on the 20:37 ET boot, ``ws_stale_market_frames``), the market lane sat 3-5k
deep, and ``event_loop_lag`` ran p50 67-134 ms / p99 0.4-1.0 s with the
``slow_callback`` recorder naming rfq-worker / rfq-retry / streams.read.
Every one of the ~3,000 frames/s (Saturday night, ``ws_inbound_rate``) was
``json.loads``-ed on a reader thread (GIL time the main loop competes for),
pushed through the shared lanes, popped, dispatched and handed to the intake
— which then discarded ~98 % of the ``rfq_created`` frames on its own
leg-series allowlist (``rfq.dropped_series_fastpath``): the communications
channel is the WHOLE exchange's RFQ stream (docs/api-notes/SUMMARY.md:28,
"RFQ events are broadcast to ALL subscribers"; market filters are ignored),
and this bot quotes a few dozen series of it.

Measured (docs/reports/2026-09-05-build-raw-prefilter-governor-tick.md):
the wire mix is ~50.5 % ``rfq_created`` / ~49.5 % ``rfq_deleted`` (93,619 vs
91,637 over the 60 oldest-first shed lines of the 17:59 boot — a
type-agnostic sample of the market lane); ~21 RFQs/s reach pricing against
~1,530 ``rfq_created``/s read, so the allowlist passes ≈ 1.5 % of them
(≤ 11.6 % even if every stale drop were allowlisted). The frames this module
removes are therefore ≈ 49 % of ALL frames read, and the work removed per
frame is the parse, the lane round trip, the dispatch and the intake's own
check — everything but the socket read.

The mechanism
-------------
On the READER THREAD, before ``json.loads`` (``exchange/ws.py`` ``_read_loop``
— the earliest point in the process that sees the text, so the parse never
happens and the frame never enters the lanes), the raw frame text is judged:

  * the envelope's ``type`` is read from the raw text (``raw_frame_type``):
    the ``"type"`` key must be one of the ENVELOPE's own keys — before the
    first nested container or after the last one closes (so never a key
    inside ``msg``), whatever the serializer's key order (type-first,
    sid-first, msg-first: review fix 2026-09-05 — the wire's order is
    unverifiable from this box) — and be followed by ``:`` and a quoted
    string. Anything else is UNIDENTIFIABLE → the frame passes to the full
    parser untouched (fail-open).
  * a frame identified as the target type (``rfq_created``) is DROPPED iff NO
    ``"market_ticker"`` string value anywhere in the raw text starts with an
    allowlisted series prefix — the SAME tuple the intake filters on, passed
    in from the same config object (never a second list). One readable
    occurrence that matches, or one occurrence that cannot be read, ⇒ PASS.
  * a frame of any OTHER identified type is untouched (priority and control
    frames never reach the ticker test — the type check comes first).
  * a frame containing a backslash passes untouched: JSON escapes are the one
    way a string's raw text can differ from its decoded value, and the proof
    below needs them equal. (0 of 20,000 archive frames carried one.)

Decision neutrality — the proof
-------------------------------
The intake (``rfq/intake.py`` ``_handle_rfq_created``) with prefixes P drops
an ``rfq_created`` whose ``mve_selected_legs`` is empty or has ANY dict leg
whose ``market_ticker`` does not ``startswith(P)`` (a leg without the key
reads "" and fails). The pre-filter drops only when (1) the text has no
backslash, so every string value's raw text IS its decoded value; (2) the
envelope type reads ``rfq_created``; (3) at least one ``"market_ticker"``
value exists and EVERY one fails ``startswith(P)``. Every dict leg's
``market_ticker`` is one of those values, so (3) ⇒ no leg passes ⇒ the intake
drops it too (empty legs, or its first dict leg fails). The pre-filter's drop
set is a SUBSET of the intake's drop set: the RFQs that reach ``Rfq.from_ws``
and the fan-out are identical with and without it — property-tested over the
real-frame corpus in ``tests/test_ws_prefilter.py``. Conversely a frame with
one allowlisted leg and one foreign leg PASSES here and is dropped by the
intake exactly as today (the pre-filter only ever removes work).

The one shape where the two paths differ in TELEMETRY (never in outcome): a
non-empty legs array holding only non-object items. The intake's comprehension
skips non-dicts, so it proceeds to ``Rfq.from_ws`` — which raises
``RfqParseError`` ("malformed mve_selected_legs item") and drops the frame
with ``rfq.parse_error`` + a warning; the pre-filter drops it silently
(counted). Both drop; the schema (communications-ws.md §6.1) makes legs
objects, so the exchange never sends this shape.

What it does NOT touch
----------------------
``rfq_deleted`` frames (≈ 49.5 % of the wire) PASS. They are small (~300
bytes, a ~3 µs parse), their handler is a dict pop + two set discards + two
cheap handlers, and no reader-side rule is provably neutral: a delete for an
RFQ whose ``rfq_created`` is still queued in the lane, or that arrived via REST
reconciliation (``inject_rfq``), or whose create landed on another shard
socket, must still reach the intake — the registry and the F2 liveness probe
depend on it. Measured cost of passing them all: see the report.

Blast radius: the reader thread's per-frame path and the metrics below. The
intake's interface and decisions are unchanged (proof above); pricing, risk
and quote construction read nothing from here.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable

_WHITESPACE = " \t\r\n"
_JSON_WS = "[ \\t\\r\\n]*"  # JSON's own whitespace set (RFC 8259 §2), never \s
TYPE_KEY = '"type"'
TICKER_KEY = '"market_ticker"'
CREATED_TS_KEY = '"created_ts"'
# The two separators a JSON serializer emits between key and string value
# (compact / spaced). A frame whose every ticker occurrence uses one of them
# takes the ONE-SCAN fast path; any other layout takes the per-occurrence
# parser (same verdicts, ~4x slower — measured on the real corpus).
_TICKER_SEPARATORS = (TICKER_KEY + ':"', TICKER_KEY + ': "')


def _trie_pattern(words: Iterable[str]) -> str:
    """A regex matching any of ``words`` as a PREFIX of the text at the match
    position, structured as a trie so the engine tests ~one branch per
    character level instead of every alternative (measured 1.0 µs vs 2.0 µs
    for the flat alternation on the real corpus). A word that is itself a
    prefix of another (``KXMLBHR`` / ``KXMLBHRR``) terminates its node, so
    the shorter match suffices — exactly ``str.startswith(tuple)``."""

    def build(ws: list[str]) -> str:
        groups: dict[str, list[str]] = {}
        terminal = False
        for w in ws:
            if w == "":
                terminal = True
                continue
            groups.setdefault(w[0], []).append(w[1:])
        alts = [re.escape(ch) + build(rest) for ch, rest in sorted(groups.items())]
        if not alts:
            return ""
        if len(alts) == 1 and not terminal:
            return alts[0]
        return "(?:" + "|".join(alts) + ")" + ("?" if terminal else "")

    return build(sorted(set(words)))


class Verdict(enum.Enum):
    """The pre-filter's answer for one raw frame."""

    OTHER = "other"  # identified as a non-target type: untouched
    PASS = "pass"  # target type carrying an allowlisted series: full parse
    DROP = "drop"  # target type with NO allowlisted series: parse + lanes skipped
    FAIL_OPEN = "fail_open"  # type unidentifiable / escaped text: full parse


def _quoted_value_after(raw: str, pos: int) -> str | None:
    """The string value that follows ``:`` at ``pos`` (JSON whitespace
    tolerated on both sides), or None if the text there is not ``: "..."``.
    Correct for backslash-free text (a quote then always ends the value)."""
    n = len(raw)
    j = pos
    while j < n and raw[j] in _WHITESPACE:
        j += 1
    if j >= n or raw[j] != ":":
        return None
    j += 1
    while j < n and raw[j] in _WHITESPACE:
        j += 1
    if j >= n or raw[j] != '"':
        return None
    k = raw.find('"', j + 1)
    if k < 0:
        return None
    return raw[j + 1 : k]


def _type_key_value_in(raw: str, start: int, end: int) -> str | None:
    """The string value of a ``"type"`` KEY whose occurrence starts in
    ``raw[start:end]`` — an occurrence that is a VALUE (``"sid": "type"``)
    is followed by ``,`` / ``}`` and skipped; a key with a non-string value
    is skipped too."""
    pos = raw.find(TYPE_KEY, start, end)
    while pos >= 0:
        value = _quoted_value_after(raw, pos + len(TYPE_KEY))
        if value is not None:
            return value
        pos = raw.find(TYPE_KEY, pos + len(TYPE_KEY), end)
    return None


def raw_frame_type(raw: str) -> str | None:
    """The envelope's ``type`` value read from the raw text, or None when it
    cannot be identified (the caller must then treat the frame as unknown
    and hand it to the full parser).

    Sound for backslash-free text (a quote then always delimits a string, so
    ``"type"`` in the text is always a whole string token). The key is read
    from either of the two regions that hold ONLY the envelope's own keys:

      * BEFORE the first nested container opens (``{`` — type-first and
        sid-first layouts, the docs' examples: asyncapi-ws.md §3,
        communications-ws.md:131), or
      * AFTER the last nested container closes (the last ``}`` / ``]``
        before the envelope's own closing brace — msg-first layouts, review
        fix 2026-09-05: the wire's key order is unverifiable from this box,
        so the identifier must not depend on it).

    Every key order of the documented envelope (``type``, ``sid``, ``seq``,
    ``id`` scalars + the ONE nested ``msg`` object) falls in one of the two.
    A brace inside a string value can only SHRINK a region (a ``}`` in a
    ``msg`` string moves the tail's start later; one in a tail string ends
    the region there), never grow it — so a misread is impossible and the
    only failure mode is fail-open. A ``"type"`` key lying BETWEEN two nested
    containers (a shape the envelope never has) is not read → fail-open.
    Measured on the real corpus (msg-first, 902 B median): 0.85 µs vs
    ``json.loads`` 4.2 µs; the full quote-aware depth walk that would also
    read the between-containers shape costs 8.6 µs — MORE than the parse it
    exists to save — and was rejected for that reason."""
    if not raw.startswith("{"):
        return None
    nested = raw.find("{", 1)
    if nested < 0:
        return _type_key_value_in(raw, 1, len(raw))
    value = _type_key_value_in(raw, 1, nested)
    if value is not None:
        return value
    end = len(raw.rstrip(_WHITESPACE)) - 1
    if end <= nested or raw[end] != "}":
        return None
    last_close = max(raw.rfind("}", nested, end), raw.rfind("]", nested, end))
    if last_close < 0:
        return None  # a container opened and never closed: not JSON
    return _type_key_value_in(raw, last_close + 1, end)


def raw_created_ts(raw: str) -> str | None:
    """The first ``"created_ts"`` KEY's string value in the raw text (an
    ``rfq_created`` carries exactly one, on ``msg``; legs carry none) — so a
    frame dropped before parsing still feeds the pipe-lag meter. An
    occurrence that is a VALUE, or a key with a non-string value, is skipped
    for the next (parity with ``raw_frame_type``; review fix 2026-09-05)."""
    pos = raw.find(CREATED_TS_KEY)
    while pos >= 0:
        value = _quoted_value_after(raw, pos + len(CREATED_TS_KEY))
        if value is not None:
            return value
        pos = raw.find(CREATED_TS_KEY, pos + len(CREATED_TS_KEY))
    return None


class RawSeriesPrefilter:
    """Judge raw communications frames against the leg-series allowlist
    (module doc). Immutable after construction; safe to share across reader
    threads (it holds only a tuple of prefixes and a type name)."""

    __slots__ = ("msg_type", "prefixes", "_allowlisted_ticker")

    def __init__(self, series_prefixes: Iterable[str], *, msg_type: str = "rfq_created") -> None:
        prefixes = tuple(dict.fromkeys(str(p) for p in series_prefixes))
        if not prefixes:
            raise ValueError("a raw pre-filter needs at least one series prefix")
        for prefix in prefixes:
            # A prefix must be text JSON never escapes, or the raw-text
            # startswith could disagree with the decoded startswith the
            # intake performs (module doc, proof step 1).
            if (
                not prefix
                or not prefix.isascii()
                or not prefix.isprintable()
                or '"' in prefix
                or "\\" in prefix
            ):
                raise ValueError(f"series prefix is not plain JSON text: {prefix!r}")
        if not msg_type or not msg_type.isascii():
            raise ValueError(f"msg_type must be plain ASCII text, got {msg_type!r}")
        self.msg_type = msg_type
        self.prefixes = prefixes
        # ONE SCAN for "some ticker value starts with an allowlisted prefix":
        # the literal key lets the engine skip to each occurrence, the trie
        # decides in ~one branch per character. Built from the same tuple the
        # slow path and the intake use.
        self._allowlisted_ticker = re.compile(
            TICKER_KEY + _JSON_WS + ":" + _JSON_WS + '"' + _trie_pattern(prefixes)
        )

    def judge(self, raw: str) -> Verdict:
        if "\\" in raw:
            return Verdict.FAIL_OPEN
        kind = raw_frame_type(raw)
        if kind is None:
            return Verdict.FAIL_OPEN
        if kind != self.msg_type:
            return Verdict.OTHER
        return Verdict.PASS if self._carries_allowlisted_ticker(raw) else Verdict.DROP

    def _carries_allowlisted_ticker(self, raw: str) -> bool:
        """True if ANY ``"market_ticker"`` string value in the raw text starts
        with an allowlisted prefix — or if there is no readable occurrence to
        judge (fail-open). ``str.startswith(tuple)`` is the intake's own test,
        run on the same raw text the decoded value equals (no backslashes).

        Fast path (the wire's layouts): when every occurrence of the key is
        followed by one of the two serializer separators — i.e. every ticker
        value opens as a string — one trie-regex search answers it (1.0 µs
        on the real corpus vs 4.6 µs for ``json.loads``). Any other layout
        (odd whitespace, a non-string value) takes the per-occurrence parser
        below, which fail-opens on the first occurrence it cannot read. Both
        paths give the same verdict on every frame they both can read
        (property-tested). A text cut off INSIDE a ticker value is not valid
        JSON: the fast path may DROP it where the slow path would pass it to
        the parser, and the parser rejects it as ``ws_bad_json`` — no RFQ
        reaches the intake on either path (outcome-identical; only the
        counter differs, for a frame a WebSocket never delivers)."""
        occurrences = raw.count(TICKER_KEY)
        if occurrences == 0:
            return True
        compact, spaced = _TICKER_SEPARATORS
        if raw.count(compact) + raw.count(spaced) == occurrences:
            return self._allowlisted_ticker.search(raw) is not None
        return self._carries_allowlisted_ticker_slow(raw)

    def _carries_allowlisted_ticker_slow(self, raw: str) -> bool:
        prefixes = self.prefixes
        pos = raw.find(TICKER_KEY)
        while pos >= 0:
            value = _quoted_value_after(raw, pos + len(TICKER_KEY))
            if value is None or value.startswith(prefixes):
                return True
            pos = raw.find(TICKER_KEY, pos + len(TICKER_KEY))
        return False
