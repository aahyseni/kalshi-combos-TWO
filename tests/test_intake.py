from typing import Any

from combomaker.rfq.intake import RfqIntake
from combomaker.rfq.models import Rfq
from tests.test_feed import FakeWs

JsonDict = dict[str, Any]

RFQ_MSG: JsonDict = {
    "id": "rfq_1",
    "market_ticker": "KXMVE-C1",
    "created_ts": "2026-07-05T10:00:00Z",
    "contracts_fp": "100.00",
    "mve_collection_ticker": "KXMVESPORTS",
    "mve_selected_legs": [{"market_ticker": "M1", "side": "yes"}],
}


def envelope(msg_type: str, msg: JsonDict) -> JsonDict:
    return {"type": msg_type, "sid": 15, "msg": msg}


async def make() -> tuple[RfqIntake, FakeWs, list[Rfq], list[str], list[str]]:
    ws = FakeWs()
    intake = RfqIntake(ws)
    seen: list[Rfq] = []
    deleted: list[str] = []
    lost: list[str] = []

    async def on_rfq(rfq: Rfq) -> None:
        seen.append(rfq)

    async def on_deleted(rfq_id: str, raw: JsonDict) -> None:
        deleted.append(rfq_id)

    async def on_lost(reason: str) -> None:
        lost.append(reason)

    intake.on_rfq(on_rfq)
    intake.on_rfq_deleted(on_deleted)
    intake.on_channel_lost(on_lost)
    return intake, ws, seen, deleted, lost


async def test_subscribes_to_communications() -> None:
    _, ws, _, _, _ = await make()
    assert ws.subscriptions[0]["channels"] == ["communications"]


async def test_rfq_created_dispatch_and_registry() -> None:
    intake, ws, seen, _, _ = await make()
    await ws.deliver(envelope("rfq_created", RFQ_MSG))
    assert len(seen) == 1
    assert seen[0].rfq_id == "rfq_1"
    assert "rfq_1" in intake.open_rfqs


async def test_rfq_deleted_clears_registry() -> None:
    intake, ws, _, deleted, _ = await make()
    await ws.deliver(envelope("rfq_created", RFQ_MSG))
    await ws.deliver(envelope("rfq_deleted", {"id": "rfq_1", "deleted_ts": "t"}))
    assert deleted == ["rfq_1"]
    assert intake.open_rfqs == {}


async def test_malformed_rfq_skipped_not_fatal() -> None:
    intake, ws, seen, _, _ = await make()
    await ws.deliver(envelope("rfq_created", {"id": "bad"}))  # missing required fields
    await ws.deliver(envelope("rfq_created", RFQ_MSG))
    assert [r.rfq_id for r in seen] == ["rfq_1"]


async def test_terminal_error_codes_fire_channel_lost() -> None:
    _, ws, _, _, lost = await make()
    await ws.deliver({"type": "error", "msg": {"code": 25, "msg": "buffer overflow"}})
    assert lost == ["ws_terminal_error_25"]
    await ws.deliver({"type": "error", "msg": {"code": 6, "msg": "already subscribed"}})
    assert len(lost) == 1  # non-terminal codes don't trigger


async def test_disconnect_clears_open_rfqs() -> None:
    intake, ws, _, _, _ = await make()
    await ws.deliver(envelope("rfq_created", RFQ_MSG))
    await ws.drop_connection()
    assert intake.open_rfqs == {}


async def test_inject_deduplicates() -> None:
    intake, ws, seen, _, _ = await make()
    await ws.deliver(envelope("rfq_created", RFQ_MSG))
    rfq = Rfq.from_ws(RFQ_MSG)
    await intake.inject_rfq(rfq, source="rest_poll")  # already known: no-op
    assert len(seen) == 1
    await intake.inject_rfq(
        Rfq.from_ws({**RFQ_MSG, "id": "rfq_2"}), source="rest_poll"
    )
    assert len(seen) == 2


async def test_quote_events_fan_out() -> None:
    intake, ws, _, _, _ = await make()
    events: list[tuple[str, JsonDict]] = []

    async def on_quote(kind: str, msg: JsonDict) -> None:
        events.append((kind, msg))

    intake.on_quote_event(on_quote)
    await ws.deliver(envelope("quote_accepted", {"quote_id": "q1", "accepted_side": "yes"}))
    assert events == [("quote_accepted", {"quote_id": "q1", "accepted_side": "yes"})]
