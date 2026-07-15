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
from combomaker.risk.reservation import open_combo_positions_from_positions

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
        self.get_positions_calls: list[dict[str, Any]] = []

    async def get_positions(self, **params: Any) -> dict[str, Any]:
        # Record how the endpoint was queried so tests can assert the QUERY-LAYER
        # subaccount pin (index-scan §portfolio: GET /portfolio/positions returns
        # only the queried subaccount's positions — the real pin mechanism).
        self.get_positions_calls.append(dict(params))
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


async def test_rehydrate_reserves_gated_off_series(tmp_path: Path) -> None:
    """P0-4: a rehydrated position on a GATED-OFF series (MLB while allowlist=[KXWC])
    has no subscribed leg books → unavailable marginals. It must NOT be dropped — it
    is RESERVED into the risk book (``risk_modeled=False``): its exact premium loss /
    gross / per-game concentration STAY in the deterministic caps, but its marginals
    are never queried (no p=0.5) and it never poisons unknown_marginals, so
    quote-eligible quoting is not blocked."""
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
        by_ticker = {p.combo_ticker: p for p in exposure.positions.values()}
        # BOTH are in the book now (P0-4 reserves the gated one, not skips it).
        assert set(by_ticker) == {"KXMVE-ARG", "KXMVE-MLB"}
        assert by_ticker["KXMVE-ARG"].risk_modeled is True
        assert by_ticker["KXMVE-MLB"].risk_modeled is False  # gated → RESERVED
        # the reserved position uses the reserve: id prefix
        reserved = next(p for p in exposure.positions.values()
                        if p.combo_ticker == "KXMVE-MLB")
        assert reserved.position_id == "reserve:KXMVE-MLB"
        # with no allowlist, both are risk-modeled (default None = keep all)
        exposure2 = ExposureBook(CONV, is_me_event=IS_ME)
        await QuoteApp._rehydrate_exposure_book(
            cast(Any, None), cast(Any, rest), store, exposure2, None)
        assert len(exposure2.positions) == 2
        assert all(p.risk_modeled for p in exposure2.positions.values())
    finally:
        await store.close()


# --- P0-5: exact exchange-quantity reconciliation ------------------------------
#
# The exchange (ticker / side / position_fp / subaccount) is AUTHORITATIVE for
# quantity; local fills supply only cost basis, legs, provenance. On mismatch we
# reserve the LARGER exposure and tag it. The nine mandated cases below.

_GAME = "26JUL15ENGARG"


async def _rehydrate(store: Store, payload: dict[str, Any], **kw: Any) -> ExposureBook:
    exposure = ExposureBook(CONV, is_me_event=IS_ME)
    await QuoteApp._rehydrate_exposure_book(
        cast(Any, None), cast(Any, _StubRest(payload)), store, exposure, **kw)
    return exposure


def _arg_max_loss(exposure: ExposureBook) -> int:
    snap = exposure.snapshot(lambda t: 0.5, mass_acceptance=False)
    return snap.worst_case_loss_by_game_cc[_GAME]


async def test_reconcile_exact_match(tmp_path: Path) -> None:
    """Exchange -50.00 == local 5000 centi: folded at the local number, no mismatch,
    positioned as a plain rehydrate (not a reconcile)."""
    store = await _seed_store(tmp_path)
    try:
        exposure = await _rehydrate(store, {"market_positions": [
            {"ticker": "KXMVE-ARG", "position_fp": "-50.00"}]})
        ids = {p.position_id for p in exposure.positions.values()}
        assert ids == {"rehydrate:KXMVE-ARG"}  # exact match ⇒ NOT a reconcile id
        p = next(iter(exposure.positions.values()))
        assert int(p.contracts) == 5000
        assert _arg_max_loss(exposure) == 370_000
    finally:
        await store.close()


async def test_reconcile_exchange_smaller(tmp_path: Path) -> None:
    """Exchange -30.00 (3000) < local 5000: reserve the LARGER (local 5000), tag it."""
    store = await _seed_store(tmp_path)
    try:
        exposure = await _rehydrate(store, {"market_positions": [
            {"ticker": "KXMVE-ARG", "position_fp": "-30.00"}]})
        ids = {p.position_id for p in exposure.positions.values()}
        assert ids == {"reconcile:KXMVE-ARG"}
        p = next(iter(exposure.positions.values()))
        assert int(p.contracts) == 5000  # LARGER of (exchange 3000, local 5000)
        assert p.our_side is Side.NO
        assert _arg_max_loss(exposure) == 370_000  # 5000 * 7400 // 100
    finally:
        await store.close()


async def test_reconcile_exchange_larger(tmp_path: Path) -> None:
    """Exchange -80.00 (8000) > local 5000: reserve the LARGER (exchange 8000) at the
    local cost basis. Exchange quantity is authoritative — the book must reflect it,
    NOT the smaller local number."""
    store = await _seed_store(tmp_path)
    try:
        exposure = await _rehydrate(store, {"market_positions": [
            {"ticker": "KXMVE-ARG", "position_fp": "-80.00"}]})
        ids = {p.position_id for p in exposure.positions.values()}
        assert ids == {"reconcile:KXMVE-ARG"}
        p = next(iter(exposure.positions.values()))
        assert int(p.contracts) == 8000  # LARGER of (exchange 8000, local 5000)
        # max_loss now binds on the exchange count: 8000 * 7400 // 100 = 592_000.
        assert _arg_max_loss(exposure) == 592_000
    finally:
        await store.close()


async def test_reconcile_missing_local_fill(tmp_path: Path) -> None:
    """Exchange reports a ticker we have NO local fill for. No legs ⇒ can't cluster /
    model marginals ⇒ NOT added to the book, surfaced as an unmodeled reconciliation
    gap (never modeled from a guess)."""
    store = await _seed_store(tmp_path)
    try:
        exposure = await _rehydrate(store, {"market_positions": [
            {"ticker": "KXMVE-ARG", "position_fp": "-50.00"},
            {"ticker": "KXMVE-NOFILL", "position_fp": "-20.00"}]})
        assert {p.combo_ticker for p in exposure.positions.values()} == {"KXMVE-ARG"}
    finally:
        await store.close()


async def test_reconcile_manual_trade(tmp_path: Path) -> None:
    """A manual/external trade (exchange holds it, no local record at all) is the
    same fail-closed path: excluded from the modeled book, surfaced for manual
    reconciliation — never invented into a cap."""
    store = await _seed_store(tmp_path)
    try:
        exposure = await _rehydrate(store, {"market_positions": [
            {"ticker": "KXMVE-MANUAL", "position_fp": "-10.00"}]})
        assert len(exposure.positions) == 0
    finally:
        await store.close()


async def test_reconcile_opposite_side(tmp_path: Path) -> None:
    """Local fills say we are NO on ARG, but the exchange reports a YES (positive
    position_fp) — an unexpected opposite-side holding. Exchange side is
    authoritative: fold as YES, tag reconcile."""
    store = await _seed_store(tmp_path)
    try:
        exposure = await _rehydrate(store, {"market_positions": [
            {"ticker": "KXMVE-ARG", "position_fp": "50.00"}]})  # POSITIVE ⇒ YES
        ids = {p.position_id for p in exposure.positions.values()}
        assert ids == {"reconcile:KXMVE-ARG"}
        p = next(iter(exposure.positions.values()))
        assert p.our_side is Side.YES  # exchange side wins over local NO
        assert int(p.contracts) == 5000
    finally:
        await store.close()


async def test_reconcile_settled_ticker_excluded(tmp_path: Path) -> None:
    """A SETTLED / netted-out position reports position_fp == 0 (flat). It is
    excluded at the source — never rehydrated into the risk book."""
    store = await _seed_store(tmp_path)
    try:
        exposure = await _rehydrate(store, {"market_positions": [
            {"ticker": "KXMVE-ARG", "position_fp": "0"},        # settled ⇒ flat
            {"ticker": "KXMVE-ENG", "position_fp": "-40.00"}]})  # still open
        assert {p.combo_ticker for p in exposure.positions.values()} == {"KXMVE-ENG"}
    finally:
        await store.close()


async def test_reconcile_subaccount_pinned_at_query_layer(tmp_path: Path) -> None:
    """P0-5 pin. The subaccount pin is applied at the QUERY LAYER — the documented
    MarketPosition schema has NO per-row subaccount field, so the endpoint itself is
    what filters (GET /portfolio/positions returns only the queried subaccount's
    positions; index-scan §portfolio). We must therefore PASS the pinned subaccount
    to get_positions; the endpoint then never returns another subaccount's rows.

    Here the stub endpoint (as the real one does) returns only subaccount 0's
    positions when queried with subaccount=0. The test asserts the pin was threaded
    to the query, and that every returned row is folded (no fictional row filter)."""
    store = await _seed_store(tmp_path)
    try:
        rest = _StubRest({"market_positions": [
            {"ticker": "KXMVE-ARG", "position_fp": "-50.00"},
            {"ticker": "KXMVE-ENG", "position_fp": "-40.00"}]})
        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        await QuoteApp._rehydrate_exposure_book(
            cast(Any, None), cast(Any, rest), store, exposure, subaccount=0)
        # the pin reached the endpoint (the REAL filter mechanism):
        assert rest.get_positions_calls == [{"subaccount": 0}]
        # both rows the pinned endpoint returned are folded (no row-level filter):
        assert {p.combo_ticker for p in exposure.positions.values()} == {
            "KXMVE-ARG", "KXMVE-ENG"}

        # a numbered subaccount threads through unchanged:
        rest3 = _StubRest({"market_positions": [
            {"ticker": "KXMVE-ARG", "position_fp": "-50.00"}]})
        await QuoteApp._rehydrate_exposure_book(
            cast(Any, None), cast(Any, rest3), store,
            ExposureBook(CONV, is_me_event=IS_ME), subaccount=3)
        assert rest3.get_positions_calls == [{"subaccount": 3}]

        # subaccount=None ⇒ NO param (the exchange default / all-subaccount posture,
        # unchanged from the pre-P0-5 call): the helper never invents a pin.
        rest2 = _StubRest({"market_positions": [
            {"ticker": "KXMVE-ARG", "position_fp": "-50.00"}]})
        await QuoteApp._rehydrate_exposure_book(
            cast(Any, None), cast(Any, rest2), store,
            ExposureBook(CONV, is_me_event=IS_ME))
        assert rest2.get_positions_calls == [{}]
    finally:
        await store.close()


async def test_reconcile_conflicting_legs(tmp_path: Path) -> None:
    """An unparseable / conflicting position_fp is fail-closed: the helper skips the
    row (never invents a quantity) so it is treated as no provable open position."""
    mapped = open_combo_positions_from_positions({"market_positions": [
        {"ticker": "KXMVE-ARG", "position_fp": "-50.00"},
        {"ticker": "KXMVE-BAD", "position_fp": "not-a-number"}]})
    assert set(mapped) == {"KXMVE-ARG"}
    assert mapped["KXMVE-ARG"].side is Side.NO
    assert mapped["KXMVE-ARG"].contracts_centi == 5000


def test_open_combo_positions_side_and_magnitude() -> None:
    """The helper keeps BOTH side and magnitude (unlike the side-only reconcile
    helper): negative ⇒ NO, positive ⇒ YES, magnitude = abs(position_fp)."""
    mapped = open_combo_positions_from_positions({"market_positions": [
        {"ticker": "A", "position_fp": "-30.00"},
        {"ticker": "B", "position_fp": "12.00"},
        {"ticker": "C", "position_fp": "0"}]})
    assert mapped["A"].side is Side.NO and mapped["A"].contracts_centi == 3000
    assert mapped["B"].side is Side.YES and mapped["B"].contracts_centi == 1200
    assert "C" not in mapped  # flat excluded


# --- P0-5: the subaccount pin is a real, threaded config value -----------------


def test_safety_config_subaccount_default_and_range() -> None:
    """P0-5: the pin is a config value (SafetyConfig.subaccount), default 0 =
    primary (the exchange default), constrained to 0..63 (Kalshi's 64-subaccount
    cap). This is what makes the query-layer pin threadable — without it the pin
    could never be set."""
    import pytest
    from pydantic import ValidationError

    from combomaker.ops.config import SafetyConfig

    assert SafetyConfig().subaccount == 0  # default = primary
    assert SafetyConfig(subaccount=7).subaccount == 7
    assert SafetyConfig(subaccount=63).subaccount == 63
    for bad in (-1, 64, 100):
        with pytest.raises(ValidationError):
            SafetyConfig(subaccount=bad)
