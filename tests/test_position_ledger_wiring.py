"""DURABLE POSITION LEDGER wiring (2026-07-26).

The 2026-07-25 adversarial review found `position_ledger` had NO live writers:
`record_position_open` / `record_position_settled` existed with zero
production callers, the settlement handler was constructed without a store,
and so p_night's day-anchored seed was a silent no-op and settlement
calibration ("do our 4-6 leg K combos hit at the rate we price?") was
unanswerable from local data — which is exactly what blocked that analysis
when the operator asked for it on 7/26.

These pin the two write paths and the round trip through the real Store.
"""

from __future__ import annotations

from pathlib import Path

from combomaker.core.clock import FakeClock
from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.ops.persistence import Store
from combomaker.risk.exposure import LegRef, OpenPosition


def _pos(pid: str = "fill:q1") -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker="KXMVE-C1",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(10_000),
        entry_price_cc=CentiCents(2_500),
        legs=(LegRef("KXMLBKS-26JUL26X-TORHGREENE21-4", "KX-G1", "yes"),),
    )


async def test_open_then_settled_round_trip(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    pos = _pos()
    await store.record_position_open(pos, subaccount="")
    # The settled write UPDATEs WHERE status='open' — without the open row it
    # is a silent no-op, which is precisely how the ledger stayed empty.
    await store.record_position_settled(
        pos.position_id,
        settled_value=0.0,
        realized_pnl_cc=12_345,
        settlement_fee_cc=0,
    )
    async with store._db.execute(  # noqa: SLF001 — direct read, no public getter
        "SELECT status, realized_pnl_cc, reconciled_at FROM position_ledger"
        " WHERE position_id = ?",
        (pos.position_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "settled"
    assert row[1] == 12_345
    assert row[2] is not None
    await store.close()


async def test_day_realized_sees_a_booked_settlement(tmp_path: Path) -> None:
    # The whole point of the wiring: a settled position must now show up in
    # the day-anchored realized sum that seeds p_night across restarts.
    clock = FakeClock()
    store = await Store.open(tmp_path / "t.sqlite3", clock)
    pos = _pos()
    await store.record_position_open(pos, subaccount="")
    await store.record_position_settled(
        pos.position_id, settled_value=0.0, realized_pnl_cc=50_000,
        settlement_fee_cc=0,
    )
    stamp = clock.now()
    lo = stamp.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    hi = stamp.replace(hour=23, minute=59, second=59).isoformat()
    assert await store.day_realized_pnl_cc(lo, hi) == 50_000
    await store.close()
