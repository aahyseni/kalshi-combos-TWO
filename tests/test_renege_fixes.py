"""RENEGE FIXES (2026-07-25 big-fill audit: 49 auctions won, 15 filled, $355
premium won-then-declined in one evening — the missing $20-50 combos).

Three flag-gated mechanism repairs, all default OFF (byte-identical pinned):

1. AWARD SIZING (root cause #1): quote-time risk sized target-cost RFQs at
   OUR cheapest bid (the NO bid ~80c on longshots) while the exchange awards
   at the TAKER's price (~$1 − bid) — a 3.6–4.7× understatement that let
   quote-time caps pass a phantom-small candidate the confirm layer then
   correctly declined at true size. Fixed: contracts = target / ($1 − bid),
   a strict fee-free UPPER bound on the award.
2. GATE EV SOURCE (root cause #2): the admission gate scored same-game
   combos with the band-high risk copula, structurally negative where the
   calibrated pricing fair (the number that PRICED the quote) said +EV —
   won auctions reneged as negative_ev_not_risk_reducing. Fixed: admission
   EV sign check can use the pricing edge; tail budgets keep the risk models.
3. WAIVER GAME-SCOPED STABILITY (peak-flow gap): the waiver's stability key
   used GLOBAL generation/version counters, so any concurrent fill anywhere
   invalidated certificates for unrelated games ("book moved during every
   enumeration"). Fixed: compare the breached games' position/reservation
   CONTENT — same-game changes still invalidate, unrelated ones don't.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.ops.config import FiltersConfig, PricingConfig
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.quote import ConstructedQuote
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.lifecycle import LifecycleConfig, QuoteLifecycle
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.inplay import InPlayDetector
from combomaker.risk.lastlook import LastLookPolicy
from combomaker.risk.limits import LimitChecker, RiskLimits
from combomaker.sim.book_risk import evaluate_candidate_book_risk
from tests.test_filters import Harness
from tests.test_lifecycle import TEST_CONVENTIONS, FakeSender
from tests.test_pricing_engine import combo

TARGET_50 = "50.00"  # tonight's reneged archetype: $50 target-cost longshot


async def _lifecycle(tmp_path: Path, config: LifecycleConfig) -> QuoteLifecycle:
    h = Harness()
    store = await Store.open(tmp_path / "t.sqlite3", h.clock)
    engine = PricingEngine(h.feed, h.metadata, TEST_CONVENTIONS, PricingConfig())
    rfq_filter = RfqFilter(
        FiltersConfig(min_time_to_close_s=0.0).model_copy(
            update={"allowed_leg_series_prefixes": None}
        ),
        h.feed, h.metadata, h.killswitch, h.clock,
    )
    return QuoteLifecycle(
        clock=h.clock,
        sender=FakeSender(),
        engine=engine,
        rfq_filter=rfq_filter,
        limits=LimitChecker(RiskLimits()),
        exposure=ExposureBook(TEST_CONVENTIONS),
        feed=h.feed,
        metadata=h.metadata,
        inplay=InPlayDetector(h.clock),
        killswitch=h.killswitch,
        conventions=TEST_CONVENTIONS,
        store=store,
        metrics=Metrics(),
        lastlook_policy=LastLookPolicy(),
        config=config,
        start_time_provider=rfq_filter.leg_start_time,
    )


def _constructed(no_bid_cc: int) -> ConstructedQuote:
    return ConstructedQuote(
        yes_bid_cc=CentiCents(0),  # sell-only: YES declined
        no_bid_cc=CentiCents(no_bid_cc),
        fair_cc=CentiCents(10_000 - no_bid_cc),
        width_components_cc={},
    )


class TestAwardSizing:
    async def test_legacy_understates_award_sizing_bounds_it(
        self, tmp_path: Path
    ) -> None:
        # Tonight's exact reneged fill: $50 target at no_bid 0.8210. Exchange
        # awarded 264.13 contracts; legacy risk saw 60.91 (4.3x small); award
        # sizing sees 279.33 — a strict conservative ceiling on the award.
        rfq = combo(
            [{"market_ticker": "A", "side": "yes", "event_ticker": "KX-G1"}],
            id="r1", market_ticker="KXMVE-1",
            contracts_fp=None, target_cost_dollars=TARGET_50,
        )
        legacy = await _lifecycle(
            tmp_path, LifecycleConfig(risk_qty_award_sizing=False)
        )
        armed = await _lifecycle(
            tmp_path / "b", LifecycleConfig(risk_qty_award_sizing=True)
        )
        q_legacy = legacy._risk_qty(rfq, _constructed(8_210))
        q_armed = armed._risk_qty(rfq, _constructed(8_210))
        assert q_legacy is not None and q_armed is not None
        assert int(q_legacy) == -(-500_000 * 100 // 8_210)   # 6_091cc = 60.91
        assert int(q_armed) == -(-500_000 * 100 // 1_790)    # 27_933cc = 279.33
        # The award-true size DOMINATES the exchange's actual award (264.13).
        assert int(q_armed) >= 26_413
        # Fee-free ceiling: within ~6% above the actual award, never below.
        assert int(q_armed) <= int(26_413 * 1.06)

    async def test_favorites_unchanged_direction(self, tmp_path: Path) -> None:
        # A favorite (no_bid 0.30 → taker pays 0.70): award sizing yields
        # FEWER contracts than legacy — legacy overstated favorites, award
        # sizing is exact-side truth in both directions.
        rfq = combo(
            [{"market_ticker": "A", "side": "yes", "event_ticker": "KX-G1"}],
            id="r2", market_ticker="KXMVE-2",
            contracts_fp=None, target_cost_dollars="20.00",
        )
        legacy = await _lifecycle(
            tmp_path, LifecycleConfig(risk_qty_award_sizing=False)
        )
        armed = await _lifecycle(
            tmp_path / "b", LifecycleConfig(risk_qty_award_sizing=True)
        )
        assert int(legacy._risk_qty(rfq, _constructed(3_000)))  # type: ignore[arg-type]
        q_legacy = int(legacy._risk_qty(rfq, _constructed(3_000)))  # type: ignore[arg-type]
        q_armed = int(armed._risk_qty(rfq, _constructed(3_000)))  # type: ignore[arg-type]
        assert q_legacy == -(-200_000 * 100 // 3_000)  # 6_667 = 66.67 contracts
        assert q_armed == -(-200_000 * 100 // 7_000)   # 2_858 = 28.58 contracts
        assert q_armed < q_legacy

    async def test_contracts_mode_untouched(self, tmp_path: Path) -> None:
        rfq = combo(
            [{"market_ticker": "A", "side": "yes", "event_ticker": "KX-G1"}],
            id="r3", market_ticker="KXMVE-3", contracts_fp="10.00",
        )
        armed = await _lifecycle(
            tmp_path, LifecycleConfig(risk_qty_award_sizing=True)
        )
        assert int(armed._risk_qty(rfq, _constructed(8_210))) == 1_000  # type: ignore[arg-type]


def _pos(pid: str, ticker: str, event: str, *, price_cc: int) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=f"COMBO-{pid}",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(100),
        entry_price_cc=CentiCents(price_cc),
        legs=(LegRef(ticker, event, "yes"),),
    )


class TestGateEvSource:
    # A NO bought at 0.60 on a coin-flip parlay is worth 0.50 → MC EV ≈ −10c/
    # contract. The pricing fair (the calibrated moat) may still say the fill
    # is +EV — the flag makes THAT the admission judgment.

    def _run(self, **kw):
        committed = [_pos("c1", "A", "KX-G1", price_cc=4_000)]
        cand = _pos("cand", "B", "KX-G2", price_cc=6_000)
        return evaluate_candidate_book_risk(
            committed, cand, marginals=lambda t: 0.5,
            n_samples=20_000, seed=3, **kw,
        )

    def test_flag_off_mc_ev_declines(self) -> None:
        r = self._run()
        assert not r.confirm
        assert r.decline_reason == "negative_ev_no_hedge_budget"

    def test_flag_on_pricing_edge_admits(self) -> None:
        r = self._run(gate_ev_from_pricing_fair=True, pricing_edge_cc=500.0)
        assert r.confirm, r.decline_reason

    def test_flag_on_none_edge_falls_back_to_mc(self) -> None:
        r = self._run(gate_ev_from_pricing_fair=True, pricing_edge_cc=None)
        assert not r.confirm
        assert r.decline_reason == "negative_ev_no_hedge_budget"

    def test_flag_on_negative_edge_still_declines(self) -> None:
        r = self._run(gate_ev_from_pricing_fair=True, pricing_edge_cc=-500.0)
        assert not r.confirm
        assert r.decline_reason == "negative_ev_no_hedge_budget"


class TestSlotEvEviction:
    """EV-BASED SLOT EVICTION (2026-07-25 big-fill audit): at the slot cap
    the weakest stored-EV resting quote is evicted for a strictly-higher-EV
    candidate; any other enforced wall stands untouched."""

    def _resting(self, qid: str, edge_cc: int | None):
        from combomaker.core.money import CentiCents as CC
        from combomaker.risk.exposure import OpenQuoteRisk

        return OpenQuoteRisk(
            quote_id=qid, rfq_id=f"r-{qid}", combo_ticker=f"C-{qid}",
            collection=None, yes_bid_cc=CC(0), no_bid_cc=CC(5_000),
            contracts=CentiContracts(100),
            legs=(LegRef(f"L-{qid}", f"KX-G-{qid}", "yes"),),
            expected_edge_cc=edge_cc,
        )

    async def _rig(self, tmp_path: Path, *, cap: int = 2):
        from fractions import Fraction

        lc = await _lifecycle(
            tmp_path, LifecycleConfig(open_quote_ev_eviction=True)
        )
        lc._limits = LimitChecker(  # noqa: SLF001 — test rig wiring
            RiskLimits(
                max_open_quotes=cap,
                caps_shadow_mode=True,
                per_combo_loss_frac=Fraction(99, 100),
            )
        )
        lc._marginals = lambda t: 0.5  # noqa: SLF001 — resolvable deltas
        return lc

    def _cand(self, no_bid_cc: int, fair_cc: int) -> ConstructedQuote:
        return ConstructedQuote(
            yes_bid_cc=CentiCents(0),
            no_bid_cc=CentiCents(no_bid_cc),
            fair_cc=CentiCents(fair_cc),
            width_components_cc={},
        )

    async def test_evicts_weakest_and_admits(self, tmp_path: Path) -> None:
        from combomaker.core.reasons import ReasonCode
        from combomaker.risk.limits import Breach

        lc = await self._rig(tmp_path)
        lc._exposure.upsert_quote(self._resting("weak", 100))  # noqa: SLF001
        lc._exposure.upsert_quote(self._resting("strong", 9_000))  # noqa: SLF001
        rfq = combo(
            [{"market_ticker": "A", "side": "yes", "event_ticker": "KX-G1"}],
            id="rc", market_ticker="KXMVE-C", contracts_fp="1.00",
        )
        # NO at 4_000 on a fair of 3_000 ⇒ NO-side fair = 7_000 ⇒ edge
        # (7_000 − 4_000) × 1 contract = 3_000cc > weakest 100cc.
        result = self._cand(no_bid_cc=4_000, fair_cc=3_000)
        qrisk = lc._quote_risk(  # noqa: SLF001
            rfq, result, quote_id="pending", qty=CentiContracts(100)
        )
        breaches = [
            Breach(ReasonCode.SKIP_MAX_OPEN_QUOTES, "2 at cap 2", shadow=False)
        ]
        out = await lc._try_slot_eviction(  # noqa: SLF001
            rfq, result, CentiContracts(100), qrisk, breaches
        )
        assert "weak" not in lc._exposure.open_quotes  # noqa: SLF001
        assert "strong" in lc._exposure.open_quotes  # noqa: SLF001
        assert not [b for b in out if not b.shadow]  # slot breach cleared

    async def test_lower_ev_candidate_does_not_evict(
        self, tmp_path: Path
    ) -> None:
        from combomaker.core.reasons import ReasonCode
        from combomaker.risk.limits import Breach

        lc = await self._rig(tmp_path)
        lc._exposure.upsert_quote(self._resting("weak", 5_000))  # noqa: SLF001
        rfq = combo(
            [{"market_ticker": "A", "side": "yes", "event_ticker": "KX-G1"}],
            id="rc2", market_ticker="KXMVE-C2", contracts_fp="1.00",
        )
        result = self._cand(no_bid_cc=6_900, fair_cc=3_000)  # edge 100cc
        qrisk = lc._quote_risk(  # noqa: SLF001
            rfq, result, quote_id="pending", qty=CentiContracts(100)
        )
        breaches = [
            Breach(ReasonCode.SKIP_MAX_OPEN_QUOTES, "at cap", shadow=False)
        ]
        out = await lc._try_slot_eviction(  # noqa: SLF001
            rfq, result, CentiContracts(100), qrisk, breaches
        )
        assert out is breaches  # unchanged — no eviction
        assert "weak" in lc._exposure.open_quotes  # noqa: SLF001

    async def test_other_enforced_breach_untouched(
        self, tmp_path: Path
    ) -> None:
        from combomaker.core.reasons import ReasonCode
        from combomaker.risk.limits import Breach

        lc = await self._rig(tmp_path)
        lc._exposure.upsert_quote(self._resting("weak", 100))  # noqa: SLF001
        rfq = combo(
            [{"market_ticker": "A", "side": "yes", "event_ticker": "KX-G1"}],
            id="rc3", market_ticker="KXMVE-C3", contracts_fp="1.00",
        )
        result = self._cand(no_bid_cc=4_000, fair_cc=3_000)
        qrisk = lc._quote_risk(  # noqa: SLF001
            rfq, result, quote_id="pending", qty=CentiContracts(100)
        )
        breaches = [
            Breach(ReasonCode.SKIP_MAX_OPEN_QUOTES, "at cap", shadow=False),
            Breach(ReasonCode.SKIP_PER_COMBO_LOSS_CAP, "loss", shadow=False),
        ]
        out = await lc._try_slot_eviction(  # noqa: SLF001
            rfq, result, CentiContracts(100), qrisk, breaches
        )
        assert out is breaches  # a real risk wall present — never loosened
        assert "weak" in lc._exposure.open_quotes  # noqa: SLF001

    async def test_unknown_ev_never_evicted(self, tmp_path: Path) -> None:
        from combomaker.core.reasons import ReasonCode
        from combomaker.risk.limits import Breach

        lc = await self._rig(tmp_path)
        lc._exposure.upsert_quote(self._resting("mystery", None))  # noqa: SLF001
        rfq = combo(
            [{"market_ticker": "A", "side": "yes", "event_ticker": "KX-G1"}],
            id="rc4", market_ticker="KXMVE-C4", contracts_fp="1.00",
        )
        result = self._cand(no_bid_cc=4_000, fair_cc=3_000)
        qrisk = lc._quote_risk(  # noqa: SLF001
            rfq, result, quote_id="pending", qty=CentiContracts(100)
        )
        breaches = [
            Breach(ReasonCode.SKIP_MAX_OPEN_QUOTES, "at cap", shadow=False)
        ]
        out = await lc._try_slot_eviction(  # noqa: SLF001
            rfq, result, CentiContracts(100), qrisk, breaches
        )
        assert out is breaches
        assert "mystery" in lc._exposure.open_quotes  # noqa: SLF001


class TestWaiverScopedStability:
    async def test_unrelated_fill_no_longer_invalidates(
        self, tmp_path: Path
    ) -> None:
        scoped = await _lifecycle(
            tmp_path, LifecycleConfig(waiver_game_scoped_stability=True)
        )
        fp0 = scoped._waiver_games_fingerprint(["G1"])
        # A fill on an UNRELATED game (the peak-flow churn source): the
        # scoped fingerprint is unchanged — the certificate survives.
        scoped._exposure.add_position(_pos("other", "Z", "KX-G9", price_cc=5_000))
        assert scoped._waiver_games_fingerprint(["G1"]) == fp0
        # A SAME-game fill still invalidates (the 2026-07-16 findings hold).
        scoped._exposure.add_position(_pos("same", "A", "KX-G1", price_cc=5_000))
        assert scoped._waiver_games_fingerprint(["G1"]) != fp0

    async def test_legacy_global_counters_invalidate_on_any_fill(
        self, tmp_path: Path
    ) -> None:
        legacy = await _lifecycle(
            tmp_path, LifecycleConfig(waiver_game_scoped_stability=False)
        )
        fp0 = legacy._waiver_games_fingerprint(["G1"])
        legacy._exposure.add_position(_pos("other", "Z", "KX-G9", price_cc=5_000))
        assert legacy._waiver_games_fingerprint(["G1"]) != fp0  # byte-identical old behavior
