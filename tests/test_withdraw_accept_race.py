"""ADVERSARIAL GATE PoCs — TEMPORARY, delete after the gate.

Hunts a defect introduced by the B1 accepted-guard itself: the guard is
evaluated on a SNAPSHOT taken before the resolver's awaits, and never
re-checked afterwards.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from combomaker.core.reasons import ReasonCode
from tests.test_lifecycle import accepted_msg
from tests.test_withdraw_resolution import (
    _429,
    _FakeLister,
    _rig,
    _tick,
    _wire_lister,
)


async def _park_accept_now(rig: Any, quote_id: str) -> asyncio.Task[None]:
    """The taker ACCEPTS `quote_id` right now; the confirm REST call is held
    open so the quote sits ACCEPTED (mid-confirm) from here on."""
    task = asyncio.create_task(rig.lifecycle.on_quote_accepted(accepted_msg(quote_id)))
    for _ in range(2000):
        if rig.sender.confirmed:
            break
        await asyncio.sleep(0)
    assert rig.sender.confirmed == [quote_id], "accept did not park mid-confirm"
    assert rig.lifecycle._open[quote_id].accepted  # noqa: SLF001
    return task


class _RacyLister:
    """The open-quote read. While the request is IN FLIGHT (a real REST RTT),
    the taker accepts one of the pending quotes — exactly the window the
    resolver's pre-await snapshot cannot see."""

    def __init__(self, rig: Any, accept_id: str, answer: set[str]) -> None:
        self._rig = rig
        self._accept_id = accept_id
        self._answer = answer
        self.task: asyncio.Task[None] | None = None
        self.calls = 0

    async def get_quotes(self, **params: Any) -> dict[str, Any]:
        self.calls += 1
        if self.task is None:
            self.task = await _park_accept_now(self._rig, self._accept_id)
        return {"quotes": [{"id": q} for q in sorted(self._answer)], "cursor": ""}


async def _pend_both(rig: Any, ids: list[str]) -> None:
    async def refuses(qid: str) -> dict[str, Any]:
        raise _429()

    rig.sender.delete_quote = refuses  # type: ignore[assignment]
    await rig.lifecycle._withdraw_and_reconcile(  # noqa: SLF001
        ids, ReasonCode.DELETE_TTL_EXPIRED
    )
    for qid in ids:
        assert rig.lifecycle._open[qid].withdraw_pending_reason is not None  # noqa: SLF001


async def _hold_confirm(rig: Any) -> asyncio.Event:
    gate = asyncio.Event()

    async def slow_confirm(qid: str) -> dict[str, Any]:
        rig.sender.confirmed.append(qid)
        await gate.wait()
        return {}

    rig.sender.confirm_quote = slow_confirm  # type: ignore[assignment]
    return gate


async def test_poc_R1_accept_during_the_read_reaps_a_mid_confirm_quote(
    tmp_path: Path,
) -> None:
    """PoC R1 (PoC A, via the race the guard leaves open).

    The resolver snapshots `pending` BEFORE `_read_open_quote_ids`, then awaits
    a REST round trip, then acts on the STALE snapshot. An accept that lands
    inside that RTT is invisible to the `state.accepted` guard — and an ACCEPTED
    quote is NOT OPEN, so the read's answer is exactly "absent" => REAPED."""
    rig = await _rig(tmp_path, "r1.sqlite3", max_open_quotes=10, quotes=2)
    q1, q2 = list(rig.lifecycle._open)  # noqa: SLF001
    await _pend_both(rig, [q1, q2])
    gate = await _hold_confirm(rig)

    lister = _RacyLister(rig, q1, answer=set())  # exchange lists NEITHER
    _wire_lister(rig, _FakeLister(set))          # wires the read budget
    rig.lifecycle._quote_lister = lister         # noqa: SLF001

    await _tick(rig)

    still_open = q1 in rig.lifecycle._open  # noqa: SLF001
    print(
        "\n  PoC R1  (accept lands DURING the resolver's read)\n"
        f"    mid-confirm quote still in _open?   {still_open}\n"
        f"    withdraw_resolve.proven_gone      = "
        f"{rig.metrics.counter('withdraw_resolve.proven_gone')}\n"
        f"    withdraw_resolve.accepted_deferred= "
        f"{rig.metrics.counter('withdraw_resolve.accepted_deferred')}\n"
        f"    quote.deleted.<reason>            = "
        f"{rig.metrics.counter(f'quote.deleted.{ReasonCode.DELETE_TTL_EXPIRED}')}\n"
    )

    gate.set()
    if lister.task is not None:
        await lister.task
    assert still_open, "a mid-confirm quote was REAPED as a proven withdrawal"


async def test_poc_R2_accept_during_the_read_gets_a_fresh_delete(
    tmp_path: Path,
) -> None:
    """PoC R2 (PoC A2, via the same race). Same window, other branch of the
    read: the exchange still LISTS the quote, so the stale snapshot routes it
    into `still_resting` and a FRESH DELETE is fired at a quote we have already
    reserved headroom for and are confirming."""
    rig = await _rig(tmp_path, "r2.sqlite3", max_open_quotes=10, quotes=2)
    q1, q2 = list(rig.lifecycle._open)  # noqa: SLF001
    await _pend_both(rig, [q1, q2])
    gate = await _hold_confirm(rig)

    lister = _RacyLister(rig, q1, answer={q1, q2})  # both still listed OPEN
    _wire_lister(rig, _FakeLister(set))
    rig.lifecycle._quote_lister = lister  # noqa: SLF001

    asked: list[str] = []

    async def refuses(qid: str) -> dict[str, Any]:
        asked.append(qid)
        raise _429()

    rig.sender.delete_quote = refuses  # type: ignore[assignment]

    await _tick(rig)

    print(
        "\n  PoC R2  (accept lands DURING the resolver's read; quote still listed)\n"
        f"    DELETEs issued against the MID-CONFIRM quote: "
        f"{[q for q in asked if q == q1]}\n"
        f"    DELETEs issued against the resting quote    : "
        f"{[q for q in asked if q == q2]}\n"
    )

    gate.set()
    if lister.task is not None:
        await lister.task
    assert q1 not in asked, "a DELETE was issued against a MID-CONFIRM quote"


