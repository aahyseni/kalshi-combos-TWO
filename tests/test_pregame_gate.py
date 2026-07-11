"""Phase 3 — pregame-only quote gate (operator directive 2026-07-10).

Covers: the MLB embedded-ET-start parse (real prod fixture tickers, values
cross-checked against the live API evidence of 2026-07-10), the
expiry-minus-offset estimate path (soccer + per-prefix overrides), UNKNOWN ⇒
decline, the allow_inplay_legs re-enable flag, the start==now boundary, the
last-look straddle re-check, coexistence with the market-motion detector, and
the quiet-failure-defense-#2 property: with the gate active an RFQ carrying an
in-play or unknown-start leg literally CANNOT reach quote creation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from combomaker.core.money import CentiCents
from combomaker.core.reasons import ReasonCode
from combomaker.ops.config import FiltersConfig
from combomaker.ops.persistence import Store
from combomaker.rfq.models import Rfq
from combomaker.rfq.pregame import embedded_start_time
from tests.test_filters import Harness, combo_rfq
from tests.test_lifecycle import Rig, accepted_msg
from tests.test_pricing_engine import CROSS_EVENT_LEGS, combo, seed_event

# ---------------------------------------------------------------------------
# (a) MLB embedded-start parse — REAL prod tickers (tape 2026-07-10); expected
# UTC instants are the live-API cross-check: expected_expiration_time minus
# exactly 3h on every probed market (see the Phase 3 report).
# ---------------------------------------------------------------------------

EMBEDDED_FIXTURES = [
    ("KXMLBGAME-26JUL101915BOSNYM-BOS", datetime(2026, 7, 10, 23, 15, tzinfo=UTC)),
    ("KXMLBKS-26JUL091235ATLPIT-ATLBELDER55-2", datetime(2026, 7, 9, 16, 35, tzinfo=UTC)),
    ("KXMLBRFI-26JUL101845NYYWSH", datetime(2026, 7, 10, 22, 45, tzinfo=UTC)),
    ("KXMLBHIT-26JUL101840MILPIT-MILBTURANG2-1", datetime(2026, 7, 10, 22, 40, tzinfo=UTC)),
    # 21:45 EDT crosses UTC midnight (API exp 2026-07-10T04:45Z − 3h).
    ("KXMLBTOTAL-26JUL092145COLSF-11", datetime(2026, 7, 10, 1, 45, tzinfo=UTC)),
]


@pytest.mark.parametrize(("ticker", "expected_utc"), EMBEDDED_FIXTURES)
def test_embedded_start_parses_verified_eastern(ticker: str, expected_utc: datetime) -> None:
    start = embedded_start_time(ticker)
    assert start is not None
    assert start.astimezone(UTC) == expected_utc


def test_embedded_start_handles_est_after_dst_ends() -> None:
    # November game: EST (UTC-5), not the July EDT (UTC-4) — zoneinfo owns DST.
    start = embedded_start_time("KXMLBGAME-26NOV021905AAABBB-AAA")
    assert start is not None
    assert start.astimezone(UTC) == datetime(2026, 11, 3, 0, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    "ticker",
    [
        "KXWC1H-26JUL11ARGSUI-ARG",           # unverified series: NEVER trusted
        "KXWCTOTAL-26JUL09FRAMAR-3",
        "KXWCGAME-26JUL09FRAMAR-FRA",
        "KXMLBGAME-26JUL10BOSNYM-BOS",        # no time token
        "KXMLBGAME-26JUL102500BOSNYM-BOS",    # hour 25
        "KXMLBGAME-26JUL101070BOSNYM-BOS",    # minute 70
        "KXMLBGAME-26XXX101915BOSNYM-BOS",    # bad month
        "KXMLBGAME-26FEB301915BOSNYM-BOS",    # impossible date
        "KXMLBGAME",                          # no game-code segment
        "M1",                                 # plain test ticker
    ],
)
def test_embedded_start_refuses_unverified_or_malformed(ticker: str) -> None:
    assert embedded_start_time(ticker) is None


# ---------------------------------------------------------------------------
# Filter-level gate semantics (Harness NOW = 2026-07-05 12:00 UTC; default
# meta close = 6h out ⇒ estimated start = NOW + 1.5h ⇒ pregame).
# ---------------------------------------------------------------------------


def _pregame_codes(reasons: list[ReasonCode]) -> set[ReasonCode]:
    return set(reasons) & {ReasonCode.SKIP_INPLAY_LEG, ReasonCode.SKIP_START_TIME_UNKNOWN}


async def pregame_harness(config: FiltersConfig | None = None) -> Harness:
    # Synthetic M1/M2 legs; the series gate has dedicated tests in test_filters.
    cfg = (config or FiltersConfig()).model_copy(
        update={"allowed_leg_series_prefixes": None})
    h = Harness(cfg)
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    return h


async def test_pregame_combo_passes_cleanly() -> None:
    h = await pregame_harness()
    assert h.filter.evaluate(combo_rfq()) == []


async def test_estimated_started_leg_declines() -> None:
    h = await pregame_harness()
    h.with_meta("M1", close_in_s=7200.0)  # close 2h out − 4.5h offset ⇒ started
    assert ReasonCode.SKIP_INPLAY_LEG in h.filter.evaluate(combo_rfq())


async def test_boundary_start_equals_now_declines() -> None:
    # close − offset == NOW exactly: now >= start ⇒ in-play, not pregame.
    h = await pregame_harness()
    h.with_meta("M1", close_in_s=4.5 * 3600.0)
    assert ReasonCode.SKIP_INPLAY_LEG in h.filter.evaluate(combo_rfq())


async def test_one_second_before_start_passes() -> None:
    h = await pregame_harness()
    h.with_meta("M1", close_in_s=4.5 * 3600.0 + 1.0)
    assert ReasonCode.SKIP_INPLAY_LEG not in h.filter.evaluate(combo_rfq())


async def test_unknown_start_time_declines() -> None:
    h = await pregame_harness()
    h.with_meta("M1", close_in_s=None)  # meta present, but no time anchors
    assert ReasonCode.SKIP_START_TIME_UNKNOWN in h.filter.evaluate(combo_rfq())


async def test_missing_metadata_is_unknown_start() -> None:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M2")  # M1 has a book but NO metadata at all
    assert ReasonCode.SKIP_START_TIME_UNKNOWN in h.filter.evaluate(combo_rfq())


async def test_allow_inplay_legs_stands_the_gate_down() -> None:
    # Same in-play-by-estimate combo; the config flag re-enables quoting
    # WITHOUT code changes (min_time_to_close_s still applies: 2h > 1h ok).
    h = await pregame_harness(FiltersConfig(allow_inplay_legs=True))
    h.with_meta("M1", close_in_s=7200.0)
    h.with_meta("M2", close_in_s=7200.0)
    assert h.filter.evaluate(combo_rfq()) == []


async def test_allow_inplay_legs_also_waives_unknown_start() -> None:
    # Gate off ⇒ start times are irrelevant, including UNKNOWN ones. The old
    # close-time gate still flags the missing close as UNKNOWN — only the
    # pregame codes stand down.
    h = await pregame_harness(FiltersConfig(allow_inplay_legs=True))
    h.with_meta("M1", close_in_s=None)
    assert _pregame_codes(h.filter.evaluate(combo_rfq())) == set()


# --- soccer estimate path (no embedded start ⇒ expiry − offset) --------------

WC_LEGS = [
    {"market_ticker": "KXWCGAME-26JUL09FRAMAR-FRA", "side": "yes"},
    {"market_ticker": "KXWCTOTAL-26JUL09FRAMAR-3", "side": "yes"},
]


async def wc_harness(config: FiltersConfig | None = None) -> Harness:
    h = Harness(config)
    tickers = [leg["market_ticker"] for leg in WC_LEGS]
    await h.with_books(tickers)
    for t in tickers:
        h.with_meta(t)
    return h


async def test_soccer_pregame_estimate_passes() -> None:
    h = await wc_harness()  # default 6h close ⇒ estimated kickoff +1.5h
    reasons = h.filter.evaluate(combo_rfq(mve_selected_legs=WC_LEGS))
    assert _pregame_codes(reasons) == set()


async def test_soccer_started_by_estimate_declines() -> None:
    h = await wc_harness()
    h.with_meta("KXWCTOTAL-26JUL09FRAMAR-3", close_in_s=4 * 3600.0)  # −4.5h ⇒ started
    reasons = h.filter.evaluate(combo_rfq(mve_selected_legs=WC_LEGS))
    assert ReasonCode.SKIP_INPLAY_LEG in reasons


async def test_soccer_offset_prefix_override_applies() -> None:
    # Same 4h-out anchor, but a KXWC-specific 3.0h offset ⇒ kickoff estimate
    # +1h ⇒ pregame. Proves per-series tuning needs no code change.
    h = await wc_harness(
        FiltersConfig(pregame_start_offset_hours_by_prefix={"KXWC": 3.0})
    )
    h.with_meta("KXWCTOTAL-26JUL09FRAMAR-3", close_in_s=4 * 3600.0)
    reasons = h.filter.evaluate(combo_rfq(mve_selected_legs=WC_LEGS))
    assert _pregame_codes(reasons) == set()


# --- MLB embedded path precedence --------------------------------------------

MLB_FUTURE = "KXMLBGAME-26JUL101915BOSNYM-BOS"   # starts Jul 10 (NOW = Jul 5)
MLB_STARTED = "KXMLBGAME-26JUL041915BOSNYM-BOS"  # started Jul 4


async def mlb_harness(ticker: str, config: FiltersConfig | None = None) -> Harness:
    h = Harness(config)
    await h.with_books([ticker, "M2"])
    h.with_meta(ticker)
    h.with_meta("M2")
    return h


def mlb_combo(ticker: str) -> Rfq:
    return combo_rfq(
        mve_selected_legs=[
            {"market_ticker": ticker, "side": "yes"},
            {"market_ticker": "M2", "side": "no"},
        ]
    )


async def test_mlb_embedded_start_beats_late_estimate() -> None:
    # close only 2h out would say "started" by estimate, but the VERIFIED
    # embedded start (Jul 10) wins the chain: still pregame on Jul 5.
    h = await mlb_harness(MLB_FUTURE)
    h.with_meta(MLB_FUTURE, close_in_s=7200.0)
    assert _pregame_codes(h.filter.evaluate(mlb_combo(MLB_FUTURE))) == set()


async def test_mlb_embedded_started_declines_despite_far_close() -> None:
    h = await mlb_harness(MLB_STARTED)
    h.with_meta(MLB_STARTED, close_in_s=100 * 3600.0)  # estimate would say pregame
    assert ReasonCode.SKIP_INPLAY_LEG in h.filter.evaluate(mlb_combo(MLB_STARTED))


async def test_mlb_unparseable_code_falls_to_prefix_estimate() -> None:
    # Chain (a) fails ⇒ chain (b) with the KXMLB 4.0h override: close 5h out
    # ⇒ first pitch estimate +1h ⇒ pregame; close 3h out ⇒ −1h ⇒ declined.
    bad = "KXMLBGAME-BADCODE-BOS"
    h = await mlb_harness(bad)
    h.with_meta(bad, close_in_s=5 * 3600.0)
    assert _pregame_codes(h.filter.evaluate(mlb_combo(bad))) == set()
    h.with_meta(bad, close_in_s=3 * 3600.0)
    assert ReasonCode.SKIP_INPLAY_LEG in h.filter.evaluate(mlb_combo(bad))


# ---------------------------------------------------------------------------
# PROPERTY (quiet-failure defense #2): with the gate active, an RFQ with an
# in-play or unknown-start leg CANNOT reach quote creation — asserted at the
# QuoteSender seam, with the gate's own reason confirmed as the cause.
# ---------------------------------------------------------------------------


async def _rig(tmp_path: Path, filters: FiltersConfig | None = None) -> Rig:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / "t.sqlite3", h.clock)
    return Rig(h, store, filters)


async def test_property_baseline_pregame_quotes(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)  # sweep is non-vacuous: the clean combo quotes
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS))
    assert len(rig.sender.created) == 1


@pytest.mark.parametrize(
    ("close_in_s", "expected"),
    [
        (7200.0, ReasonCode.SKIP_INPLAY_LEG),          # started by estimate
        (4.5 * 3600.0, ReasonCode.SKIP_INPLAY_LEG),    # boundary: start == now
        (None, ReasonCode.SKIP_START_TIME_UNKNOWN),    # no usable source
    ],
    ids=["estimate_started", "boundary_now", "unknown_start"],
)
async def test_property_gated_rfq_never_reaches_create_quote(
    tmp_path: Path, close_in_s: float | None, expected: ReasonCode
) -> None:
    rig = await _rig(tmp_path)
    rig.h.with_meta("M1", close_in_s=close_in_s)
    rfq = combo(CROSS_EVENT_LEGS)
    assert expected in rig.lifecycle._filter.evaluate(rfq)  # noqa: SLF001 (cause)
    await rig.lifecycle.handle_rfq(rfq)
    assert rig.sender.created == []                          # effect


async def test_property_missing_meta_never_reaches_create_quote(
    tmp_path: Path,
) -> None:
    rig = await _rig(tmp_path)
    del rig.h.metadata._markets["M1"]  # noqa: SLF001 (test seam)
    rfq = combo(CROSS_EVENT_LEGS)
    assert ReasonCode.SKIP_START_TIME_UNKNOWN in rig.lifecycle._filter.evaluate(  # noqa: SLF001
        rfq
    )
    await rig.lifecycle.handle_rfq(rfq)
    assert rig.sender.created == []


async def test_property_flag_true_reenables_quoting(tmp_path: Path) -> None:
    rig = await _rig(
        tmp_path, FiltersConfig(min_time_to_close_s=0.0, allow_inplay_legs=True)
    )
    rig.h.with_meta("M1", close_in_s=7200.0)  # in-play by estimate
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS))
    assert len(rig.sender.created) == 1


# ---------------------------------------------------------------------------
# Straddle safety: a leg going in-play BETWEEN quote and accept is caught by
# the last-look re-check and never confirmed.
# ---------------------------------------------------------------------------


async def test_straddle_leg_starts_after_quote_declines_at_confirm(
    tmp_path: Path,
) -> None:
    rig = await _rig(tmp_path)
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS))
    assert len(rig.sender.created) == 1        # pregame at quote time
    rig.h.clock.advance(2 * 3600.0)            # past estimated start (+1.5h)
    await rig.lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    assert rig.sender.confirmed == []          # deliberate lapse, never confirm
    assert rig.metrics.counter(
        f"confirm.declined.{ReasonCode.DECLINE_INPLAY_LEG}"
    ) == 1


async def test_motion_detector_still_declines_independently(tmp_path: Path) -> None:
    # The Phase 4 market-motion detector is UNTOUCHED and still fires on its
    # own (higher in the severity ladder than the schedule gate).
    rig = await _rig(tmp_path)
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS))
    inplay = rig.lifecycle._inplay  # noqa: SLF001 (test seam)
    inplay.note_mid("M1", CentiCents(4_000))
    inplay.note_mid("M1", CentiCents(4_400))   # 400cc jump > 300cc threshold
    await rig.lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    assert rig.sender.confirmed == []
    assert rig.metrics.counter(
        f"confirm.declined.{ReasonCode.DECLINE_IN_PLAY}"
    ) == 1
