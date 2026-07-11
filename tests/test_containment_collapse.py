"""CONTAINMENT-IN-LARGER-COMBO collapse (2026-07-11): the shapes that used to
decline UNKNOWN "logical containment pair inside a larger combo: not modeled"
(194 resolved decliners on the WC tape: 1h_btts⟹ft_btts x127, ml⟹over-0.5 x70,
1h_over_N⟹ft_over_N x29) now collapse like nested bands — each implied
SUPERSET leg drops (the pair's joint IS the subset leg's selected marginal,
price_containment semantics), each {A no, B yes} window pair becomes a band
super-leg P(B) − P(A) — and the reduced set prices through the ordinary
sgp/copula machinery.

Covers all four side rules e2e, the multi-pair WC shape, N-leg reduction
correctness against the independently-priced reduced combo, the
previously-priced-shapes-unchanged regression (the classifier output — the
ONLY change surface — must be untouched for every shape that priced before),
IMPOSSIBLE routing, both inverted-marginal treatments (containment Fréchet
clamp vs band NoQuote), and the fail-closed guards (window-band same-game
companion, double collapse role, cyclic implication)."""

from __future__ import annotations

from combomaker.core.conventions import DOC_ASSUMED
from combomaker.core.money import cc_from_prob
from combomaker.core.reasons import ReasonCode
from combomaker.ops.config import (
    MarginTotalConfig,
    MlbRunsConfig,
    PricingConfig,
    StructuralConfig,
)
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.legs import KalshiBookSource
from combomaker.pricing.quote import ConstructedQuote, NoQuote
from combomaker.pricing.relationships import (
    RelationshipKind,
    _collapse_containments,
    classify_legs,
)
from combomaker.rfq.models import Rfq
from tests.test_feed import snapshot_env
from tests.test_filters import Harness
from tests.test_pricing_engine import combo, seed_event
from tests.test_relationships import ExplodingProvider, MappingProvider, leg

# Game 1 (MEXENG) — the containment families' home game.
FH_BTTS = "KXWC1HBTTS-26JUL05MEXENG-BTTS"
FT_BTTS = "KXWCBTTS-26JUL05MEXENG-BTTS"
ML_MEX = "KXWCGAME-26JUL05MEXENG-MEX"
TOT1 = "KXWCTOTAL-26JUL05MEXENG-1"     # over 0.5 (>=1 goal)
TOT3 = "KXWCTOTAL-26JUL05MEXENG-3"
# Game 2 (ARGEGY) — cross-game companions.
ML_ARG = "KXWCGAME-26JUL06ARGEGY-ARG"
FH_BTTS2 = "KXWC1HBTTS-26JUL06ARGEGY-BTTS"
FT_BTTS2 = "KXWCBTTS-26JUL06ARGEGY-BTTS"
TOT1_2 = "KXWCTOTAL-26JUL06ARGEGY-1"
# Match-corner ladder (one event) for the same-side rung chain.
MC_EV = "KXWCCORNERS-26JUL10ESPBEL"
MC_6 = f"{MC_EV}-6"
MC_8 = f"{MC_EV}-8"
MC_11 = f"{MC_EV}-11"


def ev(ticker: str) -> str:
    return ticker.rsplit("-", 1)[0]


def rfq_of(*legs: tuple[str, str]) -> Rfq:
    return combo(
        [{"market_ticker": t, "side": s, "event_ticker": ev(t)} for t, s in legs]
    )


# (ticker, yes bid, no bid): one level each side, equal size => micro == mid.
BOOKS: list[tuple[str, str, str]] = [
    (FH_BTTS, "0.2800", "0.7000"),   # p = 0.29
    (FT_BTTS, "0.5500", "0.4300"),   # p = 0.56
    (ML_MEX, "0.6000", "0.3800"),    # p = 0.61
    (TOT1, "0.9000", "0.0800"),      # p = 0.91
    (TOT3, "0.5000", "0.4800"),      # p = 0.51
    (ML_ARG, "0.6200", "0.3600"),    # p = 0.63
    (FH_BTTS2, "0.3000", "0.6800"),  # p = 0.31
    (FT_BTTS2, "0.5800", "0.4000"),  # p = 0.59
    (TOT1_2, "0.8800", "0.1000"),    # p = 0.89
]
# Subset priced ABOVE its superset (noisy books): the containment Fréchet
# clamp / the inverted-band decline.
INVERTED_BOOKS: list[tuple[str, str, str]] = [
    (FH_BTTS, "0.6000", "0.3800"),   # p = 0.61  (subset)
    (FT_BTTS, "0.4000", "0.5800"),   # p = 0.41  (superset, BELOW subset)
    (ML_ARG, "0.6200", "0.3600"),
    (TOT1_2, "0.8800", "0.1000"),
]


async def seed_books(h: Harness, books: list[tuple[str, str, str]]) -> None:
    h.feed.watch([t for t, _, _ in books])
    await h.ws.ack_subscription(0, 5)
    for seq, (t, yes_px, no_px) in enumerate(books, start=1):
        env = snapshot_env(5, seq, t)
        env["msg"]["yes_dollars_fp"] = [[yes_px, "50.00"]]
        env["msg"]["no_dollars_fp"] = [[no_px, "50.00"]]
        await h.ws.deliver(env)


async def engine_with(
    books: list[tuple[str, str, str]], config: PricingConfig | None = None
) -> tuple[PricingEngine, Harness]:
    h = Harness()
    await seed_books(h, books)
    h.with_meta("KXMVE-C1")
    engine = PricingEngine(h.feed, h.metadata, DOC_ASSUMED, config or PricingConfig())
    return engine, h


def marg(h: Harness, ticker: str) -> float:
    belief = KalshiBookSource(h.feed).marginal(ticker)
    assert belief is not None, ticker
    return belief.p


# --- classifier: the collapse plan ------------------------------------------------


def test_multi_pair_combo_records_both_pairs() -> None:
    """The real WC decliner shape: one combo holding TWO containment pairs
    (ml⟹over-0.5 in game 1, 1h_btts⟹ft_btts in game 2) — both recorded, both
    supersets to drop."""
    legs = (
        leg(ML_MEX, ev(ML_MEX), "yes"),
        leg(TOT1, ev(TOT1), "yes"),
        leg(FH_BTTS2, ev(FH_BTTS2), "yes"),
        leg(FT_BTTS2, ev(FT_BTTS2), "yes"),
    )
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.CONTAINMENT
    assert rel.containment is None
    # Recording order follows family order (btts family runs first) — the
    # collapse is order-independent, so assert the SET of pairs.
    assert set(rel.containments) == {(0, 1), (2, 3)}
    assert rel.bands == ()
    assert rel.same_event_groups == ((0, 1), (2, 3))


def test_window_pair_joins_the_collapse_plan_as_band() -> None:
    """{1H no, FT yes} alongside a PINNED containment pair: the window pair is
    recorded as a band (low = FT superset-YES leg, high = 1H subset-NO leg),
    the nested-band mirror P(B) − P(A)."""
    legs = (
        leg(FH_BTTS, ev(FH_BTTS), "no"),
        leg(FT_BTTS, ev(FT_BTTS), "yes"),
        leg(ML_ARG, ev(ML_ARG), "yes"),
        leg(TOT1_2, ev(TOT1_2), "yes"),
    )
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.CONTAINMENT
    assert rel.containments == ((2, 3),)
    assert rel.bands == ((1, 0),)


def test_window_pair_with_same_game_kept_companion_declines_unknown() -> None:
    """A window band super-leg is non-monotone in the latent count, so a KEPT
    same-game companion (the ML leg survives its own pair's collapse in the
    SAME game) is the unmeasured band-vs-neighbour correlation — UNKNOWN,
    the post-collapse mirror of the NESTED_BAND companion guard."""
    legs = (
        leg(FH_BTTS, ev(FH_BTTS), "no"),
        leg(FT_BTTS, ev(FT_BTTS), "yes"),
        leg(ML_MEX, ev(ML_MEX), "yes"),
        leg(TOT1, ev(TOT1), "yes"),
    )
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.UNKNOWN
    assert any("band-vs-neighbour" in n for n in rel.notes)


def test_window_pair_without_pinned_containment_stays_ok() -> None:
    """DON'T OVER-COLLAPSE (bit-identical invariant): {1H no, FT yes} in a
    combo with NO recorded containment pair priced via the copula before this
    change and must keep that exact path — no collapse, no band."""
    legs = (
        leg(FH_BTTS, ev(FH_BTTS), "no"),
        leg(FT_BTTS, ev(FT_BTTS), "yes"),
        leg(TOT3, ev(TOT3), "yes"),
    )
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.OK
    assert rel.containments == () and rel.bands == ()
    assert rel.same_event_groups == ((0, 1, 2),)


def test_cyclic_implication_without_kept_witness_declines_unknown() -> None:
    """Fail-closed guard on the collapse plan itself: a mutual A⊆B⊆A pair set
    would drop BOTH legs and silently lose the constraint — UNKNOWN. (No
    shipped family can emit this today; the guard protects future families.)"""
    legs = [
        leg(FH_BTTS, ev(FH_BTTS), "yes"),
        leg(FT_BTTS, ev(FT_BTTS), "yes"),
        leg(TOT3, ev(TOT3), "yes"),
    ]
    rel = _collapse_containments(
        legs, [(0, 1), (1, 0)], [], [], ["G", "G", "G"], []
    )
    assert rel.kind is RelationshipKind.UNKNOWN
    assert any("witness" in n for n in rel.notes)


def test_previously_priced_shapes_keep_their_classification() -> None:
    """Bit-identical regression at the change surface: the classifier is the
    ONLY module deciding which pricing path a combo takes, so every shape that
    priced before must classify EXACTLY as before (kind + fields) — the engine
    then runs untouched code on it."""
    # Bare 2-leg containment: still the single-pair price_containment path.
    rel = classify_legs(
        (leg(FH_BTTS, ev(FH_BTTS), "yes"), leg(FT_BTTS, ev(FT_BTTS), "yes")),
        ExplodingProvider(),
    )
    assert rel.kind is RelationshipKind.CONTAINMENT
    assert rel.containment == (0, 1)
    assert rel.containments == () and rel.bands == ()
    # Bare 2-leg window pair: OK (copula), never a band.
    rel = classify_legs(
        (leg(FH_BTTS, ev(FH_BTTS), "no"), leg(FT_BTTS, ev(FT_BTTS), "yes")),
        ExplodingProvider(),
    )
    assert rel.kind is RelationshipKind.OK
    assert rel.same_event_groups == ((0, 1),)
    # Bare nested band: NESTED_BAND with empty containments.
    rel = classify_legs(
        (leg(MC_8, MC_EV, "yes"), leg(MC_11, MC_EV, "no")),
        MappingProvider({MC_EV: False}),
    )
    assert rel.kind is RelationshipKind.NESTED_BAND
    assert rel.bands == ((0, 1),) and rel.containments == ()
    # Clean cross-game combo: OK, ungrouped.
    rel = classify_legs(
        (leg(ML_MEX, ev(ML_MEX), "yes"), leg(ML_ARG, ev(ML_ARG), "yes")),
        ExplodingProvider(),
    )
    assert rel.kind is RelationshipKind.OK
    assert rel.same_event_groups == ()


# --- engine: the four side rules, e2e ----------------------------------------------


async def test_buried_yes_yes_prices_subset_times_companion() -> None:
    """YES-A + YES-B + cross-game companion: the implied FT leg drops, fair =
    P(1H-BTTS) x P(companion) exactly (cross_event_rho = 0)."""
    engine, h = await engine_with(BOOKS)
    result = engine.price(
        rfq_of((FH_BTTS, "yes"), (FT_BTTS, "yes"), (ML_ARG, "yes")),
        time_to_close_s=100_000,
    )
    assert isinstance(result, ConstructedQuote), result
    assert result.fair_cc == cc_from_prob(marg(h, FH_BTTS) * marg(h, ML_ARG))


async def test_buried_no_no_prices_superset_complement_times_companion() -> None:
    """NO-A + NO-B: ¬A∧¬B = ¬B, so the 1H-no leg drops and fair =
    (1 − P(FT-BTTS)) x P(companion)."""
    engine, h = await engine_with(BOOKS)
    result = engine.price(
        rfq_of((FH_BTTS, "no"), (FT_BTTS, "no"), (ML_ARG, "yes")),
        time_to_close_s=100_000,
    )
    assert isinstance(result, ConstructedQuote), result
    expected = (1.0 - marg(h, FT_BTTS)) * marg(h, ML_ARG)
    assert result.fair_cc == cc_from_prob(expected)


async def test_buried_yes_no_is_impossible_and_farms() -> None:
    """YES-A + NO-B = A∧¬B = ∅: the EXISTING IMPOSSIBLE handling owns it at
    any combo size (farmable stays as the tautology rules say — soccer scoring
    tautology ⇒ farmed certain-NO quote, fair 0, yes_bid 0)."""
    engine, _h = await engine_with(BOOKS)
    result = engine.price(
        rfq_of((FH_BTTS, "yes"), (FT_BTTS, "no"), (ML_ARG, "yes")),
        time_to_close_s=100_000,
    )
    assert isinstance(result, ConstructedQuote), result
    assert result.farmed is True
    assert result.yes_bid_cc == 0
    assert result.fair_cc == 0


async def test_window_band_prices_exact_difference_product() -> None:
    """NO-A + YES-B: the window ¬A∧B = P(B) − P(A), a band super-leg exactly
    like a nested band; alongside a cross-game containment pair the fair is
    (P(FT) − P(1H)) x P(ml subset)."""
    engine, h = await engine_with(BOOKS)
    result = engine.price(
        rfq_of((FH_BTTS, "no"), (FT_BTTS, "yes"), (ML_ARG, "yes"), (TOT1_2, "yes")),
        time_to_close_s=100_000,
    )
    assert isinstance(result, ConstructedQuote), result
    expected = (marg(h, FT_BTTS) - marg(h, FH_BTTS)) * marg(h, ML_ARG)
    assert result.fair_cc == cc_from_prob(expected)


# --- engine: multi-pair, N-leg reduction, inversions, chains ------------------------


async def test_multi_pair_wc_combo_prices_product_of_subsets() -> None:
    """The 4-leg two-pair WC decliner: both supersets drop, fair =
    P(ml) x P(1h_btts) exactly (kept legs are cross-game)."""
    engine, h = await engine_with(BOOKS)
    result = engine.price(
        rfq_of((ML_MEX, "yes"), (TOT1, "yes"), (FH_BTTS2, "yes"), (FT_BTTS2, "yes")),
        time_to_close_s=100_000,
    )
    assert isinstance(result, ConstructedQuote), result
    assert result.fair_cc == cc_from_prob(marg(h, ML_MEX) * marg(h, FH_BTTS2))


async def test_same_game_reduction_equals_reduced_combo_to_the_centicent() -> None:
    """N-leg reduction correctness: the 4-leg same-game two-pair combo must
    price EXACTLY like the hand-reduced 2-leg combo [ml, 1h_btts] through the
    same engine (structural disabled on both sides so the reference 2-leg
    takes the copula, the only joint the collapse path uses)."""
    cfg = PricingConfig(
        structural=StructuralConfig(enabled=False),
        margin_total=MarginTotalConfig(enabled_sports=[]),
        mlb_runs=MlbRunsConfig(enabled=False),
    )
    engine, _h = await engine_with(BOOKS, cfg)
    full = engine.price(
        rfq_of((ML_MEX, "yes"), (TOT1, "yes"), (FH_BTTS, "yes"), (FT_BTTS, "yes")),
        time_to_close_s=100_000,
    )
    reduced = engine.price(
        rfq_of((ML_MEX, "yes"), (FH_BTTS, "yes")), time_to_close_s=100_000
    )
    assert isinstance(full, ConstructedQuote), full
    assert isinstance(reduced, ConstructedQuote), reduced
    assert full.fair_cc == reduced.fair_cc


async def test_inverted_containment_marginals_frechet_clamp() -> None:
    """Noisy books pricing the subset ABOVE its superset are NOT a decline:
    mirror price_containment's clamp_to_frechet on the bare pair — the kept
    subset's marginal caps at the superset's, fair = P(FT) x P(companion)."""
    engine, h = await engine_with(INVERTED_BOOKS)
    result = engine.price(
        rfq_of((FH_BTTS, "yes"), (FT_BTTS, "yes"), (ML_ARG, "yes")),
        time_to_close_s=100_000,
    )
    assert isinstance(result, ConstructedQuote), result
    p_sub, p_sup = marg(h, FH_BTTS), marg(h, FT_BTTS)
    assert p_sub > p_sup  # the probe is actually inverted
    assert result.fair_cc == cc_from_prob(p_sup * marg(h, ML_ARG))


async def test_inverted_window_band_declines() -> None:
    """A window band whose difference goes non-positive (books contradict the
    containment ordering) declines — the exact _price_nested_bands inverted-mid
    rule, never a clamped-to-0 fair."""
    engine, _h = await engine_with(INVERTED_BOOKS)
    result = engine.price(
        rfq_of((FH_BTTS, "no"), (FT_BTTS, "yes"), (ML_ARG, "yes"), (TOT1_2, "yes")),
        time_to_close_s=100_000,
    )
    assert isinstance(result, NoQuote)
    assert result.reason is ReasonCode.SKIP_PRICING_FAILED


async def test_three_rung_chain_reduces_to_highest_rung() -> None:
    """Transitive chain over-11 ⊆ over-8 ⊆ over-6, all YES: iterative pairwise
    collapse drops BOTH implied lower rungs; the whole combo reduces to the one
    minimal leg — fair = P(over-11) exactly."""
    books = [(MC_6, "0.8500", "0.1300"), (MC_8, "0.6000", "0.3800"),
             (MC_11, "0.2500", "0.7300")]
    engine, h = await engine_with(books)
    seed_event(h, MC_EV, exclusive=False)  # three legs share the ladder event
    result = engine.price(
        rfq_of((MC_6, "yes"), (MC_8, "yes"), (MC_11, "yes")),
        time_to_close_s=100_000,
    )
    assert isinstance(result, ConstructedQuote), result
    assert result.fair_cc == cc_from_prob(marg(h, MC_11))


async def test_collapse_guard_decline_reaches_engine_as_unknown_noquote() -> None:
    """UNKNOWN still means no-quote: the window-band-with-kept-companion guard
    (genuinely unmodeled correlation) declines at the engine boundary with the
    classifier-unknown reason — the collapse never over-reaches."""
    engine, _h = await engine_with(BOOKS)
    result = engine.price(
        rfq_of((FH_BTTS, "no"), (FT_BTTS, "yes"), (ML_MEX, "yes"), (TOT1, "yes")),
        time_to_close_s=100_000,
    )
    assert isinstance(result, NoQuote)
    assert result.reason is ReasonCode.SKIP_CLASSIFIER_UNKNOWN
