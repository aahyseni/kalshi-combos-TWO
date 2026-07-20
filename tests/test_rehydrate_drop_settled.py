"""DROP-SETTLED-ON-REHYDRATION (2026-07-16 — clears the stale $4.46 reserve).

``_rehydrate_exposure_book`` folds every exchange-reported open position back
into the risk book at startup — including, before this fix, positions on
markets that had ALREADY definitively settled (the reserved 7/14 MLB combo
whose $4.46 kept consuming deterministic-cap headroom). Now each candidate
ticker's Market.status is checked via ``rest.get_market``:

- ``finalized`` (the Market.status FIELD vocabulary for settled — index-scan
  notes; ``settled`` accepted too in case the wire uses the filter-vocabulary
  spelling) ⇒ DROPPED, logged ``rehydrate_dropped_settled``;
- ``closed`` / ``determined`` (closed-but-unsettled — payout not booked yet)
  ⇒ KEPT, today's behaviour: still real risk;
- ANY error (endpoint missing/unreachable, unreadable payload) ⇒ KEPT
  (fail-safe — risk we cannot disprove stays in the caps).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import structlog

from combomaker.ops.quote_app import QuoteApp
from combomaker.risk.exposure import ExposureBook
from tests.test_rehydrate_positions import CONV, IS_ME, _seed_store


class _StubRestWithMarkets:
    """get_positions + get_market with a scripted status per ticker (or an
    Exception to raise). A ticker with no script raises KeyError — the
    fail-safe KEEP branch."""

    def __init__(
        self,
        positions_payload: dict[str, Any],
        market_status: dict[str, Any] | None = None,
    ) -> None:
        self._positions = positions_payload
        self._status = market_status or {}
        self.get_market_calls: list[str] = []

    async def get_positions(self, **params: Any) -> dict[str, Any]:
        return self._positions

    async def get_market(self, ticker: str) -> dict[str, Any]:
        self.get_market_calls.append(ticker)
        scripted = self._status[ticker]  # KeyError ⇒ the error/KEEP branch
        if isinstance(scripted, Exception):
            raise scripted
        return {"market": {"ticker": ticker, "status": scripted}}


_POSITIONS = {"market_positions": [
    {"ticker": "KXMVE-ARG", "position_fp": "-50.00"},
    {"ticker": "KXMVE-ENG", "position_fp": "-40.00"},
]}


async def _rehydrate(store: Any, rest: Any) -> ExposureBook:
    exposure = ExposureBook(CONV, is_me_event=IS_ME)
    await QuoteApp._rehydrate_exposure_book(
        cast(Any, None), cast(Any, rest), store, exposure)
    return exposure


async def test_settled_market_dropped_and_logged(tmp_path: Path) -> None:
    store = await _seed_store(tmp_path)
    try:
        rest = _StubRestWithMarkets(
            _POSITIONS,
            {"KXMVE-ARG": "finalized", "KXMVE-ENG": "active"},
        )
        with structlog.testing.capture_logs() as cap:
            exposure = await _rehydrate(store, rest)
        # Only the still-live position is rehydrated; the settled one is gone.
        assert {p.combo_ticker for p in exposure.positions.values()} == {"KXMVE-ENG"}
        dropped = [e for e in cap if e.get("event") == "rehydrate_dropped_settled"]
        assert len(dropped) == 1
        assert dropped[0]["ticker"] == "KXMVE-ARG"
        assert dropped[0]["status"] == "finalized"
        # A settled market never shows up as an "unmodeled" reconcile warning.
        assert not [e for e in cap if e.get("event") == "rehydrate_unmodeled_positions"]
    finally:
        await store.close()


async def test_settled_spelling_also_drops(tmp_path: Path) -> None:
    # The GET-/markets FILTER vocabulary spelling ("settled") drops too, in
    # case the wire ever returns it on the field.
    store = await _seed_store(tmp_path)
    try:
        rest = _StubRestWithMarkets(
            _POSITIONS,
            {"KXMVE-ARG": "settled", "KXMVE-ENG": "settled"},
        )
        exposure = await _rehydrate(store, rest)
        assert len(exposure.positions) == 0
    finally:
        await store.close()


async def test_closed_but_unsettled_kept(tmp_path: Path) -> None:
    """closed/determined = the payout has NOT landed: still real risk, kept
    exactly as today (only a DEFINITIVELY settled market drops)."""
    store = await _seed_store(tmp_path)
    try:
        rest = _StubRestWithMarkets(
            _POSITIONS,
            {"KXMVE-ARG": "closed", "KXMVE-ENG": "determined"},
        )
        exposure = await _rehydrate(store, rest)
        assert {p.combo_ticker for p in exposure.positions.values()} == {
            "KXMVE-ARG", "KXMVE-ENG"}
    finally:
        await store.close()


async def test_status_error_keeps_position_fail_safe(tmp_path: Path) -> None:
    store = await _seed_store(tmp_path)
    try:
        rest = _StubRestWithMarkets(
            _POSITIONS,
            {"KXMVE-ARG": RuntimeError("exchange 503"),  # poll error ⇒ KEEP
             # KXMVE-ENG unscripted ⇒ KeyError inside get_market ⇒ KEEP
             },
        )
        exposure = await _rehydrate(store, rest)
        assert {p.combo_ticker for p in exposure.positions.values()} == {
            "KXMVE-ARG", "KXMVE-ENG"}
    finally:
        await store.close()


async def test_missing_status_field_keeps_position(tmp_path: Path) -> None:
    class _NoStatusRest(_StubRestWithMarkets):
        async def get_market(self, ticker: str) -> dict[str, Any]:
            self.get_market_calls.append(ticker)
            return {"market": {"ticker": ticker}}  # no status field

    store = await _seed_store(tmp_path)
    try:
        rest = _NoStatusRest(_POSITIONS)
        exposure = await _rehydrate(store, rest)
        # "" is not a settled status ⇒ everything kept (never dropped blind).
        assert len(exposure.positions) == 2
        assert sorted(rest.get_market_calls) == ["KXMVE-ARG", "KXMVE-ENG"]
    finally:
        await store.close()


async def test_legacy_rest_without_get_market_keeps_all(tmp_path: Path) -> None:
    """A rest handle with NO get_market at all (the pre-fix stubs and any
    minimal embedding) must behave exactly as before: AttributeError is the
    error branch ⇒ every position kept."""
    class _PositionsOnly:
        async def get_positions(self, **params: Any) -> dict[str, Any]:
            return _POSITIONS

    store = await _seed_store(tmp_path)
    try:
        exposure = await _rehydrate(store, _PositionsOnly())
        assert len(exposure.positions) == 2
    finally:
        await store.close()
