"""THE FEE ENTERS EVERY EV, ONCE (2026-09-04 build A, item 4).

``rfq/edge.py`` is the single candidate-edge function; the lifecycle wraps
it with the measured fee. Pins:

- the pure arithmetic (YES / verified NO / unverified NO / fee None / fee);
- on a collection where the measured schedule charges 0.035, the fill
  ledger's ``expected_edge_cc`` == the quote-time candidate EV (eviction
  key on the open quote) == the confirm-time fresh pricing edge == the
  KILL-marginal fill EV, all NET of the same nonzero fee, to the cent;
- the fee is booked ONCE (replayed execution), and realized P&L carries it;
- an exchange-reported fee on a recovery replay nets the edge INSTEAD of the
  model fee (never both);
- on a plain quadratic collection every figure equals the gross edge.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.pricing.fee_observer import FeeObservation, model_fee_cc
from combomaker.pricing.fees import FeeType
from combomaker.rfq.edge import candidate_edge_cc, gross_edge_cc
from tests.test_fee_seam_wiring import COMBO_MAKER, _rig, _tick
from tests.test_lifecycle import accepted_msg, rfq

# ------------------------------------------------------------- pure arithmetic


def test_pure_edge_arithmetic() -> None:
    kw = dict(fair_cc=4_000, bid_cc=3_800, qty_centi=1_000, complement_verified=True)
    assert gross_edge_cc(our_side=Side.YES, **kw) == 200 * 10  # type: ignore[arg-type]
    assert gross_edge_cc(our_side=Side.NO, **kw) == (6_000 - 3_800) * 10  # type: ignore[arg-type]
    unverified = dict(kw, complement_verified=None)
    assert gross_edge_cc(our_side=Side.NO, **unverified) is None  # type: ignore[arg-type]
    assert candidate_edge_cc(our_side=Side.NO, fee_cc=None, **unverified) is None  # type: ignore[arg-type]
    assert candidate_edge_cc(our_side=Side.YES, fee_cc=None, **kw) == 2_000  # type: ignore[arg-type]
    assert candidate_edge_cc(our_side=Side.YES, fee_cc=0, **kw) == 2_000  # type: ignore[arg-type]
    assert candidate_edge_cc(our_side=Side.YES, fee_cc=875, **kw) == 1_125  # type: ignore[arg-type]
    # Floor division on fractional contracts, then the fee: never rounds the
    # fee away.
    assert candidate_edge_cc(
        fair_cc=4_449, bid_cc=5_270, qty_centi=400, our_side=Side.NO,
        complement_verified=True, fee_cc=350,
    ) == (5_551 - 5_270) * 400 // 100 - 350


# ------------------------------------------------------------- the lifecycle


def _charged_on_test_collection(n: int = 12) -> list[FeeObservation]:
    """Charged maker fills on the test rig's collection (``KXMVESPORTS``) at
    the measured 0.035 — enough to pin the coefficient."""
    out = []
    for i in range(n):
        probe = FeeObservation(
            f"t{i}", f"2026-09-01T00:00:{i:02d}Z", "KXMVESPORTS", 1_000 + 50 * i,
            4_500 + 20 * i, 0, True,
        )
        out.append(FeeObservation(
            probe.fill_id, probe.created_time, probe.collection_prefix,
            probe.contracts_centi, probe.price_cc, model_fee_cc(COMBO_MAKER, probe), True,
        ))
    return out


async def test_ledger_quote_confirm_and_kill_inputs_share_one_fee_net_edge(
    tmp_path: Path,
) -> None:
    rig = await _rig(tmp_path, fills=None, series=None)
    rig.sched.ingest(_charged_on_test_collection())
    assert rig.sched.maker_coef == COMBO_MAKER
    test_rfq = rfq()
    assert rig.engine.fee_type_for(test_rfq) is FeeType.QUADRATIC_WITH_COMBO_MAKER_FEES
    lc = rig.lifecycle
    await lc.handle_rfq(test_rfq)
    assert len(rig.sender.created) == 1
    yes_bid = int(rig.sender.created[0]["yes"])
    no_bid = int(rig.sender.created[0]["no"])
    state = lc._open["q1"]  # noqa: SLF001
    fair = int(state.constructed.fair_cc)
    qty = CentiContracts(1_000)  # accepted_msg fills 10.00 contracts
    fee_yes = lc._fill_fee_cc(  # noqa: SLF001
        CentiCents(yes_bid), qty, combo_ticker=test_rfq.market_ticker,
        collection=test_rfq.mve_collection_ticker,
    )
    assert fee_yes is not None and fee_yes > 0
    gross_yes = (fair - yes_bid) * 10
    net_yes = gross_yes - fee_yes
    # Quote-time: the open quote's eviction EV is the better side's NET edge.
    fee_no = lc._fill_fee_cc(  # noqa: SLF001
        CentiCents(no_bid), qty, combo_ticker=test_rfq.market_ticker,
        collection=test_rfq.mve_collection_ticker,
    )
    assert fee_no is not None
    net_no = (10_000 - fair - no_bid) * 10 - fee_no
    open_ev = rig.lifecycle._exposure.open_quotes["q1"].expected_edge_cc  # noqa: SLF001
    risk_qty = int(rig.lifecycle._exposure.open_quotes["q1"].contracts)  # noqa: SLF001
    assert open_ev is not None
    assert open_ev == max(
        (fair - yes_bid) * risk_qty // 100 - int(
            lc._fill_fee_cc(CentiCents(yes_bid), CentiContracts(risk_qty),  # noqa: SLF001
                            combo_ticker=test_rfq.market_ticker,
                            collection=test_rfq.mve_collection_ticker) or 0),
        (10_000 - fair - no_bid) * risk_qty // 100 - int(
            lc._fill_fee_cc(CentiCents(no_bid), CentiContracts(risk_qty),  # noqa: SLF001
                            combo_ticker=test_rfq.market_ticker,
                            collection=test_rfq.mve_collection_ticker) or 0),
    )
    # Accept YES, then confirm-time inputs BEFORE execution.
    await lc.on_quote_accepted(accepted_msg("q1", "yes"))
    executed = lc._executed_states["q1"]  # noqa: SLF001
    assert executed.pending_fill is not None
    assert lc._fill_ev_cc(executed) == net_yes  # noqa: SLF001 — the KILL-marginal chain
    fresh = lc._pricing_edge_cc(executed)  # noqa: SLF001 — the admission EV
    assert fresh == float(net_yes)
    # Execute: the ledger row carries the SAME net figure and the fee once.
    await lc.on_quote_executed({"quote_id": "q1", "order_id": "o1"})
    async with rig.store._db.execute(  # noqa: SLF001
        "SELECT fee_cc, expected_edge_cc FROM fills"
    ) as cur:
        rows = [tuple(r) async for r in cur]
    assert rows == [(fee_yes, net_yes)]
    assert lc._realized_pnl_cc == -fee_yes  # noqa: SLF001
    await lc.on_quote_executed({"quote_id": "q1", "order_id": "o1"})  # replay
    assert await rig.store.count("fills") == 1
    assert lc._realized_pnl_cc == -fee_yes  # noqa: SLF001 — booked once
    _ = net_no


async def test_exchange_reported_fee_nets_the_edge_instead_of_the_model(
    tmp_path: Path,
) -> None:
    rig = await _rig(tmp_path, fills=None, series=None)
    rig.sched.ingest(_charged_on_test_collection())
    lc = rig.lifecycle
    await lc.handle_rfq(rfq())
    yes_bid = int(rig.sender.created[0]["yes"])
    fair = int(lc._open["q1"].constructed.fair_cc)  # noqa: SLF001
    await lc.on_quote_accepted(accepted_msg("q1", "yes"))
    await lc.on_quote_executed({"quote_id": "q1", "order_id": "o1", "exchange_fee_cc": 999})
    async with rig.store._db.execute(  # noqa: SLF001
        "SELECT fee_cc, expected_edge_cc FROM fills"
    ) as cur:
        rows = [tuple(r) async for r in cur]
    assert rows == [(999, (fair - yes_bid) * 10 - 999)]
    assert lc._realized_pnl_cc == -999  # noqa: SLF001


async def test_plain_quadratic_collection_records_the_gross_edge(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, fills=None, series=None)
    lc = rig.lifecycle
    await lc.handle_rfq(rfq())
    yes_bid = int(rig.sender.created[0]["yes"])
    no_bid = int(rig.sender.created[0]["no"])
    fair = int(lc._open["q1"].constructed.fair_cc)  # noqa: SLF001
    await lc.on_quote_accepted(accepted_msg("q1", "yes"))
    # The fill EV is the BETTER-priced side's edge (pre-existing semantics),
    # gross on a plain quadratic collection (fee 0 on both sides).
    assert lc._fill_ev_cc(lc._executed_states["q1"]) == max(  # noqa: SLF001
        (fair - yes_bid) * 10, (10_000 - fair - no_bid) * 10
    )
    await lc.on_quote_executed({"quote_id": "q1", "order_id": "o1"})
    async with rig.store._db.execute(  # noqa: SLF001
        "SELECT fee_cc, expected_edge_cc FROM fills"
    ) as cur:
        rows = [tuple(r) async for r in cur]
    assert rows == [(0, (fair - yes_bid) * 10)]
    assert lc._realized_pnl_cc == 0  # noqa: SLF001
    await _tick(rig)
    assert rig.sched.maker_coef == Fraction(7, 100)  # still cold; nothing polled
