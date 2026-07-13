"""Settlement source + handler: the live wiring that makes the realized-P&L
ledger and the exchange-first reconciliation ACTIVE (RISK_BUILD_PLAN Phase 6;
code audit 2026-07-13 §3 "Realized-P&L ledger — zero callers").

The dead path this closes: ``balance.apply_settlement`` / ``lifecycle
.record_realized_pnl`` / ``lifecycle.reconcile_combo_settlement`` had ZERO live
callers — nothing constructed a ``Settlement`` from the exchange, so realized
P&L stayed 0 forever and no fill ever reconciled cash/fee/sign to the cent. This
module is the POLLER that lands settlements: for each combo position we HOLD that
the exchange reports settled, it

  1. constructs a ``Settlement`` from the REAL fill (our_side / contracts /
     entry_price from the ``OpenPosition``; V, revenue, fee from the exchange
     settlement row), books it via ``balance_tracker.apply_settlement`` (realized
     P&L + fee, NO pays contracts·(1−V));
  2. feeds the same realized delta into ``lifecycle.record_realized_pnl`` so the
     ENFORCED daily-loss cap sees realized P&L (its realized half was永 0);
  3. RECONCILES predicted vs the exchange ledger TO THE CENT and HALTs
     ``HALT_RECONCILIATION_MISMATCH`` on any mismatch (quiet-failure defense #3),
     plus the farmed settle-YES tripwire via ``reconcile_combo_settlement``.

Fail-closed everywhere (CLAUDE.md hard rule 6, defense #2):
- A settlement whose value/convention is UNKNOWN (``market_result`` unreadable,
  ``value`` absent on a scalar, ``combo_no_pays_complement`` not verified for a NO
  credit) HALTs ``HALT_RECONCILIATION_MISMATCH`` — it NEVER silently books 0.
- Idempotent per ``position_id``: a re-polled settlement is a no-op (the ledger's
  own ``_settled_ids`` guard plus this module's ``_reconciled`` set).

Money is integer centi-cents / Fractions only; no binary-float money. Secrets
never touch this module. The poller is a sibling of ``_balance_loop`` /
``_status_loop`` in ``ops/quote_app.py`` and only runs in paper/quote mode.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from combomaker.core.money import (
    ZERO,
    CentiCents,
    MoneyParseError,
    cc_from_dollars_str,
)
from combomaker.core.reasons import ReasonCode
from combomaker.ops.logging import get_logger
from combomaker.risk.balance import BalanceTracker, Settlement
from combomaker.risk.exposure import ExposureBook, OpenPosition
from combomaker.risk.killswitch import KillSwitch

log = get_logger(__name__)

JsonDict = dict[str, Any]

# Kalshi settlement `revenue` and `value` are int CENTS on the wire
# (docs/api-notes/index-scan.md §portfolio settlements). 1 cent = 100 cc.
CC_PER_CENT = 100


class SettlementSource(Protocol):
    """The one REST method the poller needs. ``KalshiRestClient`` satisfies it;
    tests pass a fake so no live credentials are ever required."""

    async def get_settlements(self, **params: str | int) -> JsonDict: ...


class RecordsRealizedPnl(Protocol):
    """The lifecycle slice the handler feeds: the realized-P&L sink the ENFORCED
    daily-loss cap reads, plus the farmed settle-YES tripwire."""

    def record_realized_pnl(self, delta_cc: int) -> None: ...

    async def reconcile_combo_settlement(
        self,
        combo_ticker: str,
        *,
        settled_yes: bool,
        settled_value: float | None = None,
        expected_revenue_cc: int | None = None,
    ) -> None: ...


class SettlementReconcileError(ValueError):
    """A settlement row could not be read/reconciled exactly — fail closed."""


@dataclass(frozen=True, slots=True)
class ParsedSettlement:
    """The exchange's settlement row, parsed to exact integers.

    ``settled_value`` V ∈ [0,1] is the combo's realized YES value — the payout
    per YES contract (``value`` cents ÷ 100 dollars). ``revenue_cc`` is the gross
    settlement credit the exchange booked (int cents ×100). ``fee_cc`` is the
    settlement fee (``fee_cost`` dollars). All exact; any doubt raises."""

    ticker: str
    market_result: str  # "yes" | "no" | "scalar"
    settled_value: float
    revenue_cc: CentiCents
    fee_cc: CentiCents


def _value_to_settled_value(value_cents: int) -> float:
    """Payout per YES contract (int cents) → V ∈ [0,1] dollars. $1 binary YES =
    100¢ → 1.0; NO = 0¢ → 0.0; a scalar 43¢ → 0.43. Out of [0,100]¢ is a bad row
    (fail closed)."""
    if not 0 <= value_cents <= 100:
        raise SettlementReconcileError(
            f"settlement value {value_cents}¢ out of [0,100]¢ per YES contract"
        )
    return value_cents / 100.0


def parse_settlement(row: JsonDict) -> ParsedSettlement:
    """Parse one exchange settlement row to exact integers, fail-closed.

    ``market_result`` (yes/no/scalar) is authoritative for the binary V; a
    ``scalar`` result REQUIRES the ``value`` field (payout per YES contract in
    cents) — absent, we HALT rather than guess a fractional value (defense #2).
    When ``value`` is present for a binary result it must agree with the result,
    else the row is internally inconsistent (fail closed).
    """
    ticker = str(row.get("ticker") or "")
    if not ticker:
        raise SettlementReconcileError("settlement row missing ticker")
    result = str(row.get("market_result") or "")
    if result not in ("yes", "no", "scalar"):
        raise SettlementReconcileError(
            f"settlement {ticker}: unreadable market_result {result!r} "
            "(yes|no|scalar) — UNKNOWN settlement, refusing to book"
        )

    raw_value = row.get("value")
    value_present = raw_value is not None
    if value_present:
        if not isinstance(raw_value, int) or isinstance(raw_value, bool):
            raise SettlementReconcileError(
                f"settlement {ticker}: value is not int cents: {raw_value!r}"
            )
        v = _value_to_settled_value(raw_value)
    elif result == "yes":
        v = 1.0
    elif result == "no":
        v = 0.0
    else:  # scalar with no value ⇒ cannot know the fractional payout
        raise SettlementReconcileError(
            f"settlement {ticker}: scalar result with no `value` — the fractional "
            "payout is UNKNOWN; refusing to book (defense #2)"
        )

    # Cross-check a present value against a binary result (internal consistency).
    if value_present and result in ("yes", "no"):
        want = 1.0 if result == "yes" else 0.0
        if v != want:
            raise SettlementReconcileError(
                f"settlement {ticker}: market_result={result} but value implies "
                f"V={v} (expected {want}) — inconsistent row, fail closed"
            )

    revenue_cc = _parse_cents_field(row, "revenue", ticker)
    fee_cc = _parse_dollars_field(row, "fee_cost", ticker)
    return ParsedSettlement(
        ticker=ticker,
        market_result=result,
        settled_value=v,
        revenue_cc=revenue_cc,
        fee_cc=fee_cc,
    )


def _parse_cents_field(row: JsonDict, key: str, ticker: str) -> CentiCents:
    """Int-cents wire field → cc (×100). Missing/non-int is a bad row."""
    raw = row.get(key)
    if raw is None:
        raise SettlementReconcileError(f"settlement {ticker}: missing {key} (int cents)")
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise SettlementReconcileError(f"settlement {ticker}: {key} not int cents: {raw!r}")
    return CentiCents(raw * CC_PER_CENT)


def _parse_dollars_field(row: JsonDict, key: str, ticker: str) -> CentiCents:
    """Fixed-point ``*_dollars`` string field → cc. Absent ⇒ $0 (many rows omit a
    zero fee), but a PRESENT unparseable value raises (never guess)."""
    raw = row.get(key)
    if raw is None:
        return ZERO
    try:
        return cc_from_dollars_str(str(raw))
    except MoneyParseError as exc:
        raise SettlementReconcileError(
            f"settlement {ticker}: bad {key} {raw!r}: {exc}"
        ) from exc


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    position_id: str
    combo_ticker: str
    realized_cc: int
    booked: bool  # False on a duplicate (already reconciled)


class SettlementHandler:
    """Books + reconciles settled positions we HOLD. Owns no loop — the poller
    drives it, and tests call ``handle_settlements`` directly against fakes.

    Idempotent per position id (its own ``_reconciled`` set plus the ledger's
    ``_settled_ids``): a re-polled settlement is a no-op. Fail-closed: an
    unreadable/inconsistent settlement, or a to-the-cent predicted-vs-ledger
    mismatch, HALTs ``HALT_RECONCILIATION_MISMATCH`` (defense #3) rather than
    booking a convenient 0.
    """

    def __init__(
        self,
        *,
        exposure: ExposureBook,
        balance_tracker: BalanceTracker,
        lifecycle: RecordsRealizedPnl,
        killswitch: KillSwitch,
    ) -> None:
        self._exposure = exposure
        self._balance = balance_tracker
        self._lifecycle = lifecycle
        self._killswitch = killswitch
        # Position ids we have already booked+reconciled (idempotency backstop on
        # top of the ledger's own dedup — a re-polled settlement never double-books
        # NOR re-runs the reconcile HALT check).
        self._reconciled: set[str] = set()

    async def handle_settlements(self, rows: list[JsonDict]) -> list[ReconcileResult]:
        """Process a batch of exchange settlement rows. Returns the results for
        the rows that matched a position we hold (rows for tickers we don't hold
        are ignored — the exchange returns all settlements, we only book ours).

        A single unreadable row or a mismatch HALTs immediately; already-halted
        ⇒ we stop (cancel-all already ran)."""
        results: list[ReconcileResult] = []
        for row in rows:
            if self._killswitch.halted:
                break
            result = await self._handle_one(row)
            if result is not None:
                results.append(result)
        return results

    async def _handle_one(self, row: JsonDict) -> ReconcileResult | None:
        try:
            parsed = parse_settlement(row)
        except SettlementReconcileError as exc:
            # An UNKNOWN/inconsistent settlement is exactly the quiet-failure
            # species defense #3 exists for: HALT, never skip.
            await self._killswitch.halt(
                ReasonCode.HALT_RECONCILIATION_MISMATCH,
                f"unreadable settlement row: {exc}",
            )
            return None

        positions = [
            pos
            for pos in self._exposure.positions.values()
            if pos.combo_ticker == parsed.ticker
        ]
        if not positions:
            return None  # a settlement for a market we don't hold — not ours

        # A combo market yields ONE settlement row; we may hold >1 position on it
        # (e.g. re-quoted). Reconcile the ledger figures against the SUM of our
        # positions on this ticker (revenue/fee are per-market aggregates).
        return await self._reconcile_positions(parsed, positions)

    async def _reconcile_positions(
        self, parsed: ParsedSettlement, positions: list[OpenPosition]
    ) -> ReconcileResult | None:
        # FULL to-the-cent reconcile + the farmed settle-YES tripwire live in ONE
        # place — the lifecycle's reconcile_combo_settlement (it owns the exposure
        # book + killswitch). Reconcile FIRST (predicted gross credit vs the
        # exchange revenue): a settlement whose sign/value/convention is wrong must
        # HALT before we book a wrong figure into the ledger (defense #3).
        settled_yes = parsed.settled_value >= 1.0
        await self._lifecycle.reconcile_combo_settlement(
            parsed.ticker,
            settled_yes=settled_yes,
            settled_value=parsed.settled_value,
            expected_revenue_cc=int(parsed.revenue_cc),
        )
        if self._killswitch.halted:
            return None  # a mismatch / farm tripwire halted (cancel-all ran)

        total_realized_cc = 0
        any_new = False
        primary_id = positions[0].position_id
        for pos in positions:
            if pos.position_id in self._reconciled:
                continue  # idempotent: already booked this position
            any_new = True
            settlement = self._build_settlement(pos, parsed)
            try:
                realized_cc = self._balance.apply_settlement(settlement)
            except Exception as exc:
                # apply_settlement fails closed (e.g. NO credit but the
                # complement convention is unverified) — that is an UNKNOWN
                # settlement we must not book: HALT (defense #2/#3).
                await self._killswitch.halt(
                    ReasonCode.HALT_RECONCILIATION_MISMATCH,
                    f"refused to book settlement for {pos.position_id}: {exc}",
                )
                return None
            self._reconciled.add(pos.position_id)
            # Feed the ENFORCED daily-loss cap's realized half.
            self._lifecycle.record_realized_pnl(realized_cc)
            total_realized_cc += realized_cc
            log.info(
                "settlement_reconciled",
                position_id=pos.position_id,
                combo_ticker=parsed.ticker,
                market_result=parsed.market_result,
                settled_value=parsed.settled_value,
                realized_cc=realized_cc,
            )

        return ReconcileResult(
            position_id=primary_id,
            combo_ticker=parsed.ticker,
            realized_cc=total_realized_cc,
            booked=any_new,
        )

    def _build_settlement(
        self, pos: OpenPosition, parsed: ParsedSettlement
    ) -> Settlement:
        """Construct a ``Settlement`` from the REAL fill (our side/contracts/entry
        off the position) + the exchange's V + a per-position share of the
        settlement fee. The fee is split by contract share so a multi-position
        market's fees sum to the exchange's ``fee_cost`` to the cent."""
        fee_cc = self._fee_share_cc(pos, parsed)
        return Settlement(
            position_id=pos.position_id,
            our_side=pos.our_side,
            contracts=pos.contracts,
            entry_price_cc=pos.entry_price_cc,
            settled_value=parsed.settled_value,
            fee_cc=fee_cc,
        )

    def _fee_share_cc(self, pos: OpenPosition, parsed: ParsedSettlement) -> CentiCents:
        """This position's share of the market's settlement fee, by contract
        weight (exact integer split). $0 for our combo maker fills today."""
        if int(parsed.fee_cc) == 0:
            return ZERO
        total_ct = sum(
            int(p.contracts)
            for p in self._exposure.positions.values()
            if p.combo_ticker == parsed.ticker
        )
        if total_ct <= 0:
            return ZERO
        return CentiCents(int(parsed.fee_cc) * int(pos.contracts) // total_ct)


class SettlementPoller:
    """Async loop that polls ``GET /portfolio/settlements`` and drives the
    handler. Sibling of ``_balance_loop`` / ``_status_loop`` in the QuoteApp.

    Only settled markets since the last seen ``settled_time`` are new, but the
    handler is idempotent per position so a wider poll is safe; we page to
    exhaustion each pass and let the handler ignore rows for tickers we don't
    hold. A failed poll simply retries next interval (the handler HALTs on a real
    mismatch, not on a transient REST error)."""

    def __init__(
        self,
        *,
        source: SettlementSource,
        handler: SettlementHandler,
        poll_interval_s: float,
        page_limit: int = 200,
        max_pages: int = 50,
    ) -> None:
        self._source = source
        self._handler = handler
        self._poll_interval_s = poll_interval_s
        self._page_limit = page_limit
        self._max_pages = max_pages

    async def poll_once(self) -> list[ReconcileResult]:
        """One poll pass: page settlements to exhaustion, hand them to the
        handler. Returns the reconcile results (booked positions)."""
        rows: list[JsonDict] = []
        cursor = ""
        for _ in range(self._max_pages):
            params: dict[str, str | int] = {"limit": self._page_limit}
            if cursor:
                params["cursor"] = cursor
            payload = await self._source.get_settlements(**params)
            rows.extend(payload.get("settlements", []) or [])
            cursor = str(payload.get("cursor") or "")
            if not cursor:
                break
        if not rows:
            return []
        return await self._handler.handle_settlements(rows)

    async def run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # transient REST error ⇒ retry next interval
                log.warning("settlement_poll_failed", error=repr(exc))
            await asyncio.sleep(self._poll_interval_s)
