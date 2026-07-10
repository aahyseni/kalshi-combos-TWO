"""MLB team routing: teammate/opponent + ML-orientation resolvers.

The parse trap: 2-vs-3-char team codes with NO delimiter, in BOTH the game
code's team blob (COLSF = COL+SF) and the player segment's team prefix
(COLRFELTNER18). Naive both-split prefix-matching is ambiguous ~20% of the
time; resolution is end-anchoring against the blob (prefix=away, suffix=home),
provably unique on the live-enumerated 30-code vocabulary (no code prefixes
another; all 870 concatenations tile uniquely — 2026-07-09 verification).
Every doubt fails to the plain neutralized/unrouted entry, mirroring the
soccer scorer guard. Real tickers from the live API (2026-07-09)."""

from __future__ import annotations

from combomaker.ops.config import CorrelationConfig
from combomaker.pricing.legtypes import LegType, pair_key
from combomaker.pricing.relationships import RelationshipKind, classify_legs
from combomaker.pricing.sgp import (
    SgpCorrelation,
    SgpParams,
    _mlb_player_side,
    _mlb_side_of,
    _mlb_team_blob,
    build_sgp_correlation,
)
from combomaker.rfq.models import RfqLeg

_G = "26JUL092145COLSF"       # real game code (COL @ SF, 2026-07-09 live)
_G2 = "26JUL101610AZSD"       # real 2-char × 2-char code game (AZ @ SD)
_DH1 = "26JUL071835MILSTLG1"  # doubleheader game 1 (prod tape shape)
_DH2 = "26JUL071835MILSTLG2"


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


class _Prov:
    def event_mutually_exclusive(self, e: str) -> bool:
        return False


def _leg(mt: str) -> RfqLeg:
    return RfqLeg(mt, "-".join(mt.split("-")[:2]), "yes", None)


def _corr(a: str, b: str) -> SgpCorrelation:
    legs = [_leg(a), _leg(b)]
    rel = classify_legs(legs, _Prov())
    assert rel.kind is RelationshipKind.OK
    assert rel.same_event_groups == ((0, 1),)  # game-code grouping merges families
    return build_sgp_correlation(legs, rel.same_event_groups, shipped_params(),
                                 marginals=[0.5, 0.5])


def _rho(a: str, b: str) -> float:
    return float(_corr(a, b).corr[0][1])


# --- parse unit tests -----------------------------------------------------------

class TestTeamParse:
    def test_blob_from_real_gamecodes(self) -> None:
        assert _mlb_team_blob(f"KXMLBKS-{_G}-SFCWHISENHUNT88-8") == (_G, "COLSF")
        assert _mlb_team_blob(f"KXMLBGAME-{_G2}-AZ") == (_G2, "AZSD")

    def test_doubleheader_suffix_stripped_but_segment_kept(self) -> None:
        assert _mlb_team_blob(f"KXMLBGAME-{_DH1}-STL") == (_DH1, "MILSTL")

    def test_garbage_gamecode_refuses(self) -> None:
        assert _mlb_team_blob("KXMLBGAME-BAD-SF") is None
        assert _mlb_team_blob("KXMLBRFI") is None

    def test_side_anchoring_two_and_three_char(self) -> None:
        assert _mlb_side_of("COL", "COLSF") == "away"
        assert _mlb_side_of("SF", "COLSF") == "home"
        assert _mlb_side_of("AZ", "AZSD") == "away"
        assert _mlb_side_of("OLS", "COLSF") is None    # interior fragment: refuse
        assert _mlb_side_of("AB", "ABAB") is None      # both ends: refuse
        assert _mlb_side_of("COLSF", "COLSF") is None  # whole blob: refuse

    def test_player_side_resolves_the_col_ambiguity(self) -> None:
        # COLRFELTNER18 naive-prefix-matches CO and COL; anchoring is unique.
        assert _mlb_player_side("COLRFELTNER18", "COLSF") == "away"
        assert _mlb_player_side("SFCWHISENHUNT88", "COLSF") == "home"
        assert _mlb_player_side("AZCCARROLL7", "AZSD") == "away"
        assert _mlb_player_side("XXYZNOBODY1", "COLSF") is None


# --- ML-orientation resolver (the mission's integration triple) ------------------

class TestMlOrientation:
    def test_own_team_ml_x_ks_is_plus_024(self) -> None:
        c = _corr(f"KXMLBGAME-{_G}-SF", f"KXMLBKS-{_G}-SFCWHISENHUNT88-8")
        assert abs(c.corr[0, 1] - 0.24) < 1e-9
        assert abs(c.corr_high[0, 1] - 0.30) < 1e-9  # band 0.06
        assert any("mlb:moneyline|player_ks:same" in n for n in c.notes)

    def test_opponent_ml_x_ks_is_minus_024(self) -> None:
        assert abs(_rho(f"KXMLBGAME-{_G}-COL",
                        f"KXMLBKS-{_G}-SFCWHISENHUNT88-8") - (-0.24)) < 1e-9

    def test_unresolvable_falls_to_neutralized_0_band_030(self) -> None:
        c = _corr("KXMLBGAME-BAD-SF", "KXMLBKS-BAD-SFCWHISENHUNT88-8")
        assert abs(c.corr[0, 1] - 0.00) < 1e-9
        assert abs(c.corr_high[0, 1] - 0.30) < 1e-9   # sign-spanning band

    def test_order_symmetric(self) -> None:
        a = _rho(f"KXMLBGAME-{_G}-SF", f"KXMLBKS-{_G}-SFCWHISENHUNT88-8")
        b = _rho(f"KXMLBKS-{_G}-SFCWHISENHUNT88-8", f"KXMLBGAME-{_G}-SF")
        assert abs(a - b) < 1e-9

    def test_ml_x_hr_hit_hrr_signed(self) -> None:
        assert abs(_rho(f"KXMLBGAME-{_G}-COL",
                        f"KXMLBHR-{_G}-COLHGOODMAN15-1") - 0.23) < 1e-9
        assert abs(_rho(f"KXMLBGAME-{_G}-SF",
                        f"KXMLBHIT-{_G}-COLWCASTRO3-2") - (-0.23)) < 1e-9
        assert abs(_rho(f"KXMLBGAME-{_G}-SF",
                        f"KXMLBHRR-{_G}-COLHGOODMAN15-3") - (-0.37)) < 1e-9


# --- prop × prop routing ----------------------------------------------------------

class TestPropPairRouting:
    def test_facing_hit_x_ks_negative(self) -> None:
        # COL batter vs SF starter = FACING -> :opp carries -0.13
        c = _corr(f"KXMLBHIT-{_G}-COLWCASTRO3-2", f"KXMLBKS-{_G}-SFCWHISENHUNT88-6")
        assert abs(c.corr[0, 1] - (-0.13)) < 1e-9
        assert any("player_hit|player_ks:opp" in n for n in c.notes)

    def test_teammate_hit_x_ks_zero_tight(self) -> None:
        c = _corr(f"KXMLBHIT-{_G}-SFJLEE51-2", f"KXMLBKS-{_G}-SFCWHISENHUNT88-6")
        assert abs(c.corr[0, 1] - 0.00) < 1e-9
        assert abs(c.corr_high[0, 1] - 0.05) < 1e-9

    def test_teammate_hr_x_ks_falls_to_plain_unmeasured(self) -> None:
        # :same key deliberately absent -> plain 0.00 band 0.12 (never invent)
        c = _corr(f"KXMLBHR-{_G}-SFJLEE51-1", f"KXMLBKS-{_G}-SFCWHISENHUNT88-6")
        assert abs(c.corr[0, 1] - 0.00) < 1e-9
        assert abs(c.corr_high[0, 1] - 0.12) < 1e-9

    def test_facing_hr_and_hrr_x_ks(self) -> None:
        assert abs(_rho(f"KXMLBHR-{_G}-COLHGOODMAN15-1",
                        f"KXMLBKS-{_G}-SFCWHISENHUNT88-6") - (-0.075)) < 1e-9
        assert abs(_rho(f"KXMLBHRR-{_G}-COLHGOODMAN15-3",
                        f"KXMLBKS-{_G}-SFCWHISENHUNT88-6") - (-0.18)) < 1e-9

    def test_same_family_teammate_vs_opponent(self) -> None:
        assert abs(_rho(f"KXMLBHR-{_G}-COLHGOODMAN15-1",
                        f"KXMLBHR-{_G}-COLETOVAR14-1") - 0.04) < 1e-9
        assert abs(_rho(f"KXMLBHR-{_G}-COLHGOODMAN15-1",
                        f"KXMLBHR-{_G}-SFJLEE51-1") - 0.02) < 1e-9
        assert abs(_rho(f"KXMLBHRR-{_G}-COLHGOODMAN15-3",
                        f"KXMLBHRR-{_G}-COLETOVAR14-3") - 0.17) < 1e-9

    def test_same_player_cross_family_owned_by_containment_phase(self) -> None:
        # HR x HRR same player is containment-shaped, never a :same rho. Since
        # step 4 (2026-07-10) the CLASSIFIER owns the pair: HR-1 yes x HRR-3
        # yes is an EXACT arithmetic containment (a HR is 1 hit + >=1 R +
        # >=1 RBI => HRR >= 3), so it never reaches the copula at all.
        legs = [_leg(f"KXMLBHR-{_G}-COLHGOODMAN15-1"),
                _leg(f"KXMLBHRR-{_G}-COLHGOODMAN15-3")]
        rel = classify_legs(legs, _Prov())
        assert rel.kind is RelationshipKind.CONTAINMENT
        assert rel.containment == (0, 1)  # subset = the HR leg
        # RETARGETED 2026-07-10 (full table): the sgp seam ALONE (classifier
        # bypassed) now prices via the REVERSE measured cell ('hrr',3,'hr',1)
        # = 0.366, n=276,418 — restored with the full 142-cell table. This is
        # exact-consistent (P(A∧B) = P(B)·P(A|B) is an identity), so the seam
        # no longer falls to plain 0.03; it emits the conditional-implied rho.
        c = build_sgp_correlation(legs, ((0, 1),), shipped_params(),
                                  marginals=[0.5, 0.5])
        assert any("conditional" in n for n in c.notes)
        # With the synthetic 0.5/0.5 test marginals, joint = 0.5*0.366 = 0.183
        # < independence 0.25, so the conditional-implied rho is NEGATIVE
        # (-0.408) — the sign is an artifact of the fake marginals, the point
        # is the seam prices the conditional instead of plain 0.03.
        assert c.corr[0, 1] < -0.3

    def test_hr_hrr_distinct_player_routes(self) -> None:
        assert abs(_rho(f"KXMLBHR-{_G}-COLHGOODMAN15-1",
                        f"KXMLBHRR-{_G}-COLETOVAR14-3") - 0.05) < 1e-9
        assert abs(_rho(f"KXMLBHR-{_G}-COLHGOODMAN15-1",
                        f"KXMLBHRR-{_G}-SFJLEE51-3") - 0.00) < 1e-9

    def test_unrouted_cross_family_stays_plain(self) -> None:
        # hit|hr has no oriented keys (split unmeasured) -> plain 0.01
        assert abs(_rho(f"KXMLBHIT-{_G}-COLWCASTRO3-2",
                        f"KXMLBHR-{_G}-COLHGOODMAN15-1") - 0.01) < 1e-9

    def test_ks_x_ks_stays_plain(self) -> None:
        assert abs(_rho(f"KXMLBKS-{_G}-SFCWHISENHUNT88-6",
                        f"KXMLBKS-{_G}-COLRFELTNER18-6") - 0.04) < 1e-9

    def test_doubleheader_same_game_routes_cross_game_refuses(self) -> None:
        assert abs(_rho(f"KXMLBGAME-{_DH1}-STL",
                        f"KXMLBKS-{_DH1}-STLSGRAY54-7") - 0.24) < 1e-9
        # G1 x G2: different games — grouping separates them entirely
        legs = [_leg(f"KXMLBGAME-{_DH1}-STL"), _leg(f"KXMLBKS-{_DH2}-STLSGRAY54-7")]
        rel = classify_legs(legs, _Prov())
        assert rel.same_event_groups == ()


# --- config invariants -------------------------------------------------------------

def test_mlb_oriented_keys_have_bands_sorted_bases_and_plain_fallbacks() -> None:
    cfg = CorrelationConfig()
    mlb = cfg.pair_rho_by_sport["mlb"]
    oriented = [k for k in mlb if ":" in k]
    assert len(oriented) >= 22
    for k in oriented:
        assert f"mlb:{k}" in cfg.pair_rho_uncertainty, k       # 1:1 bands
        base = k.split(":")[0]
        a, b = base.split("|")
        assert pair_key(LegType(a), LegType(b)) == base, k     # sorted keys
        assert base in mlb, k                                  # plain coexists


def test_ml_oriented_point_within_neutralized_span() -> None:
    # |routed point value| must sit inside the old neutralized 0 ± wide band:
    # the neutralized band spans the POINT of either orientation (that is what
    # made shipping 0.00 safe), NOT point ± the new tighter routed band —
    # e.g. ml|hr 0.23 <= 0.28 holds while 0.23 + 0.08 = 0.31 would not.
    cfg = CorrelationConfig()
    mlb = cfg.pair_rho_by_sport["mlb"]
    for base in ("moneyline|player_ks", "moneyline|player_hr",
                 "moneyline|player_hit", "moneyline|player_hrr"):
        wide = cfg.pair_rho_uncertainty[f"mlb:{base}"]
        for o in (":same", ":opp"):
            v = mlb[base + o]
            assert abs(v) <= wide + 1e-9, base + o
