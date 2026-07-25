"""IN-PLAY SHADOW INSTRUMENTATION (2026-07-25): measurement only — NO quote is
ever sent. Flag OFF (default) = byte-identical (no rows, no pricing calls);
flag ON prices an RFQ skipped SOLELY for in-play reasons via the live engine
(in_play=True) and records ONE would_quotes_inplay row; the skip decision is
recorded exactly as today and survives any shadow-path exception."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from combomaker.core.reasons import ReasonCode
from combomaker.ops.config import FiltersConfig
from combomaker.ops.persistence import Store
from combomaker.rfq.models import Rfq
from tests.test_filters import Harness
from tests.test_lifecycle import Rig
from tests.test_pricing_engine import CROSS_EVENT_LEGS, combo, seed_event

# Harness geometry (tests.test_filters.NOW = 2026-07-05 12:00Z, estimate offset
# 4.5h): M1 close 2h out ⇒ estimated start = NOW − 2.5h ⇒ IN-PLAY (and 2h ≥ the
# 1h min_time_to_close bar, so SKIP_IN_PLAY does NOT fire — the skip is SOLELY
# skip_inplay_leg). M2 keeps the default 6h close ⇒ start = NOW + 1.5h, pregame.
M1_CLOSE_IN_S = 7_200.0
M1_TIME_TO_START_S = -9_000.0  # 7200 − 4.5h: 2.5h INTO the game
M2_TIME_TO_START_S = 5_400.0   # 21600 − 4.5h: 1.5h before start


class CountingEngine:
    """Delegating engine proxy: records every price() call's in_play flag (the
    shadow forces True; the live path passes the motion detector's False) and
    can be armed to raise on a FORCED call (the shadow-exception test)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[bool] = []  # the in_play flag of each price() call
        self.fail_forced = False

    def price(
        self,
        rfq: Rfq,
        *,
        time_to_close_s: float,
        in_play: bool = False,
        inventory_skew_cc: int = 0,
    ) -> Any:
        self.calls.append(in_play)
        if self.fail_forced and in_play:
            raise RuntimeError("shadow pricing boom")
        return self._inner.price(
            rfq,
            time_to_close_s=time_to_close_s,
            in_play=in_play,
            inventory_skew_cc=inventory_skew_cc,
        )

    def __getattr__(self, name: str) -> Any:  # everything else delegates
        return getattr(self._inner, name)


async def _rig(
    tmp_path: Path, *, shadow: bool, m1_close_in_s: float = M1_CLOSE_IN_S
) -> tuple[Rig, CountingEngine, Store]:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1", close_in_s=m1_close_in_s)
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / "shadow.sqlite3", h.clock)
    rig = Rig(h, store, FiltersConfig(inplay_shadow_enabled=shadow))
    engine = CountingEngine(rig.lifecycle._engine)  # noqa: SLF001 (test seam)
    rig.lifecycle._engine = engine  # type: ignore[assignment]  # noqa: SLF001 (test seam)
    return rig, engine, store


def _inplay_rfq(**overrides: object) -> Rfq:
    return combo(CROSS_EVENT_LEGS, **overrides)


async def _shadow_rows(store: Store) -> list[dict[str, Any]]:
    async with store._db.execute(  # noqa: SLF001 (test seam)
        "SELECT at, rfq_id, market_ticker, fair_cc, yes_bid_cc, no_bid_cc,"
        " target_cost_cc, contracts_centi, leg_time_to_start_s_json,"
        " context_json FROM would_quotes_inplay"
    ) as cursor:
        rows = await cursor.fetchall()
    keys = (
        "at", "rfq_id", "market_ticker", "fair_cc", "yes_bid_cc", "no_bid_cc",
        "target_cost_cc", "contracts_centi", "leg_time_to_start_s_json",
        "context_json",
    )
    return [dict(zip(keys, row, strict=True)) for row in rows]


async def _skip_decisions(store: Store) -> list[list[str]]:
    async with store._db.execute(  # noqa: SLF001 (test seam)
        "SELECT reasons_json FROM decisions WHERE kind = 'no_quote'"
    ) as cursor:
        rows = await cursor.fetchall()
    return [json.loads(row[0]) for row in rows]


async def test_inplay_only_skip_is_solely_skip_inplay_leg(tmp_path: Path) -> None:
    # Pin the fixture geometry itself: the crafted RFQ is skipped for EXACTLY
    # skip_inplay_leg (the shadow's eligibility premise, not an accident).
    rig, _engine, _store = await _rig(tmp_path, shadow=False)
    reasons = rig.lifecycle._filter.evaluate(_inplay_rfq())  # noqa: SLF001
    assert reasons == [ReasonCode.SKIP_INPLAY_LEG]


async def test_flag_off_is_byte_identical(tmp_path: Path) -> None:
    # Default OFF: the in-play skip records exactly as today — no shadow row,
    # and the engine is NEVER invoked (counting fake proves no pricing work).
    rig, engine, store = await _rig(tmp_path, shadow=False)
    await rig.lifecycle.handle_rfq(_inplay_rfq())
    assert rig.sender.created == []                      # still skipped
    assert engine.calls == []                            # zero pricing calls
    assert await store.count("would_quotes_inplay") == 0
    assert await _skip_decisions(store) == [[str(ReasonCode.SKIP_INPLAY_LEG)]]
    assert rig.metrics.counter("rfq.skipped") == 1
    assert rig.metrics.counter("inplay_shadow.recorded") == 0


async def test_flag_on_inplay_only_skip_records_one_row(tmp_path: Path) -> None:
    rig, engine, store = await _rig(tmp_path, shadow=True)
    await rig.lifecycle.handle_rfq(_inplay_rfq())
    # The RFQ is STILL skipped exactly as today — no quote is ever sent.
    assert rig.sender.created == []
    assert await _skip_decisions(store) == [[str(ReasonCode.SKIP_INPLAY_LEG)]]
    # Exactly one shadow pricing, with the engine's in-play treatment forced.
    assert engine.calls == [True]
    rows = await _shadow_rows(store)
    assert len(rows) == 1
    row = rows[0]
    assert row["at"]  # timestamped
    assert row["rfq_id"] == "rfq_1"
    assert row["market_ticker"] == "KXMVE-C1"
    # Money fields are int centi-cents; fair is a real interior probability.
    assert isinstance(row["fair_cc"], int)
    assert 0 < row["fair_cc"] < 10_000
    assert isinstance(row["yes_bid_cc"], int) and row["yes_bid_cc"] >= 0
    assert isinstance(row["no_bid_cc"], int) and row["no_bid_cc"] >= 0
    # Sizing: the fixture RFQ is contracts-mode ("10.00" ⇒ 1000 centi).
    assert row["contracts_centi"] == 1_000
    assert row["target_cost_cc"] is None
    # Per-leg time-to-start from the SAME pregame ladder that produced the
    # skip: NEGATIVE = seconds INTO the game.
    assert json.loads(row["leg_time_to_start_s_json"]) == {
        "M1": M1_TIME_TO_START_S,
        "M2": M2_TIME_TO_START_S,
    }
    context = json.loads(row["context_json"])
    assert context["skip_reasons"] == [str(ReasonCode.SKIP_INPLAY_LEG)]
    assert context["collection"] == "KXMVESPORTS"
    assert context["width_cc"] > 0
    assert rig.metrics.counter("inplay_shadow.recorded") == 1


async def test_flag_on_target_cost_sizing_recorded(tmp_path: Path) -> None:
    rig, engine, store = await _rig(tmp_path, shadow=True)
    await rig.lifecycle.handle_rfq(
        _inplay_rfq(contracts_fp=None, target_cost_dollars="25.00")
    )
    assert engine.calls == [True]
    rows = await _shadow_rows(store)
    assert len(rows) == 1
    assert rows[0]["target_cost_cc"] == 250_000  # $25 in centi-cents
    assert rows[0]["contracts_centi"] is None


async def test_flag_on_pregame_rfq_no_shadow_row(tmp_path: Path) -> None:
    # A PREGAME RFQ (no in-play skip) with the flag ON: the normal quote path
    # runs untouched (one live pricing, motion-detector in_play=False, quote
    # posted) and the shadow never fires.
    rig, engine, store = await _rig(tmp_path, shadow=True, m1_close_in_s=21_600.0)
    await rig.lifecycle.handle_rfq(_inplay_rfq())
    assert len(rig.sender.created) == 1        # quoted normally
    assert engine.calls == [False]             # live pricing only, never forced
    assert await store.count("would_quotes_inplay") == 0
    assert rig.metrics.counter("inplay_shadow.recorded") == 0


async def test_flag_on_mixed_reason_skip_no_shadow_row(tmp_path: Path) -> None:
    # In-play AND another decline reason (size below min): the RFQ would have
    # been declined even with in-play quoting armed, so pricing it would
    # poison the measurement — no shadow.
    rig, engine, store = await _rig(tmp_path, shadow=True)
    await rig.lifecycle.handle_rfq(_inplay_rfq(contracts_fp="0.50"))
    reasons = (await _skip_decisions(store))[0]
    assert str(ReasonCode.SKIP_INPLAY_LEG) in reasons
    assert str(ReasonCode.SKIP_SIZE_BELOW_MIN) in reasons
    assert engine.calls == []
    assert await store.count("would_quotes_inplay") == 0


async def test_engine_exception_in_shadow_never_breaks_the_skip(
    tmp_path: Path,
) -> None:
    rig, engine, store = await _rig(tmp_path, shadow=True)
    engine.fail_forced = True  # the FORCED shadow pricing raises
    await rig.lifecycle.handle_rfq(_inplay_rfq())  # must not raise
    # The skip decision is intact and no partial shadow row leaked.
    assert rig.sender.created == []
    assert await _skip_decisions(store) == [[str(ReasonCode.SKIP_INPLAY_LEG)]]
    assert await store.count("would_quotes_inplay") == 0
    assert rig.metrics.counter("inplay_shadow.errored") == 1


async def test_store_exception_in_shadow_never_breaks_the_skip(
    tmp_path: Path,
) -> None:
    rig, engine, store = await _rig(tmp_path, shadow=True)

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("store boom")

    rig.lifecycle._store.record_would_quote_inplay = _boom  # type: ignore[method-assign]  # noqa: SLF001
    await rig.lifecycle.handle_rfq(_inplay_rfq())  # must not raise
    assert engine.calls == [True]  # priced, then the record step failed
    assert await _skip_decisions(store) == [[str(ReasonCode.SKIP_INPLAY_LEG)]]
    assert await store.count("would_quotes_inplay") == 0
    assert rig.metrics.counter("inplay_shadow.errored") == 1


async def test_retry_reskip_records_one_row_per_rfq(tmp_path: Path) -> None:
    # quote_app's pending-retry loop re-runs handle_rfq up to 5x on a skipped
    # RFQ: the shadow must record ONE row per rfq_id (dedupe on recorded ids),
    # while a DIFFERENT RFQ still records its own row.
    rig, engine, store = await _rig(tmp_path, shadow=True)
    await rig.lifecycle.handle_rfq(_inplay_rfq())
    await rig.lifecycle.handle_rfq(_inplay_rfq())  # the retry re-skip
    assert engine.calls == [True]  # second pass never even re-prices
    assert await store.count("would_quotes_inplay") == 1
    await rig.lifecycle.handle_rfq(_inplay_rfq(id="rfq_2"))
    assert engine.calls == [True, True]
    assert await store.count("would_quotes_inplay") == 2
    rows = await _shadow_rows(store)
    assert [r["rfq_id"] for r in rows] == ["rfq_1", "rfq_2"]


async def test_backlog_suppresses_shadow_until_idle(tmp_path: Path) -> None:
    # THROUGHPUT ISOLATION: queued live work (queue depth > 0) suppresses the
    # shadow entirely; an idle pool admits it. The bound is the pool's own
    # measured state — no sample-rate knob to tune.
    rig, engine, store = await _rig(tmp_path, shadow=True)
    rig.lifecycle.attach_rfq_backlog_probe(lambda: 3)  # live RFQs queued
    await rig.lifecycle.handle_rfq(_inplay_rfq())
    assert engine.calls == []
    assert await store.count("would_quotes_inplay") == 0
    assert rig.metrics.counter("inplay_shadow.skipped_backlog") == 1
    # Pool drained ⇒ the next eligible skip is sampled.
    rig.lifecycle.attach_rfq_backlog_probe(lambda: 0)
    await rig.lifecycle.handle_rfq(_inplay_rfq())
    assert engine.calls == [True]
    assert await store.count("would_quotes_inplay") == 1
