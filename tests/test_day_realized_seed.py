"""DAY-ANCHORED realized P&L reconstruction (2026-07-25 operator KPI:
p_night = P(the day ends positive) must roll across restarts — the
in-process accumulator resets at boot). ``Store.day_realized_pnl_cc``
mirrors what ``record_realized_pnl`` accumulates in one process:
settlement realized P&L reconciled in the window minus fill fees."""

from __future__ import annotations

from pathlib import Path

from combomaker.core.clock import FakeClock
from combomaker.ops.persistence import Store


async def _settled_row(
    store: Store, pid: str, at: str, realized_cc: int
) -> None:
    await store._db.execute(  # noqa: SLF001 — direct fixture insert
        "INSERT INTO position_ledger (position_id, opened_at, combo_ticker,"
        " collection_ticker, subaccount, our_side, contracts_centi,"
        " entry_price_cc, cost_cc, fees_cc, leg_set_hash, legs_json, status,"
        " settled_value, realized_pnl_cc, settlement_fee_cc, reconciled_at)"
        " VALUES (?, ?, 'C', NULL, '', 'no', 100, 5000, 5000, 0, ?, '[]',"
        " 'settled', 1.0, ?, 0, ?)",
        (pid, at, f"hash-{pid}", realized_cc, at),
    )
    await store._db.commit()  # noqa: SLF001


async def _fill_row(store: Store, ref: str, at: str, fee_cc: int) -> None:
    await store._db.execute(  # noqa: SLF001
        "INSERT INTO fills (at, fill_ref, order_id, combo_ticker, our_side,"
        " contracts_centi, price_cc, fee_cc, expected_edge_cc, raw_json)"
        " VALUES (?, ?, NULL, 'C', 'no', 100, 5000, ?, 0, '{}')",
        (at, ref, fee_cc),
    )
    await store._db.commit()  # noqa: SLF001


async def test_day_window_sums_settlements_minus_fees(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    day = "2026-07-25T"
    await _settled_row(store, "p1", day + "14:00:00+00:00", 40_000)
    await _settled_row(store, "p2", day + "20:00:00+00:00", -15_000)
    # Outside the window: yesterday + tomorrow.
    await _settled_row(store, "p0", "2026-07-24T23:00:00+00:00", 99_000)
    await _settled_row(store, "p3", "2026-07-26T04:30:00+00:00", 99_000)
    await _fill_row(store, "f1", day + "15:00:00+00:00", 120)
    await _fill_row(store, "f2", "2026-07-24T15:00:00+00:00", 999)
    got = await store.day_realized_pnl_cc(
        "2026-07-25T04:00:00+00:00", "2026-07-26T04:00:00+00:00"
    )
    assert got == 40_000 - 15_000 - 120
    await store.close()


async def test_empty_ledger_is_zero(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    assert await store.day_realized_pnl_cc(
        "2026-07-25T04:00:00+00:00", "2026-07-26T04:00:00+00:00"
    ) == 0
    await store.close()
