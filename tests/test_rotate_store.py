"""Store rotation tool (tools/ops/rotate_store.py, 2026-09-05 design item 7).

The copy logic on a SYNTHETIC store built by the real ``Store`` (so the schema
is the live DDL, ids come from the live AUTOINCREMENT path, and the ledger rows
are written by the live writers): what is carried (ledgers whole, ids
preserved; the seed window of decisions/rfqs; one leg-set row per unresolved
fills ticker; sqlite_sequence continued), what is not (old tape), the archive
left intact, the fresh store opening through the REAL ``Store.open`` with every
boot-time reader answering the same as before, and the refusals (live bot,
un-checkpointable WAL, foreign WAL on another hard link, rollback on a failed
build). No live path is touched: everything is under tmp_path.
"""

from __future__ import annotations

import json
import os
import sqlite3
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import tools.ops.rotate_store as rs
from combomaker.core.clock import FakeClock
from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.ops.acceptance_seed import SEED_WINDOW_S, _bisect_first_id
from combomaker.ops.persistence import Store
from combomaker.risk.exposure import LegRef, OpenPosition

NOW = datetime(2026, 9, 5, 18, 0, tzinfo=UTC)
LEGS = (
    LegRef("KXMLBKS-26SEP05-A-3", "KXMLBKS-26SEP05-A", "yes"),
    LegRef("KXMLBKS-26SEP05-B-4", "KXMLBKS-26SEP05-B", "yes"),
)
LEDGERED = "KXMVE-LEDGERED"
TAPE_ONLY = "KXMVE-TAPEONLY"  # a fills ticker with NO ledger row (legacy shape)
CONFLICT = "KXMVE-CONFLICT"  # tape holds TWO leg sets for one ticker


class _Clock(FakeClock):
    """FakeClock with an absolute setter (the tape needs ids ascending with time)."""

    def set(self, when: datetime) -> None:
        self._now = when


class _Rfq:
    """Minimal duck-typed Rfq for Store.record_rfq."""

    def __init__(self, rfq_id: str, ticker: str, legs: tuple[LegRef, ...]) -> None:
        self.rfq_id = rfq_id
        self.market_ticker = ticker
        self.mve_collection_ticker = "KXMVECROSSCATEGORY"
        self.contracts = CentiContracts(1000)
        self.target_cost_cc = None
        self.legs = legs
        self.raw = {"id": rfq_id}


async def _build_store(path: Path, *, clock: _Clock) -> dict[str, int]:
    """A store with 3 days of tape (ids ascend with time), ledgers, fills,
    markouts, ev rows, a store_meta watermark, and the two vitals tables."""
    store = await Store.open(path, clock)
    counts: dict[str, int] = {}
    # ---- tape: 3 days, one rfq + decisions per hour ----------------------
    start = NOW - timedelta(days=3)
    n_rfq = n_dec = 0
    for h in range(72):
        clock.set(start + timedelta(hours=h))
        ticker = LEDGERED if h % 3 else TAPE_ONLY
        legs = LEGS if h % 7 else LEGS[:1]
        if ticker == LEDGERED:
            legs = LEGS
        await store.record_rfq(_Rfq(f"rfq-{h}", ticker, legs), source="ws")
        n_rfq += 1
        if h == 1:
            # a conflicting-leg-set ticker on the tape only (ids stay
            # time-ordered, as the live enqueue-ordered tape is)
            await store.record_rfq(_Rfq("rfq-c1", CONFLICT, LEGS), source="ws")
            await store.record_rfq(_Rfq("rfq-c2", CONFLICT, LEGS[:1]), source="ws")
            n_rfq += 2
        for kind in ("quote_sent", "skip", "confirm"):
            await store.record_decision(kind, f"rfq-{h}", ["r"], {"quote_id": f"q-{h}"})
            n_dec += 1
    counts["rfqs"] = n_rfq
    counts["decisions"] = n_dec
    # ---- ledgers -----------------------------------------------------------
    clock.set(NOW - timedelta(days=2))
    for i in range(5):
        pos = OpenPosition(
            position_id=f"pos-{i}",
            combo_ticker=LEDGERED,
            collection="KXMVECROSSCATEGORY",
            our_side=Side.NO,
            contracts=CentiContracts(500),
            entry_price_cc=CentiCents(6200),
            legs=LEGS,
        )
        await store.record_position_open(pos, subaccount="0", fees_cc=10)
        await store.record_fill(
            f"fill:{i}",
            order_id=f"order-{i}",
            combo_ticker=LEDGERED,
            our_side="no",
            contracts_centi=500,
            price_cc=6200,
            fee_cc=10,
            expected_edge_cc=150,
            raw={"i": i},
        )
        await store.record_markout(
            f"fill:{i}", horizon_s=60.0, fair_at_fill_cc=6000, fair_now_cc=6100,
            raw_mid_at_fill_cc=None, raw_mid_now_cc=None,
        )
    # a fills row whose ticker has NO ledger row: legs resolvable only from tape
    await store.record_fill(
        "fill:tape", order_id="order-tape", combo_ticker=TAPE_ONLY, our_side="no",
        contracts_centi=300, price_cc=5000, fee_cc=5, expected_edge_cc=100, raw={},
    )
    await store.record_fill(
        "fill:conflict", order_id="order-conflict", combo_ticker=CONFLICT, our_side="no",
        contracts_centi=300, price_cc=5000, fee_cc=5, expected_edge_cc=100, raw={},
    )
    counts["fills"] = 7
    counts["position_ledger"] = 5
    counts["markouts"] = 5
    await store.close()
    # ---- the two vitals tables (created by no live module; present live) ---
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE daily_ruin_anchors (
            environment TEXT NOT NULL, subaccount INTEGER NOT NULL, utc_date TEXT NOT NULL,
            equity_cc INTEGER NOT NULL, peak_equity_cc INTEGER NOT NULL,
            realized_pnl_cc INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, PRIMARY KEY (environment, subaccount, utc_date));
        INSERT INTO daily_ruin_anchors VALUES ('prod', 0, '2026-07-16', 20504100, 20504100, 0,
            'x', 'x');
        CREATE TABLE daily_realized_events (
            environment TEXT NOT NULL, subaccount INTEGER NOT NULL, utc_date TEXT NOT NULL,
            event_id TEXT NOT NULL, delta_cc INTEGER NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (environment, subaccount, utc_date, event_id));
        """
    )
    con.commit()
    con.close()
    counts["daily_ruin_anchors"] = 1
    counts["daily_realized_events"] = 0
    return counts


@pytest.fixture
def clock() -> _Clock:
    return _Clock(NOW)


@pytest.fixture
async def synthetic(tmp_path: Path, clock: _Clock) -> tuple[Path, dict[str, int]]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    store = data_dir / "combomaker-prod-live-wc.sqlite3"
    counts = await _build_store(store, clock=clock)
    return store, counts


def _exists(path: Path) -> bool:
    return path.exists()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_bytes(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def _seq(path: Path) -> dict[str, int]:
    con = rs.connect_ro(path)
    try:
        return dict(con.execute("SELECT name, seq FROM sqlite_sequence").fetchall())
    finally:
        con.close()


def _rows(path: Path, table: str) -> list[tuple]:
    con = rs.connect_ro(path)
    try:
        return con.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
    finally:
        con.close()


# ----------------------------------------------------------------- pinning


def test_seed_kinds_pin_the_seed_readers_literals() -> None:
    """The carried decision kinds are exactly the literals the boot seed
    reads (rule 8(c): a duplicated fact is kept in sync by a check)."""
    import inspect

    from combomaker.ops import acceptance_seed

    src = inspect.getsource(acceptance_seed.seed_counts_from_store)
    for kind in rs.SEED_KINDS:
        assert f"'{kind}'" in src, kind
    assert rs.seed_window_s() == SEED_WINDOW_S


def test_tape_tables_are_exactly_the_queue_writers_plus_measurement_recorders() -> None:
    """Every table the live Store writes through ``_write`` (the drop-on-
    overflow queue) except the bounded structural_fits, plus the observe-only
    would_quotes recorders — and nothing the ledgers depend on."""
    assert set(rs.TAPE_TABLES) == {
        "rfqs", "rfq_deletions", "decisions", "would_quotes", "would_quotes_inplay"
    }
    for ledger in ("fills", "position_ledger", "ev_ledger", "markouts", "store_meta"):
        assert ledger not in rs.TAPE_TABLES


async def test_bisect_parity_with_the_seed(synthetic: tuple[Path, dict[str, int]]) -> None:
    store, _ = synthetic
    con = rs.connect_ro(store)
    try:
        for hours in (1, 12, 24, 47, 72, 96):
            cutoff = (NOW - timedelta(hours=hours)).isoformat()
            assert rs.bisect_first_id(con, "decisions", "at", cutoff) == _bisect_first_id(
                con, "decisions", cutoff
            )
        # Beyond the last row the seed returns the last id (its tolerated
        # off-by-one); a copy must carry nothing.
        future = (NOW + timedelta(hours=1)).isoformat()
        assert _bisect_first_id(con, "decisions", future) is not None
        assert rs.bisect_first_id(con, "decisions", "at", future) is None
    finally:
        con.close()


# -------------------------------------------------------------------- plan


async def test_plan_is_read_only_and_reports_the_carry(
    synthetic: tuple[Path, dict[str, int]]
) -> None:
    store, counts = synthetic
    before = store.stat().st_mtime_ns
    p = rs.plan(store, now=NOW, window_s=SEED_WINDOW_S)
    assert store.stat().st_mtime_ns == before  # a read-only plan never touches the file
    assert p["tables"]["fills"]["count"] == counts["fills"]
    assert p["tables"]["decisions"]["tape"] is True
    assert p["tables"]["decisions"]["max_id"] == counts["decisions"]
    dec = p["seed"]["tables"]["decisions"]
    # 24 hourly rows per kind inside the window (72 h of tape, 3 kinds/h)
    assert dec["rows_by_kind"] == {"quote_sent": 24, "confirm": 24, "decline": 0}
    assert dec["rows"] == 48
    rfq = p["seed"]["tables"]["rfqs"]
    assert rfq["rows_estimate"] == 24
    lp = p["leg_provenance"]
    assert set(lp["tickers"]) == {TAPE_ONLY, CONFLICT}
    # TAPE_ONLY carries two leg shapes on the tape (the h%7 twist), CONFLICT
    # two by construction: both ambiguous, one representative row per shape.
    assert set(lp["conflicting"]) == {TAPE_ONLY, CONFLICT}
    assert len(lp["rfq_ids"]) == 4
    assert p["estimate"]["live_rows"] == sum(
        v["count"] for v in p["tables"].values() if not v["tape"]
    )
    assert any(r["reader"].startswith("Store.open") for r in p["readers"])
    assert p["measured"]["live_rows"] == p["estimate"]["live_rows"]


# ------------------------------------------------------------------- apply


async def test_rotate_carries_ledgers_whole_tape_by_window_and_leaves_archive(
    synthetic: tuple[Path, dict[str, int]], tmp_path: Path, clock: _Clock
) -> None:
    store, counts = synthetic
    live_tables = [
        "fills", "position_ledger", "ev_ledger", "markouts", "structural_fits", "store_meta",
        "daily_ruin_anchors", "daily_realized_events", "combo_trades",
    ]
    before_live = {t: _rows(store, t) for t in live_tables}
    before_seq = _seq(store)
    m = rs.rotate(
        store,
        now=NOW,
        window_s=SEED_WINDOW_S,
        manifest_dir=tmp_path / "backups",
        liveness_window_s_=60.0,
        skip_process_probe=True,
    )
    archive = Path(m["archive"])
    assert m["ok"] is True
    assert archive.name == "combomaker-prod-live-wc.sqlite3.archive-20260905"
    assert _exists(archive) and _exists(store)
    assert not _exists(Path(m["fresh_tmp"]))
    # LIVE tables: identical rows, ids preserved.
    for t in live_tables:
        assert _rows(store, t) == before_live[t], t
        assert _rows(archive, t) == before_live[t], t
    # the fills verification watermark is the archive's, not re-stamped
    meta = dict(_rows(store, "store_meta"))
    assert meta[Store.META_FILLS_VERIFICATION_WATERMARK_ID] == "0"  # 0 fills at migration
    assert meta[Store.META_FILLS_VERIFICATION_MIGRATED_AT].startswith("2026-09-05T18")
    # TAPE: the seed window only; every carried row is a verbatim archive row.
    fresh_dec = _rows(store, "decisions")
    assert len(fresh_dec) == 48 == m["built"]["copied"]["decisions"]
    assert {r[2] for r in fresh_dec} == {"quote_sent", "confirm"}
    cutoff = (NOW - timedelta(seconds=SEED_WINDOW_S)).isoformat()
    assert all(r[1] >= cutoff for r in fresh_dec)
    arch_dec = {r[0]: r for r in _rows(archive, "decisions")}
    assert all(arch_dec[r[0]] == r for r in fresh_dec)
    fresh_rfq = _rows(store, "rfqs")
    in_window = [r for r in fresh_rfq if r[2] >= cutoff]
    provenance = [r for r in fresh_rfq if r[2] < cutoff]
    assert len(in_window) == 24
    # provenance rows: TAPE_ONLY's two leg shapes + CONFLICT's two, none duplicated
    assert {r[4] for r in provenance} == {TAPE_ONLY, CONFLICT}
    assert len(provenance) == len({r[0] for r in provenance}) == 4
    assert m["built"]["copied"]["rfqs"] == len(fresh_rfq)
    # archive untouched: full tape still there
    assert len(_rows(archive, "decisions")) == counts["decisions"]
    assert len(_rows(archive, "rfqs")) == counts["rfqs"]
    # sequences continue ABOVE the archive's for every AUTOINCREMENT table
    after_seq = _seq(store)
    assert {k: after_seq[k] for k in before_seq} == before_seq
    # tables that never had a row get a seq of 0 from the empty copy — the
    # same "next id is 1" the archive expressed by having no row at all
    assert all(v == 0 for k, v in after_seq.items() if k not in before_seq)
    assert after_seq["decisions"] == counts["decisions"]
    # WAL mode, clean
    con = rs.connect_ro(store)
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    con.close()
    # manifest on disk
    mp = Path(m["manifest_path"])
    assert _exists(mp)
    loaded = _read_json(mp)
    assert loaded["built"]["copied"] == m["built"]["copied"]
    # --verify (read-only) is clean
    v = rs.verify(store, mp)
    assert v["ok"] is True, v["problems"]


async def test_fresh_store_opens_with_the_real_store_and_boot_readers_agree(
    synthetic: tuple[Path, dict[str, int]], tmp_path: Path, clock: _Clock
) -> None:
    store, _ = synthetic
    # what the boot readers answer BEFORE
    s0 = await Store.open(store, clock)
    try:
        wm0 = await s0.fills_verification_watermark()
        held0 = await s0.held_positions([LEDGERED, TAPE_ONLY, CONFLICT])
        grade0 = await s0.settled_grade_rows()
        ids0 = await s0.fill_order_ids()
        open0 = await s0.open_ledger_identities()
        day0 = await s0.day_realized_pnl_cc(
            (NOW - timedelta(days=3)).isoformat(), (NOW + timedelta(days=1)).isoformat()
        )
        booked0 = await s0.booked_unverified_fills(after_id=0)
    finally:
        await s0.close()
    rs.rotate(
        store, now=NOW, window_s=SEED_WINDOW_S, manifest_dir=tmp_path / "b",
        liveness_window_s_=60.0, skip_process_probe=True,
    )
    s1 = await Store.open(store, clock)  # the live open path: DDL, migrations, indexes
    try:
        assert await s1.fills_verification_watermark() == wm0
        held1 = await s1.held_positions([LEDGERED, TAPE_ONLY, CONFLICT])
        assert held1 == held0
        by_ticker = {h["combo_ticker"]: h for h in held1}
        assert by_ticker[LEDGERED]["legs_source"] == "position_ledger"
        # TAPE_ONLY had two leg shapes on the tape → ambiguous → rejected both
        # before and after (fail-closed parity); CONFLICT likewise.
        assert TAPE_ONLY not in by_ticker and CONFLICT not in by_ticker
        assert await s1.settled_grade_rows() == grade0
        assert await s1.fill_order_ids() == ids0
        assert await s1.open_ledger_identities() == open0
        assert (
            await s1.day_realized_pnl_cc(
                (NOW - timedelta(days=3)).isoformat(), (NOW + timedelta(days=1)).isoformat()
            )
            == day0
        )
        assert await s1.booked_unverified_fills(after_id=0) == booked0
        # a NEW fill gets an id above the archive's last one
        await s1.record_fill(
            "fill:new", order_id="order-new", combo_ticker=LEDGERED, our_side="no",
            contracts_centi=100, price_cc=5000, fee_cc=1, expected_edge_cc=1, raw={},
        )
        rows = await s1.booked_unverified_fills(after_id=0)
        assert max(r["id"] for r in rows) == 8
    finally:
        await s1.close()


async def test_tape_fallback_single_leg_set_survives_rotation(
    tmp_path: Path, clock: _Clock
) -> None:
    """A fills ticker with NO ledger row and ONE leg set on the (old) tape
    rehydrates from the carried provenance row after rotation."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    store = data_dir / "s.sqlite3"
    s = await Store.open(store, clock)
    clock.set(NOW - timedelta(days=10))
    await s.record_rfq(_Rfq("old", "KXMVE-OLD", LEGS), source="ws")
    await s.record_rfq(_Rfq("old2", "KXMVE-OLD", LEGS), source="ws")  # same leg set again
    await s.record_fill(
        "fill:old", order_id="o", combo_ticker="KXMVE-OLD", our_side="no",
        contracts_centi=300, price_cc=5000, fee_cc=5, expected_edge_cc=100, raw={},
    )
    before = await s.held_positions(["KXMVE-OLD"])
    await s.close()
    assert before and before[0]["legs_source"] == "rfqs_tape"
    m = rs.rotate(
        store, now=NOW, window_s=SEED_WINDOW_S, manifest_dir=tmp_path / "b",
        liveness_window_s_=60.0, skip_process_probe=True,
    )
    assert m["built"]["copied"]["rfqs"] == 1  # ONE representative row, not two
    s1 = await Store.open(store, clock)
    try:
        assert await s1.held_positions(["KXMVE-OLD"]) == before
    finally:
        await s1.close()


# ---------------------------------------------------------------- refusals


async def test_refuses_while_the_bot_is_alive(
    synthetic: tuple[Path, dict[str, int]], tmp_path: Path
) -> None:
    store, _ = synthetic
    (store.parent / "heartbeat.txt").write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
    with pytest.raises(SystemExit, match="ALIVE"):
        rs.rotate(
            store, now=NOW, window_s=SEED_WINDOW_S, manifest_dir=tmp_path / "b",
            liveness_window_s_=60.0, skip_process_probe=True,
        )
    assert store.exists()
    assert not list(store.parent.glob("*.archive-*"))


async def test_refuses_when_the_wal_cannot_be_checkpointed(
    synthetic: tuple[Path, dict[str, int]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = synthetic
    monkeypatch.setattr(rs, "busy_timeout_ms", lambda: 200)  # the wait, not the verdict
    # a writer that has not committed → its WAL frames are not foldable
    holder = sqlite3.connect(store, timeout=0.1)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO store_meta (key, value) VALUES ('x', 'y')")
    try:
        with pytest.raises(SystemExit, match="checkpointed|ALIVE|REFUSED"):
            rs.rotate(
                store, now=NOW, window_s=SEED_WINDOW_S, manifest_dir=tmp_path / "b",
                liveness_window_s_=60.0, skip_process_probe=True,
            )
    finally:
        holder.rollback()
        holder.close()
    assert store.exists()
    assert not list(store.parent.glob("*.archive-*"))


def _hardlink_ok(tmp_path: Path) -> bool:
    a = tmp_path / "hl_a"
    a.write_text("x")
    try:
        os.link(a, tmp_path / "hl_b")
    except OSError:
        return False
    return True


async def test_refuses_on_a_foreign_wal_at_another_hard_link(
    synthetic: tuple[Path, dict[str, int]], tmp_path: Path
) -> None:
    store, _ = synthetic
    if not _hardlink_ok(tmp_path):
        pytest.skip("hard links unsupported here")
    other_dir = tmp_path / "vitals_snapshot"
    other_dir.mkdir()
    other = other_dir / store.name
    os.link(store, other)
    # a plausible foreign WAL: valid header + one 4096-byte frame
    hdr = struct.pack(">8I", 0x377F0682, 3007000, 4096, 7, 1, 2, 0, 0)
    _write_bytes(Path(str(other) + "-wal"), hdr + b"\0" * (24 + 4096))
    files = rs.store_files(store)
    assert files["hard_links"] == 2
    if not files["other_names"]:
        pytest.skip("hard-link names not enumerable on this platform")
    refusals = rs.foreign_wal_refusals(files)
    assert refusals and "1 frames" in refusals[0]
    with pytest.raises(SystemExit, match="OWN WAL"):
        rs.rotate(
            store, now=NOW, window_s=SEED_WINDOW_S, manifest_dir=tmp_path / "b",
            liveness_window_s_=60.0, skip_process_probe=True,
        )
    assert store.exists()


async def test_failed_build_rolls_the_archive_back(
    synthetic: tuple[Path, dict[str, int]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, counts = synthetic
    before = _rows(store, "fills")

    def boom(*a: object, **k: object) -> dict[str, object]:
        raise RuntimeError("simulated copy failure")

    monkeypatch.setattr(rs, "build_fresh_store", boom)
    with pytest.raises(RuntimeError, match="simulated"):
        rs.rotate(
            store, now=NOW, window_s=SEED_WINDOW_S, manifest_dir=tmp_path / "b",
            liveness_window_s_=60.0, skip_process_probe=True,
        )
    assert store.exists()
    assert not list(store.parent.glob("*.archive-*"))
    assert not list(store.parent.glob("*.rotating-*"))
    assert _rows(store, "fills") == before
    failed = list((tmp_path / "b").glob("*-FAILED.json"))
    assert len(failed) == 1


async def test_refuses_an_existing_archive_name(
    synthetic: tuple[Path, dict[str, int]], tmp_path: Path
) -> None:
    store, _ = synthetic
    store.with_name(store.name + ".archive-20260905").write_bytes(b"")
    with pytest.raises(SystemExit, match="already exists"):
        rs.rotate(
            store, now=NOW, window_s=SEED_WINDOW_S, manifest_dir=tmp_path / "b",
            liveness_window_s_=60.0, skip_process_probe=True,
        )


def test_wal_header_parses_and_counts_frames(tmp_path: Path) -> None:
    p = tmp_path / "x-wal"
    assert rs.wal_header(p) is None
    p.write_bytes(b"")
    assert rs.wal_header(p)["frames"] == 0
    hdr = struct.pack(">8I", 0x377F0683, 3007000, 4096, 42, 11, 22, 0, 0)
    p.write_bytes(hdr + b"\0" * 3 * (24 + 4096))
    h = rs.wal_header(p)
    assert h["magic_ok"] and h["frames"] == 3 and h["checkpoint_seq"] == 42
    assert h["salt"] == [11, 22]


async def test_cli_dry_run_writes_the_plan(
    synthetic: tuple[Path, dict[str, int]], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store, _ = synthetic
    out = tmp_path / "plan.json"
    rc = rs.main(["--store", str(store), "--dry-run", "--no-measure", "--out", str(out)])
    assert rc == 0
    text = capsys.readouterr().out
    assert "BOOT-TIME READERS" in text and "SEED WINDOW" in text
    plan = json.loads(out.read_text(encoding="utf-8"))
    assert plan["tables"]["fills"]["count"] == 7
    assert "measured" not in plan
    assert store.exists() and not list(store.parent.glob("*.archive-*"))


# ------------------------------------------------ schema via the live Store.open


async def test_schema_comes_from_store_open_and_a_pre_migration_archive_gains_the_column(
    synthetic: tuple[Path, dict[str, int]], tmp_path: Path, clock: _Clock
) -> None:
    """An archive created BEFORE a migration (here: fills.exchange_fill_id
    dropped) rotates into a fresh store that HAS the column — the live
    ``Store.open`` DDL + migrations define the schema, the archive's rows are
    copied by column name and the fresh-only column takes its default."""
    store, counts = synthetic
    con = sqlite3.connect(store)
    con.execute("ALTER TABLE fills DROP COLUMN exchange_fill_id")
    con.commit()
    con.close()
    con = rs.connect_ro(store)
    before_fills = con.execute(
        "SELECT id, fill_ref, order_id, combo_ticker FROM fills ORDER BY id"
    ).fetchall()
    con.close()
    m = rs.rotate(
        store, now=NOW, window_s=SEED_WINDOW_S, manifest_dir=tmp_path / "b",
        liveness_window_s_=60.0, skip_process_probe=True,
    )
    built = m["built"]
    assert built["schema_source"].startswith("Store.open")
    assert built["columns_defaulted"] == {"fills": ["exchange_fill_id"]}
    # the two vitals-owned tables the bot's DDL does not know come from the archive
    assert set(built["archive_only_tables"]) == {"daily_ruin_anchors", "daily_realized_events"}
    con = rs.connect_ro(store)
    cols = [r[1] for r in con.execute("PRAGMA table_info(fills)")]
    assert "exchange_fill_id" in cols
    # every archive row carried, same ids, the new column NULL (its default)
    rows = con.execute(
        "SELECT id, fill_ref, order_id, combo_ticker, exchange_fill_id FROM fills ORDER BY id"
    ).fetchall()
    con.close()
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == before_fills
    assert all(r[4] is None for r in rows)
    assert len(rows) == counts["fills"]
    # the watermark is still the archive's, not re-stamped by the fresh open
    meta = dict(_rows(store, "store_meta"))
    assert meta[Store.META_FILLS_VERIFICATION_WATERMARK_ID] == "0"
    assert meta[Store.META_FILLS_VERIFICATION_MIGRATED_AT].startswith("2026-09-05T18")
    # and the fresh store opens through the real Store cleanly
    s1 = await Store.open(store, clock)
    try:
        migrated_at = meta[Store.META_FILLS_VERIFICATION_MIGRATED_AT]
        assert await s1.fills_verification_watermark() == (0, migrated_at)
    finally:
        await s1.close()
    # the exact operator sequence is in the manifest
    steps = m["next_steps"]
    assert steps[0].startswith("1. ") and "START_BOT.bat" in steps[0]
    assert "--verify --manifest" in steps[2] and m["manifest_path"] in steps[2]
    assert m["archive"] in steps[3]
    assert _read_json(Path(m["manifest_path"]))["next_steps"] == steps


async def test_refuses_an_archive_column_the_live_ddl_no_longer_creates(
    synthetic: tuple[Path, dict[str, int]], tmp_path: Path
) -> None:
    """Data is never silently dropped: an archive column absent from the live
    schema is a refusal, and the archive is renamed back."""
    store, _ = synthetic
    con = sqlite3.connect(store)
    con.execute("ALTER TABLE fills ADD COLUMN legacy_extra TEXT")
    con.commit()
    con.close()
    before = _rows(store, "fills")
    with pytest.raises(RuntimeError, match="REFUSED.*legacy_extra"):
        rs.rotate(
            store, now=NOW, window_s=SEED_WINDOW_S, manifest_dir=tmp_path / "b",
            liveness_window_s_=60.0, skip_process_probe=True,
        )
    assert store.exists() and not list(store.parent.glob("*.archive-*"))
    assert not list(store.parent.glob("*.rotating-*"))
    assert _rows(store, "fills") == before
    assert len(list((tmp_path / "b").glob("*-FAILED.json"))) == 1


async def test_dry_run_reports_the_retention_alternative(
    synthetic: tuple[Path, dict[str, int]], capsys: pytest.CaptureFixture[str]
) -> None:
    store, _ = synthetic
    p = rs.plan(store, now=NOW, window_s=SEED_WINDOW_S, measure=False)
    r = p["retention"]
    assert r["retention_window_s"] == 2 * SEED_WINDOW_S  # reader + cadence, 0 disorder
    assert r["tables"]["decisions"]["rows_estimate"] == 72
    assert set(r["protected"]["tickers"]) == {TAPE_ONLY, CONFLICT}
    rs.print_plan(p)
    text = capsys.readouterr().out
    assert "RETENTION ALTERNATIVE (dark" in text
    assert "protected rfqs rows (leg provenance): 4" in text
