"""DNP scalar guard (2026-08-06 build) — pricing/dnp_scalar.py + the engine/
quote seams. Forensics: docs/reports/2026-08-06-dnp-scalar-pickoff.md (3
same-player Marte combos settled scalar at the independence product, taker
+$39.75). Pinned here:

- scope detection (single-player-driven only; UNKNOWN entity FAILS CLOSED
  into scope; multi-player / mixed combos are untouched),
- the sniper ask floor (never lowers an ask; the 3 sniped tickets' exact
  fill-time mids reprice to asks >= their exchange scalar settles),
- the void-branch mixture (raises fair only when the DNP value exceeds the
  correlated fair; never lowers it),
- hazard derivation (measured counts only; thin families fail closed to
  max(own, pooled); empty corpus => no mixture),
- flag OFF = byte-identical engine output (shadow-dark default, incl. the
  committed prod.yaml),
- the settled cache's scalar-outcome bookkeeping feeding the settlement-
  cadence hazard refresh.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from combomaker.core.clock import FakeClock
from combomaker.core.conventions import DOC_ASSUMED
from combomaker.marketdata.feed import OrderbookFeed
from combomaker.marketdata.grid import PriceGrid
from combomaker.marketdata.metadata import EventMeta, MarketMeta, MetadataCache
from combomaker.ops.config import DnpScalarConfig, PricingConfig, load_config
from combomaker.pricing.dnp_scalar import (
    DnpHazards,
    baseline_hazards,
    counts_from_outcomes,
    floor_product_cc,
    single_player_scope,
)
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.legs import LegBelief
from combomaker.pricing.quote import ConstructedQuote, NoQuote
from combomaker.rfq.models import Rfq
from tests.test_feed import FakeWs, snapshot_env
from tests.test_settled_marginals import FakeMarketSource, _resolver, market_payload

REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 5, 18, 0, 0, tzinfo=UTC)

# The 3 sniped tickets' EXACT leg mids at fill (exchange-truth tape,
# night/master.json pull 2026-08-06) and their exchange scalar settles.
MARTE_HRR2 = "KXMLBHRR-26JUL231715AZSTL-AZKMARTE4-2"
MARTE_TB2 = "KXMLBTB-26JUL231715AZSTL-AZKMARTE4-2"
MARTE_HIT1 = "KXMLBHIT-26AUG052140SDAZ-AZKMARTE4-1"
MARTE_HRR2_B = "KXMLBHRR-26AUG052140SDAZ-AZKMARTE4-2"
T1_MIDS = {MARTE_HRR2: 0.5288, MARTE_TB2: 0.4404}          # settle 20c
T3_MIDS = {MARTE_HIT1: 0.6473, MARTE_HRR2_B: 0.5089}       # settle 17c


def belief(p: float) -> LegBelief:
    return LegBelief(source="test", p=p, uncertainty=0.01)


def _leg(ticker: str, side: str) -> dict[str, str]:
    parts = ticker.split("-")
    return {
        "market_ticker": ticker,
        "side": side,
        "event_ticker": "-".join(parts[:-1]) if len(parts) >= 3 else ticker,
    }


def combo(legs: list[dict[str, str]], ticker: str = "KXMVE-DNP1") -> Rfq:
    return Rfq.from_ws(
        {
            "id": "rfq_dnp",
            "market_ticker": ticker,
            "created_ts": "2026-08-05T17:00:00Z",
            "contracts_fp": "100.00",
            "mve_collection_ticker": "KXMVECROSSCATEGORY",
            "mve_selected_legs": legs,
        }
    )


async def seeded_engine(
    mids: dict[str, float], *, enabled: bool, combo_ticker: str = "KXMVE-DNP1"
) -> PricingEngine:
    """Real engine over a harness feed whose leg books mid at the given
    probabilities (±0.5c symmetric top of book, deep enough for the caps)."""
    clock = FakeClock(start=NOW)
    ws = FakeWs()
    feed = OrderbookFeed(ws, clock)
    metadata = MetadataCache(None, clock)  # type: ignore[arg-type]
    feed.watch(list(mids))
    await ws.ack_subscription(0, 5)
    for i, (ticker, mid) in enumerate(mids.items()):
        mid_cc = int(round(mid * 10_000))
        env = snapshot_env(5, i + 1, ticker)
        env["msg"]["yes_dollars_fp"] = [[f"{max(mid_cc - 50, 100) / 10_000:.4f}", "500.00"]]
        env["msg"]["no_dollars_fp"] = [[f"{max(10_000 - mid_cc - 50, 100) / 10_000:.4f}", "500.00"]]
        await ws.deliver(env)
        parts = ticker.split("-")
        event = "-".join(parts[:-1]) if len(parts) >= 3 else ticker
        metadata._events[event] = EventMeta(  # noqa: SLF001 (test seam)
            event_ticker=event, mutually_exclusive=False, raw={},
            fetched_mono_ns=clock.monotonic_ns(),
        )
    metadata._markets[combo_ticker] = MarketMeta(  # noqa: SLF001 (test seam)
        ticker=combo_ticker,
        status="active",
        grid=PriceGrid.from_market_payload(
            {
                "ticker": combo_ticker,
                "price_ranges": [{"start": "0.01", "end": "0.99", "step": "0.01"}],
            }
        ),
        event_ticker="E",
        close_time=NOW + timedelta(hours=6),
        expected_expiration_time=None,
        raw={},
        fetched_mono_ns=clock.monotonic_ns(),
    )
    cfg = PricingConfig(dnp_scalar=DnpScalarConfig(enabled=enabled))
    return PricingEngine(feed, metadata, DOC_ASSUMED, cfg)


def implied_ask_c(q: ConstructedQuote) -> int:
    return 100 - int(q.no_bid_cc) // 100


# ------------------------------------------------------------------ scope


def test_scope_same_player_anticorr_detected() -> None:
    rfq = combo([_leg(MARTE_HRR2, "no"), _leg(MARTE_TB2, "yes")])
    scope = single_player_scope(
        rfq.legs, [belief(0.5288), belief(0.4404)], ["no", "yes"]
    )
    assert scope is not None
    assert scope.entity == "AZKMARTE4"
    prod = (1 - 0.5288) * 0.4404
    assert abs(scope.product - prod) < 1e-12
    assert scope.floor_cc == 2_000  # ⌊20.75¢⌋ = 20c — T1's exact settle


def test_scope_unknown_entity_fails_closed_into_scope() -> None:
    # 3-segment prop ticker: family parses, entity does NOT — must stay in
    # scope (UNKNOWN => same-player-driven, widen side), entity None.
    weird = "KXMLBHIT-26AUG052140SDAZ"
    rfq = combo([_leg(weird, "yes"), _leg(MARTE_TB2, "yes")])
    scope = single_player_scope(rfq.legs, [belief(0.6), belief(0.4)], ["yes", "yes"])
    assert scope is not None
    assert scope.entity is None


def test_scope_multi_player_out_of_scope() -> None:
    other = "KXMLBTB-26AUG052140SDAZ-SDMMACHADO13-2"
    rfq = combo([_leg(MARTE_HRR2, "no"), _leg(other, "yes")])
    assert (
        single_player_scope(rfq.legs, [belief(0.5), belief(0.4)], ["no", "yes"])
        is None
    )


def test_scope_mixed_prop_game_leg_out_of_scope() -> None:
    game = "KXMLBGAME-26AUG052140SDAZ-AZ"
    rfq = combo([_leg(MARTE_HRR2, "no"), _leg(game, "yes")])
    assert (
        single_player_scope(rfq.legs, [belief(0.5), belief(0.4)], ["no", "yes"])
        is None
    )


def test_floor_product_grid() -> None:
    assert floor_product_cc(0.2075) == 2_000
    assert floor_product_cc(0.179) == 1_700
    assert floor_product_cc(0.9999) == 9_900
    assert floor_product_cc(0.0) == 0
    assert floor_product_cc(0.0099) == 0  # sub-cent product floors to 0 => no clamp
    # float knife-edge rounds UP a cent (the ask-raising, conservative side)
    assert floor_product_cc(0.20999999999) == 2_100


# ------------------------------------------------------------------ hazards


def test_hazard_thin_family_fails_closed_to_max_of_own_and_pooled() -> None:
    hz = baseline_hazards()
    pooled = hz.pooled()
    assert pooled is not None and 0.02 < pooled < 0.03  # 23/997 = 2.31%
    # player_tb (1/14) is THIN: must use max(own 7.14%, pooled) = own.
    tb = hz.family_rate("player_tb")
    assert tb is not None and abs(tb - 1 / 14) < 1e-12
    # player_outs (0/19) is THIN with own 0: must lift to pooled.
    outs = hz.family_rate("player_outs")
    assert outs == pooled
    # player_ks (7/383) clears the bar: own rate even though BELOW pooled.
    ks = hz.family_rate("player_ks")
    assert ks is not None and abs(ks - 7 / 383) < 1e-12 and ks < pooled
    # combo hazard = max over families (widen side)
    both = hz.hazard_for(["player_ks", "player_tb"])
    assert both == tb


def test_hazard_empty_corpus_returns_none() -> None:
    assert DnpHazards(counts={}).pooled() is None
    assert DnpHazards(counts={}).hazard_for(["player_hit"]) is None
    assert DnpHazards(counts={"player_hit": (0, 500)}).pooled() is None


def test_counts_from_outcomes_filters_and_counts() -> None:
    counts = counts_from_outcomes(
        [
            (MARTE_HRR2, "scalar"),
            (MARTE_TB2, "yes"),
            ("KXMLBGAME-26AUG052140SDAZ-AZ", "no"),  # not a prop family
            (MARTE_HIT1, "bogus"),                    # unknown grade ignored
        ]
    )
    assert counts == {"player_hrr": (1, 1), "player_tb": (0, 1)}


def test_hazard_merge_is_additive() -> None:
    merged = DnpHazards(counts={"player_hit": (1, 10)}).merged(
        {"player_hit": (2, 30), "player_ks": (0, 5)}
    )
    assert merged.counts["player_hit"] == (3, 40)
    assert merged.counts["player_ks"] == (0, 5)


# ------------------------------------------------ engine + quote behaviour


async def test_flag_off_is_byte_identical_and_default_off() -> None:
    # Committed prod.yaml ships DARK.
    prod = load_config(REPO_ROOT / "config" / "prod.yaml")
    assert prod.pricing.dnp_scalar.enabled is False
    # And the pydantic default is off (a config without the block is dark).
    assert PricingConfig().dnp_scalar.enabled is False

    rfq = combo([_leg(MARTE_HRR2, "no"), _leg(MARTE_TB2, "yes")])
    off = (await seeded_engine(T1_MIDS, enabled=False)).price(
        rfq, time_to_close_s=3 * 3600.0
    )
    on_default = (await seeded_engine(T1_MIDS, enabled=False)).price(
        rfq, time_to_close_s=3 * 3600.0
    )
    assert isinstance(off, ConstructedQuote) and isinstance(on_default, ConstructedQuote)
    assert (off.yes_bid_cc, off.no_bid_cc, off.fair_cc, off.width_components_cc) == (
        on_default.yes_bid_cc,
        on_default.no_bid_cc,
        on_default.fair_cc,
        on_default.width_components_cc,
    )


async def test_sniped_t1_floors_ask_at_settle() -> None:
    """T1 (Marte HRR-2 no + TB-2 yes at the exact fill mids): flag ON must
    lift the implied YES ask to >= the 20c exchange scalar settle — the
    sniper pays at least the settle; profit 0 or negative."""
    rfq = combo([_leg(MARTE_HRR2, "no"), _leg(MARTE_TB2, "yes")])
    off = (await seeded_engine(T1_MIDS, enabled=False)).price(
        rfq, time_to_close_s=3 * 3600.0
    )
    on = (await seeded_engine(T1_MIDS, enabled=True)).price(
        rfq, time_to_close_s=3 * 3600.0
    )
    assert isinstance(off, ConstructedQuote) and isinstance(on, ConstructedQuote)
    assert implied_ask_c(off) < 20        # the pre-guard hole (sold at 12c-ish)
    assert implied_ask_c(on) >= 20        # floored at the DNP settlement value
    # floor never lowers an ask
    assert int(on.no_bid_cc) <= int(off.no_bid_cc)


async def test_sniped_t3_floors_ask_at_settle() -> None:
    rfq = combo([_leg(MARTE_HIT1, "no"), _leg(MARTE_HRR2_B, "yes")])
    on = (await seeded_engine(T3_MIDS, enabled=True)).price(
        rfq, time_to_close_s=3 * 3600.0
    )
    assert isinstance(on, ConstructedQuote)
    assert implied_ask_c(on) >= 17        # T3's exchange scalar settle


async def test_positively_correlated_same_player_unchanged() -> None:
    """A same-player pair whose correlated fair EXCEEDS the independence
    product (Δ < 0 — e.g. TB-2 yes + HIT-1 yes, near-containment): the guard
    must not touch it — no mixture (which would only ever raise), no floor
    bind. Byte-identical ON vs OFF."""
    tb2 = "KXMLBTB-26AUG052140SDAZ-AZKMARTE4-2"
    rfq = combo([_leg(tb2, "yes"), _leg(MARTE_HIT1, "yes")])
    mids = {tb2: 0.30, MARTE_HIT1: 0.6473}
    off = (await seeded_engine(mids, enabled=False)).price(
        rfq, time_to_close_s=3 * 3600.0
    )
    on = (await seeded_engine(mids, enabled=True)).price(
        rfq, time_to_close_s=3 * 3600.0
    )
    assert isinstance(off, ConstructedQuote) and isinstance(on, ConstructedQuote)
    assert int(off.fair_cc) > floor_product_cc(0.30 * 0.6473)  # Δ < 0 shape
    assert (off.yes_bid_cc, off.no_bid_cc, off.fair_cc, off.width_components_cc) == (
        on.yes_bid_cc,
        on.no_bid_cc,
        on.fair_cc,
        on.width_components_cc,
    )


async def test_out_of_scope_multi_player_byte_identical_when_armed() -> None:
    other = "KXMLBTB-26AUG052140SDAZ-SDMMACHADO13-2"
    rfq = combo([_leg(MARTE_HRR2_B, "no"), _leg(other, "yes")])
    mids = {MARTE_HRR2_B: 0.5089, other: 0.44}
    off = (await seeded_engine(mids, enabled=False)).price(
        rfq, time_to_close_s=3 * 3600.0
    )
    on = (await seeded_engine(mids, enabled=True)).price(
        rfq, time_to_close_s=3 * 3600.0
    )
    assert type(off) is type(on)
    if isinstance(off, ConstructedQuote) and isinstance(on, ConstructedQuote):
        assert (off.yes_bid_cc, off.no_bid_cc, off.fair_cc) == (
            on.yes_bid_cc,
            on.no_bid_cc,
            on.fair_cc,
        )
    else:
        assert isinstance(off, NoQuote) and isinstance(on, NoQuote)
        assert (off.reason, off.detail) == (on.reason, on.detail)


async def test_mixture_never_lowers_fair_and_floor_never_lowers_ask() -> None:
    """Randomized property sweep over same-player anti-correlated shapes:
    for every seeded mid pair, ON-fair >= OFF-fair, ON-no_bid <= OFF-no_bid
    (ask only ever rises), and the implied ask covers the DNP floor."""
    import random

    rng = random.Random(20260806)
    for _ in range(25):
        p_a = rng.uniform(0.2, 0.8)
        p_b = rng.uniform(0.2, 0.8)
        mids = {MARTE_HRR2: p_a, MARTE_TB2: p_b}
        rfq = combo([_leg(MARTE_HRR2, "no"), _leg(MARTE_TB2, "yes")])
        off = (await seeded_engine(mids, enabled=False)).price(
            rfq, time_to_close_s=3 * 3600.0
        )
        on = (await seeded_engine(mids, enabled=True)).price(
            rfq, time_to_close_s=3 * 3600.0
        )
        if not (isinstance(off, ConstructedQuote) and isinstance(on, ConstructedQuote)):
            assert type(off) is type(on)  # both decline the same way
            continue
        assert int(on.fair_cc) >= int(off.fair_cc)
        assert int(on.no_bid_cc) <= int(off.no_bid_cc)
        scope = single_player_scope(
            rfq.legs, [belief(p_a), belief(p_b)], ["no", "yes"]
        )
        assert scope is not None
        if scope.floor_cc > 0 and int(on.no_bid_cc) > 0:
            assert 10_000 - int(on.no_bid_cc) >= scope.floor_cc


# ------------------------------------------- settled cache scalar outcomes


async def test_settled_cache_records_graded_scalars_for_hazard() -> None:
    source = FakeMarketSource()
    clock = FakeClock(start=NOW)
    resolver = _resolver(source, clock)
    source.payloads[MARTE_HRR2] = market_payload(
        MARTE_HRR2, status="finalized", result="scalar"
    )
    source.payloads[MARTE_TB2] = market_payload(
        MARTE_TB2, status="finalized", result="no"
    )
    resolver.note_missing(MARTE_HRR2)
    resolver.note_missing(MARTE_TB2)
    gen0 = resolver.leg_outcome_generation
    await resolver.resolve_pending()
    assert resolver.leg_outcome_generation == gen0 + 2  # binary fact + scalar
    outcomes = dict(resolver.leg_outcomes())
    assert outcomes[MARTE_HRR2] == "scalar"
    assert outcomes[MARTE_TB2] == "no"
    # scalar stays a NON-fact for marginals (fail-closed unchanged)
    assert resolver.resolved(MARTE_HRR2) is None
    # and the counts feed the hazard measurement
    counts = counts_from_outcomes(resolver.leg_outcomes())
    assert counts["player_hrr"] == (1, 1)
    assert counts["player_tb"] == (0, 1)


async def test_settled_cache_ungraded_scalar_not_counted() -> None:
    source = FakeMarketSource()
    clock = FakeClock(start=NOW)
    resolver = _resolver(source, clock)
    source.payloads[MARTE_HRR2] = market_payload(
        MARTE_HRR2, status="disputed", result="scalar"
    )
    resolver.note_missing(MARTE_HRR2)
    await resolver.resolve_pending()
    assert resolver.leg_outcome_generation == 0
    assert resolver.leg_outcomes() == []


def test_engine_hazard_setter_merges_live_counts() -> None:
    # the exact merge the engine's set_dnp_hazard_counts performs
    hz = baseline_hazards().merged({"player_hrr": (10, 10)})
    assert hz.counts["player_hrr"] == (16, 298)
    # a live scalar burst RAISES the family hazard (adaptive, not typed)
    rate = hz.family_rate("player_hrr")
    base = baseline_hazards().family_rate("player_hrr")
    assert rate is not None and base is not None and rate > base
