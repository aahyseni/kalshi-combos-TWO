"""LEG-DIRECTION AXIS (operator directive 2026-07-25: "the bot needs to
recognize direction for all legs, and know when to start raising its
markups").

The gap this closes: P0-9 nets only moneyline-family ME events, so six
same-direction strikeout-over combos across six games looked directionless
to every existing axis (the 7/25 K-ladder book: ~$127 riding one arm across
combos, invisible until hand-decomposed). Pinned here:

- exposure: (family × side) / (family:entity × side) keys off REAL ticker
  shapes; full combo loss attributed per key, deduped per position (two
  rungs of one ladder count once); COMMITTED positions only;
- skew: cross-game same-family widen (the gap itself), other-side rebate,
  same-entity widen, absent-key diversification rebate, caps-derived onset,
  p_book=None / empty book ⇒ neutral;
- shadow-first: leg_axis_armed=False keeps ``skew_cc`` byte-identical with
  and without a profile; arming adds family_cc + entity_cc.
"""

from __future__ import annotations

from collections.abc import Callable

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.risk.exposure import (
    ExposureBook,
    LegRef,
    OpenPosition,
    leg_entity_key,
    leg_family_key,
)
from combomaker.risk.skew import (
    LegAxisProfile,
    SkewLimits,
    SkewParams,
    compute_inventory_skew,
)

CONVENTIONS = Conventions(
    verified=True,
    source="test",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)

LIMITS = SkewLimits(
    max_event_delta_contracts=500.0,
    max_event_worst_case_loss_dollars=100_000.0,
    max_event_gross_notional_dollars=500_000.0,
)

PARAMS = SkewParams(enabled=True)  # leg_axis_enabled=True, leg_axis_armed=False

# Real live ticker shapes (sampled from the 2026-07-25 tape — the
# source-of-truth rule: never invent ticker grammar).
KS_CEASE_5 = "KXMLBKS-26JUL251610SDMIA-SDDCEASE44-5"
KS_CEASE_6 = "KXMLBKS-26JUL251610SDMIA-SDDCEASE44-6"
KS_SKENES_5 = "KXMLBKS-26JUL251905PITATL-PITPSKENES30-5"
HR_SCHWARBER = "KXMLBHR-26JUL251805NYYPHI-PHIKSCHWARBER12-1"
RFI_LAASF = "KXMLBRFI-26JUL251605LAASF"
GAME_BAL = "KXMLBGAME-26JUL251905ATLBAL-BAL"


def provider(mapping: dict[str, float]) -> Callable[[str], float | None]:
    return lambda ticker: mapping.get(ticker)


def leg(market: str, event: str, side: str = "yes") -> LegRef:
    return LegRef(market_ticker=market, event_ticker=event, side=side)


def no_position(
    pid: str,
    legs: tuple[LegRef, ...],
    *,
    contracts: int = 10_000,
    combo: str = "COMBO",
) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=combo,
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(contracts),
        entry_price_cc=CentiCents(5_000),
        legs=legs,
    )


class TestKeys:
    def test_family_key_is_series_and_side(self) -> None:
        assert leg_family_key(leg(KS_CEASE_5, "EV")) == "KXMLBKS:yes"
        assert leg_family_key(leg(KS_CEASE_5, "EV", side="no")) == "KXMLBKS:no"
        assert leg_family_key(leg(GAME_BAL, "EV")) == "KXMLBGAME:yes"

    def test_entity_key_folds_rungs_and_keeps_direction(self) -> None:
        k5 = leg_entity_key(leg(KS_CEASE_5, "EV"))
        k6 = leg_entity_key(leg(KS_CEASE_6, "EV"))
        assert k5 == "KXMLBKS:SDDCEASE44:yes"
        assert k5 == k6  # every rung of one ladder is ONE key
        assert leg_entity_key(leg(KS_CEASE_5, "EV", side="no")) != k5

    def test_entity_key_degrades_to_coarser_never_drops(self) -> None:
        # 2-segment series (game-scoped): entity = the game blob.
        assert (
            leg_entity_key(leg(RFI_LAASF, "EV"))
            == "KXMLBRFI:26JUL251605LAASF:yes"
        )
        # Unknown single-segment shape: the whole ticker.
        assert leg_entity_key(leg("WEIRD", "EV")) == "WEIRD:WEIRD:yes"


class TestExposureAccumulation:
    def test_family_and_entity_attribution_across_combos(self) -> None:
        book = ExposureBook(CONVENTIONS)
        marginals = {KS_CEASE_5: 0.5, KS_SKENES_5: 0.5, KS_CEASE_6: 0.5}
        p1 = no_position(
            "p1", (leg(KS_CEASE_5, "KX-G1"), leg(KS_SKENES_5, "KX-G2")),
            combo="C1",
        )
        p2 = no_position("p2", (leg(KS_CEASE_6, "KX-G1"),), combo="C2")
        book.add_position(p1)
        book.add_position(p2)
        snap = book.snapshot(provider(marginals), mass_acceptance=False)
        fam = snap.committed_loss_by_family_cc
        ent = snap.committed_loss_by_entity_cc
        # Both combos ride K-overs: family key carries BOTH losses.
        assert fam["KXMLBKS:yes"] == p1.max_loss_cc + p2.max_loss_cc
        # Cease appears in both combos (different rungs): ONE entity key
        # carries both — the cross-combo "$127 on one arm" measurement.
        assert (
            ent["KXMLBKS:SDDCEASE44:yes"] == p1.max_loss_cc + p2.max_loss_cc
        )
        assert ent["KXMLBKS:PITPSKENES30:yes"] == p1.max_loss_cc

    def test_two_rungs_in_one_combo_count_once(self) -> None:
        book = ExposureBook(CONVENTIONS)
        marginals = {KS_CEASE_5: 0.5, KS_CEASE_6: 0.5}
        p = no_position(
            "p", (leg(KS_CEASE_5, "KX-G1"), leg(KS_CEASE_6, "KX-G1"))
        )
        book.add_position(p)
        snap = book.snapshot(provider(marginals), mass_acceptance=False)
        assert snap.committed_loss_by_family_cc["KXMLBKS:yes"] == p.max_loss_cc
        assert (
            snap.committed_loss_by_entity_cc["KXMLBKS:SDDCEASE44:yes"]
            == p.max_loss_cc
        )

    def test_candidates_and_extras_are_never_counted(self) -> None:
        book = ExposureBook(CONVENTIONS)
        marginals = {KS_CEASE_5: 0.5, KS_SKENES_5: 0.5}
        book.add_position(no_position("held", (leg(KS_CEASE_5, "KX-G1"),)))
        extra = no_position("cand", (leg(KS_SKENES_5, "KX-G2"),))
        snap = book.snapshot(
            provider(marginals), mass_acceptance=False, extra_positions=[extra]
        )
        fam = snap.committed_loss_by_family_cc
        assert "KXMLBKS:PITPSKENES30:yes" not in snap.committed_loss_by_entity_cc
        assert fam["KXMLBKS:yes"] == book.positions["held"].max_loss_cc


def _k_heavy_profile(
    *,
    p_book: float | None = 0.45,
    budget_cc: float = 1.0e6,
    total_cc: float = 1.0e6,
) -> LegAxisProfile:
    """A book 90% short K-overs by family; Cease carrying most of it."""
    return LegAxisProfile(
        shares_by_family={"KXMLBKS:yes": 0.9, "KXMLBTOTAL:no": 0.1},
        total_family_cc=total_cc,
        shares_by_entity={
            "KXMLBKS:SDDCEASE44:yes": 0.7,
            "KXMLBKS:PITPSKENES30:yes": 0.2,
            "KXMLBTOTAL:7.5:no": 0.1,
        },
        total_entity_cc=total_cc,
        budget_cc=budget_cc,
        p_book=p_book,
    )


def _skew(candidate, profile, *, params=PARAMS):
    book = ExposureBook(CONVENTIONS)
    snap = book.snapshot(provider({}), mass_acceptance=False)
    return compute_inventory_skew(
        candidate,
        snap,
        provider({}),
        CONVENTIONS,
        LIMITS,
        params,
        leg_axis_profile=profile,
    )


class TestLegAxisComponent:
    def test_cross_game_same_family_widens(self) -> None:
        # THE gap: a K-over on a game the book has NO position in still
        # widens, because the FAMILY direction is overweight.
        cand = no_position("c", (leg(KS_SKENES_5, "KX-G9"),))
        skew = _skew(cand, _k_heavy_profile())
        assert skew.family_cc > 0
        reasons = {r for _k, _a, _f, r in skew.leg_axis_rows}
        assert "leg_concentrating" in reasons

    def test_other_side_of_the_ladder_rebates(self) -> None:
        cand = no_position("c", (leg(KS_SKENES_5, "KX-G9", side="no"),))
        skew = _skew(cand, _k_heavy_profile())
        assert skew.family_cc < 0  # KXMLBKS:no is the missing direction

    def test_same_entity_widens_more_than_new_entity(self) -> None:
        same = _skew(
            no_position("c1", (leg(KS_CEASE_6, "KX-G1"),)), _k_heavy_profile()
        )
        new = _skew(
            no_position("c2", (leg(HR_SCHWARBER, "KX-G3"),)),
            _k_heavy_profile(),
        )
        assert same.entity_cc > 0  # another Cease rung: the $127-arm shape
        assert new.entity_cc < 0  # brand-new entity: diversification rebate
        assert new.family_cc < 0  # brand-new family direction too

    def test_onset_scales_with_the_enforced_budget(self) -> None:
        cand = no_position("c", (leg(KS_SKENES_5, "KX-G9"),))
        near_cap = _skew(
            cand, _k_heavy_profile(total_cc=1.0e6, budget_cc=1.0e6)
        )
        tiny_book = _skew(
            cand, _k_heavy_profile(total_cc=1.0e4, budget_cc=1.0e6)
        )
        assert near_cap.family_cc > tiny_book.family_cc >= 0

    def test_unknown_p_book_and_empty_book_are_neutral(self) -> None:
        cand = no_position("c", (leg(KS_SKENES_5, "KX-G9"),))
        no_pb = _skew(cand, _k_heavy_profile(p_book=None))
        assert no_pb.family_cc == 0 and no_pb.entity_cc == 0
        empty = LegAxisProfile(
            shares_by_family={},
            total_family_cc=0.0,
            shares_by_entity={},
            total_entity_cc=0.0,
            budget_cc=1.0e6,
            p_book=0.45,
        )
        sk = _skew(cand, empty)
        assert sk.family_cc == 0 and sk.entity_cc == 0


class TestShadowFirst:
    def test_unarmed_is_byte_identical_with_and_without_profile(self) -> None:
        cand = no_position("c", (leg(KS_SKENES_5, "KX-G9"),))
        with_profile = _skew(cand, _k_heavy_profile())
        without = _skew(cand, None)
        assert with_profile.skew_cc == without.skew_cc
        assert with_profile.applied_cc == without.applied_cc
        assert with_profile.family_cc > 0  # measured, just not applied

    def test_armed_adds_family_and_entity(self) -> None:
        cand = no_position("c", (leg(KS_SKENES_5, "KX-G9"),))
        armed = SkewParams(enabled=True, leg_axis_armed=True)
        dark = _skew(cand, _k_heavy_profile())
        live = _skew(cand, _k_heavy_profile(), params=armed)
        assert live.skew_cc == dark.skew_cc + dark.family_cc + dark.entity_cc
