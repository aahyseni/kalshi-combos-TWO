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
from datetime import UTC, datetime
from typing import Any, Protocol

from combomaker.core.conventions import Side
from combomaker.core.money import (
    ZERO,
    CentiCents,
    MoneyParseError,
    fee_cc_from_dollars_str,
)
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.ops.logging import get_logger
from combomaker.risk.balance import (
    BalanceTracker,
    Settlement,
    settlement_payout_per_contract_cc,
    settlement_realized_cc,
)
from combomaker.risk.exposure import ExposureBook, OpenPosition, stable_ledger_key
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
    """Fixed-point ``*_dollars`` string FEE field → cc. Absent ⇒ $0 (many rows omit
    a zero fee). A present value is booked at cc granularity, rounding UP a sub-cc
    fee (Kalshi can charge one, e.g. ``"0.000080"`` = 0.8 cc — observed live on a
    combo settlement) so we never understate a cost we paid; a genuinely
    unparseable value still raises (never guess). Used only for ``fee_cost``."""
    raw = row.get(key)
    if raw is None:
        return ZERO
    try:
        return fee_cc_from_dollars_str(str(raw))
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


class SettlementLedger(Protocol):
    """The durable-ledger surface the handler needs (2026-07-26). Structural so
    ``risk.settlement`` never imports ``ops.persistence``; the live wiring
    passes a ``Store``-backed adapter, tests pass a stub, None disables it.

    The stable-identity fields (``leg_set_hash`` / ``combo_ticker`` /
    ``our_side`` / ``contracts_centi``) are the DURABLE key: ``position_id`` is
    volatile — a restart re-mints it (``rehydrate:<ticker>``), so a ledger
    keyed on it alone can never match an open row written before the restart."""

    def record_settled(
        self,
        *,
        position_id: str,
        settled_value: float,
        realized_pnl_cc: int,
        settlement_fee_cc: int,
        leg_set_hash: str | None = None,
        combo_ticker: str | None = None,
        our_side: str | None = None,
        contracts_centi: int | None = None,
    ) -> None: ...


class OrphanLedgerReconciler(Protocol):
    """The ledger surface the ORPHAN-ROW reconciliation needs (2026-07-27).

    Separate from ``SettlementLedger`` on purpose: it is additive and optional
    (None ⇒ the pre-fix behaviour exactly), so no existing wiring or stub has
    to grow methods it never calls."""

    async def open_ledger_tickers(self) -> set[str]: ...

    async def open_ledger_rows_for_ticker(
        self, combo_ticker: str
    ) -> list[JsonDict]: ...

    async def close_ledger_row_settled(
        self,
        position_id: str,
        *,
        settled_value: float,
        realized_pnl_cc: int,
        settlement_fee_cc: int,
        reconciled_at: str,
    ) -> str | None: ...


def _settled_at_iso(row: JsonDict) -> str | None:
    """The exchange's own ``settled_time`` normalised to the ledger's stamp
    format (tz-aware UTC isoformat), or None when it cannot be read.

    Attribution matters: ``day_realized_pnl_cc`` buckets on ``reconciled_at``,
    so a row closed today for a combo that settled last night must carry LAST
    NIGHT's stamp. Unreadable ⇒ None ⇒ the caller REFUSES to close the row
    (fail-closed: a wrong day is a corrupted anchor, and the row keeps its
    exposure meaning until we can attribute it)."""
    raw = row.get("settled_time")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


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
        ledger: SettlementLedger | None = None,
        orphan_ledger: OrphanLedgerReconciler | None = None,
    ) -> None:
        self._exposure = exposure
        self._balance = balance_tracker
        self._lifecycle = lifecycle
        self._killswitch = killswitch
        # DURABLE SETTLEMENT LEDGER (2026-07-26). Until now every settlement
        # was booked ONLY into the in-memory realized accumulator, so
        # ``position_ledger`` — the table that carries realized P&L across
        # restarts and is the ONLY local record of how a combo settled — had
        # no live writer at all. That made ``p_night``'s day-anchored seed a
        # silent no-op AND made settlement calibration ("do our 4-6 leg K
        # combos hit at the rate we price?") unanswerable from local data.
        # None ⇒ ledger writes are skipped (tests/backtests), never an error.
        self._ledger = ledger
        # ORPHAN-ROW RECONCILIATION (2026-07-27). None ⇒ exactly the pre-fix
        # behaviour (a settlement for a ticker the exposure book does not hold
        # is dropped and its open ledger row lives forever).
        self._orphan_ledger = orphan_ledger
        # Position ids we have already booked+reconciled (idempotency backstop on
        # top of the ledger's own dedup — a re-polled settlement never double-books
        # NOR re-runs the reconcile HALT check).
        self._reconciled: set[str] = set()
        # Tickers whose orphan rows this process has already reconciled (or
        # refused as ambiguous) — the poller re-pages the SAME settlement rows
        # every 30 s and neither the DB write nor the alarm should repeat.
        self._orphans_seen: set[str] = set()
        # Tickers that currently HAVE an open ledger row, refreshed ONCE per
        # settlement batch (one indexed read) instead of once per settlement
        # row. The poller pages the account's WHOLE settlement history every
        # 30 s — thousands of rows, ~130 of which can possibly have an open row
        # — so without this pre-filter the reconciliation would issue one DB
        # round-trip per historical settlement on the first pass of every
        # process. None ⇒ the batch read failed ⇒ fall back to the per-ticker
        # read (fail-OPEN toward doing the work, never toward skipping it).
        self._open_ledger_tickers: set[str] | None = None
        # OBSERVABLE counters (the divergence sweep is the other half).
        self.orphan_rows_closed = 0
        self.orphan_rows_ambiguous = 0

    async def handle_settlements(self, rows: list[JsonDict]) -> list[ReconcileResult]:
        """Process a batch of exchange settlement rows. Returns the results for
        the rows that matched a position we hold (rows for tickers we don't hold
        are ignored — the exchange returns all settlements, we only book ours).

        A single unreadable row or a mismatch HALTs immediately; already-halted
        ⇒ we stop (cancel-all already ran)."""
        # P1-7 MUTEX-METADATA SETTLEMENT TRIPWIRE. Audit the WHOLE batch for a
        # violation of the mutual-exclusivity our risk netting trusted, BEFORE any
        # settled position is booked+removed (the map is derived from held legs).
        # If ≥2 distinct outcome markets of one explicit-True ME event both settled
        # YES, the exclusivity our loss/directional netting credited was FALSE — a
        # classification/metadata failure that UNDER-stated risk → HALT (defense
        # #2/#3), never a log. A metadata-read error must fail closed like the
        # farmed tripwire, so this runs before we touch the ledger.
        await self._audit_mutex_metadata(rows)
        # ONE indexed read for the whole batch (see ``_open_ledger_tickers``).
        await self._refresh_open_ledger_tickers()
        results: list[ReconcileResult] = []
        for row in rows:
            if self._killswitch.halted:
                break
            result = await self._handle_one(row)
            if result is not None:
                results.append(result)
        return results

    async def _audit_mutex_metadata(self, rows: list[JsonDict]) -> None:
        """Collect the market tickers this batch settled YES and HALT if any
        explicit-True ME event we netted on saw ≥2 of its outcome markets settle
        YES (the exclusivity was false → our netting under-stated risk)."""
        settled_yes: list[str] = []
        for row in rows:
            ticker = str(row.get("ticker") or "")
            if not ticker:
                continue
            # A market "settled YES" iff its binary result is yes. A scalar result
            # is a graded payout, not a mutually-exclusive YES outcome, so it never
            # participates in the exclusivity audit (the netting only ever credited
            # binary yes/no outcome markets of a RESULT event).
            if str(row.get("market_result") or "") == "yes":
                settled_yes.append(ticker)
        if not settled_yes:
            return
        violations = self._exposure.audit_mutex_settlements(settled_yes)
        if not violations:
            return
        detail = "; ".join(
            f"{event}: {sorted(markets)}" for event, markets in sorted(violations.items())
        )
        await self._killswitch.halt(
            ReasonCode.HALT_RECONCILIATION_MISMATCH,
            f"mutex-metadata tripwire: {len(violations)} netted mutually-exclusive "
            f"event(s) settled ≥2 outcome markets YES — exclusivity FALSE, risk "
            f"netting under-stated exposure ({detail})",
        )

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
            # Not in the exposure book — no live risk to book. But the DURABLE
            # ledger may still carry an OPEN row for it (the combo settled while
            # this process was down, or the boot rehydrate dropped it as
            # already-finalized), and that row can never close by itself.
            # Reconcile it from exchange truth; never a risk/cash side effect.
            await self._reconcile_orphan_ledger_rows(row, parsed)
            return None  # a settlement for a market we don't hold — not ours

        # A combo market yields ONE settlement row; we may hold >1 position on it
        # (e.g. re-quoted). Reconcile the ledger figures against the SUM of our
        # positions on this ticker (revenue/fee are per-market aggregates).
        return await self._reconcile_positions(parsed, positions)

    async def _refresh_open_ledger_tickers(self) -> None:
        """Re-read the set of tickers that currently have an OPEN ledger row —
        one indexed query per settlement batch.

        The orphan reconciliation is a needle-in-a-haystack search: the poller
        pages the account's ENTIRE settlement history every 30 s, and at most a
        few hundred of those tickers can possibly still carry an open row. This
        turns "one DB round-trip per historical settlement row" into "one per
        batch". FAIL-OPEN: any error leaves the set None, which makes the
        pre-filter a no-op and every row take the per-ticker read — the work
        still happens, it is just slower."""
        if self._orphan_ledger is None:
            return
        try:
            self._open_ledger_tickers = await self._orphan_ledger.open_ledger_tickers()
        except Exception:
            log.exception("orphan_ledger_ticker_scan_failed")
            self._open_ledger_tickers = None

    async def _reconcile_orphan_ledger_rows(
        self, row: JsonDict, parsed: ParsedSettlement
    ) -> None:
        """Close OPEN ledger rows for a settled combo the exposure book does
        not hold — the ledger-divergence repair (2026-07-27).

        WHY the rows exist: a combo that settled while the process was down is
        absent from ``get_positions`` at the next boot, so the exposure book
        never holds it, so ``_handle_one`` dropped its settlement as "not ours"
        and the open row survived forever. Measured on the live store: the
        divergence was CONSTANT within a run and stepped ONLY at restarts —
        exactly this. Same class: a boot rehydrate that drops an
        already-``finalized`` position.

        WHAT IT DOES AND DOES NOT DO. Ledger-only: it writes the settled row
        (status, V, realized, fee, and the EXCHANGE's settled_time as the
        reconciliation stamp) so p_night's realized anchor and the settlement
        calibration curve stop under-counting. It NEVER touches cash, the
        realized accumulators, the exposure book or the caps — that money moved
        on the exchange in a previous process and re-booking it into today's
        in-memory realized half would corrupt the ENFORCED daily-loss ladder.

        FAIL-CLOSED ON AMBIGUITY (never silently deletes a row that may be real
        exposure): the row is only closed when the rows' gross payout
        reconstructs the exchange's own ``revenue`` to within a cent — the same
        to-the-cent identity the live path HALTs on — and when the exchange's
        settled_time and the row's own side/quantity are readable. Anything
        else leaves the row OPEN and raises a loud alarm. Unlike the live path
        this ALARMS rather than HALTs: we hold no position, so there is no live
        risk to stop trading over, and halting the bot on a bookkeeping
        ambiguity would be the cure being worse than the disease."""
        if self._orphan_ledger is None or parsed.ticker in self._orphans_seen:
            return
        known = self._open_ledger_tickers
        if known is not None and parsed.ticker not in known:
            # Batch pre-filter: this ticker had no open ledger row when the
            # batch started. Deliberately NOT memoised in ``_orphans_seen`` —
            # the set is re-read at the top of every batch, so a row opened
            # later is picked up on the next poll instead of being suppressed
            # for the life of the process.
            return
        try:
            rows = await self._orphan_ledger.open_ledger_rows_for_ticker(parsed.ticker)
        except Exception:
            log.exception("orphan_ledger_read_failed", combo_ticker=parsed.ticker)
            return
        if not rows:
            return

        settled_at = _settled_at_iso(row)
        if settled_at is None:
            self.orphan_rows_ambiguous += len(rows)
            self._orphans_seen.add(parsed.ticker)
            log.warning(
                "settlement_orphan_row_unattributable",
                combo_ticker=parsed.ticker,
                open_rows=len(rows),
                raw_settled_time=repr(row.get("settled_time")),
                detail="the exchange settlement carries no readable "
                "settled_time — the realized P&L could only be attributed to "
                "the WRONG day, so the open ledger row is LEFT OPEN "
                "(fail-closed; never deleted)",
            )
            return

        # Rebuild each row as the Settlement it was, under the ONE payout
        # formula (risk.balance) — no second copy of the convention.
        parcels: list[tuple[str, Settlement]] = []
        total_ct = 0
        try:
            for r in rows:
                side = Side(str(r["our_side"]))
                contracts = int(r["contracts_centi"])
                if contracts <= 0:
                    raise ValueError(f"non-positive contracts {contracts}")
                total_ct += contracts
                parcels.append(
                    (
                        str(r["position_id"]),
                        Settlement(
                            position_id=str(r["position_id"]),
                            our_side=side,
                            contracts=CentiContracts(contracts),
                            entry_price_cc=CentiCents(int(r["entry_price_cc"])),
                            settled_value=parsed.settled_value,
                            fee_cc=ZERO,  # replaced by the exact share below
                        ),
                    )
                )
        except (KeyError, TypeError, ValueError) as exc:
            self.orphan_rows_ambiguous += len(rows)
            self._orphans_seen.add(parsed.ticker)
            log.warning(
                "settlement_orphan_row_unreadable",
                combo_ticker=parsed.ticker,
                open_rows=len(rows),
                error=repr(exc),
                detail="an open ledger row's side/quantity/price could not be "
                "read — refusing to close it (fail-closed; never deleted)",
            )
            return

        # TO-THE-CENT identity: Σ gross payout over these rows must reconstruct
        # the exchange's `revenue`. If it does not, these rows are NOT this
        # settlement's counterpart (partial coverage, a re-quote we already
        # booked, a convention drift) — leave them open and alarm.
        try:
            gross_cc = sum(
                int(s.contracts)
                * settlement_payout_per_contract_cc(
                    s.our_side,
                    parsed.settled_value,
                    no_pays_complement=self._balance.combo_no_pays_complement,
                )
                // 100
                for _, s in parcels
            )
        except Exception as exc:  # noqa: BLE001 — unverified convention ⇒ refuse
            self.orphan_rows_ambiguous += len(rows)
            self._orphans_seen.add(parsed.ticker)
            log.warning(
                "settlement_orphan_row_convention_refused",
                combo_ticker=parsed.ticker,
                error=repr(exc),
                detail="payout convention not verified — open ledger rows left "
                "OPEN (fail-closed)",
            )
            return
        residual_cc = abs(gross_cc - int(parsed.revenue_cc))
        if residual_cc >= CC_PER_CENT:
            self.orphan_rows_ambiguous += len(rows)
            self._orphans_seen.add(parsed.ticker)
            log.warning(
                "settlement_orphan_row_ambiguous",
                combo_ticker=parsed.ticker,
                open_rows=len(rows),
                predicted_gross_cc=gross_cc,
                exchange_revenue_cc=int(parsed.revenue_cc),
                residual_cc=residual_cc,
                settled_value=parsed.settled_value,
                detail="open ledger rows for this settled combo do not "
                "reconstruct the exchange's revenue to the cent — they may not "
                "be this settlement's counterpart, so they are LEFT OPEN "
                "(fail-closed; a row that may represent real exposure is never "
                "deleted or closed on a guess)",
            )
            return

        closed = 0
        for position_id, parcel in parcels:
            fee_share = (
                CentiCents(int(parsed.fee_cc) * int(parcel.contracts) // total_ct)
                if int(parsed.fee_cc) and total_ct > 0
                else ZERO
            )
            settlement = Settlement(
                position_id=parcel.position_id,
                our_side=parcel.our_side,
                contracts=parcel.contracts,
                entry_price_cc=parcel.entry_price_cc,
                settled_value=parsed.settled_value,
                fee_cc=fee_share,
            )
            realized_cc = settlement_realized_cc(
                settlement,
                no_pays_complement=self._balance.combo_no_pays_complement,
            )
            try:
                landed = await self._orphan_ledger.close_ledger_row_settled(
                    position_id,
                    settled_value=float(parsed.settled_value),
                    realized_pnl_cc=int(realized_cc),
                    settlement_fee_cc=int(fee_share),
                    reconciled_at=settled_at,
                )
            except Exception:
                log.exception(
                    "settlement_orphan_row_write_failed",
                    combo_ticker=parsed.ticker,
                    position_id=position_id,
                )
                return  # retry on the next poll; the row stays OPEN
            if landed is None:
                continue  # raced/already closed — idempotent
            closed += 1
            self.orphan_rows_closed += 1
            log.info(
                "settlement_orphan_row_closed",
                combo_ticker=parsed.ticker,
                position_id=position_id,
                settled_value=parsed.settled_value,
                realized_cc=int(realized_cc),
                settlement_fee_cc=int(fee_share),
                reconciled_at=settled_at,
                detail="ledger row for a combo that settled while it was not "
                "in the exposure book (settled during a process gap) closed "
                "from exchange truth — ledger-only, no cash/risk effect",
            )
        if closed:
            self._orphans_seen.add(parsed.ticker)

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
        # Build every Settlement (incl. its fee share) BEFORE the book+remove
        # loop below, while the exposure book still holds all positions on this
        # ticker — `_fee_share_cc` splits the exchange fee by contract weight over
        # the whole ticker, so it must see the full set (removing positions
        # mid-loop would shrink the denominator and break the exact fee split).
        settlements = {pos.position_id: self._build_settlement(pos, parsed) for pos in positions}
        for pos in positions:
            if pos.position_id in self._reconciled:
                continue  # idempotent: already booked this position
            any_new = True
            settlement = settlements[pos.position_id]
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
            # Settlement-receivable confirm (give-back cascade shield): the
            # exchange row for this position is now booked, so its receivable —
            # if the fact sweep noted one — drops at the first balance poll
            # whose request starts after this instant (that poll provably
            # contains the credited cash). No receivable noted ⇒ no-op. The
            # EXCHANGE-derived credit rides along (review F4): a fact-derived
            # receivable that disagrees ≥1¢ was a phony shield and drops
            # immediately instead of confirming.
            self._balance.confirm_receivable(
                pos.position_id,
                expected_credit_cc=self._exchange_credit_cc(pos, parsed),
            )
            # A settled position no longer carries live risk: drop it from the
            # exposure book so it stops counting toward the enforced
            # game/slate/gross/CVaR caps + the daily-P&L mark (else settled
            # exposure accumulates forever over a long run), and so a later
            # re-quote+re-fill of the SAME ticker does not make the reconcile
            # re-sum this already-settled position against the new settlement's
            # revenue (a false HALT_RECONCILIATION_MISMATCH). Removed only AFTER
            # apply_settlement succeeds + the id is marked reconciled, so a
            # replayed row is still an idempotent no-op.
            self._exposure.remove_position(pos.position_id)
            # Feed the ENFORCED daily-loss cap's realized half.
            self._lifecycle.record_realized_pnl(realized_cc)
            # Durable twin of the line above: the in-memory accumulator resets
            # every restart, this does not. Best-effort — a ledger failure must
            # never break settlement booking (the money path).
            if self._ledger is not None:
                try:
                    # DURABLE IDENTITY (2026-07-26): pass the restart-stable
                    # key alongside the volatile position_id. After a restart
                    # the rehydrator re-mints ids, so a position_id-only match
                    # can NEVER hit the open row this position was written
                    # under — every settled write silently vanished and the
                    # ledger covered 3.74% of the book (6 rows / $27.35 of
                    # $731.04), heading to 0% at the next restart.
                    self._ledger.record_settled(
                        position_id=pos.position_id,
                        settled_value=float(parsed.settled_value or 0.0),
                        realized_pnl_cc=int(realized_cc),
                        # The EXACT per-position share of the exchange's
                        # settlement fee (was hardcoded 0). Recorded only into
                        # the ledger's fee columns — `realized_cc` above is
                        # already NET of fees (balance.apply_settlement), and
                        # `day_realized_pnl_cc` sums realized_pnl_cc + fills
                        # fees, so this never double-counts a cost.
                        settlement_fee_cc=int(settlement.fee_cc),
                        leg_set_hash=stable_ledger_key(pos),
                        combo_ticker=pos.combo_ticker,
                        our_side=pos.our_side.value,
                        contracts_centi=int(pos.contracts),
                    )
                except Exception:
                    log.exception(
                        "settlement_ledger_write_failed",
                        position_id=pos.position_id,
                    )
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

    @staticmethod
    def _exchange_credit_cc(pos: OpenPosition, parsed: ParsedSettlement) -> int:
        """This position's gross settlement credit from the EXCHANGE's V —
        the same convention frame as the lifecycle's predicted-credit helper
        (LONG NO pays $1−V, LONG YES pays V; keep in sync with
        ``QuoteLifecycle._predicted_settlement_credit_cc``). Used to
        cross-check a fact-derived receivable at confirm time (review F4)."""
        contracts = int(pos.contracts)
        v_cc = round(parsed.settled_value * 10_000)
        per_ct = (10_000 - v_cc) if pos.our_side is Side.NO else v_cc
        return contracts * per_ct // 100

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
