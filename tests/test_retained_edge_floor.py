"""MEASURED retained-edge floor + rebate bound (2026-09-04 build A item 2;
the POINT rule since build "floor-point-estimate" the same day).

- estimator: contract-weighted, game-clustered SE, empirical-Bayes shrink
  to the sport pool, floor = max(0, −shrunk point shortfall) — NO z·SE term
  (PIN CHANGED 2026-09-04 build "floor-point-estimate": build A's
  t_{G−1}(Φ(−3))·SE upper bound published 15-59c floors on populated cells
  and 5.9-49.5c pool floors against 1-3c tier margins, so the rebate cap
  margin − fee − floor was <= 0 on essentially every quote — the diversity
  steer was muted on 100% of populated cells; the z ladder anchors TAIL
  risk, the floor is a point estimate of a cost); thin cells (derived
  n_min: SE² > τ²) take the pool's POINT; nothing publishes below the
  14-day pooled span;
- rebate bound: es_value caps at the measured Cov price; exposure-backed
  drops a leg-axis rebate whose mirror direction the book does not hold;
  widening passes untouched;
- store read + lifecycle sweep: settled rows -> cells -> published table on
  the engine (one dict lookup on the quote path);
- FAIL-CLOSED lookup (review fix M2): a cell ABSENT from the published
  table resolves to its sport pool's point, an unknown sport to the largest
  published pool point — never to None (= the unmeasured cap) while a table
  is published;
- cluster key (review fix S8): a settled row whose legs carry no event
  ticker is its own cluster, keyed on the combo ticker;
- property: through construct_quote the post-rebate retained margin never
  drops below the fee floor, and never below fee + cell floor unless the
  margin itself is smaller (floor >= fee always).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from combomaker.core.money import CC_PER_DOLLAR
from combomaker.pricing.quote import ConstructedQuote
from combomaker.pricing.retained_cell import CellKey, cell_key, floor_for_cell
from combomaker.risk.rebate_bound import bound_rebate, mirror_key
from combomaker.risk.retained_edge_floor import (
    MIN_POOL_DAYS,
    GradeRow,
    estimate_retained_floor,
    grade_row_from_store,
    point_floor_cc,
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


def test_point_floor_is_the_measured_loss_and_never_negative() -> None:
    assert point_floor_cc(-1_999.0) == 1_999  # mlb|rfi|rfi|all_no|cross: −20c/ct
    assert point_floor_cc(-0.4) == 1  # ceil: a loss of a fraction of a cc still floors
    assert point_floor_cc(0.0) == 0
    assert point_floor_cc(2_790.0) == 0  # an outperforming cell floors at the fee alone
    assert point_floor_cc(-0.0) == 0


def test_floor_is_the_shrunk_point_shortfall() -> None:
    """PIN CHANGED 2026-09-04 (build "floor-point-estimate"): build A pinned
    floor == max(0, q·SE − mean) with q the Student-t quantile of Φ(−3)
    at the cell's clusters − 1 df; live that floored every populated cell
    at 15-59c against 1-3c margins (rebate muted on 100% of populated
    cells). The floor is now the SHRUNK POINT: a losing cell keeps its
    whole measured shortfall (fee + |shortfall| after construct_quote adds
    the fee), a cell at or above the model floors at 0 (the fee alone)."""
    # Two well-populated cells: one outperforms the model by ~+5c/ct, one
    # loses ~−30c/ct (the all-NO NRFI×NRFI signature), plus a filler cell.
    good = _cell(MLB_YES_ML, [5.0 + (i % 3) for i in range(120)])
    bad = _cell(MLB_ALL_NO_RFI, [-30.0 + (i % 5) for i in range(120)])
    filler = _cell(MLB_HR_NO, [-2.0 + (i % 4) for i in range(120)])
    est = estimate_retained_floor(good + bad + filler)
    assert est.published
    by = {c.cell: c for c in est.cells}
    assert not by[MLB_YES_ML].thin and not by[MLB_ALL_NO_RFI].thin
    b = by[MLB_ALL_NO_RFI]
    assert b.post_mean_cc < -20.0 and b.post_se_cc is not None and b.source == "cell"
    # NEGATIVE cell: floor = ⌈−post_mean⌉ — the whole measured loss, no SE term.
    assert b.floor_cc == math.ceil(-b.post_mean_cc) == point_floor_cc(b.post_mean_cc)
    assert 20 <= b.floor_cc <= 30
    # What build A would have published on the same cell: strictly more.
    assert b.floor_cc < math.ceil(3.0 * b.post_se_cc - b.post_mean_cc)
    # POSITIVE cell: floor 0 — the fee alone; its SE (≈0.5c here) no longer
    # enters the floor at all.
    g = by[MLB_YES_ML]
    assert g.post_mean_cc > 0.0 and g.floor_cc == 0 and g.post_se_cc is not None
    assert g.post_se_cc > 0.0
    assert est.table[MLB_ALL_NO_RFI] == b.floor_cc and est.table[MLB_YES_ML] == 0
    summary = summarize(est)
    assert summary["n_cells"] == 3 and summary["published"] is True
    assert summary["rule"] == "shrunk_point"
    assert summary["n_populated_losing"] == 2 and summary["n_populated_at_fee"] == 1
    assert summary["n_populated_sign_unresolved"] == 0  # all three cells strongly signed
    assert "pool_quantile_by_sport" not in summary and "z" not in summary


def test_a_cell_whose_sign_is_unresolved_is_counted_not_trusted() -> None:
    """Review fix S2 (2026-09-04): a populated cell whose |shrunk point| is
    inside one posterior SE still gets its point floor (the mechanism never
    guesses a sign), but summarize() counts it so the log line carries the
    watch list for the pre-registered >= 2-week read — live, 61 of 206
    populated cells, the ML×ML cross-game class (|post|/SE 0.29, floor
    171 cc) among them. Such a floor is not a measured loss."""
    good = _cell(MLB_YES_ML, [5.0 + (i % 3) for i in range(120)])
    bad = _cell(MLB_ALL_NO_RFI, [-30.0 + (i % 5) for i in range(120)])
    coin: CellKey = ("mlb", "player_ks|player_ks", "all_yes", "cross")
    noisy = _cell(coin, [-40.0, 38.0] * 30)  # mean −1, clustered SE ≈ 5: sign open
    est = estimate_retained_floor(good + bad + noisy)
    c = {x.cell: x for x in est.cells}[coin]
    assert not c.thin and c.post_se_cc is not None
    assert abs(c.post_mean_cc) < c.post_se_cc
    assert c.floor_cc == point_floor_cc(c.post_mean_cc)  # still a point, never a bound
    summary = summarize(est)
    assert summary["n_populated_sign_unresolved"] == 1
    assert summary["n_populated_losing"] + summary["n_populated_at_fee"] == 3


def test_thin_cell_takes_the_sport_pools_point() -> None:
    """PIN CHANGED 2026-09-04 (build "floor-point-estimate"): a thin cell
    used to take the pool's t-quantile UPPER bound (5.9c on mlb live); it
    takes the pool's POINT max(0, −pool mean) — 0 on a pool at or above
    the model, its measured loss on a losing pool."""
    good = _cell(MLB_YES_ML, [5.0 + (i % 3) for i in range(120)])
    bad = _cell(MLB_ALL_NO_RFI, [-30.0 + (i % 5) for i in range(120)])
    # A brand-new shape with 2 settled games: its SE is far above τ.
    new_cell: CellKey = ("mlb", "player_ks|total", "mixed", "same")
    thin = _cell(new_cell, [40.0, -35.0])
    est = estimate_retained_floor(good + bad + thin)
    by = {c.cell: c for c in est.cells}
    assert by[new_cell].thin and by[new_cell].source == "pool"
    assert by[new_cell].floor_cc == est.pool_floor_cc["mlb"]
    pool = est.pools["mlb"]
    assert est.pool_floor_cc["mlb"] == point_floor_cc(pool.mean_cc) == math.ceil(-pool.mean_cc)
    assert pool.mean_cc < 0.0 and est.pool_floor_cc["mlb"] > 0  # this pool loses on net
    assert pool.se_cc is not None
    assert est.pool_floor_cc["mlb"] < math.ceil(3.0 * pool.se_cc - pool.mean_cc)  # not the bound
    # And a single-cluster cell (SE undefined) is thin too.
    one = _cell(("mlb", "total|total", "all_yes", "cross"), [1.0])
    est2 = estimate_retained_floor(good + bad + one)
    lone = {c.cell: c for c in est2.cells}[("mlb", "total|total", "all_yes", "cross")]
    assert lone.thin and lone.floor_cc == est2.pool_floor_cc["mlb"]
    # A pool at or above the model: its thin cells floor at 0 (fee alone).
    winning = _cell(("soccer", "btts|total", "all_no", "same"), [3.0 + (i % 5) for i in range(60)])
    thin_soccer = _cell(("soccer", "moneyline|total", "mixed", "same"), [-50.0, 45.0])
    est3 = estimate_retained_floor(good + bad + winning + thin_soccer)
    assert est3.pools["soccer"].mean_cc > 0.0 and est3.pool_floor_cc["soccer"] == 0
    assert est3.table[("soccer", "moneyline|total", "mixed", "same")] == 0


def test_unknown_sport_takes_the_largest_pool_point() -> None:
    """The fail-closed DIRECTION for a sport with no settled record: the
    largest pool POINT (never a bound) — through the live lookup."""
    mlb = _cell(MLB_ALL_NO_RFI, [-30.0 + (i % 5) for i in range(120)])
    soccer = _cell(("soccer", "btts|total", "all_no", "same"), [3.0 + (i % 5) for i in range(60)])
    est = estimate_retained_floor(mlb + soccer)
    assert est.pool_floor_cc["mlb"] > 0 and est.pool_floor_cc["soccer"] == 0
    nfl: CellKey = ("nfl", "moneyline|moneyline", "all_yes", "cross")
    assert floor_for_cell(nfl, est.table, est.pool_floor_cc) == est.pool_floor_cc["mlb"]
    assert floor_for_cell(nfl, est.table, est.pool_floor_cc) == max(est.pool_floor_cc.values())
    # a known sport, an unseen shape: that sport's point (0 on soccer here)
    assert floor_for_cell(("soccer", "spread|total", "mixed", "same"), est.table,
                          est.pool_floor_cc) == 0


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
    # A populated cell's floor is exactly the point of its SHRUNK mean.
    if not c.thin:
        assert c.floor_cc == point_floor_cc(c.post_mean_cc)


@settings(derandomize=True, max_examples=200)
@given(
    shortfalls=st.lists(st.floats(-8_000.0, 3_000.0), min_size=2, max_size=40),
    pool_shortfalls=st.lists(st.floats(-3_000.0, 3_000.0), min_size=30, max_size=60),
)
def test_every_published_floor_is_non_negative_and_a_point(
    shortfalls: list[float], pool_shortfalls: list[float]
) -> None:
    rows = _cell(MLB_YES_ML, pool_shortfalls) + _cell(MLB_ALL_NO_RFI, shortfalls)
    est = estimate_retained_floor(rows)
    assert est.published
    for c in est.cells:
        assert c.floor_cc >= 0
        if c.thin:
            assert c.floor_cc == est.pool_floor_cc[c.cell[0]]
        else:
            assert c.floor_cc == point_floor_cc(c.post_mean_cc)
    for sport, pool in est.pools.items():
        assert est.pool_floor_cc[sport] == point_floor_cc(pool.mean_cc) >= 0


# ---------------------------------------- floor >= fee ALWAYS (construct_quote)


@settings(derandomize=True, max_examples=300)
@given(
    fair=st.floats(0.05, 0.95),
    markup=st.integers(0, 400),
    skew=st.integers(0, 800),
    cell_floor=st.integers(0, 7_000),
    qty=st.sampled_from([100, 120, 250, 1_000, 5_000]),
)
def test_retained_margin_after_the_rebate_never_drops_below_fee_plus_floor(
    fair: float, markup: int, skew: int, cell_floor: int, qty: int
) -> None:
    """floor >= fee ALWAYS: through the live construct_quote (floor mode, the
    measured combo schedule), the retained margin left after ANY rebate is
    at least m_min (the confirm gate's fee predicate at this quantity) and
    at least fee + cell floor unless the tier margin itself is smaller —
    the cap is margin − m_min − floor and nothing looser; a floor of 0
    leaves exactly margin − m_min. The grid snap only ever LOWERS the bid
    (raises the retained margin)."""
    from tests.test_quote_fee_floor import confirm_edge_cc, derived_floor, quote

    q = quote(fair=fair, markup=markup, skew=skew, mode="floor", retained_floor=cell_floor, qty=qty)
    if not isinstance(q, ConstructedQuote) or int(q.no_bid_cc) <= 0:
        return  # declined side: nothing was sold
    fair_cc = int(round(fair * CC_PER_DOLLAR))
    no_fair = CC_PER_DOLLAR - fair_cc
    retained = no_fair - int(q.no_bid_cc)
    fee_floor = derived_floor(no_fair, markup, skew, qty=qty)
    margin = max(markup, fee_floor)
    assert retained >= fee_floor
    assert retained >= min(margin, fee_floor + cell_floor)
    assert confirm_edge_cc(fair_cc, int(q.no_bid_cc), qty) > 0


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
    # measured_floor (2026-09-04 night, pin CHANGED with the rule): the
    # build-A exposure_backed rule removed the HR:no family/entity rebates on
    # a book holding no HR:yes exposure (184 -> 150). Measured on the 9/4 tape
    # it stripped the rebate from 77% of sends (0.3-0.5c on the wire, the
    # margin auctions are won by) and contradicted the diversity-via-pricing
    # doctrine, so it was RETIRED: the rebate passes through here and the
    # measured per-cell floor bounds it in construct_quote. ``unbacked_cc``
    # is telemetry only (what the retired rule would have removed).
    b = bound_rebate(184, value_cc_per_contract=None, **common)  # type: ignore[arg-type]
    assert (b.rebate_cc, b.rule, b.unbacked_cc) == (184, "measured_floor", 34)
    # Telemetry still distinguishes a mirror the book holds (family backed,
    # entity not) — the rebate itself is unchanged either way.
    held = dict(common, shares_by_family={"KXMLBHR:yes": 0.4, "KXMLBKS:yes": 0.6})
    b = bound_rebate(184, value_cc_per_contract=None, **held)  # type: ignore[arg-type]
    assert b.unbacked_cc == 8 and b.rebate_cc == 184
    # Leg axis unarmed: nothing to report (it never entered the price).
    b = bound_rebate(184, value_cc_per_contract=None, **dict(common, leg_axis_armed=False))  # type: ignore[arg-type]
    assert b.rebate_cc == 184 and b.unbacked_cc == 0
    # A rebate smaller than the telemetry figure is still passed through whole.
    b = bound_rebate(20, value_cc_per_contract=None, **common)  # type: ignore[arg-type]
    assert b.rebate_cc == 20 and b.unbacked_cc == 34


# ------------------------------------------------ store read + lifecycle sweep


async def test_sweep_publishes_a_floor_table_from_the_settled_grade(tmp_path: Path) -> None:
    from combomaker.ops.persistence import Store
    from tests.test_fee_seam_wiring import _rig, _tick
    from tests.test_lifecycle import rfq
    from tests.test_pricing_engine import combo

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
            # Commit each direct seed before the next store call: since the
            # tape-writer review #2 fix pass a transaction found open on entry
            # to a ledger write is ROLLED BACK (foreign residue), never joined.
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
    # PIN (build "floor-point-estimate"): the losing shape floors at EXACTLY
    # its measured loss — (−2300 − 200) cc over 10 contracts = −250 cc/ct —
    # the winning shape at 0 (fee alone); the mlb pool point is the
    # contract-weighted mean of the two: (−250·40 + 30·40)/80 = −110 → 110.
    assert rfi == 250 and ml == 0
    assert rfi > ml and rfi >= 20  # the losing shape floors far above the winner
    # REVIEW FIX M2: the pool points travel with the table, and an RFQ whose
    # cell was never settled resolves to its sport's pool point — never None
    # (the margin // 2 cap) while a table is published.
    est = estimate_retained_floor([r for r in map(grade_row_from_store, rows) if r])
    assert rig.engine.retained_pool_floor == est.pool_floor_cc and "mlb" in est.pool_floor_cc
    assert est.pool_floor_cc["mlb"] == 110
    unseen_mlb = combo([
        {"market_ticker": "KXMLBHR-26AUG262105MINATH-MINRLEWIS23-1",
         "event_ticker": "KXMLBHR-26AUG262105MINATH", "side": "no"},
        {"market_ticker": "KXMLBHR-26AUG262105MINATH-ATHBROOKER-1",
         "event_ticker": "KXMLBHR-26AUG262105MINATH", "side": "no"},
    ])
    assert cell_key(unseen_mlb.legs) not in table
    assert rig.engine._retained_floor_for(unseen_mlb) == est.pool_floor_cc["mlb"]  # noqa: SLF001
    # An unknown sport (the harness's M1/M2 legs key to "other", which has
    # no pool) takes the LARGEST published pool point.
    other = rfq()
    assert cell_key(other.legs)[0] not in est.pool_floor_cc
    assert rig.engine._retained_floor_for(other) == max(est.pool_floor_cc.values())  # noqa: SLF001
    # Inside the cadence nothing re-runs; the table stays.
    await _tick(rig)
    assert rig.metrics.counter("retained_floor.ran") == 1
    # Clearing the table clears the pools with it.
    rig.engine.publish_retained_floor(None)
    assert rig.engine._retained_floor_for(other) is None  # noqa: SLF001
    assert rig.engine.retained_pool_floor == {}


# --------------------------------------------- fail-closed lookup (review M2)


def test_absent_cell_resolves_to_its_pool_never_to_none() -> None:
    """The reviewer's demonstration: on a 300cc margin / 88cc fee, a cell
    ABSENT from the table used to get margin // 2 = 150cc of rebate (the
    loosest cap in the system) while a THIN cell got 0 and a floor-0 cell
    210. The lookup fails closed exactly like a thin cell — to the pool's
    published value (a POINT since build "floor-point-estimate"; the
    numbers here are the lookup's inputs, not a rule)."""
    table = {MLB_ALL_NO_RFI: 448, MLB_YES_ML: 0}
    pools = {"mlb": 590, "soccer": 1_128}
    assert floor_for_cell(MLB_ALL_NO_RFI, table, pools) == 448
    assert floor_for_cell(MLB_YES_ML, table, pools) == 0
    # never settled, known sport -> that sport's pool value
    assert floor_for_cell(("mlb", "rfi|rfi", "all_no", "same"), table, pools) == 590
    assert floor_for_cell(("soccer", "btts|total", "all_no", "same"), table, pools) == 1_128
    # unknown sport ('other', 'esports' with no pool) -> the largest pool
    other_cell: CellKey = ("other", "moneyline|moneyline", "all_yes", "cross")
    assert floor_for_cell(other_cell, table, pools) == 1_128
    # a table published without pools (a rig) -> the largest cell floor
    assert floor_for_cell(("other", "x", "all_yes", "cross"), table, {}) == 448
    assert floor_for_cell(("other", "x", "all_yes", "cross"), {}, {}) == 0


# ---------------------------------------------------- cluster key (review S8)


def test_rows_without_event_tickers_are_their_own_cluster() -> None:
    def store_row(ticker: str, legs: list[dict[str, object]]) -> dict[str, object]:
        return {
            "combo_ticker": ticker,
            "ledger_contracts_centi": 1_000,
            "realized_pnl_cc": -500,
            "settlement_fee_cc": 0,
            "opened_at": "2026-08-01T10:00:00+00:00",
            "settled_at": "2026-08-02T03:00:00+00:00",
            "legs_json": json.dumps(legs),
            "fill_contracts_centi": 1_000,
            "expected_edge_cc": 200,
            "fill_fee_cc": 30,
        }

    with_events = grade_row_from_store(store_row("KXMVE-A", [
        {"market_ticker": "KXMLBRFI-26AUG01SFCLE", "event_ticker": "KXMLBRFI-26AUG01SFCLE",
         "side": "no"},
    ]))
    a = grade_row_from_store(store_row("KXMVE-B", [
        {"market_ticker": "KXMLBRFI-26AUG01TEXCWS", "side": "no"},
    ]))
    b = grade_row_from_store(store_row("KXMVE-C", [
        {"market_ticker": "KXMLBRFI-26AUG01NYYBOS", "side": "no"},
    ]))
    assert with_events is not None and a is not None and b is not None
    assert with_events.games == frozenset({"26AUG01SFCLE"})
    assert a.games == frozenset({"combo:KXMVE-B"}) and b.games == frozenset({"combo:KXMVE-C"})
    assert a.games != b.games  # two rows, two clusters (not one shared empty set)
    assert pool_stats([a, b]).n_clusters == 2
    # modeled = expected_edge + booked fee − settlement fee; realized as booked
    assert a.modeled_cc == 230 and a.realized_cc == -500
    # unreadable legs -> None (skipped, never a guessed row)
    assert grade_row_from_store(store_row("KXMVE-D", [])) is None
    bad = store_row("KXMVE-E", [{"side": "no"}])
    assert grade_row_from_store(bad) is None


# ------------------------------------ a small outperforming pool floors at 0


def test_an_outperforming_pool_floors_at_zero_by_its_point() -> None:
    """PIN CHANGED 2026-09-04 (build "floor-point-estimate"). The live
    cross-sport pool: 3 settled rows in 3 clusters, +27.9c/ct vs the
    model, SE 4.0c. Build A's fix pass floored it at the 2-df t quantile
    (19.2·SE − mean ≈ 49.5c) so that every absent cross-sport cell was
    fail-closed to a BOUND; the point rule floors it at max(0, −27.9c) = 0:
    the record says the shape wins, so the fee alone is retained and the
    rebate room is margin − m_min. The direction stays fail-closed where it
    matters — an UNKNOWN sport takes the largest pool point, never 0 by
    default (see test_unknown_sport_takes_the_largest_pool_point)."""
    mlb = _cell(MLB_YES_ML, [0.0 + (i % 7) - 3 for i in range(200)])
    other_cell: CellKey = ("other", "btts|moneyline", "all_yes", "cross")
    other = _cell(other_cell, [27.0, 32.0, 24.6])
    est = estimate_retained_floor(mlb + other)
    pool = est.pools["other"]
    assert pool.n_clusters == 3 and pool.mean_cc > 20.0 and pool.se_cc is not None
    assert est.pool_floor_cc["other"] == 0
    assert math.ceil(19.2 * pool.se_cc - pool.mean_cc) > 0  # what build A published
    absent: CellKey = ("other", "rfi|rfi", "all_no", "cross")
    assert floor_for_cell(absent, est.table, est.pool_floor_cc) == 0
    assert summarize(est)["pool_floor_cc"] == {"mlb": est.pool_floor_cc["mlb"], "other": 0}
