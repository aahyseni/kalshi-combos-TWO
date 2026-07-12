"""Bankroll / balance tracker — the authoritative source of the money figure
the risk caps scale from, plus a running realized-P&L ledger.

Operator directive (R2 §1): "keep track of our current kalshi balance, and add
NO-settlement wins; the system must know balance / legs / losses at any moment."

Two responsibilities, deliberately separated:

1. **Live bankroll** (``bankroll_cc``). Polled from the exchange via the existing
   ``get_balance`` REST call. This is AUTHORITATIVE — the exchange ledger is the
   one ruler we can't bend (CLAUDE.md defense #3). It already reflects every
   premium debit and every settlement credit the matching engine has applied, so
   it is the number the caps use. Each successful poll stamps a monotonic time;
   if the poll goes STALE (no fresh reading within ``stale_after_s``) the
   bankroll is UNKNOWN and every %-of-bankroll cap must fail closed — a book
   whose size we can't measure can't be risk-checked (CLAUDE.md hard rule 6).

2. **Realized-P&L ledger** (``realized_pnl_cc`` / ``cumulative_loss_cc``). An
   INDEPENDENT running tally the tracker maintains as settlements land, so the
   operator can ask "what have we made / lost so far" at any instant WITHOUT
   waiting for the next balance poll and without decomposing the raw exchange
   ledger. It is a cross-check on the live balance, never a driver of it (the
   live poll already contains the same money) — so the two are never summed.

Settlement sign convention comes ONLY from ``Conventions`` +the verified
ground truth (2026-07-10 demo), never hardcoded here beyond the arithmetic:

- our position is LONG NO (sell-only). When the parlay **MISSES** (settles NO)
  the NO pays $1/contract: realized = payout_received - premium_paid (a WIN,
  e.g. the demo's +$0.50 on 1 contract paid $0.50). When the parlay **HITS**
  (settles YES) the NO is worthless: realized = -premium_paid (we forfeit what
  we paid). Both are expressed as an economic delta so the ledger is a clean
  sum.

All money is integer centi-cents (``core/money.py``). No binary floats.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from combomaker.core.clock import Clock
from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import (
    CC_PER_DOLLAR,
    CentiCents,
    MoneyParseError,
    cc_from_dollars_str,
)
from combomaker.core.quantity import CentiContracts
from combomaker.ops.logging import get_logger

log = get_logger(__name__)

# Kalshi's /portfolio/balance returns `balance` as an int in CENTS
# (docs/api-notes/index-scan.md: int64 cents) and `balance_dollars` as an exact
# fixed-point string. 1 cent = 100 centi-cents.
CC_PER_CENT = 100


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


@dataclass(frozen=True, slots=True)
class Settlement:
    """One combo position settling. ``our_side`` and the two money numbers come
    straight off the ``OpenPosition`` that filled (exposure.py)."""

    position_id: str
    our_side: Side
    contracts: CentiContracts
    entry_price_cc: CentiCents  # premium we PAID per contract
    settled_yes: bool           # True = parlay HIT (YES); False = MISSED (NO)


class BalanceTracker:
    """Live bankroll (fail-closed on stale) + realized-P&L ledger.

    The bankroll is refreshed by ``await refresh(source)`` (call it from the
    status loop / maintenance tick). The realized ledger is advanced by
    ``apply_settlement(...)`` as combos settle. Queries are O(1).
    """

    def __init__(self, conventions: Conventions, clock: Clock, *, stale_after_s: float) -> None:
        self._conventions = conventions
        self._clock = clock
        self._stale_after_s = stale_after_s
        self._bankroll_cc: CentiCents | None = None
        self._last_poll_ns: int | None = None
        self._realized_pnl_cc: int = 0
        self._cumulative_loss_cc: int = 0
        self._settled_ids: set[str] = set()

    # --- live bankroll -------------------------------------------------------

    async def refresh(self, source: BalanceSource) -> CentiCents:
        """Poll the exchange and update the live bankroll. Returns the fresh cc.

        On a parse failure the last good bankroll is LEFT UNCHANGED and its
        staleness clock keeps running (a bad poll must not overwrite a good
        reading, and must not reset the freshness stamp) — after
        ``stale_after_s`` the frozen value goes stale and every cap fails closed.
        """
        payload = await source.get_balance()
        bankroll = _parse_balance_cc(payload)
        self._bankroll_cc = bankroll
        self._last_poll_ns = self._clock.monotonic_ns()
        return bankroll

    @property
    def is_stale(self) -> bool:
        """True when there is no bankroll yet, or the last good poll is older
        than ``stale_after_s`` (fail-closed sentinel for the caps)."""
        if self._bankroll_cc is None or self._last_poll_ns is None:
            return True
        age_s = (self._clock.monotonic_ns() - self._last_poll_ns) / 1e9
        return age_s > self._stale_after_s

    @property
    def bankroll_cc(self) -> CentiCents:
        """The authoritative live bankroll. Raises ``StaleBalanceError`` when
        stale — callers that scale by bankroll MUST treat that as a breach
        (never substitute a default). Use ``is_stale`` to branch first."""
        if self.is_stale:
            raise StaleBalanceError(
                "bankroll reading is stale or absent — %-of-bankroll caps must "
                "fail closed (poll get_balance)"
            )
        assert self._bankroll_cc is not None  # guarded by is_stale
        return self._bankroll_cc

    def bankroll_cc_or_none(self) -> CentiCents | None:
        """Non-raising accessor for display/logging: the live bankroll, or None
        when stale."""
        return None if self.is_stale else self._bankroll_cc

    # --- realized-P&L ledger -------------------------------------------------

    def apply_settlement(self, settlement: Settlement) -> int:
        """Advance the realized ledger by one settled position. Idempotent per
        ``position_id`` (a replayed settlement message is a no-op). Returns the
        realized cc delta this settlement contributed (0 on a duplicate).

        Economic result, in integer cc:
          LONG NO, MISSED (settles NO):  +($1 x contracts) - premium_paid   (win)
          LONG NO, HIT    (settles YES): -premium_paid                      (loss)
          LONG YES (defensive; not sell-only): mirror — HIT wins, MISS loses.
        Sign of "which side wins on a NO-settle" comes from Conventions, not a
        hardcode: ``combo_no_pays_complement`` must be verified True for a NO
        credit (it is, since 2026-07-10).
        """
        if settlement.position_id in self._settled_ids:
            return 0
        self._settled_ids.add(settlement.position_id)

        contracts = int(settlement.contracts)
        premium_paid_cc = contracts * int(settlement.entry_price_cc) // 100
        payout_cc = contracts * CC_PER_DOLLAR // 100  # $1/contract if our side pays

        if settlement.our_side is Side.NO:
            # A NO credit requires the verified complement convention.
            if not self._conventions.combo_no_pays_complement:
                raise StaleBalanceError(
                    "NO settlement credit requires combo_no_pays_complement "
                    "verified True (Conventions) — refusing to book an unverified "
                    "settlement"
                )
            our_side_paid = not settlement.settled_yes  # NO pays when it MISSES
        else:
            our_side_paid = settlement.settled_yes      # YES pays when it HITS

        realized_cc = (payout_cc if our_side_paid else 0) - premium_paid_cc
        self._realized_pnl_cc += realized_cc
        if realized_cc < 0:
            self._cumulative_loss_cc += -realized_cc

        log.info(
            "settlement_booked",
            position_id=settlement.position_id,
            our_side=str(settlement.our_side),
            settled_yes=settlement.settled_yes,
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
    def settled_count(self) -> int:
        return len(self._settled_ids)
