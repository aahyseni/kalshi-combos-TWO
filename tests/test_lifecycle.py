"""Hot-path lifecycle tests: quote → accept → last look → confirm/lapse →
executed → position, plus TTL/reprice/cancel-all, all against fakes."""

from __future__ import annotations

from fractions import Fraction
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
from combomaker.pricing.fees import FeeModel, FeeSchedule, FeeType
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
        self,
        h: Harness,
        store: Store,
        filters: FiltersConfig | None = None,
        *,
        fee_model: FeeModel | None = None,
        fee_type: FeeType = FeeType.QUADRATIC,
        joint_pool: Any = None,
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
            fee_model=fee_model,
            fee_type=fee_type,
            joint_pool=joint_pool,
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


@pytest.fixture()
async def fee_rig(tmp_path: Path) -> Rig:
    """A rig identical to ``rig`` but wired with a REAL nonzero-fee FeeModel
    (QUADRATIC_WITH_MAKER_FEES so the maker coef bites under our conventions) —
    for the trade-fee-into-realized-P&L test (defense #3)."""
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / "fee.sqlite3", h.clock)
    fee_model = FeeModel(
        FeeSchedule.from_strings(taker="0.07", maker="0.0175"), TEST_CONVENTIONS
    )
    return Rig(h, store, fee_model=fee_model, fee_type=FeeType.QUADRATIC_WITH_MAKER_FEES)


def rfq() -> Rfq:
    return combo(CROSS_EVENT_LEGS)


def accepted_msg(quote_id: str, side: str = "yes") -> JsonDict:
    # Real Kalshi quote_accepted WS shape (docs.kalshi.com/websockets/
    # communications): a CONTRACTS-mode accept carries the count in
    # contracts_accepted_fp. A TARGET-COST accept has contracts_accepted_fp=null
    # and carries yes/no_contracts_offered_fp instead — see
    # test_ground_truth_accept_fields_size_target_cost_rfq for that path.
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


async def test_nonzero_trade_fee_enters_realized_pnl_at_fill(fee_rig: Rig) -> None:
    # The trade fee charged AT FILL is a real cash cost — it must enter the
    # realized ledger the ENFORCED daily-loss cap reads, not only the settlement
    # fee. $0 today for our quadratic maker fills; this proves a nonzero-fee
    # series is booked (defense #3). Uses the REAL FeeModel (CLAUDE.md rule 8),
    # QUADRATIC_WITH_MAKER_FEES so the maker coef bites under our conventions.
    await fee_rig.lifecycle.handle_rfq(rfq())
    await fee_rig.lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    await fee_rig.lifecycle.on_quote_executed({"quote_id": "q1"})
    position = next(iter(fee_rig.exposure.positions.values()))

    fee_model = FeeModel(
        FeeSchedule.from_strings(taker="0.07", maker="0.0175"), TEST_CONVENTIONS
    )
    expected_fee_cc = int(
        fee_model.trade_fee_cc(
            price_cc=position.entry_price_cc,
            qty=position.contracts,
            fee_type=FeeType.QUADRATIC_WITH_MAKER_FEES,
            multiplier=Fraction(1),
        )
    )
    assert expected_fee_cc > 0  # the series genuinely charges a fee
    # The fee reduces realized P&L by exactly the trade fee (negative delta).
    assert fee_rig.lifecycle._realized_pnl_cc == -expected_fee_cc  # noqa: SLF001


async def test_zero_fee_fill_leaves_realized_pnl_untouched(rig: Rig) -> None:
    # Our default quadratic maker fill charges $0 (and the default rig has no fee
    # model), so realized P&L stays 0 at fill — no behaviour change (defense #2:
    # an UNKNOWN/None fee is never booked as a convenient 0).
    await rig.lifecycle.handle_rfq(rfq())
    await rig.lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    await rig.lifecycle.on_quote_executed({"quote_id": "q1"})
    assert rig.lifecycle._realized_pnl_cc == 0  # noqa: SLF001


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


async def test_full_reconcile_fractional_contract_scalar_does_not_halt(rig: Rig) -> None:
    # A target-cost RFQ leaves a FRACTIONAL contract count (0.90 ct = 90 centi-
    # contracts). On a SCALAR settlement (V=0.43, a leg DNP/rain/void), our
    # predicted NO credit = 90 ct × (100−43)¢ // 100 = 5130 cc = 51.3¢, which the
    # exchange's integer-cent revenue (51¢ or 52¢) can NEVER equal. A strict `!=`
    # would spuriously HALT a legitimate settlement; reconciling to the cent must
    # NOT halt when the residual is sub-cent.
    rig.exposure.add_position(held_no_position(contracts=90))
    predicted = 90 * (10_000 - round(0.43 * 10_000)) // 100  # 5130 cc
    assert predicted == 5_130
    # A legitimate settlement never halts, so both candidate exchange bookings
    # (floor 51¢ or round 52¢ of the true 51.3¢) reconcile without any reset.
    for exchange_revenue_cc in (5_100, 5_200):  # exchange floors OR rounds 51.3¢
        await rig.lifecycle.reconcile_combo_settlement(
            "KXMVE-C1",
            settled_yes=False,
            settled_value=0.43,
            expected_revenue_cc=exchange_revenue_cc,
        )
        assert not rig.killswitch.halted


async def test_full_reconcile_fractional_scalar_still_halts_on_real_mismatch(rig: Rig) -> None:
    # The sub-cent tolerance does NOT weaken defense #3: a genuine model error on
    # the same fractional-contract scalar (revenue a full cent+ off predicted)
    # STILL halts. Predicted 5130 cc; exchange 4900 cc (49¢) is 230 cc ≥ 1¢ away.
    rig.exposure.add_position(held_no_position(contracts=90))
    await rig.lifecycle.reconcile_combo_settlement(
        "KXMVE-C1", settled_yes=False, settled_value=0.43, expected_revenue_cc=4_900
    )
    assert rig.killswitch.halted
    assert rig.killswitch.halt_event is not None
    assert rig.killswitch.halt_event.reason is ReasonCode.HALT_RECONCILIATION_MISMATCH


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


# --------------------------------------------------------------------------
# P2-4: per-quote/confirm risk-audit log line (RISK_ENGINE_AUDIT_ACTION_PLAN
# P2 item 4). One consolidated ``risk_audit`` structured line carries every
# enumerated field: book/snapshot generation + age, candidate EV, ES/P(ruin),
# deterministic loss, gross, direction, reservations, model split/residual,
# fallback reason, and the binding cap.
# --------------------------------------------------------------------------

import structlog  # noqa: E402

# The exact fields the audit spec enumerates — every one must be present on the
# line so the operator never has to reconstruct a decision from scattered logs.
_AUDIT_FIELDS = {
    "snapshot_generation",
    "live_generation",
    "snapshot_age_s",
    "candidate_ev_cc",
    "es_99_cc",
    "p_ruin",
    "p_ruin_upper",
    "deterministic_max_loss_cc",
    "gross_cc",
    "direction_cc",
    "reservations",
    "production_es_99_cc",
    "challenger_es_99_cc",
    "bridge_es_99_cc",
    "bridge_active",
    "es_residual_cc",
    "fallback_reason",
    "binding_cap",
}


def _audit_lines(cap: list[dict[str, Any]], phase: str) -> list[dict[str, Any]]:
    return [
        e for e in cap if e.get("event") == "risk_audit" and e.get("phase") == phase
    ]


async def test_quote_emits_risk_audit_line_with_all_fields(rig: Rig) -> None:
    with structlog.testing.capture_logs() as cap:
        await rig.lifecycle.handle_rfq(rfq())
    lines = _audit_lines(cap, "quote")
    assert len(lines) == 1
    line = lines[0]
    # Every enumerated field is present.
    assert _AUDIT_FIELDS <= set(line.keys())
    # A cleanly-sent quote has no binding cap / fallback and a real candidate EV.
    assert line["binding_cap"] == ""
    assert line["fallback_reason"] == ""
    assert line["reason"] == str(ReasonCode.QUOTE_SENT)
    assert isinstance(line["candidate_ev_cc"], int)
    # No COMMITTED positions ⇒ no committed tail to gate on (fields honestly
    # None, not 0). The gross axis DOES reflect the just-sent open quote's
    # mass-acceptance premium (it is upserted into the book before the audit),
    # so gross_cc is a positive integer, not zero.
    assert line["es_99_cc"] is None
    assert line["p_ruin"] is None
    assert isinstance(line["gross_cc"], int) and line["gross_cc"] > 0
    assert line["reservations"] == 0


async def test_confirm_emits_risk_audit_line(rig: Rig) -> None:
    await rig.lifecycle.handle_rfq(rfq())
    with structlog.testing.capture_logs() as cap:
        await rig.lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    lines = _audit_lines(cap, "confirm")
    assert len(lines) == 1
    line = lines[0]
    assert _AUDIT_FIELDS <= set(line.keys())
    # The confirmed fill carries a sized candidate EV from side/bid/qty.
    assert isinstance(line["candidate_ev_cc"], int)
    assert line["binding_cap"] == ""
    assert line["quote_id"] == "q1"


async def test_risk_declined_quote_audit_reports_binding_cap(rig: Rig) -> None:
    # A too-big quote is risk-declined; the audit line names the binding cap.
    big = combo(CROSS_EVENT_LEGS, contracts_fp="9999.00")
    with structlog.testing.capture_logs() as cap:
        await rig.lifecycle.handle_rfq(big)
    lines = _audit_lines(cap, "quote")
    assert len(lines) == 1
    line = lines[0]
    assert line["binding_cap"] != ""  # a cap bound this quote
    assert line["reason"] == line["binding_cap"]


async def test_decline_audit_reports_binding_cap(rig: Rig) -> None:
    # Kill switch between accept and last look declines the confirm; the audit
    # line's binding cap is the decline reason.
    await rig.lifecycle.handle_rfq(rfq())
    await rig.killswitch.halt(ReasonCode.HALT_MANUAL)
    with structlog.testing.capture_logs() as cap:
        await rig.lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    lines = _audit_lines(cap, "decline")
    assert len(lines) == 1
    assert lines[0]["binding_cap"] == str(ReasonCode.DECLINE_KILL_SWITCH)


async def test_risk_audit_fallback_reason_on_unmeasured_book(rig: Rig) -> None:
    # A NON-EMPTY book with NO book-risk snapshot must fail closed: the audit
    # reports the fail-closed FALLBACK reason and honestly None tail numbers
    # (an unmeasured joint tail is never a convenient value — hard rule 6).
    rig.exposure.add_position(
        OpenPosition(
            position_id="p1",
            combo_ticker="KXMVE-C1",
            collection="KXMVE-C1",
            our_side=Side.YES,
            contracts=CentiContracts(1_000),
            entry_price_cc=CentiCents(5_000),
            legs=(LegRef("M1", "E1", "yes"), LegRef("M2", "E2", "yes")),
        )
    )
    fields = rig.lifecycle._risk_audit_fields(  # noqa: SLF001
        candidate_ev_cc=None, binding_cap="", fallback_reason=""
    )
    assert fields["fallback_reason"] == "book_risk_never_measured"
    assert fields["es_99_cc"] is None
    assert fields["deterministic_max_loss_cc"] is None
    assert fields["p_ruin"] is None
