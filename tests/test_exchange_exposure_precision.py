"""EXCHANGE-EXPOSURE PRECISION — the live fail-open defect (2026-07-26).

WHAT WAS BROKEN (present on HEAD, not introduced by the current tree).
``_exchange_exposure_cc_by_ticker`` parsed the positions payload's
``market_exposure_dollars`` with ``cc_from_dollars_str``, which RAISES on any
value that is not a whole number of centi-cents. Kalshi's ``FixedPointDollars``
carries "up to 6 decimal places of precision"
(docs.kalshi.com/api-reference/portfolio/get-positions.md), and the live wire
uses all six. Measured READ-ONLY against prod on 2026-07-26 (fixture
``tests/fixtures/live_positions_20260726.json``):

  * 46/46 open rows carry a SIX-decimal ``market_exposure_dollars``;
  * 21/46 of those are sub-centi-cent (e.g. ``"2.688790"`` = 26.8879 cc) and
    therefore RAISED;
  * the documented int-cents fallback ``market_exposure`` is ABSENT from 46/46
    rows — the fallback branch was dead code.

Consequence: for 21 positions the exposure map had no entry,
``reserve_from_exchange_figures`` returned ``None``, and the fail-CLOSED
reserve — the entire safety net for holdings no durable source can model —
silently did not fire. Two open positions worth $9.0106 contributed ZERO to the
risk book at boot AND after reconcile (book $630.5271 vs exchange $639.5533).
Systemically, 18 more positions carrying $329.84 (51.6% of the book) were one
dropped ``rfqs`` tape row away from the same zero.

THE FIX, pinned here:
1. ``exposure_cc_from_dollars_str`` — a SEPARATE, explicitly-named conversion
   for exchange-reported at-risk figures that rounds CEILING, so a booked
   exposure is never LESS than the exchange's truth. The shared exactness
   primitive ``cc_from_dollars_str`` is untouched (prices/balances/settlement
   values still must be exact).
2. An unreadable figure is no longer silent: the position is adopted at the
   PROVEN $1.00/contract upper bound and a metric + error log fire.
"""

from __future__ import annotations

import json
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any, cast

import pytest
import structlog

from combomaker.core.clock import FakeClock
from combomaker.core.conventions import Side
from combomaker.core.money import (
    MoneyParseError,
    cc_from_dollars_str,
    exposure_cc_from_dollars_str,
)
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.ops.quote_app import (
    QuoteApp,
    _exchange_exposure_cc_by_ticker,
    position_reconcile_unmodeled_once,
    reserve_at_full_notional,
)
from combomaker.risk.exposure import ExposureBook
from combomaker.sim.book_model import build_book_model
from tests.test_rehydrate_positions import CONV, IS_ME

_FIXTURE = Path(__file__).parent / "fixtures" / "live_positions_20260726.json"


def _live_payload() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(_FIXTURE.read_text(encoding="utf-8")))


def _live_rows() -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], _live_payload()["market_positions"])


# --------------------------------------------------------------------------
# 1. six-decimal strings parse, and round FAIL-CLOSED (never under truth)
# --------------------------------------------------------------------------


def test_six_decimal_exposure_parses_and_never_rounds_below_truth() -> None:
    """The exact parser raises on these; the exposure parser books them at or
    ABOVE truth, by strictly less than one centi-cent."""
    cases = [
        "2.688790",   # the live string from the defect report
        "0.210540",
        "56.181690",
        "81.465330",
        "0.000001",   # smallest sub-cc dust: must NOT floor to zero
        "0.00005",
        "0.00015",
        "0.000100",   # exactly 1cc — unchanged
        "3.100000",   # whole cc — unchanged
        "0.000000",
    ]
    for s in cases:
        truth_cc = Decimal(s) * 10_000
        booked = int(exposure_cc_from_dollars_str(s))
        assert booked >= truth_cc, f"{s}: booked {booked} < truth {truth_cc}"
        assert booked - truth_cc < 1, f"{s}: overstated by ≥1cc"
        assert booked == int(truth_cc.to_integral_value(rounding=ROUND_CEILING))


def test_sub_cc_exposure_never_books_zero() -> None:
    """Rounding DOWN (or to nearest) would let a real position book as $0.00 —
    the fail-OPEN direction. Ceiling guarantees a positive booking for any
    positive exposure, however small."""
    for s in ("0.000001", "0.000049", "0.000099"):
        assert int(exposure_cc_from_dollars_str(s)) == 1


def test_exposure_parser_rejects_garbage_and_negatives() -> None:
    """Unreadable stays unreadable — the caller's fail-closed route handles it.
    Non-finite input raises MoneyParseError (not a bare ArithmeticError), because
    every caller handles MoneyParseError fail-safe and none handle the latter."""
    for bad in ("abc", "", "-0.01", "NaN", "Infinity"):
        with pytest.raises(MoneyParseError):
            exposure_cc_from_dollars_str(bad)


def test_exact_primitive_semantics_unchanged() -> None:
    """The shared exactness primitive was NOT loosened: callers that legitimately
    require exactness (prices, balances, revenue, settlement values) still raise
    on a sub-centi-cent value."""
    with pytest.raises(MoneyParseError):
        cc_from_dollars_str("2.688790")
    assert int(cc_from_dollars_str("0.5600")) == 5_600


# --------------------------------------------------------------------------
# 2. every CURRENTLY-LIVE exposure string parses
# --------------------------------------------------------------------------


def test_live_payload_field_presence_is_what_we_coded_against() -> None:
    """The wire, not the docs, is the source of truth (hard rule 4). Pinned so a
    schema change is a test failure rather than a silent fail-open."""
    rows = _live_rows()
    assert len(rows) == 46
    for row in rows:
        assert row["market_exposure_dollars"] is not None
        # The documented int-cents fallback is ABSENT on every real row.
        assert "market_exposure" not in row
        assert len(str(row["market_exposure_dollars"]).split(".")[1]) == 6


def test_every_live_exposure_string_parses() -> None:
    """ALL 46 — including the 21 that the old exact parser dropped."""
    strings = [str(r["market_exposure_dollars"]) for r in _live_rows()]
    dropped_by_old_parser = 0
    for s in strings:
        exposure_cc_from_dollars_str(s)  # must not raise
        try:
            cc_from_dollars_str(s)
        except MoneyParseError:
            dropped_by_old_parser += 1
    assert dropped_by_old_parser == 21  # the measured defect, pinned


def test_live_payload_maps_all_46_tickers_and_never_understates() -> None:
    """End to end on the real payload: no ticker is unreadable, and the booked
    total is ≥ the exchange's own $639.5533 of exposure."""
    by_ticker, unreadable = _exchange_exposure_cc_by_ticker(_live_payload())
    assert unreadable == {}
    assert len(by_ticker) == 46
    exchange_truth_cc = sum(
        Decimal(str(r["market_exposure_dollars"])) * 10_000 for r in _live_rows()
    )
    assert exchange_truth_cc == Decimal("6395533.30")  # $639.5533
    booked = sum(by_ticker.values())
    assert booked >= exchange_truth_cc
    assert booked - exchange_truth_cc < len(by_ticker)  # <1cc per position


def test_every_live_row_yields_a_nonzero_reserve() -> None:
    """The consequence the defect had: a position that maps to nothing reserves
    nothing. Every live row must now produce a reserve whose max_loss_cc is at
    or above the exchange's figure for that ticker."""
    by_ticker, _ = _exchange_exposure_cc_by_ticker(_live_payload())
    from combomaker.core.quantity import qty_from_fp_str
    from combomaker.ops.quote_app import reserve_from_exchange_figures

    for row in _live_rows():
        ticker = str(row["ticker"])
        signed = int(qty_from_fp_str(str(row["position_fp"])))
        side = Side.NO if signed < 0 else Side.YES
        pos = reserve_from_exchange_figures(
            ticker, side, abs(signed), by_ticker[ticker]
        )
        assert pos is not None, ticker
        assert pos.max_loss_cc >= by_ticker[ticker], ticker


# --------------------------------------------------------------------------
# 3. an UNPARSEABLE figure still raises det-max, and alarms
# --------------------------------------------------------------------------

_ORPHAN = "KXMVECROSSCATEGORY-S2026PRECISION-BBB"


class _StubRest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def get_positions(self, **params: Any) -> dict[str, Any]:
        return self._payload

    async def get_market(self, ticker: str) -> dict[str, Any]:
        return {"market": {"ticker": ticker, "status": "active"}}


def test_unreadable_exposure_is_reported_not_swallowed() -> None:
    """Silent ``None`` was the defect. The parse now hands the caller the raw
    unreadable value so it can fail closed AND alarm."""
    payload = {
        "market_positions": [
            {"ticker": "A", "position_fp": "-5.00", "market_exposure_dollars": "3.10"},
            {"ticker": "B", "position_fp": "-5.00", "market_exposure_dollars": "wat"},
            {"ticker": "C", "position_fp": "-5.00"},  # field absent entirely
        ]
    }
    by_ticker, unreadable = _exchange_exposure_cc_by_ticker(payload)
    assert by_ticker == {"A": 31_000}
    assert set(unreadable) == {"B", "C"}
    assert unreadable["B"] == "wat"


def test_full_notional_reserve_is_a_proven_upper_bound() -> None:
    """$1.00/contract is the maximum a Kalshi binary can cost, so the booked
    max loss is ≥ any true cost basis for that contract count."""
    for contracts in (1, 7, 500, 32_717):
        pos = reserve_at_full_notional("T", Side.NO, contracts)
        assert pos is not None
        assert pos.risk_modeled is False
        assert pos.max_loss_cc == contracts * 10_000 // 100
        assert pos.max_loss_cc >= contracts * 9_999 // 100
    assert reserve_at_full_notional("T", Side.NO, 0) is None


async def test_runtime_reconcile_unparseable_exposure_raises_det_max_and_alarms(
    tmp_path: Path,
) -> None:
    """THE REGRESSION, runtime half. An exchange position whose at-risk figure
    cannot be parsed must still contribute a NON-ZERO amount to
    ``deterministic_max_loss_cc`` (via the book model's reserved loss) and must
    fire a metric + an error log. Booking zero is the fail-open defect."""
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        metrics = Metrics(FakeClock())
        before = build_book_model(
            exposure.positions.values(), marginals=lambda _t: 0.5
        ).reserved_loss_cc
        assert before == 0.0

        rest = _StubRest(
            {
                "market_positions": [
                    {
                        "ticker": _ORPHAN,
                        "position_fp": "-5.00",
                        # Unreadable at-risk figure of any kind.
                        "market_exposure_dollars": "not-a-number",
                    }
                ]
            }
        )
        with structlog.testing.capture_logs() as cap:
            unmodeled = await position_reconcile_unmodeled_once(
                cast(Any, rest), exposure, store, metrics, subaccount=0
            )

        assert unmodeled == [_ORPHAN]
        pos = exposure.positions[f"reserve:{_ORPHAN}"]
        assert pos.max_loss_cc == 50_000  # 500 centi × $1.00
        after = build_book_model(
            exposure.positions.values(), marginals=lambda _t: 0.5
        ).reserved_loss_cc
        assert after == 50_000.0
        assert after - before > 0.0  # the whole point

        counters = metrics.snapshot()["counters"]
        assert counters["exchange_exposure.unreadable"] == 1
        assert counters["exchange_exposure.reserved_at_full_notional"] == 1
        alarms = [e for e in cap if e["event"] == "exchange_exposure_unreadable"]
        assert len(alarms) == 1 and alarms[0]["log_level"] == "error"
        floored = [
            e for e in cap if e["event"] == "position_reconcile_reserved_at_full_notional"
        ]
        assert len(floored) == 1 and floored[0]["ticker"] == _ORPHAN
        report = [e for e in cap if e["event"] == "position_reconcile_unmodeled"][0]
        assert report["adopted_as_reserve"] == [_ORPHAN]
        assert report["full_notional_fallback"] == [_ORPHAN]
        assert report["alarm_only"] == []
    finally:
        await store.close()


async def test_boot_rehydrate_six_decimal_exposure_now_reserves(
    tmp_path: Path,
) -> None:
    """THE REGRESSION, boot half. A six-decimal exposure string used to make the
    boot rehydrator drop the position entirely; it now reserves it at a premium
    ≥ the exchange's figure, with NO full-notional fallback needed."""
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        metrics = Metrics(FakeClock())
        rest = _StubRest(
            {
                "market_positions": [
                    {
                        "ticker": _ORPHAN,
                        "position_fp": "-3.19",
                        "market_exposure_dollars": "2.688790",  # live string
                    }
                ]
            }
        )
        with structlog.testing.capture_logs() as cap:
            await QuoteApp._rehydrate_exposure_book(
                cast(Any, None), cast(Any, rest), store, exposure, metrics=metrics
            )
        pos = exposure.positions[f"reserve:{_ORPHAN}"]
        # 2.688790 → ceil(26887.90) = 26_888 cc; entry = ceil(26_888*100/319).
        assert pos.max_loss_cc >= 26_888
        assert pos.max_loss_cc < 26_888 + 319  # ≤1cc/contract of ceil slack
        assert metrics.snapshot()["counters"].get("exchange_exposure.unreadable") is None
        assert not [e for e in cap if e["event"] == "exchange_exposure_unreadable"]
    finally:
        await store.close()
