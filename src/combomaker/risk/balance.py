"""Bankroll / balance tracker — the authoritative source of the money figure
the risk caps scale from, plus a running realized-P&L ledger.

Operator directive (R2 §1): "keep track of our current kalshi balance, and add
NO-settlement wins; the system must know balance / legs / losses at any moment."

Two responsibilities, deliberately separated:

1. **Live bankroll — EQUITY-AWARE denominator** (FIX 1). Polled from the
   exchange via ``get_balance``, which returns BOTH ``balance`` (available cash)
   AND ``portfolio_value`` (mark of open positions), each in CENTS → converted
   ×100 to centi-cents explicitly. We keep the two SEPARATE
   (``available_cash_cc`` / ``portfolio_value_cc``), derive
   ``exchange_equity_cc = cash + portfolio_value``, and expose the risk-capital
   denominator the caps scale from:
   ``risk_bankroll_cc = min(start_of_day_equity, cash + haircut·portfolio_value)``.
   The exchange ledger is AUTHORITATIVE — the one ruler we can't bend (defense
   #3) — but available cash alone is the WRONG denominator: it shrinks the moment
   capital is deployed (deployed != lost). The ``min`` keeps the denominator flat
   on pure deployment while refusing to inflate caps from a mark-to-model gain.
   Each successful poll stamps a monotonic time; STALE (no fresh reading within
   ``stale_after_s``) ⇒ the WHOLE denominator is UNKNOWN and every %-of-bankroll
   cap fails closed (hard rule 6).

2. **Realized-P&L ledger** (``realized_pnl_cc`` / ``cumulative_loss_cc``). An
   INDEPENDENT running tally the tracker maintains as settlements land, so the
   operator can ask "what have we made / lost so far" at any instant WITHOUT
   waiting for the next balance poll and without decomposing the raw exchange
   ledger. It is a cross-check on the live balance, never a driver of it (the
   live poll already contains the same money) — so the two are never summed.

Settlement sign convention comes ONLY from ``Conventions`` +the verified
ground truth (2026-07-10 demo), never hardcoded here beyond the arithmetic. The
combo settles to a REALIZED YES value ``V = settled_value ∈ [0,1]`` (product of
the leg values — SCALAR under a DNP/rain/void, not just {0,1};
docs/dnp_scalar_settlement.md), and realized P&L is NET of the fee booked at
fill (FIX 2/3):

- our position is LONG NO (sell-only). The NO pays ``(1 − V)`` per contract:
  realized = contracts × ((1 − V) − entry_price) − fee.
    V=0 (binary MISS) → +($1 − premium) − fee (e.g. the demo's +$0.50 on 1 ct
      paid $0.50, $0 fee); V=1 (binary HIT) → −premium − fee (NO worthless);
    a scalar V=0.7 → NO pays $0.30 → partial. Binary cases reproduce Phase 0
    exactly; the fee is $0 for our combo maker fills today (pricing/fees.py) but
    booked correctly for any nonzero-fee series.

All money is integer centi-cents (``core/money.py``). No binary floats.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from typing import Any, Protocol

from combomaker.core.clock import Clock
from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import (
    CC_PER_DOLLAR,
    ZERO,
    CentiCents,
    MoneyParseError,
    cc_from_dollars_str,
)
from combomaker.core.quantity import CentiContracts
from combomaker.ops.logging import get_logger

log = get_logger(__name__)

# Kalshi's /portfolio/balance returns `balance` as an int in CENTS
# (docs/api-notes/index-scan.md: int64 cents) and `balance_dollars` as an exact
# fixed-point string. 1 cent = 100 centi-cents. The same payload also carries
# `portfolio_value` (CENTS) = current mark of all open positions.
CC_PER_CENT = 100

# Operator-set haircut on portfolio_value in the risk-capital denominator (FIX 1,
# RISK_BUILD_PLAN Phase 1). Conservative DEFAULT 0.5: we lend only half of the
# mark-to-model position value to the caps, because deployed capital is real
# (deployed != lost) but a mark is softer than settled cash. FLAGGED for operator
# to set per risk tolerance. Range [0,1]: 0 = cash-only denominator (most
# conservative), 1 = full equity.
DEFAULT_PORTFOLIO_HAIRCUT = Fraction(1, 2)

# Backstop TTL on a settlement RECEIVABLE (2026-07-19 false-positive give-back
# kill): a pending receivable that is never exchange-confirmed expires after this
# long and the give-back measurement returns to the raw peak−equity figure. Sized
# from the observed settlement-cascade payment lag (minutes) with a wide margin;
# NOT an operator knob — it is a structural bound on how long a predicted credit
# may shield the give-back halts, and a wrong prediction is already caught to the
# cent by HALT_RECONCILIATION_MISMATCH when the settlement row lands.
DEFAULT_RECEIVABLE_TTL_S = 1800.0


class BalanceSource(Protocol):
    """The one REST method the tracker needs. ``KalshiRestClient`` satisfies it;
    tests pass a fake so no live credentials are ever required."""

    async def get_balance(self) -> dict[str, Any]: ...


class StaleBalanceError(RuntimeError):
    """The live bankroll reading is older than ``stale_after_s`` — fail closed."""


class BalanceParseError(ValueError):
    """The exchange balance payload could not be represented exactly in cc."""


def _parse_balance_cc(payload: dict[str, Any]) -> CentiCents:
    """Exact centi-cents from a /portfolio/balance payload.

    Prefer the exact fixed-point ``balance_dollars`` string; fall back to the
    int-cents ``balance`` field. Any doubt raises (never guess a bankroll).
    """
    dollars = payload.get("balance_dollars")
    if dollars is not None:
        try:
            return cc_from_dollars_str(str(dollars))
        except MoneyParseError as exc:
            raise BalanceParseError(f"bad balance_dollars {dollars!r}: {exc}") from exc
    cents = payload.get("balance")
    if cents is None:
        raise BalanceParseError("balance payload has neither balance_dollars nor balance")
    if not isinstance(cents, int) or isinstance(cents, bool):
        raise BalanceParseError(f"balance is not an int-cents value: {cents!r}")
    return CentiCents(cents * CC_PER_CENT)


def _parse_portfolio_value_cc(payload: dict[str, Any]) -> CentiCents:
    """Exact centi-cents for ``portfolio_value`` from a /portfolio/balance
    payload — the current mark of all open positions.

    Prefer the exact fixed-point ``portfolio_value_dollars`` string if present;
    else the int-cents ``portfolio_value`` field (× 100 → cc, explicit). Any
    doubt raises (never guess). NOTE the wire is CENTS, not centi-cents — the
    ×100 conversion is the explicit boundary the caps depend on.
    """
    dollars = payload.get("portfolio_value_dollars")
    if dollars is not None:
        try:
            return cc_from_dollars_str(str(dollars))
        except MoneyParseError as exc:
            raise BalanceParseError(
                f"bad portfolio_value_dollars {dollars!r}: {exc}"
            ) from exc
    cents = payload.get("portfolio_value")
    if cents is None:
        raise BalanceParseError(
            "balance payload has neither portfolio_value_dollars nor portfolio_value"
        )
    if not isinstance(cents, int) or isinstance(cents, bool):
        raise BalanceParseError(f"portfolio_value is not an int-cents value: {cents!r}")
    return CentiCents(cents * CC_PER_CENT)


def _no_payout_per_contract_cc(settled_value: float) -> int:
    """NO payout per contract in cc = $1 − V, with the DNP "rounded down"
    settlement convention (docs/dnp_scalar_settlement.md §6): the combo's YES
    value V is floored onto the centi-cent grid, so ``1 − floor(V)`` is what the
    NO side receives — always ≥ ``1 − V``, i.e. NO-seller favorable by ≤ ½ tick.

    Binary V ∈ {0,1} is exact either way (V=0 → $1.00, V=1 → $0.00). A scalar
    that already lands on the grid (e.g. 0.70 → 7,000 cc) is likewise exact.
    """
    # V arrives ALREADY floored onto Kalshi's cent grid by the DNP settlement
    # convention, so we only convert it to cc — round(), not int(): the float
    # int(0.57 * 10000) is 5699.999… → 5699 (a spurious 1cc under-floor that
    # would trip HALT_RECONCILIATION_MISMATCH), whereas round() recovers the
    # true grid value 5700. Grid-aligned V ⇒ round == the intended floor.
    v_cc = round(settled_value * CC_PER_DOLLAR)
    return CC_PER_DOLLAR - v_cc


@dataclass(frozen=True, slots=True)
class Settlement:
    """One combo position settling. ``our_side``, ``contracts`` and
    ``entry_price_cc`` come straight off the ``OpenPosition`` that filled
    (exposure.py).

    ``settled_value`` is the combo's REALIZED YES value V ∈ [0,1] — the product
    of each leg's settlement value (a leg can settle SCALAR under DNP/rain/void,
    docs/dnp_scalar_settlement.md), so V is NOT restricted to {0,1}. The ACTUAL
    scalar is retained here and never coerced to 0/1. Our LONG NO pays
    ``contracts × (1 − V)``. ``fee_cc`` is booked at fill via pricing/fees.py
    ($0 for our combo maker fills today; correct for any nonzero-fee series).
    """

    position_id: str
    our_side: Side
    contracts: CentiContracts
    entry_price_cc: CentiCents  # premium we PAID per contract
    settled_value: float = 0.0  # V = combo realized YES value ∈ [0,1]
    fee_cc: CentiCents = ZERO   # fees booked at fill (pricing/fees.py)

    @classmethod
    def binary(
        cls,
        position_id: str,
        our_side: Side,
        contracts: CentiContracts,
        entry_price_cc: CentiCents,
        *,
        settled_yes: bool,
        fee_cc: CentiCents = ZERO,
    ) -> Settlement:
        """Binary convenience constructor: HIT (settles YES) → V=1, MISS → V=0.
        Retained so binary settlements read exactly as before Phase 1."""
        return cls(
            position_id=position_id,
            our_side=our_side,
            contracts=contracts,
            entry_price_cc=entry_price_cc,
            settled_value=1.0 if settled_yes else 0.0,
            fee_cc=fee_cc,
        )

    @property
    def settled_yes(self) -> bool:
        """Derived binary convenience: did the combo fully hit (V == 1)? Only
        true at exactly V=1 — a scalar V ∈ (0,1) is neither a clean HIT nor a
        clean MISS, so this is a HELPER, never the settlement value itself."""
        return self.settled_value >= 1.0


class BalanceTracker:
    """Live bankroll (fail-closed on stale) + realized-P&L ledger.

    The bankroll is refreshed by ``await refresh(source)`` (call it from the
    status loop / maintenance tick). The realized ledger is advanced by
    ``apply_settlement(...)`` as combos settle. Queries are O(1).
    """

    def __init__(
        self,
        conventions: Conventions,
        clock: Clock,
        *,
        stale_after_s: float,
        portfolio_haircut: Fraction = DEFAULT_PORTFOLIO_HAIRCUT,
        receivable_ttl_s: float = DEFAULT_RECEIVABLE_TTL_S,
    ) -> None:
        if not 0 <= portfolio_haircut <= 1:
            raise ValueError(f"portfolio_haircut must be in [0,1], got {portfolio_haircut}")
        self._conventions = conventions
        self._clock = clock
        self._stale_after_s = stale_after_s
        self._haircut = portfolio_haircut
        # Available cash (`balance`) and current position mark (`portfolio_value`),
        # BOTH from get_balance, each converted cents -> cc explicitly. Kept
        # SEPARATE (never conflated); exchange_equity is their derived sum.
        self._available_cash_cc: CentiCents | None = None
        self._portfolio_value_cc: CentiCents | None = None
        self._last_poll_ns: int | None = None
        # Start-of-day equity anchor + the UTC date it was set for (day-boundary
        # rule: first successful poll whose UTC calendar date differs re-anchors).
        self._start_of_day_equity_cc: CentiCents | None = None
        self._anchor_utc_date: date | None = None
        # Intraday peak of exchange equity — the high-water mark the give-back
        # (drawdown / hard-trip) halts measure against. Re-anchored to the new
        # day's start-of-day equity at the SAME UTC boundary as the SOD anchor
        # (the halts are INTRADAY give-back, matching CAP_recommendation_2000.md),
        # then high-water-marked on every fresh poll.
        self._peak_equity_cc: CentiCents | None = None
        self._realized_pnl_cc: int = 0
        self._cumulative_loss_cc: int = 0
        self._accrued_fees_cc: int = 0
        self._settled_ids: set[str] = set()
        # SETTLEMENT RECEIVABLES (2026-07-19 false-positive give-back kill: the
        # exchange removes a settled position from ``portfolio_value`` BEFORE
        # crediting ``balance``, so during a settlement cascade exchange equity
        # transiently dips by exactly the in-flight settlement value — the
        # give-back halts read that trough as a $430 drawdown whose real losers
        # were $29.51). A receivable = a held position's PREDICTED gross
        # settlement credit once its outcome is KNOWN from exchange-graded leg
        # facts. The give-back halts subtract the pending sum from the measured
        # give-back (floored at 0) — receivables never touch equity or the peak
        # itself, so they cannot inflate a peak or fabricate equity. Lifecycle:
        # noted by the fact sweep → confirmed when the settlement reconciler
        # books the exchange row → DROPPED at the first successful balance poll
        # whose request STARTED after that confirmation (that poll provably
        # includes the credited cash, so the shield lifts exactly when the cash
        # is in the reading) → TTL backstop expires a never-confirmed one loudly.
        self._receivable_ttl_s = receivable_ttl_s
        self._receivables_cc: dict[str, int] = {}          # position_id → credit cc
        self._receivable_noted_ns: dict[str, int] = {}     # for the TTL backstop
        self._receivable_confirmed_ns: dict[str, int] = {}  # reconciler confirm stamp

    # --- live bankroll -------------------------------------------------------

    async def refresh(self, source: BalanceSource) -> CentiCents:
        """Poll the exchange, update available cash + portfolio value, and
        (on the first poll of a new UTC trading day) re-anchor start-of-day
        equity. Returns the fresh available-cash cc.

        On a parse failure both readings are LEFT UNCHANGED and the staleness
        clock keeps running (a bad poll must not overwrite good readings, and
        must not reset the freshness stamp) — after ``stale_after_s`` the frozen
        values go stale and every cap fails closed.

        Day-boundary rule (FLAGGED for operator): the trading day is keyed on
        the UTC calendar date of ``clock.now()``. The FIRST successful poll on a
        new UTC date sets ``start_of_day_equity`` to that poll's exchange equity.
        This is a simple, deterministic boundary — NOT the exchange's settlement
        session. If the desk wants an ET/close-based boundary or a manual anchor
        (e.g. re-anchor after a deposit), call ``set_start_of_day_equity`` to
        override.
        """
        # Receivable drop rule needs the REQUEST-START instant: a poll that
        # STARTED after the reconciler confirmed a settlement provably includes
        # its credited cash (the exchange books the settlement row and the
        # balance credit as one ledger event), so dropping the receivable on
        # this poll's success can never leave a double-count window in either
        # direction. Captured BEFORE the await.
        request_start_ns = self._clock.monotonic_ns()
        payload = await source.get_balance()
        cash_cc = _parse_balance_cc(payload)
        portfolio_cc = _parse_portfolio_value_cc(payload)
        # Both parsed OK before any mutation (fail atomically).
        self._available_cash_cc = cash_cc
        self._portfolio_value_cc = portfolio_cc
        self._last_poll_ns = self._clock.monotonic_ns()
        self._drop_confirmed_receivables(request_start_ns)
        today = self._clock.now().date()
        equity_cc = int(cash_cc) + int(portfolio_cc)
        if self._anchor_utc_date != today:
            self._start_of_day_equity_cc = CentiCents(equity_cc)
            self._anchor_utc_date = today
            # New trading day: the intraday peak restarts at today's SOD equity,
            # never carried over from yesterday (give-back is measured within the
            # day, in lockstep with the SOD re-anchor above).
            self._peak_equity_cc = CentiCents(equity_cc)
        elif self._peak_equity_cc is None or equity_cc > int(self._peak_equity_cc):
            self._peak_equity_cc = CentiCents(equity_cc)
        return cash_cc

    def set_start_of_day_equity(self, equity_cc: CentiCents) -> None:
        """Operator override of the start-of-day equity anchor (e.g. after a
        deposit/withdrawal, or to use a non-UTC session boundary). Also stamps
        the current UTC date so the next auto-anchor waits for the following
        day."""
        self._start_of_day_equity_cc = equity_cc
        self._anchor_utc_date = self._clock.now().date()
        # A manual re-anchor also restarts the intraday peak: the drawdown the
        # halts measure is give-back from THIS anchor, not a stale prior peak
        # (e.g. after a deposit the old high-water mark is meaningless).
        self._peak_equity_cc = equity_cc

    def apply_external_transfer(self, delta_cc: int, *, kind: str, ref: str) -> None:
        """AUTOMATIC anchor adjustment for an external cash transfer (a NEW
        applied deposit ⇒ positive ``delta_cc`` net of its fee; a withdrawal ⇒
        negative, gross of its fee). Called by the app's transfer watcher —
        the no-manual-intervention replacement for "call
        ``set_start_of_day_equity`` after a deposit" (2026-07-21).

        BOTH anchors shift by exactly the transfer: SOD' = SOD + Δ keeps the
        daily-loss measurement pure P&L (a deposit is not profit, a withdrawal
        is not loss), and peak' = peak + Δ keeps the give-back halts pure
        drawdown (a withdrawal must never read as a $-for-$ give-back, and a
        deposit must not inflate headroom under the peak). No anchor yet (no
        poll this day) ⇒ nothing to adjust — the first poll anchors on a
        balance that already contains the transfer."""
        if self._start_of_day_equity_cc is not None:
            self._start_of_day_equity_cc = CentiCents(
                int(self._start_of_day_equity_cc) + delta_cc
            )
        if self._peak_equity_cc is not None:
            self._peak_equity_cc = CentiCents(int(self._peak_equity_cc) + delta_cc)
        log.info(
            "external_transfer_anchors_adjusted",
            kind=kind,
            ref=ref,
            delta_cc=delta_cc,
            start_of_day_equity_cc=(
                None
                if self._start_of_day_equity_cc is None
                else int(self._start_of_day_equity_cc)
            ),
            peak_equity_cc=(
                None if self._peak_equity_cc is None else int(self._peak_equity_cc)
            ),
        )

    @property
    def is_stale(self) -> bool:
        """True when there is no reading yet, or the last good poll is older
        than ``stale_after_s`` (fail-closed sentinel for the caps)."""
        if (
            self._available_cash_cc is None
            or self._portfolio_value_cc is None
            or self._last_poll_ns is None
        ):
            return True
        age_s = (self._clock.monotonic_ns() - self._last_poll_ns) / 1e9
        return age_s > self._stale_after_s

    def _fresh_or_raise(self) -> None:
        if self.is_stale:
            raise StaleBalanceError(
                "balance reading is stale or absent — %-of-bankroll caps must "
                "fail closed (poll get_balance)"
            )

    @property
    def available_cash_cc(self) -> CentiCents:
        """Available cash for trading (Kalshi ``balance``). Fails closed on
        stale. This is the RAW cash, kept separate from equity — never
        conflated."""
        self._fresh_or_raise()
        assert self._available_cash_cc is not None
        return self._available_cash_cc

    @property
    def portfolio_value_cc(self) -> CentiCents:
        """Current mark of all open positions (Kalshi ``portfolio_value``).
        Fails closed on stale. A mark-to-model figure — softer than cash, which
        is why the risk denominator haircuts it."""
        self._fresh_or_raise()
        assert self._portfolio_value_cc is not None
        return self._portfolio_value_cc

    @property
    def exchange_equity_cc(self) -> CentiCents:
        """Total exchange equity = available_cash + portfolio_value. Fails closed
        on stale. This is total account value, NOT the risk denominator (that is
        ``risk_bankroll_cc``, which haircuts the position mark and floors at
        start-of-day)."""
        self._fresh_or_raise()
        assert self._available_cash_cc is not None
        assert self._portfolio_value_cc is not None
        return CentiCents(int(self._available_cash_cc) + int(self._portfolio_value_cc))

    @property
    def peak_equity_cc(self) -> CentiCents:
        """Intraday high-water mark of exchange equity — what the give-back
        (drawdown / hard-trip) halts measure against. Fails closed on stale, and
        raises if never anchored (no poll yet ⇒ no peak; inventing one would be a
        convenient default the halts must never rely on)."""
        self._fresh_or_raise()
        if self._peak_equity_cc is None:
            raise StaleBalanceError("peak equity not anchored yet")
        return self._peak_equity_cc

    @property
    def start_of_day_equity_cc(self) -> CentiCents:
        """The start-of-day equity anchor (see the day-boundary rule in
        ``refresh``). Fails closed on stale, and raises if never anchored."""
        self._fresh_or_raise()
        if self._start_of_day_equity_cc is None:
            raise StaleBalanceError("start-of-day equity not anchored yet")
        return self._start_of_day_equity_cc

    @property
    def risk_bankroll_cc(self) -> CentiCents:
        """The RISK-CAPITAL DENOMINATOR the %-of-bankroll caps scale from:

            min(start_of_day_equity, available_cash + haircut · portfolio_value)

        The ``min`` does two jobs at once: the right term keeps the denominator
        ~flat when capital is merely DEPLOYED (cash falls, position mark rises —
        deployed != lost, so caps must not shrink), while the left term (SOD
        equity) prevents an intraday mark-to-model GAIN from inflating the caps.
        The haircut applies ONLY to ``portfolio_value`` (the softer mark), never
        to cash. Fails closed on stale — the whole denominator is UNKNOWN and
        every cap must breach.
        """
        self._fresh_or_raise()
        assert self._available_cash_cc is not None
        assert self._portfolio_value_cc is not None
        if self._start_of_day_equity_cc is None:
            raise StaleBalanceError("start-of-day equity not anchored yet")
        haircut_pv = (
            self._haircut.numerator * int(self._portfolio_value_cc)
        ) // self._haircut.denominator
        deployed_aware = int(self._available_cash_cc) + haircut_pv
        return CentiCents(min(int(self._start_of_day_equity_cc), deployed_aware))

    @property
    def bankroll_cc(self) -> CentiCents:
        """Back-compat: the authoritative live AVAILABLE CASH. Raises
        ``StaleBalanceError`` when stale. New risk code should scale caps from
        ``risk_bankroll_cc`` (equity-aware) and query cash via
        ``available_cash_cc``; this alias stays so existing callers/tests read
        unchanged."""
        return self.available_cash_cc

    def bankroll_cc_or_none(self) -> CentiCents | None:
        """Non-raising accessor for display/logging: available cash, or None
        when stale."""
        return None if self.is_stale else self._available_cash_cc

    def available_cash_cc_or_none(self) -> CentiCents | None:
        """Non-raising current AVAILABLE CASH (Kalshi ``balance``), or None when
        stale. Clearly-named alias of ``bankroll_cc_or_none`` for risk-critical
        callers: the book-risk ruin basis is built on cash + modeled entry cost
        (COST basis, not exchange equity) so it never double-counts the already-
        marked position value (P1-3). None ⇒ the ruin cap simply does not
        evaluate (fail-closed: a missing cash reading is never an invented
        equity)."""
        return None if self.is_stale else self._available_cash_cc

    def risk_bankroll_cc_or_none(self) -> CentiCents | None:
        """Non-raising accessor for display/logging: the risk denominator, or
        None when stale / not yet anchored."""
        if self.is_stale or self._start_of_day_equity_cc is None:
            return None
        return self.risk_bankroll_cc

    def exchange_equity_cc_or_none(self) -> CentiCents | None:
        """Non-raising current exchange equity (cash + portfolio), or None when
        stale. Pairs with ``peak_equity_cc_or_none`` to feed the give-back halts
        fail-closed (a missing reading simply skips that halt's evaluation)."""
        if self.is_stale or self._available_cash_cc is None or self._portfolio_value_cc is None:
            return None
        return CentiCents(int(self._available_cash_cc) + int(self._portfolio_value_cc))

    def peak_equity_cc_or_none(self) -> CentiCents | None:
        """Non-raising intraday peak equity, or None when stale / not yet
        anchored. Feeds ``HaltInputs.peak_equity_cc``; None ⇒ the give-back
        halts skip (no invented peak)."""
        if self.is_stale or self._peak_equity_cc is None:
            return None
        return self._peak_equity_cc

    # --- settlement receivables (give-back cascade shield) -------------------

    def note_receivable(self, position_id: str, amount_cc: int) -> None:
        """Record/refresh a held position's PREDICTED gross settlement credit —
        called by the lifecycle's fact sweep once EVERY leg of the position
        carries an exchange-graded fact (outcome KNOWN, cash not yet observed).

        Zero/negative amounts are ignored (a losing position produces no
        receivable, so a genuine loss cascade is never shielded). Re-noting an
        already-confirmed receivable is a no-op — once the reconciler has
        confirmed the exchange row, only the drop rule may touch it (a sweep
        racing the reconciler must not resurrect the TTL clock)."""
        if amount_cc <= 0:
            return
        if position_id in self._receivable_confirmed_ns:
            return
        if position_id not in self._receivables_cc:
            self._receivable_noted_ns[position_id] = self._clock.monotonic_ns()
            log.info(
                "settlement_receivable_noted",
                position_id=position_id,
                amount_cc=amount_cc,
            )
        self._receivables_cc[position_id] = amount_cc

    def confirm_receivable(self, position_id: str) -> None:
        """Stamp a receivable exchange-CONFIRMED — called by the settlement
        reconciler right after it books the exchange's settlement row for this
        position. The receivable then drops at the first successful balance
        poll whose request started after this instant (see ``refresh``). A
        position with no pending receivable is a no-op (the reconciler confirms
        everything it books; not everything was fact-resolved first)."""
        if position_id not in self._receivables_cc:
            return
        self._receivable_confirmed_ns[position_id] = self._clock.monotonic_ns()

    def pending_receivables_cc(self) -> int:
        """Sum of pending settlement receivables in cc — what the give-back
        halts subtract from the measured give-back (floored at 0 by the
        checker). TTL-expired entries are dropped LOUDLY first: a receivable
        the reconciler never confirmed within the TTL stops shielding the
        halts (fail-closed backstop — if the cash truly is not coming, the
        give-back must become visible again)."""
        if self._receivables_cc:
            now_ns = self._clock.monotonic_ns()
            ttl_ns = int(self._receivable_ttl_s * 1e9)
            expired = [
                pid
                for pid, noted_ns in self._receivable_noted_ns.items()
                if pid not in self._receivable_confirmed_ns
                and now_ns - noted_ns > ttl_ns
            ]
            for pid in expired:
                amount = self._receivables_cc.pop(pid, 0)
                self._receivable_noted_ns.pop(pid, None)
                log.warning(
                    "settlement_receivable_ttl_expired",
                    position_id=pid,
                    amount_cc=amount,
                    ttl_s=self._receivable_ttl_s,
                )
        return sum(self._receivables_cc.values())

    def _drop_confirmed_receivables(self, poll_request_start_ns: int) -> None:
        """Drop every receivable confirmed BEFORE this successful poll's request
        started — that poll's reading provably contains the credited cash, so
        the shield lifts in the same instant the cash enters the equity figure
        (no window where both the receivable and the cash count)."""
        dropped = [
            pid
            for pid, confirmed_ns in self._receivable_confirmed_ns.items()
            if confirmed_ns < poll_request_start_ns
        ]
        for pid in dropped:
            amount = self._receivables_cc.pop(pid, 0)
            self._receivable_noted_ns.pop(pid, None)
            self._receivable_confirmed_ns.pop(pid)
            log.info(
                "settlement_receivable_dropped",
                position_id=pid,
                amount_cc=amount,
            )

    # --- realized-P&L ledger -------------------------------------------------

    def apply_settlement(self, settlement: Settlement) -> int:
        """Advance the realized ledger by one settled position. Idempotent per
        ``position_id`` (a replayed settlement message is a no-op). Returns the
        realized cc delta this settlement contributed (0 on a duplicate).

        The combo settles to a REALIZED YES value V = ``settled_value`` ∈ [0,1]
        (product of the leg settlement values; SCALAR under DNP/rain/void, not
        restricted to {0,1} — docs/dnp_scalar_settlement.md). Economic result,
        in integer cc, NET of the fee booked at fill (FIX 2/3):

          LONG NO:  realized = contracts × ((1 − V) − entry_price) − fee
                      V=0 → +($1 − premium) (binary miss; full win)
                      V=1 →  −premium        (binary hit; forfeit premium)
                      V=0.7 → partial        (scalar; NO pays $0.30)
          LONG YES (defensive; not sell-only): the mirror payout ``V × $1``.

        A NO credit requires the verified complement convention:
        ``combo_no_pays_complement`` must be True (it is, since 2026-07-10) — a
        fractional NO payout is exactly the gate behind that flag and must NOT
        trip HALT_RECONCILIATION_MISMATCH.
        """
        if settlement.position_id in self._settled_ids:
            return 0

        contracts = int(settlement.contracts)
        premium_paid_cc = contracts * int(settlement.entry_price_cc) // 100
        fee_cc = int(settlement.fee_cc)

        if settlement.our_side is Side.NO:
            # A NO credit requires the verified complement convention.
            if not self._conventions.combo_no_pays_complement:
                raise StaleBalanceError(
                    "NO settlement credit requires combo_no_pays_complement "
                    "verified True (Conventions) — refusing to book an unverified "
                    "settlement"
                )
            # NO pays $1 − V per contract (scalar-aware, "rounded down").
            payout_per_ct_cc = _no_payout_per_contract_cc(settlement.settled_value)
        else:
            # LONG YES pays V per contract (the mirror; defensive path).
            payout_per_ct_cc = round(settlement.settled_value * CC_PER_DOLLAR)

        # Mark settled ONLY after every raising guard above, so a settlement that
        # raised (e.g. convention not yet verified) can be replayed and booked
        # once the guard clears — idempotency without a poison-pill drop.
        self._settled_ids.add(settlement.position_id)
        payout_cc = contracts * payout_per_ct_cc // 100

        realized_cc = payout_cc - premium_paid_cc - fee_cc
        self._realized_pnl_cc += realized_cc
        self._accrued_fees_cc += fee_cc
        if realized_cc < 0:
            self._cumulative_loss_cc += -realized_cc

        log.info(
            "settlement_booked",
            position_id=settlement.position_id,
            our_side=str(settlement.our_side),
            settled_value=settlement.settled_value,
            fee_cc=fee_cc,
            realized_cc=realized_cc,
            realized_pnl_cc=self._realized_pnl_cc,
        )
        return realized_cc

    @property
    def realized_pnl_cc(self) -> int:
        """Cumulative realized P&L (signed cc) across all booked settlements."""
        return self._realized_pnl_cc

    @property
    def cumulative_loss_cc(self) -> int:
        """Running sum of realized LOSSES (positive cc) — the losing side of the
        ledger, queryable at any moment (operator directive)."""
        return self._cumulative_loss_cc

    @property
    def accrued_fees_cc(self) -> int:
        """Cumulative fees (positive cc) subtracted from realized P&L across all
        booked settlements. $0 today for our combo maker fills (pricing/fees.py
        computes 0 on a QUADRATIC maker fill), but accrued correctly here so a
        nonzero-fee series reconciles to the cent (defense #3)."""
        return self._accrued_fees_cc

    @property
    def settled_count(self) -> int:
        return len(self._settled_ids)
