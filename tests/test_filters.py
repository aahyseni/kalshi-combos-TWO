from datetime import UTC, datetime

from combomaker.core.clock import FakeClock
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.marketdata.feed import OrderbookFeed
from combomaker.marketdata.grid import PriceGrid
from combomaker.marketdata.metadata import MarketMeta, MetadataCache
from combomaker.ops.config import FiltersConfig
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.models import Rfq
from combomaker.risk.killswitch import KillSwitch
from tests.test_feed import FakeWs, snapshot_env

CC = CentiCents
Q = CentiContracts

NOW = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)


def combo_rfq(**overrides: object) -> Rfq:
    msg: dict[str, object] = {
        "id": "rfq_1",
        "market_ticker": "KXMVE-C1",
        "created_ts": "2026-07-05T10:00:00Z",
        "contracts_fp": "100.00",
        "mve_collection_ticker": "KXMVESPORTS",
        "mve_selected_legs": [
            {"market_ticker": "M1", "side": "yes"},
            {"market_ticker": "M2", "side": "no"},
        ],
    }
    msg.update(overrides)
    return Rfq.from_ws(msg)


class Harness:
    def __init__(self, config: FiltersConfig | None = None) -> None:
        self.clock = FakeClock(start=NOW)
        self.ws = FakeWs()
        self.feed = OrderbookFeed(self.ws, self.clock)
        self.metadata = MetadataCache(None, self.clock)  # type: ignore[arg-type]
        self.killswitch = KillSwitch(self.clock)
        self.filter = RfqFilter(
            config or FiltersConfig(), self.feed, self.metadata, self.killswitch, self.clock
        )

    async def with_books(self, tickers: list[str]) -> None:
        """Snapshot each ticker with a TIGHT book (spread $0.02, decent depth)."""
        self.feed.watch(tickers)
        await self.ws.ack_subscription(0, 5)
        for i, ticker in enumerate(tickers):
            env = snapshot_env(5, i + 1, ticker)
            env["msg"]["yes_dollars_fp"] = [["0.3000", "50.00"], ["0.4700", "20.00"]]
            env["msg"]["no_dollars_fp"] = [["0.4000", "60.00"], ["0.5100", "25.00"]]
            await self.ws.deliver(env)

    # Default close 6h out: past BOTH the 1h min_time_to_close gate AND the
    # Phase 3 pregame gate (estimated start = close − 4.5h = NOW + 1.5h).
    def with_meta(self, ticker: str, *, close_in_s: float | None = 21_600.0) -> None:
        close = None
        if close_in_s is not None:
            from datetime import timedelta

            close = NOW + timedelta(seconds=close_in_s)
        self.metadata._markets[ticker] = MarketMeta(  # noqa: SLF001 (test seam)
            ticker=ticker,
            status="active",
            grid=PriceGrid.from_market_payload(
                {
                    "ticker": ticker,
                    "price_ranges": [{"start": "0.01", "end": "0.99", "step": "0.01"}],
                }
            ),
            event_ticker="E",
            close_time=close,
            expected_expiration_time=None,
            raw={},
            fetched_mono_ns=self.clock.monotonic_ns(),
        )


async def ready_harness(config: FiltersConfig | None = None) -> Harness:
    # Synthetic M1/M2 legs; the series gate has its own dedicated tests below.
    cfg = (config or FiltersConfig()).model_copy(
        update={"allowed_leg_series_prefixes": None})
    h = Harness(cfg)
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    return h


async def test_clean_combo_passes() -> None:
    h = await ready_harness()
    assert h.filter.evaluate(combo_rfq()) == []


async def test_halted_skips() -> None:
    h = await ready_harness()
    await h.killswitch.halt(ReasonCode.HALT_MANUAL)
    assert ReasonCode.SKIP_HALTED in h.filter.evaluate(combo_rfq())


async def test_non_combo_skipped_when_combos_only() -> None:
    h = await ready_harness()
    rfq = combo_rfq(mve_collection_ticker=None, mve_selected_legs=[])
    assert ReasonCode.SKIP_NOT_WHITELISTED in h.filter.evaluate(rfq)


async def test_whitelist_prefix() -> None:
    h = await ready_harness(FiltersConfig(collection_whitelist=["KXMVENBA"]))
    assert ReasonCode.SKIP_NOT_WHITELISTED in h.filter.evaluate(combo_rfq())
    h2 = await ready_harness(FiltersConfig(collection_whitelist=["KXMVESPORTS", "KXMVENBA"]))
    assert h2.filter.evaluate(combo_rfq()) == []


async def test_leg_count_gate() -> None:
    h = await ready_harness(FiltersConfig(max_legs=1))
    assert ReasonCode.SKIP_TOO_MANY_LEGS in h.filter.evaluate(combo_rfq())


async def test_size_gates() -> None:
    h = await ready_harness()
    small = combo_rfq(contracts_fp="0.50")
    assert ReasonCode.SKIP_SIZE_BELOW_MIN in h.filter.evaluate(small)
    big = combo_rfq(contracts_fp="999999.00")
    assert ReasonCode.SKIP_SIZE_ABOVE_MAX in h.filter.evaluate(big)


async def test_target_cost_gates() -> None:
    h = await ready_harness()
    rfq = combo_rfq(target_cost_dollars="0.50", contracts_fp=None)
    assert ReasonCode.SKIP_SIZE_BELOW_MIN in h.filter.evaluate(rfq)


async def test_no_sizing_mode_is_unknown() -> None:
    h = await ready_harness()
    rfq = combo_rfq(contracts_fp=None)
    assert ReasonCode.SKIP_CLASSIFIER_UNKNOWN in h.filter.evaluate(rfq)


async def test_unknown_leg_side_is_unknown_classifier() -> None:
    h = await ready_harness()
    rfq = combo_rfq(
        mve_selected_legs=[
            {"market_ticker": "M1", "side": "maybe"},
            {"market_ticker": "M2", "side": "no"},
        ]
    )
    assert ReasonCode.SKIP_CLASSIFIER_UNKNOWN in h.filter.evaluate(rfq)


async def test_unwatched_leg_book() -> None:
    h = Harness()
    await h.with_books(["M1"])  # M2 never watched
    h.with_meta("M1")
    h.with_meta("M2")
    assert ReasonCode.SKIP_LEG_UNKNOWN in h.filter.evaluate(combo_rfq())


async def test_invalid_leg_book_is_stale() -> None:
    h = await ready_harness()
    h.feed.book("M2").invalidate("test")
    assert ReasonCode.SKIP_LEG_STALE in h.filter.evaluate(combo_rfq())


async def test_wide_leg_spread() -> None:
    h = await ready_harness(FiltersConfig(max_leg_spread_cc=100))
    # tight harness book: yes bid 0.47, yes ask 1-0.51=0.49 => spread 200cc > 100cc
    assert ReasonCode.SKIP_LEG_SPREAD_TOO_WIDE in h.filter.evaluate(combo_rfq())


async def test_thin_leg_book() -> None:
    h = await ready_harness(FiltersConfig(min_leg_depth_contracts=1000.0))
    assert ReasonCode.SKIP_LEG_BOOK_THIN in h.filter.evaluate(combo_rfq())


async def test_missing_close_time_is_unknown() -> None:
    h = await ready_harness()
    h.with_meta("M2", close_in_s=None)
    assert ReasonCode.SKIP_CLASSIFIER_UNKNOWN in h.filter.evaluate(combo_rfq())


async def test_event_too_close_is_in_play() -> None:
    h = await ready_harness()
    h.with_meta("M1", close_in_s=600.0)  # 10 min out < 1h gate
    assert ReasonCode.SKIP_IN_PLAY in h.filter.evaluate(combo_rfq())


async def test_ws_unhealthy() -> None:
    h = await ready_harness()
    h.ws._healthy = False
    assert ReasonCode.SKIP_WS_UNHEALTHY in h.filter.evaluate(combo_rfq())


async def test_multiple_reasons_all_reported() -> None:
    h = await ready_harness(FiltersConfig(max_legs=1, min_contracts=1000.0))
    reasons = h.filter.evaluate(combo_rfq())
    assert ReasonCode.SKIP_TOO_MANY_LEGS in reasons
    assert ReasonCode.SKIP_SIZE_BELOW_MIN in reasons


# --- two-legged-tie (UCL/UEL/UECL) regime gate --------------------------------


def _regime_combo(tk: str) -> Rfq:
    return combo_rfq(mve_selected_legs=[
        {"market_ticker": tk, "side": "yes"},
        {"market_ticker": "KXWCTOTAL-26JUL09FRAMAR-3", "side": "yes"},
    ])


def test_ucl_uel_uecl_legs_declined_as_unmodeled_regime() -> None:
    h = Harness()
    for tk in ("KXUCLGAME-26AUG12REALARS-REAL", "KXUELGAME-26AUG12ABCDEF-ABC",
               "KXUECLGAME-26AUG12GHIJKL-GHI"):
        assert ReasonCode.SKIP_UNMODELED_REGIME in h.filter.evaluate(_regime_combo(tk)), tk


def test_pure_wc_combo_not_declined_for_regime() -> None:
    h = Harness()
    rfq = combo_rfq(mve_selected_legs=[
        {"market_ticker": "KXWCADVANCE-26JUL09FRAMAR-FRA", "side": "yes"},
        {"market_ticker": "KXWCTOTAL-26JUL09FRAMAR-3", "side": "yes"},
    ])
    assert ReasonCode.SKIP_UNMODELED_REGIME not in h.filter.evaluate(rfq)


def test_regime_gate_can_be_disabled() -> None:
    h = Harness(FiltersConfig(decline_two_legged_tie=False))
    assert ReasonCode.SKIP_UNMODELED_REGIME not in h.filter.evaluate(
        _regime_combo("KXUCLGAME-26AUG12REALARS-REAL")
    )


# --- Leg-series allowlist (operator directive 2026-07-11, judge finding F1:
# collections mix sports, so crypto/esports/unmodeled-league legs reached the
# pricer and priced at flat priors instead of declining) ---


def _legs(*tickers: str) -> list[dict[str, str]]:
    return [{"market_ticker": t, "side": "yes"} for t in tickers]


def test_series_gate_blocks_crypto_leg() -> None:
    h = Harness()
    rfq = combo_rfq(mve_selected_legs=_legs(
        "KXWCGAME-26JUL09FRAMAR-FRA", "KXETH15M-26JUL091200-T3100"))
    assert ReasonCode.SKIP_SERIES_NOT_ALLOWED in h.filter.evaluate(rfq)


def test_series_gate_blocks_soccer_lookalike_series() -> None:
    # The F4 masquerade class: esports / club / minor-league series that match
    # soccer keywords but must NOT inherit WC priors until deliberately
    # unblocked (classification + priors first).
    h = Harness()
    for tk in ("KXEWCGAME-26AUG12ABCDEF-ABC", "KXCLUBWCGAME-26AUG12REALARS-REAL",
               "KXUAEPLGAME-26AUG12GHIJKL-GHI"):
        rfq = combo_rfq(mve_selected_legs=_legs(tk, "KXWCTOTAL-26JUL09FRAMAR-3"))
        assert ReasonCode.SKIP_SERIES_NOT_ALLOWED in h.filter.evaluate(rfq), tk


def test_series_gate_passes_wc_and_mlb() -> None:
    h = Harness()
    rfq = combo_rfq(mve_selected_legs=_legs(
        "KXWCGAME-26JUL09FRAMAR-FRA", "KXMLBGAME-26JUL091840NYYTB-NYY"))
    assert ReasonCode.SKIP_SERIES_NOT_ALLOWED not in h.filter.evaluate(rfq)


def test_series_gate_none_disables() -> None:
    h = Harness(FiltersConfig(allowed_leg_series_prefixes=None))
    rfq = combo_rfq(mve_selected_legs=_legs("KXETH15M-26JUL091200-T3100", "M2"))
    assert ReasonCode.SKIP_SERIES_NOT_ALLOWED not in h.filter.evaluate(rfq)


def test_series_gate_empty_list_blocks_everything_fail_closed() -> None:
    h = Harness(FiltersConfig(allowed_leg_series_prefixes=[]))
    rfq = combo_rfq(mve_selected_legs=_legs(
        "KXWCGAME-26JUL09FRAMAR-FRA", "KXWCTOTAL-26JUL09FRAMAR-3"))
    assert ReasonCode.SKIP_SERIES_NOT_ALLOWED in h.filter.evaluate(rfq)


def test_series_gate_unblock_via_config_prefix() -> None:
    # The easy-UNBLOCK path: one YAML prefix re-admits a competition.
    h = Harness(FiltersConfig(allowed_leg_series_prefixes=["KXWC", "KXMLB", "KXUCL"]))
    rfq = _regime_combo("KXUCLGAME-26AUG12REALARS-REAL")
    assert ReasonCode.SKIP_SERIES_NOT_ALLOWED not in h.filter.evaluate(rfq)
