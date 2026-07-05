from pathlib import Path

from combomaker.core.clock import FakeClock
from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.ops.persistence import Store
from combomaker.ops.report import build_report, format_report
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition

CONV = Conventions(
    verified=True,
    source="test",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)


async def seeded_store(tmp_path: Path) -> Store:
    store = await Store.open(tmp_path / "r.sqlite3", FakeClock())
    await store.record_decision("no_quote", "r1", ["skip_leg_stale"], {})
    await store.record_decision("quote_sent", "r2", ["quote_sent"], {})
    await store.record_fill(
        "fill:q1",
        order_id="o1",
        combo_ticker="KXMVE-C1",
        our_side="yes",
        contracts_centi=1_000,
        price_cc=2_500,
        fee_cc=None,
        expected_edge_cc=550,
        raw={},
    )
    await store.record_markout(
        "fill:q1",
        horizon_s=10.0,
        fair_at_fill_cc=3_000,
        fair_now_cc=2_900,
        raw_mid_at_fill_cc=3_050,
        raw_mid_now_cc=2_800,
    )
    return store


async def test_report_aggregates(tmp_path: Path) -> None:
    store = await seeded_store(tmp_path)
    try:
        report = await build_report(store, env="demo")
    finally:
        await store.close()
    assert report["rfqs_seen"] == 0
    assert report["decisions_by_kind"] == {"no_quote": 1, "quote_sent": 1}
    assert report["ev"]["fills"] == 1
    assert report["ev"]["expected_edge_cc"] == 550
    markout = report["markouts"][0]
    assert markout["n"] == 1
    assert markout["mean_fair_drift_cc"] == -100.0
    assert markout["mean_raw_mid_drift_cc"] == -250.0
    assert "plumbing" in report["note"]  # demo P&L is never edge validation
    text = format_report(report)
    assert "skip_leg_stale" in text


async def test_report_with_portfolio_mc(tmp_path: Path) -> None:
    store = await seeded_store(tmp_path)
    exposure = ExposureBook(CONV)
    exposure.add_position(
        OpenPosition(
            position_id="p1",
            combo_ticker="C",
            collection=None,
            our_side=Side.YES,
            contracts=CentiContracts(1_000),
            entry_price_cc=CentiCents(2_500),
            legs=(LegRef("M1", "E1", "yes"), LegRef("M2", "E2", "no")),
        )
    )
    marginals = {"M1": 0.6, "M2": 0.5}
    try:
        report = await build_report(
            store,
            env="demo",
            exposure=exposure,
            marginals=lambda t: marginals.get(t),
            mc_samples=20_000,
        )
    finally:
        await store.close()
    mc = report["portfolio_mc"]
    assert mc["positions"] == 1
    assert not mc["unknown_marginals"]
    # fair value of the combo = 0.6 * 0.5 = 0.30; we paid 0.25 → EV ≈ +0.05×10
    assert mc["ev_cc"] > 0
    assert 0.0 <= mc["p_profit"] <= 1.0
