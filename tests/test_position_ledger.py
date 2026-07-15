"""P1.10 — durable position ledger.

The ledger carries every field the audit plan mandates (exchange quantity/side,
cost, fees, subaccount, status, settlement, reconciliation time, leg-set hash),
is keyed on the exchange position_id, and settles idempotently.
"""

from pathlib import Path

import pytest

from combomaker.core.clock import FakeClock
from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.ops.persistence import Store
from combomaker.risk.exposure import LegRef, OpenPosition, leg_set_hash

LEGS = (
    LegRef(market_ticker="M2", event_ticker="E2", side="no"),
    LegRef(market_ticker="M1", event_ticker="E1", side="yes"),
)


def make_position(pid: str = "q1:yes") -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker="KXCOMBO-1",
        collection="KXCOL",
        our_side=Side.NO,
        contracts=CentiContracts(150),  # 1.50 contracts
        entry_price_cc=CentiCents(4_000),  # $0.40/contract
        legs=LEGS,
    )


def test_leg_set_hash_order_independent_and_side_aware() -> None:
    a = leg_set_hash(LEGS)
    b = leg_set_hash(tuple(reversed(LEGS)))
    assert a == b  # order-independent
    assert len(a) == 64  # SHA-256 hex
    # Flipping a side changes identity.
    flipped = (
        LegRef(market_ticker="M1", event_ticker="E1", side="no"),
        LegRef(market_ticker="M2", event_ticker="E2", side="no"),
    )
    assert leg_set_hash(flipped) != a
    # event_ticker is excluded from identity (nullable field must not split).
    no_events = (
        LegRef(market_ticker="M1", event_ticker=None, side="yes"),
        LegRef(market_ticker="M2", event_ticker=None, side="no"),
    )
    assert leg_set_hash(no_events) == a


def test_leg_set_hash_empty_fails_closed() -> None:
    with pytest.raises(ValueError):
        leg_set_hash(())


async def test_ledger_records_all_mandated_fields(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        pos = make_position()
        await store.record_position_open(pos, subaccount="maker-A", fees_cc=25)
        assert await store.count("position_ledger") == 1

        row = await store.ledger_position("q1:yes")
        assert row is not None
        # exchange quantity/side + cost + fees + subaccount + status + leg hash
        assert row["contracts_centi"] == 150
        assert row["our_side"] == "no"
        assert row["entry_price_cc"] == 4_000
        assert row["cost_cc"] == pos.max_loss_cc == 6_000  # 1.50 * $0.40
        assert row["fees_cc"] == 25
        assert row["subaccount"] == "maker-A"
        assert row["status"] == "open"
        assert row["leg_set_hash"] == leg_set_hash(LEGS)
        assert row["collection_ticker"] == "KXCOL"
        # settlement / reconciliation fields are unset while open
        assert row["settled_value"] is None
        assert row["realized_pnl_cc"] is None
        assert row["reconciled_at"] is None
    finally:
        await store.close()


async def test_record_open_is_idempotent(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        pos = make_position()
        await store.record_position_open(pos, subaccount="maker-A")
        await store.record_position_open(pos, subaccount="maker-A", fees_cc=10)
        assert await store.count("position_ledger") == 1  # UPSERT, not duplicate
        row = await store.ledger_position("q1:yes")
        assert row is not None and row["fees_cc"] == 10
    finally:
        await store.close()


async def test_settlement_records_value_pnl_fee_and_reconcile_time(
    tmp_path: Path,
) -> None:
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        await store.record_position_open(make_position(), subaccount="maker-A", fees_cc=5)
        await store.record_position_settled(
            "q1:yes",
            settled_value=0.0,  # combo settled NO ⇒ our long-NO wins
            realized_pnl_cc=9_000,
            settlement_fee_cc=3,
        )
        row = await store.ledger_position("q1:yes")
        assert row is not None
        assert row["status"] == "settled"
        assert row["settled_value"] == 0.0
        assert row["realized_pnl_cc"] == 9_000
        assert row["settlement_fee_cc"] == 3
        assert row["fees_cc"] == 8  # 5 open + 3 settlement
        assert row["reconciled_at"] is not None
    finally:
        await store.close()


async def test_settlement_idempotent_and_never_regresses(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        await store.record_position_open(make_position(), subaccount="maker-A")
        await store.record_position_settled(
            "q1:yes", settled_value=1.0, realized_pnl_cc=-6_000, settlement_fee_cc=0
        )
        first = await store.ledger_position("q1:yes")
        # A re-polled settlement (status already 'settled') is a no-op.
        await store.record_position_settled(
            "q1:yes", settled_value=1.0, realized_pnl_cc=-6_000, settlement_fee_cc=0
        )
        again = await store.ledger_position("q1:yes")
        assert first == again

        # A re-recorded OPEN after settlement never regresses status→open.
        await store.record_position_open(make_position(), subaccount="maker-A")
        after = await store.ledger_position("q1:yes")
        assert after is not None and after["status"] == "settled"
    finally:
        await store.close()


async def test_settle_unknown_position_is_noop(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        await store.record_position_settled(
            "nope", settled_value=0.0, realized_pnl_cc=0, settlement_fee_cc=0
        )
        assert await store.ledger_position("nope") is None
    finally:
        await store.close()
