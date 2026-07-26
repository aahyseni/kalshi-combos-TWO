"""BOOK COMPLETENESS — an exchange position must NEVER count as zero exposure.

The fail-OPEN gap this pins (measured live 2026-07-26 on the prod-live-wc store):
8 held combos — 7 of them at measurement time, $40.03 of premium at risk on a
$788.22 book, a 5.35% understatement of ``deterministic_max_loss_cc`` — had ZERO
rows on the ``rfqs`` tape. Legs unresolvable ⇒

  * ``_rehydrate_exposure_book`` dropped them (``held.get(ticker) is None`` →
    ``continue``), and
  * ``position_reconcile_unmodeled_once`` skipped them too, because a local
    ``fills`` row made them look "owned by the fill-recovery sweep" — a sweep
    that only ever re-models THIS run's own in-memory quotes.

Counted by NEITHER path, so their premium was invisible to every cap that scales
off det-max, and their legs were absent from ``leg_axis_exposure`` entirely.

Two mechanism repairs, both covered here:

1. Leg provenance is LEDGER-FIRST — the durable ``position_ledger`` (written and
   committed on every confirmed fill) beats the ``rfqs`` OBSERVABILITY tape,
   which is allowed to drop rows. Risk-book completeness never depends on a tape.
2. Reconcile fails CLOSED — a position whose legs NO durable source can resolve
   is adopted as a RESERVE from exchange figures, so its premium counts on the
   deterministic axis even though its legs (and hence its entity/family
   attribution) are unknowable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import structlog

from combomaker.core.clock import FakeClock
from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.ops.quote_app import (
    QuoteApp,
    position_reconcile_unmodeled_once,
    reserve_from_exchange_figures,
)
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.sim.book_model import build_book_model
from tests.test_rehydrate_positions import CONV, IS_ME, _rfq

# A combo we hold 5.00 contracts (500 centi) of, NO side, at 62¢ — $3.10 of
# premium at risk that the exchange reports and no local leg source explains.
ORPHAN = "KXMVECROSSCATEGORY-S2026ORPHAN-AAA"
ORPHAN_EXPOSURE_CC = 31_000  # $3.10, the exchange's own market_exposure


def _positions_payload(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"market_positions": list(rows)}


_ORPHAN_ROW = {
    "ticker": ORPHAN,
    "position_fp": "-5.0000",  # short 5.00 contracts = 500 centi ⇒ NO
    "market_exposure_dollars": "3.10",
}


class _StubRest:
    """get_positions + an always-active get_market (so the drop-settled pass
    keeps everything) — the minimum surface the rehydrator touches."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def get_positions(self, **params: Any) -> dict[str, Any]:
        return self._payload

    async def get_market(self, ticker: str) -> dict[str, Any]:
        return {"market": {"ticker": ticker, "status": "active"}}


async def _store_with_orphan_fill(tmp_path: Path) -> Store:
    """A store that knows we FILLED the orphan combo but has no leg definition
    for it anywhere: no rfqs row (the tape dropped it) and no ledger row."""
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    await store.record_fill(
        "orphan-fill",
        order_id="o-orphan",
        combo_ticker=ORPHAN,
        our_side="no",
        contracts_centi=500,
        price_cc=6200,
        fee_cc=0,
        expected_edge_cc=100,
        raw={},
    )
    return store


# --------------------------------------------------------------------------
# (2) reconcile fails CLOSED: unresolvable legs still raise det-max
# --------------------------------------------------------------------------


async def test_rehydrate_reserves_position_with_unresolvable_legs(
    tmp_path: Path,
) -> None:
    """THE REGRESSION. An exchange position whose legs no durable source can
    resolve must still RAISE the deterministic max loss and appear as a reserve
    — not silently vanish from the risk book."""
    store = await _store_with_orphan_fill(tmp_path)
    try:
        # Pre-state: legs are genuinely unresolvable from every durable source.
        assert await store.held_positions([ORPHAN]) == []
        assert await store.has_fill_for_ticker(ORPHAN) is True

        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        empty_det = build_book_model(
            exposure.positions.values(), marginals=lambda _t: 0.5
        ).reserved_loss_cc
        assert empty_det == 0.0

        rest = _StubRest(_positions_payload(_ORPHAN_ROW))
        with structlog.testing.capture_logs() as cap:
            await QuoteApp._rehydrate_exposure_book(
                cast(Any, None), cast(Any, rest), store, exposure
            )

        # It is IN the book, as a reserve (risk_modeled=False).
        assert list(exposure.positions) == [f"reserve:{ORPHAN}"]
        pos = exposure.positions[f"reserve:{ORPHAN}"]
        assert pos.risk_modeled is False
        assert pos.our_side is Side.NO
        assert int(pos.contracts) == 500
        # Premium at risk is the exchange's own figure, rounded fail-safe UP:
        # entry = ceil(31_000 * 100 / 500) = 6200cc ⇒ max_loss = 500*6200//100.
        assert int(pos.entry_price_cc) == 6200
        assert pos.max_loss_cc == ORPHAN_EXPOSURE_CC

        # And it RAISES the deterministic axis by exactly that premium — the
        # $40.03 that used to be invisible to every cap scaling off det-max.
        model = build_book_model(
            exposure.positions.values(), marginals=lambda _t: 0.5
        )
        assert model.reserved_loss_cc == float(ORPHAN_EXPOSURE_CC)
        assert model.reserved_loss_cc - empty_det == float(ORPHAN_EXPOSURE_CC)

        # Loud, and never counted as "unmodeled/ignored".
        adopted = [e for e in cap if e["event"] == "rehydrate_unknown_legs_reserved"]
        assert len(adopted) == 1
        assert adopted[0]["tickers"] == [ORPHAN]
        assert adopted[0]["reserved_max_loss_cc"] == ORPHAN_EXPOSURE_CC
        assert not [e for e in cap if e["event"] == "rehydrate_unmodeled_positions"]
    finally:
        await store.close()


async def test_rehydrate_unreadable_exposure_reserves_at_full_notional(
    tmp_path: Path,
) -> None:
    """SUPERSEDED PREMISE (2026-07-26 fail-open fix). This test previously
    asserted that a row with NO readable at-risk figure stayed ALARM-ONLY —
    i.e. a real exchange holding contributing ZERO to every cap. That is the
    fail-open defect, not the defense: it is exactly how 21 of 46 live rows
    (six-decimal ``market_exposure_dollars``) left the book entirely.

    Fail-CLOSED now means the PROVEN upper bound, not omission: a Kalshi binary
    costs at most $1.00/contract, so ``contracts × $1.00`` is derived from the
    exchange's own signed count and nothing else. It is deliberately punitive
    and loudly alarmed."""
    store = await _store_with_orphan_fill(tmp_path)
    try:
        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        metrics = Metrics(FakeClock())
        rest = _StubRest(
            _positions_payload({"ticker": ORPHAN, "position_fp": "-5.0000"})
        )
        with structlog.testing.capture_logs() as cap:
            await QuoteApp._rehydrate_exposure_book(
                cast(Any, None), cast(Any, rest), store, exposure, metrics=metrics
            )
        # In the book at the $1.00/contract bound: 500 centi × 10_000cc // 100.
        pos = exposure.positions[f"reserve:{ORPHAN}"]
        assert pos.risk_modeled is False
        assert int(pos.entry_price_cc) == 10_000
        assert pos.max_loss_cc == 50_000  # $5.00 ≥ any true cost basis
        assert pos.max_loss_cc > ORPHAN_EXPOSURE_CC  # strictly conservative
        # Loud + metered, never silent.
        assert metrics.snapshot()["counters"]["exchange_exposure.unreadable"] == 1
        assert (
            metrics.snapshot()["counters"][
                "exchange_exposure.reserved_at_full_notional"
            ]
            == 1
        )
        floored = [e for e in cap if e["event"] == "rehydrate_reserved_at_full_notional"]
        assert len(floored) == 1 and floored[0]["ticker"] == ORPHAN
        assert [e for e in cap if e["event"] == "exchange_exposure_unreadable"]
        # And it is NOT reported as an ignored/unmodeled position any more.
        assert not [e for e in cap if e["event"] == "rehydrate_unmodeled_positions"]
    finally:
        await store.close()


async def test_runtime_reconcile_adopts_unresolvable_legs_despite_local_fill(
    tmp_path: Path,
) -> None:
    """The other half of the gap: at RUNTIME the reconcile net used to skip any
    unmodeled ticker with a local fills row ("the fill-recovery sweep owns it").
    That sweep only re-models THIS run's quotes, so a position with no resolvable
    leg set was owned by nobody. Ownership now requires PROVEN resolvability."""
    store = await _store_with_orphan_fill(tmp_path)
    try:
        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        metrics = Metrics(FakeClock())
        rest = _StubRest(_positions_payload(_ORPHAN_ROW))

        with structlog.testing.capture_logs() as cap:
            unmodeled = await position_reconcile_unmodeled_once(
                cast(Any, rest), exposure, store, metrics, subaccount=0
            )

        assert unmodeled == [ORPHAN]
        assert list(exposure.positions) == [f"reserve:{ORPHAN}"]
        assert exposure.positions[f"reserve:{ORPHAN}"].max_loss_cc == ORPHAN_EXPOSURE_CC
        report = [e for e in cap if e["event"] == "position_reconcile_unmodeled"][0]
        assert report["local_fill_tickers"] == [ORPHAN]
        assert report["unresolvable_leg_tickers"] == [ORPHAN]
        assert report["adopted_as_reserve"] == [ORPHAN]
    finally:
        await store.close()


async def test_runtime_reconcile_still_defers_when_legs_resolve(
    tmp_path: Path,
) -> None:
    """The 2026-07-18 contract is preserved exactly: a ticker with a local fill
    AND a resolvable leg set stays ALARM-ONLY (the fill-recovery sweep re-models
    it exactly), so two writers never race one position."""
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        await store.record_rfq(_rfq("KXMVE-ARG", "ARG"), source="ws")
        await store.record_fill(
            "f1", order_id="o1", combo_ticker="KXMVE-ARG", our_side="no",
            contracts_centi=500, price_cc=6200, fee_cc=0, expected_edge_cc=100,
            raw={})
        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        rest = _StubRest(
            _positions_payload(
                {"ticker": "KXMVE-ARG", "position_fp": "-5.0000",
                 "market_exposure_dollars": "3.10"}
            )
        )
        with structlog.testing.capture_logs() as cap:
            unmodeled = await position_reconcile_unmodeled_once(
                cast(Any, rest), exposure, store, Metrics(FakeClock()), subaccount=0
            )
        assert unmodeled == ["KXMVE-ARG"]
        assert exposure.positions == {}  # deferred, not adopted
        report = [e for e in cap if e["event"] == "position_reconcile_unmodeled"][0]
        assert report["unresolvable_leg_tickers"] == []
        assert report["adopted_as_reserve"] == []
    finally:
        await store.close()


async def test_reserve_superseded_once_ticker_is_modeled(tmp_path: Path) -> None:
    """A reserve is a placeholder for UNKNOWN legs. The exchange reports ONE
    aggregate row per ticker, so once a leg-aware modeled position covers that
    ticker, keeping both would double-count the identical contracts."""
    store = await _store_with_orphan_fill(tmp_path)
    try:
        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        exposure.add_position(
            cast(
                OpenPosition,
                reserve_from_exchange_figures(
                    ORPHAN, Side.NO, 500, ORPHAN_EXPOSURE_CC
                ),
            )
        )
        exposure.add_position(
            OpenPosition(
                position_id=f"fill:{ORPHAN}",
                combo_ticker=ORPHAN,
                collection=None,
                our_side=Side.NO,
                contracts=CentiContracts(500),
                entry_price_cc=CentiCents(6200),
                legs=(LegRef("KXMLBKS-26JUL261215CLETB-CLEPMESSICK77-3",
                             "KXMLBKS-26JUL261215CLETB", "yes"),),
            )
        )
        assert len(exposure.positions) == 2

        rest = _StubRest(_positions_payload(_ORPHAN_ROW))
        await position_reconcile_unmodeled_once(
            cast(Any, rest), exposure, store, Metrics(FakeClock()), subaccount=0
        )
        # The reserve is gone; the leg-aware record carries the whole holding.
        assert list(exposure.positions) == [f"fill:{ORPHAN}"]
    finally:
        await store.close()


# --------------------------------------------------------------------------
# (1b) B1 — the supersede must be QUANTITY-AWARE, not all-or-nothing
#
# A ``fill:<quote_id>`` position carries ONLY its own fill; the exchange row is
# the AGGREGATE. Dropping a whole reserve because ANY modeled position shares
# the ticker fails OPEN, and repeat fills per combo ticker are normal (live
# store: 31 / 21 / 19 / 15 fills on single tickers). The reserve must carry
# ``max(0, exchange − modeled)``, rebuilt from exchange truth every pass.
# --------------------------------------------------------------------------


class _MutableRest(_StubRest):
    """_StubRest whose positions payload can be swapped between passes."""

    def set_payload(self, payload: dict[str, Any]) -> None:
        self._payload = payload


def _row(ticker: str, contracts_centi: int, exposure_cc: int) -> dict[str, Any]:
    """A NO (short) exchange row: signed position_fp + its own cost basis."""
    return {
        "ticker": ticker,
        "position_fp": f"-{contracts_centi / 100:.4f}",
        "market_exposure_dollars": f"{exposure_cc / 10_000:.4f}",
    }


def _fill_position(
    ticker: str, pid: str, contracts_centi: int, entry_cc: int
) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=ticker,
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(contracts_centi),
        entry_price_cc=CentiCents(entry_cc),
        legs=(
            LegRef("KXMLBKS-26JUL261215CLETB-CLEPMESSICK77-3",
                   "KXMLBKS-26JUL261215CLETB", "yes"),
        ),
    )


def _book_totals(exposure: ExposureBook, ticker: str) -> tuple[int, int]:
    """(contracts_centi, premium_cc) the risk book carries for one ticker."""
    rows = [p for p in exposure.positions.values() if p.combo_ticker == ticker]
    return (
        sum(int(p.contracts) for p in rows),
        sum(p.max_loss_cc for p in rows),
    )


async def test_b1_poc_partial_fill_does_not_erase_the_reserve(
    tmp_path: Path,
) -> None:
    """THE REVIEWER'S EXACT PoC. Reserve 500 centi / 31,000cc plus an ADDITIONAL
    fill 200 centi / 12,400cc on the same ticker, exchange reporting the
    aggregate 700 centi / 43,400cc. The all-or-nothing supersede left the book
    holding 12,400cc — a 71% understatement of that ticker's premium. Quantity
    awareness leaves the book EQUAL to exchange truth."""
    store = await _store_with_orphan_fill(tmp_path)
    try:
        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        exposure.add_position(
            cast(
                OpenPosition,
                reserve_from_exchange_figures(ORPHAN, Side.NO, 500, 31_000),
            )
        )
        exposure.add_position(_fill_position(ORPHAN, f"fill:{ORPHAN}", 200, 6_200))
        assert _book_totals(exposure, ORPHAN) == (700, 43_400)

        rest = _MutableRest(_positions_payload(_row(ORPHAN, 700, 43_400)))
        metrics = Metrics(FakeClock())
        with structlog.testing.capture_logs() as cap:
            await position_reconcile_unmodeled_once(
                cast(Any, rest), exposure, store, metrics, subaccount=0
            )

        # The book still carries the FULL exchange holding, split reserve+fill.
        assert _book_totals(exposure, ORPHAN) == (700, 43_400)
        reserve = exposure.positions[f"reserve:{ORPHAN}"]
        assert int(reserve.contracts) == 500          # 700 exchange − 200 modeled
        assert reserve.max_loss_cc == 31_000          # pro-rated exchange basis
        assert reserve.risk_modeled is False
        assert exposure.positions[f"fill:{ORPHAN}"].max_loss_cc == 12_400
        # Book == exchange ⇒ nothing left for the divergence net to alarm on.
        assert metrics.counter("position_reconcile.quantity_divergence") == 0
        assert not [
            e for e in cap if e["event"] == "position_reconcile_reserve_superseded"
        ]
        # And the deterministic axis carries the whole premium.
        model = build_book_model(
            exposure.positions.values(), marginals=lambda _t: 0.5
        )
        assert model.reserved_loss_cc == float(31_000)
    finally:
        await store.close()


async def test_b1_repeat_fills_never_double_count_and_never_drop_premium(
    tmp_path: Path,
) -> None:
    """Repeat fills on ONE combo ticker are normal. After every reconcile pass
    the book must equal exchange truth exactly — never the sum of reserve+fills
    (double count), never just the last fill (dropped premium)."""
    store = await _store_with_orphan_fill(tmp_path)
    try:
        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        exposure.add_position(
            cast(
                OpenPosition,
                reserve_from_exchange_figures(ORPHAN, Side.NO, 700, 43_400),
            )
        )
        rest = _MutableRest(_positions_payload(_row(ORPHAN, 700, 43_400)))
        metrics = Metrics(FakeClock())

        for n in range(1, 8):  # seven successive fills of 100 centi @ 62¢
            exposure.add_position(
                _fill_position(ORPHAN, f"fill:q-{n}", 100, 6_200)
            )
            exch_contracts = 700 + 100 * n
            exch_exposure = 43_400 + 6_200 * n
            rest.set_payload(
                _positions_payload(_row(ORPHAN, exch_contracts, exch_exposure))
            )
            await position_reconcile_unmodeled_once(
                cast(Any, rest), exposure, store, metrics, subaccount=0
            )
            assert _book_totals(exposure, ORPHAN) == (exch_contracts, exch_exposure)
            # The unknown-legs chunk is preserved at full size throughout.
            assert int(exposure.positions[f"reserve:{ORPHAN}"].contracts) == 700
        assert metrics.counter("position_reconcile.quantity_divergence") == 0
    finally:
        await store.close()


async def test_b1_modeled_exceeding_exchange_clamps_to_zero_and_alarms(
    tmp_path: Path,
) -> None:
    """Reverse direction: modeled contracts EXCEED the exchange figure ⇒ the
    reserve clamps to zero (never negative, never resurrected) AND the
    pre-existing quantity-divergence alarm still fires on the excess."""
    store = await _store_with_orphan_fill(tmp_path)
    try:
        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        exposure.add_position(
            cast(
                OpenPosition,
                reserve_from_exchange_figures(ORPHAN, Side.NO, 500, 31_000),
            )
        )
        exposure.add_position(_fill_position(ORPHAN, f"fill:{ORPHAN}", 900, 6_200))
        rest = _MutableRest(_positions_payload(_row(ORPHAN, 700, 43_400)))
        metrics = Metrics(FakeClock())
        with structlog.testing.capture_logs() as cap:
            await position_reconcile_unmodeled_once(
                cast(Any, rest), exposure, store, metrics, subaccount=0
            )
        assert f"reserve:{ORPHAN}" not in exposure.positions
        assert _book_totals(exposure, ORPHAN) == (900, 55_800)
        assert metrics.counter("position_reconcile.quantity_divergence") == 1
        superseded = [
            e for e in cap if e["event"] == "position_reconcile_reserve_superseded"
        ]
        assert len(superseded) == 1
        assert superseded[0]["modeled_contracts_centi"] == 900
    finally:
        await store.close()


async def test_b1_fully_resolvable_position_leaves_no_reserve(
    tmp_path: Path,
) -> None:
    """A leg-aware record that covers the WHOLE exchange holding leaves NO
    reserve — the double-count the supersede exists to prevent."""
    store = await _store_with_orphan_fill(tmp_path)
    try:
        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        exposure.add_position(
            cast(
                OpenPosition,
                reserve_from_exchange_figures(ORPHAN, Side.NO, 700, 43_400),
            )
        )
        exposure.add_position(_fill_position(ORPHAN, f"fill:{ORPHAN}", 700, 6_200))
        rest = _MutableRest(_positions_payload(_row(ORPHAN, 700, 43_400)))
        metrics = Metrics(FakeClock())
        await position_reconcile_unmodeled_once(
            cast(Any, rest), exposure, store, metrics, subaccount=0
        )
        assert list(exposure.positions) == [f"fill:{ORPHAN}"]
        assert _book_totals(exposure, ORPHAN) == (700, 43_400)
        assert metrics.counter("position_reconcile.quantity_divergence") == 0
    finally:
        await store.close()


async def test_b1_unresolvable_legs_still_raise_deterministic_max_loss(
    tmp_path: Path,
) -> None:
    """The ORIGINAL fail-closed property survives the B1 repair: an exchange
    position no durable source can re-model still enters the book as a reserve
    and still raises the deterministic max loss."""
    store = await _store_with_orphan_fill(tmp_path)
    try:
        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        assert (
            build_book_model(
                exposure.positions.values(), marginals=lambda _t: 0.5
            ).reserved_loss_cc
            == 0.0
        )
        rest = _MutableRest(_positions_payload(_ORPHAN_ROW))
        await position_reconcile_unmodeled_once(
            cast(Any, rest), exposure, store, Metrics(FakeClock()), subaccount=0
        )
        assert list(exposure.positions) == [f"reserve:{ORPHAN}"]
        model = build_book_model(
            exposure.positions.values(), marginals=lambda _t: 0.5
        )
        assert model.reserved_loss_cc == float(ORPHAN_EXPOSURE_CC)
    finally:
        await store.close()


# --------------------------------------------------------------------------
# (1) ledger-first leg provenance
# --------------------------------------------------------------------------

LEDGER_LEGS = (
    LegRef("KXMLBKS-26JUL261215CLETB-CLEPMESSICK77-3",
           "KXMLBKS-26JUL261215CLETB", "yes"),
    LegRef("KXMLBHIT-26JUL261215CLETB-CLESKWAN38-1",
           "KXMLBHIT-26JUL261215CLETB", "yes"),
)


async def _record_ledger_open(store: Store, ticker: str) -> None:
    await store.record_position_open(
        OpenPosition(
            position_id=f"fill:{ticker}",
            combo_ticker=ticker,
            collection="KXMVECROSSCATEGORY",
            our_side=Side.NO,
            contracts=CentiContracts(500),
            entry_price_cc=CentiCents(6200),
            legs=LEDGER_LEGS,
        ),
        subaccount="0",
    )


async def test_ledger_legs_are_preferred_over_the_rfqs_tape(
    tmp_path: Path,
) -> None:
    """LEDGER-FIRST. With BOTH sources present and DISAGREEING, the durable
    position_ledger wins — the rfqs tape is observability, not risk truth."""
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        # The tape says this combo is a two-leg WC advance/goal parlay...
        await store.record_rfq(_rfq("KXMVE-ARG", "ARG"), source="ws")
        # ...the durable ledger says it is the MLB strikeout/hit pair we fill-
        # confirmed. The ledger is the source of truth for what we hold.
        await _record_ledger_open(store, "KXMVE-ARG")
        await store.record_fill(
            "f1", order_id="o1", combo_ticker="KXMVE-ARG", our_side="no",
            contracts_centi=500, price_cc=6200, fee_cc=0, expected_edge_cc=100,
            raw={})

        rows = await store.held_positions(["KXMVE-ARG"])
        assert len(rows) == 1
        assert rows[0]["legs_source"] == "position_ledger"
        assert [leg["market_ticker"] for leg in rows[0]["legs"]] == [
            leg.market_ticker for leg in LEDGER_LEGS
        ]
        assert rows[0]["collection"] == "KXMVECROSSCATEGORY"
    finally:
        await store.close()


async def test_ledger_only_ticker_is_fully_rehydrated_not_reserved(
    tmp_path: Path,
) -> None:
    """The durable repair: a combo the rfqs tape NEVER recorded (the live
    signature — all 8 gap tickers had ZERO rfqs rows) is rehydrated with its
    REAL legs from the ledger, so it lands on the per-entity / per-family axes
    instead of degrading to an attribution-blind reserve."""
    store = await _store_with_orphan_fill(tmp_path)
    try:
        await _record_ledger_open(store, ORPHAN)
        rows = await store.held_positions([ORPHAN])
        assert len(rows) == 1
        assert rows[0]["legs_source"] == "position_ledger"

        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        rest = _StubRest(_positions_payload(_ORPHAN_ROW))
        await QuoteApp._rehydrate_exposure_book(
            cast(Any, None), cast(Any, rest), store, exposure
        )
        assert list(exposure.positions) == [f"rehydrate:{ORPHAN}"]
        pos = exposure.positions[f"rehydrate:{ORPHAN}"]
        assert pos.risk_modeled is True
        # The KXMLBKS strikeout family is now VISIBLE on the leg axis — the
        # exact axis the invisible positions were concentrated in.
        snap = exposure.snapshot(lambda _t: 0.5, mass_acceptance=False)
        assert snap.committed_loss_by_family_cc["KXMLBKS:yes"] == ORPHAN_EXPOSURE_CC
        assert snap.committed_loss_by_entity_cc[
            "KXMLBKS:CLEPMESSICK77:yes"
        ] == ORPHAN_EXPOSURE_CC
    finally:
        await store.close()


async def test_tape_still_answers_when_the_ledger_cannot(tmp_path: Path) -> None:
    """Fallback preserved: pre-ledger positions (every fill before the ledger
    writer existed) still resolve from the rfqs tape."""
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        await store.record_rfq(_rfq("KXMVE-ARG", "ARG"), source="ws")
        await store.record_fill(
            "f1", order_id="o1", combo_ticker="KXMVE-ARG", our_side="no",
            contracts_centi=500, price_cc=6200, fee_cc=0, expected_edge_cc=100,
            raw={})
        rows = await store.held_positions(["KXMVE-ARG"])
        assert len(rows) == 1
        assert rows[0]["legs_source"] == "rfqs_tape"
        assert len(rows[0]["legs"]) == 2
    finally:
        await store.close()


async def test_ledger_leg_order_is_not_a_false_conflict(tmp_path: Path) -> None:
    """Two ledger rows spelling the SAME leg set in a different JSON order share
    one order-independent ``leg_set_hash`` ⇒ ONE identity, not an ambiguous
    provenance that would fail closed and needlessly degrade to a reserve."""
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        await _record_ledger_open(store, "KXMVE-ARG")
        await store.record_position_open(
            OpenPosition(
                position_id="fill:KXMVE-ARG-second",
                combo_ticker="KXMVE-ARG",
                collection="KXMVECROSSCATEGORY",
                our_side=Side.NO,
                contracts=CentiContracts(100),
                entry_price_cc=CentiCents(6200),
                legs=tuple(reversed(LEDGER_LEGS)),  # same set, reversed order
            ),
            subaccount="0",
        )
        await store.record_fill(
            "f1", order_id="o1", combo_ticker="KXMVE-ARG", our_side="no",
            contracts_centi=500, price_cc=6200, fee_cc=0, expected_edge_cc=100,
            raw={})
        rows = await store.held_positions(["KXMVE-ARG"])
        assert len(rows) == 1
        assert rows[0]["legs_source"] == "position_ledger"
        assert {leg["market_ticker"] for leg in rows[0]["legs"]} == {
            leg.market_ticker for leg in LEDGER_LEGS
        }
    finally:
        await store.close()


async def test_conflicting_ledger_leg_sets_fail_closed_to_a_reserve(
    tmp_path: Path,
) -> None:
    """Genuinely AMBIGUOUS durable provenance (two DIFFERENT leg sets on one
    combo ticker) is rejected rather than guessed — and the position is then
    reserved from exchange figures, so failing closed never means zero."""
    store = await _store_with_orphan_fill(tmp_path)
    try:
        await _record_ledger_open(store, ORPHAN)
        await store.record_position_open(
            OpenPosition(
                position_id=f"fill:{ORPHAN}-other",
                combo_ticker=ORPHAN,
                collection="KXMVECROSSCATEGORY",
                our_side=Side.NO,
                contracts=CentiContracts(100),
                entry_price_cc=CentiCents(6200),
                legs=(LegRef("KXMLBGAME-26JUL261335TORBOS-TOR",
                             "KXMLBGAME-26JUL261335TORBOS", "yes"),),
            ),
            subaccount="0",
        )
        assert await store.held_positions([ORPHAN]) == []  # ambiguous ⇒ rejected

        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        rest = _StubRest(_positions_payload(_ORPHAN_ROW))
        await QuoteApp._rehydrate_exposure_book(
            cast(Any, None), cast(Any, rest), store, exposure
        )
        assert list(exposure.positions) == [f"reserve:{ORPHAN}"]
        assert exposure.positions[f"reserve:{ORPHAN}"].max_loss_cc == ORPHAN_EXPOSURE_CC
    finally:
        await store.close()


async def test_ledger_hit_skips_the_rfqs_tape_query(tmp_path: Path) -> None:
    """Throughput guard: a ledger-resolved ticker must NOT also index-scan the
    (huge, 54GB-in-prod) rfqs table. The tape query runs only for leftovers."""
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        await _record_ledger_open(store, "KXMVE-ARG")
        await store.record_fill(
            "f1", order_id="o1", combo_ticker="KXMVE-ARG", our_side="no",
            contracts_centi=500, price_cc=6200, fee_cc=0, expected_edge_cc=100,
            raw={})

        seen: list[str] = []
        real_execute = store._db.execute

        def _spy(sql: str, *args: Any, **kw: Any) -> Any:
            seen.append(sql)
            return real_execute(sql, *args, **kw)

        store._db.execute = _spy  # type: ignore[method-assign]
        try:
            rows = await store.held_positions(["KXMVE-ARG"])
        finally:
            store._db.execute = real_execute  # type: ignore[method-assign]

        assert rows[0]["legs_source"] == "position_ledger"
        assert not [s for s in seen if "FROM rfqs" in s]
        assert [s for s in seen if "FROM position_ledger" in s]
    finally:
        await store.close()


def test_reserve_helper_never_understates_the_exchange_figure() -> None:
    """The ceil-div entry price is fail-safe LARGER on EVERY remainder: the
    booked max_loss_cc is never below the exchange's own premium at risk."""
    for exposure_cc in range(1, 400):
        for contracts in (1, 7, 100, 319, 1797, 32_717):
            pos = reserve_from_exchange_figures(
                "T", Side.NO, contracts, exposure_cc
            )
            assert pos is not None
            assert pos.max_loss_cc >= exposure_cc
    # No provable at-risk figure ⇒ nothing is invented.
    assert reserve_from_exchange_figures("T", Side.NO, 500, None) is None
    assert reserve_from_exchange_figures("T", Side.NO, 500, 0) is None
    assert reserve_from_exchange_figures("T", Side.NO, 0, 31_000) is None


async def test_ledger_legs_json_round_trips_unchanged(tmp_path: Path) -> None:
    """The ledger's legs_json is the exact shape held_positions parses (the same
    market_ticker/event_ticker/side dicts the rfqs tape stores)."""
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        await _record_ledger_open(store, "KXMVE-ARG")
        row = await store.ledger_position("fill:KXMVE-ARG")
        assert row is not None
        assert row["legs"] == json.loads(
            json.dumps(
                [
                    {"market_ticker": leg.market_ticker,
                     "event_ticker": leg.event_ticker,
                     "side": leg.side}
                    for leg in LEDGER_LEGS
                ]
            )
        )
    finally:
        await store.close()
