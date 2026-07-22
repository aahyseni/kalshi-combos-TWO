"""WIRE-1..WIRE-4 (2026-07-11): universal exact containment windows, the
soccer spread⟹win family, S41 exact cells, the spread×total impossibility
rule, and the embedded same-player conditional collapse.

WIRE-1 — for ANY detected strict-containment pair A ⟹ B in the {A no, B yes}
mix, the joint is the EXACT window arithmetic P(B) − P(A) (band super-leg,
u = u_A + u_B, inverted books ⇒ NoQuote) — bare 2-leg AND embedded, one rule:
S1-ny, S2-ny, S3-ny, S12-ny (via the NEW scope-matched soccer spread⟹win
family), S33-ny (MLB win-not-cover).

WIRE-2 — ('tb', N, 'hrr', 1) exact cells (TB ⟹ ≥1 hit ⟹ HRR ≥ 1), wired from
a RE-RUN of the same-player measurement export (job 24844262 wire2, verified
== 1.0 pooled AND 2021-25 on the 1,033,852 batter-game population).

WIRE-3 — (1h_)spread cover-by-N YES × total over-(M−0.5) NO, M ≤ N, is
logically impossible (S7/S8/S13/S34); farmable ONLY for soccer SAME-scope
pairs (S7 1H×1H, S13 FT×FT); S8 cross-scope farmable=False per the V2 ruling
(two-official-records lemma unverified), MLB always False (48h rain scalar).

WIRE-4 — same-player MEASURED-conditional pairs inside >2-leg combos collapse
to a super-leg carrying the bare path's 2-leg conditional joint (all four
side mixes); the bare 2-leg path stays bit-identical. V2 REFUTATION
(2026-07-11): the super-leg is priced at side "yes" under the kept leg's
ticker, so a SAME-GAME companion sees it through the kept leg's YES-side rho
— whose sign INVERTS for NO-side mixes (live counterexample: HIT3-no x
HR1-no x own-ML-yes engine 0.4183 vs 0.3451 trivariate truth, +7.32c).
Mandated remedy: conditional pairs carry the SAME same-game-companion
isolation guard as window bands — same-game companion ⇒ UNKNOWN decline;
cross-game companions (ρ = 0) stay priceable.
"""

from __future__ import annotations

from combomaker.core.money import cc_from_prob
from combomaker.core.reasons import ReasonCode
from combomaker.pricing.conditionals_mlb import SAME_PLAYER_CONDITIONALS
from combomaker.pricing.joint import price_joint_matrices
from combomaker.pricing.legs import LegBelief
from combomaker.pricing.quote import ConstructedQuote, NoQuote
from combomaker.pricing.relationships import RelationshipKind, classify_legs
from combomaker.pricing.sgp import build_sgp_correlation
from combomaker.rfq.models import RfqLeg
from tests.test_containment_collapse import engine_with, marg, rfq_of
from tests.test_mlb_containments import shipped_params
from tests.test_relationships import ExplodingProvider

# --- Soccer game ESPBEL: the spread⟹win / spread×total shapes ---------------------
SP_ESP2 = "KXWCSPREAD-26JUL10ESPBEL-ESP2"
SP_ESP3 = "KXWCSPREAD-26JUL10ESPBEL-ESP3"
SP_ESP0 = "KXWCSPREAD-26JUL10ESPBEL-ESP0"
SP_BARE_LINE = "KXWCSPREAD-26JUL10ESPBEL-2"      # no team code: unparseable
ML_ESP = "KXWCGAME-26JUL10ESPBEL-ESP"
ML_BEL = "KXWCGAME-26JUL10ESPBEL-BEL"
ML_TIE = "KXWCGAME-26JUL10ESPBEL-TIE"
FT_TOT1 = "KXWCTOTAL-26JUL10ESPBEL-1"
FT_TOT2 = "KXWCTOTAL-26JUL10ESPBEL-2"
FT_TOT3 = "KXWCTOTAL-26JUL10ESPBEL-3"
FH_TOT2 = "KXWC1HTOTAL-26JUL10ESPBEL-2"
FH_SP_ESP2 = "KXWC1HSPREAD-26JUL10ESPBEL-ESP2"
FH_ML_ESP = "KXWC1H-26JUL10ESPBEL-ESP"
# Cross-game companion (ARGSUI).
ML_ARG = "KXWCGAME-26JUL11ARGSUI-ARG"
# Other-game spread (no relation to ESPBEL legs).
SP_OTHER = "KXWCSPREAD-26JUL11ARGSUI-ARG2"

# --- MLB game NYYTB: S33/S34 --------------------------------------------------------
MLB_SP2 = "KXMLBSPREAD-26JUL081840NYYTB-NYY2"
MLB_SP4 = "KXMLBSPREAD-26JUL081840NYYTB-NYY4"
MLB_ML = "KXMLBGAME-26JUL081840NYYTB-NYY"
MLB_TOT3 = "KXMLBTOTAL-26JUL081840NYYTB-3"
MLB_TOT4 = "KXMLBTOTAL-26JUL081840NYYTB-4"

# --- NFL pair (sport gate probe) ------------------------------------------------------
NFL_SP = "KXNFLSPREAD-25SEP04DALPHI-DAL3"
NFL_TOT = "KXNFLTOTAL-25SEP04DALPHI-3"

# --- MLB same-player (real live shapes, COL @ SF) -------------------------------------
_G = "26JUL092145COLSF"
HIT3 = f"KXMLBHIT-{_G}-COLHGOODMAN15-3"
HR1 = f"KXMLBHR-{_G}-COLHGOODMAN15-1"
TB2 = f"KXMLBTB-{_G}-COLHGOODMAN15-2"
TB5 = f"KXMLBTB-{_G}-COLHGOODMAN15-5"
TB7 = f"KXMLBTB-{_G}-COLHGOODMAN15-7"
HRR1 = f"KXMLBHRR-{_G}-COLHGOODMAN15-1"
TB2_TEAMMATE = f"KXMLBTB-{_G}-COLETOVAR14-2"
ML_OWN = f"KXMLBGAME-{_G}-COL"                  # the batter's OWN game's ML
MLB_ML_OTHER = "KXMLBGAME-26JUL101610AZSD-SD"   # cross-game companion


def _leg(mt: str, side: str = "yes") -> RfqLeg:
    return RfqLeg(mt, "-".join(mt.split("-")[:2]), side, None)


# Books: one level each side, equal size => micro == mid.
BOOKS: list[tuple[str, str, str]] = [
    (SP_ESP2, "0.3400", "0.6400"),    # p = 0.35 (cover by 2)
    (ML_ESP, "0.6900", "0.2900"),     # p = 0.70 (win)
    (FT_TOT1, "0.9100", "0.0700"),    # p = 0.92 (over 0.5)
    (FH_TOT2, "0.2900", "0.6900"),    # p = 0.30 (1H over 1.5)
    (FT_TOT2, "0.6500", "0.3300"),    # p = 0.66 (FT over 1.5)
    (ML_ARG, "0.6200", "0.3600"),     # p = 0.63 (companion)
    (MLB_SP2, "0.4400", "0.5400"),    # p = 0.45 (MLB cover)
    (MLB_ML, "0.5700", "0.4100"),     # p = 0.58 (MLB win)
    (HIT3, "0.2000", "0.7800"),       # p = 0.21
    (HR1, "0.1400", "0.8400"),        # p = 0.15
    (TB2, "0.4400", "0.5400"),        # p = 0.45
    (TB5, "0.1000", "0.8800"),        # p = 0.11
    (HRR1, "0.6300", "0.3500"),       # p = 0.64
    (TB2_TEAMMATE, "0.4000", "0.5800"),  # p = 0.41
    (ML_OWN, "0.5700", "0.4100"),        # p = 0.58 (V2 counterexample book)
    (MLB_ML_OTHER, "0.5500", "0.4300"),  # p = 0.56
]
# Window inverted: books price the subset ABOVE its superset.
INVERTED_BOOKS: list[tuple[str, str, str]] = [
    (SP_ESP2, "0.7400", "0.2400"),    # p = 0.75 (cover ABOVE win)
    (ML_ESP, "0.5900", "0.3900"),     # p = 0.60
]


# =============================== WIRE-1: classifier =================================


def test_soccer_1h_spread_1h_winner_all_four_mixes() -> None:
    """Scope nesting: 1H spread pairs the 1H winner (KXWC1H) — the full
    ``_containment_sign`` matrix, incl. the exact window."""
    yy = classify_legs((_leg(FH_SP_ESP2), _leg(FH_ML_ESP)), ExplodingProvider())
    assert yy.kind is RelationshipKind.CONTAINMENT
    assert yy.containment == (0, 1)  # subset = the 1H spread (cover) leg
    yn = classify_legs((_leg(FH_SP_ESP2), _leg(FH_ML_ESP, "no")), ExplodingProvider())
    assert yn.kind is RelationshipKind.IMPOSSIBLE
    assert yn.farmable is True  # soccer one-scoreline tautology
    nn = classify_legs(
        (_leg(FH_SP_ESP2, "no"), _leg(FH_ML_ESP, "no")), ExplodingProvider()
    )
    assert nn.kind is RelationshipKind.CONTAINMENT
    assert nn.containment == (1, 0)  # subset = the 1H-winner-NO leg
    ny = classify_legs((_leg(FH_SP_ESP2, "no"), _leg(FH_ML_ESP)), ExplodingProvider())
    assert ny.kind is RelationshipKind.NESTED_BAND
    assert ny.bands == ((1, 0),)  # window = P(1H win) − P(1H cover)


def test_soccer_spread_win_cross_scope_is_never_claimed() -> None:
    """1H spread × FT winner and FT spread × 1H winner carry NO containment
    (a 1H lead does not force the FT result and vice versa) — both fall to
    the copula's measured priors."""
    rel = classify_legs((_leg(FH_SP_ESP2, "no"), _leg(ML_ESP)), ExplodingProvider())
    assert rel.kind is RelationshipKind.OK
    rel = classify_legs((_leg(SP_ESP2, "no"), _leg(FH_ML_ESP)), ExplodingProvider())
    assert rel.kind is RelationshipKind.OK


def test_soccer_spread_win_refusals_fail_closed() -> None:
    """Draw side, line-0, unparseable suffix, different game: never a
    containment claim — all fall through (defect-#3 discipline). The
    other-suffix yes+yes pair is NO longer OK: it is the pinned S17 exclusion
    (cover ⟹ win excludes the opponent's win), intercepted by the FIX-4
    taxonomy tripwire as a farmable=False DECLINE — defect-#3 still holds
    because nothing is ever priced or farmed off the unproven team parse."""
    for legs in (
        (_leg(SP_ESP2), _leg(ML_TIE, "no")),     # draw side is never implied
        (_leg(SP_ESP0), _leg(ML_ESP, "no")),     # line 0 proves nothing
        (_leg(SP_BARE_LINE), _leg(ML_ESP, "no")),  # unparseable spread suffix
        (_leg(SP_OTHER, "no"), _leg(ML_ESP)),    # different game
    ):
        rel = classify_legs(legs, ExplodingProvider())
        assert rel.kind is RelationshipKind.OK, legs[0].market_ticker
    s17 = classify_legs((_leg(SP_ESP2), _leg(ML_BEL)), ExplodingProvider())
    assert s17.kind is RelationshipKind.IMPOSSIBLE
    assert s17.farmable is False
    assert any("taxonomy-impossible tripwire: S17" in n for n in s17.notes)


def test_s1_moneyline_no_orientations_are_wired_defensively() -> None:
    """Family 2's NO orientations (exchange-blocked today): {win no, over-0.5
    yes} = a goal but no win — the exact window; {win no, over-0.5 no} —
    containment with the total-NO leg as effective subset."""
    ny = classify_legs(
        (_leg(ML_ESP, "no"), _leg(FT_TOT1)), ExplodingProvider()
    )
    assert ny.kind is RelationshipKind.NESTED_BAND
    assert ny.bands == ((1, 0),)
    nn = classify_legs(
        (_leg(ML_ESP, "no"), _leg(FT_TOT1, "no")), ExplodingProvider()
    )
    assert nn.kind is RelationshipKind.CONTAINMENT
    assert nn.containment == (1, 0)


# =============================== WIRE-1: engine e2e =================================


async def test_bare_s12_window_prices_exact_difference() -> None:
    """S12-ny (the 637-combo tape cell): fair = P(win) − P(cover), no ρ."""
    engine, h = await engine_with(BOOKS)
    result = engine.price(
        rfq_of((SP_ESP2, "no"), (ML_ESP, "yes")), time_to_close_s=100_000
    )
    assert isinstance(result, ConstructedQuote), result
    assert result.fair_cc == cc_from_prob(marg(h, ML_ESP) - marg(h, SP_ESP2))


async def test_bare_s12_containment_prices_p_cover() -> None:
    """S12-yy: cover ⟹ win, fair = P(cover) exactly."""
    engine, h = await engine_with(BOOKS)
    result = engine.price(
        rfq_of((SP_ESP2, "yes"), (ML_ESP, "yes")), time_to_close_s=100_000
    )
    assert isinstance(result, ConstructedQuote), result
    assert result.fair_cc == cc_from_prob(marg(h, SP_ESP2))


async def test_bare_s33_mlb_window_prices_exact_difference() -> None:
    """S33-ny (MLB win-not-cover): fair = P(win) − P(cover) exactly — was the
    :same ±0.95 copula route."""
    engine, h = await engine_with(BOOKS)
    result = engine.price(
        rfq_of((MLB_SP2, "no"), (MLB_ML, "yes")), time_to_close_s=100_000
    )
    assert isinstance(result, ConstructedQuote), result
    assert result.fair_cc == cc_from_prob(marg(h, MLB_ML) - marg(h, MLB_SP2))


async def test_bare_s3_window_prices_exact_difference() -> None:
    """S3-ny (the 379-combo tape cell): fair = P(FT-over-N) − P(1H-over-N)."""
    engine, h = await engine_with(BOOKS)
    result = engine.price(
        rfq_of((FH_TOT2, "no"), (FT_TOT2, "yes")), time_to_close_s=100_000
    )
    assert isinstance(result, ConstructedQuote), result
    assert result.fair_cc == cc_from_prob(marg(h, FT_TOT2) - marg(h, FH_TOT2))


async def test_embedded_window_with_cross_game_companion_prices_product() -> None:
    """The SAME rule embedded: window super-leg × cross-game companion —
    fair = (P(win) − P(cover)) × P(companion) (cross_event_rho = 0)."""
    engine, h = await engine_with(BOOKS)
    result = engine.price(
        rfq_of((SP_ESP2, "no"), (ML_ESP, "yes"), (ML_ARG, "yes")),
        time_to_close_s=100_000,
    )
    assert isinstance(result, ConstructedQuote), result
    expected = (marg(h, ML_ESP) - marg(h, SP_ESP2)) * marg(h, ML_ARG)
    assert result.fair_cc == cc_from_prob(expected)


async def test_inverted_window_declines_noquote() -> None:
    """Books pricing the cover ABOVE the win contradict the containment
    ordering ⇒ NoQuote (the nested-band inverted-mid rule), never a
    clamped-to-0 fair."""
    engine, _h = await engine_with(INVERTED_BOOKS)
    result = engine.price(
        rfq_of((SP_ESP2, "no"), (ML_ESP, "yes")), time_to_close_s=100_000
    )
    assert isinstance(result, NoQuote)
    assert result.reason is ReasonCode.SKIP_PRICING_FAILED


# =============================== WIRE-2: S41 cells ==================================


def test_tb_hrr1_exact_cells_wired_with_measured_ns() -> None:
    """('tb', N, 'hrr', 1) == 1.0 exact for N 2..8; the re-run n's for 2..6
    reproduce the existing tb-row conditioning counts EXACTLY (same join,
    same population — the cross-check that the re-run is the same export)."""
    expected_n = {2: 340_876, 3: 195_319, 4: 133_796, 5: 64_038, 6: 31_347,
                  7: 14_744, 8: 8_813}
    for rung, n in expected_n.items():
        cell = SAME_PLAYER_CONDITIONALS[("tb", rung, "hrr", 1)]
        assert cell == (1.0, n, "exact"), (rung, cell)
        if rung <= 6:  # rows that existed before WIRE-2 share the denominator
            assert SAME_PLAYER_CONDITIONALS[("tb", rung, "hit", 1)][1] == n


def test_preexisting_cells_untouched_and_table_size() -> None:
    """Pre-existing cells stay byte-identical (spot checks on the hit⟹hrr /
    hr⟹hrr exact cells earlier passes singled out) and the table grew by the
    OUTS/RBI/SB wire (2026-07-22): 233 + 70 (7 exact hr⟹rbi / rbi⟹hrr cells +
    63 measured RBI/SB same-player cells) = 303 cells / 77 + 7 = 84 exact."""
    assert SAME_PLAYER_CONDITIONALS[("hit", 2, "hrr", 2)] == (1.0, 212_507, "exact")
    assert SAME_PLAYER_CONDITIONALS[("hit", 3, "hrr", 3)] == (1.0, 48_375, "exact")
    assert SAME_PLAYER_CONDITIONALS[("hr", 1, "hrr", 2)] == (1.0, 101_186, "exact")
    assert SAME_PLAYER_CONDITIONALS[("hr", 1, "hrr", 3)] == (1.0, 101_186, "exact")
    assert SAME_PLAYER_CONDITIONALS[("hr", 2, "hrr", 5)] == (1.0, 6_195, "exact")
    # OUTS/RBI/SB wire spot checks (2026-07-22): the new exact containments.
    assert SAME_PLAYER_CONDITIONALS[("hr", 1, "rbi", 1)] == (1.0, 100_369, "exact")
    assert SAME_PLAYER_CONDITIONALS[("rbi", 1, "hrr", 1)] == (1.0, 277_025, "exact")
    assert SAME_PLAYER_CONDITIONALS[("sb", 1, "hit", 1)] == (0.828043, 51_321, "measured")
    assert len(SAME_PLAYER_CONDITIONALS) == 303
    n_exact = sum(1 for v in SAME_PLAYER_CONDITIONALS.values() if v[2] == "exact")
    assert n_exact == 84


def test_s41_classifier_verdicts() -> None:
    """TB-N × HRR-1 same player: yy containment (subset = TB), yn IMPOSSIBLE
    never farmable (MLB scalar settlement), nn containment (subset = the
    HRR-NO leg), ny OK — the M2 zero-gaps wire (2026-07-12) added the FULL
    ('hrr', 1, *) measured reverse row (n=650,346), so the formerly-UNKNOWN
    S41-ny residual (tb-no × hrr1-yes, 146 tape combos) now prices via the
    conditional-table sgp seam."""
    yy = classify_legs((_leg(TB2), _leg(HRR1)), ExplodingProvider())
    assert yy.kind is RelationshipKind.CONTAINMENT
    assert yy.containment == (0, 1)
    yn = classify_legs((_leg(TB2), _leg(HRR1, "no")), ExplodingProvider())
    assert yn.kind is RelationshipKind.IMPOSSIBLE
    assert yn.farmable is False
    nn = classify_legs((_leg(TB2, "no"), _leg(HRR1, "no")), ExplodingProvider())
    assert nn.kind is RelationshipKind.CONTAINMENT
    assert nn.containment == (1, 0)
    ny = classify_legs((_leg(TB2, "no"), _leg(HRR1)), ExplodingProvider())
    assert ny.kind is RelationshipKind.OK
    assert any("conditional table" in n for n in ny.notes)
    # TB-7 (beyond the old 2..6 grid) carries the same exact cell.
    r7 = classify_legs((_leg(TB7), _leg(HRR1)), ExplodingProvider())
    assert r7.kind is RelationshipKind.CONTAINMENT
    assert r7.containment == (0, 1)


async def test_s41_containment_prices_p_tb() -> None:
    """e2e: TB-2 yes × HRR-1 yes prices at P(TB-2) exactly."""
    engine, h = await engine_with(BOOKS)
    result = engine.price(
        rfq_of((TB2, "yes"), (HRR1, "yes")), time_to_close_s=100_000
    )
    assert isinstance(result, ConstructedQuote), result
    assert result.fair_cc == cc_from_prob(marg(h, TB2))


# =========================== WIRE-3: spread×total impossibility =====================


def test_soccer_spread_total_impossible_same_and_lower_line() -> None:
    """S13-yn: cover-by-N yes × over-(M−0.5) no, M <= N ⇒ IMPOSSIBLE and
    farmable (airtight one-scoreline soccer tautology)."""
    same = classify_legs((_leg(SP_ESP2), _leg(FT_TOT2, "no")), ExplodingProvider())
    assert same.kind is RelationshipKind.IMPOSSIBLE
    assert same.farmable is True
    lower = classify_legs((_leg(SP_ESP3), _leg(FT_TOT2, "no")), ExplodingProvider())
    assert lower.kind is RelationshipKind.IMPOSSIBLE
    assert lower.farmable is True


def test_soccer_spread_total_higher_line_is_possible() -> None:
    """M > N: covering by 2 does NOT force over 2.5 — possible, no claim."""
    rel = classify_legs((_leg(SP_ESP2), _leg(FT_TOT3, "no")), ExplodingProvider())
    assert rel.kind is RelationshipKind.OK


def test_soccer_1h_spread_scope_nesting() -> None:
    """S7-yn (1H spread × 1H total, ONE half-time record) stays
    impossible+farmable; S8-yn (1H spread × FT total, CROSS-scope) is
    NARROWED per the V2 adversarial ruling 2026-07-11: still IMPOSSIBLE
    no-quote, but farmable=False — the implication spans TWO official
    records (half-time + full-time), and Kalshi's abandonment/award rules
    text for KXWC totals has not been captured as evidence that both records
    stay consistent (unverified lemma ⇒ fails the airtight one-record farm
    bar). The REVERSE cross-scope (FT spread × 1H total) is never claimed."""
    s7 = classify_legs((_leg(FH_SP_ESP2), _leg(FH_TOT2, "no")), ExplodingProvider())
    assert s7.kind is RelationshipKind.IMPOSSIBLE
    assert s7.farmable is True
    s8 = classify_legs((_leg(FH_SP_ESP2), _leg(FT_TOT2, "no")), ExplodingProvider())
    assert s8.kind is RelationshipKind.IMPOSSIBLE
    assert s8.farmable is False  # V2 ruling: two-official-records claim
    rev = classify_legs((_leg(SP_ESP2), _leg(FH_TOT2, "no")), ExplodingProvider())
    assert rev.kind is RelationshipKind.OK


def test_mlb_spread_total_impossible_never_farmable() -> None:
    """S34-yn: MLB cover-by-N yes × total over-(M−0.5) no, M <= N ⇒ IMPOSSIBLE
    but farmable=False (48h rain-scalar policy — the ml|spread precedent).
    Extras only ADD runs, so the implication is airtight for pricing logic."""
    same = classify_legs((_leg(MLB_SP4), _leg(MLB_TOT4, "no")), ExplodingProvider())
    assert same.kind is RelationshipKind.IMPOSSIBLE
    assert same.farmable is False
    lower = classify_legs((_leg(MLB_SP4), _leg(MLB_TOT3, "no")), ExplodingProvider())
    assert lower.kind is RelationshipKind.IMPOSSIBLE
    assert lower.farmable is False


def test_spread_total_rule_gates_and_refusals() -> None:
    """Sport gate (NFL untouched), other side-mixes untouched, line-0 spread
    and unparseable suffixes refuse — fail-closed everywhere."""
    nfl = classify_legs((_leg(NFL_SP), _leg(NFL_TOT, "no")), ExplodingProvider())
    assert nfl.kind is RelationshipKind.OK  # only soccer + MLB are wired
    yy = classify_legs((_leg(SP_ESP2), _leg(FT_TOT2)), ExplodingProvider())
    assert yy.kind is RelationshipKind.OK  # yy/nn/ny keep structural/copula
    nn = classify_legs(
        (_leg(SP_ESP2, "no"), _leg(FT_TOT2, "no")), ExplodingProvider()
    )
    assert nn.kind is RelationshipKind.OK
    zero = classify_legs((_leg(SP_ESP0), _leg(FT_TOT2, "no")), ExplodingProvider())
    assert zero.kind is RelationshipKind.OK
    bare = classify_legs(
        (_leg(SP_BARE_LINE), _leg(FT_TOT2, "no")), ExplodingProvider()
    )
    assert bare.kind is RelationshipKind.OK


# ====================== WIRE-4: embedded conditional collapse =======================


def _pair_joint(
    ticker_a: str,
    ticker_b: str,
    p_a: float,
    p_b: float,
    side_a: str,
    side_b: str,
) -> float:
    """The bare 2-leg conditional joint through the SAME live machinery the
    engine uses (build_sgp_correlation + price_joint_matrices — the sgp
    implied-rho seam over SAME_PLAYER_CONDITIONALS)."""
    beliefs = [
        LegBelief(p=p_a, uncertainty=0.005, source="t"),
        LegBelief(p=p_b, uncertainty=0.005, source="t"),
    ]
    corr = build_sgp_correlation(
        [_leg(ticker_a), _leg(ticker_b)],
        ((0, 1),),
        shipped_params(),
        marginals=[p_a, p_b],
    )
    return price_joint_matrices(
        beliefs, [side_a, side_b], corr.corr, corr.corr_low, corr.corr_high
    ).p


async def test_embedded_conditional_all_four_mixes_price_pair_joint() -> None:
    """(HIT3, HR1) — measured in both directions, no exact cell — buried with
    a cross-game companion: for ALL FOUR side mixes the fair is the bare
    path's 2-leg conditional joint × P(companion) (cross_event_rho = 0)."""
    engine, h = await engine_with(BOOKS)
    p_hit3, p_hr1 = marg(h, HIT3), marg(h, HR1)
    for s_a, s_b in (("yes", "yes"), ("yes", "no"), ("no", "yes"), ("no", "no")):
        result = engine.price(
            rfq_of((HIT3, s_a), (HR1, s_b), (MLB_ML_OTHER, "yes")),
            time_to_close_s=100_000,
        )
        assert isinstance(result, ConstructedQuote), (s_a, s_b, result)
        expected = _pair_joint(HIT3, HR1, p_hit3, p_hr1, s_a, s_b) * marg(
            h, MLB_ML_OTHER
        )
        assert result.fair_cc == cc_from_prob(expected), (s_a, s_b)


async def test_bare_conditional_pair_stays_bit_identical() -> None:
    """The bare 2-leg pair keeps the OK → sgp path: fair == the directly
    computed conditional joint (the same number WIRE-4's super-leg carries)."""
    engine, h = await engine_with(BOOKS)
    result = engine.price(
        rfq_of((HIT3, "yes"), (HR1, "yes")), time_to_close_s=100_000
    )
    assert isinstance(result, ConstructedQuote), result
    expected = _pair_joint(HIT3, HR1, marg(h, HIT3), marg(h, HR1), "yes", "yes")
    assert result.fair_cc == cc_from_prob(expected)


async def test_embedded_conditional_with_same_game_companion_declines() -> None:
    """RE-EXPRESSED 2026-07-11 (V2 refutation; was ..._prices, which only
    passed because the poisoned test event tickers hid the companion in a
    different game group): a conditional super-leg with a same-game KEPT
    companion (another player's TB leg of the SAME game) has an unmodeled
    neighbour-correlation SIGN — the classifier declines UNKNOWN, the exact
    guard window bands carry."""
    engine, _h = await engine_with(BOOKS)
    result = engine.price(
        rfq_of((HIT3, "yes"), (HR1, "yes"), (TB2_TEAMMATE, "yes")),
        time_to_close_s=100_000,
    )
    assert isinstance(result, NoQuote), result
    assert result.reason is ReasonCode.SKIP_CLASSIFIER_UNKNOWN
    assert "conditional-vs-neighbour correlation sign unmodeled" in result.detail


async def test_v2_counterexample_no_no_own_ml_declines_unknown() -> None:
    """THE V2 live counterexample as a regression pin (prod-convention
    2-segment event tickers, tape-verified): HIT3-no x HR1-no x own-ML-yes.
    Engine BEFORE the guard: fair 0.4183 via corr(super, ML) = +0.23 — the
    kept leg's YES-side rho applied to a NO/NO super-leg, ABOVE the 0.3451
    trivariate truth by +7.32c (sign inverted; truth sits BELOW the 0.3849
    independence product). Now: same-game companion ⇒ UNKNOWN ⇒ NoQuote."""
    legs = (
        _leg(HIT3, "no"),
        _leg(HR1, "no"),
        _leg(ML_OWN, "yes"),
    )
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.UNKNOWN
    assert any(
        "conditional-vs-neighbour correlation sign unmodeled" in n for n in rel.notes
    )
    engine, _h = await engine_with(BOOKS)
    for s_a, s_b in (("no", "no"), ("yes", "yes"), ("no", "yes"), ("yes", "no")):
        result = engine.price(
            rfq_of((HIT3, s_a), (HR1, s_b), (ML_OWN, "yes")),
            time_to_close_s=100_000,
        )
        # Fail-closed doctrine: EVERY side mix declines on a same-game
        # companion, not just the sign-inverted NO mixes.
        assert isinstance(result, NoQuote), (s_a, s_b, result)
        assert result.reason is ReasonCode.SKIP_CLASSIFIER_UNKNOWN, (s_a, s_b)


async def test_conditional_and_containment_pairs_compose_in_one_plan() -> None:
    """A conditional pair (HIT3, HR1) + a containment pair (cover ⟹ win) in
    ONE combo: fair = pair_joint × P(cover) exactly (both games cross)."""
    engine, h = await engine_with(BOOKS)
    result = engine.price(
        rfq_of((HIT3, "yes"), (HR1, "yes"), (SP_ESP2, "yes"), (ML_ESP, "yes")),
        time_to_close_s=100_000,
    )
    assert isinstance(result, ConstructedQuote), result
    expected = _pair_joint(HIT3, HR1, marg(h, HIT3), marg(h, HR1), "yes", "yes") * marg(
        h, SP_ESP2
    )
    assert result.fair_cc == cc_from_prob(expected)


def test_overlapping_conditional_pairs_decline_unknown() -> None:
    """One role per leg: three same-player legs whose pairs are ALL measured
    conditionals (HIT3/HR1/TB5) would need one leg in two super-legs — the
    plan guard fails closed to UNKNOWN."""
    rel = classify_legs((_leg(HIT3), _leg(HR1), _leg(TB5)), ExplodingProvider())
    assert rel.kind is RelationshipKind.UNKNOWN
    assert any("more than one collapse role" in n for n in rel.notes)


async def test_unknown_conditional_shapes_never_reach_a_quote() -> None:
    """UNKNOWN still means no-quote at the engine boundary (defense #2)."""
    engine, _h = await engine_with(BOOKS)
    result = engine.price(
        rfq_of((HIT3, "yes"), (HR1, "yes"), (TB5, "yes")), time_to_close_s=100_000
    )
    assert isinstance(result, NoQuote)
    assert result.reason is ReasonCode.SKIP_CLASSIFIER_UNKNOWN
