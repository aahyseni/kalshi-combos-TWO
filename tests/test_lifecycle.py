"""Hot-path lifecycle tests: quote → accept → last look → confirm/lapse →
executed → position, plus TTL/reprice/cancel-all, all against fakes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.ops.config import FiltersConfig, PricingConfig
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.pricing.engine import PricingEngine
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.lifecycle import LifecycleConfig, QuoteLifecycle
from combomaker.rfq.models import Rfq
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.inplay import InPlayDetector
from combomaker.risk.lastlook import LastLookPolicy
from combomaker.risk.limits import LimitChecker, RiskLimits
from tests.test_filters import Harness
from tests.test_pricing_engine import (
    CROSS_EVENT_LEGS,
    SAME_MARKET_BOTH_SIDES,
    combo,
    seed_event,
)

JsonDict = dict[str, Any]

TEST_CONVENTIONS = Conventions(
    verified=True,
    source="test",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)


class FakeSender:
    def __init__(self) -> None:
        self.created: list[JsonDict] = []
        self.deleted: list[str] = []
        self.confirmed: list[str] = []
        self.fail_confirm = False
        self._next_id = 0

    async def create_quote(
        self,
        rfq_id: str,
        *,
        yes_bid_cc: CentiCents,
        no_bid_cc: CentiCents,
        rest_remainder: bool = False,
    ) -> JsonDict:
        self._next_id += 1
        quote_id = f"q{self._next_id}"
        self.created.append(
            {"rfq_id": rfq_id, "id": quote_id, "yes": int(yes_bid_cc), "no": int(no_bid_cc)}
        )
        return {"id": quote_id}

    async def delete_quote(self, quote_id: str) -> JsonDict:
        self.deleted.append(quote_id)
        return {}

    async def confirm_quote(self, quote_id: str) -> JsonDict:
        if self.fail_confirm:
            raise RuntimeError("confirm boom")
        self.confirmed.append(quote_id)
        return {}


class Rig:
    def __init__(
        self, h: Harness, store: Store, filters: FiltersConfig | None = None
    ) -> None:
        self.h = h
        self.sender = FakeSender()
        self.killswitch = h.killswitch
        self.exposure = ExposureBook(TEST_CONVENTIONS)
        self.metrics = Metrics()
        engine = PricingEngine(h.feed, h.metadata, TEST_CONVENTIONS, PricingConfig())
        self.lifecycle = QuoteLifecycle(
            clock=h.clock,
            sender=self.sender,
            engine=engine,
            rfq_filter=RfqFilter(
                # synthetic legs; series gate off (it has dedicated tests in test_filters)
                (filters or FiltersConfig(min_time_to_close_s=0.0)).model_copy(
                    update={"allowed_leg_series_prefixes": None}),
                h.feed, h.metadata, h.killswitch, h.clock,
            ),
            limits=LimitChecker(RiskLimits()),
            exposure=self.exposure,
            feed=h.feed,
            metadata=h.metadata,
            inplay=InPlayDetector(h.clock),
            killswitch=h.killswitch,
            conventions=TEST_CONVENTIONS,
            store=store,
            metrics=self.metrics,
            lastlook_policy=LastLookPolicy(),
            config=LifecycleConfig(quote_ttl_s=30.0, reprice_threshold_cc=100),
        )


@pytest.fixture()
async def rig(tmp_path: Path) -> Rig:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / "t.sqlite3", h.clock)
    return Rig(h, store)


def rfq() -> Rfq:
    return combo(CROSS_EVENT_LEGS)


def accepted_msg(quote_id: str, side: str = "yes") -> JsonDict:
    return {
        "quote_id": quote_id,
        "rfq_id": "rfq_1",
        "accepted_side": side,
        "contracts_accepted_fp": "10.00",
    }


async def test_quotable_rfq_sends_quote_and_tracks_it(rig: Rig) -> None:
    await rig.lifecycle.handle_rfq(rfq())
    assert len(rig.sender.created) == 1
    assert rig.lifecycle.open_quote_count == 1
    assert rig.sender.created[0]["yes"] > 0
    assert "q1" in rig.exposure.open_quotes


async def test_filtered_rfq_never_reaches_sender(rig: Rig) -> None:
    await rig.killswitch.halt(ReasonCode.HALT_MANUAL)
    await rig.lifecycle.handle_rfq(rfq())
    assert rig.sender.created == []


async def test_risk_breach_blocks_quote(rig: Rig) -> None:
    big = combo(CROSS_EVENT_LEGS, contracts_fp="9999.00")
    await rig.lifecycle.handle_rfq(big)
    assert rig.sender.created == []


async def test_accept_confirm_happy_path(rig: Rig) -> None:
    await rig.lifecycle.handle_rfq(rfq())
    await rig.lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    assert rig.sender.confirmed == ["q1"]
    assert rig.lifecycle.open_quote_count == 0  # accepted ⇒ no longer open
    # execution creates the position with conventions-mapped side
    await rig.lifecycle.on_quote_executed({"quote_id": "q1", "order_id": "o1"})
    assert len(rig.exposure.positions) == 1
    position = next(iter(rig.exposure.positions.values()))
    assert position.our_side is Side.YES
    assert int(position.contracts) == 1_000
    assert rig.metrics.counter("fill.count") == 1


async def test_accept_no_side_maps_via_conventions(rig: Rig) -> None:
    await rig.lifecycle.handle_rfq(rfq())
    await rig.lifecycle.on_quote_accepted(accepted_msg("q1", "no"))
    await rig.lifecycle.on_quote_executed({"quote_id": "q1"})
    position = next(iter(rig.exposure.positions.values()))
    assert position.our_side is Side.NO


async def test_killswitch_between_accept_declines_without_confirm(rig: Rig) -> None:
    await rig.lifecycle.handle_rfq(rfq())
    await rig.killswitch.halt(ReasonCode.HALT_MANUAL)
    await rig.lifecycle.on_quote_accepted(accepted_msg("q1"))
    assert rig.sender.confirmed == []  # deliberate lapse, no confirm call
    assert rig.metrics.counter(f"confirm.declined.{ReasonCode.DECLINE_KILL_SWITCH}") == 1


async def test_leg_move_beyond_tolerance_declines(rig: Rig) -> None:
    await rig.lifecycle.handle_rfq(rfq())
    # crash M1 mid far beyond the 150cc tolerance
    await rig.h.ws.deliver(
        {
            "type": "orderbook_delta",
            "sid": 5,
            "seq": 3,
            "msg": {
                "market_ticker": "M1",
                "price_dollars": "0.9000",
                "delta_fp": "500.00",
                "side": "yes",
                "ts_ms": 1,
            },
        }
    )
    await rig.lifecycle.on_quote_accepted(accepted_msg("q1"))
    assert rig.sender.confirmed == []


async def test_unreadable_accepted_side_lapses(rig: Rig) -> None:
    await rig.lifecycle.handle_rfq(rfq())
    await rig.lifecycle.on_quote_accepted(
        {"quote_id": "q1", "accepted_side": "sideways"}
    )
    assert rig.sender.confirmed == []


async def test_rfq_deleted_deletes_quote(rig: Rig) -> None:
    await rig.lifecycle.handle_rfq(rfq())
    await rig.lifecycle.on_rfq_deleted("rfq_1", {})
    assert rig.sender.deleted == ["q1"]
    assert rig.lifecycle.open_quote_count == 0


async def test_ttl_expiry_deletes(rig: Rig) -> None:
    await rig.lifecycle.handle_rfq(rfq())
    rig.h.clock.advance(31.0)
    await rig.lifecycle.maintenance_tick()
    assert rig.sender.deleted == ["q1"]


async def delta(rig: Rig, seq: int, side: str, price: str, change: str) -> None:
    await rig.h.ws.deliver(
        {
            "type": "orderbook_delta",
            "sid": 5,
            "seq": seq,
            "msg": {
                "market_ticker": "M1",
                "price_dollars": price,
                "delta_fp": change,
                "side": side,
                "ts_ms": 1,
            },
        }
    )


async def test_reprice_on_fair_move_replaces_quote(rig: Rig) -> None:
    await rig.lifecycle.handle_rfq(rfq())
    # move M1's mid up ~5c WITHOUT crossing: no bid 0.51→0.43, yes bid →0.50
    await delta(rig, 3, "no", "0.5100", "-25.00")
    await delta(rig, 4, "no", "0.4300", "25.00")
    await delta(rig, 5, "yes", "0.5000", "30.00")
    await rig.lifecycle.maintenance_tick()
    assert len(rig.sender.created) == 2  # replacement sent
    assert rig.lifecycle.open_quote_count == 1  # old state dropped


async def test_reprice_refusal_deletes_stale_quote(rig: Rig) -> None:
    await rig.lifecycle.handle_rfq(rfq())
    # fair moves AND the filter now refuses (spread blown out): stale quote
    # must be deleted, not left on the wire
    await delta(rig, 3, "no", "0.5100", "-25.00")  # best no falls to 0.40 → wide
    await delta(rig, 4, "yes", "0.4700", "-20.00")  # best yes falls to 0.30
    await rig.lifecycle.maintenance_tick()
    assert rig.lifecycle.open_quote_count == 0
    assert rig.sender.deleted == ["q1"]


async def test_cancel_all_is_idempotent(rig: Rig) -> None:
    await rig.lifecycle.handle_rfq(rfq())
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id="rfq_2"))
    assert rig.lifecycle.open_quote_count == 2
    await rig.lifecycle.cancel_all("test")
    assert sorted(rig.sender.deleted) == ["q1", "q2"]
    assert rig.lifecycle.open_quote_count == 0
    await rig.lifecycle.cancel_all("test")  # no-op
    assert len(rig.sender.deleted) == 2


async def test_confirm_failure_is_counted_not_raised(rig: Rig) -> None:
    await rig.lifecycle.handle_rfq(rfq())
    rig.sender.fail_confirm = True
    await rig.lifecycle.on_quote_accepted(accepted_msg("q1"))
    assert rig.metrics.counter("confirm.failed") == 1


# --- Impossible-combo farming: hot-path behavior + settlement guard --------------


def farmed_position(combo_ticker: str = "KXMVE-C1", *, farmed: bool = True) -> OpenPosition:
    return OpenPosition(
        position_id=f"fill:{combo_ticker}",
        combo_ticker=combo_ticker,
        collection="KXMVESPORTS",
        our_side=Side.NO,
        contracts=CentiContracts(500),
        entry_price_cc=CentiCents(9_000),
        legs=(LegRef("M1", "E1", "yes"), LegRef("M1", "E1", "no")),
        farmed=farmed,
    )


async def test_farm_yes_side_accept_never_confirms(rig: Rig) -> None:
    """End-to-end #1 invariant: a farmed combo quotes YES at 0, and even if an
    accept lands on that declined side we NEVER confirm — we can never end up
    long the worthless YES."""
    await rig.lifecycle.handle_rfq(combo(SAME_MARKET_BOTH_SIDES))
    assert len(rig.sender.created) == 1
    assert rig.sender.created[0]["yes"] == 0     # farm: YES side declined
    assert rig.sender.created[0]["no"] > 0
    await rig.lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    assert rig.sender.confirmed == []            # never confirmed the worthless YES
    assert len(rig.exposure.positions) == 0
    assert rig.metrics.counter(
        f"confirm.declined.{ReasonCode.DECLINE_SIDE_NOT_QUOTED}"
    ) == 1


async def test_farm_no_side_accept_books_farmed_position(rig: Rig) -> None:
    await rig.lifecycle.handle_rfq(combo(SAME_MARKET_BOTH_SIDES))
    await rig.lifecycle.on_quote_accepted(accepted_msg("q1", "no"))
    await rig.lifecycle.on_quote_executed({"quote_id": "q1"})
    position = next(iter(rig.exposure.positions.values()))
    assert position.farmed is True               # farmed flag threaded to the book
    assert position.our_side is Side.NO          # long the certain-NO side


async def test_farmed_combo_settling_yes_halts_reconciliation(rig: Rig) -> None:
    rig.exposure.add_position(farmed_position())
    await rig.lifecycle.reconcile_combo_settlement("KXMVE-C1", settled_yes=True)
    assert rig.killswitch.halted
    assert rig.killswitch.halt_event is not None
    assert rig.killswitch.halt_event.reason is ReasonCode.HALT_RECONCILIATION_MISMATCH


async def test_farmed_combo_settling_no_does_not_halt(rig: Rig) -> None:
    rig.exposure.add_position(farmed_position())
    await rig.lifecycle.reconcile_combo_settlement("KXMVE-C1", settled_yes=False)
    assert not rig.killswitch.halted


async def test_non_farmed_combo_settling_yes_does_not_halt(rig: Rig) -> None:
    rig.exposure.add_position(farmed_position(farmed=False))
    await rig.lifecycle.reconcile_combo_settlement("KXMVE-C1", settled_yes=True)
    assert not rig.killswitch.halted


# --- extended full-path reconcile (to-the-cent revenue check) --------------------


def held_no_position(
    combo_ticker: str = "KXMVE-C1", *, contracts: int = 100, entry_price: int = 5_000
) -> OpenPosition:
    return OpenPosition(
        position_id=f"fill:{combo_ticker}",
        combo_ticker=combo_ticker,
        collection="KXMVESPORTS",
        our_side=Side.NO,
        contracts=CentiContracts(contracts),
        entry_price_cc=CentiCents(entry_price),
        legs=(LegRef("M1", "E1", "no"),),
    )


async def test_full_reconcile_matching_revenue_does_not_halt(rig: Rig) -> None:
    # LONG NO 1 ct, combo settles NO (V=0) → predicted credit = 1 ct × $1 = 100¢.
    rig.exposure.add_position(held_no_position())
    await rig.lifecycle.reconcile_combo_settlement(
        "KXMVE-C1", settled_yes=False, settled_value=0.0, expected_revenue_cc=10_000
    )
    assert not rig.killswitch.halted


async def test_full_reconcile_cent_mismatch_halts(rig: Rig) -> None:
    # Exchange booked 99¢ but we predicted 100¢ → to-the-cent mismatch HALTs.
    rig.exposure.add_position(held_no_position())
    await rig.lifecycle.reconcile_combo_settlement(
        "KXMVE-C1", settled_yes=False, settled_value=0.0, expected_revenue_cc=9_900
    )
    assert rig.killswitch.halted
    assert rig.killswitch.halt_event is not None
    assert rig.killswitch.halt_event.reason is ReasonCode.HALT_RECONCILIATION_MISMATCH


async def test_full_reconcile_scalar_matches(rig: Rig) -> None:
    # V=0.7 → NO pays $0.30/ct → predicted credit 30¢ for 1 ct; matches revenue.
    rig.exposure.add_position(held_no_position())
    await rig.lifecycle.reconcile_combo_settlement(
        "KXMVE-C1", settled_yes=False, settled_value=0.7, expected_revenue_cc=3_000
    )
    assert not rig.killswitch.halted


async def test_full_reconcile_sums_multiple_positions_on_ticker(rig: Rig) -> None:
    # Two LONG-NO positions on the same combo, 1 ct + 2 ct, settle NO (V=0):
    # predicted credit = (1 + 2) ct × $1 = 300¢. Matches → no halt.
    rig.exposure.add_position(held_no_position(contracts=100))
    rig.exposure.add_position(
        OpenPosition(
            position_id="fill:KXMVE-C1-b",
            combo_ticker="KXMVE-C1",
            collection="KXMVESPORTS",
            our_side=Side.NO,
            contracts=CentiContracts(200),
            entry_price_cc=CentiCents(4_000),
            legs=(LegRef("M1", "E1", "no"),),
        )
    )
    await rig.lifecycle.reconcile_combo_settlement(
        "KXMVE-C1", settled_yes=False, settled_value=0.0, expected_revenue_cc=30_000
    )
    assert not rig.killswitch.halted


async def test_farmed_yes_tripwire_precedes_revenue_check(rig: Rig) -> None:
    # A farmed combo settling YES halts on the tripwire even when revenue figures
    # are supplied (the tripwire is the first, hardest guard).
    rig.exposure.add_position(farmed_position())
    await rig.lifecycle.reconcile_combo_settlement(
        "KXMVE-C1", settled_yes=True, settled_value=1.0, expected_revenue_cc=0
    )
    assert rig.killswitch.halted
    assert rig.killswitch.halt_event is not None
    assert rig.killswitch.halt_event.reason is ReasonCode.HALT_RECONCILIATION_MISMATCH


# --- real fill fee booked at execution -------------------------------------------


async def test_fill_fee_none_without_fee_model(rig: Rig) -> None:
    # The default rig wires NO fee model → fee_cc is None (pre-Phase-6 behaviour),
    # never a guessed 0.
    assert rig.lifecycle._fill_fee_cc(CentiCents(5_000), CentiContracts(100)) is None


async def test_fill_fee_zero_for_combo_maker_quadratic() -> None:
    # A wired quadratic fee model + maker attribution charges $0 on our fill.
    from combomaker.pricing.fees import FeeModel, FeeSchedule, FeeType

    model = FeeModel(FeeSchedule.from_strings("0.07", "0.0175"), TEST_CONVENTIONS)
    from tests.test_filters import Harness

    h = Harness()
    await h.with_books(["M1", "M2"])
    engine = PricingEngine(h.feed, h.metadata, TEST_CONVENTIONS, PricingConfig())
    from combomaker.ops.metrics import Metrics as _Metrics
    from combomaker.ops.persistence import Store as _Store

    store = await _Store.open(Path(":memory:"), h.clock)
    lc = QuoteLifecycle(
        clock=h.clock,
        sender=FakeSender(),
        engine=engine,
        rfq_filter=RfqFilter(
            FiltersConfig(min_time_to_close_s=0.0).model_copy(
                update={"allowed_leg_series_prefixes": None}
            ),
            h.feed, h.metadata, h.killswitch, h.clock,
        ),
        limits=LimitChecker(RiskLimits()),
        exposure=ExposureBook(TEST_CONVENTIONS),
        feed=h.feed,
        metadata=h.metadata,
        inplay=InPlayDetector(h.clock),
        killswitch=h.killswitch,
        conventions=TEST_CONVENTIONS,
        store=store,
        metrics=_Metrics(),
        lastlook_policy=LastLookPolicy(),
        config=LifecycleConfig(),
        fee_model=model,
        fee_type=FeeType.QUADRATIC,
    )
    assert lc._fill_fee_cc(CentiCents(5_000), CentiContracts(100)) == 0
    await store.close()
