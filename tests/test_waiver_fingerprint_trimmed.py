"""TRIMMED WAIVER STABILITY FINGERPRINT (2026-07-18) — the "waiver unstable:
book moved during every enumeration" churn fix (51 live declines 2026-07-17
night).

With the entity-set trim armed (``lastlook_waiver_topk_resting > 0``) the
enumeration prices only the K largest resting quotes per breached game plus a
constant conservative tail adder — but the old stability fingerprint keyed on
the ids of ALL same-game resting quotes, so reprice/rotation churn among small
quotes the enumeration never priced invalidated certificates whose bound
provably still held. The fix keys stability on the certificate's own support:

  - position generation + reservation version compare EXACTLY (committed fills
    and reservation churn are real risk changes — never waived through);
  - quote churn is judged by grant-time revalidation: the waiver stays valid
    iff every still-present SELECTED quote is unchanged (id + priced size) AND
    the CURRENT outside-selection tail per breached game fits the enumerated
    adder — then (trimmed worst + adder) still upper-bounds the CURRENT book;
  - anything else fails closed exactly as before (retry once, then the
    unstable decline).

Every mid-enumeration case here goes through the public check path
(``_lastlook_mc_waiver`` with a churn-injecting pool — the established
test seam), never by poking the fingerprint helpers directly; one direct test
pins the ``tail_outside_selection`` partition contract. The untrimmed default
(topk == 0) keeps the exact-id-set semantics, pinned by
tests/test_lastlook_mc_waiver.py.
"""

from __future__ import annotations

import structlog

from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.ops.persistence import Store
from combomaker.risk.exposure import LegRef, OpenPosition
from combomaker.risk.limits import LimitChecker
from combomaker.sim.state_worst_case import (
    quote_from_open_quote,
    tail_outside_selection,
)
from tests import test_lastlook_mc_waiver as _waiver_tests
from tests.test_filters import Harness
from tests.test_lastlook_mc_waiver import (
    BANKROLL_CC,
    WAIVER_LIMITS,
    _assert_waiver_counters,
    _build_rig,
    _BumpingPool,
    _QuoteChurnPool,
    _resting_quote,
    _trim_config,
    _wc_state,
)
from tests.test_lifecycle import TEST_CONVENTIONS
from tests.test_state_worst_case import ADV_EV, ARG_ADV, ENG_ADV, FRA_ML, GAME, ML2_EV

# Re-export the shared World-Cup harness fixture for this module's tests
# (assignment, not import — a fixture name shadowed by test parameters).
kxwc = _waiver_tests.kxwc

TOLERATED_EVENT = "lastlook_waiver_tail_churn_tolerated"
TOLERATED_DETAIL = "waiver stable: tail churn within adder"


# ------------------------------------------- 1. churn OUTSIDE the top-K


async def test_tail_churn_outside_topk_still_grants(
    kxwc: tuple[Harness, Store],
) -> None:
    """THE FIXED BEHAVIOUR: K=1 keeps the 8000cc opposing hedge; the 500cc
    co-directional tail quote rides as the adder. Mid-enumeration the tail
    quote is REPLACED (remove + new id at 400cc) — the same-game id set
    changes, so the OLD fingerprint declined here ("book moved during every
    enumeration"). Now: the selected set is untouched and the current tail
    (400) fits the enumerated adder (500) ⇒ granted on the FIRST attempt with
    the enumerated certificate (8000 + 500 adder), zero conflicts, and the
    tolerated-churn debug line."""
    h, store = kxwc
    limits = LimitChecker(WAIVER_LIMITS)
    lifecycle, _sender, exposure, reservation, metrics = _build_rig(
        h, store, limits=limits, bankroll_cc=BANKROLL_CC, config=_trim_config(1)
    )
    exposure.upsert_quote(_resting_quote("q:eng", ENG_ADV, ADV_EV))
    exposure.upsert_quote(_resting_quote("q:small", ARG_ADV, ADV_EV, no_bid_cc=500))

    def churn() -> None:
        exposure.remove_quote("q:small")
        exposure.upsert_quote(
            _resting_quote("q:small2", ARG_ADV, ADV_EV, no_bid_cc=400)
        )

    pool = _QuoteChurnPool([churn])
    lifecycle._book_risk_pool = pool  # type: ignore[assignment]  # noqa: SLF001
    state = _wc_state()
    denied = lifecycle._reserve_headroom("fill:q1", "q1", state)  # noqa: SLF001
    assert denied is not None and not denied.granted
    assert {b.reason for b in denied.breaches} == {ReasonCode.SKIP_GAME_LOSS_CAP}

    with structlog.testing.capture_logs() as cap:
        ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
            "q1", state, "fill:q1", denied.breaches
        )
    assert ok is True and detail == ""
    assert pool.calls == 1  # first attempt stood — no rebuild burned
    assert reservation.is_outstanding("fill:q1")
    _assert_waiver_counters(metrics, attempted=1, granted=1)
    assert lifecycle._waiver_audit == {  # noqa: SLF001
        "granted": True,
        "worst_case_cc": 8500,  # the ENUMERATED certificate: 8000 + 500 adder
        "games": [GAME],
        "trim_adders_cc": {GAME: 500},
    }
    tolerated = [e for e in cap if e.get("event") == TOLERATED_EVENT]
    assert len(tolerated) == 1
    assert tolerated[0]["detail"] == TOLERATED_DETAIL
    assert tolerated[0]["log_level"] == "debug"


# --------------------------------------------- 2. churn INSIDE the top-K


async def test_selected_quote_replaced_conflicts_then_rebuild_grants(
    kxwc: tuple[Harness, Store],
) -> None:
    """A SELECTED quote repriced (remove + new id, same 8000cc) with NO dropped
    tail: the vanished id is conservative, but the replacement is outside the
    enumerated selection and 8000 > the 0cc adder ⇒ NOT provably covered ⇒
    invalidate (version_conflict) — the ONE rebuild prices the replacement and
    the stable second attempt grants."""
    h, store = kxwc
    limits = LimitChecker(WAIVER_LIMITS)
    lifecycle, _sender, exposure, reservation, metrics = _build_rig(
        h, store, limits=limits, bankroll_cc=BANKROLL_CC, config=_trim_config(1)
    )
    exposure.upsert_quote(_resting_quote("q:eng", ENG_ADV, ADV_EV))

    def reprice() -> None:
        exposure.remove_quote("q:eng")
        exposure.upsert_quote(_resting_quote("q:eng2", ENG_ADV, ADV_EV))

    pool = _QuoteChurnPool([reprice])
    lifecycle._book_risk_pool = pool  # type: ignore[assignment]  # noqa: SLF001
    state = _wc_state()
    denied = lifecycle._reserve_headroom("fill:q1", "q1", state)  # noqa: SLF001
    assert denied is not None and not denied.granted

    ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", state, "fill:q1", denied.breaches
    )
    assert ok is True and detail == ""
    assert pool.calls == 2  # invalidated once, rebuilt on the current book
    assert reservation.is_outstanding("fill:q1")
    _assert_waiver_counters(metrics, attempted=1, granted=1, version_conflict=1)


async def test_selected_quote_churn_on_every_attempt_declines_unstable(
    kxwc: tuple[Harness, Store],
) -> None:
    """Selected-set churn on BOTH attempts ⇒ exactly one rebuild, then the
    fail-closed unstable decline — the pre-fix decline string verbatim (the
    monitoring contract for genuinely-unstable books)."""
    h, store = kxwc
    limits = LimitChecker(WAIVER_LIMITS)
    lifecycle, _sender, exposure, reservation, metrics = _build_rig(
        h, store, limits=limits, bankroll_cc=BANKROLL_CC, config=_trim_config(1)
    )
    exposure.upsert_quote(_resting_quote("q:eng", ENG_ADV, ADV_EV))

    def reprice_1() -> None:
        exposure.remove_quote("q:eng")
        exposure.upsert_quote(_resting_quote("q:eng2", ENG_ADV, ADV_EV))

    def reprice_2() -> None:
        exposure.remove_quote("q:eng2")
        exposure.upsert_quote(_resting_quote("q:eng3", ENG_ADV, ADV_EV))

    pool = _QuoteChurnPool([reprice_1, reprice_2])
    lifecycle._book_risk_pool = pool  # type: ignore[assignment]  # noqa: SLF001
    state = _wc_state()
    denied = lifecycle._reserve_headroom("fill:q1", "q1", state)  # noqa: SLF001
    assert denied is not None and not denied.granted

    ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", state, "fill:q1", denied.breaches
    )
    assert ok is False
    assert detail == "waiver unstable: book moved during every enumeration"
    assert pool.calls == 2  # one rebuild, never a third attempt
    assert reservation.outstanding_count == 0
    _assert_waiver_counters(metrics, attempted=1, version_conflict=2)


async def test_selected_quote_mutated_under_its_id_fails_closed(
    kxwc: tuple[Harness, Store],
) -> None:
    """Belt+braces the OLD id-set fingerprint could not see: ``upsert_quote``
    physically CAN swap content under an unchanged id (invariant violation —
    a real reprice replaces the id). Same id set, same stamps — but the
    selected quote's priced size moved 8000 → 9000 ⇒ the revalidation fails
    closed (version_conflict) and the ONE rebuild prices the mutated book."""
    h, store = kxwc
    limits = LimitChecker(WAIVER_LIMITS)
    lifecycle, _sender, exposure, reservation, metrics = _build_rig(
        h, store, limits=limits, bankroll_cc=BANKROLL_CC, config=_trim_config(1)
    )
    exposure.upsert_quote(_resting_quote("q:eng", ENG_ADV, ADV_EV))
    pool = _QuoteChurnPool(
        [
            lambda: exposure.upsert_quote(
                _resting_quote("q:eng", ENG_ADV, ADV_EV, no_bid_cc=9000)
            )
        ]
    )
    lifecycle._book_risk_pool = pool  # type: ignore[assignment]  # noqa: SLF001
    state = _wc_state()
    denied = lifecycle._reserve_headroom("fill:q1", "q1", state)  # noqa: SLF001
    assert denied is not None and not denied.granted

    ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", state, "fill:q1", denied.breaches
    )
    assert ok is True and detail == ""
    assert pool.calls == 2  # never granted off the stale 8000cc enumeration
    assert reservation.is_outstanding("fill:q1")
    _assert_waiver_counters(metrics, attempted=1, granted=1, version_conflict=1)


async def test_selected_quote_vanishing_is_conservative_and_grants(
    kxwc: tuple[Harness, Store],
) -> None:
    """A SELECTED quote removed outright mid-enumeration: its enumerated
    clamped contribution was >= 0 per state, so the certificate only
    OVERSTATES the shrunken book — grant on the first attempt (the surviving
    tail quote, 500cc, still fits its 500cc adder)."""
    h, store = kxwc
    limits = LimitChecker(WAIVER_LIMITS)
    lifecycle, _sender, exposure, reservation, metrics = _build_rig(
        h, store, limits=limits, bankroll_cc=BANKROLL_CC, config=_trim_config(1)
    )
    exposure.upsert_quote(_resting_quote("q:eng", ENG_ADV, ADV_EV))
    exposure.upsert_quote(_resting_quote("q:small", ARG_ADV, ADV_EV, no_bid_cc=500))
    pool = _QuoteChurnPool([lambda: exposure.remove_quote("q:eng")])
    lifecycle._book_risk_pool = pool  # type: ignore[assignment]  # noqa: SLF001
    state = _wc_state()
    denied = lifecycle._reserve_headroom("fill:q1", "q1", state)  # noqa: SLF001
    assert denied is not None and not denied.granted

    ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", state, "fill:q1", denied.breaches
    )
    assert ok is True and detail == ""
    assert pool.calls == 1
    assert reservation.is_outstanding("fill:q1")
    _assert_waiver_counters(metrics, attempted=1, granted=1)
    audit = lifecycle._waiver_audit  # noqa: SLF001
    assert audit is not None
    assert audit["worst_case_cc"] == 8500  # the enumerated bound, unchanged


# ------------------------------------------- 3. tail grows beyond the adder


async def test_tail_growth_beyond_adder_on_every_attempt_declines(
    kxwc: tuple[Harness, Store],
) -> None:
    """The tail OUTGROWS its adder on both attempts (600cc then 700cc of new
    small quotes against 500cc/1100cc enumerated adders) ⇒ never granted off
    a bound the current book exceeds — one rebuild, then the unstable
    decline."""
    h, store = kxwc
    limits = LimitChecker(WAIVER_LIMITS)
    lifecycle, _sender, exposure, reservation, metrics = _build_rig(
        h, store, limits=limits, bankroll_cc=BANKROLL_CC, config=_trim_config(1)
    )
    exposure.upsert_quote(_resting_quote("q:eng", ENG_ADV, ADV_EV))
    exposure.upsert_quote(_resting_quote("q:small", ARG_ADV, ADV_EV, no_bid_cc=500))
    pool = _QuoteChurnPool(
        [
            # tail 500 + 600 = 1100 > adder 500 ⇒ conflict + rebuild
            lambda: exposure.upsert_quote(
                _resting_quote("q:g600", ARG_ADV, ADV_EV, no_bid_cc=600)
            ),
            # rebuild's adder 1100; tail 500 + 600 + 700 = 1800 > 1100 ⇒ decline
            lambda: exposure.upsert_quote(
                _resting_quote("q:g700", ARG_ADV, ADV_EV, no_bid_cc=700)
            ),
        ]
    )
    lifecycle._book_risk_pool = pool  # type: ignore[assignment]  # noqa: SLF001
    state = _wc_state()
    denied = lifecycle._reserve_headroom("fill:q1", "q1", state)  # noqa: SLF001
    assert denied is not None and not denied.granted

    ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", state, "fill:q1", denied.breaches
    )
    assert ok is False
    assert detail == "waiver unstable: book moved during every enumeration"
    assert pool.calls == 2
    assert reservation.outstanding_count == 0
    _assert_waiver_counters(metrics, attempted=1, version_conflict=2)


async def test_tail_growth_once_rebuild_reprices_honestly_then_over_budget(
    kxwc: tuple[Harness, Store],
) -> None:
    """A single 3000cc tail arrival: attempt 1 invalidates (3500 > 500); the
    rebuild folds the grown tail into a fresh 3500cc adder and the stable
    second enumeration certifies 8000 + 3500 = 11500cc > the 10000cc budget ⇒
    the honest over-budget decline — never a grant off the stale small
    adder."""
    h, store = kxwc
    limits = LimitChecker(WAIVER_LIMITS)
    lifecycle, _sender, exposure, reservation, metrics = _build_rig(
        h, store, limits=limits, bankroll_cc=BANKROLL_CC, config=_trim_config(1)
    )
    exposure.upsert_quote(_resting_quote("q:eng", ENG_ADV, ADV_EV))
    exposure.upsert_quote(_resting_quote("q:small", ARG_ADV, ADV_EV, no_bid_cc=500))
    pool = _QuoteChurnPool(
        [
            lambda: exposure.upsert_quote(
                _resting_quote("q:grow", ARG_ADV, ADV_EV, no_bid_cc=3000)
            )
        ]
    )
    lifecycle._book_risk_pool = pool  # type: ignore[assignment]  # noqa: SLF001
    state = _wc_state()
    denied = lifecycle._reserve_headroom("fill:q1", "q1", state)  # noqa: SLF001
    assert denied is not None and not denied.granted

    ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", state, "fill:q1", denied.breaches
    )
    assert ok is False
    assert "game-loss budget" in detail
    assert pool.calls == 2
    assert reservation.outstanding_count == 0
    _assert_waiver_counters(
        metrics, attempted=1, version_conflict=1, declined_over_budget=1
    )
    audit = lifecycle._waiver_audit  # noqa: SLF001
    assert audit is not None
    assert audit["worst_case_cc"] == 11500
    assert audit["trim_adders_cc"] == {GAME: 3500}


# ------------------------- 4+5. positions / reservations still invalidate


async def test_position_fill_mid_enumeration_still_invalidates_with_trim(
    kxwc: tuple[Harness, Store],
) -> None:
    """A fill landing (position generation bump) during every enumeration must
    STILL invalidate under the trimmed fingerprint — committed positions are
    real risk, never waived through: one rebuild, then the unstable decline."""
    h, store = kxwc
    limits = LimitChecker(WAIVER_LIMITS)
    lifecycle, _sender, exposure, reservation, metrics = _build_rig(
        h, store, limits=limits, bankroll_cc=BANKROLL_CC, config=_trim_config(1)
    )
    exposure.upsert_quote(_resting_quote("q:eng", ENG_ADV, ADV_EV))

    def land_fill(n: int) -> None:
        exposure.add_position(
            OpenPosition(
                position_id=f"pos:landed{n}",
                combo_ticker=f"KXMVE-L{n}",
                collection=None,
                our_side=Side.NO,
                contracts=CentiContracts(10),
                entry_price_cc=CentiCents(1000),
                legs=(LegRef(ARG_ADV, ADV_EV, "yes"),),
            )
        )

    pool = _QuoteChurnPool([lambda: land_fill(1), lambda: land_fill(2)])
    lifecycle._book_risk_pool = pool  # type: ignore[assignment]  # noqa: SLF001
    state = _wc_state()
    denied = lifecycle._reserve_headroom("fill:q1", "q1", state)  # noqa: SLF001
    assert denied is not None and not denied.granted

    ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", state, "fill:q1", denied.breaches
    )
    assert ok is False
    assert detail == "waiver unstable: book moved during every enumeration"
    assert pool.calls == 2
    assert reservation.outstanding_count == 0
    _assert_waiver_counters(metrics, attempted=1, version_conflict=2)


async def test_reservation_version_bump_still_invalidates_with_trim(
    kxwc: tuple[Harness, Store],
) -> None:
    """Reservation churn during every enumeration under the trimmed
    fingerprint: exactly as today — one rebuild, then fail closed."""
    h, store = kxwc
    limits = LimitChecker(WAIVER_LIMITS)
    lifecycle, _sender, exposure, reservation, metrics = _build_rig(
        h, store, limits=limits, bankroll_cc=BANKROLL_CC, config=_trim_config(1)
    )
    exposure.upsert_quote(_resting_quote("q:eng", ENG_ADV, ADV_EV))
    pool = _BumpingPool(reservation, bumps=2)
    lifecycle._book_risk_pool = pool  # type: ignore[assignment]  # noqa: SLF001
    state = _wc_state()
    denied = lifecycle._reserve_headroom("fill:q1", "q1", state)  # noqa: SLF001
    assert denied is not None and not denied.granted

    ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", state, "fill:q1", denied.breaches
    )
    assert ok is False
    assert detail == "waiver unstable: book moved during every enumeration"
    assert pool.calls == 2
    assert reservation.outstanding_count == 0
    _assert_waiver_counters(metrics, attempted=1, version_conflict=2)


async def test_single_reservation_bump_with_trim_rebuild_grants(
    kxwc: tuple[Harness, Store],
) -> None:
    """One reservation conflict ⇒ one rebuild ⇒ the stable second enumeration
    grants — the retry budget is preserved verbatim under the trim."""
    h, store = kxwc
    limits = LimitChecker(WAIVER_LIMITS)
    lifecycle, _sender, exposure, reservation, metrics = _build_rig(
        h, store, limits=limits, bankroll_cc=BANKROLL_CC, config=_trim_config(1)
    )
    exposure.upsert_quote(_resting_quote("q:eng", ENG_ADV, ADV_EV))
    pool = _BumpingPool(reservation, bumps=1)
    lifecycle._book_risk_pool = pool  # type: ignore[assignment]  # noqa: SLF001
    state = _wc_state()
    denied = lifecycle._reserve_headroom("fill:q1", "q1", state)  # noqa: SLF001
    assert denied is not None and not denied.granted

    ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", state, "fill:q1", denied.breaches
    )
    assert ok is True and detail == ""
    assert pool.calls == 2
    assert reservation.is_outstanding("fill:q1")
    _assert_waiver_counters(metrics, attempted=1, granted=1, version_conflict=1)


# --------------------------------- the revalidation partition, pinned direct


def test_tail_outside_selection_partitions_and_flags_mutations() -> None:
    """The one direct pin of the grant-time helper's contract: tails sum ONLY
    outside-selection breached-game touchers; a vanished selected quote is
    silently conservative; a selected id whose priced size moved is flagged
    ``mutated``; unrelated-game quotes contribute nothing."""
    conv = TEST_CONVENTIONS
    q_big = quote_from_open_quote(_resting_quote("q:big", ENG_ADV, ADV_EV), conv)
    q_small = quote_from_open_quote(
        _resting_quote("q:small", ARG_ADV, ADV_EV, no_bid_cc=500), conv
    )
    q_other = quote_from_open_quote(_resting_quote("q:other", FRA_ML, ML2_EV), conv)
    selected = {"q:big": q_big.worst_hit_loss_cc}

    tails, mutated = tail_outside_selection(
        (q_big, q_small, q_other), [GAME], None, selected
    )
    assert tails == {GAME: 500} and mutated == ()

    # Vanished selected quote: conservative — absent from both outputs.
    tails, mutated = tail_outside_selection((q_small,), [GAME], None, selected)
    assert tails == {GAME: 500} and mutated == ()

    # Same id, different priced size ⇒ flagged (callers fail closed).
    q_big_mut = quote_from_open_quote(
        _resting_quote("q:big", ENG_ADV, ADV_EV, no_bid_cc=9000), conv
    )
    tails, mutated = tail_outside_selection(
        (q_big_mut, q_small), [GAME], None, selected
    )
    assert mutated == ("q:big",)
    assert tails == {GAME: 500}  # the mutated quote never leaks into the tail
