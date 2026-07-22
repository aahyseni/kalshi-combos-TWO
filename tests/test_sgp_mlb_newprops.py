"""MLB new prop families OUTS / RBI / SB — wired 2026-07-22.

Pins the behavior two adversarial judges verified (measurement + wiring, reports
`docs/reports/2026-07-22-mlb-newprops-adversarial-judge-*.md`): classification +
collision safety, the same-PITCHER ks×outs :same routing (the seam-2 fix), RBI
rung keys, the ml×sb divergence, the HR⇒RBI / RBI⇒HRR exact containments, and
fail-closed on the unmeasured outs×batter cell. Source of the values:
`docs/calibration/staged_mlb_new_props.md` §4. Real ticker shapes (live-verified
2026-07-22): 4-segment, rung = LAST hyphen segment (player-id token also ends in
digits)."""

from __future__ import annotations

from combomaker.ops.config import CorrelationConfig
from combomaker.pricing.legtypes import LegType, classify_leg, pair_key
from combomaker.pricing.relationships import RelationshipKind, classify_legs
from combomaker.pricing.sgp import SgpParams, build_sgp_correlation
from combomaker.rfq.models import RfqLeg

_G = "26JUL092145COLSF"  # real game code (COL @ SF)
_PIT = "SFCWHISENHUNT88"  # SF starting pitcher segment
_BAT = "COLHGOODMAN15"    # COL batter segment


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


class _Prov:
    def event_mutually_exclusive(self, e: str) -> bool:
        return False


def _leg(mt: str, side: str = "yes") -> RfqLeg:
    return RfqLeg(mt, "-".join(mt.split("-")[:2]), side, None)


def _corr(a: str, b: str, sa: str = "yes", sb: str = "yes"):
    legs = [_leg(a, sa), _leg(b, sb)]
    rel = classify_legs(legs, _Prov())
    return rel, build_sgp_correlation(
        legs, rel.same_event_groups, _params(), marginals=[0.5, 0.5]
    ) if rel.kind is RelationshipKind.OK else None


def _rho(a: str, b: str) -> float:
    _, c = _corr(a, b)
    assert c is not None
    return float(c.corr[0][1])


# --- classification + collision safety -------------------------------------------

class TestNewFamilyClassification:
    def test_three_families_classify(self) -> None:
        assert classify_leg(f"KXMLBOUTS-{_G}-{_PIT}-17") is LegType.PLAYER_OUTS
        assert classify_leg(f"KXMLBRBI-{_G}-{_BAT}-2") is LegType.PLAYER_RBI
        assert classify_leg(f"KXMLBSB-{_G}-{_BAT}-1") is LegType.PLAYER_SB

    def test_mls_soccer_btts_not_stolen_base(self) -> None:
        # bare "SB" would hit KXMLSBTTS (MLS soccer BTTS); the MLB-anchored
        # keyword must NOT — it classifies as its real (soccer) type, never SB.
        assert classify_leg("KXMLSBTTS-ABC-XYZ") is not LegType.PLAYER_SB

    def test_existing_families_unchanged(self) -> None:
        assert classify_leg(f"KXMLBHRR-{_G}-{_BAT}-3") is LegType.PLAYER_HRR
        assert classify_leg(f"KXMLBHR-{_G}-{_BAT}-1") is LegType.PLAYER_HR
        assert classify_leg(f"KXMLBKS-{_G}-{_PIT}-6") is LegType.PLAYER_KS

    def test_sort_traps(self) -> None:
        assert pair_key(LegType.PLAYER_KS, LegType.PLAYER_OUTS) == "player_ks|player_outs"
        assert pair_key(LegType.PLAYER_HR, LegType.PLAYER_RBI) == "player_hr|player_rbi"
        assert pair_key(LegType.PLAYER_HIT, LegType.PLAYER_SB) == "player_hit|player_sb"


# --- the seam-2 same-pitcher ks×outs fix -----------------------------------------

class TestSamePitcherOutsKs:
    def test_same_pitcher_routes_same_not_plain(self) -> None:
        # Same PITCHER segment: ks × outs is a copula rho routed :same (+0.56),
        # NOT the plain 0.30 fallback (the seam-2 fix).
        c = _rho(f"KXMLBOUTS-{_G}-{_PIT}-17", f"KXMLBKS-{_G}-{_PIT}-6")
        assert abs(c - 0.56) < 1e-9

    def test_unlisted_rung_falls_to_same_never_interpolated(self) -> None:
        # r17 is not a wired rung (12/15/18/21) -> un-runged :same 0.56, never a
        # value interpolated between r15 (0.53) and r18 (0.44).
        assert abs(_rho(f"KXMLBOUTS-{_G}-{_PIT}-17", f"KXMLBKS-{_G}-{_PIT}-6") - 0.56) < 1e-9
        assert abs(_rho(f"KXMLBOUTS-{_G}-{_PIT}-15", f"KXMLBKS-{_G}-{_PIT}-6") - 0.53) < 1e-9

    def test_opposing_starters_routes_opp(self) -> None:
        # COL pitcher outs × SF pitcher Ks = opposing starters -> :opp 0.045.
        c = _rho(f"KXMLBOUTS-{_G}-COLRFELTNER18-17", f"KXMLBKS-{_G}-{_PIT}-6")
        assert abs(c - 0.045) < 1e-9


# --- oriented + rung-keyed values ------------------------------------------------

class TestOrientedAndRungs:
    def test_outs_total_and_ml(self) -> None:
        assert abs(_rho(f"KXMLBOUTS-{_G}-{_PIT}-17", f"KXMLBTOTAL-{_G}-9") - (-0.50)) < 1e-9
        assert abs(_rho(f"KXMLBGAME-{_G}-SF", f"KXMLBOUTS-{_G}-{_PIT}-17") - 0.43) < 1e-9
        assert abs(_rho(f"KXMLBGAME-{_G}-COL", f"KXMLBOUTS-{_G}-{_PIT}-17") - (-0.43)) < 1e-9

    def test_rbi_total_rungs_monotone(self) -> None:
        r1 = _rho(f"KXMLBRBI-{_G}-{_BAT}-1", f"KXMLBTOTAL-{_G}-9")
        r3 = _rho(f"KXMLBRBI-{_G}-{_BAT}-3", f"KXMLBTOTAL-{_G}-9")
        assert abs(r1 - 0.31) < 1e-9
        assert abs(r3 - 0.42) < 1e-9
        assert r3 > r1  # rung-monotone

    def test_ml_sb_divergent_value(self) -> None:
        # ml×sb = +0.15 (DIVERGED from ≈0 prior; judge-confirmed REAL, WIDEN-ONLY)
        assert abs(_rho(f"KXMLBGAME-{_G}-COL", f"KXMLBSB-{_G}-{_BAT}-1") - 0.15) < 1e-9

    def test_sb_total_near_zero_band_spans_zero(self) -> None:
        _, c = _corr(f"KXMLBSB-{_G}-{_BAT}-1", f"KXMLBTOTAL-{_G}-9")
        assert c is not None
        assert abs(c.corr[0, 1] - 0.02) < 1e-9
        assert c.corr_low[0, 1] < 0.0 < c.corr_high[0, 1]


# --- containments ----------------------------------------------------------------

class TestNewContainments:
    def test_hr_implies_rbi(self) -> None:
        rel, _ = _corr(f"KXMLBHR-{_G}-{_BAT}-1", f"KXMLBRBI-{_G}-{_BAT}-1")
        assert rel.kind is RelationshipKind.CONTAINMENT
        assert rel.containment == (0, 1)  # subset = the HR leg

    def test_hr_yes_rbi_no_impossible_and_not_farmable(self) -> None:
        rel, _ = _corr(f"KXMLBHR-{_G}-{_BAT}-1", f"KXMLBRBI-{_G}-{_BAT}-1", "yes", "no")
        assert rel.kind is RelationshipKind.IMPOSSIBLE
        assert rel.farmable is False  # MLB 48h rain scalar -> never farmable

    def test_rbi_implies_hrr(self) -> None:
        rel, _ = _corr(f"KXMLBRBI-{_G}-{_BAT}-1", f"KXMLBHRR-{_G}-{_BAT}-1")
        assert rel.kind is RelationshipKind.CONTAINMENT

    def test_sb_hit_is_not_a_containment(self) -> None:
        # SB⇒HIT is REFUTED (17.2% hitless-SB games) — must price via a
        # conditional cell, never a containment/impossible verdict.
        rel, c = _corr(f"KXMLBSB-{_G}-{_BAT}-1", f"KXMLBHIT-{_G}-{_BAT}-1")
        assert rel.kind is RelationshipKind.OK
        assert c is not None
        assert any("conditional" in n for n in c.notes)


# --- fail-closed on the unmeasured outs×batter cell ------------------------------

def test_outs_x_batter_is_fail_closed_wide() -> None:
    # player_hit|player_outs (batter hit × pitcher outs) is UNMEASURED (staged
    # doc queue). It must fall to the flat default with a band that SPANS ZERO —
    # never a confident pin.
    _, c = _corr(f"KXMLBHIT-{_G}-{_BAT}-1", f"KXMLBOUTS-{_G}-{_PIT}-17")
    assert c is not None
    assert c.corr_low[0, 1] <= 0.0  # honestly uncertain (fail-closed-wide)


def test_zero_gaps_no_new_family_pair_falls_to_flat() -> None:
    """ZERO-GAPS mandate (operator + playbook Stage 3/7): no reachable OUTS/RBI/SB
    same-game pair may price the flat +0.6 default. Enumerate every family pair
    involving a new family; assert none resolves to a flat/no-prior source."""
    import itertools
    base = {
        "moneyline": f"KXMLBGAME-{_G}-COL", "total": f"KXMLBTOTAL-{_G}-9",
        "spread": f"KXMLBSPREAD-{_G}-COL2", "rfi": f"KXMLBRFI-{_G}",
        "ks": f"KXMLBKS-{_G}-COLYP1-6", "hit": f"KXMLBHIT-{_G}-COLAB1-1",
        "hr": f"KXMLBHR-{_G}-COLAB1-1", "tb": f"KXMLBTB-{_G}-COLAB1-2",
        "hrr": f"KXMLBHRR-{_G}-COLAB1-2", "outs": f"KXMLBOUTS-{_G}-COLYP1-17",
        "rbi": f"KXMLBRBI-{_G}-COLAB1-1", "sb": f"KXMLBSB-{_G}-COLAB1-1",
    }
    alt = {  # distinct player/team for cross-entity + same-family pairs
        "ks": f"KXMLBKS-{_G}-SFZP2-6", "hit": f"KXMLBHIT-{_G}-SFXB2-1",
        "hr": f"KXMLBHR-{_G}-SFXB2-1", "tb": f"KXMLBTB-{_G}-SFXB2-2",
        "hrr": f"KXMLBHRR-{_G}-SFXB2-2", "outs": f"KXMLBOUTS-{_G}-SFZP2-18",
        "rbi": f"KXMLBRBI-{_G}-SFXB2-1", "sb": f"KXMLBSB-{_G}-SFXB2-1",
    }
    new = {"outs", "rbi", "sb"}
    flat = []
    for fa, fb in itertools.combinations_with_replacement(list(base), 2):
        if not ({fa, fb} & new):
            continue
        legs = [_leg(base[fa]), _leg(alt.get(fb, base[fb]))]
        rel = classify_legs(legs, _Prov())
        if rel.kind is not RelationshipKind.OK:
            continue
        co = build_sgp_correlation(legs, rel.same_event_groups, _params(), marginals=[0.5, 0.5])
        notes = [n for n in co.notes if "pair" in n.lower() or "conditional" in n.lower()]
        if notes and ("flat prior" in notes[0] or "no prior" in notes[0]):
            flat.append(f"{fa}|{fb}")
    assert flat == [], f"new-family pairs still on the flat default: {flat}"


def test_new_family_rho_band_one_to_one() -> None:
    cfg = CorrelationConfig()
    mlb = cfg.pair_rho_by_sport["mlb"]
    fams = ("player_outs", "player_rbi", "player_sb")
    new = [k for k in mlb if any(f in k for f in fams)]
    assert len(new) == 133  # new-props (61) + gap-pairs (80) - 8 dead outs×spread keys
    for k in new:
        assert f"mlb:{k}" in cfg.pair_rho_uncertainty, k  # every point has a band
        base = k.split(":")[0]
        a, b = base.split("|")
        assert pair_key(LegType(a), LegType(b)) == base, k  # sorted key
