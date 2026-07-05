"""Live orderbook mirror for one market.

Kalshi books are BIDS ONLY on both sides (docs/api-notes/orderbooks.md):
a YES bid at X is a NO ask at $1−X, so every ask here is derived from the
opposite side's bids. Levels are ascending; best bid is the highest price.

The mirror never guesses: it is ``valid`` only between a snapshot and the first
sign of trouble (gap, negative count, disconnect). Consumers must check
``valid`` — reading prices off an invalid book is a bug upstream, so the
accessors raise.
"""

from __future__ import annotations

from dataclasses import dataclass

from combomaker.core.clock import Clock
from combomaker.core.money import CC_PER_DOLLAR, CentiCents
from combomaker.core.quantity import CentiContracts, cost_micro_dollars
from combomaker.ops.logging import get_logger

log = get_logger(__name__)

Side = str  # "yes" | "no"

Level = tuple[CentiCents, CentiContracts]


class BookInvalidError(RuntimeError):
    """Read attempted on an invalid (never-snapshotted or gapped) book."""


@dataclass(frozen=True, slots=True)
class TopOfBook:
    """Derived top of book; None on a side with no bids."""

    yes_bid_cc: CentiCents | None
    yes_bid_qty: CentiContracts | None
    no_bid_cc: CentiCents | None
    no_bid_qty: CentiContracts | None

    @property
    def yes_ask_cc(self) -> CentiCents | None:
        if self.no_bid_cc is None:
            return None
        return CentiCents(CC_PER_DOLLAR - self.no_bid_cc)

    @property
    def spread_cc(self) -> CentiCents | None:
        if self.yes_bid_cc is None or self.yes_ask_cc is None:
            return None
        return CentiCents(self.yes_ask_cc - self.yes_bid_cc)

    @property
    def mid_cc(self) -> CentiCents | None:
        """Simple YES mid. None unless both sides exist."""
        if self.yes_bid_cc is None or self.yes_ask_cc is None:
            return None
        return CentiCents((self.yes_bid_cc + self.yes_ask_cc) // 2)

    def microprice(self) -> float | None:
        """Size-weighted YES price in probability space (float is fine here).

        Weighted toward the side with LESS size (standard microprice): with a
        big bid and a small ask the true price sits nearer the ask.
        """
        if (
            self.yes_bid_cc is None
            or self.yes_ask_cc is None
            or not self.yes_bid_qty
            or not self.no_bid_qty
        ):
            return None
        bid_qty = float(self.yes_bid_qty)
        ask_qty = float(self.no_bid_qty)  # size behind the derived ask
        total = bid_qty + ask_qty
        return (self.yes_bid_cc * ask_qty + self.yes_ask_cc * bid_qty) / total / CC_PER_DOLLAR


@dataclass(frozen=True, slots=True)
class ExecutableQuote:
    """Result of walking derived asks for a marketable buy."""

    filled: CentiContracts
    worst_price_cc: CentiCents
    cost_micro_dollars: int

    @property
    def vwap_prob(self) -> float:
        if self.filled == 0:
            return 0.0
        return self.cost_micro_dollars / self.filled / CC_PER_DOLLAR


class OrderbookMirror:
    def __init__(self, ticker: str, clock: Clock) -> None:
        self.ticker = ticker
        self._clock = clock
        self._yes: dict[CentiCents, CentiContracts] = {}
        self._no: dict[CentiCents, CentiContracts] = {}
        self._valid = False
        self.last_change_ts_ms: int | None = None  # exchange time of last delta
        self.last_change_mono_ns: int | None = None
        self.snapshot_mono_ns: int | None = None

    # --- state transitions ---

    @property
    def valid(self) -> bool:
        return self._valid

    def invalidate(self, reason: str) -> None:
        if self._valid:
            log.info("book_invalidated", ticker=self.ticker, reason=reason)
        self._valid = False

    def apply_snapshot(self, yes: list[Level], no: list[Level]) -> None:
        self._yes = {price: qty for price, qty in yes if qty > 0}
        self._no = {price: qty for price, qty in no if qty > 0}
        self._valid = True
        now = self._clock.monotonic_ns()
        self.snapshot_mono_ns = now
        self.last_change_mono_ns = now

    def apply_delta(
        self, side: Side, price_cc: CentiCents, delta: CentiContracts, ts_ms: int | None
    ) -> bool:
        """Apply a signed delta. Returns False (and invalidates) on corruption.

        A delta driving a level negative means we missed a message the seq
        numbers didn't catch — treat exactly like a gap.
        """
        if not self._valid:
            return True  # ignored; a snapshot must arrive before deltas count
        book = self._yes if side == "yes" else self._no
        new_count = book.get(price_cc, CentiContracts(0)) + delta
        if new_count < 0:
            log.warning(
                "book_negative_count",
                ticker=self.ticker,
                side=side,
                price_cc=int(price_cc),
                delta=int(delta),
            )
            self.invalidate("negative_count")
            return False
        if new_count == 0:
            book.pop(price_cc, None)
        else:
            book[price_cc] = CentiContracts(new_count)
        self.last_change_ts_ms = ts_ms
        self.last_change_mono_ns = self._clock.monotonic_ns()
        return True

    # --- reads (raise on invalid: reading a bad book is an upstream bug) ---

    def _require_valid(self) -> None:
        if not self._valid:
            raise BookInvalidError(f"book {self.ticker} is not valid")

    def top(self) -> TopOfBook:
        self._require_valid()
        yes_best = max(self._yes) if self._yes else None
        no_best = max(self._no) if self._no else None
        return TopOfBook(
            yes_bid_cc=yes_best,
            yes_bid_qty=self._yes[yes_best] if yes_best is not None else None,
            no_bid_cc=no_best,
            no_bid_qty=self._no[no_best] if no_best is not None else None,
        )

    def depth_qty(self, side: Side) -> CentiContracts:
        self._require_valid()
        book = self._yes if side == "yes" else self._no
        return CentiContracts(sum(book.values()))

    def executable_buy(self, side: Side, qty: CentiContracts) -> ExecutableQuote | None:
        """Walk derived asks to buy ``qty`` of ``side``. None if underfilled.

        Buying YES lifts NO bids at derived price $1−no_bid, best (highest
        no_bid) first; symmetric for NO.
        """
        self._require_valid()
        if qty <= 0:
            raise ValueError("qty must be positive")
        opposite = self._no if side == "yes" else self._yes
        remaining = int(qty)
        cost = 0
        worst: CentiCents | None = None
        for opp_price in sorted(opposite, reverse=True):
            available = opposite[opp_price]
            take = min(remaining, int(available))
            derived_price = CentiCents(CC_PER_DOLLAR - opp_price)
            cost += cost_micro_dollars(CentiContracts(take), derived_price)
            worst = derived_price
            remaining -= take
            if remaining == 0:
                break
        if remaining > 0 or worst is None:
            return None
        return ExecutableQuote(filled=qty, worst_price_cc=worst, cost_micro_dollars=cost)

    def age_since_change_s(self) -> float | None:
        """Seconds since the last book change (None before any snapshot).

        NOTE: a quiet book is not a stale FEED — feed health lives at the
        subscription level. This age feeds velocity/in-play heuristics.
        """
        if self.last_change_mono_ns is None:
            return None
        return (self._clock.monotonic_ns() - self.last_change_mono_ns) / 1e9
