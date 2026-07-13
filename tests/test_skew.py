"""Tests for combomaker.risk.skew — the inventory-aware quote skew (R3 Part A)
and the widen-vs-DECLINE policy (R3 Part R2).

The three MANDATORY property tests (R3 §A6):

1. SIGN SAFETY (load-bearing): an OFFSETTING candidate returns skew <= 0
   (tightens the NO bid — we win more), a CONCENTRATING candidate returns
   skew >= 0 (widens — we sell less), and a candidate touching an EMPTY-book
   game returns exactly 0.
2. SELL-ONLY invariant survives across the skew's whole range — extended in
   test_quote.py (draw skew from compute_inventory_skew's [−tighten, +widen]).
3. NO-ARB survives: a large negative (tightening) skew never lets no_bid exceed
   the free-money clamp — exercised in test_quote.py against the clamp.

Plus: offset-rebate boundedness, the convex monotone-in-utilisation widen, the
empty-book/unknown-marginal cases, the DARK-ship applied_cc, and the widen
policy's shadow/enabled + offsetting-never-declined behaviour.
"""

from __future__ import annotations

from collections.abc import Callable

from hypothesis import given, settings
from hypothesis import strategies as st

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.risk.exposure import (
    ExposureBook,
    ExposureSnapshot,
    LegRef,
    OpenPosition,
)
from combomaker.risk.skew import (
    GameSkewCache,
    SkewLimits,
    SkewParams,
    WidenPolicyParams,
    compute_inventory_skew,
    decide_widen_or_decline,
)

CC = CentiCents
Q = CentiContracts

CONVENTIONS = Conventions(
    verified=True,
    source="test",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)

# Headroom denominators sized so the delta axis is the binding utilisation for
# the positions below (contracts are CENTI: a position of 96_000 centi = 960
# contracts, game delta = 960 × 0.5 other-marginal = 480 contracts ≈ cap).
LIMITS = SkewLimits(
    max_event_delta_contracts=500.0,
    max_event_worst_case_loss_dollars=100_000.0,   # loose: keep delta binding
    max_event_gross_notional_dollars=500_000.0,    # loose: keep delta binding
)

PARAMS = SkewParams(enabled=True)  # enable so skew_cc == applied_cc in tests

# One game, two leg markets. event_ticker "KX-G1" ⇒ game key "G1".
EVENT = "KX-G1"
GAME = "G1"


def provider(mapping: dict[str, float]) -> Callable[[str], float | None]:
    return lambda ticker: mapping.get(ticker)


def no_position(
    pid: str,
    legs: tuple[LegRef, ...],
    *,
    contracts: int = 100,
    entry_price: int = 5_000,
) -> OpenPosition:
    """A LONG-NO position (our_side=NO) — the only side a sell-only book holds."""
    return OpenPosition(
        position_id=pid,
        combo_ticker="COMBO",
        collection=None,
        our_side=Side.NO,
        contracts=Q(contracts),
        entry_price_cc=CC(entry_price),
        legs=legs,
    )


def leg(market: str, side: str) -> LegRef:
    return LegRef(market_ticker=market, event_ticker=EVENT, side=side)


def snapshot_of(book: ExposureBook, marginals: dict[str, float]) -> ExposureSnapshot:
    return book.snapshot(provider(marginals), mass_acceptance=False)


# ---------------------------------------------------------------------------
# (i) SIGN SAFETY — the load-bearing property.
# ---------------------------------------------------------------------------


class TestSignSafety:
    def test_empty_book_game_returns_exactly_zero(self) -> None:
        # No positions ⇒ delta_by_game empty ⇒ every touched game is empty ⇒ 0.
        book = ExposureBook(CONVENTIONS)
        marginals = {"A": 0.5, "B": 0.5}
        snap = snapshot_of(book, marginals)
        candidate = no_position("cand", (leg("A", "yes"), leg("B", "yes")))
        skew = compute_inventory_skew(
            candidate, snap, provider(marginals), CONVENTIONS, LIMITS, PARAMS
        )
        assert skew.skew_cc == 0
        assert skew.concentration_cc == 0
        assert skew.offset_cc == 0

    def test_concentrating_candidate_is_nonnegative(self) -> None:
        # Book already long-NO of a {A yes, B yes} combo ⇒ net delta on the game
        # is negative (NO position, yes legs). A candidate with the SAME shape
        # ADDS to that direction ⇒ concentration ⇒ skew >= 0.
        book = ExposureBook(CONVENTIONS)
        marginals = {"A": 0.5, "B": 0.5}
        book.add_position(
            no_position("held", (leg("A", "yes"), leg("B", "yes")), contracts=60_000)
        )
        snap = snapshot_of(book, marginals)
        candidate = no_position("cand", (leg("A", "yes"), leg("B", "yes")), contracts=10_000)
        skew = compute_inventory_skew(
            candidate, snap, provider(marginals), CONVENTIONS, LIMITS, PARAMS
        )
        assert skew.skew_cc >= 0
        assert skew.offset_cc == 0
        assert skew.applied_cc == skew.skew_cc  # enabled

    def test_offsetting_candidate_is_nonpositive(self) -> None:
        # Book long-NO of {A yes, B yes} (game net negative). A candidate long-NO
        # of {A no, B no} has the OPPOSITE per-game delta sign ⇒ offset ⇒ rebate
        # ⇒ skew <= 0.
        book = ExposureBook(CONVENTIONS)
        marginals = {"A": 0.5, "B": 0.5}
        book.add_position(
            no_position("held", (leg("A", "yes"), leg("B", "yes")), contracts=60_000)
        )
        snap = snapshot_of(book, marginals)
        candidate = no_position("cand", (leg("A", "no"), leg("B", "no")), contracts=10_000)
        skew = compute_inventory_skew(
            candidate, snap, provider(marginals), CONVENTIONS, LIMITS, PARAMS
        )
        assert skew.skew_cc <= 0
        assert skew.concentration_cc == 0

    def test_offset_never_exceeds_tighten_cap(self) -> None:
        # A huge overweight book + a large offsetting candidate: the rebate is
        # clamped to −skew_max_tighten_cc, never deeper.
        book = ExposureBook(CONVENTIONS)
        marginals = {"A": 0.5, "B": 0.5}
        for i in range(20):
            book.add_position(
                no_position(f"h{i}", (leg("A", "yes"), leg("B", "yes")), contracts=60_000)
            )
        snap = snapshot_of(book, marginals)
        candidate = no_position(
            "cand", (leg("A", "no"), leg("B", "no")), contracts=900_000
        )
        skew = compute_inventory_skew(
            candidate,
            snap,
            provider(marginals),
            CONVENTIONS,
            LIMITS,
            SkewParams(enabled=True, skew_max_tighten_cc=150),
        )
        assert skew.skew_cc >= -150

    def test_concentration_never_exceeds_widen_cap(self) -> None:
        book = ExposureBook(CONVENTIONS)
        marginals = {"A": 0.5, "B": 0.5}
        for i in range(20):
            book.add_position(
                no_position(f"h{i}", (leg("A", "yes"), leg("B", "yes")), contracts=60_000)
            )
        snap = snapshot_of(book, marginals)
        candidate = no_position(
            "cand", (leg("A", "yes"), leg("B", "yes")), contracts=900_000
        )
        skew = compute_inventory_skew(
            candidate,
            snap,
            provider(marginals),
            CONVENTIONS,
            LIMITS,
            SkewParams(enabled=True, skew_max_widen_cc=600),
        )
        assert skew.skew_cc <= 600


# ---------------------------------------------------------------------------
# DARK SHIP — applied_cc is 0 while disabled, the honest number is still logged.
# ---------------------------------------------------------------------------


class TestDarkShip:
    def test_disabled_applies_zero_but_computes_honest(self) -> None:
        book = ExposureBook(CONVENTIONS)
        marginals = {"A": 0.5, "B": 0.5}
        book.add_position(
            no_position("held", (leg("A", "yes"), leg("B", "yes")), contracts=60_000)
        )
        snap = snapshot_of(book, marginals)
        candidate = no_position("cand", (leg("A", "yes"), leg("B", "yes")), contracts=10_000)
        skew = compute_inventory_skew(
            candidate,
            snap,
            provider(marginals),
            CONVENTIONS,
            LIMITS,
            SkewParams(enabled=False),
        )
        assert skew.applied_cc == 0        # dark: passed as 0 to the pricer
        assert skew.skew_cc >= 0           # ...but the honest number is nonzero
        assert not skew.enabled


# ---------------------------------------------------------------------------
# Offset-rebate boundedness + convex monotone-in-utilisation widen.
# ---------------------------------------------------------------------------


class TestShape:
    def test_widen_is_monotone_in_utilisation(self) -> None:
        # Same concentrating candidate; a MORE overweight book (higher util)
        # never pays LESS widen than a less overweight one (convex ramp). Held in
        # centi-contracts: 12_000..96_000 centi ⇒ game delta 60..480 (cap 500).
        marginals = {"A": 0.5, "B": 0.5}
        candidate = no_position("cand", (leg("A", "yes"), leg("B", "yes")), contracts=10_000)
        last = -1
        for held in (12_000, 24_000, 48_000, 72_000, 96_000):
            book = ExposureBook(CONVENTIONS)
            book.add_position(
                no_position("held", (leg("A", "yes"), leg("B", "yes")), contracts=held)
            )
            snap = snapshot_of(book, marginals)
            skew = compute_inventory_skew(
                candidate, snap, provider(marginals), CONVENTIONS, LIMITS, PARAMS
            )
            assert skew.skew_cc >= last
            last = skew.skew_cc
        assert last > 0  # by the top util the widen is strictly positive

    def test_offset_bounded_by_min_of_candidate_and_net(self) -> None:
        # A tiny offsetting candidate against a big book: the rebate is bounded
        # by min(d, |net|)·util — a small d ⇒ small rebate, never the whole cap.
        book = ExposureBook(CONVENTIONS)
        marginals = {"A": 0.5, "B": 0.5}
        for i in range(10):
            book.add_position(
                no_position(f"h{i}", (leg("A", "yes"), leg("B", "yes")), contracts=60_000)
            )
        snap = snapshot_of(book, marginals)
        tiny = no_position("cand", (leg("A", "no"), leg("B", "no")), contracts=200)
        skew = compute_inventory_skew(
            tiny, snap, provider(marginals), CONVENTIONS, LIMITS, PARAMS
        )
        # d ≈ 1 contract-equiv (2 contracts × 0.5 other-marginal); rebate is
        # min(d, |net|)·util ≈ 1·util ⇒ a couple cc at most, never the −150 cap.
        assert -3 <= skew.skew_cc <= 0


# ---------------------------------------------------------------------------
# Empty-book / unknown-marginal safety.
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_unknown_marginal_yields_zero_skew(self) -> None:
        # A missing marginal ⇒ analytic_leg_deltas None ⇒ empty candidate map ⇒
        # no term contributes ⇒ skew 0 (never a fabricated non-zero).
        book = ExposureBook(CONVENTIONS)
        marginals = {"A": 0.5, "B": 0.5}
        book.add_position(
            no_position("held", (leg("A", "yes"), leg("B", "yes")), contracts=60_000)
        )
        snap = snapshot_of(book, marginals)
        candidate = no_position(
            "cand", (leg("A", "yes"), leg("MISSING", "yes")), contracts=10_000
        )
        skew = compute_inventory_skew(
            candidate, snap, provider({"A": 0.5}), CONVENTIONS, LIMITS, PARAMS
        )
        assert skew.skew_cc == 0

    def test_candidate_on_untouched_game_earns_no_rebate(self) -> None:
        # A candidate offsetting a game we have ZERO position in earns nothing
        # (net==0 ⇒ empty-book branch ⇒ 0).
        book = ExposureBook(CONVENTIONS)
        marginals = {"A": 0.5, "B": 0.5, "C": 0.5, "D": 0.5}
        book.add_position(
            no_position("held", (leg("A", "yes"), leg("B", "yes")), contracts=60_000)
        )
        snap = snapshot_of(book, marginals)
        other = LegRef("C", "KX-G2", "no")
        other2 = LegRef("D", "KX-G2", "no")
        candidate = no_position("cand", (other, other2), contracts=10_000)
        skew = compute_inventory_skew(
            candidate, snap, provider(marginals), CONVENTIONS, LIMITS, PARAMS
        )
        assert skew.skew_cc == 0


# ---------------------------------------------------------------------------
# Slow-path ΔES cache override (direction hint).
# ---------------------------------------------------------------------------


class TestCacheOverride:
    def test_cache_flips_direction(self) -> None:
        # Book long-NO of {A yes, B yes} (analytic game dir negative). A candidate
        # of the same shape ADDS (concentrates) analytically. If the cache says
        # the book is adverse the OTHER way (+1), the same candidate now OPPOSES
        # ⇒ becomes an offset (skew flips sign).
        book = ExposureBook(CONVENTIONS)
        marginals = {"A": 0.5, "B": 0.5}
        book.add_position(
            no_position("held", (leg("A", "yes"), leg("B", "yes")), contracts=60_000)
        )
        snap = snapshot_of(book, marginals)
        candidate = no_position("cand", (leg("A", "yes"), leg("B", "yes")), contracts=10_000)
        analytic = compute_inventory_skew(
            candidate, snap, provider(marginals), CONVENTIONS, LIMITS, PARAMS
        )
        assert analytic.skew_cc >= 0
        # candidate delta sign for {A yes, B yes} NO position is negative; the
        # book analytic dir is also negative (aligns). Force the book dir to +1.
        cache = GameSkewCache(direction_by_game={GAME: 1})
        overridden = compute_inventory_skew(
            candidate,
            snap,
            provider(marginals),
            CONVENTIONS,
            LIMITS,
            PARAMS,
            cache=cache,
        )
        assert overridden.skew_cc <= 0  # now treated as offsetting


# ---------------------------------------------------------------------------
# Property sweep — the honest skew always sits in [−tighten, +widen].
# ---------------------------------------------------------------------------


class TestRangeProperty:
    @settings(derandomize=True, max_examples=300, deadline=None)
    @given(
        held=st.integers(min_value=0, max_value=500),
        cand=st.integers(min_value=1, max_value=500),
        cand_side=st.sampled_from(["yes", "no"]),
        widen_cap=st.integers(min_value=0, max_value=1_200),
        tighten_cap=st.integers(min_value=0, max_value=400),
        gamma=st.floats(min_value=0.5, max_value=4.0),
    )
    def test_skew_always_within_caps(
        self,
        held: int,
        cand: int,
        cand_side: str,
        widen_cap: int,
        tighten_cap: int,
        gamma: float,
    ) -> None:
        book = ExposureBook(CONVENTIONS)
        marginals = {"A": 0.5, "B": 0.5}
        if held > 0:
            book.add_position(
                no_position("held", (leg("A", "yes"), leg("B", "yes")), contracts=held)
            )
        snap = snapshot_of(book, marginals)
        candidate = no_position(
            "cand", (leg("A", cand_side), leg("B", cand_side)), contracts=cand
        )
        params = SkewParams(
            enabled=True,
            gamma=gamma,
            skew_max_widen_cc=widen_cap,
            skew_max_tighten_cc=tighten_cap,
        )
        skew = compute_inventory_skew(
            candidate, snap, provider(marginals), CONVENTIONS, LIMITS, params
        )
        assert -tighten_cap <= skew.skew_cc <= widen_cap
        assert skew.concentration_cc >= 0
        assert skew.offset_cc >= 0


# ---------------------------------------------------------------------------
# Widen-vs-DECLINE policy (R3 Part R2).
# ---------------------------------------------------------------------------


class TestWidenPolicy:
    def _concentrated_book(self, held: int) -> tuple[ExposureBook, dict[str, float]]:
        book = ExposureBook(CONVENTIONS)
        marginals = {"A": 0.5, "B": 0.5}
        book.add_position(
            no_position("held", (leg("A", "yes"), leg("B", "yes")), contracts=held)
        )
        return book, marginals

    def test_declines_near_cap_on_concentrating_flow(self) -> None:
        book, marginals = self._concentrated_book(held=96_000)  # util ~0.96 delta
        snap = snapshot_of(book, marginals)
        candidate = no_position("cand", (leg("A", "yes"), leg("B", "yes")), contracts=10_000)
        skew = compute_inventory_skew(
            candidate, snap, provider(marginals), CONVENTIONS, LIMITS, PARAMS
        )
        decision = decide_widen_or_decline(
            skew, snap, candidate, LIMITS, WidenPolicyParams(enabled=True)
        )
        assert decision.would_decline
        assert decision.applied  # enabled ⇒ takes effect

    def test_shadow_mode_logs_but_does_not_apply(self) -> None:
        book, marginals = self._concentrated_book(held=96_000)
        snap = snapshot_of(book, marginals)
        candidate = no_position("cand", (leg("A", "yes"), leg("B", "yes")), contracts=10_000)
        skew = compute_inventory_skew(
            candidate, snap, provider(marginals), CONVENTIONS, LIMITS, PARAMS
        )
        decision = decide_widen_or_decline(
            skew, snap, candidate, LIMITS, WidenPolicyParams(enabled=False)
        )
        assert decision.would_decline      # honest verdict still fires
        assert not decision.applied        # ...but shadow: zero live impact

    def test_offsetting_candidate_never_declined_near_cap(self) -> None:
        book, marginals = self._concentrated_book(held=96_000)
        snap = snapshot_of(book, marginals)
        # Offsetting shape: {A no, B no} against a {A yes, B yes} book.
        candidate = no_position("cand", (leg("A", "no"), leg("B", "no")), contracts=10_000)
        skew = compute_inventory_skew(
            candidate, snap, provider(marginals), CONVENTIONS, LIMITS, PARAMS
        )
        decision = decide_widen_or_decline(
            skew, snap, candidate, LIMITS, WidenPolicyParams(enabled=True)
        )
        assert not decision.would_decline  # balancing flow is welcome near a cap

    def test_far_from_cap_does_not_decline(self) -> None:
        book, marginals = self._concentrated_book(held=4_000)  # low util
        snap = snapshot_of(book, marginals)
        candidate = no_position("cand", (leg("A", "yes"), leg("B", "yes")), contracts=10_000)
        skew = compute_inventory_skew(
            candidate, snap, provider(marginals), CONVENTIONS, LIMITS, PARAMS
        )
        decision = decide_widen_or_decline(
            skew, snap, candidate, LIMITS, WidenPolicyParams(enabled=True)
        )
        assert not decision.would_decline
