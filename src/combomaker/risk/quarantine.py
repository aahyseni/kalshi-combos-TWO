"""Per-market SAFETY QUARANTINE — the scoped response to a market whose
LIFECYCLE state moved under us (2026-07-26 live halt, KXMLBTOTAL-…CLETB-6).

Why this exists
---------------
The metadata-change breaker protects SETTLEMENT correctness: if what an open
position PAYS changes under us, the whole bot must stop and reconcile. But the
2026-07-26 14:15 ET halt was not that. It was ``status: active → inactive`` —
Kalshi's ``deactivated`` event, an in-play trading PAUSE at game end. Nothing
about the payoff moved (``rules_primary`` / ``strike_type`` / ``floor_strike``
/ ``event_ticker`` / ``expiration_time`` / ``latest_expiration_time`` were all
byte-identical, and the market went on to settle ``result="no"`` on the same
rule six minutes later). A whole-book kill for one market's lifecycle
transition is itself the defect.

The proportionate response to a lifecycle move is not "keep trading" either:
- a PAUSE may be transient (game-end / rain / review) or PERMANENT (the market
  is withdrawn and VOIDED — an open position is refunded, not paid 0/1), and
  the two are INDISTINGUISHABLE at the instant of the change (measured
  2026-07-26: deactivation moves ONLY ``status``; 50 in-play MLB props were
  sitting in ``inactive`` with untouched far-future close times);
- on UNPAUSE (``activated``) Kalshi AUTO-CANCELS every resting order
  (docs/api-notes/index-scan.md:150), so our open-quote mirror is wrong from
  that moment until re-synced;
- a ``close_time`` rewrite means every resting quote on that market was priced
  against the wrong time-to-close.

All three are cured by the SAME scoped action: pull our resting quotes off that
market and refuse to quote it until the exchange says it is normally tradable
again. That is what a quarantine is — a market-scoped stop, not a bot-wide one.

Fail-closed by construction
---------------------------
- The lane is chosen by FIELD, never by ticker, series, or elapsed time. A
  quarantine is only ever reachable when the SETTLEMENT fingerprint
  (``quote_app._settlement_fingerprint``) is byte-identical across the two
  samples; any settlement-relevant field moving is a hard halt as before.
- ``enforced`` starts False and only becomes True when every DELETABLE resting
  quote touching the market was actually pulled. A quarantine still unenforced
  on the next status tick is PROMOTED to the whole-bot halt: if we cannot get
  our quotes off a market whose lifecycle state moved, the scoped response has
  failed and the fail-closed response is the only one left.
- Release is STRUCTURAL, never a timer: the market must read a normally
  tradable status again AND its lifecycle fingerprint must have stopped
  moving. A permanently deactivated (voided) market never returns to
  ``active``, so it never leaves quarantine.

This is measured live-exchange state, not a static blocklist: nothing here is
hand-listed, and no ticker can be entered or held by configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from combomaker.ops.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class QuarantinedMarket:
    """One market held out of quoting. ``enforced`` is the proof that our
    resting exposure on it was actually withdrawn."""

    ticker: str
    detail: str
    enforced: bool = False


@dataclass(slots=True)
class MarketQuarantine:
    """The live quarantine set. Plain in-memory state read on the hot path
    (one set lookup per leg in ``RfqFilter.evaluate``) and mutated only from
    the 15s status loop — single-loop discipline, no locking."""

    _held: dict[str, QuarantinedMarket] = field(default_factory=dict)
    # ARMED = a quote-refusal consumer has registered itself (``RfqFilter``
    # calls ``arm()`` when it is constructed with this object). Half a scoped
    # response is not a scoped response: if nothing is refusing NEW quotes on
    # a quarantined market, the metadata breaker must not use the scoped lane
    # at all — it hard-halts as it did before. This makes "someone forgot to
    # wire it" a fail-closed condition instead of a silent pass.
    _armed: bool = False

    def arm(self) -> None:
        """Called by the quote-refusal consumer (``RfqFilter``) at wiring time."""
        self._armed = True

    @property
    def armed(self) -> bool:
        return self._armed

    def quarantine(self, ticker: str, detail: str) -> None:
        """Hold ``ticker`` out of quoting. RE-ARMS the enforcement flag on every
        new lifecycle move: a market that moves again after we pulled our quotes
        must prove itself clear again (a quote could have been placed in the
        interval between the two moves)."""
        prior = self._held.get(ticker)
        self._held[ticker] = QuarantinedMarket(ticker, detail, enforced=False)
        if prior is None:
            log.warning("market_quarantined", ticker=ticker, detail=detail)
        else:
            log.info("market_quarantine_rearmed", ticker=ticker, detail=detail)

    def mark_enforced(self, ticker: str) -> None:
        """Record that every deletable resting quote touching ``ticker`` is
        gone. Only the enforcement pass may call this."""
        held = self._held.get(ticker)
        if held is None or held.enforced:
            return
        self._held[ticker] = QuarantinedMarket(held.ticker, held.detail, enforced=True)

    def release(self, ticker: str) -> None:
        """Drop the hold (the market re-read normally tradable AND stable)."""
        if self._held.pop(ticker, None) is not None:
            log.info("market_quarantine_released", ticker=ticker)

    def is_quarantined(self, ticker: str) -> bool:
        """Hot-path predicate: pure dict lookup, no network, never raises."""
        return ticker in self._held

    def unenforced(self) -> tuple[str, ...]:
        """Markets whose resting quotes we have NOT provably withdrawn. The
        caller promotes these to the whole-bot halt on the next status tick."""
        return tuple(
            sorted(t for t, held in self._held.items() if not held.enforced)
        )

    def pending(self) -> tuple[str, ...]:
        """Every quarantined ticker (sorted, stable for logging)."""
        return tuple(sorted(self._held))

    def detail(self, ticker: str) -> str | None:
        held = self._held.get(ticker)
        return None if held is None else held.detail

    def __len__(self) -> int:
        return len(self._held)
