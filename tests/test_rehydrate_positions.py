"""#33 — exposure-book rehydration on restart.

The exposure book used to start EMPTY after a restart (positions we still held on
the exchange were invisible to the caps + portfolio MC → over-commit risk). These
cover the store rehydration query (``held_positions``) and the QuoteApp step that
rebuilds ``OpenPosition``s from exchange-open ∩ our fills and adds them to the book
(including the Stage-B mutex netting on the rehydrated book).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from combomaker.core.clock import FakeClock
from combomaker.core.conventions import Conventions, Side
from combomaker.ops.persistence import Store
from combomaker.ops.quote_app import QuoteApp
from combomaker.rfq.models import Rfq
from combomaker.risk.exposure import ExposureBook

CONV = Conventions(
    verified=True, source="test",
    maker_side_on_yes_accept=Side.YES, maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True, maker_is_taker_on_fill=False, combo_no_pays_complement=True,
)
def IS_ME(e):     # advance events are mutually exclusive (one team advances)
    return True if (e and e.startswith("KXWCADVANCE")) else None


def _rfq(combo: str, team: str) -> Rfq:
    return Rfq.from_ws({
        "id": f"rfq_{team}", "market_ticker": combo,
        "created_ts": "2026-07-15T10:00:00Z", "target_cost_dollars": "50.00",
        "mve_collection_ticker": "KXMVESPORTS",
        "mve_selected_legs": [
            {"market_ticker": f"KXWCADVANCE-26JUL15ENGARG-{team}", "side": "yes",
             "event_ticker": "KXWCADVANCE-26JUL15ENGARG"},
            {"market_ticker": f"KXWCGOAL-26JUL15ENGARG-{team}P-1", "side": "yes",
             "event_ticker": f"KXWCGOAL-26JUL15ENGARG-{team}P"},
        ],
    })


class _StubRest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def get_positions(self, **_: Any) -> dict[str, Any]:
        return self._payload


async def _seed_store(tmp_path: Path) -> Store:
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    await store.record_rfq(_rfq("KXMVE-ARG", "ARG"), source="ws")
    await store.record_rfq(_rfq("KXMVE-ENG", "ENG"), source="ws")
    # two fills on the ARG combo (aggregation) + one on the ENG combo, all NO.
    await store.record_fill(
        "f1", order_id="o1", combo_ticker="KXMVE-ARG", our_side="no",
        contracts_centi=3000, price_cc=7000, fee_cc=0, expected_edge_cc=100, raw={})
    await store.record_fill(
        "f2", order_id="o2", combo_ticker="KXMVE-ARG", our_side="no",
        contracts_centi=2000, price_cc=8000, fee_cc=0, expected_edge_cc=100, raw={})
    await store.record_fill(
        "f3", order_id="o3", combo_ticker="KXMVE-ENG", our_side="no",
        contracts_centi=4000, price_cc=5000, fee_cc=0, expected_edge_cc=100, raw={})
    return store


async def test_held_positions_aggregates_and_attaches_legs(tmp_path: Path) -> None:
    store = await _seed_store(tmp_path)
    try:
        rows = await store.held_positions(["KXMVE-ARG", "KXMVE-ENG"])
        held = {h["combo_ticker"]: h for h in rows}
        arg = held["KXMVE-ARG"]
        # contracts summed: 3000 + 2000 = 5000 centi-contracts
        assert arg["contracts_centi"] == 5000
        # max-loss preserving price: (30*70 + 20*80)/50 = (2100+1600)/50 = 74.0¢ → 7400cc
        # in cc arithmetic: (3000*7000 + 2000*8000) // 5000 = 37_000_000 // 5000 = 7400
        assert arg["entry_price_cc"] == 7400
        assert arg["our_side"] == "no"
        markets = {leg["market_ticker"] for leg in arg["legs"]}
        assert "KXWCADVANCE-26JUL15ENGARG-ARG" in markets
        # max_loss the cap will see = 5000 * 7400 // 100 = 370_000 cc = $37.00
        assert arg["contracts_centi"] * arg["entry_price_cc"] // 100 == 370_000
    finally:
        await store.close()


async def test_held_positions_not_inflated_by_rfq_tape_fanout(tmp_path: Path) -> None:
    """REGRESSION: the rfqs tape holds one row per re-quote (thousands per combo in
    prod). A ``fills JOIN rfqs`` fans each fill out by that count BEFORE the SUM,
    inflating contracts_centi (and every risk cap that scales with it) by the fanout
    factor — the exact cause of a −259,302-contract delta that blocked ALL quoting.
    Here the ARG combo is re-quoted 6× and filled once for 5000 centi; contracts must
    read 5000, NOT 30_000."""
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        for _ in range(6):  # the combo re-quoted 6 times → 6 rfqs tape rows
            await store.record_rfq(_rfq("KXMVE-ARG", "ARG"), source="ws")
        await store.record_fill(
            "f1", order_id="o1", combo_ticker="KXMVE-ARG", our_side="no",
            contracts_centi=5000, price_cc=7400, fee_cc=0, expected_edge_cc=100, raw={})
        rows = await store.held_positions(["KXMVE-ARG"])
        assert len(rows) == 1
        arg = rows[0]
        assert arg["contracts_centi"] == 5000  # NOT 5000 * 6 = 30_000
        assert arg["entry_price_cc"] == 7400  # price was already fanout-safe
        assert len(arg["legs"]) == 2  # legs attached exactly once
    finally:
        await store.close()


async def test_held_positions_ignores_unrecorded_ticker(tmp_path: Path) -> None:
    store = await _seed_store(tmp_path)
    try:
        held = await store.held_positions(["KXMVE-ARG", "KXMVE-UNKNOWN"])
        assert {h["combo_ticker"] for h in held} == {"KXMVE-ARG"}
    finally:
        await store.close()


async def test_rehydrate_populates_book_and_nets_mutex(tmp_path: Path) -> None:
    store = await _seed_store(tmp_path)
    try:
        # exchange reports BOTH combos open (NO ⇒ negative position_fp).
        rest = _StubRest({"market_positions": [
            {"ticker": "KXMVE-ARG", "position_fp": "-50.00"},
            {"ticker": "KXMVE-ENG", "position_fp": "-40.00"},
        ]})
        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        await QuoteApp._rehydrate_exposure_book(
            cast(Any, None), cast(Any, rest), store, exposure)
        assert len(exposure.positions) == 2
        snap = exposure.snapshot(lambda t: 0.5, mass_acceptance=False)
        game = "26JUL15ENGARG"
        # ARG max_loss = 5000*7400//100 = 370_000; ENG = 4000*5000//100 = 200_000.
        # advance(ARG) ⊥ advance(ENG) ⇒ mutex = max(370_000, 200_000), NOT the sum.
        assert snap.worst_case_loss_by_game_cc[game] == 370_000
    finally:
        await store.close()


async def test_rehydrate_skips_unmodeled_and_empty(tmp_path: Path) -> None:
    store = await _seed_store(tmp_path)
    try:
        # one modeled + one exchange position with no local record → only 1 added.
        rest = _StubRest({"market_positions": [
            {"ticker": "KXMVE-ARG", "position_fp": "-50.00"},
            {"ticker": "KXMVE-EXTERNAL", "position_fp": "-10.00"},
        ]})
        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        await QuoteApp._rehydrate_exposure_book(
            cast(Any, None), cast(Any, rest), store, exposure)
        assert {p.combo_ticker for p in exposure.positions.values()} == {"KXMVE-ARG"}

        # flat (position_fp 0) and empty payloads add nothing.
        exposure2 = ExposureBook(CONV, is_me_event=IS_ME)
        rest2 = _StubRest({"market_positions": [{"ticker": "KXMVE-ARG", "position_fp": "0"}]})
        await QuoteApp._rehydrate_exposure_book(
            cast(Any, None), cast(Any, rest2), store, exposure2)
        assert len(exposure2.positions) == 0
    finally:
        await store.close()


async def test_rehydrate_skips_gated_off_series(tmp_path: Path) -> None:
    """A rehydrated position on a GATED-OFF series (MLB while allowlist=[KXWC]) has
    no subscribed leg books → unavailable marginals → would poison unknown_marginals
    and block ALL quoting. It must be skipped (regression fix, verified live)."""
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    await store.record_rfq(_rfq("KXMVE-ARG", "ARG"), source="ws")  # KXWC legs
    mlb = Rfq.from_ws({
        "id": "rfq_mlb", "market_ticker": "KXMVE-MLB",
        "created_ts": "2026-07-16T00:00:00Z", "target_cost_dollars": "50.00",
        "mve_collection_ticker": "KXMVESPORTS",
        "mve_selected_legs": [
            {"market_ticker": "KXMLBGAME-26JUL16NYMPHI-PHI", "side": "yes",
             "event_ticker": "KXMLBGAME-26JUL16NYMPHI"},
            {"market_ticker": "KXMLBTOTAL-26JUL16NYMPHI-9", "side": "yes",
             "event_ticker": "KXMLBTOTAL-26JUL16NYMPHI"},
        ],
    })
    await store.record_rfq(mlb, source="ws")
    await store.record_fill("f1", order_id="o1", combo_ticker="KXMVE-ARG", our_side="no",
                            contracts_centi=3000, price_cc=7000, fee_cc=0,
                            expected_edge_cc=1, raw={})
    await store.record_fill("f2", order_id="o2", combo_ticker="KXMVE-MLB", our_side="no",
                            contracts_centi=3000, price_cc=7000, fee_cc=0,
                            expected_edge_cc=1, raw={})
    try:
        rest = _StubRest({"market_positions": [
            {"ticker": "KXMVE-ARG", "position_fp": "-30.00"},
            {"ticker": "KXMVE-MLB", "position_fp": "-30.00"},
        ]})
        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        await QuoteApp._rehydrate_exposure_book(
            cast(Any, None), cast(Any, rest), store, exposure, ["KXWC"])
        assert {p.combo_ticker for p in exposure.positions.values()} == {"KXMVE-ARG"}
        # with no allowlist, both come back (default None = keep all)
        exposure2 = ExposureBook(CONV, is_me_event=IS_ME)
        await QuoteApp._rehydrate_exposure_book(
            cast(Any, None), cast(Any, rest), store, exposure2, None)
        assert len(exposure2.positions) == 2
    finally:
        await store.close()
