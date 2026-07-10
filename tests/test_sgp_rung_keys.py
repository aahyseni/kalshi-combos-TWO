""":rN rung-key lookup mechanism (Phase 2 B1, wire-list convention line 2).

Rung-keyed families: player_hit / player_hr / player_tb / player_hrr / spread.
player_ks / total / moneyline / rfi NEVER carry a rung even when their ticker
ends in digits. Suffixes chain in pair_key leg order (first leg's rung first);
fallback chain: exact rung key → un-runged oriented key → plain key →
flat-default fail-closed. NO rung interpolation/extrapolation, ever.

ISOLATION: another agent is concurrently rewriting the shipped mlb table in
ops/config.py, so every test here injects a SYNTHETIC pair-rho table — this
file must never import CorrelationConfig or assert shipped values. Poison
entries (values only reachable by constructing a forbidden key) prove the
never-runged / wrong-order keys are never built.
"""

from __future__ import annotations

from combomaker.pricing.legtypes import LegType
from combomaker.pricing.sgp import (
    SgpCorrelation,
    SgpParams,
    _leg_rung,
    _pair_rung_suffix,
    build_sgp_correlation,
)
from combomaker.rfq.models import RfqLeg

_G = "26JUL092145COLSF"  # real game-code shape (COL @ SF, 2026-07-09 live)

# Real ticker shapes (test_sgp_mlb_routing.py / NOTES.md K2 conventions).
HIT1_COL = f"KXMLBHIT-{_G}-COLWCASTRO3-1"
HIT2_COL = f"KXMLBHIT-{_G}-COLWCASTRO3-2"
HIT2_SF = f"KXMLBHIT-{_G}-SFJLEE51-2"
HITX_COL = f"KXMLBHIT-{_G}-COLWCASTRO3-X"  # unparseable line segment
HR2_COL = f"KXMLBHR-{_G}-COLHGOODMAN15-2"
TB4_COL = f"KXMLBTB-{_G}-COLWCASTRO3-4"
HRR3_COL = f"KXMLBHRR-{_G}-COLHGOODMAN15-3"
KS6_SF = f"KXMLBKS-{_G}-SFCWHISENHUNT88-6"
SP2_COL = f"KXMLBSPREAD-{_G}-COL2"
SP3_COL = f"KXMLBSPREAD-{_G}-COL3"
SP3_SF = f"KXMLBSPREAD-{_G}-SF3"
SP2_SF = f"KXMLBSPREAD-{_G}-SF2"
SP5_COL = f"KXMLBSPREAD-{_G}-COL5"
SP_NOLINE = f"KXMLBSPREAD-{_G}-COL"  # team parses, rung does not
ML_SF = f"KXMLBGAME-{_G}-SF"
TOTAL9 = f"KXMLBTOTAL-{_G}-9"
RFI = f"KXMLBRFI-{_G}"

_MLB = {
    # hit|spread: full three-level ladder (exact rung → oriented → plain).
    "player_hit|spread": 0.00,
    "player_hit|spread:same": 0.27,
    "player_hit|spread:opp": -0.19,
    "player_hit|spread:same:r1:r2": 0.239,
    "player_hit|spread:same:r2:r5": 0.304,
    "player_hit|spread:opp:r1:r3": -0.172,
    # POISON — reversed chain order must never be constructed.
    "player_hit|spread:same:r2:r1": 0.777,
    "player_hit|spread:same:r5:r2": 0.777,
    # ks|spread: single rung (the SPREAD leg's; ks never runged).
    "player_ks|spread": 0.00,
    "player_ks|spread:same": 0.19,
    "player_ks|spread:same:r2": 0.207,
    # POISON — only reachable if the ks line -6 leaked a rung.
    "player_ks|spread:same:r6:r2": 0.888,
    # ks|tb: single rung (the TB leg's).
    "player_ks|player_tb": -0.06,
    "player_ks|player_tb:opp": -0.12,
    "player_ks|player_tb:opp:r4": -0.103,
    # hr|total: ORIENTATION-FREE rung key on the plain base.
    "player_hr|total": 0.24,
    "player_hr|total:r2": 0.306,
    # POISON — only reachable if the TOTAL line -9 leaked a rung.
    "player_hr|total:r2:r9": 0.999,
    # ml|ks: neither leg rung-keyed; POISON the runged variant.
    "moneyline|player_ks:same": 0.24,
    "moneyline|player_ks:same:r6": 0.666,
    # tb|spread: oriented level only (no exact rung wired).
    "player_tb|spread": 0.00,
    "player_tb|spread:same": 0.28,
    # hrr|spread: plain level only.
    "player_hrr|spread": 0.11,
    # hit|rfi: rung key on the plain base (rfi never runged, hit is).
    "player_hit|rfi": 0.065,
    "player_hit|rfi:r2": 0.085,
}
_BANDS = {
    "mlb:player_hit|spread": 0.31,
    "mlb:player_hit|spread:same": 0.08,
    "mlb:player_hit|spread:same:r1:r2": 0.05,
    # NOTE: 'mlb:player_hit|spread:opp:r1:r3' band deliberately ABSENT →
    # typed_uncertainty default at THAT level (same-level invariant).
    "mlb:player_ks|spread:same": 0.07,
    "mlb:player_ks|spread:same:r2": 0.05,
    "mlb:player_ks|player_tb:opp:r4": 0.04,
    "mlb:player_hr|total:r2": 0.04,
    "mlb:player_tb|spread:same": 0.06,
    "mlb:player_hrr|spread": 0.42,
}

_TYPED_BAND = 0.15
_DEFAULT_RHO = 0.6
_UNTYPED_BAND = 0.30


def _params(
    mlb: dict[str, float] | None = None, bands: dict[str, float] | None = None
) -> SgpParams:
    return SgpParams(
        pair_rho={},
        default_rho=_DEFAULT_RHO,
        cross_event_rho=0.0,
        typed_uncertainty=_TYPED_BAND,
        untyped_uncertainty=_UNTYPED_BAND,
        pair_uncertainty=dict(_BANDS if bands is None else bands),
        pair_rho_by_sport={"mlb": dict(_MLB if mlb is None else mlb)},
    )


def _leg(mt: str) -> RfqLeg:
    return RfqLeg(mt, "-".join(mt.split("-")[:2]), "yes", None)


def _corr(a: str, b: str) -> SgpCorrelation:
    # build_sgp_correlation called directly with an explicit same-event group:
    # isolates sgp.py from the (concurrently edited) classifier/config stack.
    return build_sgp_correlation([_leg(a), _leg(b)], ((0, 1),), _params())


def _rho_band(a: str, b: str) -> tuple[float, float]:
    c = _corr(a, b)
    return float(c.corr[0, 1]), float(c.corr_high[0, 1] - c.corr[0, 1])


# --- rung extraction unit tests ---------------------------------------------------


class TestLegRung:
    def test_prop_trailing_line_int(self) -> None:
        assert _leg_rung(LegType.PLAYER_HIT, HIT1_COL) == 1
        assert _leg_rung(LegType.PLAYER_HR, HR2_COL) == 2
        assert _leg_rung(LegType.PLAYER_TB, TB4_COL) == 4
        assert _leg_rung(LegType.PLAYER_HRR, HRR3_COL) == 3

    def test_spread_team_plus_digits(self) -> None:
        assert _leg_rung(LegType.SPREAD, SP2_COL) == 2
        assert _leg_rung(LegType.SPREAD, SP5_COL) == 5

    def test_never_runged_families_even_with_trailing_digits(self) -> None:
        assert _leg_rung(LegType.PLAYER_KS, KS6_SF) is None  # ks line -6
        assert _leg_rung(LegType.TOTAL, TOTAL9) is None      # total line -9
        assert _leg_rung(LegType.MONEYLINE, ML_SF) is None
        assert _leg_rung(LegType.RFI, RFI) is None

    def test_unparseable_rung_is_none_never_guessed(self) -> None:
        assert _leg_rung(LegType.PLAYER_HIT, HITX_COL) is None      # line 'X'
        assert _leg_rung(LegType.SPREAD, SP_NOLINE) is None         # no digits
        assert _leg_rung(LegType.PLAYER_HIT, f"KXMLBHIT-{_G}-COLWCASTRO3") is None


class TestPairRungSuffix:
    def test_chained_in_pair_key_leg_order_first_leg_first(self) -> None:
        # 'player_hit' < 'spread' → hit's rung first, either argument order.
        assert _pair_rung_suffix(HIT1_COL, SP2_COL) == ":r1:r2"
        assert _pair_rung_suffix(SP2_COL, HIT1_COL) == ":r1:r2"
        assert _pair_rung_suffix(HIT2_COL, SP5_COL) == ":r2:r5"

    def test_single_suffix_when_one_leg_rung_keyed(self) -> None:
        assert _pair_rung_suffix(KS6_SF, SP2_SF) == ":r2"    # spread's rung
        assert _pair_rung_suffix(KS6_SF, TB4_COL) == ":r4"   # tb's rung
        assert _pair_rung_suffix(HR2_COL, TOTAL9) == ":r2"   # hr's rung
        assert _pair_rung_suffix(RFI, HIT2_COL) == ":r2"     # hit's rung

    def test_no_rung_keyed_leg_is_empty(self) -> None:
        assert _pair_rung_suffix(ML_SF, KS6_SF) == ""
        assert _pair_rung_suffix(ML_SF, TOTAL9) == ""

    def test_any_unparseable_rung_collapses_whole_chain(self) -> None:
        # A partial chain would collide with the single-suffix grammar.
        assert _pair_rung_suffix(HITX_COL, SP2_COL) == ""
        assert _pair_rung_suffix(HIT1_COL, SP_NOLINE) == ""

    def test_equal_type_pair_is_order_independent(self) -> None:
        assert _pair_rung_suffix(HIT1_COL, HIT2_SF) == ":r1:r2"
        assert _pair_rung_suffix(HIT2_SF, HIT1_COL) == ":r1:r2"


# --- exact rung keys through the copula builder ------------------------------------


class TestExactRungKeys:
    def test_chained_prop_x_spread_same_team(self) -> None:
        # COL batter hit 1+ × COL wins by 2+ → 'player_hit|spread:same:r1:r2'.
        rho, band = _rho_band(HIT1_COL, SP2_COL)
        assert abs(rho - 0.239) < 1e-9
        assert abs(band - 0.05) < 1e-9

    def test_chain_order_never_reversed(self) -> None:
        # Poison keys ':r2:r1'/':r5:r2' (0.777) must be unreachable.
        rho, _ = _rho_band(HIT2_COL, SP5_COL)
        assert abs(rho - 0.304) < 1e-9
        for a, b in ((HIT1_COL, SP2_COL), (HIT2_COL, SP5_COL)):
            assert abs(_rho_band(a, b)[0] - 0.777) > 0.4

    def test_orientation_flips_with_spread_team(self) -> None:
        # COL batter × SF wins by 3+ → :opp:r1:r3; band ABSENT at that level
        # → typed default, NOT the :same/:plain bands (same-level invariant).
        rho, band = _rho_band(HIT1_COL, SP3_SF)
        assert abs(rho - (-0.172)) < 1e-9
        assert abs(band - _TYPED_BAND) < 1e-9

    def test_single_rung_ks_x_spread_spread_leg_owns_the_rung(self) -> None:
        # SF pitcher Ks (line -6, NEVER runged) × SF by 2+ → ':same:r2';
        # poison ':same:r6:r2' (0.888) unreachable.
        rho, band = _rho_band(KS6_SF, SP2_SF)
        assert abs(rho - 0.207) < 1e-9
        assert abs(band - 0.05) < 1e-9

    def test_single_rung_ks_x_tb_facing(self) -> None:
        # COL batter TB 4+ × SF starter Ks = FACING → ':opp:r4' (tb's rung).
        rho, band = _rho_band(TB4_COL, KS6_SF)
        assert abs(rho - (-0.103)) < 1e-9
        assert abs(band - 0.04) < 1e-9

    def test_orientation_free_rung_on_plain_base(self) -> None:
        # hr 2+ × game total: no orientation level → 'player_hr|total:r2';
        # poison ':r2:r9' (0.999, total leaking its -9) unreachable.
        rho, band = _rho_band(HR2_COL, TOTAL9)
        assert abs(rho - 0.306) < 1e-9
        assert abs(band - 0.04) < 1e-9

    def test_rfi_never_runged_but_prop_rung_still_keys(self) -> None:
        rho, _ = _rho_band(RFI, HIT2_COL)
        assert abs(rho - 0.085) < 1e-9  # 'player_hit|rfi:r2'

    def test_ml_x_ks_stays_un_runged(self) -> None:
        # Neither leg rung-keyed: poison ':same:r6' (0.666) unreachable.
        rho, _ = _rho_band(ML_SF, KS6_SF)
        assert abs(rho - 0.24) < 1e-9

    def test_leg_order_symmetric(self) -> None:
        a, _ = _rho_band(HIT1_COL, SP2_COL)
        b, _ = _rho_band(SP2_COL, HIT1_COL)
        assert abs(a - b) < 1e-12


# --- fallback chain ----------------------------------------------------------------


class TestFallbackChain:
    def test_missing_exact_rung_falls_to_oriented(self) -> None:
        # hit r2 × COL by 3+: ':same:r2:r3' not wired → ':same' 0.27 with ITS
        # band 0.08 — never a neighbouring rung (no interpolation, ever).
        rho, band = _rho_band(HIT2_COL, SP3_COL)
        assert abs(rho - 0.27) < 1e-9
        assert abs(band - 0.08) < 1e-9

    def test_oriented_level_without_exact_rung(self) -> None:
        # tb|spread wires ':same' only → r4:r3 misses to ':same' 0.28.
        rho, band = _rho_band(TB4_COL, SP3_COL)
        assert abs(rho - 0.28) < 1e-9
        assert abs(band - 0.06) < 1e-9

    def test_plain_only_pair_falls_all_the_way(self) -> None:
        # hrr|spread wires plain only → 0.11 with the plain band 0.42.
        rho, band = _rho_band(HRR3_COL, SP2_COL)
        assert abs(rho - 0.11) < 1e-9
        assert abs(band - 0.42) < 1e-9

    def test_nothing_wired_falls_to_flat_default_fail_closed(self) -> None:
        # hr|spread absent at every level → flat default rho with the
        # sign-spanning fallback band (0.6 ± 0.9, clamped to [-0.3, 0.95]).
        c = _corr(HR2_COL, SP2_COL)
        assert abs(float(c.corr[0, 1]) - _DEFAULT_RHO) < 1e-9
        assert abs(float(c.corr_high[0, 1]) - 0.95) < 1e-9  # clamp of 0.6+0.9
        assert float(c.corr_low[0, 1]) < 0.0  # low bound spans the negative regime

    def test_unparseable_prop_rung_falls_to_oriented(self) -> None:
        # hit line 'X' (orientation still parses) → ':same' 0.27.
        rho, band = _rho_band(HITX_COL, SP2_COL)
        assert abs(rho - 0.27) < 1e-9
        assert abs(band - 0.08) < 1e-9

    def test_unparseable_spread_rung_falls_to_oriented(self) -> None:
        # spread suffix 'COL' (team parses, no line digits) → ':same' 0.27.
        rho, band = _rho_band(HIT1_COL, SP_NOLINE)
        assert abs(rho - 0.27) < 1e-9
        assert abs(band - 0.08) < 1e-9

    def test_value_and_band_resolve_at_the_same_level(self) -> None:
        # Three pairs, three levels — each (value, band) tuple must be one
        # level's OWN pairing; a cross-level mix would mismatch one member.
        for a, b, want_rho, want_band in (
            (HIT1_COL, SP2_COL, 0.239, 0.05),   # exact-rung level
            (HIT2_COL, SP3_COL, 0.27, 0.08),    # oriented level
            (HRR3_COL, SP2_COL, 0.11, 0.42),    # plain level
        ):
            rho, band = _rho_band(a, b)
            assert abs(rho - want_rho) < 1e-9, (a, b)
            assert abs(band - want_band) < 1e-9, (a, b)
