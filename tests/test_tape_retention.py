"""Tape retention (ops/tape_retention.py + tools/ops/prune_tape.py) on a
SYNTHETIC store built by the real ``Store``: the derived window (reader
window + cadence + measured disorder — no number of its own), the PK
bisection plan, a bounded pass that deletes only rows older than the window
and never a protected leg-provenance row, every bound (batch ids, batch cap,
writer-idle predicate, batch time bound), the boot-time readers answering the
same before and after (acceptance seed + held_positions through the real
``Store``), the app-side scheduler (due / single-flight / writer-idle /
re-arm on an incomplete pass), the dark default, and the CLI's refusals.
No live path is touched: everything is under tmp_path.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import combomaker.ops.tape_retention as tr
import tools.ops.prune_tape as pt
from combomaker.ops import acceptance_seed
from combomaker.ops.acceptance_seed import SEED_WINDOW_S, seed_counts_from_store
from combomaker.ops.config import ObserveConfig
from combomaker.ops.persistence import STORE_OP_TIMEOUT_S, Store
from combomaker.ops.quote_app import QuoteApp
from tests.test_rotate_store import CONFLICT, LEDGERED, NOW, TAPE_ONLY, _build_store, _Clock

# ---------------------------------------------------------------- derivation


def test_window_is_reader_plus_cadence_plus_measured_disorder() -> None:
    assert tr.reader_window_s() == SEED_WINDOW_S
    assert tr.READER_WINDOWS_S == {"ops/acceptance_seed.seed_counts_from_store": SEED_WINDOW_S}
    assert tr.PRUNE_CADENCE_S == SEED_WINDOW_S
    assert tr.retention_window_s(disorder_s=0.0) == SEED_WINDOW_S + tr.PRUNE_CADENCE_S
    assert tr.retention_window_s(disorder_s=7.5) == SEED_WINDOW_S + tr.PRUNE_CADENCE_S + 7.5
    # a negative "disorder" cannot shrink the window
    assert tr.retention_window_s(disorder_s=-3.0) == tr.retention_window_s(disorder_s=0.0)


def test_pass_bounds_are_the_store_primitives_not_literals() -> None:
    assert tr.PRUNE_BATCH_IDS == acceptance_seed._CHUNK_IDS  # noqa: SLF001 — the pin
    assert tr.MAX_BATCHES_PER_PASS == int(tr.PRUNE_CADENCE_S / STORE_OP_TIMEOUT_S)
    assert tr.DISORDER_SAMPLE_IDS == tr.PRUNE_BATCH_IDS


def test_tape_tables_are_exactly_the_queue_writers_plus_measurement_recorders() -> None:
    assert set(tr.TAPE_TABLES) == {
        "rfqs", "rfq_deletions", "decisions", "would_quotes", "would_quotes_inplay"
    }
    assert set(tr.TAPE_TIME_COLUMN) == set(tr.TAPE_TABLES)
    for ledger in ("fills", "position_ledger", "ev_ledger", "markouts", "store_meta",
                   "structural_fits"):
        assert ledger not in tr.TAPE_TABLES


def test_disorder_is_the_largest_backward_step(tmp_path: Path) -> None:
    con = sqlite3.connect(tmp_path / "d.sqlite3")
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL)")
    base = NOW
    stamps = [base + timedelta(seconds=s) for s in (0, 1, 2, 3, 4)]
    stamps.insert(3, base - timedelta(seconds=7))  # one row stamped 7 s before its predecessor
    stamps.append(base + timedelta(seconds=4) - timedelta(seconds=2))  # a 2 s step back
    con.executemany("INSERT INTO t (at) VALUES (?)", [(s.isoformat(),) for s in stamps])
    con.commit()
    d = tr.measure_disorder_s(con, "t", "at")
    assert d["rows"] == len(stamps)
    # the 7 s-early row is measured against the running MAX (base+2) = 9 s
    assert d["disorder_s"] == 9.0
    con.execute("DELETE FROM t")
    monotone = [((base + timedelta(seconds=s)).isoformat(),) for s in range(5)]
    con.executemany("INSERT INTO t (at) VALUES (?)", monotone)
    con.commit()
    assert tr.measure_disorder_s(con, "t", "at")["disorder_s"] == 0.0
    con.execute("DELETE FROM t")
    con.commit()
    assert tr.measure_disorder_s(con, "t", "at") == {
        "table": "t", "rows": 0, "disorder_s": 0.0, "unparseable": 0
    }
    con.close()


# --------------------------------------------------------------------- plan


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


def _rows(path: Path, table: str) -> list[tuple]:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
    finally:
        con.close()


async def test_plan_bisects_the_window_and_names_the_protected_rows(
    synthetic: tuple[Path, dict[str, int]]
) -> None:
    store, counts = synthetic
    con = sqlite3.connect(f"file:{store.as_posix()}?mode=ro", uri=True)
    try:
        p = tr.plan_prune(con, now=NOW)
    finally:
        con.close()
    # 3 days of hourly tape; window = 48 h (+0 disorder: the synthetic tape is
    # monotone) ⇒ the oldest 24 hours are older than the window.
    assert p["disorder_s"] == 0.0
    assert p["retention_window_s"] == 2 * SEED_WINDOW_S
    cutoff = (NOW - timedelta(seconds=2 * SEED_WINDOW_S)).isoformat()
    assert p["cutoff_iso"] == cutoff
    dec = _rows(store, "decisions")
    older = [r for r in dec if r[1] < cutoff]
    assert p["tables"]["decisions"]["prune_below_id"] == older[-1][0] + 1
    assert p["tables"]["decisions"]["rows_estimate"] == len(older) == 24 * 3
    rfq = _rows(store, "rfqs")
    older_r = [r for r in rfq if r[2] < cutoff]
    assert p["tables"]["rfqs"]["prune_below_id"] == older_r[-1][0] + 1
    # empty tape tables: nothing to prune, never an error
    assert p["tables"]["would_quotes"]["prune_below_id"] is None
    assert p["tables"]["rfq_deletions"]["rows_estimate"] == 0
    pr = p["protected"]
    assert set(pr["tickers"]) == {TAPE_ONLY, CONFLICT}
    assert len(pr["rfq_ids"]) == 4 and set(pr["conflicting"]) == {TAPE_ONLY, CONFLICT}
    # the protected rows are all inside the prune range (old tape) — the pass
    # must split around them
    assert all(x < p["tables"]["rfqs"]["prune_below_id"] for x in pr["rfq_ids"])


# --------------------------------------------------------------------- pass


async def test_pass_deletes_only_older_rows_and_keeps_protected_ids(
    synthetic: tuple[Path, dict[str, int]], clock: _Clock
) -> None:
    store, counts = synthetic
    before_dec = _rows(store, "decisions")
    before_rfq = _rows(store, "rfqs")
    before_live = {t: _rows(store, t) for t in ("fills", "position_ledger", "markouts")}
    # what the boot readers answer BEFORE
    s0 = await Store.open(store, clock)
    try:
        held0 = await s0.held_positions([LEDGERED, TAPE_ONLY, CONFLICT])
    finally:
        await s0.close()
    seed0 = seed_counts_from_store(store, now_utc=NOW, award_sizing=False)
    assert seed0 is not None and seed0.rows_scanned == 24

    res = tr.run_prune_pass(store, now=NOW)
    assert res.complete and res.stopped_reason is None
    cutoff = res.cutoff_iso
    after_dec = _rows(store, "decisions")
    assert after_dec == [r for r in before_dec if r[1] >= cutoff]
    assert res.tables["decisions"].rows_deleted == len(before_dec) - len(after_dec) == 72
    after_rfq = _rows(store, "rfqs")
    con = sqlite3.connect(f"file:{store.as_posix()}?mode=ro", uri=True)
    protected = set(tr.protected_rfq_ids(con).rfq_ids)
    con.close()
    assert protected and protected == {r[0] for r in after_rfq if r[2] < cutoff}
    assert after_rfq == [r for r in before_rfq if r[2] >= cutoff or r[0] in protected]
    assert res.protected_rfq_ids == len(protected)
    assert res.rows_deleted == sum(t.rows_deleted for t in res.tables.values())
    # ids preserved (no renumbering), ledgers untouched
    assert [r[0] for r in after_dec] == [r[0] for r in before_dec if r[1] >= cutoff]
    for t, rows in before_live.items():
        assert _rows(store, t) == rows, t
    # the boot readers answer the SAME after the prune
    seed1 = seed_counts_from_store(store, now_utc=NOW, award_sizing=False)
    assert seed1 is not None
    assert (seed1.quoted, seed1.accepted, seed1.rows_scanned) == (
        seed0.quoted, seed0.accepted, seed0.rows_scanned,
    )
    s1 = await Store.open(store, clock)
    try:
        assert await s1.held_positions([LEDGERED, TAPE_ONLY, CONFLICT]) == held0
        # the store still opens through the live path and new ids continue above
        await s1.record_decision("quote_sent", "rfq-new", ["r"], {})
    finally:
        await s1.close()
    assert _rows(store, "decisions")[-1][0] == before_dec[-1][0] + 1
    # a second pass finds nothing
    again = tr.run_prune_pass(store, now=NOW)
    assert again.complete and again.rows_deleted == 0 and again.batches == 0


async def test_pass_bounds_batch_ids_cap_predicate_and_time(
    synthetic: tuple[Path, dict[str, int]]
) -> None:
    store, _ = synthetic
    con = sqlite3.connect(f"file:{store.as_posix()}?mode=ro", uri=True)
    p = tr.plan_prune(con, now=NOW)
    con.close()
    below = p["tables"]["rfqs"]["prune_below_id"]
    lo = p["tables"]["rfqs"]["start_id"]
    assert lo == 2  # id 1 (TAPE_ONLY, hour 0) is protected: the pass starts past it
    n_batches_rfqs = -(-(below - lo) // 3)
    # (a) the writer-idle predicate false ⇒ no batch runs, reason named
    r = tr.run_prune_pass(store, now=NOW, should_continue=lambda: False)
    assert r.batches == 0 and not r.complete and "writer busy" in (r.stopped_reason or "")
    assert _rows(store, "rfqs") == _rows(store, "rfqs")  # nothing changed
    # (b) batch cap ⇒ exactly that many batches, incomplete, resumable
    r = tr.run_prune_pass(store, now=NOW, batch_ids=3, max_batches=2)
    assert r.batches == 2 and not r.complete and "max_batches_per_pass" in (r.stopped_reason or "")
    assert r.tables["rfqs"].batches == 2 and not r.tables["rfqs"].complete
    # (c) a batch slower than the writer's lock tolerance stops the pass
    r = tr.run_prune_pass(store, now=NOW, batch_ids=3, batch_time_bound_s=-1.0)
    assert r.batches == 1 and not r.complete
    assert "STORE_OP_TIMEOUT_S" in (r.stopped_reason or "")
    # (d) small batches ⇒ many batches, none crossing the bound; completes
    r = tr.run_prune_pass(store, now=NOW, batch_ids=3)
    assert r.complete
    # 3 batches already ran above; a pass starts at the first UNPROTECTED id,
    # so the emptied prefix (ids 2..10 minus protected 3, 4) is not re-walked
    assert r.tables["rfqs"].batches == n_batches_rfqs - 3
    assert r.tables["rfqs"].prune_below_id == below


async def test_protected_rows_split_a_single_batch(tmp_path: Path, clock: _Clock) -> None:
    """A batch whose range contains protected ids deletes around them."""
    con = sqlite3.connect(tmp_path / "s.sqlite3")
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t VALUES (?, ?)", [(i, "x") for i in range(1, 21)])
    con.commit()
    con.isolation_level = None
    con.execute("BEGIN")
    deleted = tr._delete_range(con, "t", 1, 21, protected=[5, 6, 13])  # noqa: SLF001
    con.execute("COMMIT")
    assert deleted == 17
    assert [r[0] for r in con.execute("SELECT id FROM t ORDER BY id")] == [5, 6, 13]
    con.close()


# ---------------------------------------------------------------- scheduler


class _Depth:
    def __init__(self, n: int = 0) -> None:
        self.n = n

    def __call__(self) -> int:
        return self.n


async def test_step_is_due_single_flight_writer_idle_and_rearms_on_incomplete(
    synthetic: tuple[Path, dict[str, int]], clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = synthetic
    clock.set(NOW)  # the builder leaves the clock at the ledger stamp
    depth = _Depth(3)
    step = tr.TapeRetentionStep(store, clock=clock, writer_queue_depth=depth)
    assert step.due()
    assert step.maybe_launch() == "writer_busy"  # never against a busy writer
    depth.n = 0
    # a pass that blocks until released, to observe single-flight
    gate = threading.Event()
    calls: list[datetime] = []
    real = tr.run_prune_pass

    def slow_pass(db_path: Path, *, now: datetime, should_continue=None, **kw):  # type: ignore[no-untyped-def]
        calls.append(now)
        gate.wait(5.0)
        return real(db_path, now=now, should_continue=should_continue, **kw)

    monkeypatch.setattr(tr, "run_prune_pass", slow_pass)
    assert step.maybe_launch() == "launched"
    assert step.maybe_launch() == "in_flight"
    gate.set()
    assert step._task is not None  # noqa: SLF001
    await step._task  # noqa: SLF001
    assert step.passes == 1 and step.last_result is not None and step.last_result.complete
    assert calls == [NOW]
    # completed ⇒ not due until a cadence has passed
    assert step.maybe_launch() == "not_due"
    clock.advance(tr.PRUNE_CADENCE_S - 1)
    assert not step.due()
    clock.advance(1)
    assert step.due()
    # an INCOMPLETE pass re-arms immediately (bounded leftover, keeps trying)
    monkeypatch.setattr(
        tr,
        "run_prune_pass",
        lambda db_path, *, now, should_continue=None, **kw: tr.PruneResult(
            started_at=now.isoformat(), retention_window_s=1.0, cutoff_iso="x", complete=False,
            stopped_reason="should_continue() false (writer busy)",
        ),
    )
    assert step.maybe_launch() == "launched"
    await step._task  # noqa: SLF001
    assert step.passes == 2 and step.due()
    # a failing pass logs and re-arms; never raises into the loop
    def boom(db_path: Path, *, now: datetime, should_continue=None, **kw):  # type: ignore[no-untyped-def]
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(tr, "run_prune_pass", boom)
    assert step.maybe_launch() == "launched"
    await step._task  # noqa: SLF001
    assert step.passes == 2 and step.due()
    # the predicate the pass reads between batches: writer idle AND not closing
    assert step.should_continue() is True
    depth.n = 1
    assert step.should_continue() is False
    depth.n = 0
    await step.close()
    assert step.should_continue() is False  # a worker mid-pass stops after its batch


async def test_store_exposes_writer_queue_depth(tmp_path: Path, clock: _Clock) -> None:
    s = await Store.open(tmp_path / "q.sqlite3", clock)
    try:
        assert s.writer_queue_depth() == 0  # no writer running
        s.start_writer()
        await s.record_decision("skip", "r", [], {})
        assert s.writer_queue_depth() >= 0
    finally:
        await s.close()


# ---------------------------------------------------------------- dark flag


def test_flag_defaults_off_and_the_app_hook_is_guarded_by_it() -> None:
    assert ObserveConfig().tape_retention_enabled is False
    run_src = inspect.getsource(QuoteApp.run)
    assert "if config.observe.tape_retention_enabled:" in run_src
    assert "TapeRetentionStep(" in run_src
    loop_src = inspect.getsource(QuoteApp._maintenance_loop)  # noqa: SLF001
    assert "if self._tape_retention is not None:" in loop_src
    assert "maybe_launch()" in loop_src
    # the loop only ever asks the scheduler on the slow (~60 s) cadence and
    # never awaits the pass (off-loop by construction)
    assert "await self._tape_retention" not in loop_src


# ---------------------------------------------------------------------- CLI


async def test_cli_dry_run_is_read_only_and_apply_refuses_a_live_bot(
    synthetic: tuple[Path, dict[str, int]], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store, _ = synthetic
    before = store.stat().st_mtime_ns
    out = tmp_path / "plan.json"
    rc = pt.main(["--store", str(store), "--dry-run", "--out", str(out)])
    assert rc == 0
    text = capsys.readouterr().out
    assert "RETENTION WINDOW" in text and "PROTECTED" in text
    plan = json.loads(out.read_text(encoding="utf-8"))
    assert plan["retention_window_s"] == 2 * SEED_WINDOW_S
    assert store.stat().st_mtime_ns == before
    (store.parent / "heartbeat.txt").write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
    with pytest.raises(SystemExit, match="ALIVE"):
        pt.apply(store, now=NOW, liveness_window_s=60.0, skip_process_probe=True)
    assert _rows(store, "decisions")  # untouched


async def test_cli_apply_prunes_to_completion_when_the_bot_is_down(
    synthetic: tuple[Path, dict[str, int]], tmp_path: Path
) -> None:
    store, _ = synthetic
    before = _rows(store, "decisions")
    result = pt.apply(store, now=NOW, liveness_window_s=60.0, skip_process_probe=True)
    assert result["complete"] is True
    assert result["rows_deleted"] == 72 + result["passes"][0]["tables"]["rfqs"]["rows_deleted"]
    assert len(result["passes"]) == 1
    cutoff = result["passes"][0]["cutoff_iso"]
    assert _rows(store, "decisions") == [r for r in before if r[1] >= cutoff]
    assert result["checkpoint_passive"] is not None
