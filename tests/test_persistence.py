from pathlib import Path

from combomaker.core.clock import FakeClock
from combomaker.ops.persistence import Store
from combomaker.rfq.models import Rfq

RFQ = Rfq.from_ws(
    {
        "id": "rfq_1",
        "market_ticker": "KXMVE-C1",
        "created_ts": "2026-07-05T10:00:00Z",
        "target_cost_dollars": "50.00",
        "mve_collection_ticker": "KXMVESPORTS",
        "mve_selected_legs": [
            {"market_ticker": "M1", "side": "yes", "event_ticker": "E1"},
        ],
    }
)


async def test_roundtrip(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        await store.record_rfq(RFQ, source="ws")
        await store.record_rfq_deleted("rfq_1", {"id": "rfq_1"})
        await store.record_decision(
            "no_quote", "rfq_1", ["skip_leg_stale", "skip_in_play"], {"k": "v"}
        )
        await store.record_would_quote(
            "rfq_1",
            fair_prob=0.31,
            fair_cc=3_100,
            width_cc=600,
            leg_probs=(0.62, 0.5),
            context={},
        )
        assert await store.count("rfqs") == 1
        assert await store.count("rfq_deletions") == 1
        assert await store.count("decisions") == 1
        assert await store.count("would_quotes") == 1
        reasons = await store.decision_reason_counts()
        assert reasons == {"skip_leg_stale": 1, "skip_in_play": 1}
    finally:
        await store.close()


async def test_record_rfq_seen_at_override_and_default(tmp_path: Path) -> None:
    """rfqs.seen_at semantics (risk audit fix 2026-07-16): the fast-lane
    passes the wall-clock captured at worker PICKUP so the column keeps its
    pre-fast-lane meaning even though the row lands after pricing; the default
    (no override) still stamps call time for every other caller."""
    clock = FakeClock()
    store = await Store.open(tmp_path / "t.sqlite3", clock)
    try:
        pickup = clock.now()
        clock.advance(2.0)  # pricing-pool dwell between pickup and the write
        await store.record_rfq(RFQ, source="ws", seen_at=pickup)
        await store.record_rfq(RFQ, source="ws")  # default: call-time stamp
        async with store._db.execute(  # noqa: SLF001
            "SELECT seen_at FROM rfqs ORDER BY id"
        ) as cursor:
            rows = [row[0] async for row in cursor]
        assert rows[0] == pickup.isoformat()          # override: pickup time
        assert rows[1] == clock.now().isoformat()     # default: write time
        assert rows[0] != rows[1]
    finally:
        await store.close()


async def test_open_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "t.sqlite3"
    store1 = await Store.open(path, FakeClock())
    await store1.record_rfq(RFQ, source="ws")
    await store1.close()
    store2 = await Store.open(path, FakeClock())  # DDL re-runs harmlessly
    try:
        assert await store2.count("rfqs") == 1
    finally:
        await store2.close()
