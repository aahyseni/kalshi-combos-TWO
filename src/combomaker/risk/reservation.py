"""Single-writer risk-reservation service (RISK_BUILD_PLAN Phase 3).

The concurrency & state-safety P0. Today the hot path CHECKS the limits, then
does a network round-trip (``confirm_quote``), then books the position into the
exposure book. Between the check passing and the position landing in the book,
the reserved headroom is invisible: a second accept can pass the SAME check
against the SAME (stale) headroom and both confirm, silently breaching a cap.
Race-free today ONLY because we run one asyncio loop; this service makes the
invariant hold for any future fan-out (multiple accept handlers / workers).

The fix (single-writer, atomic, versioned):

  reserve(candidate) → re-run limits.check against
      committed positions  +  all OUTSTANDING reservations  +  this candidate
    all in ONE synchronous critical section. If (and only if) it passes with no
    ENFORCED breach, record a versioned reservation that CONSUMES that headroom
    from this instant, and hand back a ``Reservation`` token. A second reserve()
    now sees the smaller headroom — two RFQs can NEVER both claim it.

  commit(token)  → the fill is real (confirm landed): promote the reservation
                   into a committed OpenPosition in the exposure book.
  release(token) → the confirm was DECLINED / lapsed: free the headroom.
  mark_unconfirmed(token) → the confirm TIMED OUT (unknown-committed state): we
                   ASSUME COMMITTED — the headroom STAYS consumed (a reservation
                   is conservative: it must not vanish just because the ack was
                   lost) — and the token is flagged for exchange reconciliation.
  reconcile(...) → exchange-first truth: given the exchange's actual open
                   positions, commit the reservations that DID land and release
                   the ones that did NOT. Used at startup and after a timeout.

INVARIANTS (all tested):
- A reservation held by the service is ALWAYS folded into the risk snapshot the
  next reserve() checks, so headroom is never double-counted (no double-reserve).
- commit / release / mark_unconfirmed are IDEMPOTENT per reservation id (a
  replayed message, or a commit after a timeout-then-real-execution, is a no-op).
- A version counter increments on EVERY state mutation (reserve/commit/release/
  mark_unconfirmed) — a monotonic stamp a caller can compare to detect that the
  headroom moved under it (the "versioned" requirement; also cheap tamper-evidence).
- SHADOW-safe: reservation uses the SAME ``LimitChecker.check`` the lifecycle
  uses, and honours the ``_partition_breaches`` split via the injected splitter,
  so in Phase-2 SHADOW mode a %-cap breach does NOT block a reservation (only
  ENFORCED breaches do) — the reservation layer never changes what the caps do,
  only WHEN the headroom is consumed.

The service holds NO locks: asyncio is single-threaded, and every public method
is a synchronous critical section (no ``await`` inside), so it is atomic between
awaits by construction — exactly the guarantee the exposure book relies on. If
the system ever fans out onto threads, wrap the mutators in one lock; the API is
already shaped for it (one writer, no interleaving reads of half-updated state).

Money stays integer centi-cents; no binary floats (hard rule 5).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from combomaker.core.conventions import Side
from combomaker.core.quantity import qty_from_fp_str
from combomaker.ops.logging import get_logger
from combomaker.risk.exposure import ExposureBook, MarginalProvider, OpenPosition
from combomaker.risk.limits import (
    Breach,
    DailyPnl,
    HaltInputs,
    LimitChecker,
    PortfolioRisk,
    StartTimeProvider,
    WaiverCertificate,
)

log = get_logger(__name__)


def open_combo_tickers_from_positions(positions_payload: dict[str, Any]) -> dict[str, Side]:
    """Map a ``GET /portfolio/positions`` payload to ``{combo_ticker: our Side}``
    for every market the exchange reports OPEN with a nonzero position.

    ``MarketPosition.position_fp`` is a SIGNED count (docs/api-notes/index-scan.md
    §portfolio positions): NEGATIVE = a NO position, POSITIVE = a YES position, 0 =
    flat (netted out — not open). Fail-closed: an unparseable ``position_fp`` is
    SKIPPED (treated as no-open-position), which makes the reconcile RELEASE a
    reservation we can't prove is backed — the conservative direction (a released
    reservation frees headroom only if the exchange really isn't holding it; if it
    IS, the periodic reconcile re-commits on the next clean parse). The exchange
    ledger is the one ruler we can't bend (defense #3).
    """
    rows = positions_payload.get("market_positions") or positions_payload.get("positions") or []
    out: dict[str, Side] = {}
    for row in rows:
        ticker = str(row.get("ticker") or row.get("market_ticker") or "")
        if not ticker:
            continue
        raw = row.get("position_fp")
        if raw is None:
            continue
        try:
            signed = int(qty_from_fp_str(str(raw)))
        except ValueError:
            continue  # fail-closed: unreadable count ⇒ not a provable open position
        if signed > 0:
            out[ticker] = Side.YES
        elif signed < 0:
            out[ticker] = Side.NO
        # signed == 0 ⇒ flat, not open
    return out


@dataclass(frozen=True, slots=True)
class ExchangePosition:
    """The exchange's AUTHORITATIVE view of one open market (P0-5): the ticker, the
    side, and the ABSOLUTE quantity in centi-contracts read off ``position_fp``.
    Quantity is exchange truth; local fills supply only cost basis/legs/fees."""

    side: Side
    contracts_centi: int  # ABS(position_fp) in centi-contracts; always > 0 here


def open_combo_positions_from_positions(
    positions_payload: dict[str, Any],
) -> dict[str, ExchangePosition]:
    """P0-5 exact exchange-quantity reconciliation. Map a ``GET /portfolio/positions``
    payload to ``{combo_ticker: ExchangePosition}`` — the exchange's AUTHORITATIVE
    ticker, side, AND quantity for every market it reports OPEN with a nonzero
    position. Unlike :func:`open_combo_tickers_from_positions` (side only, used by
    the reservation reconcile) this keeps the MAGNITUDE so the exposure book can be
    rehydrated on the exchange's count rather than the reconstructed local one.

    ``position_fp`` is a SIGNED count (index-scan §portfolio): NEGATIVE = a NO
    position, POSITIVE = a YES position, 0 = flat. A flat/zero position is a
    SETTLED or netted-out market — EXCLUDED here (never rehydrated). An unparseable
    ``position_fp`` is SKIPPED (fail-closed: we never invent a quantity we can't
    read; rule 6 / defense #3).

    Subaccount pinning is NOT done here: the documented ``MarketPosition`` schema
    (index-scan §portfolio: ticker / total_traded_dollars / position_fp /
    market_exposure_dollars / realized_pnl_dollars / fees_paid_dollars /
    last_updated_ts) carries NO per-row subaccount field, so there is nothing to
    filter on. The pin is applied at the QUERY LAYER instead — the caller passes
    ``subaccount`` to ``GET /portfolio/positions`` and the endpoint returns ONLY
    that subaccount's positions (see ``QuoteApp._rehydrate_exposure_book``)."""
    rows = positions_payload.get("market_positions") or positions_payload.get("positions") or []
    out: dict[str, ExchangePosition] = {}
    for row in rows:
        ticker = str(row.get("ticker") or row.get("market_ticker") or "")
        if not ticker:
            continue
        raw = row.get("position_fp")
        if raw is None:
            continue
        try:
            signed = int(qty_from_fp_str(str(raw)))
        except ValueError:
            continue  # fail-closed: unreadable count ⇒ not a provable open position
        if signed == 0:
            continue  # flat ⇒ settled / netted out, not open
        side = Side.YES if signed > 0 else Side.NO
        out[ticker] = ExchangePosition(side=side, contracts_centi=abs(signed))
    return out


def reservation_ids_backed_by_exchange(
    outstanding: Sequence[Reservation | OpenPosition],
    open_by_ticker: dict[str, Side],
) -> set[str]:
    """Given the outstanding reservations (or their positions) and the exchange's
    open ``{combo_ticker: Side}`` map, return the reservation ids the exchange
    CONFIRMS landed — an outstanding reservation whose combo_ticker is open on the
    SAME side we hold. Everything else is treated as not-landed (released).

    Side match matters: a sell-only fill leaves us LONG NO, so the exchange must
    report a NO (negative) position on that ticker to confirm our fill. A position
    on the opposite side is NOT our reservation landing (fail-closed → release)."""
    backed: set[str] = set()
    for item in outstanding:
        position = item.position if isinstance(item, Reservation) else item
        exch_side = open_by_ticker.get(position.combo_ticker)
        if exch_side is not None and exch_side is position.our_side:
            backed.add(position.position_id)
    return backed

# A splitter turns the raw breach list from ``LimitChecker.check`` into the
# ENFORCED-only list (dropping SHADOW breaches, logging them). The lifecycle's
# ``_partition_breaches`` is exactly this; injecting it keeps the shadow policy
# in ONE place (the lifecycle) instead of duplicating the shadow rule here.
BreachSplitter = Callable[[list[Breach]], list[Breach]]


@dataclass(frozen=True, slots=True)
class Reservation:
    """A token for headroom held for one contemplated fill. Opaque to callers
    beyond its id; the service keys all its bookkeeping on ``reservation_id``.

    ``position`` is the exact ``OpenPosition`` that will be booked on commit — so
    the headroom RESERVED and the headroom COMMITTED are the same figure to the
    cent (no drift between the reservation snapshot and the booked position)."""

    reservation_id: str
    position: OpenPosition
    version: int  # the service version at which this reservation was granted


@dataclass(frozen=True, slots=True)
class ReserveResult:
    """Outcome of a ``try_reserve``. Exactly one of ``reservation`` /
    ``breaches`` is meaningful: a granted reservation, OR the ENFORCED breaches
    that denied it (empty ``breaches`` + None reservation never happens)."""

    reservation: Reservation | None
    breaches: list[Breach] = field(default_factory=list)

    @property
    def granted(self) -> bool:
        return self.reservation is not None


@dataclass
class _Held:
    """Internal record of one outstanding reservation."""

    position: OpenPosition
    # True once the confirm timed out and we assumed-committed: the headroom
    # stays consumed but the position is pending exchange reconciliation.
    unconfirmed: bool = False


class RiskReservationService:
    """Single writer of risk headroom. Wraps the exposure book + limit checker
    so capacity is reserved BEFORE the confirm network call, not after the fill.

    The exposure book remains the source of COMMITTED positions and open quotes
    (its mass-acceptance snapshot is unchanged). This service adds the layer of
    OUTSTANDING reservations that sit between "checked" and "committed", and folds
    them into every headroom check via the book's ``extra_positions`` seam — so
    the reservations reuse the exact same limit machinery (no reimplementation;
    hard rule 8).
    """

    def __init__(
        self,
        *,
        exposure: ExposureBook,
        limits: LimitChecker,
        breach_splitter: BreachSplitter,
    ) -> None:
        self._exposure = exposure
        self._limits = limits
        self._split = breach_splitter
        self._held: dict[str, _Held] = {}
        self._version = 0

    @property
    def version(self) -> int:
        """Monotonic stamp bumped on every state mutation. A caller can compare
        it before/after to learn the headroom moved under it."""
        return self._version

    @property
    def outstanding_count(self) -> int:
        return len(self._held)

    def outstanding_positions(self) -> list[OpenPosition]:
        """The positions of every outstanding reservation — the headroom that is
        held but not yet committed. Folded into every reserve() check."""
        return [held.position for held in self._held.values()]

    def is_outstanding(self, reservation_id: str) -> bool:
        return reservation_id in self._held

    def is_unconfirmed(self, reservation_id: str) -> bool:
        held = self._held.get(reservation_id)
        return held is not None and held.unconfirmed

    def try_reserve(
        self,
        reservation_id: str,
        candidate: OpenPosition,
        *,
        marginals: MarginalProvider,
        daily_pnl: DailyPnl,
        risk_bankroll_cc: int | None = None,
        bankroll_source_configured: bool = True,
        start_time_provider: StartTimeProvider | None = None,
        halt_inputs: HaltInputs | None = None,
        book_risk: PortfolioRisk | None = None,
        waived_games: Mapping[str, WaiverCertificate] | None = None,
    ) -> ReserveResult:
        """Atomically reserve headroom for ``candidate`` if the limits allow it.

        The check sees: committed positions (in the book) + ALL outstanding
        reservations + this candidate — so it can never grant headroom another
        reservation already holds. On PASS (no ENFORCED breach after the shadow
        split) the reservation is recorded and the version bumped; on FAIL nothing
        is recorded and the ENFORCED breaches are returned.

        ``waived_games`` (CONFIRM-PATH last-look MC waiver): per-game
        state-consistent worst-case certificates forwarded verbatim to
        ``LimitChecker.check``, which skips ONLY the game-loss and
        mutex-directional caps for exactly those games (re-validating each
        certificate against the live game-loss budget). Passed ONLY by the
        lifecycle's single waiver RETRY after a denial whose every enforced
        breach was one of those two caps; every other caller leaves the default
        None (byte-identical behaviour).

        Idempotent by ``reservation_id``: re-reserving an id already held returns
        the existing reservation WITHOUT re-checking or double-counting (a
        retried accept for the same quote must not consume headroom twice).

        This whole method is one synchronous critical section (no ``await``), so
        it is atomic between asyncio awaits — two concurrent accepts cannot
        interleave a check with a record.
        """
        existing = self._held.get(reservation_id)
        if existing is not None:
            # Already holding this id — hand back the same reservation, no
            # re-check, no double count (idempotent).
            return ReserveResult(
                reservation=Reservation(
                    reservation_id=reservation_id,
                    position=existing.position,
                    version=self._version,
                )
            )

        # Fold the candidate AND every already-outstanding reservation into the
        # snapshot the checker sees. ``candidate_positions`` drives the per-combo
        # + last-look caps; ``extra`` (the outstanding reservations) rides in via
        # the book snapshot's extra_positions so the game/slate/gross aggregates
        # count held-but-uncommitted headroom too.
        outstanding = self.outstanding_positions()
        raw = self._limits.check(
            self._exposure,
            marginals,
            daily_pnl,
            candidate_positions=[*outstanding, candidate],
            risk_bankroll_cc=risk_bankroll_cc,
            bankroll_source_configured=bankroll_source_configured,
            start_time_provider=start_time_provider,
            halt_inputs=halt_inputs,
            book_risk=book_risk,
            waived_games=waived_games,
        )
        enforced = self._split(raw)
        if enforced:
            return ReserveResult(reservation=None, breaches=enforced)

        self._held[reservation_id] = _Held(position=candidate)
        self._version += 1
        log.info(
            "risk_reservation_granted",
            reservation_id=reservation_id,
            combo_ticker=candidate.combo_ticker,
            max_loss_cc=candidate.max_loss_cc,
            outstanding=len(self._held),
            version=self._version,
        )
        return ReserveResult(
            reservation=Reservation(
                reservation_id=reservation_id,
                position=candidate,
                version=self._version,
            )
        )

    def commit(self, reservation_id: str) -> bool:
        """The fill is real (confirm landed): promote the held reservation into a
        committed position in the exposure book and drop the reservation. Returns
        True if this call committed it, False if it was not outstanding (already
        committed / released — idempotent, safe to replay).

        The committed position uses the reservation's own ``position`` (same id,
        same figures), so the headroom that was reserved equals the headroom now
        committed to the cent — the book's total is unchanged by the promotion.
        """
        held = self._held.pop(reservation_id, None)
        if held is None:
            return False
        # Idempotent against the book too: add_position is keyed on position_id,
        # so a re-add of the same position is a harmless overwrite. But if the
        # lifecycle already booked it directly (belt-and-suspenders), don't
        # double-count — add_position replaces by id, never appends.
        self._exposure.add_position(held.position)
        self._version += 1
        log.info(
            "risk_reservation_committed",
            reservation_id=reservation_id,
            combo_ticker=held.position.combo_ticker,
            was_unconfirmed=held.unconfirmed,
            outstanding=len(self._held),
            version=self._version,
        )
        return True

    def release(self, reservation_id: str) -> bool:
        """The confirm was DECLINED or lapsed: free the reserved headroom without
        booking a position. Returns True if this call released it, False if it
        was not outstanding (idempotent)."""
        held = self._held.pop(reservation_id, None)
        if held is None:
            return False
        self._version += 1
        log.info(
            "risk_reservation_released",
            reservation_id=reservation_id,
            combo_ticker=held.position.combo_ticker,
            outstanding=len(self._held),
            version=self._version,
        )
        return True

    def mark_unconfirmed(self, reservation_id: str) -> bool:
        """The confirm round-trip TIMED OUT — we do not know whether the fill
        landed. ASSUME COMMITTED: keep the headroom consumed (the reservation
        stays outstanding and still counts against every future check) and flag
        it for exchange reconciliation. This is the conservative branch — a
        reservation must never silently vanish on a lost ack, which would let a
        possibly-real position stop counting against the caps.

        Returns True if this call flagged it, False if it was not outstanding.
        Idempotent (flagging an already-flagged reservation is a no-op that still
        returns True — it IS outstanding-and-unconfirmed)."""
        held = self._held.get(reservation_id)
        if held is None:
            return False
        if not held.unconfirmed:
            held.unconfirmed = True
            self._version += 1
            log.warning(
                "risk_reservation_unconfirmed",
                reservation_id=reservation_id,
                combo_ticker=held.position.combo_ticker,
                detail="confirm timed out — assuming committed, headroom held "
                "pending exchange reconciliation",
                version=self._version,
            )
        return True

    def reconcile(self, committed_position_ids: set[str]) -> ReconcileOutcome:
        """Exchange-first reconciliation (startup, or after a confirm timeout).

        ``committed_position_ids`` is the set of position ids the EXCHANGE reports
        as actually open (mapped from its ledger to our position-id scheme). For
        every OUTSTANDING reservation:
          - id IS in the exchange set → the fill really landed → commit it.
          - id is NOT in the set → it did not land → release it.
        The exchange ledger is the one ruler we can't bend (defense #3): its truth
        overrides our assume-committed guess from a timeout. Idempotent and
        order-independent. Returns which ids were committed vs released.

        This reconciles ALL outstanding reservations (not only the unconfirmed
        ones), so it doubles as the exchange-first startup pass: any stale
        reservation the exchange does not report open simply did not fill and is
        released. That is safe because a reservation that has NOT been marked
        unconfirmed is one whose confirm has not returned yet — calling reconcile
        while a confirm is genuinely in flight would race, so the caller runs this
        only from the maintenance/startup loop (no confirm in flight), never mid
        round-trip.
        """
        committed: list[str] = []
        released: list[str] = []
        for reservation_id in list(self._held):
            position_id = self._held[reservation_id].position.position_id
            if position_id in committed_position_ids:
                if self.commit(reservation_id):
                    committed.append(reservation_id)
            else:
                if self.release(reservation_id):
                    released.append(reservation_id)
        if committed or released:
            log.info(
                "risk_reservations_reconciled",
                committed=committed,
                released=released,
                remaining=len(self._held),
                version=self._version,
            )
        return ReconcileOutcome(committed=committed, released=released)


@dataclass(frozen=True, slots=True)
class ReconcileOutcome:
    committed: list[str]
    released: list[str]
