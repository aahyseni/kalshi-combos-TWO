from pathlib import Path

import pytest

from combomaker.core.conventions import DOC_ASSUMED
from combomaker.core.money import CentiCents, cc_from_prob
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.marketdata.metadata import EventMeta
from combomaker.ops.config import PricingConfig, QuoteConfig, load_config
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.legs import KalshiBookSource
from combomaker.pricing.quote import ConstructedQuote, NoQuote
from combomaker.rfq.models import Rfq
from tests.test_filters import Harness

REPO_ROOT = Path(__file__).resolve().parents[1]  # module scope: no Path I/O inside async tests

SAME_MARKET_BOTH_SIDES = [
    {"market_ticker": "M1", "side": "yes", "event_ticker": "E1"},
    {"market_ticker": "M1", "side": "no", "event_ticker": "E1"},
]


def combo(legs: list[dict[str, str]], **overrides: object) -> Rfq:
    msg: dict[str, object] = {
        "id": "rfq_1",
        "market_ticker": "KXMVE-C1",
        "created_ts": "2026-07-05T10:00:00Z",
        "contracts_fp": "10.00",
        "mve_collection_ticker": "KXMVESPORTS",
        "mve_selected_legs": legs,
    }
    msg.update(overrides)
    return Rfq.from_ws(msg)


CROSS_EVENT_LEGS = [
    {"market_ticker": "M1", "side": "yes", "event_ticker": "E1"},
    {"market_ticker": "M2", "side": "no", "event_ticker": "E2"},
]


def seed_event(h: Harness, event_ticker: str, exclusive: bool | None) -> None:
    h.metadata._events[event_ticker] = EventMeta(  # noqa: SLF001 (test seam)
        event_ticker=event_ticker,
        mutually_exclusive=exclusive,
        raw={},
        fetched_mono_ns=0,
    )


async def engine_harness() -> tuple[PricingEngine, Harness]:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("KXMVE-C1")  # combo market metadata incl. 1-cent grid
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    engine = PricingEngine(h.feed, h.metadata, DOC_ASSUMED, PricingConfig())
    return engine, h


async def test_happy_path_produces_two_sided_quote() -> None:
    engine, _ = await engine_harness()
    result = engine.price(combo(CROSS_EVENT_LEGS), time_to_close_s=100_000)
    assert isinstance(result, ConstructedQuote), result
    assert 0 < result.yes_bid_cc and 0 < result.no_bid_cc
    assert result.yes_bid_cc + result.no_bid_cc <= 10_000 - 100
    assert result.yes_bid_cc % 100 == 0  # on the 1-cent grid
    assert result.width_components_cc["legs"] == 200  # 2 legs x 100


async def test_sell_parlays_only_declines_yes_side() -> None:
    """Full-stack fade defense: QuoteConfig.sell_parlays_only flows YAML ->
    PricingConfig -> engine -> QuoteParams -> construct_quote, so a normal combo
    quotes ONLY the NO (parlay-seller) side. yes_bid=0, no_bid still live."""
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    cfg = PricingConfig(quote=QuoteConfig(sell_parlays_only=True))
    engine = PricingEngine(h.feed, h.metadata, DOC_ASSUMED, cfg)
    result = engine.price(combo(CROSS_EVENT_LEGS), time_to_close_s=100_000)
    assert isinstance(result, ConstructedQuote), result
    assert result.yes_bid_cc == 0          # fade defense: never long the YES combo
    assert result.no_bid_cc > 0            # still selling the parlay


async def test_sell_only_wired_from_real_prod_yaml() -> None:
    """True end-to-end: load the ACTUAL config/prod.yaml, build the engine from
    the loaded config.pricing, price a normal combo -> yes_bid must be 0. Locks
    the whole YAML -> PricingConfig -> engine -> quote chain (a future refactor of
    the engine exclusion list could pass the narrower tests but break this)."""
    cfg = load_config(REPO_ROOT / "config" / "prod.yaml")
    assert cfg.pricing.quote.sell_parlays_only is True
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    engine = PricingEngine(h.feed, h.metadata, DOC_ASSUMED, cfg.pricing)
    result = engine.price(combo(CROSS_EVENT_LEGS), time_to_close_s=100_000)
    assert isinstance(result, ConstructedQuote), result
    assert result.yes_bid_cc == 0
    assert result.no_bid_cc > 0


async def test_engine_boundary_zeros_a_leaked_yes_bid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt-and-suspenders (defense in depth): if a builder ever WRONGLY returned a
    non-zero yes_bid, the engine boundary must still zero it in sell-only mode.
    Forces the leak via monkeypatch and asserts the real price() path corrects it;
    a two-sided engine leaves it untouched."""
    import combomaker.pricing.engine as eng_mod

    leaked = ConstructedQuote(
        yes_bid_cc=CentiCents(2_600), no_bid_cc=CentiCents(6_500),
        fair_cc=CentiCents(3_000), width_components_cc={"base": 200},
    )
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    monkeypatch.setattr(eng_mod, "construct_quote", lambda **_: leaked)

    sell = PricingEngine(h.feed, h.metadata, DOC_ASSUMED,
                         PricingConfig(quote=QuoteConfig(sell_parlays_only=True)))
    corrected = sell.price(combo(CROSS_EVENT_LEGS), time_to_close_s=100_000)
    assert isinstance(corrected, ConstructedQuote)
    assert corrected.yes_bid_cc == 0        # boundary caught the leaked YES
    assert corrected.no_bid_cc == 6_500     # sell side untouched

    two_sided = PricingEngine(h.feed, h.metadata, DOC_ASSUMED, PricingConfig())
    passed = two_sided.price(combo(CROSS_EVENT_LEGS), time_to_close_s=100_000)
    assert isinstance(passed, ConstructedQuote)
    assert passed.yes_bid_cc == 2_600       # two-sided: untouched


async def test_engine_boundary_declines_a_leaked_yes_only_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H1: if a builder ever leaked a YES-only quote (yes>0, no=0), zeroing YES
    must NOT emit an invalid (0,0) quote — the boundary declines cleanly."""
    import combomaker.pricing.engine as eng_mod

    yes_only = ConstructedQuote(
        yes_bid_cc=CentiCents(2_600), no_bid_cc=CentiCents(0),
        fair_cc=CentiCents(3_000), width_components_cc={"base": 200},
    )
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    monkeypatch.setattr(eng_mod, "construct_quote", lambda **_: yes_only)
    sell = PricingEngine(h.feed, h.metadata, DOC_ASSUMED,
                         PricingConfig(quote=QuoteConfig(sell_parlays_only=True)))
    result = sell.price(combo(CROSS_EVENT_LEGS), time_to_close_s=100_000)
    assert isinstance(result, NoQuote)      # not a both-zero ConstructedQuote
    assert result.reason is ReasonCode.SKIP_PRICING_FAILED


async def test_impossible_combo_refused_not_arbed() -> None:
    engine, h = await engine_harness()
    rfq = combo(
        [
            {"market_ticker": "M1", "side": "yes", "event_ticker": "E1"},
            {"market_ticker": "M2", "side": "yes", "event_ticker": "E1"},
        ]
    )
    result = engine.price(rfq, time_to_close_s=100_000)
    assert isinstance(result, NoQuote)
    assert result.reason == ReasonCode.SKIP_LOGICALLY_IMPOSSIBLE


async def test_unknown_event_metadata_is_no_quote() -> None:
    engine, h = await engine_harness()
    rfq = combo(
        [
            {"market_ticker": "M1", "side": "yes", "event_ticker": "E_UNSEEN"},
            {"market_ticker": "M2", "side": "no", "event_ticker": "E_UNSEEN"},
        ]
    )
    result = engine.price(rfq, time_to_close_s=100_000)
    assert isinstance(result, NoQuote)
    assert result.reason == ReasonCode.SKIP_CLASSIFIER_UNKNOWN


async def test_same_event_mixed_sides_quotes_with_extra_width() -> None:
    engine, _ = await engine_harness()
    cross = engine.price(combo(CROSS_EVENT_LEGS), time_to_close_s=100_000)
    same_event = engine.price(
        combo(
            [
                {"market_ticker": "M1", "side": "yes", "event_ticker": "E1"},
                {"market_ticker": "M2", "side": "no", "event_ticker": "E1"},
            ]
        ),
        time_to_close_s=100_000,
    )
    assert isinstance(cross, ConstructedQuote) and isinstance(same_event, ConstructedQuote)
    # correlation uncertainty must cost width relative to the cross-event case
    assert (
        same_event.width_components_cc["uncertainty"] > cross.width_components_cc["uncertainty"]
    )


async def test_missing_combo_grid_is_no_quote() -> None:
    engine, h = await engine_harness()
    del h.metadata._markets["KXMVE-C1"]  # noqa: SLF001
    result = engine.price(combo(CROSS_EVENT_LEGS), time_to_close_s=100_000)
    assert isinstance(result, NoQuote)
    assert result.reason == ReasonCode.SKIP_CLASSIFIER_UNKNOWN


async def test_invalid_leg_book_is_pricing_failure() -> None:
    engine, h = await engine_harness()
    h.feed.book("M2").invalidate("test")
    result = engine.price(combo(CROSS_EVENT_LEGS), time_to_close_s=100_000)
    assert isinstance(result, NoQuote)
    assert result.reason == ReasonCode.SKIP_PRICING_FAILED


async def test_target_cost_qty_estimate_rounds_up() -> None:
    engine, _ = await engine_harness()
    rfq = combo(CROSS_EVENT_LEGS, contracts_fp=None, target_cost_dollars="100.00")
    qty = engine._resolve_qty(rfq, fair_prob=0.30)  # noqa: SLF001
    assert qty is not None
    # $100 / $0.30 = 333.33... contracts -> rounds UP (more size => more width)
    assert qty == CentiContracts(-(-1_000_000 * 100 // 3_000))


async def test_no_sizing_mode_is_unknown() -> None:
    engine, _ = await engine_harness()
    rfq = combo(CROSS_EVENT_LEGS, contracts_fp=None)
    result = engine.price(rfq, time_to_close_s=100_000)
    assert isinstance(result, NoQuote)
    assert result.reason == ReasonCode.SKIP_CLASSIFIER_UNKNOWN


async def test_farmable_impossible_is_farmed_not_declined() -> None:
    """A LOGICALLY-CERTAIN impossibility (same market both sides) is FARMED:
    we short the certain-NO side at the naive-independence YES value and never
    touch the worthless YES."""
    engine, h = await engine_harness()
    result = engine.price(combo(SAME_MARKET_BOTH_SIDES), time_to_close_s=100_000)
    assert isinstance(result, ConstructedQuote), result
    assert result.farmed is True
    assert result.yes_bid_cc == 0          # never long the worthless YES
    assert result.fair_cc == 0             # true fair of an impossible combo
    # M1 marginal 0.4789 ⇒ naive 0.4789*0.5211 = 0.2496 ⇒ ask 2495cc ⇒ bid NO
    # at $1 - 0.2495 = 0.7505, snapped down to the cent grid.
    assert result.no_bid_cc == 7_500
    assert result.width_components_cc == {"farm_sell_price": 2_500}


async def test_farm_ask_is_below_every_selected_leg_marginal() -> None:
    """Arb-free: the naive YES value we sell at is strictly below each selected
    leg's marginal (an impossible combo's YES is dominated by every leg)."""
    engine, h = await engine_harness()
    result = engine.price(combo(SAME_MARKET_BOTH_SIDES), time_to_close_s=100_000)
    assert isinstance(result, ConstructedQuote)
    p = KalshiBookSource(h.feed).marginal("M1")
    assert p is not None
    farm_ask = 10_000 - int(result.no_bid_cc)  # implied YES sell price = 1 - no_bid
    # selected marginals: M1 yes = p, M1 no = 1 - p, both in cc
    assert farm_ask < int(cc_from_prob(p.p))          # below the yes-leg marginal
    assert farm_ask < int(cc_from_prob(1.0 - p.p))    # below the no-leg marginal


async def test_farm_flag_off_declines_as_before() -> None:
    engine, h = await engine_harness()
    engine_off = PricingEngine(
        h.feed,
        h.metadata,
        DOC_ASSUMED,
        PricingConfig(quote=QuoteConfig(farm_impossible_combos=False)),
    )
    result = engine_off.price(combo(SAME_MARKET_BOTH_SIDES), time_to_close_s=100_000)
    assert isinstance(result, NoQuote)
    assert result.reason == ReasonCode.SKIP_LOGICALLY_IMPOSSIBLE


async def test_non_farmable_impossible_still_declines_with_flag_on() -> None:
    """Mutual-exclusion IMPOSSIBLE is metadata-dependent (NOT farmable): it must
    keep declining even with farming enabled."""
    engine, h = await engine_harness()
    rfq = combo(
        [
            {"market_ticker": "M1", "side": "yes", "event_ticker": "E1"},
            {"market_ticker": "M2", "side": "yes", "event_ticker": "E1"},
        ]
    )
    result = engine.price(rfq, time_to_close_s=100_000)
    assert isinstance(result, NoQuote)
    assert result.reason == ReasonCode.SKIP_LOGICALLY_IMPOSSIBLE


async def test_farm_without_beliefs_falls_back_to_no_quote() -> None:
    """Never farm blind: a missing/invalid leg book ⇒ the ordinary
    SKIP_LOGICALLY_IMPOSSIBLE no-quote, never a farm at an unknown price."""
    engine, h = await engine_harness()
    h.feed.book("M1").invalidate("test")
    result = engine.price(combo(SAME_MARKET_BOTH_SIDES), time_to_close_s=100_000)
    assert isinstance(result, NoQuote)
    assert result.reason == ReasonCode.SKIP_LOGICALLY_IMPOSSIBLE


async def test_btts_containment_prices_at_subset_marginal_not_independence() -> None:
    from combomaker.core.money import cc_from_prob
    from combomaker.pricing.legs import KalshiBookSource

    fh_btts = "KXWC1HBTTS-26JUL05MEXENG-BTTS"
    ft_btts = "KXWCBTTS-26JUL05MEXENG-BTTS"
    h = Harness()
    await h.with_books([fh_btts, ft_btts])  # identical books => equal marginals
    h.with_meta("KXMVE-C1")
    seed_event(h, "KXWC1HBTTS-26JUL05MEXENG", exclusive=None)
    seed_event(h, "KXWCBTTS-26JUL05MEXENG", exclusive=None)
    engine = PricingEngine(h.feed, h.metadata, DOC_ASSUMED, PricingConfig())
    rfq = combo(
        [
            {"market_ticker": fh_btts, "side": "yes", "event_ticker": "KXWC1HBTTS-26JUL05MEXENG"},
            {"market_ticker": ft_btts, "side": "yes", "event_ticker": "KXWCBTTS-26JUL05MEXENG"},
        ]
    )
    result = engine.price(rfq, time_to_close_s=100_000)
    assert isinstance(result, ConstructedQuote), result
    p = KalshiBookSource(h.feed).marginal(fh_btts)
    assert p is not None
    # Containment pins the fair at P(1H-BTTS) exactly, well above the
    # independence product P(1H-BTTS)*P(FT-BTTS) the copula would have produced.
    assert result.fair_cc == cc_from_prob(p.p)
    assert result.fair_cc > cc_from_prob(p.p * p.p)
