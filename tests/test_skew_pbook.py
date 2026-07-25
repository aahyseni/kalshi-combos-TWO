"""P(book) steering component, Phase B1 (operator directive 2026-07-25:
"Pbook should be steering our betting, more variance = higher Pbook").

The component is SHADOW-FIRST: ``pbook_armed`` defaults False, so ``skew_cc``
(and therefore pricing) is byte-identical whether or not a profile is wired —
pinned below — while ``pbook_cc`` is computed + logged for the
derive-before-arm measurement. Semantics pinned:

- same-way add on an OVERWEIGHT game (the DET one-way shape) ⇒ WIDEN;
- offsetting an overweight game (the missing other side)     ⇒ REBATE;
- ANY flow on an underweight/empty game (new-game variance)  ⇒ REBATE;
- absent/stale profile, empty tail, disabled                 ⇒ EXACTLY 0;
- magnitude scales with the P(book) deficit (a healthy 0.9 book barely
  steers; the 7/23 one-way 0.40 book steers hard).
"""

from __future__ import annotations

from collections.abc import Callable

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.skew import (
    PBookProfile,
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

PARAMS = SkewParams(enabled=True)  # pbook_enabled=True, pbook_armed=False
GEN = 7


def provider(mapping: dict[str, float]) -> Callable[[str], float | None]:
    return lambda ticker: mapping.get(ticker)


def leg(market: str, event: str) -> LegRef:
    return LegRef(market_ticker=market, event_ticker=event, side="yes")


def no_position(
    pid: str, legs: tuple[LegRef, ...], *, contracts: int = 10_000
) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker="COMBO",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(contracts),
        entry_price_cc=CentiCents(5_000),
        legs=legs,
    )


def _one_way_book() -> tuple[ExposureBook, dict[str, float]]:
    """A book concentrated one-way on game G1 (the DET shape)."""
    book = ExposureBook(CONVENTIONS)
    marginals = {"A": 0.5, "B": 0.5, "C": 0.5}
    book.add_position(
        no_position("held", (leg("A", "KX-G1"),), contracts=60_000)
    )
    return book, marginals


def _profile(
    *, p_book: float = 0.40, shares: dict[str, float] | None = None, gen: int = GEN
) -> PBookProfile:
    return PBookProfile(
        input_generation=gen,
        p_book=p_book,
        tail_share_by_game=(
            {"G1": 0.90, "G2": 0.10} if shares is None else shares
        ),
    )


def _skew(candidate, book, marginals, profile, *, params=PARAMS, gen=GEN):
    snap = book.snapshot(provider(marginals), mass_acceptance=False)
    return compute_inventory_skew(
        candidate,
        snap,
        provider(marginals),
        CONVENTIONS,
        LIMITS,
        params,
        pbook_profile=profile,
        pbook_book_generation=gen,
    )


class TestShadowInvariant:
    def test_skew_cc_byte_identical_while_unarmed(self) -> None:
        """The load-bearing shadow property: with pbook_armed=False the
        composed skew_cc is IDENTICAL with and without a profile — pricing
        cannot move until the operator arms the steer."""
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("A", "KX-G1"),))
        with_profile = _skew(cand, book, marginals, _profile())
        without = _skew(cand, book, marginals, None)
        assert with_profile.skew_cc == without.skew_cc
        assert with_profile.applied_cc == without.applied_cc
        assert with_profile.pbook_cc != 0  # ...but the signal IS measured
        assert without.pbook_cc == 0

    def test_armed_adds_the_component(self) -> None:
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("A", "KX-G1"),))
        armed = SkewParams(enabled=True, pbook_armed=True)
        shadow = _skew(cand, book, marginals, _profile())
        live = _skew(cand, book, marginals, _profile(), params=armed)
        assert live.skew_cc == shadow.skew_cc + shadow.pbook_cc


class TestSteeringSemantics:
    def test_same_way_on_overweight_game_widens(self) -> None:
        """The DET shape: adding same-way risk to the game that already
        dominates the tail ⇒ positive (widen) component."""
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("A", "KX-G1"),))
        skew = _skew(cand, book, marginals, _profile())
        assert skew.pbook_cc > 0
        assert any(r[3] == "pbook_concentrating" for r in skew.pbook_per_game)

    def test_offsetting_overweight_game_rebates(self) -> None:
        """The missing other side of the concentrated game earns a rebate."""
        book, marginals = _one_way_book()
        # Opposite side of the SAME game: a NO on the complementary leg has
        # the opposite delta sign on G1.
        cand = no_position("cand", (LegRef(market_ticker="A", event_ticker="KX-G1", side="no"),))
        skew = _skew(cand, book, marginals, _profile())
        assert skew.pbook_cc < 0
        assert any(r[3] == "pbook_offsetting" for r in skew.pbook_per_game)

    def test_new_game_variance_rebates(self) -> None:
        """A bet on a game the book does not touch at all — the purest
        variance-adder (what the 7/23 slate was missing) — earns a rebate
        even though the directional component is neutral there."""
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("C", "KX-G3"),))
        skew = _skew(cand, book, marginals, _profile())
        assert skew.concentration_cc == 0 and skew.offset_cc == 0  # dir-neutral
        assert skew.pbook_cc < 0
        assert any(r[3] == "pbook_diversifying" for r in skew.pbook_per_game)

    def test_magnitude_scales_with_p_book_deficit(self) -> None:
        """A healthy book (p_book 0.9) steers far less than the one-way 0.40
        book — the need factor is measured, never a target."""
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("A", "KX-G1"),))
        needy = _skew(cand, book, marginals, _profile(p_book=0.40))
        healthy = _skew(cand, book, marginals, _profile(p_book=0.90))
        assert needy.pbook_cc > healthy.pbook_cc > 0

    def test_balanced_book_is_neutral(self) -> None:
        """A perfectly diversified profile (uniform shares) steers nothing."""
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("A", "KX-G1"),))
        skew = _skew(
            cand, book, marginals,
            _profile(shares={"G1": 0.5, "G2": 0.5}, p_book=0.40),
        )
        assert skew.pbook_cc == 0


class TestFailSafe:
    def test_absent_profile_is_zero(self) -> None:
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("A", "KX-G1"),))
        assert _skew(cand, book, marginals, None).pbook_cc == 0

    def test_stale_generation_is_zero(self) -> None:
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("A", "KX-G1"),))
        skew = _skew(cand, book, marginals, _profile(gen=GEN - 1))
        assert skew.pbook_cc == 0
        assert any(r[3] == "stale_profile" for r in skew.pbook_per_game)

    def test_disabled_is_zero(self) -> None:
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("A", "KX-G1"),))
        off = SkewParams(enabled=True, pbook_enabled=False)
        assert _skew(cand, book, marginals, _profile(), params=off).pbook_cc == 0

    def test_empty_tail_is_zero(self) -> None:
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("A", "KX-G1"),))
        skew = _skew(cand, book, marginals, _profile(shares={}))
        assert skew.pbook_cc == 0

    def test_component_bounded_by_documented_caps(self) -> None:
        """The clamp: never beyond the documented peak hard caps."""
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("A", "KX-G1"),))
        skew = _skew(
            cand, book, marginals,
            _profile(p_book=0.0, shares={"G1": 1.0}),
        )
        assert -PARAMS.peak_tighten_max_cc <= skew.pbook_cc <= PARAMS.peak_widen_max_cc
