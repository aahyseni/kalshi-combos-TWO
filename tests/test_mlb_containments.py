"""MLB same-player cross-stat containment / conditional family (DO-2, wired
2026-07-10) — the [D]-regression fix.

Policy under test (operator-approved): same-game SAME-PLAYER cross-family
batter pairs never price at the distinct-player [D] rhos. Cells the
measurement table marks 'exact' (arithmetic containments, verified == 1.0
pooled AND per-era) drive the ``_containment_sign`` verdicts — with MLB
IMPOSSIBLE verdicts NEVER farmable (the 48h-postponement rule settles markets
scalar, breaking the airtight farm bar). 'measured' cells with n >= 50k price
via the conditional table (joint = P(conditioning leg) x p_cond) through the
sgp implied-rho seam, BARE pairs only; everything else — unmeasured cells, the
truncated table region, buried partials — declines UNKNOWN.

Real ticker shapes from the live API (2026-07-09): the player segment embeds
the team prefix (COLHGOODMAN15), the line suffix -N means "N or more"."""

from __future__ import annotations

import numpy as np

from combomaker.ops.config import CorrelationConfig
from combomaker.pricing.conditionals_mlb import (
    MIN_CONDITIONAL_N,
    SAME_PLAYER_CONDITIONALS,
    SAME_PLAYER_RHO_BAND,
    implied_rho,
)
from combomaker.pricing.copula import gaussian_copula_joint_prob
from combomaker.pricing.relationships import RelationshipKind, classify_legs
from combomaker.pricing.sgp import SgpParams, build_sgp_correlation
from combomaker.rfq.models import RfqLeg

_G = "26JUL092145COLSF"        # real game code (COL @ SF, 2026-07-09 live)
_G2 = "26JUL101610AZSD"        # real 2-char x 2-char code game (AZ @ SD)

HIT1 = f"KXMLBHIT-{_G}-COLHGOODMAN15-1"
HIT3 = f"KXMLBHIT-{_G}-COLHGOODMAN15-3"
HR1 = f"KXMLBHR-{_G}-COLHGOODMAN15-1"
TB2 = f"KXMLBTB-{_G}-COLHGOODMAN15-2"
TB4 = f"KXMLBTB-{_G}-COLHGOODMAN15-4"
HRR2 = f"KXMLBHRR-{_G}-COLHGOODMAN15-2"
HRR3 = f"KXMLBHRR-{_G}-COLHGOODMAN15-3"
TB2_TEAMMATE = f"KXMLBTB-{_G}-COLETOVAR14-2"
KS6_SAME_SEGMENT = f"KXMLBKS-{_G}-COLHGOODMAN15-6"
TOTAL9 = f"KXMLBTOTAL-{_G}-9"


class _Prov:
    def event_mutually_exclusive(self, e: str) -> bool:
        return False


def _leg(mt: str, side: str = "yes") -> RfqLeg:
    return RfqLeg(mt, "-".join(mt.split("-")[:2]), side, None)


def shipped_params() -> SgpParams:
    cfg = CorrelationConfig()
    return SgpParams(
        pair_rho=dict(cfg.pair_rho),
        default_rho=cfg.same_event_rho,
        cross_event_rho=cfg.cross_event_rho,
        typed_uncertainty=cfg.typed_rho_uncertainty,
        untyped_uncertainty=cfg.untyped_rho_uncertainty,
        pair_uncertainty=dict(cfg.pair_rho_uncertainty),
        pair_rho_by_sport={s: dict(t) for s, t in cfg.pair_rho_by_sport.items()},
        oriented_curve={k: list(v) for k, v in cfg.oriented_curve.items()},
        oriented_curve_uncertainty=dict(cfg.oriented_curve_uncertainty),
    )


# --- conditional-table sanity ------------------------------------------------------


def test_exact_cells_are_exactly_one() -> None:
    """'exact' means arithmetic containment verified == 1.0 — any other value
    in an exact cell is a corrupted table."""
    for key, (p, n, marker) in SAME_PLAYER_CONDITIONALS.items():
        assert marker in ("exact", "measured"), key
        assert n > 0, key
        if marker == "exact":
            assert p == 1.0, key
        else:
            assert 0.0 <= p < 1.0, key


def test_table_covers_only_cross_family_batter_cells() -> None:
    fams = {"hit", "hr", "tb", "hrr"}
    for fam_a, _rung_a, fam_b, _rung_b in SAME_PLAYER_CONDITIONALS:
        assert fam_a in fams and fam_b in fams
        assert fam_a != fam_b  # same-family rungs are a ladder, never a cell


def test_hit3_conditioning_rows_sit_under_the_n_bar() -> None:
    """('hit', 3, ...) rows are n=48,375 — just under MIN_CONDITIONAL_N — so a
    HIT-3-conditioned cell must never price; its reverse direction may."""
    assert SAME_PLAYER_CONDITIONALS[("hit", 3, "hr", 1)][1] < MIN_CONDITIONAL_N
    assert SAME_PLAYER_CONDITIONALS[("hr", 1, "hit", 3)][1] >= MIN_CONDITIONAL_N


# --- classifier: exact cells -> containment / impossible ---------------------------


def test_same_player_hr_yes_hit_yes_is_containment_joint_p_hr() -> None:
    """The mission spot check: HIT-1 x HR-1 same game, both YES -> containment
    with joint = P(HR) (the HR leg is the subset)."""
    legs = (_leg(HIT1), _leg(HR1))
    rel = classify_legs(legs, _Prov())
    assert rel.kind is RelationshipKind.CONTAINMENT
    assert rel.containment == (1, 0)  # subset = the HR leg


def test_same_player_hr_yes_tb4_yes_is_containment() -> None:
    """HR>=1 forces TB>=4 (one HR is 4 total bases) — exact cell."""
    legs = (_leg(HR1), _leg(TB4))
    rel = classify_legs(legs, _Prov())
    assert rel.kind is RelationshipKind.CONTAINMENT
    assert rel.containment == (0, 1)


def test_same_player_hr_yes_hit_no_is_impossible_never_farmable() -> None:
    """A HR IS a hit, so HR-yes x HIT-no can never settle — IMPOSSIBLE, but NOT
    farmable: MLB scalar settlement (48h postponement) breaks the airtight bar
    (unlike the soccer tautology farms)."""
    legs = (_leg(HR1), _leg(HIT1, "no"))
    rel = classify_legs(legs, _Prov())
    assert rel.kind is RelationshipKind.IMPOSSIBLE
    assert rel.farmable is False


def test_same_player_hr_no_hit_no_is_containment_subset_is_hit_no() -> None:
    """{HR no, HIT no}: no-hit forces no-HR, so the HIT-no leg is the effective
    subset — joint = P(HIT no)."""
    legs = (_leg(HR1, "no"), _leg(HIT1, "no"))
    rel = classify_legs(legs, _Prov())
    assert rel.kind is RelationshipKind.CONTAINMENT
    assert rel.containment == (1, 0)


def test_same_player_exact_pair_buried_in_larger_combo_collapses() -> None:
    """RETARGETED 2026-07-11 (was ..._is_unknown): the engine now HAS the
    containment super-leg collapse, and it is generic at the
    CONTAINMENT-relationship level — a buried EXACT same-player pair collapses
    (the implied HIT leg drops; HR + total price via the reduced copula)."""
    legs = (_leg(HIT1), _leg(HR1), _leg(TOTAL9))
    rel = classify_legs(legs, _Prov())
    assert rel.kind is RelationshipKind.CONTAINMENT
    assert rel.containment is None
    assert rel.containments == ((1, 0),)  # subset = HR leg; HIT leg drops


# --- classifier: measured cells -> bare-pair conditional / buried UNKNOWN ----------


def test_same_player_hit_tb_bare_pair_is_exact_containment() -> None:
    """RETARGETED 2026-07-10 (full 142-cell table restored): TB>=2 arithmetically
    implies HIT>=1 (total bases only come from hits), and the restored
    reverse-direction cell ('tb',2,'hit',1) is EXACT — so HIT-1 x TB-2
    same-player is CONTAINMENT (subset = the TB leg), no longer a conditional.
    (The original assertion reflected the truncated 60-cell table, where all
    tb-conditioned rows were missing.)"""
    legs = (_leg(HIT1), _leg(TB2))
    rel = classify_legs(legs, _Prov())
    assert rel.kind is RelationshipKind.CONTAINMENT
    assert rel.containment == (1, 0)  # subset = the TB-2 leg


def test_same_player_hit3_hr1_prices_via_the_reverse_direction() -> None:
    """The mission spot check: HIT-3 x HR-1 -> conditional per table. The
    ('hit',3,...) direction sits under the n bar, but ('hr',1,'hit',3) is
    n=101,186 — the reverse direction carries it."""
    legs = (_leg(HIT3), _leg(HR1))
    rel = classify_legs(legs, _Prov())
    assert rel.kind is RelationshipKind.OK
    assert any("conditional" in n for n in rel.notes)


def test_same_player_hr_no_hit_yes_prices_via_measured_reverse() -> None:
    """{HR no, HIT yes} is the exact cell's None sign-case; the measured
    ('hit',1,'hr',1) cell upgrades it to conditional pricing (bare pair)."""
    legs = (_leg(HR1, "no"), _leg(HIT1))
    rel = classify_legs(legs, _Prov())
    assert rel.kind is RelationshipKind.OK
    assert any("conditional" in n for n in rel.notes)


def test_same_player_conditional_pair_buried_collapses() -> None:
    """RETARGETED 2026-07-11 twice (WIRE-4, then the V2 refutation): a
    measured (non-exact) same-player pair buried in a >2-leg combo records a
    CONDITIONAL collapse pair — but ONLY with cross-game companions. (HIT3 x
    HR1 — measured in both directions, no exact cell — plus another GAME's
    total.)"""
    legs = (_leg(HIT3), _leg(HR1), _leg(f"KXMLBTOTAL-{_G2}-9"))
    rel = classify_legs(legs, _Prov())
    assert rel.kind is RelationshipKind.CONTAINMENT
    assert rel.containment is None
    assert rel.conditionals == ((0, 1),)  # HIT3 kept as carrier, HR1 drops
    assert rel.containments == () and rel.bands == ()
    assert any("conditional super-leg" in n for n in rel.notes)


def test_same_player_conditional_with_same_game_companion_is_unknown() -> None:
    """V2 REFUTATION guard (2026-07-11): the SAME buried pair with its OWN
    game's total is NOT priceable — the conditional super-leg is represented
    by its kept leg at side "yes", so a same-game neighbour's rho carries the
    WRONG SIGN for NO-side mixes (live counterexample: HIT3-no x HR1-no x
    own-ML-yes, engine 0.4183 vs 0.3451 trivariate truth). Fail-closed for
    EVERY mix — the exact isolation guard window bands carry."""
    legs = (_leg(HIT3), _leg(HR1), _leg(TOTAL9))
    rel = classify_legs(legs, _Prov())
    assert rel.kind is RelationshipKind.UNKNOWN
    assert any(
        "conditional-vs-neighbour correlation sign unmodeled" in n for n in rel.notes
    )


# --- classifier: unmeasured / out-of-scope shapes ----------------------------------


def test_same_player_unmeasured_pair_is_unknown_even_bare() -> None:
    """RETARGETED 2026-07-10: TB-2 x HRR-2 is measured both directions in the
    restored full table, so the truly-unmeasured probe now uses an OUT-OF-GRID
    rung — TB-9 has no cell in either direction (the grid tops out at 6) —
    UNKNOWN, never the distinct-player [D] rho (the regression this fixes)."""
    legs = (_leg(HIT1), _leg(TB2[:-1] + "9"))  # swap ONLY the trailing rung digit
    rel = classify_legs(legs, _Prov())
    assert rel.kind is RelationshipKind.UNKNOWN
    assert any("unmeasured" in n for n in rel.notes)


def test_same_player_exact_none_sign_case_prices_via_reverse_conditional() -> None:
    """RETARGETED 2026-07-10: {HR no, HRR-2 yes} — the exact hr->hrr direction
    gives no side-verdict, but the reverse ('hrr',2,'hr',1) cell (0.2312,
    n=437,563) exists in the full table, so the pair stays OK and the sgp seam
    prices the conditional. P(A∧B) = P(B)·P(A|B) is an identity, so this is
    exact-consistent, not an approximation."""
    legs = (_leg(HR1, "no"), _leg(HRR2))
    rel = classify_legs(legs, _Prov())
    assert rel.kind is RelationshipKind.OK
    assert any("conditional" in n for n in rel.notes)


def test_distinct_players_are_not_intercepted() -> None:
    """Cross-player pairs stay with the teammate/opponent routing resolvers."""
    legs = (_leg(HIT1), _leg(TB2_TEAMMATE))
    rel = classify_legs(legs, _Prov())
    assert rel.kind is RelationshipKind.OK
    assert rel.containment is None


def test_same_player_cross_game_carries_no_relation() -> None:
    """The same player segment in DIFFERENT games (e.g. traded mid-season or a
    shape coincidence) never pins — the raw game segments must match."""
    legs = (_leg(HIT1), _leg(f"KXMLBHR-{_G2}-COLHGOODMAN15-1"))
    rel = classify_legs(legs, _Prov())
    assert rel.kind is RelationshipKind.OK
    assert rel.same_event_groups == ()


def test_ks_with_identical_segment_needs_no_branch() -> None:
    """KS x batter is a DIFFERENT entity (starter vs batter) — no same-player
    branch fires even on an identical segment; the pair stays OK for the
    routing/plain path."""
    legs = (_leg(KS6_SAME_SEGMENT), _leg(HIT1))
    rel = classify_legs(legs, _Prov())
    assert rel.kind is RelationshipKind.OK


def test_same_player_same_family_rungs_stay_ok_this_step() -> None:
    """HIT-1 x HIT-3 same player is a nested LADDER (same family) — explicitly
    out of scope for the cross-stat table this step; status quo (OK)."""
    legs = (_leg(HIT1), _leg(HIT3))
    rel = classify_legs(legs, _Prov())
    assert rel.kind is RelationshipKind.OK


# --- sgp seam: conditional-table implied-rho pricing --------------------------------


def test_conditional_rho_reproduces_the_table_joint() -> None:
    """The copula at the resolved rho must reproduce joint = P(HIT-1) x
    P(TB-2 | HIT-1) at the live marginals (integrator tolerance)."""
    legs = [_leg(HIT1), _leg(TB2)]
    c = build_sgp_correlation(legs, ((0, 1),), shipped_params(), marginals=[0.65, 0.45])
    rho = float(c.corr[0, 1])
    p_cond = SAME_PLAYER_CONDITIONALS[("hit", 1, "tb", 2)][0]
    corr = np.array([[1.0, rho], [rho, 1.0]])
    assert abs(gaussian_copula_joint_prob([0.65, 0.45], corr) - 0.65 * p_cond) < 1e-6
    assert any("same-player" in n for n in c.notes)
    assert abs(float(c.corr_high[0, 1]) - rho - SAME_PLAYER_RHO_BAND) < 1e-9


def test_conditional_rho_caps_when_marginals_contradict_the_table() -> None:
    """Live marginals can make the pooled conditional unreachable (target above
    the Frechet upper bound) — the solve caps at +0.95, the closest coupling,
    mirroring the pricer's own Frechet clamp."""
    legs = [_leg(HIT1), _leg(TB2)]
    c = build_sgp_correlation(legs, ((0, 1),), shipped_params(), marginals=[0.9, 0.3])
    assert abs(float(c.corr[0, 1]) - 0.95) < 1e-9


def test_conditional_needs_marginals_else_plain() -> None:
    """Without marginals no rho can be solved — the resolver stands down and
    the pair falls to the routing/plain path (status quo)."""
    legs = [_leg(HIT1), _leg(TB2)]
    c = build_sgp_correlation(legs, ((0, 1),), shipped_params())
    assert abs(float(c.corr[0, 1]) - 0.02) < 1e-9  # plain mlb hit|tb


def test_implied_rho_degenerate_marginals_refuse() -> None:
    assert implied_rho(0.0, 0.5, 0.5) is None
    assert implied_rho(0.5, 1.0, 0.5) is None
    assert implied_rho(0.5, 0.5, 1.5) is None


def test_implied_rho_independence_recovered() -> None:
    """p_cond == p_other means the conditional carries no dependence — the
    solved rho must be ~0."""
    rho = implied_rho(0.4, 0.3, 0.3)
    assert rho is not None
    assert abs(rho) < 1e-4


# --- sgp seam: MLB moneyline x spread routing (DO-3) --------------------------------

MLB_ML_NYY = "KXMLBGAME-26JUL081840NYYTB-NYY"
MLB_SP_NYY2 = "KXMLBSPREAD-26JUL081840NYYTB-NYY2"
MLB_SP_TB2 = "KXMLBSPREAD-26JUL081840NYYTB-TB2"
MLB_SP_BAD = "KXMLBSPREAD-26JUL081840NYYTB-XX9"


def _ml_spread_corr(ml: str, sp: str) -> tuple[float, float, float]:
    c = build_sgp_correlation(
        [_leg(ml), _leg(sp)], ((0, 1),), shipped_params(), marginals=[0.5, 0.5]
    )
    return float(c.corr[0, 1]), float(c.corr_low[0, 1]), float(c.corr_high[0, 1])


def test_mlb_ml_spread_same_team_routes_to_plus_095() -> None:
    rho, lo, hi = _ml_spread_corr(MLB_ML_NYY, MLB_SP_NYY2)
    assert abs(rho - 0.95) < 1e-9
    assert abs(lo - 0.91) < 1e-9   # band 0.04 (high side clamped at 0.95)
    assert abs(hi - 0.95) < 1e-9


def test_mlb_ml_spread_opposite_teams_route_to_minus_095() -> None:
    rho, lo, hi = _ml_spread_corr(MLB_ML_NYY, MLB_SP_TB2)
    assert abs(rho - (-0.95)) < 1e-9
    assert abs(hi - (-0.91)) < 1e-9


def test_mlb_ml_spread_unresolvable_falls_to_plain_sign_spanning() -> None:
    """The mission spot check: unresolvable -> copula at 0.00 with the 0.95
    sign-spanning band (never the old flat +0.6 default)."""
    rho, lo, hi = _ml_spread_corr(MLB_ML_NYY, MLB_SP_BAD)
    assert abs(rho - 0.00) < 1e-9
    assert abs(lo - (-0.95)) < 1e-9
    assert abs(hi - 0.95) < 1e-9


def test_nfl_ml_spread_is_untouched() -> None:
    """The 2026-07-10 spot check stays green: NFL ml|spread keeps its own
    calibrated 0.88 — the MLB routing branch is sport-gated."""
    c = build_sgp_correlation(
        [_leg("KXNFLGAME-25SEP04DALPHI-DAL"), _leg("KXNFLSPREAD-25SEP04DALPHI-DAL3")],
        ((0, 1),),
        shipped_params(),
        marginals=[0.5, 0.5],
    )
    assert abs(float(c.corr[0, 1]) - 0.88) < 1e-9


def test_config_ml_spread_entries_are_coherent() -> None:
    """Plain 0.00 + sign-spanning band; oriented +-0.95 with 0.04 bands; all
    three carry mlb:-prefixed band entries (zero orphans)."""
    cfg = CorrelationConfig()
    mlb = cfg.pair_rho_by_sport["mlb"]
    assert mlb["moneyline|spread"] == 0.00
    assert mlb["moneyline|spread:same"] == 0.95
    assert mlb["moneyline|spread:opp"] == -0.95
    assert cfg.pair_rho_uncertainty["mlb:moneyline|spread"] == 0.95
    assert cfg.pair_rho_uncertainty["mlb:moneyline|spread:same"] == 0.04
    assert cfg.pair_rho_uncertainty["mlb:moneyline|spread:opp"] == 0.04
