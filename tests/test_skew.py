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
from fractions import Fraction

from hypothesis import given, settings
from hypothesis import strategies as st

from combomaker.core.conventions import DOC_ASSUMED, Conventions, Side
from combomaker.core.money import CC_PER_DOLLAR, CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.marketdata.grid import PriceGrid
from combomaker.pricing.fees import FeeModel, FeeSchedule, FeeType
from combomaker.pricing.joint import JointEstimate
from combomaker.pricing.quote import ConstructedQuote, construct_quote
from combomaker.risk.exposure import (
    ExposureBook,
    ExposureSnapshot,
    LegRef,
    OpenPosition,
    OpenQuoteRisk,
)
from combomaker.risk.skew import (
    GameSkewCache,
    InventorySkew,
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
        # enabled ⇒ applied is the classifier NEGATED into the pricer frame (a
        # concentrating skew_cc >= 0 WIDENS ⇒ enters the pricer as <= 0).
        assert skew.applied_cc == -skew.skew_cc

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


# ---------------------------------------------------------------------------
# PRICER-BOUNDARY SIGN (finding #1). Feed compute_inventory_skew(...).applied_cc
# into the REAL construct_quote and assert the DIRECTION of the implied YES ask
# ($1 − no_bid): a CONCENTRATING candidate must quote a strictly HIGHER ask than
# base (dearer ⇒ we sell LESS of what we're loaded on), an OFFSETTING candidate a
# strictly LOWER ask (cheaper ⇒ we win MORE of the flattening flow). This checks
# the classifier→pricer seam end to end, not just the skew fn's decomposition.
# ---------------------------------------------------------------------------


_SCHEDULE = FeeSchedule.from_strings("0.07", "0.0175")
_TAKER_FEES = FeeModel(_SCHEDULE, DOC_ASSUMED)


def _deci_grid() -> PriceGrid:
    # Fine grid so a modest skew moves the snapped bid at all (a 1c grid can
    # swallow small shades; the direction, not the magnitude, is under test).
    return PriceGrid.from_market_payload(
        {"ticker": "T", "price_ranges": [{"start": "0.001", "end": "0.999", "step": "0.001"}]}
    )


def _implied_yes_ask_cc(skew_applied_cc: int) -> int:
    """Price the RFQ-candidate combo through the REAL construct_quote at the given
    applied skew and return the implied YES ask ($1 − no_bid). Fair 0.30, two
    legs, generous free-money caps so the clamp never masks the skew's effect."""
    quote = construct_quote(
        joint=JointEstimate(p=0.30, uncertainty=0.0, frechet_lo=0.0, frechet_hi=1.0, notes=()),
        n_legs=2,
        qty=Q(10_000),
        grid=_deci_grid(),
        fee_model=_TAKER_FEES,
        fee_type=FeeType.QUADRATIC,
        fee_multiplier=Fraction(1),
        time_to_close_s=48 * 3600.0,
        in_play=False,
        yes_cap_cc=CC(9_900),
        no_cap_cc=CC(9_900),
        inventory_skew_cc=skew_applied_cc,
    )
    assert isinstance(quote, ConstructedQuote)
    return CC_PER_DOLLAR - int(quote.no_bid_cc)


def _skew_for(cand_side: str, *, held: int = 96_000, cand: int = 10_000) -> InventorySkew:
    book = ExposureBook(CONVENTIONS)
    marginals = {"A": 0.5, "B": 0.5}
    book.add_position(
        no_position("held", (leg("A", "yes"), leg("B", "yes")), contracts=held)
    )
    snap = snapshot_of(book, marginals)
    candidate = no_position(
        "cand", (leg("A", cand_side), leg("B", cand_side)), contracts=cand
    )
    return compute_inventory_skew(
        candidate, snap, provider(marginals), CONVENTIONS, LIMITS, PARAMS
    )


class TestPricerBoundarySign:
    def test_concentrating_quotes_strictly_higher_yes_ask(self) -> None:
        base_ask = _implied_yes_ask_cc(0)
        conc = _skew_for("yes")                 # SAME shape ⇒ concentrating
        assert conc.skew_cc > 0                 # classifier: concentrating is +
        assert conc.applied_cc < 0              # ...negated into the pricer
        conc_ask = _implied_yes_ask_cc(conc.applied_cc)
        # Concentrating ⇒ DEARER combo ⇒ HIGHER ask ⇒ we sell LESS. (Pre-fix the
        # positive skew_cc flowed straight in and made the ask LOWER — backwards.)
        assert conc_ask > base_ask

    def test_offsetting_quotes_strictly_lower_yes_ask(self) -> None:
        base_ask = _implied_yes_ask_cc(0)
        off = _skew_for("no")                   # OPPOSITE shape ⇒ offsetting
        assert off.skew_cc < 0                  # classifier: offsetting is −
        assert off.applied_cc > 0               # ...negated into the pricer
        off_ask = _implied_yes_ask_cc(off.applied_cc)
        # Offsetting ⇒ CHEAPER combo ⇒ LOWER ask ⇒ we win MORE flattening flow.
        assert off_ask < base_ask

    def test_concentrating_and_offsetting_straddle_base(self) -> None:
        # The two halves are backwards of each other about the base, in the RIGHT
        # order: concentrating dearer, offsetting cheaper.
        base_ask = _implied_yes_ask_cc(0)
        conc_ask = _implied_yes_ask_cc(_skew_for("yes").applied_cc)
        off_ask = _implied_yes_ask_cc(_skew_for("no").applied_cc)
        assert off_ask < base_ask < conc_ask


# ---------------------------------------------------------------------------
# SKEW MUTEX FIX (2026-07-18) — P0-9 mutex-aware per-game direction.
# The raw delta-sum classifier is MUTEX-BLIND: long-NO on outcome B of an ME
# event where the book is short outcome A carries the SAME delta sign, so the
# hedge was widened (measured live: ARG-champ vs short-ESP-champion, 63/63).
# ---------------------------------------------------------------------------

CHAMP_EV = "KXCHAMP-G1"      # game key "G1" — same game bucket as EVENT
ML_EV = "KXML-G1"
ESP = "KXCHAMP-G1-ESP"
ARG = "KXCHAMP-G1-ARG"
ARG_ML = "KXML-G1-ARGML"


def champ_me(event: str) -> bool | None:
    """Explicit-True ME metadata for the champion + moneyline events (None
    elsewhere — never a convenient default)."""
    return True if event in (CHAMP_EV, ML_EV) else None


def me_leg(market: str, event: str = CHAMP_EV, side: str = "yes") -> LegRef:
    return LegRef(market_ticker=market, event_ticker=event, side=side)


class TestMutexAwareDirection:
    def _skew(
        self,
        book: ExposureBook,
        candidate: OpenPosition,
        marginals: dict[str, float],
        *,
        mutex: bool,
        limits: SkewLimits = LIMITS,
    ) -> InventorySkew:
        snap = book.snapshot(provider(marginals), mass_acceptance=False)
        if not mutex:
            return compute_inventory_skew(
                candidate, snap, provider(marginals), CONVENTIONS, limits, PARAMS
            )
        return compute_inventory_skew(
            candidate, snap, provider(marginals), CONVENTIONS, limits, PARAMS,
            dir_entries_by_game=snap.dir_entries_by_game,
            committed_dir_entries_by_game=snap.committed_dir_entries_by_game,
            is_me_event=book.is_me_event,
        )

    def test_live_scenario_arg_champ_hedge_flips_to_offsetting(self) -> None:
        """THE 2026-07-17 live shadow scenario: short-ESP-champion committed
        book, ARG-champ candidate. Raw read: same delta sign => widened (the
        63/63 mis-classification). Mutex-aware read: opposing outcomes of ONE
        ME event land in different branches of the P0-9 fold => the candidate
        raises the book's directional bound by NOTHING => OFFSETTING, skew <= 0
        = a rebate at the pricer."""
        book = ExposureBook(CONVENTIONS, is_me_event=champ_me)
        marginals = {ESP: 0.4, ARG: 0.4}
        book.add_position(no_position("held", (me_leg(ESP),), contracts=60_000))
        candidate = no_position("cand", (me_leg(ARG),), contracts=30_000)

        raw = self._skew(book, candidate, marginals, mutex=False)
        assert raw.skew_cc > 0                      # the measured mis-widen
        assert raw.mutex_direction_games == ()

        fixed = self._skew(book, candidate, marginals, mutex=True)
        assert fixed.mutex_direction_games == ("G1",)
        assert fixed.concentration_cc == 0          # nothing concentrates
        assert fixed.offset_cc > 0                  # the hedge earns a rebate
        assert fixed.skew_cc < 0                    # classifier: OFFSETTING
        assert fixed.skew_cc >= -PARAMS.skew_max_tighten_cc  # clamp holds
        assert fixed.shadow_applied_cc > 0          # pricer frame: cheaper combo
        # And the widen-vs-decline policy no longer sees a concentrating game.
        decision = decide_widen_or_decline(
            fixed,
            book.snapshot(provider(marginals), mass_acceptance=False),
            candidate,
            LIMITS,
            WidenPolicyParams(enabled=True, util_threshold=0.5),
        )
        assert decision.would_decline is False

    def test_partial_overhang_splits_into_both_terms_exactly(self) -> None:
        """Candidate magnitude 80 vs a 60-contract opposing book on the same ME
        event: 60 nets (rebate), the 20 overhang concentrates. Exact split at
        util == 1 (delta axis binding)."""
        tight = SkewLimits(
            max_event_delta_contracts=60.0,
            max_event_worst_case_loss_dollars=1e9,
            max_event_gross_notional_dollars=1e9,
        )
        book = ExposureBook(CONVENTIONS, is_me_event=champ_me)
        marginals = {ESP: 0.4, ARG: 0.4}
        book.add_position(no_position("held", (me_leg(ESP),), contracts=6_000))
        candidate = no_position("cand", (me_leg(ARG),), contracts=8_000)
        skew = self._skew(book, candidate, marginals, mutex=True, limits=tight)
        assert skew.mutex_direction_games == ("G1",)
        assert skew.concentration_cc == 20   # the overhang beyond the book's 60
        assert skew.offset_cc == 60          # netted, capped by the book's bound
        assert skew.skew_cc == -40
        assert skew.per_game == (("G1", -40),)

    def test_two_me_events_on_candidate_falls_back_to_raw(self) -> None:
        """Adversarial edge: a candidate whose this-game legs carry TWO
        explicit-ME events is OUTSIDE the single-ME math — falls back to the
        raw read (concentrating here; never a mutex rebate the math can't
        certify). The >= 2-ME netting belongs to the parked hedge-pair build."""
        book = ExposureBook(CONVENTIONS, is_me_event=champ_me)
        marginals = {ESP: 0.4, ARG: 0.4, ARG_ML: 0.5}
        book.add_position(no_position("held", (me_leg(ESP),), contracts=60_000))
        candidate = no_position(
            "cand", (me_leg(ARG), me_leg(ARG_ML, event=ML_EV)), contracts=30_000
        )
        skew = self._skew(book, candidate, marginals, mutex=True)
        assert skew.mutex_direction_games == ()     # not applicable => raw read
        assert skew.skew_cc >= 0                    # raw: same sign, concentrating
        assert skew.offset_cc == 0

    def test_no_me_metadata_is_byte_identical_to_raw(self) -> None:
        """A book with NO ME metadata (is_me_event=None) must classify exactly
        as the raw read even when the entries are passed — the mutex path arms
        only with BOTH inputs."""
        book = ExposureBook(CONVENTIONS)  # no is_me_event
        marginals = {ESP: 0.4, ARG: 0.4}
        book.add_position(no_position("held", (me_leg(ESP),), contracts=60_000))
        candidate = no_position("cand", (me_leg(ARG),), contracts=30_000)
        snap = book.snapshot(provider(marginals), mass_acceptance=False)
        with_entries = compute_inventory_skew(
            candidate, snap, provider(marginals), CONVENTIONS, LIMITS, PARAMS,
            dir_entries_by_game=snap.dir_entries_by_game,
            committed_dir_entries_by_game=snap.committed_dir_entries_by_game,
            is_me_event=book.is_me_event,        # None => raw
        )
        plain = compute_inventory_skew(
            candidate, snap, provider(marginals), CONVENTIONS, LIMITS, PARAMS
        )
        assert with_entries.skew_cc == plain.skew_cc
        assert with_entries.per_game == plain.per_game
        assert with_entries.mutex_direction_games == ()

    def test_committed_second_me_event_falls_back_to_raw(self) -> None:
        """2026-07-18 verify fix (the over-rebate corner): book = short
        ESP-champ (CHAMP event) + short ARG-ML (a SECOND explicit-ME event of
        the SAME game, correlated with ARG-champ); candidate = short ARG-champ.
        The single-event fold rides the ARG-ML entry as COMMON — inflating base
        and full equally, cancelling out of the marginal — so it would report
        marginal 0 => a FULL rebate for a candidate that truly CONCENTRATES
        against the real ARG branch (true 2-ME-aware split: concentration 30 /
        nettable 50 on the 100/50/80 shape). The committed census detects the
        second ME event and falls back to the raw read (today's behaviour —
        never an uncertified rebate)."""
        book = ExposureBook(CONVENTIONS, is_me_event=champ_me)
        marginals = {ESP: 0.4, ARG: 0.4, ARG_ML: 0.5}
        book.add_position(no_position("esp", (me_leg(ESP),), contracts=100_000))
        book.add_position(
            no_position("argml", (me_leg(ARG_ML, event=ML_EV),), contracts=50_000)
        )
        candidate = no_position("cand", (me_leg(ARG),), contracts=80_000)
        skew = self._skew(book, candidate, marginals, mutex=True)
        assert skew.mutex_direction_games == ()   # fallback: not a mutex game
        assert skew.offset_cc == 0                # NO rebate the math can't certify
        assert skew.skew_cc > 0                   # raw read: same sign => widen

    def test_resting_quote_on_second_me_event_does_not_suppress(self) -> None:
        """The live-shape guard: a RESTING QUOTE on the second ME event must
        NOT drive the fallback (the live 200-slot book spans both ME events of
        its games — a quote-driven fallback would kill the mutex fix exactly
        where it was built to act). Committed book on CHAMP only + a resting
        quote on ARG-ML: the mutex path stays engaged and the ARG-champ hedge
        still earns its rebate."""
        book = ExposureBook(CONVENTIONS, is_me_event=champ_me)
        marginals = {ESP: 0.4, ARG: 0.4, ARG_ML: 0.5}
        book.add_position(no_position("held", (me_leg(ESP),), contracts=60_000))
        book.upsert_quote(
            OpenQuoteRisk(
                quote_id="q-ml",
                rfq_id="r-ml",
                combo_ticker="COMBO-ML",
                collection=None,
                yes_bid_cc=CC(0),
                no_bid_cc=CC(4_000),
                contracts=Q(10_000),
                legs=(me_leg(ARG_ML, event=ML_EV),),
            )
        )
        candidate = no_position("cand", (me_leg(ARG),), contracts=30_000)
        snap = book.snapshot(provider(marginals), mass_acceptance=True)
        skew = compute_inventory_skew(
            candidate, snap, provider(marginals), CONVENTIONS, LIMITS, PARAMS,
            dir_entries_by_game=snap.dir_entries_by_game,
            committed_dir_entries_by_game=snap.committed_dir_entries_by_game,
            is_me_event=book.is_me_event,
        )
        assert skew.mutex_direction_games == ("G1",)   # still the mutex path
        assert skew.offset_cc > 0                      # the hedge still rebates
        assert skew.skew_cc < 0

    def test_committed_census_unavailable_falls_back_to_raw(self) -> None:
        """Fail-closed: passing the fold entries WITHOUT the committed census
        (``committed_dir_entries_by_game`` omitted) must classify raw — the
        mutex path never engages on an unverifiable committed book."""
        book = ExposureBook(CONVENTIONS, is_me_event=champ_me)
        marginals = {ESP: 0.4, ARG: 0.4}
        book.add_position(no_position("held", (me_leg(ESP),), contracts=60_000))
        candidate = no_position("cand", (me_leg(ARG),), contracts=30_000)
        snap = book.snapshot(provider(marginals), mass_acceptance=False)
        skew = compute_inventory_skew(
            candidate, snap, provider(marginals), CONVENTIONS, LIMITS, PARAMS,
            dir_entries_by_game=snap.dir_entries_by_game,
            is_me_event=book.is_me_event,
        )
        plain = compute_inventory_skew(
            candidate, snap, provider(marginals), CONVENTIONS, LIMITS, PARAMS
        )
        assert skew.mutex_direction_games == ()
        assert skew.skew_cc == plain.skew_cc
        assert skew.per_game == plain.per_game

    def test_snapshot_exports_dir_entries_including_quote_mass(self) -> None:
        """The snapshot's ``dir_entries_by_game`` carries the P0-9 entries the
        directional fold consumed: positions always, resting quotes under mass
        acceptance."""
        book = ExposureBook(CONVENTIONS, is_me_event=champ_me)
        marginals = {ESP: 0.4, ARG: 0.4}
        book.add_position(no_position("held", (me_leg(ESP),), contracts=60_000))
        book.upsert_quote(
            OpenQuoteRisk(
                quote_id="q1",
                rfq_id="r1",
                combo_ticker="COMBO-Q",
                collection=None,
                yes_bid_cc=CC(0),
                no_bid_cc=CC(4_000),
                contracts=Q(10_000),
                legs=(me_leg(ARG),),
            )
        )
        assert book.is_me_event is champ_me      # the property is the callable
        no_mass = book.snapshot(provider(marginals), mass_acceptance=False)
        assert len(no_mass.dir_entries_by_game["G1"]) == 1
        mass = book.snapshot(provider(marginals), mass_acceptance=True)
        assert len(mass.dir_entries_by_game["G1"]) == 2
        # 2026-07-18 verify fix: the COMMITTED-only census is positions-only in
        # BOTH modes — the resting quote never enters it (quote entries must
        # not drive the second-ME-event fallback).
        assert len(no_mass.committed_dir_entries_by_game["G1"]) == 1
        assert len(mass.committed_dir_entries_by_game["G1"]) == 1
        assert (
            mass.committed_dir_entries_by_game["G1"]
            == no_mass.committed_dir_entries_by_game["G1"]
        )
