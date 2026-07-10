"""Engine-level archetype behavior through PricingEngine.price.

Covers the QuoteConfig/CorrelationConfig archetype knobs end to end:

- SGP typed-pair correlations move the joint fair in the SIGNED direction:
  btts|total (+0.60) lifts fair above the independence product;
  moneyline|moneyline (-0.85) pushes it below.
- Longshot floor: below longshot_fair_threshold the uncertainty width
  component is floored at fair x longshot_min_rel_uncertainty.
- Favorites multiplier: collapses width components to a single 'scaled'
  entry at ~multiplier x total, gated on the selected-side prob threshold.
- Leg-count convexity: 'legs' component = per_leg x n^convexity.
- Regression: the default config still prices the standard cross-event combo.

Harness books (tests.test_filters.Harness.with_books) give every leg a
microprice of ~0.4789 with uncertainty 0.01, so selected-side probs are
~0.4789 (yes) / ~0.5211 (no) — comfortably inside every gate exercised here.
"""

from combomaker.core.conventions import DOC_ASSUMED
from combomaker.core.money import cc_from_prob
from combomaker.ops.config import CorrelationConfig, PricingConfig, QuoteConfig
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.legs import KalshiBookSource
from combomaker.pricing.quote import ConstructedQuote
from combomaker.rfq.models import Rfq
from tests.test_filters import Harness
from tests.test_pricing_engine import CROSS_EVENT_LEGS, combo, seed_event

# Far beyond time_wide_threshold_s (21_600s): no 'time' width component.
TTC = 100_000.0

# Same-event (SGP) fixtures: series prefixes chosen so legtypes.classify_leg
# types them BTTS / TOTAL / MONEYLINE from the ticker alone.
SGP_EVENT = "KXWC-26JUL10AB"
BTTS_LEG = "KXWCBTTS-26JUL10AB-BTTS"
TOTAL_LEG = "KXWCTOTAL-26JUL10AB-3"
ML_LEG_A = "KXWCGAME-26JUL10AB-AB"
ML_LEG_B = "KXWCGAME-26JUL10AB-BA"


async def cross_event_engine(
    config: PricingConfig | None = None,
) -> tuple[PricingEngine, Harness]:
    """The standard cross-event harness (M1 yes / M2 no on events E1/E2)."""
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("KXMVE-C1")  # combo market metadata incl. 1-cent grid
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    return PricingEngine(h.feed, h.metadata, DOC_ASSUMED, config or PricingConfig()), h


async def same_event_engine(
    tickers: list[str], config: PricingConfig | None = None
) -> tuple[PricingEngine, Harness]:
    """Two legs of ONE event; exclusive=False so two YES legs are classifiable."""
    h = Harness()
    await h.with_books(tickers)
    h.with_meta("KXMVE-C1")
    seed_event(h, SGP_EVENT, exclusive=False)
    return PricingEngine(h.feed, h.metadata, DOC_ASSUMED, config or PricingConfig()), h


def same_event_combo(tickers: list[str]) -> Rfq:
    return combo(
        [{"market_ticker": t, "side": "yes", "event_ticker": SGP_EVENT} for t in tickers]
    )


def rho_zero(pair: str) -> PricingConfig:
    """Config whose ONLY typed pair prior is `pair` at rho 0.0 (independence).

    Sport tables are blanked too — they outrank the global table now that the
    engine actually forwards them (dead-config bug fixed 2026-07-06).
    """
    return PricingConfig(
        correlation=CorrelationConfig(pair_rho={pair: 0.0}, pair_rho_by_sport={})
    )


def independence_fair_cc(h: Harness, tickers: list[str]) -> int:
    """Fair (cc) if the selected YES legs were independent: product of marginals."""
    source = KalshiBookSource(h.feed)
    product = 1.0
    for ticker in tickers:
        belief = source.marginal(ticker)
        assert belief is not None
        product *= belief.p
    return int(cc_from_prob(product))


# --- 1. SGP signed typed pairs -------------------------------------------------


async def test_btts_total_positive_rho_prices_fair_above_independence() -> None:
    tickers = [BTTS_LEG, TOTAL_LEG]
    rfq = same_event_combo(tickers)

    engine_typed, h = await same_event_engine(tickers)  # default btts|total = +0.60
    engine_ind, _ = await same_event_engine(tickers, rho_zero("btts|total"))
    typed = engine_typed.price(rfq, time_to_close_s=TTC)
    independent = engine_ind.price(rfq, time_to_close_s=TTC)

    assert isinstance(typed, ConstructedQuote), typed
    assert isinstance(independent, ConstructedQuote), independent
    # A typed rho of exactly 0.0 makes the point matrix the identity, which the
    # copula short-circuits to the exact product of the leg marginals.
    assert int(independent.fair_cc) == independence_fair_cc(h, tickers)
    # Positive correlation raises P(AND) above the product — by a real margin
    # (>5c on ~0.48 marginals), not float noise.
    assert typed.fair_cc > independent.fair_cc + 500


async def test_moneyline_pair_negative_rho_prices_fair_below_independence() -> None:
    tickers = [ML_LEG_A, ML_LEG_B]
    rfq = same_event_combo(tickers)

    engine_typed, h = await same_event_engine(tickers)  # moneyline|moneyline = -0.85
    engine_ind, _ = await same_event_engine(tickers, rho_zero("moneyline|moneyline"))
    typed = engine_typed.price(rfq, time_to_close_s=TTC)
    independent = engine_ind.price(rfq, time_to_close_s=TTC)

    assert isinstance(typed, ConstructedQuote), typed
    assert isinstance(independent, ConstructedQuote), independent
    assert int(independent.fair_cc) == independence_fair_cc(h, tickers)
    # Two winners of the same game are near-exclusive: fair must sit WELL
    # below the independence product.
    assert typed.fair_cc < int(independent.fair_cc) - 500
    assert typed.fair_cc < independence_fair_cc(h, tickers)


# --- 2. Longshot uncertainty floor ---------------------------------------------


async def test_longshot_floor_sets_min_relative_uncertainty_width() -> None:
    rfq = combo(CROSS_EVENT_LEGS)
    # This combo's fair is ~0.25; raising the threshold to 0.9 puts it in the
    # longshot regime without touching books or the joint itself.
    floored_cfg = PricingConfig(quote=QuoteConfig(longshot_fair_threshold=0.9))
    engine_floored, _ = await cross_event_engine(floored_cfg)
    engine_default, _ = await cross_event_engine()
    floored = engine_floored.price(rfq, time_to_close_s=TTC)
    default = engine_default.price(rfq, time_to_close_s=TTC)
    assert isinstance(floored, ConstructedQuote), floored
    assert isinstance(default, ConstructedQuote), default

    rel = floored_cfg.quote.longshot_min_rel_uncertainty  # 0.25 default
    # The floor touches only uncertainty, never fair.
    assert floored.fair_cc == default.fair_cc
    # Default threshold (0.15) is below this fair: floor off, and the combo's
    # intrinsic uncertainty (~0.01) sits well under fair*rel — so the floor in
    # the other engine genuinely BINDS rather than being a no-op.
    assert default.width_components_cc["uncertainty"] < default.fair_cc * rel - 2
    assert floored.width_components_cc["uncertainty"] > default.width_components_cc["uncertainty"]
    # Honest bound from the code path: engine floors joint.uncertainty at
    # p*rel, then construct_quote takes int(u * CC_PER_DOLLAR * scale) with
    # scale=1. width_unc = floor(p*rel*1e4) while fair_cc = round(p*1e4), so
    # the component is within ~1.2cc of fair_cc*rel from below.
    assert floored.width_components_cc["uncertainty"] >= floored.fair_cc * rel - 2


# --- 3. Favorites width multiplier ----------------------------------------------


async def test_favorites_multiplier_halves_total_width() -> None:
    rfq = combo(CROSS_EVENT_LEGS)
    tight_cfg = PricingConfig(
        quote=QuoteConfig(favorite_width_multiplier=0.5, favorite_leg_threshold=0.4)
    )
    engine_tight, _ = await cross_event_engine(tight_cfg)
    engine_full, _ = await cross_event_engine()
    tight = engine_tight.price(rfq, time_to_close_s=TTC)
    full = engine_full.price(rfq, time_to_close_s=TTC)
    assert isinstance(tight, ConstructedQuote), tight
    assert isinstance(full, ConstructedQuote), full

    # Selected sides (~0.4789 yes / ~0.5211 no) both clear the 0.4 threshold,
    # so the multiplier applies and COLLAPSES the breakdown to one entry.
    assert set(tight.width_components_cc) == {"scaled"}
    expected = max(int(full.total_width_cc * 0.5), tight_cfg.quote.base_width_cc // 2)
    assert tight.total_width_cc == expected
    # ~half: exact int truncation of the unscaled total (well above the
    # base/2 floor for this combo).
    assert tight.total_width_cc == full.total_width_cc // 2


async def test_favorites_multiplier_gated_by_unreachable_threshold() -> None:
    rfq = combo(CROSS_EVENT_LEGS)
    gated_cfg = PricingConfig(
        quote=QuoteConfig(favorite_width_multiplier=0.5, favorite_leg_threshold=0.99)
    )
    engine_gated, _ = await cross_event_engine(gated_cfg)
    engine_full, _ = await cross_event_engine()
    gated = engine_gated.price(rfq, time_to_close_s=TTC)
    full = engine_full.price(rfq, time_to_close_s=TTC)
    assert isinstance(gated, ConstructedQuote), gated
    assert isinstance(full, ConstructedQuote), full

    # 0.99 is unreachable for ~0.48/~0.52 selected sides: no collapse, and the
    # width breakdown is identical to the multiplier-off engine.
    assert "scaled" not in gated.width_components_cc
    assert gated.width_components_cc == full.width_components_cc
    assert gated.total_width_cc == full.total_width_cc


# --- 4. Leg-count convexity -----------------------------------------------------


async def test_leg_count_convexity_reshapes_legs_component() -> None:
    rfq = combo(CROSS_EVENT_LEGS)
    engine_convex, _ = await cross_event_engine(
        PricingConfig(quote=QuoteConfig(leg_count_convexity=1.5))
    )
    engine_linear, _ = await cross_event_engine()  # default convexity 1.0
    convex = engine_convex.price(rfq, time_to_close_s=TTC)
    linear = engine_linear.price(rfq, time_to_close_s=TTC)
    assert isinstance(convex, ConstructedQuote), convex
    assert isinstance(linear, ConstructedQuote), linear

    assert linear.width_components_cc["legs"] == 200  # 100 x 2^1.0
    assert convex.width_components_cc["legs"] == int(100 * 2**1.5)  # floor(282.84)
    assert convex.width_components_cc["legs"] == 282


# --- 5. Regression: default config still quotes the standard combo --------------


async def test_default_config_regression_two_sided_quote() -> None:
    engine, _ = await cross_event_engine()
    result = engine.price(combo(CROSS_EVENT_LEGS), time_to_close_s=TTC)
    assert isinstance(result, ConstructedQuote), result
    assert result.yes_bid_cc > 0 and result.no_bid_cc > 0
    assert result.yes_bid_cc % 100 == 0 and result.no_bid_cc % 100 == 0  # 1c grid
    assert result.yes_bid_cc + result.no_bid_cc <= 10_000 - 100  # min capture
    assert 0 < result.fair_cc < 10_000
    # no 'time' (TTC beyond threshold), no 'in_play', no 'scaled' collapse
    assert set(result.width_components_cc) == {"base", "legs", "uncertainty", "size"}


# --- 6. Sport tables + orientation reach the live engine -------------------------


def test_engine_forwards_sport_tables() -> None:
    """Dead-config regression: PricingEngine must forward pair_rho_by_sport
    into SgpParams — before 2026-07-06 the calibrated sport tables (soccer
    ml|total 0.28, nfl 0.00, mlb −0.05) silently never reached the hot path."""
    h = Harness()
    engine = PricingEngine(h.feed, h.metadata, DOC_ASSUMED, PricingConfig())
    tables = engine._sgp_params.pair_rho_by_sport  # noqa: SLF001 (test seam)
    assert tables, "sport tables missing from engine SgpParams"
    assert tables["soccer"]["moneyline|total"] == 0.28
    assert tables["nfl"]["moneyline|total"] == 0.00
    # MLB props tranche (2026-07-09): the measured player-prop entries and
    # their sport-prefixed bands must reach the hot path too.
    assert tables["mlb"]["player_ks|total"] == -0.25
    bands = engine._sgp_params.pair_uncertainty  # noqa: SLF001 (test seam)
    assert bands["mlb:player_ks|total"] == 0.12
    # DO-1 untabled-cell quick-fix (2026-07-10 sweep): the spread×prop
    # neutralized cells and the rfi labeled priors must reach the hot path —
    # one sentinel per new group, plus the enumeration-gap ml|tb cell.
    assert tables["mlb"]["moneyline|player_tb"] == 0.00
    assert bands["mlb:moneyline|player_tb"] == 0.30
    assert tables["mlb"]["player_hit|spread"] == 0.00
    assert bands["mlb:player_hit|spread"] == 0.20
    assert tables["mlb"]["rfi|spread"] == 0.00
    assert bands["mlb:rfi|spread"] == 0.15
    assert tables["mlb"]["player_hrr|rfi"] == 0.10
    assert bands["mlb:player_hrr|rfi"] == 0.20


def test_mlb_pair_table_has_no_band_orphans() -> None:
    """DO-1 invariant: every mlb pair entry has an 'mlb:'-prefixed band and
    every mlb-prefixed band has an entry — a point without a band gets the
    default width (wrong confidence), a band without a point is dead config.
    43 entries / 43 bands as of the 2026-07-10 untabled-cell quick-fix; the
    count only ever grows (routing/measurement phases add oriented keys)."""
    h = Harness()
    engine = PricingEngine(h.feed, h.metadata, DOC_ASSUMED, PricingConfig())
    mlb = engine._sgp_params.pair_rho_by_sport["mlb"]  # noqa: SLF001 (test seam)
    bands = engine._sgp_params.pair_uncertainty  # noqa: SLF001 (test seam)
    mlb_bands = {k.removeprefix("mlb:") for k in bands if k.startswith("mlb:")}
    assert set(mlb) == mlb_bands
    assert len(mlb) >= 43


async def dog_ml_btts_harness(config: PricingConfig | None = None) -> PricingEngine:
    """ML leg priced a clear dog (~0.245), BTTS ~0.605 — the SPA/POR shape."""
    from tests.test_feed import snapshot_env

    h = Harness()
    tickers = [ML_LEG_A, BTTS_LEG]
    books = {
        ML_LEG_A: ([["0.2200", "50.00"], ["0.2400", "20.00"]],
                   [["0.7400", "60.00"], ["0.7500", "25.00"]]),
        BTTS_LEG: ([["0.5800", "50.00"], ["0.6000", "20.00"]],
                   [["0.3800", "60.00"], ["0.3900", "25.00"]]),
    }
    h.feed.watch(tickers)
    await h.ws.ack_subscription(0, 5)
    for i, ticker in enumerate(tickers):
        env = snapshot_env(5, i + 1, ticker)
        env["msg"]["yes_dollars_fp"], env["msg"]["no_dollars_fp"] = books[ticker]
        await h.ws.deliver(env)
    h.with_meta("KXMVE-C1")
    seed_event(h, SGP_EVENT, exclusive=False)
    return PricingEngine(h.feed, h.metadata, DOC_ASSUMED, config or PricingConfig())


async def test_dog_moneyline_btts_prices_above_favorite_prior() -> None:
    """Orientation end to end: with the ML leg a clear dog (~0.245), the default
    config (the win-prob CURVE -> rho ~-0.09 there) must price the joint ABOVE a
    config that applies the favorites prior (-0.19) to dogs too — same books,
    same marginals. (The curve supersedes the fav/dog plateau; the flat config
    disables it via oriented_curve={}.)"""
    rfq = same_event_combo([ML_LEG_A, BTTS_LEG])

    engine_oriented = await dog_ml_btts_harness()
    base = CorrelationConfig()
    soccer_flat = dict(base.pair_rho_by_sport["soccer"])
    soccer_flat["btts|moneyline:dog"] = soccer_flat["btts|moneyline:fav"]  # −0.19 everywhere
    flat_cfg = PricingConfig(
        correlation=CorrelationConfig(
            pair_rho_by_sport={**base.pair_rho_by_sport, "soccer": soccer_flat},
            oriented_curve={},  # disable the win-prob curve -> flat fav/dog path
        )
    )
    engine_flat = await dog_ml_btts_harness(flat_cfg)

    oriented = engine_oriented.price(rfq, time_to_close_s=TTC)
    flat = engine_flat.price(rfq, time_to_close_s=TTC)
    assert isinstance(oriented, ConstructedQuote), oriented
    assert isinstance(flat, ConstructedQuote), flat
    # curve rho ~-0.09 vs flat -0.19 on ~0.245×0.605 marginals: a real gap (~127cc).
    assert oriented.fair_cc > flat.fair_cc + 50
