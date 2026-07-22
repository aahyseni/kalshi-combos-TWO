"""M1/M2 ZERO-GAPS wire (2026-07-12, job 24844262 tmp/zerogaps/): pins one
sample per wired class so a config regression is caught at the value level,
plus the count/zero-orphan invariants for both sport tables.

Classes covered — MLB: new EXACT conditional cells (37), new MEASURED
conditional cells incl. the ('hrr', 1, *) reverse row that closes S41-ny (47),
pair-table teammate rungs (S3), facing rungs (S4), TB-r8/ks|tb deep rungs
(S5), ml×hr 2+/3+ rungs (S6), the rfi|spread measured ladder replacing the
hand prior (S7). SOCCER: measured corners×1H scalars, oriented
corners_team×1H cells, the 1H-ML×FT-ML draw-orientation cells
(:tiexwin/:teamxtie/:tiextie), 1H-spread×FT-ML :tie, the btts|1H-btts
exact-containment cap, corners|player_goal, oriented corners_team|player_goal
(the SIGN FLIP vs the +0.05 folk prior), oriented advance|corners_team, and
the advance|corners STRENGTH CURVE (oriented_curve machinery keyed on the
ADVANCE leg's marginal). Resolver ROUTING for each new oriented class is
verified on REAL tape ticker shapes (pulled from the ph4 wc_fixed_printed
inputs.pkl) in the companion updates to test_sgp*.py; here the samples pin
the CONFIG values verbatim against the wire lists.
"""

from __future__ import annotations

import pytest

from combomaker.ops.config import CorrelationConfig
from combomaker.pricing.conditionals_mlb import (
    MIN_CONDITIONAL_N,
    SAME_PLAYER_CONDITIONALS,
)
from combomaker.pricing.sgp import SgpParams, build_sgp_correlation
from combomaker.rfq.models import RfqLeg

# --- conditional table: new cells ---------------------------------------------------


def test_new_exact_cells_sample() -> None:
    """Section-1 samples: hit_k=>hrr>=k, the hr-3 row, the tb-7/8 rows."""
    assert SAME_PLAYER_CONDITIONALS[("hit", 1, "hrr", 1)] == (1.0, 587_975, "exact")
    assert SAME_PLAYER_CONDITIONALS[("hit", 4, "tb", 4)] == (1.0, 6_658, "exact")
    assert SAME_PLAYER_CONDITIONALS[("hr", 2, "tb", 8)] == (1.0, 6_195, "exact")
    assert SAME_PLAYER_CONDITIONALS[("hr", 3, "hrr", 5)] == (1.0, 243, "exact")
    assert SAME_PLAYER_CONDITIONALS[("tb", 7, "hrr", 3)] == (1.0, 14_744, "exact")
    assert SAME_PLAYER_CONDITIONALS[("tb", 8, "hit", 1)] == (1.0, 8_813, "exact")


def test_new_measured_cells_sample_and_s41_row() -> None:
    """Section-2 samples, full precision. The ('hrr', 1, *) reverse row
    (n=650,346 — the largest conditioning population in the table) closes the
    S41-ny residual: every cell prices (n >= MIN_CONDITIONAL_N)."""
    assert SAME_PLAYER_CONDITIONALS[("hrr", 1, "tb", 2)] == (
        0.5241456086452442, 650_346, "measured",
    )
    assert SAME_PLAYER_CONDITIONALS[("hrr", 1, "hit", 1)] == (
        0.9040956659993296, 650_346, "measured",
    )
    assert SAME_PLAYER_CONDITIONALS[("hit", 1, "tb", 7)] == (
        0.02507589608401718, 587_975, "measured",
    )
    assert SAME_PLAYER_CONDITIONALS[("tb", 5, "hr", 3)] == (
        0.0037946219432212123, 64_038, "measured",
    )
    for fam in ("hit", "hr", "tb"):
        for rung in range(1, 9):
            cell = SAME_PLAYER_CONDITIONALS.get(("hrr", 1, fam, rung))
            if cell is not None:
                assert cell[1] >= MIN_CONDITIONAL_N, (fam, rung)


def test_not_wired_cells_stay_absent() -> None:
    """Wire-list section 8: sub-50k conditioning rows stay UNWIRED — their
    no+yes mixes must keep declining UNKNOWN (never wire-by-hope)."""
    assert ("hit", 4, "hr", 1) not in SAME_PLAYER_CONDITIONALS
    assert ("tb", 7, "hr", 3) not in SAME_PLAYER_CONDITIONALS
    assert ("tb", 8, "hrr", 4) not in SAME_PLAYER_CONDITIONALS
    assert ("hit", 3, "tb", 7) not in SAME_PLAYER_CONDITIONALS


# --- MLB pair table: one pin per wire-list section ----------------------------------


def test_mlb_pair_rungs_sample_per_section() -> None:
    cfg = CorrelationConfig()
    mlb = cfg.pair_rho_by_sport["mlb"]
    bands = cfg.pair_rho_uncertainty
    # S3 teammate rungs (the :same aggregates STAY for unparsed rungs)
    assert mlb["player_hit|player_ks:same:r4"] == -0.009
    assert mlb["player_hrr|player_ks:same:r1"] == 0.015
    assert mlb["player_hit|player_ks:same"] == 0.010  # aggregate kept
    # S4 facing rungs (monotone hrr ladder r1 -0.153 -> r5 -0.189)
    assert mlb["player_hit|player_ks:opp:r4"] == -0.187
    assert mlb["player_hrr|player_ks:opp:r1"] == -0.153
    assert mlb["player_hrr|player_ks:opp:r3"] == -0.178  # = Phase-1 anchor
    assert mlb["player_hrr|player_ks:opp:r5"] == -0.189
    assert mlb["player_hrr|player_ks:opp"] == -0.18  # un-runged fallback kept
    # S5 TB r8 + deep teammate rungs
    assert mlb["player_ks|player_tb:opp:r8"] == -0.119
    assert mlb["player_ks|player_tb:same:r7"] == -0.004
    # S6 ml x hr 2+/3+ (exact mirrors; r3 band 0.07 per the small cell)
    assert mlb["moneyline|player_hr:same:r2"] == 0.270
    assert mlb["moneyline|player_hr:opp:r2"] == -0.270
    assert mlb["moneyline|player_hr:same:r3"] == 0.338
    assert bands["mlb:moneyline|player_hr:same:r3"] == 0.07
    # S7 rfi|spread ladder + plain replacement
    assert mlb["rfi|spread:r1"] == 0.000
    assert mlb["rfi|spread:r3"] == 0.079
    assert mlb["rfi|spread:r5"] == 0.107
    assert mlb["rfi|spread"] == 0.05
    assert bands["mlb:rfi|spread"] == 0.08


# --- soccer table: all 26 wire-list keys, verbatim ----------------------------------

_SOCCER_WIRE: dict[str, tuple[float, float]] = {
    "corners|first_half_moneyline": (0.00, 0.06),
    "corners|first_half_total": (-0.02, 0.05),
    "corners|first_half_btts": (-0.01, 0.04),
    "corners|first_half_spread": (-0.05, 0.05),
    "corners_team|first_half_moneyline:same": (-0.20, 0.05),
    "corners_team|first_half_moneyline:opp": (0.23, 0.04),
    "corners_team|first_half_moneyline:tie": (0.00, 0.04),
    "corners_team|first_half_moneyline": (0.00, 0.25),
    "corners_team|first_half_total": (0.00, 0.07),
    "corners_team|first_half_btts": (0.00, 0.05),
    "corners_team|first_half_spread:same": (-0.18, 0.07),
    "corners_team|first_half_spread:opp": (0.15, 0.16),
    "corners_team|first_half_spread": (0.00, 0.22),
    "first_half_moneyline|moneyline:tiexwin": (-0.15, 0.04),
    "first_half_moneyline|moneyline:teamxtie": (-0.21, 0.04),
    "first_half_moneyline|moneyline:tiextie": (0.35, 0.04),
    "first_half_spread|moneyline:tie": (-0.44, 0.04),
    "btts|first_half_btts": (0.95, 0.04),
    "corners|player_goal": (-0.03, 0.10),
    "corners_team|player_goal:same": (-0.14, 0.05),
    "corners_team|player_goal:opp": (0.11, 0.04),
    "corners_team|player_goal": (0.00, 0.20),
    "advance|corners": (0.00, 0.25),
    "advance|corners_team:same": (-0.13, 0.05),
    "advance|corners_team:opp": (0.13, 0.05),
    "advance|corners_team": (-0.05, 0.20),
}


@pytest.mark.parametrize(("key", "expected"), sorted(_SOCCER_WIRE.items()))
def test_soccer_wire_key_verbatim(key: str, expected: tuple[float, float]) -> None:
    cfg = CorrelationConfig()
    value, band = expected
    assert cfg.pair_rho_by_sport["soccer"][key] == value
    assert cfg.pair_rho_uncertainty[f"soccer:{key}"] == band


def test_advance_corners_curve_knots_verbatim() -> None:
    """The strength curve ships as oriented_curve knots (q=0.5, line >= 9,
    soccer_measurements.json 'curves' — verbatim) with the measurement's
    recommended knot band 0.10; antisymmetric by construction."""
    cfg = CorrelationConfig()
    knots = cfg.oriented_curve["soccer:advance|corners"]
    assert knots == [
        (0.1684, 0.2274),
        (0.2823, 0.2336),
        (0.3491, 0.1549),
        (0.4195, 0.0732),
        (0.4607, 0.0204),
        (0.5393, -0.0205),
        (0.5805, -0.0732),
        (0.6509, -0.1549),
        (0.7177, -0.2336),
        (0.8316, -0.2274),
    ]
    assert cfg.oriented_curve_uncertainty["soccer:advance|corners"] == 0.10
    for (m, r), (m2, r2) in zip(knots, reversed(knots), strict=True):
        assert m == pytest.approx(1.0 - m2, abs=1e-9)
        # rho antisymmetric to measurement rounding (0.0204 vs 0.0205).
        assert r == pytest.approx(-r2, abs=1e-3)


# --- table-size + zero-orphan invariants --------------------------------------------


def test_table_sizes_and_zero_orphans_both_sports() -> None:
    """MLB 186 -> 247 (new-props) -> 327 (gap-pairs tranche closing the 16 flat-gaps
    to zero) -> 319 (8 dead outs×spread single-rung keys removed — both-rung-keyed
    pair chains :r:r), 2026-07-22; soccer 110. Every table key has a sport-prefixed
    band and vice versa — a point without a band gets the default width (wrong
    confidence), a band without a point is dead config."""
    cfg = CorrelationConfig()
    for sport, expected in (("mlb", 319), ("soccer", 110)):
        table = cfg.pair_rho_by_sport[sport]
        assert len(table) == expected, sport
        band_keys = {
            k.removeprefix(f"{sport}:")
            for k in cfg.pair_rho_uncertainty
            if k.startswith(f"{sport}:")
        }
        assert set(table) == band_keys, sport


# --- e2e: the draw-orientation resolver on the REAL tape ticker shape ---------------


def _params() -> SgpParams:
    c = CorrelationConfig()
    return SgpParams(
        pair_rho=dict(c.pair_rho),
        default_rho=c.same_event_rho,
        cross_event_rho=c.cross_event_rho,
        typed_uncertainty=c.typed_rho_uncertainty,
        untyped_uncertainty=c.untyped_rho_uncertainty,
        pair_uncertainty=dict(c.pair_rho_uncertainty),
        pair_rho_by_sport={s: dict(t) for s, t in c.pair_rho_by_sport.items()},
        oriented_curve={k: list(v) for k, v in c.oriented_curve.items()},
        oriented_curve_uncertainty=dict(c.oriented_curve_uncertainty),
    )


def _leg(mt: str) -> RfqLeg:
    return RfqLeg(
        market_ticker=mt,
        event_ticker="-".join(mt.split("-")[:2]),
        side="yes",
        yes_settlement_value_cc=None,
    )


def _rho(a: str, b: str) -> float:
    out = build_sgp_correlation(
        [_leg(a), _leg(b)], [(0, 1)], _params(), marginals=[0.5, 0.5]
    )
    return float(out.corr[0][1])


# Real tape game codes (ph4 wc_fixed_printed inputs.pkl, 2026-07-08 window).
_G = "26JUL10ESPBEL"


_DRAW_CASES = [
    # 1H draw x FT team win — the resolver suffix order is pair_key leg
    # order (1H leg first), verified leg-order-independent below.
    (f"KXWC1H-{_G}-TIE", f"KXWCGAME-{_G}-BEL", -0.15),
    (f"KXWC1H-{_G}-ESP", f"KXWCGAME-{_G}-TIE", -0.21),
    (f"KXWC1H-{_G}-TIE", f"KXWCGAME-{_G}-TIE", 0.35),
    (f"KXWC1HSPREAD-{_G}-ESP2", f"KXWCGAME-{_G}-TIE", -0.44),
    # team-vs-team orientations unchanged by the draw extension
    (f"KXWC1H-{_G}-ESP", f"KXWCGAME-{_G}-ESP", 0.71),
    (f"KXWC1H-{_G}-ESP", f"KXWCGAME-{_G}-BEL", -0.67),
    # new oriented routing: corners_team x 1H-winner / 1H-spread,
    # advance x corners_team, corners_team x scorer (the SIGN FLIP)
    (f"KXWCTCORNERS-{_G}-ESP4", f"KXWC1H-{_G}-ESP", -0.20),
    (f"KXWCTCORNERS-{_G}-ESP4", f"KXWC1H-{_G}-BEL", 0.23),
    (f"KXWCTCORNERS-{_G}-ESP4", f"KXWC1H-{_G}-TIE", 0.00),
    (f"KXWCTCORNERS-{_G}-ESP4", f"KXWC1HSPREAD-{_G}-ESP2", -0.18),
    (f"KXWCTCORNERS-{_G}-BEL4", f"KXWC1HSPREAD-{_G}-ESP2", 0.15),
    (f"KXWCADVANCE-{_G}-BEL", f"KXWCTCORNERS-{_G}-BEL4", -0.13),
    (f"KXWCADVANCE-{_G}-BEL", f"KXWCTCORNERS-{_G}-ESP4", 0.13),
    (f"KXWCTCORNERS-{_G}-BEL5", f"KXWCGOAL-{_G}-BELRLUKAK9-1", -0.14),
    (f"KXWCTCORNERS-{_G}-ESP5", f"KXWCGOAL-{_G}-BELRLUKAK9-1", 0.11),
    # btts x 1H-btts buried-pair rho (exact-containment cap value)
    (f"KXWCBTTS-{_G}-BTTS", f"KXWC1HBTTS-{_G}-BTTS", 0.95),
]


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    _DRAW_CASES,
    ids=[f"{a.split('-')[0]}|{a.rsplit('-', 1)[-1]}x{b.split('-')[0]}|"
         f"{b.rsplit('-', 1)[-1]}={e}" for a, b, e in _DRAW_CASES],
)
def test_zerogaps_pair_resolves_on_real_ticker_shapes(
    a: str, b: str, expected: float
) -> None:
    assert _rho(a, b) == pytest.approx(expected)
    assert _rho(b, a) == pytest.approx(expected)  # leg-order independent


def test_unparseable_1h_winner_suffix_still_falls_back() -> None:
    """A garbage (empty) 1H-winner suffix is neither a team nor a draw — the
    resolver must return None and the pair must fall to the flat prior, never
    a guessed draw orientation (quiet-failure defense #2)."""
    out = build_sgp_correlation(
        (_leg(f"KXWC1H-{_G}-"), _leg(f"KXWCGAME-{_G}-TIE")),
        [(0, 1)],
        _params(),
    )
    assert out.untyped_pairs == 1 and out.typed_pairs == 0
