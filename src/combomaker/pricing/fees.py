"""Fee model: quadratic trade fee with exact integer math.

fee = ceil_to_centicent(coef × multiplier × C × P × (1−P))   [dollars]

with C = contracts, P = price. Coefficients VERIFIED against the official Kalshi
fee-schedule PDF (effective 2026-06-29, operator-provided): general/taker 0.07,
maker 0.0175 (= 7/100, 7/400), quadratic C·P·(1−P), rounded UP to a centi-cent
(the PDF's "fee + positionCost rounded up to a centi-cent" is equivalent since
positionCost — whole cents × whole contracts — is always a whole centi-cent).
S&P/NASDAQ series use 0.035 (not sports; absent here). Maker fees apply ONLY to
markets on Kalshi's maker-fee list — quadratic sports/combo series charge $0
maker (Phase 2.5 ground truth + PDF), so a resting combo quote pays no fee. That
list can change (GET /series/fee_changes) — monitor it, and still reconcile
predicted vs actual to the cent on real fills (quiet-failure defense #3).

Fail-safe attribution: whether our RFQ fill is charged maker or taker fees is
unknown until Phase 2.5 ground truth. When ``Conventions.maker_is_taker_on_fill``
is None we price with the TAKER coefficient — overestimating cost widens
quotes; the convenient assumption would quietly underprice every quote.

Integer identity used throughout (no floats, no Decimal in the hot path):
fee_cc = ceil( num × qty_centi × p × (10^4 − p) / (den × 10^6) )
where coef = num/den and p is the price in centi-cents. Derivation:
(n/d)·(q/100)·(p/10^4)·((10^4−p)/10^4) dollars × 10^4 cc/$ = n·q·p·(10^4−p)/(d·10^6).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction

from combomaker.core.conventions import Conventions
from combomaker.core.money import CC_PER_DOLLAR, CentiCents
from combomaker.core.quantity import CentiContracts


class FeeType(StrEnum):
    QUADRATIC = "quadratic"
    QUADRATIC_WITH_MAKER_FEES = "quadratic_with_maker_fees"
    FLAT = "flat"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, raw: object) -> FeeType:
        try:
            return cls(str(raw))
        except ValueError:
            return cls.UNKNOWN


class FeeUnknownError(ValueError):
    """Fee cannot be computed without guessing — caller must no-quote."""


def _coef_fraction(s: str) -> Fraction:
    return Fraction(Decimal(s))


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Coefficients as exact fractions (from config decimal strings)."""

    taker_coef: Fraction
    maker_coef: Fraction

    @classmethod
    def from_strings(cls, taker: str, maker: str) -> FeeSchedule:
        return cls(taker_coef=_coef_fraction(taker), maker_coef=_coef_fraction(maker))


class FeeModel:
    def __init__(self, schedule: FeeSchedule, conventions: Conventions) -> None:
        self._schedule = schedule
        self._conventions = conventions

    def _pricing_coef(self, fee_type: FeeType) -> Fraction:
        """Coefficient for OUR side of an RFQ fill, fail-safe on unknowns."""
        if fee_type is FeeType.UNKNOWN or fee_type is FeeType.FLAT:
            # Flat-fee series need their per-series constant from the schedule
            # PDF; without it any number is a guess.
            raise FeeUnknownError(f"cannot price fees for fee_type={fee_type}")
        charged_as_taker = self._conventions.maker_is_taker_on_fill
        if charged_as_taker is None or charged_as_taker:
            return self._schedule.taker_coef  # conservative default
        if fee_type is FeeType.QUADRATIC:
            return Fraction(0)  # quadratic series charge no maker fee
        return self._schedule.maker_coef

    def trade_fee_cc(
        self,
        *,
        price_cc: CentiCents,
        qty: CentiContracts,
        fee_type: FeeType,
        multiplier: Fraction = Fraction(1),
    ) -> CentiCents:
        """Total trade fee in centi-cents, rounded UP (exchange ceils)."""
        if not 0 <= price_cc <= CC_PER_DOLLAR:
            raise ValueError(f"price out of range: {price_cc}")
        if qty < 0:
            raise ValueError(f"negative quantity: {qty}")
        coef = self._pricing_coef(fee_type) * multiplier
        numerator = coef.numerator * qty * price_cc * (CC_PER_DOLLAR - price_cc)
        denominator = coef.denominator * 1_000_000
        return CentiCents(-(-numerator // denominator))  # ceil division

    def fee_per_contract_cc(
        self,
        *,
        price_cc: CentiCents,
        fee_type: FeeType,
        multiplier: Fraction = Fraction(1),
    ) -> CentiCents:
        """Per-contract fee (ceiled) — the quote-width component.

        Slightly conservative vs the exchange's single ceil over the whole
        fill, which is the right direction for pricing.
        """
        return self.trade_fee_cc(
            price_cc=price_cc,
            qty=CentiContracts(100),
            fee_type=fee_type,
            multiplier=multiplier,
        )
