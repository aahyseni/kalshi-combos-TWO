"""Settled-leg marginal resolution (live outage fix, 2026-07-18 evening).

The failure this closes: FRAENG finished and its leg markets settled — their
order books left the feed — while the book still held CROSS-GAME combos with
FRAENG legs (open until the ESPARG legs resolve). The book-risk model requires
a marginal for EVERY risk-modeled leg; the settled legs' marginals were missing
⇒ ``BookModel.unknown`` ⇒ every ``BookRiskSnapshot`` unusable ⇒ the
portfolio-CVaR cap failed CLOSED on every quote (``book_risk_unusable``; 0
quotes for hours). But a settled leg is not UNKNOWN — its probability is a
graded FACT: 1.0 if the market settled YES, 0.0 if NO.

This module resolves that fact from the exchange REST market object and caches
it permanently, OFF the hot path:

- ``resolved(ticker)`` — sync, in-memory cache read only (hot-path safe). The
  lifecycle's marginal provider consults it ONLY after the feed has no valid
  book for the ticker (feed first, settled-cache second, else UNKNOWN).
- ``note_missing(ticker)`` — sync registration of a candidate for resolution
  (the lifecycle calls it for COMMITTED-position legs whose feed book is gone).
- ``resolve_pending()`` — async, bounded REST fetch pass driven from the
  maintenance tick (never inside quote evaluation).

Field semantics (verified against the LIVE https://docs.kalshi.com/openapi.yaml
2026-07-18 + docs/api-notes/index-scan.md §5/§10 — hard rule 4):

- ``GET /markets/{ticker}`` → ``market.result``: enum ``yes|no|scalar|''`` —
  empty until the outcome is determined; ``yes``/``no`` is the exchange-graded
  binary outcome.
- ``market.status``: enum ``initialized|inactive|active|closed|determined|
  disputed|amended|finalized``. ``determined`` = outcome graded (the market
  settles ``settlement_timer_seconds`` later); ``finalized`` = settled. A
  yes/no ``result`` is accepted as FACT only under ``determined``/``finalized``
  (a graded result is exactly the "mathematically locked" state — the payout
  can no longer change with play). ``closed`` (game over, not yet graded) stays
  UNKNOWN and is retried: we never infer an outcome from scores or feeds.
  ``disputed``/``amended`` are NOT accepted (a result under dispute/amendment
  is not a settled fact yet — retried until finalized; fail-closed).
- ``market.settlement_value_dollars`` (nullable, "Only filled after
  determination"): when present it must AGREE with the binary result
  (yes ⇒ 1, no ⇒ 0 per $1 contract) or the row is internally inconsistent and
  is refused (never cached — the same cross-check ``risk/settlement.py``
  applies to the portfolio settlements feed).

Fail-closed everywhere (hard rule 6, quiet-failure defense #2): a fetch error,
a non-graded status, a scalar result, or an inconsistent row NEVER resolves —
the leg stays UNKNOWN (snapshot unusable, no-quote) and non-terminal states are
retried on a backoff. A settlement never changes, so a graded result is cached
permanently and fetched exactly once.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from combomaker.core.clock import Clock
from combomaker.ops.logging import get_logger

log = get_logger(__name__)

JsonDict = dict[str, Any]

# Statuses under which the exchange's `result` is a graded, immutable FACT.
GRADED_STATUSES: frozenset[str] = frozenset({"determined", "finalized"})

# Statuses meaning "this is a LIVE market — the order-book feed owns it". A
# pending ticker seen in one of these is dropped (no retry loop): the feed will
# carry its marginal, and if its book later dies while we still hold the leg,
# the lifecycle re-notes it and resolution starts over.
LIVE_STATUSES: frozenset[str] = frozenset({"initialized", "inactive", "active"})


class MarketSource(Protocol):
    """The one REST method resolution needs (public, no auth).
    ``KalshiRestClient`` satisfies it; tests pass a fake."""

    async def get_market(self, ticker: str) -> JsonDict: ...


@dataclass(frozen=True, slots=True)
class SettledResult:
    """One permanently-cached graded outcome. ``marginal`` is the leg's exact
    P(YES): 1.0 for result ``yes``, 0.0 for ``no``."""

    ticker: str
    result: str  # "yes" | "no"
    marginal: float  # 1.0 | 0.0
    status: str  # exchange status at resolution time (determined|finalized)


class SettledMarginalResolver:
    """Permanent cache of exchange-graded 0/1 leg outcomes + the bounded
    off-hot-path fetcher that fills it.

    Single-loop discipline: every method is called from the one asyncio loop
    (sync reads from the hot path, the fetch pass from a maintenance-tick
    task), so no locking is needed. ``resolve_pending`` NEVER raises — any
    per-ticker error is logged and retried on the backoff."""

    def __init__(
        self,
        source: MarketSource,
        clock: Clock,
        *,
        retry_after_s: float = 30.0,
        fetch_budget_per_pass: int = 5,
        max_pending: int = 512,
    ) -> None:
        self._source = source
        self._clock = clock
        self._retry_after_ns = int(retry_after_s * 1e9)
        self._fetch_budget_per_pass = fetch_budget_per_pass
        self._max_pending = max_pending
        # ticker → graded result. Permanent: a settlement never changes.
        self._results: dict[str, SettledResult] = {}
        # ticker → earliest monotonic ns the next fetch attempt may run.
        self._pending: dict[str, int] = {}
        # Tickers that can NEVER resolve to a 0/1 marginal (scalar result /
        # inconsistent row). Remembered so they are not refetched forever;
        # their legs stay UNKNOWN (fail-closed).
        self._unresolvable: set[str] = set()
        # ticker → recheck floor (monotonic ns) set when a fetch found the
        # market LIVE. A held leg's book can flicker invalid during a WS
        # resync; without the floor every flicker would re-note + refetch on
        # the next pass. With it, a re-note of a known-live ticker is deferred
        # to the floor — at most one fetch per backoff window per ticker.
        self._live_floor: dict[str, int] = {}

    # ------------------------------------------------------------- hot path

    def resolved(self, market_ticker: str) -> float | None:
        """The cached graded marginal (exactly 0.0 or 1.0), or None. Pure
        in-memory read — hot-path safe, never touches the network."""
        cached = self._results.get(market_ticker)
        return None if cached is None else cached.marginal

    def note_missing(self, market_ticker: str) -> None:
        """Register a ticker whose feed book is gone as a resolution candidate.
        Cheap and sync (dict ops only). Already-resolved / known-unresolvable /
        already-pending tickers are no-ops; the pending set is bounded so an
        anomalous flood can never grow memory or the fetch queue unboundedly."""
        if (
            market_ticker in self._results
            or market_ticker in self._unresolvable
            or market_ticker in self._pending
        ):
            return
        if len(self._pending) >= self._max_pending:
            log.warning(
                "settled_pending_overflow",
                ticker=market_ticker,
                max_pending=self._max_pending,
            )
            return
        # Due immediately, unless a recent fetch proved the market LIVE (then
        # deferred to the recheck floor — flicker-refetch protection).
        self._pending[market_ticker] = self._live_floor.get(market_ticker, 0)

    # ------------------------------------------------------ maintenance tick

    @property
    def has_due_pending(self) -> bool:
        """True iff some pending ticker's backoff has elapsed (cheap check the
        maintenance tick uses before launching a fetch task)."""
        now = self._clock.monotonic_ns()
        return any(due <= now for due in self._pending.values())

    async def resolve_pending(self) -> int:
        """One bounded fetch pass over the due pending tickers. Returns the
        number of tickers RESOLVED this pass. Never raises: a per-ticker fetch
        error logs a warning and retries after the backoff."""
        now = self._clock.monotonic_ns()
        due = [t for t, due_ns in self._pending.items() if due_ns <= now]
        resolved = 0
        for ticker in due[: self._fetch_budget_per_pass]:
            try:
                payload = await self._source.get_market(ticker)
            except Exception as exc:
                log.warning(
                    "settled_fetch_failed", ticker=ticker, error=repr(exc)
                )
                self._pending[ticker] = self._clock.monotonic_ns() + self._retry_after_ns
                continue
            if self._ingest(ticker, payload):
                resolved += 1
        return resolved

    def _ingest(self, ticker: str, payload: JsonDict) -> bool:
        """Classify one GetMarket payload; True iff the ticker RESOLVED."""
        market = payload.get("market", payload)
        if not isinstance(market, dict):
            log.warning("settled_payload_unreadable", ticker=ticker)
            self._pending[ticker] = self._clock.monotonic_ns() + self._retry_after_ns
            return False
        status = str(market.get("status") or "")
        result = str(market.get("result") or "")

        if status in LIVE_STATUSES:
            # A live market — the feed owns it; not a settlement candidate.
            # Arm the recheck floor so a book-flicker re-note within the
            # backoff window cannot trigger another fetch.
            self._pending.pop(ticker, None)
            self._live_floor[ticker] = (
                self._clock.monotonic_ns() + self._retry_after_ns
            )
            return False

        if result == "scalar":
            # A scalar outcome is never a 0/1 leg fact — permanently
            # unresolvable here; the leg stays UNKNOWN (fail-closed).
            log.warning("settled_scalar_unresolvable", ticker=ticker, status=status)
            self._pending.pop(ticker, None)
            self._unresolvable.add(ticker)
            return False

        if result in ("yes", "no") and status in GRADED_STATUSES:
            marginal = 1.0 if result == "yes" else 0.0
            if not self._settlement_value_consistent(ticker, market, result):
                # Internally inconsistent row: refuse to cache (fail-closed).
                self._pending.pop(ticker, None)
                self._unresolvable.add(ticker)
                return False
            self._results[ticker] = SettledResult(
                ticker=ticker, result=result, marginal=marginal, status=status
            )
            self._pending.pop(ticker, None)
            log.info(
                "settled_marginal_resolved",
                ticker=ticker,
                result=result,
                status=status,
                marginal=marginal,
            )
            return True

        # Everything else — `closed` (game over, not yet graded), a graded
        # status with an empty result, `disputed`/`amended`, or an unknown
        # status string — is NOT a fact yet: stay UNKNOWN, retry on backoff.
        self._pending[ticker] = self._clock.monotonic_ns() + self._retry_after_ns
        return False

    @staticmethod
    def _settlement_value_consistent(
        ticker: str, market: JsonDict, result: str
    ) -> bool:
        """Cross-check ``settlement_value_dollars`` (when present) against the
        binary result: yes ⇒ $1, no ⇒ $0 per contract. Absent/None ⇒ trust the
        result alone (the field is optional). Unparseable or disagreeing ⇒
        False (refuse — the same internal-consistency discipline
        ``risk/settlement.py:parse_settlement`` applies)."""
        raw = market.get("settlement_value_dollars")
        if raw is None:
            return True
        try:
            value = Decimal(str(raw))
        except InvalidOperation:
            log.error(
                "settled_value_unparseable", ticker=ticker, raw=str(raw)
            )
            return False
        want = Decimal(1) if result == "yes" else Decimal(0)
        if value != want:
            log.error(
                "settled_value_inconsistent",
                ticker=ticker,
                result=result,
                settlement_value_dollars=str(raw),
            )
            return False
        return True
