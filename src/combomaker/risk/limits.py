"""Risk limits: all config, all enforced pre-quote AND pre-confirm.

``check`` returns EVERY breach, not the first — breach patterns are tuning
data. The mass-acceptance worst case is part of the standard check: if the
book-plus-all-open-quotes portfolio would breach, we stop issuing quotes even
though nothing has filled yet. Unknown marginals anywhere in the decomposition
count as a breach (UNKNOWN is never safe).
"""

from __future__ import annotations

from dataclasses import dataclass

from combomaker.core.reasons import ReasonCode
from combomaker.risk.exposure import (
    ExposureBook,
    MarginalProvider,
    OpenPosition,
)


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_contracts_per_quote: float = 100.0
    max_notional_per_quote_dollars: float = 500.0
    max_market_delta_contracts: float = 300.0
    max_event_delta_contracts: float = 500.0
    max_gross_notional_dollars: float = 5_000.0
    max_open_quotes: int = 20
    max_daily_loss_dollars: float = 500.0
    max_event_worst_case_loss_dollars: float = 1_000.0


@dataclass(frozen=True, slots=True)
class Breach:
    reason: ReasonCode
    detail: str


@dataclass(frozen=True, slots=True)
class DailyPnl:
    realized_cc: int = 0
    unrealized_cc: int = 0

    @property
    def total_cc(self) -> int:
        return self.realized_cc + self.unrealized_cc


class LimitChecker:
    def __init__(self, limits: RiskLimits) -> None:
        self._limits = limits

    def check(
        self,
        book: ExposureBook,
        marginals: MarginalProvider,
        daily_pnl: DailyPnl,
        *,
        candidate_positions: list[OpenPosition] | None = None,
        adding_quote: bool = False,
    ) -> list[Breach]:
        """All current breaches, mass-acceptance included.

        ``candidate_positions``: hypothetical fills being contemplated (last
        look passes the accepted side here). ``adding_quote``: pre-quote check
        counts one more open quote.
        """
        limits = self._limits
        breaches: list[Breach] = []
        candidates = candidate_positions or []

        for position in candidates:
            contracts = int(position.contracts) / 100
            if contracts > limits.max_contracts_per_quote:
                breaches.append(
                    Breach(
                        ReasonCode.SKIP_SIZE_ABOVE_MAX,
                        f"candidate {contracts:.2f} contracts > "
                        f"{limits.max_contracts_per_quote}",
                    )
                )
            notional_dollars = position.max_loss_cc / 10_000
            if notional_dollars > limits.max_notional_per_quote_dollars:
                breaches.append(
                    Breach(
                        ReasonCode.SKIP_SIZE_ABOVE_MAX,
                        f"candidate notional ${notional_dollars:.2f} > "
                        f"${limits.max_notional_per_quote_dollars}",
                    )
                )

        open_quotes = book.snapshot(marginals, mass_acceptance=False).open_quote_count
        if adding_quote and open_quotes + 1 > limits.max_open_quotes:
            breaches.append(
                Breach(
                    ReasonCode.SKIP_MAX_OPEN_QUOTES,
                    f"{open_quotes} open quotes at cap {limits.max_open_quotes}",
                )
            )

        snapshot = book.snapshot(
            marginals, mass_acceptance=True, extra_positions=candidates
        )
        if snapshot.unknown_marginals:
            breaches.append(
                Breach(
                    ReasonCode.SKIP_CLASSIFIER_UNKNOWN,
                    "exposure decomposition has unknown marginals",
                )
            )
        for ticker, delta in snapshot.delta_by_market.items():
            if abs(delta) > limits.max_market_delta_contracts:
                breaches.append(
                    Breach(
                        ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH,
                        f"market {ticker} delta {delta:.1f} > "
                        f"{limits.max_market_delta_contracts}",
                    )
                )
        for event, delta in snapshot.delta_by_event.items():
            if abs(delta) > limits.max_event_delta_contracts:
                breaches.append(
                    Breach(
                        ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH,
                        f"event {event} delta {delta:.1f} > "
                        f"{limits.max_event_delta_contracts}",
                    )
                )
        if snapshot.gross_notional_cc / 10_000 > limits.max_gross_notional_dollars:
            breaches.append(
                Breach(
                    ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH,
                    f"gross notional ${snapshot.gross_notional_cc / 10_000:.2f} > "
                    f"${limits.max_gross_notional_dollars}",
                )
            )
        for event, loss_cc in snapshot.worst_case_loss_by_event_cc.items():
            if loss_cc / 10_000 > limits.max_event_worst_case_loss_dollars:
                breaches.append(
                    Breach(
                        ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH,
                        f"event {event} worst-case loss ${loss_cc / 10_000:.2f} > "
                        f"${limits.max_event_worst_case_loss_dollars}",
                    )
                )

        if -daily_pnl.total_cc / 10_000 >= limits.max_daily_loss_dollars:
            breaches.append(
                Breach(
                    ReasonCode.HALT_DAILY_LOSS,
                    f"daily P&L ${daily_pnl.total_cc / 10_000:.2f} at loss limit "
                    f"${limits.max_daily_loss_dollars}",
                )
            )
        return breaches
