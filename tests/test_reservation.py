"""RiskReservationService (Phase 3) — the single-writer, atomic, versioned risk
reservation P0.

Proves the concurrency invariant the plan requires: two RFQs can NEVER both claim
the same headroom (no double-reserve), because a reservation consumes headroom the
instant it is granted and every subsequent check sees the reduced room. Plus the
commit / release / mark_unconfirmed / reconcile lifecycle, all idempotent and
version-stamped, and the SHADOW-safety (a shadow %-cap breach never denies).
"""

from __future__ import annotations

from collections.abc import Callable
from fractions import Fraction

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.limits import Breach, DailyPnl, LimitChecker, RiskLimits
from combomaker.risk.reservation import (
    ReserveResult,
    RiskReservationService,
    open_combo_tickers_from_positions,
    reservation_ids_backed_by_exchange,
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

# One leg, one game. Marginal 0.5 so deltas are finite (not None → no UNKNOWN
# breach). Two positions on the SAME game so the game-loss cap can bind on their
# sum (the concurrency case: each alone fits, together they don't).
LEG_G1 = (LegRef("A", "SER-GAME1", "yes"),)
MARGINALS: dict[str, float] = {"A": 0.5}


def marg(ticker: str) -> float | None:
    return MARGINALS.get(ticker)


BANKROLL_2K = 20_000_000  # $2,000 in cc


# 100 REAL contracts (centi-contracts = 10_000) @ $0.50/ct → max_loss = 10_000 *
# 5_000 // 100 = 500_000 cc = $50 per fill. One fits a $80 game cap, two ($100)
# don't — the concurrency case. (100 real ct is exactly the enforced
# max_contracts_per_quote=100, which breaches only at > 100, so it passes.)
def position(
    pid: str,
    *,
    contracts: int = 10_000,
    entry_price: int = 5_000,
    legs: tuple[LegRef, ...] = LEG_G1,
    our_side: Side = Side.NO,
) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=f"COMBO-{pid}",
        collection=None,
        our_side=our_side,
        contracts=Q(contracts),
        entry_price_cc=CC(entry_price),
        legs=legs,
    )


def enforced_split(breaches: list[Breach]) -> list[Breach]:
    """A splitter that ENFORCES everything (drops nothing) — caps are live."""
    return list(breaches)


def shadow_split(breaches: list[Breach]) -> list[Breach]:
    """A splitter that drops SHADOW breaches (the lifecycle's real behaviour)."""
    return [b for b in breaches if not b.shadow]


def service(
    *,
    limits: RiskLimits,
    splitter: Callable[[list[Breach]], list[Breach]] = enforced_split,
    book: ExposureBook | None = None,
) -> tuple[RiskReservationService, ExposureBook]:
    b = book or ExposureBook(CONVENTIONS)
    svc = RiskReservationService(
        exposure=b, limits=LimitChecker(limits), breach_splitter=splitter
    )
    return svc, b


def reserve(
    svc: RiskReservationService,
    rid: str,
    pos: OpenPosition,
    *,
    bankroll_cc: int | None = BANKROLL_2K,
) -> ReserveResult:
    return svc.try_reserve(
        rid, pos, marginals=marg, daily_pnl=DailyPnl(), risk_bankroll_cc=bankroll_cc
    )


# All-loose R2 fracs so only the cap under test binds; enforced hard-dollar caps
# are huge relative to these tiny books.
LOOSE: dict[str, object] = {
    "caps_shadow_mode": False,
    "game_loss_frac": Fraction(99, 100),
    "per_combo_loss_frac": Fraction(99, 100),
    "directional_frac": Fraction(99, 100),
    "slate_loss_frac": Fraction(99, 100),
    "daily_loss_frac": Fraction(99, 100),
    "drawdown_frac": Fraction(99, 100),
    "hard_trip_frac": Fraction(99, 100),
    "absolute_notional_multiple": 999,
}


def loose(**overrides: object) -> RiskLimits:
    """RiskLimits with every %-cap loose except the overrides (so exactly the cap
    under test binds). One place for the ``**dict`` type-ignore."""
    return RiskLimits(**{**LOOSE, **overrides})  # type: ignore[arg-type]


# ---------------------------------------------------------------- the core P0


def test_no_double_reserve_two_rfqs_cannot_claim_the_same_headroom() -> None:
    """THE Phase-3 invariant. A per-combo LOSS cap sized so ONE fill fits but TWO
    together breach. The first reserve() grants; the second — checked against the
    committed book PLUS the first outstanding reservation — is DENIED. Without the
    reservation layer both would pass the same check against the same headroom."""
    # Each fill: 100 ct @ $0.50 = $50 loss. Game cap 4% of $2,000 = $80, so ONE
    # ($50) fits and TWO ($100) breach — sizes the concurrency case exactly.
    limits = loose(game_loss_frac=Fraction(4, 100))  # $80 game
    svc, book = service(limits=limits)

    first = reserve(svc, "r1", position("r1"))  # $50 <= $80 → granted
    assert first.granted is True
    assert svc.outstanding_count == 1

    # Second fill on the SAME game: committed($0) + outstanding($50) + this($50) =
    # $100 > $80 game cap → DENIED. The headroom the first reservation holds is
    # visible to the second check.
    second = reserve(svc, "r2", position("r2"))
    assert second.granted is False
    assert second.breaches
    assert svc.outstanding_count == 1  # nothing recorded for the denied one


def test_reservation_frees_headroom_on_release_so_the_next_one_fits() -> None:
    limits = loose(game_loss_frac=Fraction(4, 100))  # $80
    svc, _ = service(limits=limits)
    assert reserve(svc, "r1", position("r1")).granted is True
    assert reserve(svc, "r2", position("r2")).granted is False
    # Release the first → its $50 headroom is freed → the second now fits.
    assert svc.release("r1") is True
    assert svc.outstanding_count == 0
    assert reserve(svc, "r2", position("r2")).granted is True


def test_committed_reservation_still_consumes_headroom() -> None:
    """After commit the position is in the BOOK (committed), so it still counts —
    the headroom does not reappear. The second fill is still denied."""
    limits = loose(game_loss_frac=Fraction(4, 100))  # $80
    svc, book = service(limits=limits)
    reserve(svc, "r1", position("r1"))
    assert svc.commit("r1") is True
    assert svc.outstanding_count == 0
    assert "r1" in book.positions  # committed into the book
    # committed $50 + this $50 = $100 > $80 → still denied (no double-spend).
    assert reserve(svc, "r2", position("r2")).granted is False


# ---------------------------------------------------------------- idempotency


def test_reserve_is_idempotent_by_id() -> None:
    limits = loose()
    svc, _ = service(limits=limits)
    reserve(svc, "r1", position("r1"))
    v_after_first = svc.version
    again = reserve(svc, "r1", position("r1"))  # same id
    assert again.granted is True
    assert svc.outstanding_count == 1  # NOT double counted
    assert svc.version == v_after_first  # no mutation on the idempotent re-reserve
    assert again.reservation.reservation_id == "r1"  # type: ignore[union-attr]


def test_commit_is_idempotent() -> None:
    limits = loose()
    svc, book = service(limits=limits)
    reserve(svc, "r1", position("r1"))
    assert svc.commit("r1") is True
    assert svc.commit("r1") is False  # already committed → no-op
    assert len(book.positions) == 1  # booked exactly once


def test_release_is_idempotent() -> None:
    limits = loose()
    svc, _ = service(limits=limits)
    reserve(svc, "r1", position("r1"))
    assert svc.release("r1") is True
    assert svc.release("r1") is False  # already released → no-op


def test_release_after_commit_is_a_noop() -> None:
    limits = loose()
    svc, book = service(limits=limits)
    reserve(svc, "r1", position("r1"))
    svc.commit("r1")
    assert svc.release("r1") is False  # committed, not outstanding
    assert "r1" in book.positions  # the committed position stays


# ------------------------------------------------------- confirm-timeout path


def test_mark_unconfirmed_keeps_headroom_held() -> None:
    """A confirm timeout ⇒ assume-committed: the headroom STAYS consumed so a
    possibly-real position keeps counting against the caps."""
    limits = loose(game_loss_frac=Fraction(4, 100))  # $80
    svc, _ = service(limits=limits)
    reserve(svc, "r1", position("r1"))
    assert svc.mark_unconfirmed("r1") is True
    assert svc.is_unconfirmed("r1") is True
    assert svc.outstanding_count == 1  # STILL held
    # A second fill is still denied — the unconfirmed reservation counts.
    assert reserve(svc, "r2", position("r2")).granted is False


def test_mark_unconfirmed_is_idempotent() -> None:
    limits = loose()
    svc, _ = service(limits=limits)
    reserve(svc, "r1", position("r1"))
    assert svc.mark_unconfirmed("r1") is True
    v = svc.version
    assert svc.mark_unconfirmed("r1") is True  # still True — it IS unconfirmed
    assert svc.version == v  # but no second version bump
    assert svc.mark_unconfirmed("nope") is False  # unknown id


def test_unconfirmed_reservation_can_still_be_committed() -> None:
    """After a timeout the real execution message arrives — commit converts the
    held (unconfirmed) reservation into a booked position exactly once."""
    limits = loose()
    svc, book = service(limits=limits)
    reserve(svc, "r1", position("r1"))
    svc.mark_unconfirmed("r1")
    assert svc.commit("r1") is True
    assert "r1" in book.positions
    assert svc.outstanding_count == 0


# ------------------------------------------------------- exchange reconcile


def test_reconcile_commits_landed_and_releases_not_landed() -> None:
    """Exchange-first truth: after a timeout (or at startup) the exchange ledger
    decides. Reservations whose position id the exchange reports open are
    committed; the rest are released."""
    limits = loose()
    svc, book = service(limits=limits)
    reserve(svc, "r1", position("r1"))
    reserve(svc, "r2", position("r2"))
    reserve(svc, "r3", position("r3"))
    svc.mark_unconfirmed("r1")
    svc.mark_unconfirmed("r2")
    # Exchange says r1 and r3 actually landed (by position_id); r2 did not.
    outcome = svc.reconcile({"r1", "r3"})
    assert sorted(outcome.committed) == ["r1", "r3"]
    assert outcome.released == ["r2"]
    assert set(book.positions) == {"r1", "r3"}
    assert svc.outstanding_count == 0


def test_reconcile_is_order_independent_and_idempotent() -> None:
    limits = loose()
    svc, book = service(limits=limits)
    reserve(svc, "r1", position("r1"))
    svc.reconcile({"r1"})
    # Second reconcile with nothing outstanding → empty outcome, book unchanged.
    outcome = svc.reconcile({"r1"})
    assert outcome.committed == []
    assert outcome.released == []
    assert set(book.positions) == {"r1"}


# ------------------------------------------------------------------ version


def test_version_bumps_on_every_mutation() -> None:
    limits = loose()
    svc, _ = service(limits=limits)
    v0 = svc.version
    reserve(svc, "r1", position("r1"))
    v1 = svc.version
    assert v1 == v0 + 1
    svc.mark_unconfirmed("r1")
    v2 = svc.version
    assert v2 == v1 + 1
    svc.commit("r1")
    assert svc.version == v2 + 1
    # A denied reserve does NOT bump the version (nothing changed).
    reserve(svc, "r2", position("r2"), bankroll_cc=None)  # fail-closed denial
    assert svc.version == v2 + 1


# ------------------------------------------------------------------ shadow-safe


def test_shadow_breach_does_not_deny_a_reservation() -> None:
    """With the SHADOW splitter, a %-cap that WOULD breach is dropped → the
    reservation is still granted. The reservation layer never changes what the
    caps DO, only when headroom is consumed — Phase-2 SHADOW behaviour holds."""
    # A tiny bankroll trips every %-cap, but caps_shadow_mode=True → shadow.
    limits = RiskLimits(caps_shadow_mode=True)
    svc, _ = service(limits=limits, splitter=shadow_split)
    result = reserve(svc, "r1", position("r1"), bankroll_cc=100)  # $0.01 bankroll
    assert result.granted is True
    assert svc.outstanding_count == 1


def test_fail_closed_bankroll_denies_when_enforced() -> None:
    """No bankroll (stale poll) ⇒ SKIP_BANKROLL_UNAVAILABLE; enforced → denied."""
    limits = RiskLimits(caps_shadow_mode=False)
    svc, _ = service(limits=limits)
    result = reserve(svc, "r1", position("r1"), bankroll_cc=None)
    assert result.granted is False
    assert svc.outstanding_count == 0


def test_outstanding_positions_exposed_for_folding() -> None:
    limits = loose()
    svc, _ = service(limits=limits)
    reserve(svc, "r1", position("r1"))
    reserve(svc, "r2", position("r2"))
    ids = {p.position_id for p in svc.outstanding_positions()}
    assert ids == {"r1", "r2"}


# ------------------------------------- exchange-position → reservation mapping


def _combo_position(pid: str, ticker: str, our_side: Side = Side.NO) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=ticker,
        collection=None,
        our_side=our_side,
        contracts=Q(10_000),
        entry_price_cc=CC(5_000),
        legs=LEG_G1,
    )


class TestOpenComboTickersFromPositions:
    def test_signed_position_fp_maps_to_side(self) -> None:
        # position_fp signed: negative = NO, positive = YES, 0 = flat (not open).
        payload = {
            "market_positions": [
                {"ticker": "C-NO", "position_fp": "-5.00"},
                {"ticker": "C-YES", "position_fp": "3.00"},
                {"ticker": "C-FLAT", "position_fp": "0.00"},
            ]
        }
        got = open_combo_tickers_from_positions(payload)
        assert got == {"C-NO": Side.NO, "C-YES": Side.YES}  # flat excluded

    def test_accepts_positions_alias_and_market_ticker(self) -> None:
        payload = {"positions": [{"market_ticker": "C-NO", "position_fp": "-1.00"}]}
        assert open_combo_tickers_from_positions(payload) == {"C-NO": Side.NO}

    def test_unparseable_count_is_skipped_fail_closed(self) -> None:
        # A bad position_fp is treated as no-provable-open-position (skip), not a
        # guessed side — the conservative direction (a released reservation).
        payload = {"market_positions": [{"ticker": "C", "position_fp": "junk"}]}
        assert open_combo_tickers_from_positions(payload) == {}

    def test_missing_ticker_or_fp_skipped(self) -> None:
        payload = {
            "market_positions": [
                {"position_fp": "-1.00"},        # no ticker
                {"ticker": "C", "position_fp": None},  # no count
            ]
        }
        assert open_combo_tickers_from_positions(payload) == {}


class TestReservationIdsBackedByExchange:
    def test_commits_side_match_releases_the_rest(self) -> None:
        outstanding = [
            _combo_position("fill:q1", "C1", Side.NO),
            _combo_position("fill:q2", "C2", Side.NO),
            _combo_position("fill:q3", "C3", Side.NO),
        ]
        # Exchange holds C1 NO (our fill landed), C2 YES (opposite side — NOT ours),
        # C3 absent (did not land).
        open_by_ticker = {"C1": Side.NO, "C2": Side.YES}
        backed = reservation_ids_backed_by_exchange(outstanding, open_by_ticker)
        assert backed == {"fill:q1"}  # only the side-matching open position

    def test_end_to_end_confirm_timeout_reconcile(self) -> None:
        # A reservation whose confirm timed out (mark_unconfirmed) is RESOLVED by a
        # reconcile against the exchange's real open positions — committed if the
        # exchange holds it, released if not — instead of leaking headroom.
        limits = loose()
        svc, book = service(limits=limits)
        landed = _combo_position("fill:q1", "C1", Side.NO)
        leaked = _combo_position("fill:q2", "C2", Side.NO)
        reserve(svc, "fill:q1", landed)
        reserve(svc, "fill:q2", leaked)
        svc.mark_unconfirmed("fill:q1")
        svc.mark_unconfirmed("fill:q2")
        # Exchange reports ONLY C1 open (as NO). Map → the backed id, reconcile.
        positions_payload = {"market_positions": [{"ticker": "C1", "position_fp": "-100.00"}]}
        open_by_ticker = open_combo_tickers_from_positions(positions_payload)
        backed = reservation_ids_backed_by_exchange(
            svc.outstanding_positions(), open_by_ticker
        )
        outcome = svc.reconcile(backed)
        assert outcome.committed == ["fill:q1"]  # landed ⇒ booked
        assert outcome.released == ["fill:q2"]   # not open ⇒ headroom freed
        assert set(book.positions) == {"fill:q1"}
        assert svc.outstanding_count == 0
