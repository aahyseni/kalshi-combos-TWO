"""The fee seam WIRED (2026-09-04 build A, items 2-3): one measured schedule
shared by the pricing engine and the lifecycle ledger/waiver, refit in place
by the lifecycle's fee-observer sweep from /portfolio/fills, persisted under
data_dir, series fee_type resolved on the slow loop, drift alarmed — and the
per-combo fee type the ENGINE prices with equals the one the LEDGER books.

Rig: the real QuoteLifecycle + PricingEngine over the test harness, a fake
paginated fills getter serving the REAL charged-fill fixture, and a fake
series getter. Nothing here touches the pricing hot path except through the
public schedule object the quote path reads.
"""

from __future__ import annotations

import json
import math
from fractions import Fraction
from pathlib import Path
from typing import Any

from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.ops.config import FeeConfig, FiltersConfig, PricingConfig
from combomaker.ops.fee_schedule import fee_schedule_path, load_observed_fee_schedule
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.fee_observer import ObservedFeeSchedule
from combomaker.pricing.fees import FeeModel, FeeType
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.lifecycle import LifecycleConfig, QuoteLifecycle
from combomaker.risk.exposure import ExposureBook
from combomaker.risk.inplay import InPlayDetector
from combomaker.risk.lastlook import LastLookPolicy
from combomaker.risk.limits import LimitChecker, RiskLimits
from tests.test_filters import Harness
from tests.test_lifecycle import TEST_CONVENTIONS, FakeSender, rfq
from tests.test_pricing_engine import seed_event

JsonDict = dict[str, Any]
FIXTURE = Path(__file__).parent / "fixtures" / "ground_truth" / "maker_fee_20260820.json"
COMBO_MAKER = Fraction(35, 1000)
TAKER = Fraction(7, 100)


def _fixture_rows() -> list[JsonDict]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [
        {
            "fill_id": r["fill_id"],
            "order_id": r["fill_id"],
            "created_time": r["created_time"],
            "ticker": r["ticker"],
            "count_fp": r["count_fp"],
            "no_price_dollars": r["no_price_dollars"],
            "fee_cost": r["fee_cost"],
            "is_taker": False,
            "side": "no",
        }
        for r in raw["charged_maker_fills"]
    ]


class FakeFills:
    """Paginated /portfolio/fills: page i served at cursor str(i)."""

    def __init__(self, pages: list[list[JsonDict]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, str | int]] = []

    async def get_fills(self, **params: str | int) -> JsonDict:
        self.calls.append(dict(params))
        cursor = str(params.get("cursor") or "")
        i = int(cursor) if cursor else 0
        page = self.pages[i] if i < len(self.pages) else []
        nxt = str(i + 1) if i + 1 < len(self.pages) else ""
        return {"fills": page, "cursor": nxt}


# docs/api-notes/multivariate.md §2: GET /multivariate_event_collections/
# {collection_ticker} -> GetMultivariateEventCollectionResponse, field
# ``multivariate_contract`` (singular) = one MultivariateEventCollection,
# whose ``series_ticker`` is a REQUIRED field (§1 table). This is the exact
# documented shape the resolver reads (review fix S4 — closes the build
# report's O2).
def documented_collection_payload(ticker: str, series: str = "KXMVESERIES") -> JsonDict:
    return {
        "multivariate_contract": {
            "ticker": ticker,
            "series_ticker": series,
            "title": "Cross-category combos",
            "collection_type": "combined",
            "associated_events": [],
        }
    }


class FakeSeries:
    def __init__(self, fee_type: str = "quadratic_with_combo_maker_fees") -> None:
        self.fee_type = fee_type
        self.collection_calls: list[str] = []
        self.series_calls: list[str] = []

    async def get_multivariate_collection(self, ticker: str) -> JsonDict:
        self.collection_calls.append(ticker)
        return documented_collection_payload(ticker)

    async def get_series(self, ticker: str) -> JsonDict:
        self.series_calls.append(ticker)
        return {"series": {"ticker": ticker, "fee_type": self.fee_type, "fee_multiplier": 1}}


class Rig:
    def __init__(
        self,
        h: Harness,
        store: Store,
        *,
        sched: ObservedFeeSchedule,
        path: Path,
        fills: FakeFills | None,
        series: FakeSeries | None,
    ) -> None:
        self.h = h
        self.store = store
        self.sender = FakeSender()
        self.metrics = Metrics()
        self.sched = sched
        self.engine = PricingEngine(
            h.feed, h.metadata, TEST_CONVENTIONS, PricingConfig(), fee_schedule=sched
        )
        self.lifecycle = QuoteLifecycle(
            clock=h.clock,
            sender=self.sender,
            engine=self.engine,
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
            metrics=self.metrics,
            lastlook_policy=LastLookPolicy(),
            config=LifecycleConfig(),
            fee_model=FeeModel(sched, TEST_CONVENTIONS),
            fee_type=FeeType.QUADRATIC,
            fee_schedule=sched,
            fee_schedule_path=path,
            fills_getter=fills,
            series_getter=series,
        )


async def _rig(
    tmp_path: Path,
    *,
    fills: FakeFills | None,
    series: FakeSeries | None,
    fee_cfg: FeeConfig | None = None,
) -> Rig:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / "wiring.sqlite3", h.clock)
    sched = load_observed_fee_schedule(fee_cfg or FeeConfig(), tmp_path)
    return Rig(h, store, sched=sched, path=fee_schedule_path(tmp_path), fills=fills, series=series)


async def _tick(rig: Rig) -> None:
    await rig.lifecycle.maintenance_tick()
    await rig.lifecycle.drain_diagnostic_sweeps()


# ------------------------------------------------------------------ the sweep


async def test_first_tick_measures_persists_and_resolves_series(tmp_path: Path) -> None:
    rows = _fixture_rows()
    fills = FakeFills([rows[:300], rows[300:]])
    series = FakeSeries()
    rig = await _rig(tmp_path, fills=fills, series=series)
    sched = rig.sched
    # Cold: taker-conservative bootstrap, nothing observed, no file.
    assert sched.maker_coef == TAKER and sched.maker_coef_source == "taker_fallback"
    assert not fee_schedule_path(tmp_path).exists()
    await rig.lifecycle.handle_rfq(rfq())  # registers the KXMVESPORTS collection
    await _tick(rig)
    # The FIRST tick swept: both pages (one history walk), fit pinned 0.0350.
    assert len(fills.calls) == 2 and "min_ts" not in fills.calls[0]
    assert sched.maker_coef == COMBO_MAKER and sched.maker_coef_source == "observed"
    assert sched.n_charged == len(rows) and sched.mismatches == ()
    assert sched.collections_active == {"KXMVECROSSCATEGORY-SHARD1"}
    # Series fee_type resolved for the quoted collection (two public GETs).
    assert series.collection_calls == ["KXMVESPORTS"] and series.series_calls == ["KXMVESERIES"]
    assert sched.series_fee_type("KXMVESERIES") == "quadratic_with_combo_maker_fees"
    # Persisted (survives relights): a fresh load prices with the measurement.
    assert fee_schedule_path(tmp_path).exists()
    reloaded = load_observed_fee_schedule(FeeConfig(), tmp_path)
    assert reloaded.maker_coef == COMBO_MAKER and reloaded.series_fee_type("KXMVESERIES")
    assert rig.metrics.counter("fee_observer.persisted") == 1
    assert rig.metrics.counter("fee_schedule.drift") == 0
    # Inside the cadence: no second poll.
    await _tick(rig)
    assert len(fills.calls) == 2


async def test_ledger_and_engine_agree_on_the_per_combo_fee_type(tmp_path: Path) -> None:
    rows = _fixture_rows()
    rig = await _rig(tmp_path, fills=FakeFills([rows]), series=FakeSeries())
    await rig.lifecycle.handle_rfq(rfq())
    await _tick(rig)
    lc = rig.lifecycle
    # Observed collection => COMBO maker fee type, in both the engine and the ledger.
    observed = ("KXMVECROSSCATEGORY-SHARD1-S2026F9363F0CB32-83DDBAEDB61",
                "KXMVECROSSCATEGORY-SHARD1-R")
    assert lc._effective_fee_type(*observed) is FeeType.QUADRATIC_WITH_COMBO_MAKER_FEES  # noqa: SLF001
    assert lc._maker_fee_active(*observed)  # noqa: SLF001
    # The UNSHARDED collection is different exchange truth: NOT observed.
    assert not rig.sched.observed_active("KXMVECROSSCATEGORY-S1-M", "KXMVECROSSCATEGORY-R")
    # The quoted test collection resolves through its SERIES fee_type.
    test_rfq = rfq()
    assert rig.engine.fee_type_for(test_rfq) is FeeType.QUADRATIC_WITH_COMBO_MAKER_FEES
    assert lc._effective_fee_type(test_rfq.market_ticker, test_rfq.mve_collection_ticker) is (  # noqa: SLF001
        rig.engine.fee_type_for(test_rfq)
    )
    # And the fee the ledger would book on it is the MEASURED 0.035 fee.
    fee = lc._fill_fee_cc(  # noqa: SLF001
        CentiCents(5_000), CentiContracts(1_000),
        combo_ticker=test_rfq.market_ticker, collection=test_rfq.mve_collection_ticker,
    )
    assert fee == 875  # ceil(0.035 x 10 x 0.25 x 10^4)
    # An unknown collection with no series and no observation: the default.
    assert lc._effective_fee_type("KXOTHER-1-2", "KXOTHER-R") is FeeType.QUADRATIC  # noqa: SLF001


async def test_drift_alarm_and_warm_new_fills_only_poll(tmp_path: Path) -> None:
    rows = _fixture_rows()
    fills = FakeFills([rows])
    rig = await _rig(tmp_path, fills=fills, series=None)
    await _tick(rig)
    assert rig.sched.maker_coef == COMBO_MAKER
    # A later regime at 0.0175 arrives; the next sweep is one interval away.
    newer: list[JsonDict] = []
    for i in range(12):
        contracts, price_cc = 5 + i, 4_000 + 25 * i
        p = Fraction(price_cc, 10_000)
        base_cc = contracts * p * (1 - p) * 10_000
        fee_cc = math.ceil(Fraction(175, 10_000) * base_cc)  # the exchange's own ceil
        newer.append({
            "fill_id": f"drift{i}", "order_id": f"o-drift{i}",
            "created_time": f"2026-09-03T00:00:{i:02d}Z",
            "ticker": "KXMVECROSSCATEGORY-SHARD1-S2026DRIFT-M", "count_fp": f"{contracts}.00",
            "no_price_dollars": f"{price_cc / 10_000:.4f}", "fee_cost": f"{fee_cc / 10_000:.6f}",
            "is_taker": False, "side": "no",
        })
    fills.pages = [newer]
    rig.h.clock.advance(LifecycleConfig().fills_ledger_sweep_interval_s + 1.0)
    await _tick(rig)
    # Warm sweep: NEW fills only (min_ts = the watermark), drift alarmed, refit.
    assert fills.calls[-1].get("min_ts") is not None
    assert rig.metrics.counter("fee_schedule.drift") == 1
    assert rig.sched.maker_coef == Fraction(175, 10_000)
    assert len(rig.sched.mismatches) == len(rows)  # the old regime, on history
    assert rig.lifecycle.open_quote_count == 0  # nothing halted, nothing refused


async def test_no_getter_means_no_sweep_and_legacy_resolution(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, fills=None, series=None)
    await _tick(rig)
    assert rig.metrics.counter("fee_observer.ran") == 0
    assert rig.sched.maker_coef == TAKER
    # Operator override prefix still forces the maker type (logged override).
    cfg = FeeConfig(maker_fee_active_prefixes=("KXOVR",), maker_coef_override="0.0175")
    sched = load_observed_fee_schedule(cfg, tmp_path)
    assert sched.maker_coef == Fraction(175, 10_000) and sched.maker_coef_source == "override"
    forced = sched.fee_type_for(
        combo_ticker="KXOVR-1-2", collection=None, default=FeeType.QUADRATIC
    )
    assert forced is FeeType.QUADRATIC_WITH_COMBO_MAKER_FEES


def test_series_ticker_is_read_from_the_documented_collection_shape() -> None:
    """docs/api-notes/multivariate.md §1-2: the single-collection response
    wraps one MultivariateEventCollection under ``multivariate_contract``
    and ``series_ticker`` is required on it. The resolver reads exactly
    that; a payload without it fails closed (None -> observed/default)."""
    from combomaker.rfq.lifecycle import _series_ticker_from_collection

    assert _series_ticker_from_collection(documented_collection_payload("KXMVESPORTS")) == (
        "KXMVESERIES"
    )
    # A bare object (the §1 list element shape) is read too.
    assert _series_ticker_from_collection(
        documented_collection_payload("KXMVESPORTS")["multivariate_contract"]
    ) == "KXMVESERIES"
    # Fail closed: missing / empty / wrong-typed series ticker, or no dict.
    assert _series_ticker_from_collection({"multivariate_contract": {"ticker": "X"}}) is None
    assert _series_ticker_from_collection({"multivariate_contract": {"series_ticker": ""}}) is None
    assert _series_ticker_from_collection({"series_ticker": 7}) is None
    assert _series_ticker_from_collection(["KXMVESERIES"]) is None
    assert _series_ticker_from_collection(None) is None


def test_fee_config_has_no_bare_maker_number() -> None:
    cfg = FeeConfig()
    assert not hasattr(cfg, "maker_coef")
    assert cfg.maker_coef_override is None and cfg.mode == "floor"
    assert FeeConfig(maker_coef_override="0.035").maker_coef_override == "0.035"
    for bad in ("abc", "-1", "2"):
        try:
            FeeConfig(maker_coef_override=bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} accepted")
