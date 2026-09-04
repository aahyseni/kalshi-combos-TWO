"""MEASURED retained-edge floor + rebate bound (2026-09-04 build A item 2).

- estimator: contract-weighted, game-clustered SE, empirical-Bayes shrink
  to the sport pool, thin cells (derived n_min: SE² > τ²) take the pool's
  UPPER bound, nothing publishes below the 14-day pooled span, z is the
  policy daily anchor (3);
- rebate bound: es_value caps at the measured Cov price; exposure-backed
  drops a leg-axis rebate whose mirror direction the book does not hold;
  widening passes untouched;
- store read + lifecycle sweep: settled rows -> cells -> published table on
  the engine (one dict lookup on the quote path).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from combomaker.pricing.retained_cell import CellKey
from combomaker.risk.cap_family import K_DAILY
from combomaker.risk.rebate_bound import bound_rebate, mirror_key
from combomaker.risk.retained_edge_floor import (
    MIN_POOL_DAYS,
    GradeRow,
    estimate_retained_floor,
    pool_stats,
    summarize,
)

MLB_ALL_NO_RFI: CellKey = ("mlb", "rfi|rfi", "all_no", "cross")
MLB_YES_ML: CellKey = ("mlb", "moneyline|moneyline", "all_yes", "cross")
MLB_HR_NO: CellKey = ("mlb", "player_hr|player_hr|player_hr", "all_no", "same")


def _row(
    cell: CellKey, i: int, *, shortfall_cc: float, contracts_centi: int = 1_000, day: int = 0
) -> GradeRow:
    """One settled combo with the given per-contract shortfall (cc), its own
    game cluster and settle day."""
    modeled = 1_500
    realized = modeled + int(round(shortfall_cc * contracts_centi / 100))
    return GradeRow(
        cell=cell,
        contracts_centi=contracts_centi,
        realized_cc=realized,
        modeled_cc=modeled,
        games=frozenset({f"G{cell[1][:3]}{i}"}),
        settled_at=f"2026-08-{1 + day:02d}T12:00:00+00:00",
    )


def _cell(cell: CellKey, shortfalls: list[float], *, start_day: int = 0) -> list[GradeRow]:
    return [
        _row(cell, i, shortfall_cc=s, day=start_day + (i % 20)) for i, s in enumerate(shortfalls)
    ]


def test_pool_stats_is_contract_weighted_and_game_clustered() -> None:
    rows = [
        GradeRow(MLB_YES_ML, 1_000, 2_000, 1_000, frozenset({"A"}), "2026-08-01T00:00:00Z"),
        GradeRow(MLB_YES_ML, 3_000, 2_000, 3_500, frozenset({"A"}), "2026-08-01T00:00:00Z"),
        GradeRow(MLB_YES_ML, 1_000, 1_000, 1_000, frozenset({"B"}), "2026-08-02T00:00:00Z"),
    ]
    st = pool_stats(rows)
    assert st.n_rows == 3 and st.n_clusters == 2 and st.contracts_centi == 5_000
    # Weighted mean of per-contract shortfalls: (1000*100 + 3000*(-50) + 0)/5000 = -10
    assert math.isclose(st.mean_cc, -10.0)
    assert st.se_cc is not None and st.se_cc > 0
    # One cluster only: SE undefined (never a guess).
    assert pool_stats(rows[:2]).se_cc is None


def test_nothing_publishes_below_the_pooled_span() -> None:
    rows = _cell(MLB_YES_ML, [5.0] * 30, start_day=0)
    short = [r for r in rows if r.settled_at < "2026-08-10"]
    est = estimate_retained_floor(short)
    assert not est.published and est.reason == "pool_span_below_minimum"
    assert est.table == {}
    assert MIN_POOL_DAYS == 14.0


def test_floor_is_the_z_upper_bound_of_adverse_selection() -> None:
    # Two well-populated cells: one outperforms the model by ~+5c/ct, one
    # loses ~−30c/ct (the all-NO NRFI×NRFI signature), plus a filler cell.
    good = _cell(MLB_YES_ML, [5.0 + (i % 3) for i in range(120)])
    bad = _cell(MLB_ALL_NO_RFI, [-30.0 + (i % 5) for i in range(120)])
    filler = _cell(MLB_HR_NO, [-2.0 + (i % 4) for i in range(120)])
    est = estimate_retained_floor(good + bad + filler)
    assert est.published and est.z == K_DAILY == 3.0
    by = {c.cell: c for c in est.cells}
    assert not by[MLB_YES_ML].thin and not by[MLB_ALL_NO_RFI].thin
    # The losing cell's floor covers its measured shortfall plus z·SE.
    b = by[MLB_ALL_NO_RFI]
    assert b.post_mean_cc < -20.0 and b.post_se_cc is not None
    assert b.floor_cc == max(0, math.ceil(3.0 * b.post_se_cc - b.post_mean_cc))
    assert b.floor_cc >= 20
    # The outperforming cell floors at max(0, z·SE − mean): its own
    # performance offsets the uncertainty; never negative.
    g = by[MLB_YES_ML]
    assert g.floor_cc == max(0, math.ceil(3.0 * (g.post_se_cc or 0.0) - g.post_mean_cc))
    assert g.floor_cc < b.floor_cc
    assert est.table[MLB_ALL_NO_RFI] == b.floor_cc
    summary = summarize(est)
    assert summary["n_cells"] == 3 and summary["published"] is True


def test_thin_cell_takes_the_sport_pools_upper_bound() -> None:
    good = _cell(MLB_YES_ML, [5.0 + (i % 3) for i in range(120)])
    bad = _cell(MLB_ALL_NO_RFI, [-30.0 + (i % 5) for i in range(120)])
    # A brand-new shape with 2 settled games: its SE is far above τ.
    new_cell: CellKey = ("mlb", "player_ks|total", "mixed", "same")
    thin = _cell(new_cell, [40.0, -35.0])
    est = estimate_retained_floor(good + bad + thin)
    by = {c.cell: c for c in est.cells}
    assert by[new_cell].thin and by[new_cell].source == "pool"
    assert by[new_cell].floor_cc == est.pool_floor_cc["mlb"]
    # And a single-cluster cell (SE undefined) is thin too.
    one = _cell(("mlb", "total|total", "all_yes", "cross"), [1.0])
    est2 = estimate_retained_floor(good + bad + one)
    lone = {c.cell: c for c in est2.cells}[("mlb", "total|total", "all_yes", "cross")]
    assert lone.thin and lone.floor_cc == est2.pool_floor_cc["mlb"]


def test_shrinkage_pulls_a_noisy_cell_toward_its_sport_pool() -> None:
    pool = _cell(MLB_YES_ML, [0.0 + (i % 7) - 3 for i in range(200)])
    noisy: CellKey = ("mlb", "spread|spread", "all_yes", "cross")
    rows = pool + _cell(noisy, [-60.0, 50.0, -55.0, 45.0, -58.0, 52.0, -50.0, 48.0])
    est = estimate_retained_floor(rows)
    c = {x.cell: x for x in est.cells}[noisy]
    raw_mean = c.stats.mean_cc
    # Posterior mean sits between the raw cell mean and the pool mean.
    mu = est.pools["mlb"].mean_cc
    assert min(raw_mean, mu) - 1e-9 <= c.post_mean_cc <= max(raw_mean, mu) + 1e-9
    assert 0.0 <= c.weight_on_cell <= 1.0


# ---------------------------------------------------------------- rebate bound


def test_mirror_key() -> None:
    assert mirror_key("KXMLBHR:no") == "KXMLBHR:yes"
    assert mirror_key("KXMLBKS:BOSSGRAY54:yes") == "KXMLBKS:BOSSGRAY54:no"
    assert mirror_key("KXMLBKS") is None and mirror_key("KXMLBKS:maybe") is None


def test_rebate_bound_rules() -> None:
    common = dict(
        family_cc=-26, entity_cc=-8,
        candidate_family_keys={"KXMLBHR:no"}, candidate_entity_keys={"KXMLBHR:MINRLEWIS23:no"},
        shares_by_family={"KXMLBKS:yes": 1.0}, shares_by_entity={"KXMLBKS:BOSSGRAY54:yes": 1.0},
        leg_axis_armed=True,
    )
    # Widening / nothing: untouched.
    assert bound_rebate(-150, value_cc_per_contract=None, **common).rebate_cc == -150  # type: ignore[arg-type]
    assert bound_rebate(0, value_cc_per_contract=5.0, **common).rule == "none"  # type: ignore[arg-type]
    # es_value: the measured Cov price caps the rebate; <= 0 value => no rebate.
    b = bound_rebate(184, value_cc_per_contract=12.3, **common)  # type: ignore[arg-type]
    assert (b.rebate_cc, b.rule, b.cap_cc) == (13, "es_value", 13)
    assert bound_rebate(184, value_cc_per_contract=-4.0, **common).rebate_cc == 0  # type: ignore[arg-type]
    assert bound_rebate(10, value_cc_per_contract=40.0, **common).rebate_cc == 10  # type: ignore[arg-type]
    # exposure_backed: the HR:no family and entity rebates on a book holding
    # NO HR:yes exposure are unbacked and removed (the 8/12 $137 ticket).
    b = bound_rebate(184, value_cc_per_contract=None, **common)  # type: ignore[arg-type]
    assert (b.rebate_cc, b.rule, b.unbacked_cc) == (184 - 34, "exposure_backed", 34)
    # ...but backed once the book holds the mirror direction.
    held = dict(common, shares_by_family={"KXMLBHR:yes": 0.4, "KXMLBKS:yes": 0.6})
    b = bound_rebate(184, value_cc_per_contract=None, **held)  # type: ignore[arg-type]
    assert b.unbacked_cc == 8 and b.rebate_cc == 176  # entity still unbacked
    # Leg axis unarmed: nothing to remove (it never entered the price).
    b = bound_rebate(184, value_cc_per_contract=None, **dict(common, leg_axis_armed=False))  # type: ignore[arg-type]
    assert b.rebate_cc == 184 and b.unbacked_cc == 0
    # Never below zero.
    b = bound_rebate(20, value_cc_per_contract=None, **common)  # type: ignore[arg-type]
    assert b.rebate_cc == 0


# ------------------------------------------------ store read + lifecycle sweep


async def test_sweep_publishes_a_floor_table_from_the_settled_grade(tmp_path: Path) -> None:
    from combomaker.ops.persistence import Store
    from tests.test_fee_seam_wiring import _rig, _tick

    rig = await _rig(tmp_path, fills=None, series=None)
    store: Store = rig.store
    legs = json.dumps([
        {"market_ticker": "KXMLBRFI-26AUG01SFCLE", "event_ticker": "KXMLBRFI-26AUG01SFCLE",
         "side": "no"},
        {"market_ticker": "KXMLBRFI-26AUG01TEXCWS", "event_ticker": "KXMLBRFI-26AUG01TEXCWS",
         "side": "no"},
    ])
    # 40 settled NRFI×NRFI combos across 40 game pairs and 20 days, each
    # losing the model by ~25c/ct; 40 ML parlays beating it by ~3c/ct.
    for i in range(40):
        for cell_i, (tk, lg, edge, pnl) in enumerate((
            (f"KXMVE-RFI{i}", legs, 200, -2_300),
            (f"KXMVE-ML{i}", json.dumps([
                {"market_ticker": f"KXMLBGAME-26AUG{i:02d}AAABBB-AAA",
                 "event_ticker": f"KXMLBGAME-26AUG{i:02d}AAABBB", "side": "yes"},
                {"market_ticker": f"KXMLBGAME-26AUG{i:02d}CCCDDD-CCC",
                 "event_ticker": f"KXMLBGAME-26AUG{i:02d}CCCDDD", "side": "yes"},
            ]), 200, 500),
        )):
            legs_i = lg.replace("26AUG01", f"26AUG{i:02d}") if cell_i == 0 else lg
            await store.record_fill(
                f"fill:{tk}", order_id=f"o{tk}", combo_ticker=tk, our_side="no",
                contracts_centi=1_000, price_cc=7_000, fee_cc=0, expected_edge_cc=edge, raw={},
            )
            await store._db.execute(  # noqa: SLF001 — seed a settled ledger row directly
                "INSERT INTO position_ledger (position_id, opened_at, combo_ticker,"
                " collection_ticker, subaccount, our_side, contracts_centi, entry_price_cc,"
                " cost_cc, fees_cc, leg_set_hash, legs_json, status, settled_value,"
                " realized_pnl_cc, settlement_fee_cc, reconciled_at) VALUES"
                " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"pos:{tk}", f"2026-08-{1 + i % 20:02d}T10:00:00+00:00", tk, "KXMVESPORTS",
                    "0", "no", 1_000, 7_000, 70_000, 0, f"h{tk}", legs_i, "settled", 0.0,
                    pnl, 0, f"2026-08-{2 + i % 20:02d}T03:00:00+00:00",
                ),
            )
    await store._db.commit()  # noqa: SLF001
    rows = await store.settled_grade_rows()
    assert len(rows) == 80
    await _tick(rig)
    assert rig.metrics.counter("retained_floor.ran") == 1
    assert rig.metrics.counter("retained_floor.published") == 1
    table = rig.engine._retained_floor  # noqa: SLF001
    assert table is not None
    rfi = table[("mlb", "rfi|rfi", "all_no", "cross")]
    ml = table[("mlb", "moneyline|moneyline", "all_yes", "cross")]
    assert rfi > ml and rfi >= 20  # the losing shape floors far above the winner
    # Inside the cadence nothing re-runs; the table stays.
    await _tick(rig)
    assert rig.metrics.counter("retained_floor.ran") == 1
