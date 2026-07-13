"""Stage 2 (wire-live): the portfolio-CVaR MC is ARMED.

Two layers:

1. ``sgp_within_game_rho_provider`` returns the PRICER's REAL per-pair band
   (not the flat DEFAULT_FLAT_BAND) — a calibrated same-game pair carries its
   shipped correlation, an untyped pair the conservative fallback band, a
   self-pair None.
2. The lifecycle arms + reads a ``BookRiskSnapshot`` so the portfolio-CVaR cap:
   - PASSES when the operative ES sits under the ceiling;
   - FIRES (blocks the quote) when a same-game correlated book pushes the
     operative ES over the ceiling;
   - FAILS CLOSED on a stale/absent snapshot for a NON-empty book;
   - is NOT evaluated on an empty book (a fresh start still quotes);
   - uses the REAL within-game rho (a same-game 2-leg NO book shows a
     correlated tail strictly larger than the independent/flat-band tail).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.ops.config import FiltersConfig, PricingConfig
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.sgp import SgpParams
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.lifecycle import LifecycleConfig, QuoteLifecycle, _StaleBookRisk
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.inplay import InPlayDetector
from combomaker.risk.lastlook import LastLookPolicy
from combomaker.risk.limits import LimitChecker, RiskLimits
from combomaker.sim.book_model import DEFAULT_FLAT_BAND, build_book_model
from combomaker.sim.book_risk import compute_book_risk
from combomaker.sim.within_game_rho import sgp_within_game_rho_provider
from tests.test_filters import Harness
from tests.test_lifecycle import TEST_CONVENTIONS, FakeSender, rfq
from tests.test_pricing_engine import seed_event
from tests.test_risk_shadow_mode import _FixedBankroll


def _sgp_params() -> SgpParams:
    cc = PricingConfig().correlation
    return SgpParams(
        pair_rho=dict(cc.pair_rho),
        default_rho=cc.same_event_rho,
        cross_event_rho=cc.cross_event_rho,
        typed_uncertainty=cc.typed_rho_uncertainty,
        untyped_uncertainty=cc.untyped_rho_uncertainty,
        pair_uncertainty=dict(cc.pair_rho_uncertainty),
        pair_rho_by_sport={s: dict(t) for s, t in cc.pair_rho_by_sport.items()},
        oriented_curve={k: list(v) for k, v in cc.oriented_curve.items()},
        oriented_curve_uncertainty=dict(cc.oriented_curve_uncertainty),
    )


class TestWithinGameRhoProvider:
    def test_calibrated_pair_returns_shipped_band_not_flat(self) -> None:
        prov = sgp_within_game_rho_provider(_sgp_params())
        band = prov("KXWCBTTS-X", "KXWCTOTAL-Y")
        assert band is not None
        low, point, high = band
        # soccer btts|total ships at +0.70 point; the band is the real one, NOT
        # the flat DEFAULT_FLAT_BAND (-0.20, 0.10, 0.40).
        assert point == pytest.approx(0.70, abs=1e-6)
        assert band != DEFAULT_FLAT_BAND
        assert low < point < high

    def test_untyped_pair_returns_conservative_fallback_band(self) -> None:
        # An untyped/unknown pair falls to build_sgp_correlation's flat fallback:
        # positive point with a low bound reaching into the negative regime (the
        # pricer's own widening) — never None, so the risk view is never blind.
        prov = sgp_within_game_rho_provider(_sgp_params())
        band = prov("ZZZ-A", "QQQ-B")
        assert band is not None
        low, point, _high = band
        assert point > 0.0
        assert low < 0.0  # reaches negative — the fail-safe band

    def test_self_pair_returns_none(self) -> None:
        prov = sgp_within_game_rho_provider(_sgp_params())
        assert prov("KXWCBTTS-X", "KXWCBTTS-X") is None


class TestRealRhoCorrelatedTail:
    def test_same_game_book_tail_exceeds_flat_band(self) -> None:
        # TWO NO positions sharing ONE game (each a single btts / total leg): with
        # the pricer's REAL positive within-game rho (btts|total high +0.82) the
        # two legs' YES outcomes co-occur, so BOTH parlays hit together in the tail
        # and we lose BOTH premiums at once — a FATTER 0.99 tail than the flat
        # default band (high +0.40, near-independent), where the co-hit is rarer.
        # A single 2-leg position can't show this (its own loss is binary — the
        # premium — regardless of the leg-leg rho); the correlation bites across
        # positions sharing a game, which is exactly what the cap defends.
        p_btts = OpenPosition(
            position_id="p_btts",
            combo_ticker="COMBO-BTTS",
            collection=None,
            our_side=Side.NO,
            contracts=CentiContracts(100),
            entry_price_cc=CentiCents(5_000),
            legs=(LegRef("KXWCBTTS-X", "KXWCGAME-G1", "yes"),),
        )
        p_total = OpenPosition(
            position_id="p_total",
            combo_ticker="COMBO-TOTAL",
            collection=None,
            our_side=Side.NO,
            contracts=CentiContracts(100),
            entry_price_cc=CentiCents(5_000),
            legs=(LegRef("KXWCTOTAL-Y", "KXWCGAME-G1", "yes"),),
        )
        book = [p_btts, p_total]
        prov = sgp_within_game_rho_provider(_sgp_params())
        real = build_book_model(book, marginals=lambda t: 0.5, within_game_rho=prov)
        flat = build_book_model(book, marginals=lambda t: 0.5)  # DEFAULT_FLAT_BAND

        # DIRECT wiring proof: the high-band within-game off-diagonal carries the
        # PRICER's real btts|total high correlation (+0.82), NOT the flat default
        # band's high (+0.40). This is the number the MC samples the joint tail on.
        assert float(real.corr_high[0, 1]) == pytest.approx(0.82, abs=1e-6)
        assert float(flat.corr_high[0, 1]) == pytest.approx(DEFAULT_FLAT_BAND[2], abs=1e-6)
        assert float(real.corr_high[0, 1]) > float(flat.corr_high[0, 1])

        # TAIL proof: with a rare co-hit (each leg YES prob 0.10 ⇒ we lose a
        # premium only when a parlay HITS), the real +0.82 correlation makes BOTH
        # hit together far more often than the near-independent flat band ⇒ a
        # strictly larger probability of the double-premium ($1.00 = 10_000cc)
        # joint-loss tail. (At the 0.99 quantile both es_99 saturate at the max
        # loss, so the correlation shows in the tail FREQUENCY, not magnitude.)
        # Bankroll 75_000cc so the 10%-ruin threshold (0.10×75_000 = 7_500cc)
        # lands BETWEEN one premium (5_000) and two (10_000): P(loss > 7_500) is
        # exactly P(BOTH parlays hit) — the co-hit the correlation drives.
        rare_real = build_book_model(book, marginals=lambda t: 0.10, within_game_rho=prov)
        rare_flat = build_book_model(book, marginals=lambda t: 0.10)
        s_real = compute_book_risk(
            rare_real, n_samples=200_000, seed=7, band="high", bankroll_cc=75_000
        )
        s_flat = compute_book_risk(
            rare_flat, n_samples=200_000, seed=7, band="high", bankroll_cc=75_000
        )
        thr = 0.10 * 75_000  # the 10%-ruin threshold key = 7_500cc
        assert s_real.p_loss_worse_than[thr] > s_flat.p_loss_worse_than[thr]


# --------------------------------------------------------------------------- #
# Lifecycle wiring: the portfolio-CVaR cap fires/passes/fails-closed through the
# real hot path.
# --------------------------------------------------------------------------- #


def _build(
    h: Harness,
    store: Store,
    *,
    bankroll_cc: int,
    cvar_frac: str,
    within_game_rho: object | None,
    book_risk_stale_after_s: float = 30.0,
) -> tuple[QuoteLifecycle, FakeSender, ExposureBook]:
    sender = FakeSender()
    exposure = ExposureBook(TEST_CONVENTIONS)
    engine = PricingEngine(h.feed, h.metadata, TEST_CONVENTIONS, PricingConfig())
    rfq_filter = RfqFilter(
        FiltersConfig(min_time_to_close_s=0.0).model_copy(
            update={"allowed_leg_series_prefixes": None}
        ),
        h.feed, h.metadata, h.killswitch, h.clock,
    )
    # All %-caps loose EXCEPT the portfolio CVaR one under test, so only it fires.
    from fractions import Fraction as F

    limits = LimitChecker(
        RiskLimits(
            caps_shadow_mode=False,
            game_loss_frac=F(99, 100),
            per_combo_loss_frac=F(99, 100),
            directional_frac=F(99, 100),
            slate_loss_frac=F(99, 100),
            daily_loss_frac=F(99, 100),
            drawdown_frac=F(99, 100),
            hard_trip_frac=F(99, 100),
            absolute_notional_multiple=999,
            portfolio_cvar_frac=F(int(float(cvar_frac) * 100), 100),
        )
    )
    lifecycle = QuoteLifecycle(
        clock=h.clock,
        sender=sender,
        engine=engine,
        rfq_filter=rfq_filter,
        limits=limits,
        exposure=exposure,
        feed=h.feed,
        metadata=h.metadata,
        inplay=InPlayDetector(h.clock),
        killswitch=h.killswitch,
        conventions=TEST_CONVENTIONS,
        store=store,
        metrics=Metrics(),
        lastlook_policy=LastLookPolicy(),
        config=LifecycleConfig(book_risk_stale_after_s=book_risk_stale_after_s),
        balance_tracker=_FixedBankroll(bankroll_cc),  # type: ignore[arg-type]
        start_time_provider=rfq_filter.leg_start_time,
        within_game_rho=within_game_rho,  # type: ignore[arg-type]
    )
    return lifecycle, sender, exposure


@pytest.fixture()
async def harness(tmp_path: Path) -> tuple[Harness, Store]:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / "t.sqlite3", h.clock)
    return h, store


def _no_position(exposure: ExposureBook, *, contracts: int, price_cc: int) -> None:
    # A same-game 2-leg NO position so the CVaR MC has a correlated tail to size.
    exposure.add_position(
        OpenPosition(
            position_id="held",
            combo_ticker="COMBO-HELD",
            collection=None,
            our_side=Side.NO,
            contracts=CentiContracts(contracts),
            entry_price_cc=CentiCents(price_cc),
            legs=(
                LegRef("M1", "E1", "yes"),
                LegRef("M2", "E1", "yes"),
            ),
        )
    )


async def test_cvar_cap_passes_when_es_under_ceiling(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    prov = sgp_within_game_rho_provider(_sgp_params())
    # Big bankroll ⇒ the CVaR ceiling dwarfs the tiny held book's ES ⇒ pass.
    lifecycle, sender, exposure = _build(
        h, store, bankroll_cc=100_000_000_000, cvar_frac="0.15", within_game_rho=prov
    )
    _no_position(exposure, contracts=100, price_cc=5_000)
    lifecycle.recompute_book_risk()
    await lifecycle.handle_rfq(rfq())
    # Under the ceiling: the quote goes out.
    assert len(sender.created) == 1


async def test_cvar_cap_fires_when_es_over_ceiling(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    prov = sgp_within_game_rho_provider(_sgp_params())
    # A large held NO book + a tiny bankroll ⇒ the operative ES blows past the
    # 15%-of-bankroll ceiling ⇒ the CVaR cap ENFORCES ⇒ no quote.
    lifecycle, sender, exposure = _build(
        h, store, bankroll_cc=200, cvar_frac="0.15", within_game_rho=prov
    )
    _no_position(exposure, contracts=100_000, price_cc=5_000)
    lifecycle.recompute_book_risk()
    await lifecycle.handle_rfq(rfq())
    assert sender.created == []


async def test_cvar_cap_fails_closed_on_stale_snapshot(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    prov = sgp_within_game_rho_provider(_sgp_params())
    # Snapshot goes stale in 0s: any positive age is stale ⇒ the non-empty book
    # fails the CVaR cap CLOSED even with a huge bankroll (UNKNOWN tail is never
    # safe). No recompute at all here → _StaleBookRisk sentinel.
    lifecycle, sender, exposure = _build(
        h, store, bankroll_cc=100_000_000_000, cvar_frac="0.15",
        within_game_rho=prov, book_risk_stale_after_s=0.0,
    )
    _no_position(exposure, contracts=100, price_cc=5_000)
    # Deliberately DO NOT recompute — a non-empty book with no snapshot fails closed.
    await lifecycle.handle_rfq(rfq())
    assert sender.created == []


async def test_cvar_cap_not_evaluated_on_empty_book(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    prov = sgp_within_game_rho_provider(_sgp_params())
    # EMPTY book (no held positions), stale window 0s, tiny bankroll: the CVaR cap
    # must NOT fire (nothing to cap) — a fresh start still quotes normally.
    lifecycle, sender, exposure = _build(
        h, store, bankroll_cc=100_000_000_000, cvar_frac="0.15",
        within_game_rho=prov, book_risk_stale_after_s=0.0,
    )
    assert not exposure.positions
    await lifecycle.handle_rfq(rfq())
    assert len(sender.created) == 1


def test_book_risk_for_check_stale_sentinel_is_unusable() -> None:
    # The fail-closed sentinel reports usable=False so the CVaR cap breaches
    # regardless of ES (the checker's unusable-snapshot branch).
    s = _StaleBookRisk()
    assert s.usable is False
    # And the cap actually breaches on it:
    from combomaker.risk.limits import DailyPnl
    from tests.test_limits_caps import CONVENTIONS, MARG

    limits = LimitChecker(RiskLimits(caps_shadow_mode=False))
    breaches = limits.check(
        ExposureBook(CONVENTIONS), MARG, DailyPnl(),
        risk_bankroll_cc=20_000_000, book_risk=s,
    )
    assert ReasonCode.SKIP_PORTFOLIO_CVAR in [b.reason for b in breaches]
