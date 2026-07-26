"""ENTITY BOUND (operator 2026-07-26: "I wouldn't prefer having our book rely
on 1 leg like that").

The 7/25 book carried ~$420 of premium on FOUR pitchers' arms — $282 of one
direction (short K-overs) — and nothing refused it: the leg-axis steer only
PRICED that concentration. This is the hard wall. It accumulates committed +
reserved + candidate on a (family:entity x direction) key, exactly mirroring
the accumulated per-combo bound, and is OFF unless a fraction is configured.
"""

from __future__ import annotations

from fractions import Fraction

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.limits import DailyPnl, LimitChecker, RiskLimits

CONVENTIONS = Conventions(
    verified=True, source="test",
    maker_side_on_yes_accept=Side.YES, maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True, maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)
BANKROLL = 20_000_000  # $2,000
GREENE_4 = "KXMLBKS-26JUL261335TORBOS-TORHGREENE21-4"
GREENE_6 = "KXMLBKS-26JUL261335TORBOS-TORHGREENE21-6"   # same arm, other rung
CEASE_5 = "KXMLBKS-26JUL261610SDMIA-SDDCEASE44-5"


def _pos(pid: str, ticker: str, *, price_cc: int = 2_500) -> OpenPosition:
    return OpenPosition(
        position_id=pid, combo_ticker=f"COMBO-{pid}", collection=None,
        our_side=Side.NO, contracts=CentiContracts(10_000),  # 100 contracts
        entry_price_cc=CentiCents(price_cc),
        legs=(LegRef(ticker, "KX-G1", "yes"),),
    )


def _check(book: ExposureBook, cand: OpenPosition, frac: str | None):
    limits = RiskLimits(
        caps_shadow_mode=False,
        entity_loss_frac=Fraction(frac) if frac else None,
        per_combo_loss_frac=Fraction(50, 100),
        game_loss_frac=Fraction(50, 100),
        slate_loss_frac=Fraction(90, 100),
        directional_frac=Fraction(90, 100),
    )
    return [
        b.reason
        for b in LimitChecker(limits).check(
            book, lambda t: 0.5, DailyPnl(),
            candidate_positions=[cand], risk_bankroll_cc=BANKROLL,
        )
    ]


def test_same_arm_across_rungs_and_combos_accumulates_and_trips() -> None:
    book = ExposureBook(CONVENTIONS)
    # $50 on Greene's arm across two combos / two rungs (100 ct @ 25c each),
    # which is UNDER the $60 wall...
    book.add_position(_pos("h1", GREENE_4))
    book.add_position(_pos("h2", GREENE_6))
    # ...and a $25 candidate on the same arm pushes it to $75. 3% of $2,000 = $60,
    # so the CANDIDATE is what breaches — the accumulation is the point.
    cand = _pos("cand", GREENE_4, price_cc=2_500)
    assert ReasonCode.SKIP_ENTITY_LOSS_CAP in _check(book, cand, "3/100")


def test_a_different_arm_is_not_blocked() -> None:
    book = ExposureBook(CONVENTIONS)
    book.add_position(_pos("h1", GREENE_4))
    book.add_position(_pos("h2", GREENE_6))
    cand = _pos("cand", CEASE_5, price_cc=2_500)  # a different pitcher
    assert ReasonCode.SKIP_ENTITY_LOSS_CAP not in _check(book, cand, "3/100")


def test_axis_off_by_default_is_byte_identical() -> None:
    book = ExposureBook(CONVENTIONS)
    book.add_position(_pos("h1", GREENE_4))
    book.add_position(_pos("h2", GREENE_6))
    cand = _pos("cand", GREENE_4, price_cc=2_500)
    assert ReasonCode.SKIP_ENTITY_LOSS_CAP not in _check(book, cand, None)


def test_opposite_direction_on_the_same_arm_is_a_different_key() -> None:
    # Short "Greene 4+ Ks" and short "Greene UNDER" are opposite directions —
    # they must NOT pool into one bucket (that would refuse the very hedge we
    # want to attract).
    book = ExposureBook(CONVENTIONS)
    book.add_position(_pos("h1", GREENE_4))
    book.add_position(_pos("h2", GREENE_6))
    other_side = OpenPosition(
        position_id="cand", combo_ticker="COMBO-cand", collection=None,
        our_side=Side.NO, contracts=CentiContracts(10_000),  # 100 contracts
        entry_price_cc=CentiCents(2_500),
        legs=(LegRef(GREENE_4, "KX-G1", "no"),),  # opposite leg side
    )
    assert ReasonCode.SKIP_ENTITY_LOSS_CAP not in _check(book, other_side, "3/100")
