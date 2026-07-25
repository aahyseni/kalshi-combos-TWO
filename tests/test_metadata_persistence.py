"""Persistent metadata cache (2026-07-25).

WHY: the cache was memory-only, so every restart re-fetched every watched
leg's metadata in one boot burst — a 429 storm (2,319 failures in 36s at the
7/25 v6 boot) that left low-flow families (HR/HIT/TB/RBI) unpriceable until
sparse RFQ flow healed them, structurally biasing fills toward high-flow
K/total series. Pinned here:

- round trip: persist → cold cache → load_persisted → ``peek`` serves the
  grid with ZERO network (the hot-path warm-boot guarantee);
- loaded entries are TTL-EXPIRED: the async ``market()`` path re-fetches on
  first touch (status can change) while ``peek`` stays warm;
- expired/undated markets are never persisted and never resurrected;
- combo-grid injections (``put_combo_grid``, raw={}) are never persisted
  (grids are per-day collections);
- events persist iff a kept market references them (no retention constant);
- corrupt/missing file ⇒ cold boot, never an error.
"""

import json
from pathlib import Path
from typing import Any

from combomaker.core.clock import FakeClock
from combomaker.core.money import CentiCents
from combomaker.marketdata.grid import PriceGrid
from combomaker.marketdata.metadata import MetadataCache

JsonDict = dict[str, Any]

FUTURE = "2099-01-01T00:00:00Z"
PAST = "2020-01-01T00:00:00Z"


def market_payload(
    ticker: str, *, close_time: str | None = FUTURE, event: str | None = "EV1"
) -> JsonDict:
    market: JsonDict = {
        "ticker": ticker,
        "status": "active",
        "price_ranges": [{"start": "0.01", "end": "0.99", "step": "0.01"}],
    }
    if close_time is not None:
        market["close_time"] = close_time
    if event is not None:
        market["event_ticker"] = event
    return {"market": market}


class FakeRest:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.payloads: dict[str, JsonDict] = {}

    async def get_market(self, ticker: str) -> JsonDict:
        self.calls.append(ticker)
        return self.payloads[ticker]

    async def get_event(self, ticker: str) -> JsonDict:
        self.calls.append(f"event:{ticker}")
        return {"event": {"event_ticker": ticker, "mutually_exclusive": True}}

    async def get_multivariate_collections(self, **params: str | int) -> JsonDict:
        return {"multivariate_contracts": []}


async def _warm_cache(rest: FakeRest, clock: FakeClock) -> MetadataCache:
    cache = MetadataCache(rest, clock)
    rest.payloads["KXMLBHR-FUT"] = market_payload("KXMLBHR-FUT")
    rest.payloads["KXMLBKS-OLD"] = market_payload(
        "KXMLBKS-OLD", close_time=PAST, event="EV-OLD"
    )
    rest.payloads["KXMLBTB-UNDATED"] = market_payload(
        "KXMLBTB-UNDATED", close_time=None, event=None
    )
    await cache.market("KXMLBHR-FUT")
    meta = await cache.market("KXMLBHR-FUT")
    if meta.event_ticker:
        await cache.event(meta.event_ticker)
    await cache.market("KXMLBKS-OLD")
    await cache.market("KXMLBTB-UNDATED")
    return cache


async def test_round_trip_warm_boot_peek_serves_without_network(
    tmp_path: Path,
) -> None:
    rest = FakeRest()
    clock = FakeClock()
    cache = await _warm_cache(rest, clock)
    path = str(tmp_path / "metadata_cache.json")
    assert cache.dirty
    written = cache.persist(path)
    assert written == 1  # only the future-dated market persists
    assert not cache.dirty

    cold_rest = FakeRest()
    cold = MetadataCache(cold_rest, FakeClock())
    kept = cold.load_persisted(path)
    assert kept == 1
    meta = cold.peek("KXMLBHR-FUT")
    assert meta is not None and meta.grid is not None
    assert meta.grid.is_on_grid(CentiCents(5_600))
    assert cold_rest.calls == []  # the warm-boot guarantee: zero network
    # Expired / undated markets never resurrect.
    assert cold.peek("KXMLBKS-OLD") is None
    assert cold.peek("KXMLBTB-UNDATED") is None
    # The referenced event came back too — peek-only, still no network.
    assert cold.event_mutually_exclusive("EV1") is True
    assert cold.event_mutually_exclusive("EV-OLD") is None


async def test_loaded_entries_are_ttl_expired_for_the_async_path(
    tmp_path: Path,
) -> None:
    rest = FakeRest()
    clock = FakeClock()
    cache = await _warm_cache(rest, clock)
    path = str(tmp_path / "metadata_cache.json")
    cache.persist(path)

    cold_rest = FakeRest()
    cold_rest.payloads["KXMLBHR-FUT"] = market_payload("KXMLBHR-FUT")
    cold = MetadataCache(cold_rest, FakeClock())
    cold.load_persisted(path)
    # peek is warm, but the ASYNC path must revalidate on first touch
    # (status can change across runs).
    await cold.market("KXMLBHR-FUT")
    assert cold_rest.calls == ["KXMLBHR-FUT"]


async def test_combo_grid_injections_never_persist(tmp_path: Path) -> None:
    rest = FakeRest()
    cache = MetadataCache(rest, FakeClock())
    grid = PriceGrid.from_market_payload(
        market_payload("ANY")["market"]
    )
    cache.put_combo_grid("KXMVE-COMBO-1", grid)
    path = str(tmp_path / "metadata_cache.json")
    assert cache.persist(path) == 0


async def test_corrupt_or_missing_file_is_a_cold_boot(tmp_path: Path) -> None:
    cache = MetadataCache(FakeRest(), FakeClock())
    assert cache.load_persisted(str(tmp_path / "absent.json")) == 0
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert cache.load_persisted(str(bad)) == 0


async def test_valid_json_wrong_shape_is_a_cold_boot_never_a_crash(
    tmp_path: Path,
) -> None:
    """2026-07-25 review (MEDIUM): valid-JSON non-object payloads escaped the
    json-only guard and crash-LOOPED boot until a human deleted the file."""
    cache = MetadataCache(FakeRest(), FakeClock())
    for i, content in enumerate(
        ['[]', 'null', '"x"', '{"markets": null}', '{"markets": 7}']
    ):
        f = tmp_path / f"shape{i}.json"
        f.write_text(content, encoding="utf-8")
        assert cache.load_persisted(str(f)) == 0


async def test_naive_close_time_is_dropped_never_a_crash(tmp_path: Path) -> None:
    """2026-07-25 review (MEDIUM): a tz-NAIVE persisted close_time raised
    TypeError against datetime.now(utc) — treated as undated (dropped)."""
    f = tmp_path / "naive.json"
    naive = market_payload("KXMLBHR-NAIVE")["market"]
    naive["close_time"] = "2099-01-01T00:00:00"  # no offset — naive
    f.write_text(json.dumps({"markets": [naive], "events": []}), encoding="utf-8")
    cache = MetadataCache(FakeRest(), FakeClock())
    assert cache.load_persisted(str(f)) == 0
    assert cache.peek("KXMLBHR-NAIVE") is None


async def test_warm_loaded_entries_need_revalidation(tmp_path: Path) -> None:
    """2026-07-25 review (HIGH): the quote app's watch gates skip the async
    fetch when peek() is non-None, so warm-loaded entries were NEVER
    revalidated (stale close_time → in-play gate blindness). The gates now
    consult needs_revalidation(): True for a warm-loaded (TTL-expired)
    entry, False right after a real fetch."""
    rest = FakeRest()
    clock = FakeClock()
    cache = await _warm_cache(rest, clock)
    path = str(tmp_path / "metadata_cache.json")
    cache.persist(path)
    assert not cache.needs_revalidation("KXMLBHR-FUT")  # freshly fetched

    cold = MetadataCache(FakeRest(), FakeClock())
    cold.load_persisted(path)
    assert cold.peek("KXMLBHR-FUT") is not None  # pricing stays warm
    assert cold.needs_revalidation("KXMLBHR-FUT")  # but the gate must fetch
    assert not cold.needs_revalidation("NEVER-SEEN")  # absent = plain miss


async def test_failed_write_rearms_the_dirty_flag(tmp_path: Path) -> None:
    """2026-07-25 review (LOW): dirty is cleared at snapshot time, so a
    failed disk write must re-arm it or a quiet book never retries."""
    rest = FakeRest()
    cache = await _warm_cache(rest, FakeClock())
    assert cache.dirty
    # A directory path makes os.replace fail on every platform.
    blocked = tmp_path / "iam_a_dir"
    blocked.mkdir()
    assert cache.persist(str(blocked)) == 0
    assert cache.dirty  # re-armed — the next interval retries
