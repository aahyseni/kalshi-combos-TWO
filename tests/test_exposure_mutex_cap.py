"""Stage B — mutual-exclusion-aware per-game worst-case loss cap.

Covers the ported ``_mutex_game_worst_cc`` (risk/exposure.py) and its wiring into
``ExposureBook.snapshot``: the game loss cap nets a single result mutually-exclusive
event (advance / moneyline) via max-over-branches instead of the comonotone sum,
and FAILS CLOSED to comonotone on 0 or >=2 ME events so the bound stays MONOTONIC
(the E2 mass-acceptance dominance invariant). Parity target: tools/proto_mutex_game_cap.py.
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.risk.exposure import (
    ExposureBook,
    LegRef,
    OpenPosition,
    _mutex_game_worst_cc,
)

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


def E(legs, loss, requires=True):
    return (tuple(legs), loss, requires)


# ----------------------------- pure function ------------------------------
class TestMutexPureFunction:
    def test_advance_hedge_nets_to_max(self):
        # ARG-adv and ENG-adv are mutually exclusive → max(10,10)=10, not 20.
        assert _mutex_game_worst_cc([E([ARG], 10), E([ENG], 10)], ME) == 10

    def test_one_sided_is_comonotone(self):
        assert _mutex_game_worst_cc([E([ARG], 10), E([ARG], 10)], ME) == 20

    def test_no_provider_is_comonotone(self):
        assert _mutex_game_worst_cc([E([ARG], 10), E([ENG], 10)], None) == 20

    def test_no_me_event_is_comonotone(self):
        assert _mutex_game_worst_cc([E([TOT], 10), E([TOT], 10)], ME) == 20

    def test_no_leg_requires_other_branch(self):
        # NO-ENG requires ARG (2-way): both need ARG → comonotone 20.
        assert _mutex_game_worst_cc([E([ARG], 10), E([NO_ENG], 10)], ME) == 20
        # NO-ENG (needs ARG) vs YES-ENG (needs ENG) → hedge → 10.
        assert _mutex_game_worst_cc([E([NO_ENG], 10), E([ENG], 10)], ME) == 10

    def test_common_leg_in_every_branch(self):
        # a non-advance leg is common → added to both branches → max(10+5,10+5)=15.
        assert _mutex_game_worst_cc([E([ARG], 10), E([ENG], 10), E([TOT], 5)], ME) == 15

    def test_two_me_events_fail_closed(self):
        # advance + moneyline both ME on the entries → fail-closed to comonotone.
        assert _mutex_game_worst_cc([E([ARG], 10), E([ML_ARG], 10)], ME2) == 20

    def test_non_no_side_is_common(self):
        # requires_all False (a YES-side / unknown hypothetical) ⇒ common ⇒ counts in
        # every branch ⇒ does NOT create the ME dimension, stays comonotone-like.
        assert _mutex_game_worst_cc([E([ARG], 10, False), E([ENG], 10, False)], ME) == 20

    def test_empty(self):
        assert _mutex_game_worst_cc([], ME) == 0

    def test_bounds_invariants(self):
        book = [E([ARG], 7), E([ENG], 11), E([TOT], 5)]
        b = _mutex_game_worst_cc(book, ME)
        assert b <= sum(e[1] for e in book)     # <= comonotone
        assert b >= max(e[1] for e in book)     # >= largest single entry


# ------------------------- monotonicity (dominance underpinning) ----------
_LEGSETS = st.sampled_from([(ARG,), (ENG,), (NO_ENG,), (TOT,), (ARG, TOT), (ENG, TOT)])


@given(
    entries=st.lists(
        st.tuples(_LEGSETS, st.integers(1, 500), st.booleans()), min_size=0, max_size=8
    ),
    extra=st.tuples(_LEGSETS, st.integers(1, 500), st.booleans()),
)
@settings(max_examples=400, deadline=None)
def test_monotonic_adding_entry_never_decreases(entries, extra):
    """Adding any entry never LOWERS the bound — the property the mass-acceptance
    dominance invariant rests on (a superset book's cap dominates every subset)."""
    base = _mutex_game_worst_cc(list(entries), ME)
    more = _mutex_game_worst_cc([*entries, extra], ME)
    assert more >= base


@given(entries=st.lists(st.tuples(_LEGSETS, st.integers(1, 500), st.booleans()), max_size=8))
@settings(max_examples=300, deadline=None)
def test_always_between_maxsingle_and_comonotone(entries):
    b = _mutex_game_worst_cc(list(entries), ME)
    if not entries:
        assert b == 0
        return
    assert max(e[1] for e in entries) <= b <= sum(e[1] for e in entries)


# ----------------------------- book integration ---------------------------
def _pos(pid, leg, price_cc, contracts=100):
    return OpenPosition(
        position_id=pid, combo_ticker=f"C-{pid}", collection=None,
        our_side=Side.NO, contracts=Q(contracts), entry_price_cc=CC(price_cc),
        legs=(leg,),
    )


class TestBookIntegration:
    def test_snapshot_nets_advance_hedge(self):
        book = ExposureBook(CONV, is_me_event=ME)
        book.add_position(_pos("p1", ARG, 7000))   # loss 7000cc
        book.add_position(_pos("p2", ENG, 5000))   # loss 5000cc
        snap = book.snapshot(lambda t: 0.5, mass_acceptance=False)
        game = "26JUL15ENGARG"
        # mutex: max(7000, 5000) = 7000, NOT the comonotone 12000.
        assert snap.worst_case_loss_by_game_cc[game] == 7000

    def test_snapshot_comonotone_without_provider(self):
        book = ExposureBook(CONV)  # no is_me_event → comonotone (byte-identical to old)
        book.add_position(_pos("p1", ARG, 7000))
        book.add_position(_pos("p2", ENG, 5000))
        snap = book.snapshot(lambda t: 0.5, mass_acceptance=False)
        assert snap.worst_case_loss_by_game_cc["26JUL15ENGARG"] == 12000

    def test_one_sided_book_unchanged(self):
        book = ExposureBook(CONV, is_me_event=ME)
        book.add_position(_pos("p1", ARG, 7000))
        book.add_position(_pos("p2", ARG, 5000))   # both ARG → no hedge
        snap = book.snapshot(lambda t: 0.5, mass_acceptance=False)
        assert snap.worst_case_loss_by_game_cc["26JUL15ENGARG"] == 12000
