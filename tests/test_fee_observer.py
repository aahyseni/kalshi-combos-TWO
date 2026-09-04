"""MEASURED maker-fee schedule (2026-09-04 fee-seam repair, build A item 1).

Ground truth: ``tests/fixtures/ground_truth/maker_fee_20260820.json`` — every
charged MAKER fill on the exchange since Kalshi began charging combo maker
fees (2026-08-20 09:07Z), pulled from GET /portfolio/fills. Pins:

- ``FeeType.parse`` of Kalshi's LIVE string routes to the MAKER branch (the
  old parser mapped it to UNKNOWN = a no-quote brick);
- the model at 0.035 reproduces EVERY charged fee to the centi-cent (the
  exchange's own cost-rounding residue accounted for), 0.0175 reproduces NONE;
- the residue ALONE is not a fee (review fix M1): every pre-onset maker fill
  of the whole real tape (``exchange_fills_uncharged_20260827.json``, 3,582
  maker + 106 taker rows) parses as UNCHARGED, so ingesting the entire
  4,228-row history is silent — zero mismatches, only the sharded collection
  marked — and a schema-1 persisted file self-heals on load;
- the observer FITS 0.0350 from the tape, quantised to the publication
  quantum, and the fill count that pins it is DERIVED from the fills;
- a synthetic 0.0175 regime arriving later flips the fit and raises the
  DRIFT verdict (alarm, never a halt);
- bootstrap with no charged fill = the TAKER coefficient (fail-safe), never
  a guessed zero; persistence round-trips; fee-type precedence.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.exchange.fills import (
    charged_fee_cc,
    fee_observation_from_fill,
    fee_observations_from_fills,
)
from combomaker.pricing.fee_observer import (
    CEIL_SLACK_CC,
    COEF_QUANTUM,
    SCHEMA_VERSION,
    FeeObservation,
    FeeScheduleSummary,
    ObservedFeeSchedule,
    charged_maker,
    collection_prefix_of,
    cost_residue_cc,
    feasible_bounds,
    fit_maker_coefficient,
    least_squares_coefficient,
    model_fee_cc,
    pinning_count,
    quantise,
    quantum_multiples,
    regime_window,
    validate,
)
from combomaker.pricing.fees import FeeModel, FeeSchedule, FeeType, FeeUnknownError

FIXTURE = Path(__file__).parent / "fixtures" / "ground_truth" / "maker_fee_20260820.json"
UNCHARGED_FIXTURE = (
    Path(__file__).parent / "fixtures" / "ground_truth" / "exchange_fills_uncharged_20260827.json"
)
TAKER = Fraction(7, 100)
COMBO_MAKER = Fraction(35, 1000)     # measured 2026-08-20+
SINGLE_MAKER = Fraction(175, 10_000)  # the 6/29 PDF single-market maker schedule

VERIFIED_MAKER = Conventions(
    verified=True,
    source="test fixture",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)

JsonDict = dict[str, Any]


@pytest.fixture(scope="module")
def fixture() -> JsonDict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _rows_as_fills(rows: list[JsonDict]) -> list[JsonDict]:
    """Fixture rows -> the /portfolio/fills wire shape the parser reads."""
    return [
        {
            "fill_id": r["fill_id"],
            "created_time": r["created_time"],
            "ticker": r["ticker"],
            "count_fp": r["count_fp"],
            "no_price_dollars": r["no_price_dollars"],
            "fee_cost": r["fee_cost"],
            "is_taker": False,
            "side": "no",
        }
        for r in rows
    ]


@pytest.fixture(scope="module")
def observations(fixture: JsonDict) -> list[FeeObservation]:
    obs = fee_observations_from_fills(_rows_as_fills(fixture["charged_maker_fills"]))
    assert len(obs) == len(fixture["charged_maker_fills"]) >= 500
    return obs


@pytest.fixture(scope="module")
def whole_tape(fixture: JsonDict) -> list[JsonDict]:
    """Every exchange fill ever (4,228 wire rows): the 3,688 uncharged rows
    of the companion fixture + the 540 charged ones."""
    raw = json.loads(UNCHARGED_FIXTURE.read_text(encoding="utf-8"))
    rows = [dict(zip(raw["columns"], r, strict=True)) for r in raw["rows"]]
    assert len(rows) == raw["provenance"]["n_rows"] == 3_688
    return rows + _rows_as_fills(fixture["charged_maker_fills"])


# --------------------------------------------------------------- enum / routing


def test_live_combo_maker_string_parses_to_the_maker_branch() -> None:
    parsed = FeeType.parse("quadratic_with_combo_maker_fees")
    assert parsed is FeeType.QUADRATIC_WITH_COMBO_MAKER_FEES
    assert parsed.charges_maker
    assert FeeType.QUADRATIC_WITH_MAKER_FEES.charges_maker
    assert not FeeType.QUADRATIC.charges_maker
    model = FeeModel(FeeSchedule(taker_coef=TAKER, maker_coef=COMBO_MAKER), VERIFIED_MAKER)
    at_peak = model.fee_per_contract_cc(price_cc=CentiCents(5_000), fee_type=parsed)
    assert at_peak == math.ceil(COMBO_MAKER * Fraction(1, 4) * 10_000) == 88
    # UNKNOWN stays fail-closed: still a FeeUnknownError, never a coefficient.
    with pytest.raises(FeeUnknownError):
        model.fee_per_contract_cc(price_cc=CentiCents(5_000), fee_type=FeeType.parse("garbage"))


# --------------------------------------------------------- ground-truth fixture


def _exact_charged_cc(row: JsonDict) -> Fraction:
    return Fraction(Decimal(row["fee_cost"])) * 10_000


def test_fixture_is_the_real_post_onset_tape(fixture: JsonDict) -> None:
    rows = fixture["charged_maker_fills"]
    assert all(r["created_time"] >= "2026-08-20T09:07:00Z" for r in rows)
    assert fixture["sample_20"] == rows[:20]
    first = rows[0]
    assert (first["count_fp"], first["no_price_dollars"], first["fee_cost"]) == (
        "4.00",
        "0.5270",
        "0.034900",
    )


def test_model_at_0035_reproduces_every_charged_fee_exactly(fixture: JsonDict) -> None:
    """The exchange debits cost AND fee each rounded UP to a centi-cent and
    reports the whole excess over exact cost as fee_cost — reproduced to the
    centi-cent on every one of the fixture's charged fills at 0.035."""
    for row in fixture["charged_maker_fills"]:
        contracts = Fraction(Decimal(row["count_fp"]))
        p = Fraction(Decimal(row["no_price_dollars"]))
        fee_cc = math.ceil(COMBO_MAKER * contracts * p * (1 - p) * 10_000)
        cost_cc = contracts * p * 10_000
        residue_cc = math.ceil(cost_cc) - cost_cc
        assert fee_cc + residue_cc == _exact_charged_cc(row), row["fill_id"]


def test_model_at_0175_fails_every_fill(fixture: JsonDict) -> None:
    for row in fixture["sample_20"]:
        contracts = Fraction(Decimal(row["count_fp"]))
        p = Fraction(Decimal(row["no_price_dollars"]))
        fee_cc = math.ceil(SINGLE_MAKER * contracts * p * (1 - p) * 10_000)
        assert abs(fee_cc - _exact_charged_cc(row)) > 1, row["fill_id"]


def test_live_fee_model_reproduces_fixture_within_one_cc(
    observations: list[FeeObservation],
) -> None:
    model = FeeModel(FeeSchedule(taker_coef=TAKER, maker_coef=COMBO_MAKER), VERIFIED_MAKER)
    for obs in observations:
        fee = model.trade_fee_cc(
            price_cc=CentiCents(obs.price_cc),
            qty=CentiContracts(obs.contracts_centi),
            fee_type=FeeType.QUADRATIC_WITH_COMBO_MAKER_FEES,
        )
        assert abs(int(fee) - obs.fee_cc) <= 1


# ----------------------------------------------------------------------- fit


def test_observer_fits_0035_from_the_tape(observations: list[FeeObservation]) -> None:
    fit = fit_maker_coefficient(observations)
    assert fit is not None
    assert abs(fit - COMBO_MAKER) < COEF_QUANTUM
    assert fit == COMBO_MAKER  # pinned exactly to the publication quantum
    assert validate(observations, fit) == []
    assert len(validate(observations, SINGLE_MAKER)) == len(observations)


def test_pinning_count_is_derived_from_the_fills(observations: list[FeeObservation]) -> None:
    chrono = sorted(charged_maker(observations), key=lambda o: o.created_time)
    n = pinning_count(chrono)
    assert n is not None and 1 <= n <= 3
    # EXACTLY one quantum multiple survives the ceilings' intersection at n,
    # more than one survives one fill earlier (or nothing was charged yet).
    bounds = feasible_bounds(chrono[:n])
    assert bounds is not None and quantum_multiples(*bounds) == [COMBO_MAKER]
    if n > 1:
        before = feasible_bounds(chrono[: n - 1])
        assert before is not None and len(quantum_multiples(*before)) > 1
    # The least-squares point estimate lands on the same pin (cross-check).
    ls = least_squares_coefficient(chrono)
    assert ls is not None and quantise(ls) == COMBO_MAKER
    # A single 10-contract fill at 50c pins alone: X = 25,000 cc/unit, so the
    # interval (874/25000, 875/25000] holds exactly 0.0350 (one ceiling: the
    # parser strips the cost residue exactly, review fix M1).
    assert CEIL_SLACK_CC == 1
    one = FeeObservation("x", "2026-09-01T00:00:00Z", "C", 1_000, 5_000, 875, True)
    assert pinning_count([one]) == 1 and fit_maker_coefficient([one]) == COMBO_MAKER
    # Too small to pin alone => None (never a coefficient from too little data)
    # ... but MANY small fills pin together because their intervals interleave.
    tiny = FeeObservation("y", "2026-09-01T00:00:00Z", "C", 10, 5_000, 9, True)
    assert pinning_count([tiny]) is None
    assert fit_maker_coefficient([tiny]) is None
    smalls = _synthetic(COMBO_MAKER, day="2026-09-02", n=12, contracts_centi=120)
    assert fit_maker_coefficient([tiny]) is None
    assert pinning_count(smalls) is not None
    assert fit_maker_coefficient(smalls) == COMBO_MAKER


def _synthetic(
    coef: Fraction, *, day: str, n: int, prefix: str = "S", contracts_centi: int = 500
) -> list[FeeObservation]:
    out = []
    for i in range(n):
        contracts_centi = contracts_centi + 37 * i
        price_cc = 4_000 + 25 * i
        probe = FeeObservation(
            fill_id=f"{prefix}{i}",
            created_time=f"{day}T00:00:{i:02d}Z",
            collection_prefix="KXMVECROSSCATEGORY-SHARD1",
            contracts_centi=contracts_centi,
            price_cc=price_cc,
            fee_cc=0,
            maker=True,
        )
        out.append(
            FeeObservation(
                probe.fill_id,
                probe.created_time,
                probe.collection_prefix,
                contracts_centi,
                price_cc,
                model_fee_cc(coef, probe),
                True,
            )
        )
    return out


def test_drift_alarm_on_a_synthetic_0175_tape(observations: list[FeeObservation]) -> None:
    sched = ObservedFeeSchedule(taker_coef=TAKER)
    first = sched.ingest(observations)
    assert first.fitted == COMBO_MAKER and not first.drifted
    assert sched.maker_coef == COMBO_MAKER and sched.maker_coef_source == "observed"
    # The exchange halves the coefficient; newer fills arrive at 0.0175.
    newer = _synthetic(SINGLE_MAKER, day="2026-09-03", n=12)
    refit = sched.ingest(newer)
    assert refit.previous == COMBO_MAKER
    assert refit.fitted == SINGLE_MAKER
    assert refit.drifted  # the ERROR-log verdict; the schedule itself moved
    assert sched.maker_coef == SINGLE_MAKER
    # The old regime now shows as mismatches against the whole history, and
    # the regime window is exactly the newer fills.
    assert len(refit.mismatches) == len(observations)
    assert {o.fill_id for o in regime_window(sched.observations())} == {o.fill_id for o in newer}
    assert refit.least_squares is not None and quantise(refit.least_squares) == SINGLE_MAKER
    # Re-ingesting the same fills is a no-op (dedup by fill id, no drift).
    again = sched.ingest(newer)
    assert again.new_fills == 0 and not again.drifted


# ------------------------------------------------------------------ bootstrap


def test_bootstrap_is_the_taker_coefficient_never_zero() -> None:
    sched = ObservedFeeSchedule(taker_coef=TAKER)
    assert sched.maker_coef == TAKER and sched.maker_coef_source == "taker_fallback"
    assert sched.current() == FeeSchedule(taker_coef=TAKER, maker_coef=TAKER)
    over = ObservedFeeSchedule(taker_coef=TAKER, maker_coef_override=SINGLE_MAKER)
    assert over.maker_coef == SINGLE_MAKER and over.maker_coef_source == "override"
    # A live FeeModel holding the source sees the refit without rebuilding.
    model = FeeModel(sched, VERIFIED_MAKER)
    before = model.fee_per_contract_cc(
        price_cc=CentiCents(5_000), fee_type=FeeType.QUADRATIC_WITH_COMBO_MAKER_FEES
    )
    assert before == 175  # taker-conservative bootstrap
    sched.ingest(_synthetic(COMBO_MAKER, day="2026-09-03", n=6))
    after = model.fee_per_contract_cc(
        price_cc=CentiCents(5_000), fee_type=FeeType.QUADRATIC_WITH_COMBO_MAKER_FEES
    )
    assert after == 88
    # Uncharged maker fills alone never fit anything (no evidence of a fee).
    cold = ObservedFeeSchedule(taker_coef=TAKER)
    cold.ingest([FeeObservation("u", "2026-09-01T00:00:00Z", "C", 1_000, 5_000, 0, True)])
    assert cold.maker_coef == TAKER and cold.fitted is None


# ---------------------------------------------------------------- persistence


def test_persistence_round_trip(tmp_path: Path, observations: list[FeeObservation]) -> None:
    sched = ObservedFeeSchedule(taker_coef=TAKER)
    sched.ingest(observations)
    sched.set_collection_series("KXMVECROSSCATEGORY-SHARD1", "KXMVECROSSCATEGORY")
    sched.set_series_fee_type("KXMVECROSSCATEGORY", "quadratic_with_combo_maker_fees")
    path = tmp_path / "fee_schedule_observed.json"
    sched.save(path)
    loaded = ObservedFeeSchedule.load(path, taker_coef=TAKER)
    assert loaded.maker_coef == COMBO_MAKER and loaded.maker_coef_source == "observed"
    assert loaded.n_charged == len(observations)
    assert loaded.collections_active == {"KXMVECROSSCATEGORY-SHARD1"}
    assert loaded.series_fee_type("KXMVECROSSCATEGORY") == "quadratic_with_combo_maker_fees"
    assert loaded.last_fill_time == sched.last_fill_time
    # A hand-edited coefficient its own fills do not support is re-derived.
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["maker_coef_fitted"] = "0.0175"
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert ObservedFeeSchedule.load(path, taker_coef=TAKER).maker_coef == COMBO_MAKER
    # Missing / corrupt file => cold (taker fallback), never an error.
    missing = ObservedFeeSchedule.load(tmp_path / "missing.json", taker_coef=TAKER)
    assert missing.maker_coef == TAKER
    (tmp_path / "bad.json").write_text("[1,2", encoding="utf-8")
    assert ObservedFeeSchedule.load(tmp_path / "bad.json", taker_coef=TAKER).maker_coef == TAKER
    summary = FeeScheduleSummary.of(loaded)
    assert summary.maker_coef == "0.0350" and summary.source == "observed"


# ---------------------------------------------------------- fee-type resolution


def test_fee_type_precedence_override_observed_series_default() -> None:
    sched = ObservedFeeSchedule(taker_coef=TAKER, override_prefixes=("KXOVR",))
    q = FeeType.QUADRATIC
    combo = FeeType.QUADRATIC_WITH_COMBO_MAKER_FEES
    # 1. operator override prefix
    assert sched.fee_type_for(combo_ticker="KXOVR-X-Y", collection=None, default=q) is combo
    # 4. nothing known => configured default
    assert sched.fee_type_for(combo_ticker="KXA-1-2", collection="KXA-R", default=q) is q
    # 3. series fee type from GET /series
    sched.set_collection_series("KXA-R", "KXA")
    sched.set_series_fee_type("KXA", "quadratic_with_combo_maker_fees")
    assert sched.fee_type_for(combo_ticker="KXA-1-2", collection="KXA-R", default=q) is combo
    sched.set_series_fee_type("KXA", "quadratic")
    assert sched.fee_type_for(combo_ticker="KXA-1-2", collection="KXA-R", default=q) is q
    # an unparseable exchange string is exchange truth we cannot price => UNKNOWN
    sched.set_series_fee_type("KXA", "flat_v2_mystery")
    assert (
        sched.fee_type_for(combo_ticker="KXA-1-2", collection="KXA-R", default=q)
        is FeeType.UNKNOWN
    )
    # 2. an OBSERVED charged maker fee on the collection beats the series string
    sched.ingest([FeeObservation("c", "2026-09-01T00:00:00Z", "KXA-1", 1_000, 5_000, 88, True)])
    assert sched.fee_type_for(combo_ticker="KXA-1-2", collection="KXA-R", default=q) is combo
    # FLAT/UNKNOWN configured defaults pass through untouched (fail-closed).
    assert (
        sched.fee_type_for(combo_ticker="KXOVR-1", collection=None, default=FeeType.FLAT)
        is FeeType.FLAT
    )
    assert sched.collections_needing_series(["KXA-R", "KXB-R"]) == ["KXB-R"]


# ----------------------------------------------------------------- fill parser


def test_fill_parser_is_fail_closed() -> None:
    good: JsonDict = {
        "fill_id": "f1",
        "created_time": "2026-08-27T06:02:49.746546Z",
        "ticker": "KXMVECROSSCATEGORY-SHARD1-S2026B8AC1F3D598-966EDD669C4",
        "count_fp": "22.24",
        "no_price_dollars": "0.7870",
        "yes_price_dollars": "0.2130",
        "fee_cost": "0.130520",
        "is_taker": False,
        "side": "no",
    }
    obs = fee_observation_from_fill(good)
    assert obs is not None
    assert obs.contracts_centi == 2_224 and obs.price_cc == 7_870 and obs.maker
    # PIN CHANGED 2026-09-04 (review fix M1): the parsed fee is the reported
    # fee_cost MINUS the exchange's position-cost rounding residue (cost
    # 22.24 x 0.7870 = 175,028.8 cc -> residue 0.2 cc), so 1305.2 - 0.2 =
    # 1305 = ceil(0.035 x 22.24 x 0.787 x 0.213 x 10^4) exactly - no longer
    # the ceiling of the raw fee_cost (1306).
    assert cost_residue_cc(2_224, 7_870) == Fraction(1, 5)
    exact = COMBO_MAKER * Fraction(2_224, 100) * Fraction(7_870, 10_000) * Fraction(2_130, 10_000)
    assert obs.fee_cc == 1_305 == math.ceil(exact * 10_000)
    assert obs.collection_prefix == "KXMVECROSSCATEGORY-SHARD1"
    assert collection_prefix_of("KXA-B") == "KXA-B"
    for missing in ("fill_id", "created_time", "ticker", "is_taker"):
        bad = dict(good)
        bad.pop(missing)
        assert fee_observation_from_fill(bad) is None, missing
    # order_id is NOT a fill id (partials of one order share it): fail closed.
    assert fee_observation_from_fill({**good, "fill_id": None, "order_id": "o1"}) is None
    trade = fee_observation_from_fill({**good, "fill_id": None, "trade_id": "t1"})
    assert trade is not None and trade.fill_id == "t1"
    assert fee_observation_from_fill({**good, "is_taker": "false"}) is None
    assert fee_observation_from_fill({**good, "count_fp": "0.00"}) is None
    assert fee_observation_from_fill({**good, "no_price_dollars": "1.5"}) is None
    taker = fee_observation_from_fill({**good, "is_taker": True})
    assert taker is not None and not taker.maker
    assert fee_observations_from_fills([good, "junk", {}]) == [obs]  # type: ignore[list-item]


# ------------------------------------------------ the residue is not a fee (M1)


def test_pre_onset_residue_fill_is_not_charged() -> None:
    """A REAL maker fill from 2026-08-20T09:04:50Z - two minutes before the
    onset - reports fee_cost $0.000040 = 0.4 cc. That is exactly the cost
    residue ceil_cc(9.08 x 0.5770) - 9.08 x 0.5770 = 0.4 cc: no maker fee
    was charged. The old parser ceiled it to 1 cc and called it charged."""
    row: JsonDict = {
        "fill_id": "072067c3-9611-9f75-c866-b98976191eda",
        "created_time": "2026-08-20T09:04:50.585879Z",
        "ticker": "KXMVECROSSCATEGORY-SHARD1-S2026FC7CD963ECA-668D16CEADE",
        "count_fp": "9.08",
        "no_price_dollars": "0.5770",
        "yes_price_dollars": "0.4230",
        "fee_cost": "0.000040",
        "is_taker": False,
        "side": "no",
    }
    assert cost_residue_cc(908, 5_770) == Fraction(2, 5)
    assert charged_fee_cc("0.000040", contracts_centi=908, price_cc=5_770) == 0
    obs = fee_observation_from_fill(row)
    assert obs is not None and obs.maker and obs.fee_cc == 0 and not obs.charged
    # A residue-only row never marks its collection as maker-fee-observed.
    sched = ObservedFeeSchedule(taker_coef=TAKER)
    sched.ingest([obs])
    assert sched.collections_active == frozenset() and sched.fitted is None


def test_whole_history_ingest_is_silent(whole_tape: list[JsonDict]) -> None:
    """The defense-#3 alarm must be SILENT on the real history: every one of
    the 4,228 fills parses, exactly the 540 post-onset fills are charged,
    the fit is 0.0350 with ZERO mismatches over the whole tape, and only the
    sharded collection is marked (the unsharded KXMVECROSSCATEGORY and
    KXMVESPORTSMULTIGAMEEXTENDED rows carried residue only)."""
    obs = fee_observations_from_fills(whole_tape)
    assert len(obs) == len(whole_tape) == 4_228
    charged = charged_maker(obs)
    assert len(charged) == 540
    assert all(o.created_time >= "2026-08-20T09:07" for o in charged)
    assert all(not o.charged for o in obs if o.maker and o.created_time < "2026-08-20T09:07")
    assert sum(1 for o in obs if not o.maker) == 106  # takers pay taker fees, out of scope
    assert {o.collection_prefix for o in obs} == {
        "KXMVECROSSCATEGORY", "KXMVECROSSCATEGORY-SHARD1", "KXMVESPORTSMULTIGAMEEXTENDED",
    }
    sched = ObservedFeeSchedule(taker_coef=TAKER)
    refit = sched.ingest(obs)
    assert refit.fitted == COMBO_MAKER and not refit.drifted
    assert refit.mismatches == () and sched.mismatches == ()
    assert sched.n_fills == 4_228 and sched.n_charged == 540
    assert sched.collections_active == {"KXMVECROSSCATEGORY-SHARD1"}
    assert not sched.observed_active("KXMVECROSSCATEGORY-S1-M", "KXMVECROSSCATEGORY-R")
    # Persisting keeps only the 540 charged rows (the residue rows are not
    # observations of a fee).
    assert len(sched.to_json()["observations"]) == 540
    assert sched.to_json()["version"] == SCHEMA_VERSION == 2
    # Ingesting the same tape again is a no-op.
    again = sched.ingest(obs)
    assert again.new_fills == 0 and not again.drifted and again.mismatches == ()


def test_schema_v1_persisted_file_self_heals(
    tmp_path: Path, observations: list[FeeObservation]
) -> None:
    """A file written by the pre-M1 parser carries residue rows at fee_cc=1
    and every charged row with residue > 0 one cc high (ceil(fee + r) =
    fee + 1). Loading it undoes the indicator exactly, drops the residue rows
    and re-derives the observed collections - no manual file deletion."""
    v1_obs: list[JsonDict] = []
    n_high = 0
    for o in observations:
        r = cost_residue_cc(o.contracts_centi, o.price_cc)
        bump = 1 if r > 0 else 0
        n_high += bump
        v1_obs.append({**o.to_json(), "fee_cc": o.fee_cc + bump})
    assert 0 < n_high < len(observations)  # both residue classes are present
    # Residue-only rows on two OTHER collections (fee_cc = 1 under the old
    # parser) - the real pre-onset shape.
    for i, coll in enumerate(("KXMVECROSSCATEGORY", "KXMVESPORTSMULTIGAMEEXTENDED")):
        v1_obs.append({
            "fill_id": f"residue{i}", "created_time": "2026-08-19T00:00:00Z",
            "collection_prefix": coll, "contracts_centi": 908, "price_cc": 5_770,
            "fee_cc": 1, "maker": True,
        })
    raw = {
        "version": 1,
        "taker_coef": "0.07",
        "maker_coef_fitted": "0.0350",
        "n_fills": len(v1_obs),
        "n_charged": len(v1_obs),
        "last_fill_time": max(o["created_time"] for o in v1_obs),
        "collections_active": [
            "KXMVECROSSCATEGORY", "KXMVECROSSCATEGORY-SHARD1", "KXMVESPORTSMULTIGAMEEXTENDED",
        ],
        "series_fee_types": {},
        "collection_series": {},
        "observations": v1_obs,
    }
    path = tmp_path / "fee_schedule_observed.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    healed = ObservedFeeSchedule.load(path, taker_coef=TAKER)
    assert healed.n_charged == len(observations)
    assert healed.collections_active == {"KXMVECROSSCATEGORY-SHARD1"}
    assert healed.maker_coef == COMBO_MAKER and healed.mismatches == ()
    by_id = {o.fill_id: o for o in healed.observations()}
    for o in observations:
        assert by_id[o.fill_id].fee_cc == o.fee_cc
    # A v2 file round-trips untouched (no double heal).
    healed.save(path)
    again = ObservedFeeSchedule.load(path, taker_coef=TAKER)
    assert [o.fee_cc for o in again.observations()] == [o.fee_cc for o in healed.observations()]
    assert again.mismatches == () and again.collections_active == {"KXMVECROSSCATEGORY-SHARD1"}
