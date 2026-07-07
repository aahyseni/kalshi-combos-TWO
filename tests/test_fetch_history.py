"""Crash-safety and fail-closed error handling of the Kalshi history fetcher
(tools/fetch_kalshi_history.py). These guard the offline calibration/gate
datasets, not live pricing — but a silent wrong-main-line or a 0-byte file that
bricks every future run corrupts the evidence the enablement gates rest on."""

from __future__ import annotations

import asyncio

import pytest

import tools.fetch_kalshi_history as fetch
from combomaker.exchange.rest import KalshiApiError, RateLimitedError

HEADER = "game_code,team,p_team_close,team_won,total_line,p_over_close,went_over\n"


class _FakeRest:
    """Drives pregame_mid: behavior(attempt_index) returns a payload or raises."""

    def __init__(self, behavior) -> None:  # type: ignore[no-untyped-def]
        self._behavior = behavior
        self.calls = 0

    async def get_candlesticks(self, series, ticker, **kw):  # type: ignore[no-untyped-def]
        i = self.calls
        self.calls += 1
        return self._behavior(i)


def _candle(bid: str, ask: str) -> dict:
    return {"yes_bid": {"close_dollars": bid}, "yes_ask": {"close_dollars": ask}}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch, "_THROTTLE_S", 0.0)
    monkeypatch.setattr(fetch, "_RETRY_BACKOFF_S", 0.0)


class TestPregameMid:
    def test_returns_mid_from_last_valid_candle(self) -> None:
        rest = _FakeRest(lambda i: {"candlesticks": [_candle("0.40", "0.44")]})
        mid = asyncio.run(fetch.pregame_mid(rest, "S", "T", 1000))
        assert mid == pytest.approx(0.42)

    def test_empty_window_is_none_not_error(self) -> None:
        rest = _FakeRest(lambda i: {"candlesticks": []})
        assert asyncio.run(fetch.pregame_mid(rest, "S", "T", 1000)) is None
        assert rest.calls == 1  # no retry on a legitimate empty window

    def test_persistent_429_raises_probe_error_after_retries(self) -> None:
        def boom(i: int) -> dict:
            raise RateLimitedError(429, "too_many_requests", "slow down")

        rest = _FakeRest(boom)
        with pytest.raises(fetch._ProbeError):
            asyncio.run(fetch.pregame_mid(rest, "S", "T", 1000))
        assert rest.calls == fetch._RETRY_ATTEMPTS  # retried, not silently dropped

    def test_transient_429_then_success_recovers(self) -> None:
        def flaky(i: int) -> dict:
            if i < 2:
                raise RateLimitedError(429, "", "")
            return {"candlesticks": [_candle("0.49", "0.51")]}

        rest = _FakeRest(flaky)
        assert asyncio.run(fetch.pregame_mid(rest, "S", "T", 1000)) == pytest.approx(0.50)
        assert rest.calls == 3

    def test_non_transient_error_raises_immediately(self) -> None:
        def bad(i: int) -> dict:
            raise KalshiApiError(400, "bad_request", "nope")

        rest = _FakeRest(bad)
        with pytest.raises(fetch._ProbeError):
            asyncio.run(fetch.pregame_mid(rest, "S", "T", 1000))
        assert rest.calls == 1  # 4xx is not retried

    def test_5xx_is_retried(self) -> None:
        def boom(i: int) -> dict:
            raise KalshiApiError(503, "unavailable", "later")

        rest = _FakeRest(boom)
        with pytest.raises(fetch._ProbeError):
            asyncio.run(fetch.pregame_mid(rest, "S", "T", 1000))
        assert rest.calls == fetch._RETRY_ATTEMPTS

    def test_transport_error_is_retried(self) -> None:
        # aiohttp connection resets under load are OSError/ClientError, NOT
        # KalshiApiError — an uncaught one crashed a heavy run. Must be retried.
        import aiohttp

        def boom(i: int) -> dict:
            raise aiohttp.ClientConnectionError("connection reset by peer")

        rest = _FakeRest(boom)
        with pytest.raises(fetch._ProbeError):
            asyncio.run(fetch.pregame_mid(rest, "S", "T", 1000))
        assert rest.calls == fetch._RETRY_ATTEMPTS

    def test_transport_error_then_success_recovers(self) -> None:
        def flaky(i: int) -> dict:
            if i < 1:
                raise OSError("connection reset")
            return {"candlesticks": [_candle("0.49", "0.51")]}

        rest = _FakeRest(flaky)
        assert asyncio.run(fetch.pregame_mid(rest, "S", "T", 1000)) == pytest.approx(0.50)
        assert rest.calls == 2


class TestFileSafety:
    def test_need_header_on_missing_and_empty(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        p = tmp_path / "h.csv"
        assert fetch._need_header(p) is True          # missing
        p.write_text("", encoding="utf-8")
        assert fetch._need_header(p) is True           # 0-byte crash artifact
        p.write_text(HEADER, encoding="utf-8")
        assert fetch._need_header(p) is False

    def test_repair_trailing_newline_closes_torn_row(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        p = tmp_path / "h.csv"
        p.write_text(HEADER + "26JUL05INDLV,LV,0.57", encoding="utf-8")  # torn, no \n
        fetch._repair_trailing_newline(p)
        assert p.read_bytes().endswith(b"\r\n")
        # a well-formed file is left untouched (idempotent)
        good = HEADER
        p.write_text(good, encoding="utf-8")
        fetch._repair_trailing_newline(p)
        assert p.read_text(encoding="utf-8") == good

    def test_done_codes_empty_and_missing_are_empty_sets(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        p = tmp_path / "h.csv"
        assert fetch.done_codes(p) == set()            # missing
        p.write_text("", encoding="utf-8")
        assert fetch.done_codes(p) == set()            # 0-byte

    def test_done_codes_reads_game_codes(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        p = tmp_path / "h.csv"
        p.write_text(HEADER + "26JUL05INDLV,LV,0.57,0,186.5,0.36,0\n", encoding="utf-8")
        assert fetch.done_codes(p) == {"26JUL05INDLV"}

    def test_done_codes_fails_loud_on_headerless_file(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        p = tmp_path / "h.csv"
        # a headerless data row (the pre-fix 0-byte-then-append corruption)
        p.write_text("26JUL05INDLV,LV,0.57,0,186.5,0.36,0\n", encoding="utf-8")
        with pytest.raises(ValueError, match="no game_code header"):
            fetch.done_codes(p)
