"""DURABLE LEDGER IDENTITY — settled rows must survive a restart (2026-07-26).

THE DEFECT these pin: ``SettlementHandler`` wrote the durable ``position_ledger``
keyed on the VOLATILE in-memory ``position_id``. The confirm path opens a row as
``fill:<quote_id>``; after a restart ``_rehydrate_exposure_book`` re-mints EVERY
held position as ``rehydrate:<ticker>``. ``record_position_settled`` UPDATEs
``WHERE position_id=? AND status='open'``, so post-restart NO settled row could
ever match an open row — every settled write was a silent no-op. Measured live:
the ledger covered 6 rows / $27.35 of a $731.04 book (3.74%), heading to 0% at
the next restart. Blast radius: p_book/p_profit never read ``realized_pnl_cc``
(no pricing/EV effect), but p_night's realized anchor and the settlement
calibration the operator is waiting on both die silently.

THE FIX, both halves:
1. STABLE KEY — the settled write resolves its row by ``position_id`` first,
   else the restart-durable ``(leg_set_hash, combo_ticker, our_side)``.
2. BOOT UPSERT — ``_rehydrate_exposure_book`` writes an OPEN row for every
   re-minted position that has none, closing the historical gap; stable-key
   gated so it never duplicates a row that already exists.
Plus a maintenance-tick DIVERGENCE INVARIANT so this drift class self-reports.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import structlog

from combomaker.core.clock import FakeClock
from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.ops.config import FiltersConfig, PricingConfig, RiskConfig
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.ops.quote_app import QuoteApp, _StoreSettlementLedger
from combomaker.pricing.engine import PricingEngine
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.lifecycle import LifecycleConfig, QuoteLifecycle
from combomaker.risk.balance import BalanceTracker
from combomaker.risk.exposure import (
    ExposureBook,
    LegRef,
    OpenPosition,
    leg_set_hash,
    stable_ledger_key,
)
from combomaker.risk.inplay import InPlayDetector
from combomaker.risk.killswitch import KillSwitch
from combomaker.risk.lastlook import LastLookPolicy
from combomaker.risk.limits import LimitChecker, RiskLimits
from combomaker.risk.settlement import SettlementHandler
from tests.test_filters import Harness
from tests.test_lifecycle import TEST_CONVENTIONS, FakeSender
from tests.test_rehydrate_positions import CONV, IS_ME, _seed_store, _StubRest
from tests.test_settlement import VERIFIED, FakeLifecycle, _settlement_row

CC = CentiCents
Q = CentiContracts

# The exact legs `_seed_store`'s ARG combo carries (see tests/test_rehydrate_
# positions._rfq) — needed to build a position with the SAME durable identity
# the rehydrator will derive.
ARG_LEGS = (
    LegRef("KXWCADVANCE-26JUL15ENGARG-ARG", "KXWCADVANCE-26JUL15ENGARG", "yes"),
    LegRef("KXWCGOAL-26JUL15ENGARG-ARGP-1", "KXWCGOAL-26JUL15ENGARG-ARGP", "yes"),
)


def _position(position_id: str = "fill:q1", *, ticker: str = "KXMVE-C1") -> OpenPosition:
    """LONG NO 1.00 contract @ $0.50 — the 2026-07-10 demo ground truth."""
    return OpenPosition(
        position_id=position_id,
        combo_ticker=ticker,
        collection=None,
        our_side=Side.NO,
        contracts=Q(100),
        entry_price_cc=CC(5_000),
        legs=(LegRef("M1", "E1", "yes"), LegRef("M2", "E2", "no")),
    )


async def _open_rows(store: Store) -> list[tuple[str, str]]:
    async with store._db.execute(  # noqa: SLF001 — no public listing getter
        "SELECT position_id, status FROM position_ledger ORDER BY position_id"
    ) as cur:
        return [(str(r[0]), str(r[1])) for r in await cur.fetchall()]


async def _drain() -> None:
    """Let the ledger adapter's fire-and-forget write task finish."""
    pending = [
        t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()
    ]
    if pending:
        await asyncio.wait(pending, timeout=2.0)


# --- 1. the stable key itself -------------------------------------------------


async def test_settled_write_misses_on_remitted_id_without_stable_key(
    tmp_path: Path,
) -> None:
    """THE DEFECT, pinned: position_id alone can never match after a re-mint."""
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        await store.record_position_open(_position("fill:q1"), subaccount="")
        # Post-restart id — exactly what _rehydrate_exposure_book mints.
        landed = await store.record_position_settled(
            "rehydrate:KXMVE-C1",
            settled_value=0.0,
            realized_pnl_cc=5_000,
            settlement_fee_cc=0,
        )
        assert landed is None  # <- the silent no-op that emptied the ledger
        row = await store.ledger_position("fill:q1")
        assert row is not None and row["status"] == "open"
    finally:
        await store.close()


async def test_settled_write_lands_via_stable_key_after_remint(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        pos = _position("fill:q1")
        await store.record_position_open(pos, subaccount="")
        landed = await store.record_position_settled(
            "rehydrate:KXMVE-C1",
            settled_value=0.0,
            realized_pnl_cc=5_000,
            settlement_fee_cc=0,
            leg_set_hash=leg_set_hash(pos.legs),
            combo_ticker=pos.combo_ticker,
            our_side=pos.our_side.value,
            contracts_centi=int(pos.contracts),
        )
        assert landed == "fill:q1"
        row = await store.ledger_position("fill:q1")
        assert row is not None
        assert row["status"] == "settled"
        assert row["realized_pnl_cc"] == 5_000
        assert row["reconciled_at"] is not None
    finally:
        await store.close()


async def test_stable_key_never_crosses_side_or_combo(tmp_path: Path) -> None:
    """The durable key is (leg_set_hash, combo_ticker, our_side) — a different
    side or a different combo is a DIFFERENT position and must not be settled."""
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        pos = _position("fill:q1")
        await store.record_position_open(pos, subaccount="")
        h = leg_set_hash(pos.legs)
        assert await store.record_position_settled(
            "rehydrate:x", settled_value=0.0, realized_pnl_cc=1,
            settlement_fee_cc=0, leg_set_hash=h,
            combo_ticker=pos.combo_ticker, our_side="yes",  # wrong side
        ) is None
        assert await store.record_position_settled(
            "rehydrate:x", settled_value=0.0, realized_pnl_cc=1,
            settlement_fee_cc=0, leg_set_hash=h,
            combo_ticker="KXMVE-OTHER", our_side="no",  # wrong combo
        ) is None
        assert await store.record_position_settled(
            "rehydrate:x", settled_value=0.0, realized_pnl_cc=1,
            settlement_fee_cc=0, leg_set_hash="deadbeef",  # wrong leg set
            combo_ticker=pos.combo_ticker, our_side="no",
        ) is None
        assert (await store.ledger_position("fill:q1"))["status"] == "open"  # type: ignore[index]
    finally:
        await store.close()


async def test_two_positions_one_combo_consume_one_row_each(tmp_path: Path) -> None:
    """Re-quoted combo ⇒ two open rows under one stable key. Each settlement
    must retire EXACTLY ONE row, or day_realized_pnl_cc double-counts."""
    clock = FakeClock()
    store = await Store.open(tmp_path / "t.sqlite3", clock)
    try:
        a = _position("fill:qA")
        b = replace(_position("fill:qB"), contracts=Q(200))
        await store.record_position_open(a, subaccount="")
        await store.record_position_open(b, subaccount="")
        h = leg_set_hash(a.legs)
        first = await store.record_position_settled(
            "rehydrate:KXMVE-C1", settled_value=0.0, realized_pnl_cc=5_000,
            settlement_fee_cc=0, leg_set_hash=h, combo_ticker="KXMVE-C1",
            our_side="no", contracts_centi=100,
        )
        second = await store.record_position_settled(
            "rehydrate:KXMVE-C1", settled_value=0.0, realized_pnl_cc=10_000,
            settlement_fee_cc=0, leg_set_hash=h, combo_ticker="KXMVE-C1",
            our_side="no", contracts_centi=200,
        )
        # Exact contract-count preference routes each settlement to its own row.
        assert first == "fill:qA"
        assert second == "fill:qB"
        # A third settlement finds nothing left — never re-settles a closed row.
        assert await store.record_position_settled(
            "rehydrate:KXMVE-C1", settled_value=0.0, realized_pnl_cc=999,
            settlement_fee_cc=0, leg_set_hash=h, combo_ticker="KXMVE-C1",
            our_side="no",
        ) is None
        stamp = clock.now()
        lo = stamp.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        hi = stamp.replace(hour=23, minute=59, second=59).isoformat()
        assert await store.day_realized_pnl_cc(lo, hi) == 15_000  # 5_000 + 10_000
    finally:
        await store.close()


# --- 2. the FULL restart round trip through the live handler + adapter --------


async def test_restart_remint_settlement_lands_on_the_pre_restart_row(
    tmp_path: Path,
) -> None:
    """The regression the task asks for, end to end through PRODUCTION code:
    the real SettlementHandler + the real _StoreSettlementLedger adapter, with
    the exposure book holding a RE-MINTED id, must settle the row the confirm
    path opened before the restart."""
    clock = FakeClock()
    store = await Store.open(tmp_path / "t.sqlite3", clock)
    try:
        # --- pre-restart: the confirm path opens the durable row.
        pre = _position("fill:q1")
        await store.record_position_open(pre, subaccount="7")

        # --- RESTART: _rehydrate_exposure_book re-mints the id.
        remint = replace(pre, position_id="rehydrate:KXMVE-C1")
        exposure = ExposureBook(VERIFIED)
        exposure.add_position(remint)
        killswitch = KillSwitch(clock)
        handler = SettlementHandler(
            exposure=exposure,
            balance_tracker=BalanceTracker(VERIFIED, clock, stale_after_s=1e9),
            lifecycle=FakeLifecycle(exposure, killswitch),
            killswitch=killswitch,
            ledger=_StoreSettlementLedger(store),
        )
        # LONG NO 1.00 ct @ $0.50 settles NO ⇒ pays $1.00 ⇒ realized +$0.50.
        results = await handler.handle_settlements(
            [_settlement_row("KXMVE-C1", market_result="no", revenue=100)]
        )
        await _drain()
        assert not killswitch.halted
        assert [r.realized_cc for r in results] == [5_000]

        settled = await store.ledger_position("fill:q1")
        assert settled is not None
        assert settled["status"] == "settled"
        assert settled["realized_pnl_cc"] == 5_000
        assert settled["settled_value"] == 0.0
        # No orphan row was minted under the volatile id.
        assert await store.ledger_position("rehydrate:KXMVE-C1") is None
        assert await _open_rows(store) == [("fill:q1", "settled")]

        # p_night's day-anchored realized anchor now sees it (the whole point).
        stamp = clock.now()
        lo = stamp.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        hi = stamp.replace(hour=23, minute=59, second=59).isoformat()
        assert await store.day_realized_pnl_cc(lo, hi) == 5_000
    finally:
        await store.close()


async def test_unmatched_settlement_is_loud_not_silent(tmp_path: Path) -> None:
    """No open row anywhere ⇒ the write still cannot land, but it now WARNS
    instead of vanishing (the failure mode that hid the defect for weeks)."""
    clock = FakeClock()
    store = await Store.open(tmp_path / "t.sqlite3", clock)
    try:
        exposure = ExposureBook(VERIFIED)
        exposure.add_position(_position("rehydrate:KXMVE-C1"))
        killswitch = KillSwitch(clock)
        handler = SettlementHandler(
            exposure=exposure,
            balance_tracker=BalanceTracker(VERIFIED, clock, stale_after_s=1e9),
            lifecycle=FakeLifecycle(exposure, killswitch),
            killswitch=killswitch,
            ledger=_StoreSettlementLedger(store),
        )
        with structlog.testing.capture_logs() as cap:
            await handler.handle_settlements(
                [_settlement_row("KXMVE-C1", market_result="no", revenue=100)]
            )
            await _drain()
        warned = [e for e in cap if e.get("event") == "position_ledger_settled_unmatched"]
        assert len(warned) == 1
        assert warned[0]["combo_ticker"] == "KXMVE-C1"
        assert not killswitch.halted  # alarm-only: the money path is untouched
    finally:
        await store.close()


# --- 3. the BOOT UPSERT (historical-gap closure) ------------------------------


async def test_rehydrate_backfills_missing_open_rows(tmp_path: Path) -> None:
    store = await _seed_store(tmp_path)
    try:
        rest = _StubRest({"market_positions": [
            {"ticker": "KXMVE-ARG", "position_fp": "-50.00"},
            {"ticker": "KXMVE-ENG", "position_fp": "-40.00"},
        ]})
        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        with structlog.testing.capture_logs() as cap:
            await QuoteApp._rehydrate_exposure_book(
                cast(Any, None), cast(Any, rest), store, exposure
            )
        assert len(exposure.positions) == 2
        assert await _open_rows(store) == [
            ("rehydrate:KXMVE-ARG", "open"),
            ("rehydrate:KXMVE-ENG", "open"),
        ]
        backfill = [e for e in cap if e.get("event") == "rehydrate_ledger_backfilled"]
        assert len(backfill) == 1 and backfill[0]["count"] == 2
    finally:
        await store.close()


async def test_rehydrate_backfill_is_idempotent_across_restarts(
    tmp_path: Path,
) -> None:
    """Restart #2 must NOT mint a second open row — the open-row count has to
    stay equal to the open-position count or the divergence metric lies."""
    store = await _seed_store(tmp_path)
    try:
        payload = {"market_positions": [{"ticker": "KXMVE-ARG", "position_fp": "-50.00"}]}
        for _ in range(3):
            await QuoteApp._rehydrate_exposure_book(
                cast(Any, None),
                cast(Any, _StubRest(payload)),
                store,
                ExposureBook(CONV, is_me_event=IS_ME),
            )
        assert await _open_rows(store) == [("rehydrate:KXMVE-ARG", "open")]
    finally:
        await store.close()


async def test_rehydrate_never_duplicates_an_existing_fill_row(
    tmp_path: Path,
) -> None:
    """A position whose ORIGINAL ``fill:<quote_id>`` row already exists is a
    NO-OP at boot: the stable key already has an open row to settle onto."""
    store = await _seed_store(tmp_path)
    try:
        original = OpenPosition(
            position_id="fill:q-old",
            combo_ticker="KXMVE-ARG",
            collection="KXMVESPORTS",
            our_side=Side.NO,
            contracts=Q(5_000),
            entry_price_cc=CC(7_400),
            legs=ARG_LEGS,
        )
        await store.record_position_open(original, subaccount="")
        payload = {"market_positions": [{"ticker": "KXMVE-ARG", "position_fp": "-50.00"}]}
        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        await QuoteApp._rehydrate_exposure_book(
            cast(Any, None), cast(Any, _StubRest(payload)), store, exposure
        )
        assert await _open_rows(store) == [("fill:q-old", "open")]
        # ...and the re-minted in-memory position still settles onto it.
        remint = next(iter(exposure.positions.values()))
        assert remint.position_id == "rehydrate:KXMVE-ARG"
        landed = await store.record_position_settled(
            remint.position_id,
            settled_value=0.0,
            realized_pnl_cc=1_234,
            settlement_fee_cc=0,
            leg_set_hash=stable_ledger_key(remint),
            combo_ticker=remint.combo_ticker,
            our_side=remint.our_side.value,
            contracts_centi=int(remint.contracts),
        )
        assert landed == "fill:q-old"
    finally:
        await store.close()


async def test_ensure_open_row_is_stable_key_gated_not_id_gated(
    tmp_path: Path,
) -> None:
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        pos = _position("fill:q1")
        assert await store.ensure_open_position_row(pos, subaccount="") is True
        # Same durable identity under a DIFFERENT id ⇒ no second row.
        assert await store.ensure_open_position_row(
            replace(pos, position_id="rehydrate:KXMVE-C1"), subaccount=""
        ) is False
        # A genuinely different combo DOES get its own row.
        other = replace(
            pos, position_id="rehydrate:KXMVE-C2", combo_ticker="KXMVE-C2"
        )
        assert await store.ensure_open_position_row(other, subaccount="") is True
        assert await _open_rows(store) == [
            ("fill:q1", "open"),
            ("rehydrate:KXMVE-C2", "open"),
        ]
    finally:
        await store.close()


# --- 4. the maintenance-tick DIVERGENCE INVARIANT -----------------------------


async def _lifecycle(tmp_path: Path, store: Store, clock: FakeClock) -> QuoteLifecycle:
    h = Harness()
    h.clock = clock
    engine = PricingEngine(h.feed, h.metadata, TEST_CONVENTIONS, PricingConfig())
    rfq_filter = RfqFilter(
        FiltersConfig(min_time_to_close_s=0.0).model_copy(
            update={"allowed_leg_series_prefixes": None}
        ),
        h.feed, h.metadata, h.killswitch, clock,
    )
    return QuoteLifecycle(
        clock=clock,
        sender=FakeSender(),
        engine=engine,
        rfq_filter=rfq_filter,
        limits=LimitChecker(RiskLimits()),
        exposure=ExposureBook(TEST_CONVENTIONS),
        feed=h.feed,
        metadata=h.metadata,
        inplay=InPlayDetector(clock),
        killswitch=h.killswitch,
        conventions=TEST_CONVENTIONS,
        store=store,
        metrics=Metrics(),
        lastlook_policy=LastLookPolicy(),
        config=LifecycleConfig(),
        start_time_provider=rfq_filter.leg_start_time,
    )


async def test_divergence_sweep_reports_a_position_with_no_ledger_row(
    tmp_path: Path,
) -> None:
    clock = FakeClock(datetime(2026, 7, 26, 2, 0, tzinfo=UTC))
    store = await Store.open(tmp_path / "t.sqlite3", clock)
    try:
        lc = await _lifecycle(tmp_path, store, clock)
        lc._exposure.add_position(_position("fill:q1"))  # noqa: SLF001
        with structlog.testing.capture_logs() as cap:
            await lc._sweep_ledger_divergence()  # noqa: SLF001
        div = [e for e in cap if e.get("event") == "position_ledger_divergence"]
        assert len(div) == 1
        assert div[0]["open_positions"] == 1
        assert div[0]["open_ledger_rows"] == 0
        assert div[0]["positions_without_row"] == 1
        assert div[0]["position_ids"] == ["fill:q1"]
    finally:
        await store.close()


async def test_divergence_sweep_clean_once_the_row_exists(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 26, 2, 0, tzinfo=UTC))
    store = await Store.open(tmp_path / "t.sqlite3", clock)
    try:
        lc = await _lifecycle(tmp_path, store, clock)
        pos = _position("fill:q1")
        lc._exposure.add_position(pos)  # noqa: SLF001
        await store.record_position_open(pos, subaccount="")
        with structlog.testing.capture_logs() as cap:
            await lc._sweep_ledger_divergence()  # noqa: SLF001
        clean = [e for e in cap if e.get("event") == "position_ledger_divergence_clean"]
        assert len(clean) == 1
        assert clean[0]["open_positions"] == 1
        assert clean[0]["open_ledger_rows"] == 1
        assert clean[0]["positions_without_row"] == 0

        # A RE-MINTED id is still clean — matching is on the durable key.
        lc._exposure.remove_position("fill:q1")  # noqa: SLF001
        lc._exposure.add_position(replace(pos, position_id="rehydrate:KXMVE-C1"))  # noqa: SLF001
        lc._ledger_divergence_last_mono_ns = None  # noqa: SLF001
        with structlog.testing.capture_logs() as cap2:
            await lc._sweep_ledger_divergence()  # noqa: SLF001
        assert [e["event"] for e in cap2] == ["position_ledger_divergence_clean"]
    finally:
        await store.close()


async def test_divergence_sweep_is_throttled_and_disableable(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 26, 2, 0, tzinfo=UTC))
    store = await Store.open(tmp_path / "t.sqlite3", clock)
    try:
        lc = await _lifecycle(tmp_path, store, clock)
        lc._exposure.add_position(_position("fill:q1"))  # noqa: SLF001
        with structlog.testing.capture_logs() as cap:
            await lc._sweep_ledger_divergence()  # noqa: SLF001
            await lc._sweep_ledger_divergence()  # inside the interval ⇒ skipped
        assert len([e for e in cap if "position_ledger_divergence" in e["event"]]) == 1
        # Past the interval it runs again.
        clock.advance(LifecycleConfig().ledger_divergence_sweep_interval_s + 1.0)
        with structlog.testing.capture_logs() as cap2:
            await lc._sweep_ledger_divergence()  # noqa: SLF001
        assert len([e for e in cap2 if "position_ledger_divergence" in e["event"]]) == 1

        # Non-positive interval ⇒ fully disabled (no store read at all).
        off = await _lifecycle(tmp_path, store, clock)
        off._config = replace(  # noqa: SLF001
            LifecycleConfig(), ledger_divergence_sweep_interval_s=0.0
        )
        off._exposure.add_position(_position("fill:q1"))  # noqa: SLF001
        with structlog.testing.capture_logs() as cap3:
            await off._sweep_ledger_divergence()  # noqa: SLF001
        assert not [e for e in cap3 if "position_ledger_divergence" in e["event"]]
    finally:
        await store.close()


def test_divergence_interval_is_config_validated() -> None:
    """The cadence is a validated config field, not a literal buried in code."""
    assert RiskConfig().ledger_divergence_sweep_interval_s > 0.0
    try:
        RiskConfig(ledger_divergence_sweep_interval_s=0.0)
    except Exception as exc:  # pydantic ValidationError
        assert "ledger_divergence_sweep_interval_s" in str(exc)
    else:  # pragma: no cover - the validator must reject it
        raise AssertionError("non-positive interval must be rejected")


def test_stable_ledger_key_degrades_on_a_legless_position() -> None:
    """A reserved holding adopted with no legs has NO durable identity — it
    must degrade to position_id-only keying, never crash the money path and
    never emit a placeholder hash that collides with a real combo."""
    legless = replace(_position(), legs=())
    assert stable_ledger_key(legless) is None
    assert stable_ledger_key(_position()) == leg_set_hash(_position().legs)
