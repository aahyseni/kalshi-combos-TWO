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
from combomaker.exchange.rest import ReadBudgetExhausted
from combomaker.ops.logging import get_logger

log = get_logger(__name__)

JsonDict = dict[str, Any]

# The resolver's own bounded per-pass fetch claim. Exported because the READ
# bucket's CRITICAL reserve is derived from it (``ops.quote_app``): reserve =
# this × the live-verified per-endpoint token cost, i.e. exactly one pass of
# this resolver and not a token more. Changing the pass budget therefore moves
# the reserve with it — there is no second number to keep in sync.
FETCH_BUDGET_PER_PASS = 5

# Statuses under which the exchange's `result` is a graded, immutable FACT.
GRADED_STATUSES: frozenset[str] = frozenset({"determined", "finalized"})

# Statuses meaning "this is a LIVE market — the order-book feed owns it". A
# pending ticker seen in one of these is dropped (no retry loop): the feed will
# carry its marginal, and if its book later dies while we still hold the leg,
# the lifecycle re-notes it and resolution starts over.
LIVE_STATUSES: frozenset[str] = frozenset({"initialized", "inactive", "active"})

# The recognized NON-LIVE statuses (trading over; the order book ceases to
# exist). Includes closed-but-UNGRADED and the dispute-window states: those
# stay UNKNOWN for the MARGINAL (no fact until graded), but their book leaving
# the feed is the normal close transition — knowledge ``market_no_longer_live``
# carries to the breaker exemption. An unknown status string is in NEITHER set
# (fail-closed: no exemption, keep retrying).
NON_LIVE_STATUSES: frozenset[str] = frozenset(
    {"closed", "determined", "disputed", "amended", "finalized"}
)


class MarketSource(Protocol):
    """The one REST method resolution needs (public, no auth).
    ``KalshiRestClient`` satisfies it; tests pass a fake."""

    async def get_market(self, ticker: str) -> JsonDict: ...


class Counters(Protocol):
    """The one metrics method this module uses (``ops.metrics.Metrics``
    satisfies it; None disables counting)."""

    def inc(self, name: str, by: int = 1) -> None: ...


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
        fetch_budget_per_pass: int = FETCH_BUDGET_PER_PASS,
        max_pending: int = 512,
        metrics: Counters | None = None,
    ) -> None:
        self._source = source
        self._clock = clock
        self._metrics = metrics
        self._retry_after_s = retry_after_s
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
        # Tickers whose LAST successful fetch reported a NON-LIVE exchange
        # status (closed/determined/disputed/amended/finalized) but no cached
        # 0/1 fact yet (closed-but-ungraded, disputed, scalar, …). Feeds
        # ``market_no_longer_live``: for such a ticker the order book leaving
        # the feed is NORMAL AND PERMANENT (books cease to exist at close), so
        # the marginal-jump breaker's dead-feed readability watch must not
        # read it as a feed failure (live halt 2026-07-18 02:17Z:
        # halt_marginal_jump "became unreadable (had 1.000)" on a settled
        # FRAENG leg hard-killed the bot 90s after preflight).
        self._known_non_live: set[str] = set()
        # ticker → fetch attempts while pending (popped with the pending
        # entry). Drives the per-pass PRIORITY: NEVER-FETCHED tickers are
        # fetched before backoff RETRIES, so a freshly-registered graded fact
        # lands within ~2 passes of registration no matter how many
        # ungraded/active tickers are cycling on the backoff (relight2
        # 2026-07-19: graded FRAENG facts sat unfetched for 25 minutes).
        self._attempts: dict[str, int] = {}
        # STARVATION OBSERVABILITY (2026-07-27). Counted and logged LOUD,
        # because the failure it precedes is SILENT: overnight we simply never
        # learn what settled. TWO signatures, both measured against the
        # resolver's own retry cycle so neither invents a number:
        #
        #   OVERDUE   — a pending entry's due time passed more than one cycle
        #               ago, i.e. the fetch PASS itself stopped running (a
        #               stalled maintenance tick, a pass budget swamped by
        #               backlog). Ordinary backoff is NOT this.
        #   UNSERVED  — a ticker whose reads have been FAILING for longer than
        #               one cycle (local budget refusal or exchange error). A
        #               failure re-arms the backoff, so such a ticker looks
        #               perfectly on-schedule forever: without this second
        #               signature the read-budget inversion this work exists
        #               to kill would raise no alarm at all.
        #
        # ``read_budget_deferrals`` counts LOCAL budget refusals separately
        # from exchange failures (``fetch_failures``) — the two have different
        # owners and only the first says the reserve stopped working.
        self.read_budget_deferrals = 0
        self.fetch_failures = 0
        self.starved_passes = 0
        # ticker → monotonic ns of the first FAILED attempt with no successful
        # read since (cleared by any successful GET and by ``_drop_pending``).
        self._unserved_since: dict[str, int] = {}
        # Last starvation alarm (monotonic ns). The alarm is THROTTLED to one
        # per retry cycle — the resolver's own backoff, so no new number: a
        # pass runs every few hundred ms, and an unthrottled WARNING would
        # emit thousands of identical lines per hour and bury the signal it
        # exists to raise.
        self._last_starve_alarm_ns: int | None = None

    # ------------------------------------------------------------- hot path

    def resolved(self, market_ticker: str) -> float | None:
        """The cached graded marginal (exactly 0.0 or 1.0), or None. Pure
        in-memory read — hot-path safe, never touches the network."""
        cached = self._results.get(market_ticker)
        return None if cached is None else cached.marginal

    def market_no_longer_live(self, market_ticker: str) -> bool:
        """True iff the EXCHANGE told us this market is no longer live: a
        graded 0/1 fact is cached, or the last successful fetch reported a
        non-live status (closed/determined/disputed/amended/finalized —
        including closed-but-UNGRADED and scalar markets). Pure in-memory read.

        Consumed by the marginal-jump circuit breaker's watch-exemption
        (``QuoteLifecycle.settled_watch_exempt``): for such a ticker
        "readable → unreadable" is the NORMAL, PERMANENT close transition, not
        the dead-feed signature the breaker exists to catch. Fail-closed: a
        ticker never successfully fetched (or last seen LIVE) returns False —
        the breaker keeps its full readability watch on it, so a genuinely
        dead feed on a live market still halts."""
        return (
            market_ticker in self._results
            or market_ticker in self._known_non_live
        )

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
        # Due NOW, unless a recent fetch proved the market LIVE (then deferred
        # to the recheck floor — flicker-refetch protection). The stamp is the
        # registration instant rather than 0 so "overdue" is measurable
        # (``_alarm_if_starved``): a 0 would read as overdue by the whole
        # monotonic clock the moment it was registered.
        now = self._clock.monotonic_ns()
        floor = self._live_floor.get(market_ticker, 0)
        self._pending[market_ticker] = floor if floor > now else now

    # ------------------------------------------------------ maintenance tick

    @property
    def pending_count(self) -> int:
        """How many tickers are awaiting resolution (the BACKLOG — the log's
        ``n_pending``). Read by the maintenance/report paths and by the tests
        that bound backlog clearance; never a risk input."""
        return len(self._pending)

    @property
    def has_due_pending(self) -> bool:
        """True iff some pending ticker's backoff has elapsed (cheap check the
        maintenance tick uses before launching a fetch task)."""
        now = self._clock.monotonic_ns()
        return any(due <= now for due in self._pending.values())

    async def resolve_pending(self) -> int:
        """One bounded fetch pass over the due pending tickers. Returns the
        number of tickers RESOLVED this pass. Never raises: a per-ticker fetch
        error logs a warning and retries after the backoff.

        PRIORITY (relight2 stall, 2026-07-19): NEVER-FETCHED tickers are
        fetched BEFORE backoff retries — a freshly-registered graded fact must
        land within ~2 passes of registration regardless of how many ungraded/
        active tickers are cycling on the backoff. The sort is stable, so
        insertion order is preserved within each tier.

        OBSERVABILITY (requirement from the same outage — the operator was
        blind to what the resolver knew): once per pass with a non-empty
        pending set, an INFO ``settled_resolution_pending`` line reports the
        pending/never-fetched counts and a ticker sample, and
        ``_alarm_if_starved`` raises a WARNING + counter if any resolution has
        been DUE for longer than one retry cycle (2026-07-27 — starvation must
        never again be inferable only from a 429 count in a log)."""
        now = self._clock.monotonic_ns()
        if self._pending:
            never_fetched = sum(
                1 for t in self._pending if self._attempts.get(t, 0) == 0
            )
            log.info(
                "settled_resolution_pending",
                n_pending=len(self._pending),
                n_never_fetched=never_fetched,
                sample=sorted(self._pending)[:8],
            )
            self._alarm_if_starved(now)
        due = [t for t, due_ns in self._pending.items() if due_ns <= now]
        due.sort(key=lambda t: self._attempts.get(t, 0) > 0)  # new first; stable
        resolved = 0
        for ticker in due[: self._fetch_budget_per_pass]:
            self._attempts[ticker] = self._attempts.get(ticker, 0) + 1
            try:
                payload = await self._source.get_market(ticker)
            except ReadBudgetExhausted as exc:
                # OUR OWN bucket refused this GET — nothing reached the
                # exchange. Distinguished from an exchange failure because it
                # is the starvation signature this priority work exists to
                # kill: with the CRITICAL reserve wired it must be ~0, so any
                # occurrence is the alarm that the reserve stopped working.
                self.read_budget_deferrals += 1
                self._bump("settled.read_budget_deferred")
                log.warning(
                    "settled_read_budget_deferred",
                    ticker=ticker,
                    error=repr(exc),
                    detail="the LOCAL read budget refused a settlement "
                    "resolution GET — correctness-critical reads are supposed "
                    "to hold a reserve the routine metadata tier cannot spend; "
                    "resolution retries on the backoff",
                )
                self._mark_unserved(ticker)
                self._pending[ticker] = self._clock.monotonic_ns() + self._retry_after_ns
                continue
            except Exception as exc:
                self.fetch_failures += 1
                self._bump("settled.fetch_failed")
                log.warning(
                    "settled_fetch_failed", ticker=ticker, error=repr(exc)
                )
                self._mark_unserved(ticker)
                self._pending[ticker] = self._clock.monotonic_ns() + self._retry_after_ns
                continue
            # A payload came back: this ticker IS being served (whether or not
            # it is graded yet), so its unserved clock stops.
            self._unserved_since.pop(ticker, None)
            if self._ingest(ticker, payload):
                resolved += 1
        return resolved

    def _bump(self, name: str, by: int = 1) -> None:
        if self._metrics is not None:
            self._metrics.inc(name, by)

    def _mark_unserved(self, ticker: str) -> None:
        """Start (never restart) this ticker's unserved clock on a failed read."""
        self._unserved_since.setdefault(ticker, self._clock.monotonic_ns())

    def _alarm_if_starved(self, now_ns: int) -> None:
        """LOUD line + counter when settled-leg resolution has stopped making
        progress for longer than ONE retry cycle (the resolver's own backoff —
        no new number). See ``_unserved_since`` for the two signatures.

        Ordinary backoff is never reported: a ticker deferred to its
        backoff/live floor is waiting BY DESIGN, and an alarm that cried wolf
        on it would train the operator to ignore the one line that matters.
        THROTTLED to one line per retry cycle (a pass runs every few hundred
        ms), and re-armed the instant progress resumes so a fresh onset is
        still news."""
        overdue = [
            t
            for t, due_ns in self._pending.items()
            if now_ns - due_ns > self._retry_after_ns
        ]
        # A failing read RE-ARMS the backoff, so a ticker whose GETs are all
        # being refused looks permanently on-schedule. This is the signature
        # that actually catches the read-budget priority inversion.
        unserved = [
            t
            for t, since_ns in self._unserved_since.items()
            if t in self._pending and now_ns - since_ns > self._retry_after_ns
        ]
        starved = sorted(set(overdue) | set(unserved))
        if not starved:
            self._last_starve_alarm_ns = None  # re-arm: the next onset is news
            return
        last = self._last_starve_alarm_ns
        if last is not None and now_ns - last < self._retry_after_ns:
            return  # already alarmed this cycle — count once, not per pass
        self._last_starve_alarm_ns = now_ns
        self.starved_passes += 1
        self._bump("settled.resolution_starved", len(starved))
        log.warning(
            "settled_resolution_starved",
            n_starved=len(starved),
            n_overdue=len(overdue),
            n_unserved=len(unserved),
            n_pending=len(self._pending),
            overdue_by_s_max=max(
                ((now_ns - self._pending[t]) / 1e9 for t in overdue), default=0.0
            ),
            unserved_for_s_max=max(
                ((now_ns - self._unserved_since[t]) / 1e9 for t in unserved),
                default=0.0,
            ),
            retry_after_s=self._retry_after_s,
            read_budget_deferrals=self.read_budget_deferrals,
            fetch_failures=self.fetch_failures,
            sample=starved[:8],
            detail="settled-leg resolutions have made no progress for more "
            "than one retry cycle — the bot is not learning what settled "
            "(p_night realized anchor, settlement calibration and position "
            "cleanup all drift from exchange truth)",
        )

    def _ingest(self, ticker: str, payload: JsonDict) -> bool:
        """Classify one GetMarket payload; True iff the ticker RESOLVED."""
        market = payload.get("market", payload)
        if not isinstance(market, dict):
            log.warning("settled_payload_unreadable", ticker=ticker)
            self._pending[ticker] = self._clock.monotonic_ns() + self._retry_after_ns
            return False
        status = str(market.get("status") or "")
        result = str(market.get("result") or "")

        # Track exchange-confirmed liveness for the breaker exemption. Only a
        # KNOWN status moves the flag: a recognized non-live status sets it
        # (the close transition is one-way on the exchange); a recognized LIVE
        # status clears it (defensive); an empty/unknown status string leaves
        # it untouched (fail-closed — never infer liveness).
        if status in LIVE_STATUSES:
            self._known_non_live.discard(ticker)
        elif status in NON_LIVE_STATUSES:
            self._known_non_live.add(ticker)

        if status in LIVE_STATUSES:
            # A live market — the feed owns it; not a settlement candidate.
            # Arm the recheck floor so a book-flicker re-note within the
            # backoff window cannot trigger another fetch.
            self._drop_pending(ticker)
            self._live_floor[ticker] = (
                self._clock.monotonic_ns() + self._retry_after_ns
            )
            return False

        if result == "scalar":
            # A scalar outcome is never a 0/1 leg fact — permanently
            # unresolvable here; the leg stays UNKNOWN (fail-closed).
            log.warning("settled_scalar_unresolvable", ticker=ticker, status=status)
            self._drop_pending(ticker)
            self._unresolvable.add(ticker)
            return False

        if result in ("yes", "no") and status in GRADED_STATUSES:
            marginal = 1.0 if result == "yes" else 0.0
            if not self._settlement_value_consistent(ticker, market, result):
                # Internally inconsistent row: refuse to cache (fail-closed).
                self._drop_pending(ticker)
                self._unresolvable.add(ticker)
                return False
            self._results[ticker] = SettledResult(
                ticker=ticker, result=result, marginal=marginal, status=status
            )
            self._drop_pending(ticker)
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

    def _drop_pending(self, ticker: str) -> None:
        """Remove a ticker from the pending queue AND its attempt counter (a
        later re-registration starts as never-fetched again — correct: it is a
        fresh discovery, and the priority tier must not remember stale
        history)."""
        self._pending.pop(ticker, None)
        self._attempts.pop(ticker, None)
        self._unserved_since.pop(ticker, None)

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
