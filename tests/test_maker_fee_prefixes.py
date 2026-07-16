"""MAKER-FEE SECTION (operator directive 2026-07-16 — Kalshi is adding maker
fees for combos; doctrine = EAT THE FEE).

Quoted prices stay unchanged/competitive; the fee is ACCOUNTED everywhere:
``FeeConfig.maker_fee_active_prefixes`` marks the series/collection prefixes
whose maker fills pay the maker fee, and a matching fill books the REAL
FeeModel fee (rule 8 — never reimplemented) into the fills ledger, realized
P&L, the recorded expected edge, and the Problem-A waiver candidate's
per-state P&L. Covered here:

- empty prefixes (the committed default) ⇒ BIT-IDENTICAL: $0 fee row, raw
  expected edge, untouched realized P&L, fee_cc=0 waiver candidate;
- active prefix (market-ticker match AND collection-ticker match) ⇒ the fee
  appears in the fill row, realized P&L, the recorded edge, and the waiver
  candidate entity — all to the cent vs FeeModel ground truth;
- non-matching prefix ⇒ the empty-list behaviour;
- quoted prices are NOT widened by the flag (eat-the-fee);
- a replayed execution books the fee ONCE (idempotency interplay with the
  2026-07-16 fill-record recovery work);
- FeeConfig default + YAML-shape coercion.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Any

from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.ops.config import FeeConfig, FiltersConfig, PricingConfig
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.fees import FeeModel, FeeSchedule, FeeType
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.lifecycle import LifecycleConfig, QuoteLifecycle
from combomaker.risk.exposure import ExposureBook
from combomaker.risk.inplay import InPlayDetector
from combomaker.risk.lastlook import LastLookPolicy
from combomaker.risk.limits import LimitChecker, RiskLimits
from combomaker.sim.structural_book import StructuralConfigView
from tests.test_filters import Harness
from tests.test_lifecycle import TEST_CONVENTIONS, FakeSender, accepted_msg, rfq
from tests.test_pricing_engine import seed_event

JsonDict = dict[str, Any]

# The shipped fee model (coefficients VERIFIED vs the official schedule PDF).
FEE_MODEL = FeeModel(FeeSchedule.from_strings("0.07", "0.0175"), TEST_CONVENTIONS)


def maker_fee_ground_truth_cc(bid_cc: int, qty_centi: int) -> int:
    """FeeModel ground truth for a maker-fee-active fill: our conventions are
    maker-attributed (maker_is_taker_on_fill=False), so the active list makes
    the fill QUADRATIC_WITH_MAKER_FEES ⇒ the 0.0175 maker coefficient."""
    return int(
        FEE_MODEL.trade_fee_cc(
            price_cc=CentiCents(bid_cc),
            qty=CentiContracts(qty_centi),
            fee_type=FeeType.QUADRATIC_WITH_MAKER_FEES,
            multiplier=Fraction(1),
        )
    )


class FeeRig:
    def __init__(
        self, h: Harness, store: Store, *, prefixes: tuple[str, ...]
    ) -> None:
        self.h = h
        self.store = store
        self.sender = FakeSender()
        self.exposure = ExposureBook(TEST_CONVENTIONS)
        self.metrics = Metrics()
        engine = PricingEngine(h.feed, h.metadata, TEST_CONVENTIONS, PricingConfig())
        self.lifecycle = QuoteLifecycle(
            clock=h.clock,
            sender=self.sender,
            engine=engine,
            rfq_filter=RfqFilter(
                FiltersConfig(min_time_to_close_s=0.0).model_copy(
                    update={"allowed_leg_series_prefixes": None}
                ),
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
            config=LifecycleConfig(),
            # The SHIPPED fee posture: real model, QUADRATIC default (combos
            # charge $0 maker TODAY) — the prefix list is the only variable.
            fee_model=FEE_MODEL,
            fee_type=FeeType.QUADRATIC,
            maker_fee_active_prefixes=prefixes,
        )


async def _make_rig(
    tmp_path: Path, *, prefixes: tuple[str, ...], db: str
) -> FeeRig:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / db, h.clock)
    return FeeRig(h, store, prefixes=prefixes)


async def _fill(rig: FeeRig) -> tuple[int, int]:
    """Quote → accept YES → execute. Returns (bid_cc, qty_centi) of the fill."""
    await rig.lifecycle.handle_rfq(rfq())
    bid_cc = int(rig.sender.created[0]["yes"])
    await rig.lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    await rig.lifecycle.on_quote_executed({"quote_id": "q1", "order_id": "o1"})
    return bid_cc, 1_000  # accepted_msg fills 10.00 contracts


async def _fill_row(store: Store) -> tuple[Any, ...]:
    async with store._db.execute(  # noqa: SLF001 - white-box ledger read
        "SELECT fee_cc, expected_edge_cc FROM fills"
    ) as cursor:
        rows = [tuple(r) async for r in cursor]
    assert len(rows) == 1
    return rows[0]


def _raw_edge_cc(rig: FeeRig, bid_cc: int, qty_centi: int) -> int:
    fair_cc = int(rig.lifecycle._executed_states["q1"].constructed.fair_cc)  # noqa: SLF001
    return (fair_cc - bid_cc) * qty_centi // 100


# --------------------------------------------------------------------------- #
# Empty prefix list (committed default) ⇒ bit-identical.                       #
# --------------------------------------------------------------------------- #


async def test_empty_prefixes_bit_identical(tmp_path: Path) -> None:
    rig = await _make_rig(tmp_path, prefixes=(), db="empty.sqlite3")
    bid_cc, qty = await _fill(rig)
    fee_cc, edge_cc = await _fill_row(rig.store)
    assert fee_cc == 0  # quadratic combo maker fill: $0 today
    assert edge_cc == _raw_edge_cc(rig, bid_cc, qty)  # edge NOT fee-adjusted
    assert rig.lifecycle._realized_pnl_cc == 0  # noqa: SLF001


async def test_empty_prefixes_waiver_candidate_fee_zero(tmp_path: Path) -> None:
    rig = await _make_rig(tmp_path, prefixes=(), db="empty_wc.sqlite3")
    await _fill(rig)
    state = rig.lifecycle._executed_states["q1"]  # noqa: SLF001
    inputs = rig.lifecycle._build_state_worst_case_inputs(  # noqa: SLF001
        "q1", state, StructuralConfigView()
    )
    candidate = inputs.entities[-1]  # THE CANDIDATE is always last
    assert candidate.entity_id == "fill:q1"
    assert candidate.fee_cc == 0  # bit-identical waiver inputs


# --------------------------------------------------------------------------- #
# Active prefix ⇒ the fee lands everywhere, to the cent vs FeeModel.           #
# --------------------------------------------------------------------------- #


async def test_active_prefix_books_fee_everywhere(tmp_path: Path) -> None:
    # "KXMVE" prefixes the combo MARKET ticker (KXMVE-C1).
    rig = await _make_rig(tmp_path, prefixes=("KXMVE",), db="active.sqlite3")
    bid_cc, qty = await _fill(rig)
    expected_fee = maker_fee_ground_truth_cc(bid_cc, qty)
    assert expected_fee > 0  # the maker coefficient genuinely bites

    fee_cc, edge_cc = await _fill_row(rig.store)
    assert fee_cc == expected_fee                                   # fills row
    assert rig.lifecycle._realized_pnl_cc == -expected_fee  # noqa: SLF001  # realized P&L
    assert edge_cc == _raw_edge_cc(rig, bid_cc, qty) - expected_fee  # net edge
    # The EV ledger graded on aggregate expected edge carries the SAME net edge.
    async with rig.store._db.execute(  # noqa: SLF001
        "SELECT expected_edge_cc FROM ev_ledger"
    ) as cursor:
        (ev_row,) = [tuple(r) async for r in cursor]
    assert ev_row[0] == edge_cc


async def test_collection_prefix_match_also_activates(tmp_path: Path) -> None:
    # "KXMVESPORTS" does NOT prefix the market ticker (KXMVE-C1) but IS the
    # collection ticker — the collection branch must activate the fee too.
    rig = await _make_rig(tmp_path, prefixes=("KXMVESPORTS",), db="coll.sqlite3")
    bid_cc, qty = await _fill(rig)
    fee_cc, _ = await _fill_row(rig.store)
    assert fee_cc == maker_fee_ground_truth_cc(bid_cc, qty) > 0


async def test_non_matching_prefix_stays_zero(tmp_path: Path) -> None:
    rig = await _make_rig(tmp_path, prefixes=("KXMLB",), db="nomatch.sqlite3")
    bid_cc, qty = await _fill(rig)
    fee_cc, edge_cc = await _fill_row(rig.store)
    assert fee_cc == 0
    assert edge_cc == _raw_edge_cc(rig, bid_cc, qty)
    assert rig.lifecycle._realized_pnl_cc == 0  # noqa: SLF001


async def test_active_prefix_waiver_candidate_carries_fee(tmp_path: Path) -> None:
    rig = await _make_rig(tmp_path, prefixes=("KXMVE",), db="active_wc.sqlite3")
    bid_cc, qty = await _fill(rig)
    state = rig.lifecycle._executed_states["q1"]  # noqa: SLF001
    inputs = rig.lifecycle._build_state_worst_case_inputs(  # noqa: SLF001
        "q1", state, StructuralConfigView()
    )
    candidate = inputs.entities[-1]
    assert candidate.entity_id == "fill:q1"
    assert candidate.fee_cc == maker_fee_ground_truth_cc(bid_cc, qty) > 0
    # hit-side loss = premium + fee (the WorstCaseEntity contract).
    assert candidate.hit_loss_cc == candidate.premium_cc + candidate.fee_cc
    # Committed positions keep fee_cc=0 (dated TODO — OpenPosition carries no
    # per-fill fee yet); only THE CANDIDATE gains the predicted fee here.
    for entity in inputs.entities[:-1]:
        assert entity.fee_cc == 0


# --------------------------------------------------------------------------- #
# Eat-the-fee: the QUOTED PRICES are untouched by the flag.                    #
# --------------------------------------------------------------------------- #


async def test_quoted_prices_unchanged_by_active_prefix(tmp_path: Path) -> None:
    rig_off = await _make_rig(tmp_path, prefixes=(), db="q_off.sqlite3")
    rig_on = await _make_rig(tmp_path, prefixes=("KXMVE",), db="q_on.sqlite3")
    await rig_off.lifecycle.handle_rfq(rfq())
    await rig_on.lifecycle.handle_rfq(rfq())
    off = rig_off.sender.created[0]
    on = rig_on.sender.created[0]
    assert (on["yes"], on["no"]) == (off["yes"], off["no"])  # never widened


# --------------------------------------------------------------------------- #
# Replay: the nonzero fee is booked exactly once.                              #
# --------------------------------------------------------------------------- #


async def test_replayed_execution_books_fee_once(tmp_path: Path) -> None:
    rig = await _make_rig(tmp_path, prefixes=("KXMVE",), db="replay.sqlite3")
    bid_cc, qty = await _fill(rig)
    expected_fee = maker_fee_ground_truth_cc(bid_cc, qty)
    await rig.lifecycle.on_quote_executed({"quote_id": "q1", "order_id": "o1"})
    assert await rig.store.count("fills") == 1
    assert rig.lifecycle._realized_pnl_cc == -expected_fee  # noqa: SLF001 — once


# --------------------------------------------------------------------------- #
# FeeConfig shape.                                                             #
# --------------------------------------------------------------------------- #


def test_fee_config_default_and_yaml_shape() -> None:
    assert FeeConfig().maker_fee_active_prefixes == ()
    # YAML lists coerce to the tuple field (the operator sets a list).
    cfg = FeeConfig(maker_fee_active_prefixes=["KXMVE", "KXWCPARLAY"])
    assert cfg.maker_fee_active_prefixes == ("KXMVE", "KXWCPARLAY")
