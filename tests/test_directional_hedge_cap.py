"""P0-9 — Directional-cap hedge semantics (mutex credit, subset-dominance proof).

Covers the ported ``_mutex_directional_game_cc`` (risk/exposure.py) — a
MUTUAL-EXCLUSION-AWARE directional bound that awards hedge credit to opposing
mutually-exclusive positions (ARG-advance vs ENG-advance) instead of the
independence-proxy sum ``delta_by_game`` — and its wiring into
``ExposureBook.snapshot`` + the R2 directional cap (risk/limits.py item 4).

Mandatory tests (plan P0-9):
  - ARG concentration INCREASES direction (same-outcome entries sum);
  - ENG balance gets JUSTIFIED credit (opposite outcomes net to max);
  - the ALL-ACCEPTED bound DOMINATES every realizable accepted subset (property,
    like Stage B's mass-acceptance dominance);
  - unproven MULTIPLE-ME structure FAILS CLOSED (>=2 ME events ⇒ summed magnitude).

Parity target: tools/proto_mutex_directional.py (the prototype validated first per
hard rule 8; the live port must equal it to the cent on the same inputs).
"""
from __future__ import annotations

import itertools

from hypothesis import given, settings
from hypothesis import strategies as st

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CC_PER_DOLLAR, CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.risk.exposure import (
    ExposureBook,
    LegRef,
    OpenPosition,
    _mutex_directional_game_cc,
)
from combomaker.risk.limits import DailyPnl, LimitChecker, RiskLimits

CC = CentiCents
Q = CentiContracts
CONV = Conventions(
    verified=True, source="test",
    maker_side_on_yes_accept=Side.YES, maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True, maker_is_taker_on_fill=False, combo_no_pays_complement=True,
)

ADV = "KXWCADVANCE-26JUL15ENGARG"
ARG = LegRef("KXWCADVANCE-26JUL15ENGARG-ARG", ADV, "yes")
ENG = LegRef("KXWCADVANCE-26JUL15ENGARG-ENG", ADV, "yes")
NO_ENG = LegRef("KXWCADVANCE-26JUL15ENGARG-ENG", ADV, "no")
TOT = LegRef("KXWCTOTAL-26JUL15ENGARG-3", "KXWCTOTAL-26JUL15ENGARG", "yes")  # not ME
ML = "KXWCGAME-26JUL15ENGARG"
ML_ARG = LegRef("KXWCGAME-26JUL15ENGARG-ARG", ML, "yes")


def ME(e):        # only advance is mutually exclusive
    return True if e == ADV else None


def ME2(e):       # advance AND moneyline both ME
    return True if e in (ADV, ML) else None


def D(legs, mag, requires=True):
    return (tuple(legs), float(mag), requires)


# ------------------------- pure function (parity with prototype) -----------
class TestDirectionalPureFunction:
    def test_arg_concentration_increases_direction(self):
        # Two same-outcome (ARG) directional entries SUM — concentration, no hedge.
        assert _mutex_directional_game_cc([D([ARG], 10), D([ARG], 10)], ME) == 20

    def test_eng_balance_gets_justified_credit(self):
        # ARG-advance vs ENG-advance are mutually exclusive → net to max(10,10)=10,
        # NOT the independence-sum 20. This is the hedge credit P0-9 awards.
        assert _mutex_directional_game_cc([D([ARG], 10), D([ENG], 10)], ME) == 10

    def test_no_provider_is_summed(self):
        assert _mutex_directional_game_cc([D([ARG], 10), D([ENG], 10)], None) == 20

    def test_no_me_event_is_summed(self):
        assert _mutex_directional_game_cc([D([TOT], 10), D([TOT], 10)], ME) == 20

    def test_no_leg_requires_other_branch(self):
        # NO-ENG requires ARG (2-way) → both need ARG → summed 20 (no hedge).
        assert _mutex_directional_game_cc([D([ARG], 10), D([NO_ENG], 10)], ME) == 20
        # NO-ENG (needs ARG) vs YES-ENG (needs ENG) → hedge → max 10.
        assert _mutex_directional_game_cc([D([NO_ENG], 10), D([ENG], 10)], ME) == 10

    def test_common_leg_pressures_every_branch(self):
        assert (
            _mutex_directional_game_cc([D([ARG], 10), D([ENG], 10), D([TOT], 5)], ME)
            == 15
        )

    def test_unproven_multiple_me_fails_closed(self):
        # advance + moneyline both ME → 2 ME events → fail closed to summed 20.
        assert _mutex_directional_game_cc([D([ARG], 10), D([ML_ARG], 10)], ME2) == 20

    def test_non_no_side_is_common(self):
        # requires_all False (YES-side / unknown) ⇒ common ⇒ counts in every branch
        # ⇒ no hedge dimension created ⇒ summed.
        assert (
            _mutex_directional_game_cc([D([ARG], 10, False), D([ENG], 10, False)], ME)
            == 20
        )

    def test_empty(self):
        assert _mutex_directional_game_cc([], ME) == 0

    def test_bounds_invariants(self):
        book = [D([ARG], 7), D([ENG], 11), D([TOT], 5)]
        b = _mutex_directional_game_cc(book, ME)
        assert b <= sum(e[1] for e in book)   # <= summed magnitude
        assert b >= max(e[1] for e in book)   # >= largest single entry

    def test_parity_with_prototype(self):
        # Hard rule 8: the live port must equal the validated prototype to the cent.
        from tools.proto_mutex_directional import DirEntry, game_directional_mutex

        def proto_me(e):
            return True if e == ADV else None

        cases = [
            [D([ARG], 10), D([ARG], 10)],
            [D([ARG], 10), D([ENG], 10)],
            [D([ARG], 10), D([NO_ENG], 10)],
            [D([NO_ENG], 10), D([ENG], 10)],
            [D([ARG], 10), D([ENG], 10), D([TOT], 5)],
            [D([ARG], 7), D([ENG], 11), D([TOT], 5)],
            [],
        ]
        for entries in cases:
            live = _mutex_directional_game_cc(entries, ME)
            proto = game_directional_mutex(
                [
                    DirEntry(
                        tuple((g.market_ticker, g.event_ticker, g.side) for g in legs),
                        mag,
                        req,
                    )
                    for legs, mag, req in entries
                ],
                proto_me,
            )
            assert live == proto, (entries, live, proto)


# ------------------- monotonicity / subset dominance (property) ------------
_LEGSETS = st.sampled_from(
    [(ARG,), (ENG,), (NO_ENG,), (TOT,), (ML_ARG,), (ARG, TOT), (ENG, TOT)]
)


@given(
    entries=st.lists(
        st.tuples(_LEGSETS, st.integers(1, 500), st.booleans()), min_size=0, max_size=7
    )
)
@settings(max_examples=400, deadline=None)
def test_all_accepted_dominates_every_accepted_subset(entries):
    """The ALL-ACCEPTED directional bound dominates every realizable accepted
    subset — the exact mass-acceptance dominance invariant (E2), proved here for
    the directional axis just as Stage B proves it for the loss axis. Monotonicity
    (adding an entry never lowers the bound) implies subset dominance; assert both."""
    tuples = [(legs, float(mag), req) for legs, mag, req in entries]
    full = _mutex_directional_game_cc(tuples, ME)
    # Every subset (realizable accepted subset of the mass-acceptance snapshot) is
    # dominated by the all-accepted bound. Exhaustive for the bounded size.
    for r in range(len(tuples) + 1):
        for combo in itertools.combinations(tuples, r):
            assert _mutex_directional_game_cc(list(combo), ME) <= full


@given(
    entries=st.lists(
        st.tuples(_LEGSETS, st.integers(1, 500), st.booleans()), min_size=0, max_size=8
    ),
    extra=st.tuples(_LEGSETS, st.integers(1, 500), st.booleans()),
)
@settings(max_examples=400, deadline=None)
def test_monotonic_adding_entry_never_decreases(entries, extra):
    tuples = [(legs, float(mag), req) for legs, mag, req in entries]
    et = (extra[0], float(extra[1]), extra[2])
    base = _mutex_directional_game_cc(tuples, ME)
    more = _mutex_directional_game_cc([*tuples, et], ME)
    assert more >= base


# ----------------------------- book integration ---------------------------
def _pos(pid, leg, price_cc, contracts=100):
    return OpenPosition(
        position_id=pid, combo_ticker=f"C-{pid}", collection=None,
        our_side=Side.NO, contracts=Q(contracts), entry_price_cc=CC(price_cc),
        legs=(leg,),
    )


class TestBookIntegration:
    def test_snapshot_nets_opposing_advance_direction(self):
        # Long-NO ARG-advance and long-NO ENG-advance: opposing mutually-exclusive
        # outcomes → the directional bound nets to the larger, not the sum.
        book = ExposureBook(CONV, is_me_event=ME)
        book.add_position(_pos("p1", ARG, 7000, contracts=100))
        book.add_position(_pos("p2", ENG, 5000, contracts=100))
        # marginal 0.5 for both legs → |delta| per single-leg NO position = 1.0 ct.
        snap = book.snapshot(lambda t: 0.5, mass_acceptance=False)
        game = "26JUL15ENGARG"
        # Each position's directional magnitude = 1.0 ct × $1 = 10000cc; they NET
        # (opposing advance) → 10000cc, not the summed 20000cc.
        assert snap.directional_by_game_cc[game] == CC_PER_DOLLAR

    def test_snapshot_sums_same_outcome_direction(self):
        # Two long-NO ARG-advance positions (SAME outcome) → concentration → sum.
        book = ExposureBook(CONV, is_me_event=ME)
        book.add_position(_pos("p1", ARG, 7000, contracts=100))
        book.add_position(_pos("p2", ARG, 5000, contracts=100))
        snap = book.snapshot(lambda t: 0.5, mass_acceptance=False)
        game = "26JUL15ENGARG"
        assert snap.directional_by_game_cc[game] == 2 * CC_PER_DOLLAR

    def test_snapshot_no_provider_is_summed(self):
        # No is_me_event metadata → fail closed to the summed magnitude (byte
        # identical to the pre-P0-9 independence-sum on this book).
        book = ExposureBook(CONV)  # no is_me_event
        book.add_position(_pos("p1", ARG, 7000, contracts=100))
        book.add_position(_pos("p2", ENG, 5000, contracts=100))
        snap = book.snapshot(lambda t: 0.5, mass_acceptance=False)
        assert snap.directional_by_game_cc["26JUL15ENGARG"] == 2 * CC_PER_DOLLAR


class TestDirectionalCapWiring:
    """The R2 directional cap now binds on the mutex-aware directional bound."""

    def _checker(self):
        # directional_frac 10% of a $1.50 bankroll = 15000cc threshold. One NO
        # position at 0.5 marginal = 10000cc directional (under threshold); two
        # concentrated = 20000cc (over); two hedged = 10000cc (under).
        limits = RiskLimits(caps_shadow_mode=False)
        return LimitChecker(limits), 150_000  # bankroll cc ($15.00) → thr 15000cc

    def test_hedge_passes_directional_cap(self):
        checker, bankroll = self._checker()
        book = ExposureBook(CONV, is_me_event=ME)
        book.add_position(_pos("p1", ARG, 7000, contracts=100))
        book.add_position(_pos("p2", ENG, 5000, contracts=100))  # opposing → nets 10000cc
        breaches = checker.check(
            book, lambda t: 0.5, DailyPnl(), risk_bankroll_cc=bankroll,
        )
        assert not any(b.reason is ReasonCode.SKIP_DIRECTIONAL_CAP for b in breaches)

    def test_concentration_trips_directional_cap(self):
        checker, bankroll = self._checker()
        book = ExposureBook(CONV, is_me_event=ME)
        book.add_position(_pos("p1", ARG, 7000, contracts=100))
        book.add_position(_pos("p2", ARG, 5000, contracts=100))  # same outcome → 20000cc
        breaches = checker.check(
            book, lambda t: 0.5, DailyPnl(), risk_bankroll_cc=bankroll,
        )
        assert any(b.reason is ReasonCode.SKIP_DIRECTIONAL_CAP for b in breaches)
