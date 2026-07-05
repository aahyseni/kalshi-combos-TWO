"""Market price grid: parse ``price_ranges`` and round bids maker-favorably.

``GET /markets/{ticker}`` exposes the grid as a list of ranges
``{start, end, step}`` (dollar strings); tapered structures use several ranges
with different steps. Quote prices must land on this grid.

Quiet-failure defense #4: both quote prices are OUR BIDS, so grid rounding for
bids is always DOWN (maker-favorable) — never nearest. ``snap_up`` exists for
walking derived ask levels, not for our bids.

Assumption (audit table): levels within a range are ``start + k*step`` for
integer ``k >= 0``, inclusive of both endpoints when they coincide with the
lattice. Boundary semantics at range joins are queued for empirical
verification (docs/api-notes/SUMMARY.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from combomaker.core.money import CentiCents, cc_from_dollars_str


class GridError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GridRange:
    start_cc: CentiCents
    end_cc: CentiCents
    step_cc: CentiCents

    def __post_init__(self) -> None:
        if self.step_cc <= 0 or self.end_cc < self.start_cc:
            raise GridError(f"malformed range: {self}")

    def contains(self, price_cc: CentiCents) -> bool:
        return self.start_cc <= price_cc <= self.end_cc

    def is_on(self, price_cc: CentiCents) -> bool:
        return self.contains(price_cc) and (price_cc - self.start_cc) % self.step_cc == 0

    def floor(self, price_cc: CentiCents) -> CentiCents:
        """Largest lattice point <= price_cc (price must be within the range)."""
        k = (price_cc - self.start_cc) // self.step_cc
        return CentiCents(self.start_cc + k * self.step_cc)


@dataclass(frozen=True, slots=True)
class PriceGrid:
    ranges: tuple[GridRange, ...]

    @classmethod
    def from_market_payload(cls, market: dict[str, Any]) -> PriceGrid:
        """Build from a ``GET /markets/{ticker}`` market object.

        Expects ``price_ranges``: list of {start, end, step} dollar strings.
        Raises GridError when absent/malformed — an unknown grid is a no-quote,
        never a guessed 1-cent default (quiet-failure defense #2).
        """
        raw = market.get("price_ranges")
        if not raw or not isinstance(raw, list):
            raise GridError(
                f"market {market.get('ticker')!r} has no usable price_ranges; "
                "unknown grid means no-quote, not a guessed default"
            )
        ranges = []
        for item in raw:
            try:
                ranges.append(
                    GridRange(
                        start_cc=cc_from_dollars_str(str(item["start"])),
                        end_cc=cc_from_dollars_str(str(item["end"])),
                        step_cc=cc_from_dollars_str(str(item["step"])),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise GridError(f"malformed price_ranges entry {item!r}: {exc}") from exc
        ranges.sort(key=lambda r: r.start_cc)
        return cls(ranges=tuple(ranges))

    def is_on_grid(self, price_cc: CentiCents) -> bool:
        return any(r.is_on(price_cc) for r in self.ranges)

    def snap_bid_down(self, price_cc: CentiCents) -> CentiCents | None:
        """Round OUR BID maker-favorably: the highest grid price <= price_cc.

        None when no grid point lies at or below the price (bid unquotable).
        """
        best: CentiCents | None = None
        for r in self.ranges:
            if price_cc >= r.start_cc:
                candidate = r.floor(price_cc) if price_cc <= r.end_cc else r.end_cc
                if not r.is_on(candidate):  # end_cc may be off-lattice
                    candidate = r.floor(candidate)
                if best is None or candidate > best:
                    best = candidate
        return best

    def snap_up(self, price_cc: CentiCents) -> CentiCents | None:
        """Smallest grid price >= price_cc (for derived-ask walks, NOT our bids)."""
        best: CentiCents | None = None
        for r in self.ranges:
            if price_cc <= r.end_cc:
                if price_cc <= r.start_cc:
                    candidate = r.start_cc
                else:
                    floored = r.floor(price_cc)
                    candidate = (
                        floored
                        if floored == price_cc
                        else CentiCents(floored + r.step_cc)
                    )
                    if candidate > r.end_cc:
                        continue
                if best is None or candidate < best:
                    best = candidate
        return best
