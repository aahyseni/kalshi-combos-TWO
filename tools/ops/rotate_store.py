"""QUIESCED STORE ROTATION (2026-09-05 design, item 7) — READ-ONLY by default.

THE PROBLEM. The live store ``combomaker-prod-live-wc.sqlite3`` is ~213 GB
(52.2M pages); ``rfqs`` (66.4M rows) and ``decisions`` (134.3M rows) are the
bulk — the recorder TAPE, written through ``Store._write``'s bounded
drop-on-overflow queue. On a store this size every index insert is a random
4 KB read into B-trees that no longer fit any cache, so the single writer
thread falls behind the firehose: queue pinned at 200k, 1.86M dropped rows on
the 9/5 00:52 boot, the 12:13 boot's tape frozen after 16:19Z (no
``store_writer_stats`` emit for an hour, last ``decisions`` row 16:18:16Z),
5 s alarm-sweep timeouts, a 400 s read-only probe. The bot needs NONE of that
history to run: every boot-time reader (audited below) reads the small
LEDGER tables, plus the last ``SEED_WINDOW_S`` of ``decisions``/``rfqs`` for
the acceptance-tape seed and one leg set per held ticker for rehydration.

THE ROTATION (operator-driven, bot DOWN — this tool never starts or stops it):

  1. STOP_BOT.bat (kills the watchdog FIRST, then the bot; nothing relights).
  2. ``--apply`` REFUSES while the bot may be alive: the SAME liveness
     evidence as ``tools/ops/repair_phantom_fills.py`` (``heartbeat.txt``,
     ``supervisor_heartbeat.txt``, ``loop_progress.json`` — the bot's own
     readers, the supervisor's own window), plus the launch-site process
     predicate (``ours_predicate.ps1``) — and REFUSES while any OTHER hard
     link of the store carries a non-empty WAL (SQLite never tolerates two
     WALs on one inode; an open through that name would replay stale pages).
  3. ``PRAGMA wal_checkpoint(TRUNCATE)`` on the live store; REFUSE unless it
     completes (busy=0, every frame folded, ``-wal`` at 0 bytes) — a WAL that
     cannot be checkpointed means a connection is still open.
  4. RENAME the store to ``<name>.archive-YYYYMMDD`` (its ``-wal``/``-shm``
     siblings follow it). A rename is atomic and fails outright while any
     process holds the file — the natural backstop under the liveness checks.
  5. CREATE the fresh store at a temporary name through the REAL
     ``Store.open`` (the live DDL, every idempotent ADD COLUMN migration,
     every index — never a hand-written or copied schema; the two vitals-
     owned tables the bot's DDL does not know come from the archive's own
     DDL), then ONE transaction copying BY COLUMN NAME (an archive column
     the live code no longer creates is a refusal — data is never dropped):
       * every LIVE table whole, ids preserved (``INSERT ... SELECT *`` over
         an ATTACHed read-only archive): fills, position_ledger, ev_ledger,
         markouts, structural_fits, store_meta, daily_ruin_anchors,
         daily_realized_events, combo_trades (+ anything else not tape);
       * the TAPE the boot readers need: ``decisions`` rows in the seed
         window with kind IN ``SEED_KINDS``, ``rfqs`` rows in the seed
         window (the seed joins sizing terms by rfq_id), and one real
         ``rfqs`` row per distinct leg set for every fills ticker the
         position_ledger cannot resolve (``Store.held_positions`` tape
         fallback);
       * ``sqlite_sequence`` for every AUTOINCREMENT table, so ids in the
         fresh store continue ABOVE the archive's — no id ever names two rows
         across the boundary (``fills.id > watermark`` keeps its meaning).
     Then ``journal_mode=WAL``, ``quick_check``, and a row-count VERIFY of
     every copied table against the copy's own rowcounts. Any failure ⇒ the
     temp file is removed and the archive renamed back — nothing half-done
     ever carries the live name.
  6. ``os.replace`` the verified temp onto the live name; write the manifest
     (``data/backups/<stamp>-rotate_store_manifest.json``).
  7. START_BOT.bat. ``--verify --manifest <path>`` (read-only) then checks
     the live store against the manifest: LIVE-table counts ≥ the carried
     counts, the fills verification watermark unchanged, sequences ≥ carried.

The archive stays on disk for analysis tools, which must be pointed at it
EXPLICITLY (its name no longer matches ``combomaker*.sqlite3`` globs — the
vitals ``derive`` and the hang watchdog therefore read the FRESH store).

``--dry-run`` (default) touches nothing: sizes, hard links + stray WAL
headers, per-table counts, the seed-window bisection, the leg-provenance
carry, the boot-time reader audit, refusals that would fire NOW, and a copy
time estimate from a throughput measured on this box during the run.

Usage (worktree root):

  PYTHONPATH=src python tools/ops/rotate_store.py --dry-run [--out plan.json]
  PYTHONPATH=src python tools/ops/rotate_store.py --apply          # bot DOWN
  PYTHONPATH=src python tools/ops/rotate_store.py --verify --manifest <json>
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import struct
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]


def _src_on_path() -> None:
    for p in (REPO / "src", REPO):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)


_src_on_path()
# ONE definition of the tape (tables, time columns, the PK bisection and the
# protected leg-provenance rows) — shared with the dark in-process retention
# step so the rotation and the nightly prune can never disagree on what the
# boot-time readers need.
from combomaker.ops.tape_retention import (  # noqa: E402 — after sys.path
    TAPE_TABLES,
    TAPE_TIME_COLUMN,
    bisect_first_id,
    plan_prune,
    protected_rfq_ids,
)

DEFAULT_STORE = "D:/kalshi-combos-TWO-data/combomaker-prod-live-wc.sqlite3"
#: The decision kinds the acceptance-tape boot seed reads — keep in sync
#: with ``combomaker.ops.acceptance_seed.seed_counts_from_store`` (pass 1
#: ``kind = 'quote_sent'``, pass 2 ``kind IN ('confirm', 'decline')``);
#: ``tests/test_rotate_store.py`` pins the literals against that source.
SEED_KINDS: tuple[str, ...] = ("quote_sent", "confirm", "decline")

#: SQLite's own busy wait for the checkpoint connection (the store's
#: ``BUSY_TIMEOUT_MS`` is the bot's statement of "how long a lock wait may
#: legitimately take"; imported, never restated, when the package is present).
_WAL_MAGIC = (0x377F0682, 0x377F0683)
_PROBE_ROWS = 2_000  # rows read to MEASURE the tape read rate (a sample size)

JsonDict = dict[str, Any]


def seed_window_s() -> int:
    """The boot seed's own window — the reader defines what must be carried."""
    _src_on_path()
    from combomaker.ops.acceptance_seed import SEED_WINDOW_S

    return int(SEED_WINDOW_S)


def busy_timeout_ms() -> int:
    _src_on_path()
    from combomaker.ops.persistence import BUSY_TIMEOUT_MS

    return int(BUSY_TIMEOUT_MS)


# ------------------------------------------------------------------ files


def wal_header(path: Path) -> JsonDict | None:
    """Parse a WAL file's 32-byte header (read-only). None if absent."""
    if not path.exists():
        return None
    size = path.stat().st_size
    out: JsonDict = {"path": str(path), "bytes": size}
    if size < 32:
        out["frames"] = 0
        return out
    with path.open("rb") as fh:
        hdr = fh.read(32)
    magic, version, page_size, ckpt_seq, salt1, salt2 = struct.unpack(">6I", hdr[:24])
    out.update(
        magic_ok=magic in _WAL_MAGIC,
        version=version,
        page_size=page_size,
        checkpoint_seq=ckpt_seq,
        salt=[salt1, salt2],
        frames=(size - 32) // (24 + page_size) if page_size else None,
    )
    return out


def hard_link_names(path: Path) -> tuple[int, list[Path], str | None]:
    """``(st_nlink, other names, note)``. On Windows the other names come from
    ``fsutil hardlink list`` (drive-less paths, re-rooted on the store's drive);
    elsewhere only the count is known."""
    try:
        nlink = int(os.stat(path).st_nlink)
    except OSError as exc:
        return 0, [], f"stat failed: {exc!r}"
    if nlink <= 1:
        return nlink, [], None
    if sys.platform != "win32":
        return nlink, [], "other names not enumerable on this platform"
    try:
        out = subprocess.run(
            ["fsutil", "hardlink", "list", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 — enumeration is best-effort
        return nlink, [], f"fsutil failed: {exc!r}"
    if out.returncode != 0:
        return nlink, [], f"fsutil rc={out.returncode}: {out.stderr.strip()[:200]}"
    drive = path.resolve().drive
    me = path.resolve()
    others: list[Path] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        cand = Path(drive + line) if not Path(line).drive else Path(line)
        try:
            if cand.resolve() == me:
                continue
        except OSError:
            pass
        others.append(cand)
    return nlink, others, None


def store_files(store: Path) -> JsonDict:
    """Sizes of the store and its WAL/SHM, every other hard link of the store
    with ITS sibling WAL/SHM (a non-empty foreign WAL is a refusal)."""
    wal = Path(str(store) + "-wal")
    shm = Path(str(store) + "-shm")
    nlink, others, note = hard_link_names(store)
    foreign: list[JsonDict] = []
    for name in others:
        fw = Path(str(name) + "-wal")
        fs = Path(str(name) + "-shm")
        foreign.append(
            {
                "name": str(name),
                "wal": wal_header(fw),
                "shm_bytes": fs.stat().st_size if fs.exists() else None,
            }
        )
    return {
        "store": str(store),
        "store_bytes": store.stat().st_size if store.exists() else None,
        "wal": wal_header(wal),
        "shm_bytes": shm.stat().st_size if shm.exists() else None,
        "hard_links": nlink,
        "other_names": foreign,
        "hard_link_note": note,
    }


def foreign_wal_refusals(files: JsonDict) -> list[str]:
    out: list[str] = []
    for other in files.get("other_names", []):
        w = other.get("wal")
        if w and (w.get("frames") or 0) > 0:
            out.append(
                f"{other['name']}: hard link of the store with its OWN WAL of "
                f"{w['frames']} frames ({w['bytes']} bytes, checkpoint_seq "
                f"{w.get('checkpoint_seq')}) — frames written through that name are "
                "invisible to the live connection and an open through it would replay "
                "them onto whatever the file holds then; move that -wal/-shm aside "
                "(and drop the extra link) before rotating"
            )
    return out


# --------------------------------------------------------------- inspect


def connect_ro(path: Path, *, timeout_s: float = 5.0) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=timeout_s)


def schema_objects(con: sqlite3.Connection) -> list[JsonDict]:
    return [
        {"type": r[0], "name": r[1], "tbl_name": r[2], "sql": r[3]}
        for r in con.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master"
            " WHERE name NOT LIKE 'sqlite_%' ORDER BY type DESC, name"
        )
    ]


def user_tables(objects: list[JsonDict]) -> list[str]:
    return [o["name"] for o in objects if o["type"] == "table"]


def table_facts(con: sqlite3.Connection, tables: list[str]) -> dict[str, JsonDict]:
    """LIVE tables: exact COUNT(*). TAPE tables: PK bounds + first/last time
    (O(log n) point reads — a COUNT on 134M rows is a scan)."""
    facts: dict[str, JsonDict] = {}
    for t in tables:
        info = con.execute(f'PRAGMA table_info("{t}")').fetchall()
        d: JsonDict = {
            "columns": [r[1] for r in info],
            "pk": [r[1] for r in info if r[5]],
            "tape": t in TAPE_TABLES,
        }
        if t in TAPE_TABLES:
            col = TAPE_TIME_COLUMN.get(t)
            row = con.execute(f'SELECT MIN(id), MAX(id) FROM "{t}"').fetchone()  # noqa: S608
            d["min_id"], d["max_id"] = row
            if row[0] is not None and col:
                d["first_at"] = con.execute(
                    f'SELECT "{col}" FROM "{t}" WHERE id >= ? ORDER BY id LIMIT 1',  # noqa: S608
                    (row[0],),
                ).fetchone()[0]
                d["last_at"] = con.execute(
                    f'SELECT "{col}" FROM "{t}" WHERE id <= ? ORDER BY id DESC LIMIT 1',  # noqa: S608
                    (row[1],),
                ).fetchone()[0]
            d["rows_estimate"] = 0 if row[0] is None else int(row[1]) - int(row[0]) + 1
        else:
            d["count"] = int(con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])  # noqa: S608
        facts[t] = d
    return facts


def seed_carry_plan(
    con: sqlite3.Connection, facts: dict[str, JsonDict], *, now: datetime, window_s: int
) -> JsonDict:
    """What of the tape the boot-time readers need: the seed window of
    ``decisions`` (seed kinds only) and ``rfqs`` (all rows — the seed joins
    sizing terms by rfq_id, ``held_positions`` may need recent leg sets)."""
    cutoff = datetime.fromtimestamp(now.timestamp() - window_s, tz=UTC).isoformat()
    plan: JsonDict = {"window_s": window_s, "cutoff_iso": cutoff, "tables": {}}
    if "decisions" in facts:
        first = bisect_first_id(con, "decisions", "at", cutoff)
        per_kind: dict[str, int] = {}
        if first is not None:
            for kind in SEED_KINDS:
                per_kind[kind] = int(
                    con.execute(
                        "SELECT COUNT(*) FROM decisions WHERE kind = ? AND id >= ?",
                        (kind, first),
                    ).fetchone()[0]
                )
        plan["tables"]["decisions"] = {
            "first_id": first,
            "max_id": facts["decisions"].get("max_id"),
            "kinds": list(SEED_KINDS),
            "rows_by_kind": per_kind,
            "rows": sum(per_kind.values()),
            "window_rows_all_kinds_estimate": (
                0 if first is None else int(facts["decisions"]["max_id"]) - first + 1
            ),
        }
    if "rfqs" in facts:
        first = bisect_first_id(con, "rfqs", "seen_at", cutoff)
        plan["tables"]["rfqs"] = {
            "first_id": first,
            "max_id": facts["rfqs"].get("max_id"),
            "rows_estimate": 0 if first is None else int(facts["rfqs"]["max_id"]) - first + 1,
        }
    return plan


def leg_provenance_plan(con: sqlite3.Connection, tables: list[str]) -> JsonDict:
    """``Store.held_positions`` resolves legs LEDGER-FIRST and falls back to the
    rfqs tape for a fills ticker with no position_ledger row. Carry ONE real
    rfqs row per distinct (market_ticker, legs_json) for exactly those tickers
    — the SAME protected set the nightly prune never deletes
    (``tape_retention.protected_rfq_ids``)."""
    if not {"fills", "position_ledger", "rfqs"} <= set(tables):
        return {"tickers": [], "rfq_ids": [], "conflicting": [], "unresolvable": []}
    return protected_rfq_ids(con).as_dict()


def measure_read_rate(
    con: sqlite3.Connection, tables: list[str], seed_plan: JsonDict
) -> JsonDict:
    """A throughput MEASURED on this box, right now: copy every LIVE table into
    an in-memory database (rows/s) and read the first ``_PROBE_ROWS`` in-window
    decisions rows (bytes/s of tape). Nothing on disk is written."""
    t0 = time.monotonic()
    mem = sqlite3.connect(":memory:")
    live_rows = 0
    live_bytes = 0
    try:
        for t in tables:
            if t in TAPE_TABLES:
                continue
            sql = con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone()
            if not sql or not sql[0]:
                continue
            mem.execute(sql[0])
            cur = con.execute(f'SELECT * FROM "{t}"')  # noqa: S608
            width = len(cur.description)
            marks = ",".join("?" * width)
            while True:
                chunk = cur.fetchmany(_PROBE_ROWS)
                if not chunk:
                    break
                mem.executemany(f'INSERT INTO "{t}" VALUES ({marks})', chunk)  # noqa: S608
                live_rows += len(chunk)
                live_bytes += sum(len(str(c)) for row in chunk for c in row)
        mem.commit()
    finally:
        mem.close()
    live_s = time.monotonic() - t0
    tape_bytes = 0
    tape_rows = 0
    tape_s = 0.0
    dec = seed_plan.get("tables", {}).get("decisions")
    if dec and dec.get("first_id") is not None:
        t1 = time.monotonic()
        for row in con.execute(
            "SELECT * FROM decisions WHERE id >= ? ORDER BY id LIMIT ?",
            (dec["first_id"], _PROBE_ROWS),
        ):
            tape_rows += 1
            tape_bytes += sum(len(str(c)) for c in row)
        tape_s = time.monotonic() - t1
    return {
        "live_rows": live_rows,
        "live_bytes_text": live_bytes,
        "live_s": round(live_s, 3),
        "live_rows_per_s": round(live_rows / live_s, 1) if live_s > 0 else None,
        "tape_probe_rows": tape_rows,
        "tape_probe_bytes_text": tape_bytes,
        "tape_probe_s": round(tape_s, 3),
        "tape_bytes_per_s": round(tape_bytes / tape_s) if tape_s > 0 else None,
        "tape_bytes_per_row": round(tape_bytes / tape_rows) if tape_rows else None,
    }


#: Every reader of store HISTORY at boot or on the slow loops, what it reads,
#: and whether the rotation carries it. Static: the audit of 2026-09-05
#: (grep of every ``Store.`` reader outside persistence.py + the two direct
#: sqlite users). Printed by --dry-run so the operator sees the contract.
BOOT_READERS: tuple[JsonDict, ...] = (
    {
        "reader": "Store.open (ops/persistence.py) — DDL, ADD COLUMNs, unique indexes, "
        "_ensure_fills_verification_columns",
        "tables": ["store_meta", "fills"],
        "carried": "yes — store_meta whole, so the fills verification WATERMARK "
        "(id + migrated_at) is the archive's, not re-stamped at 0/now",
    },
    {
        "reader": "quote_app._startup rehydrate → Store.held_positions",
        "tables": ["fills", "position_ledger", "rfqs (fallback)"],
        "carried": "yes — fills + ledger whole; one real rfqs row per leg set for every "
        "fills ticker the ledger cannot resolve",
    },
    {
        "reader": "quote_app position reconcile → has_fill_for_ticker, held_positions, "
        "ledger_quantity_reconcile_once (fills_verification_watermark, "
        "open_ledger_quantity_by_ticker)",
        "tables": ["fills", "position_ledger", "store_meta"],
        "carried": "yes",
    },
    {
        "reader": "quote_app day-anchored realized seed → Store.day_realized_pnl_cc",
        "tables": ["position_ledger.reconciled_at", "fills.at/fee_cc/status"],
        "carried": "yes",
    },
    {
        "reader": "ops/acceptance_seed.seed_counts_from_store (second ro connection, "
        "SEED_WINDOW_S)",
        "tables": ["decisions (quote_sent/confirm/decline)", "rfqs (by rfq_id)"],
        "carried": "yes — the seed window of both, bisected on the PK exactly as the "
        "seed does",
    },
    {
        "reader": "lifecycle fill-verification re-arm → fills_verification_watermark, "
        "booked_unverified_fills",
        "tables": ["store_meta", "fills"],
        "carried": "yes",
    },
    {
        "reader": "lifecycle retained-floor sweep → Store.settled_grade_rows",
        "tables": ["position_ledger (settled)", "fills"],
        "carried": "yes",
    },
    {
        "reader": "lifecycle ledger-divergence sweep → open_ledger_identities",
        "tables": ["position_ledger (open)"],
        "carried": "yes",
    },
    {
        "reader": "lifecycle fills-ledger sweep → fill_order_ids, fill_null_order_id_keys",
        "tables": ["fills"],
        "carried": "yes",
    },
    {
        "reader": "risk/settlement orphan reconcile → open_ledger_tickers, "
        "open_ledger_rows_for_ticker, record_position_settled, settle_ev_entry",
        "tables": ["position_ledger", "ev_ledger"],
        "carried": "yes",
    },
    {
        "reader": "tools/ops/hang_watchdog.store_sig (last decisions.at, newest *.sqlite3)",
        "tables": ["decisions"],
        "carried": "window only — the fresh store is the newest *.sqlite3; the archive "
        "name does not match the glob",
    },
    {
        "reader": "tools/vitals/derive.risk_bankroll_cc / live_open_positions "
        "(combomaker*.sqlite3 glob)",
        "tables": ["daily_ruin_anchors", "position_ledger"],
        "carried": "yes — daily_ruin_anchors is not written by the bot (2 rows, last "
        "2026-07-16); carried so the gate keeps its anchor",
    },
    {
        "reader": "ops/report.build_report (CLI report) — counts, decision_*_counts, "
        "ev_summary, markout_summary",
        "tables": ["rfqs", "decisions", "would_quotes", "ev_ledger", "markouts", "fills"],
        "carried": "ledgers yes; tape counts restart — point the report at the archive "
        "with --db for history",
    },
    {
        "reader": "data_dir FILES (not the store): fee_schedule_observed.json, "
        "metadata_cache.json, watchdog_tape.json, fill_prober_watermark.txt",
        "tables": [],
        "carried": "untouched — the rotation renames one file",
    },
    {
        "reader": "would_quotes_inplay (in-play shadow, measurement only)",
        "tables": ["would_quotes_inplay"],
        "carried": "NO — 3.25M rows, no boot reader; the archive keeps the study",
    },
)


def plan(
    store: Path,
    *,
    now: datetime | None = None,
    window_s: int | None = None,
    measure: bool = True,
) -> JsonDict:
    """The read-only rotation plan (what --dry-run prints)."""
    now = now or datetime.now(UTC)
    window = window_s if window_s is not None else seed_window_s()
    t0 = time.monotonic()
    out: JsonDict = {"generated_at": now.isoformat(), "store": str(store)}
    out["files"] = store_files(store)
    con = connect_ro(store)
    try:
        out["page_size"] = int(con.execute("PRAGMA page_size").fetchone()[0])
        out["page_count"] = int(con.execute("PRAGMA page_count").fetchone()[0])
        out["freelist_count"] = int(con.execute("PRAGMA freelist_count").fetchone()[0])
        out["journal_mode"] = str(con.execute("PRAGMA journal_mode").fetchone()[0])
        objects = schema_objects(con)
        tables = user_tables(objects)
        out["objects"] = objects
        out["tables"] = table_facts(con, tables)
        out["sqlite_sequence"] = [
            [str(r[0]), int(r[1])]
            for r in con.execute("SELECT name, seq FROM sqlite_sequence")
        ]
        out["seed"] = seed_carry_plan(con, out["tables"], now=now, window_s=window)
        out["leg_provenance"] = leg_provenance_plan(con, tables)
        # The ALTERNATIVE (dark, observe.tape_retention_enabled): what the
        # nightly prune would derive and delete on THIS store — read-only.
        out["retention"] = plan_prune(con, now=now)
        if measure:
            out["measured"] = measure_read_rate(con, tables, out["seed"])
    finally:
        con.close()
    # Carry summary + estimate.
    live_rows = sum(v["count"] for v in out["tables"].values() if not v["tape"])
    tape_rows = 0
    dec = out["seed"]["tables"].get("decisions")
    rfq = out["seed"]["tables"].get("rfqs")
    if dec:
        tape_rows += int(dec["rows"])
    if rfq:
        tape_rows += int(rfq["rows_estimate"])
    tape_rows += len(out["leg_provenance"]["rfq_ids"])
    total_tape_ids = sum(
        int(v.get("rows_estimate") or 0) for v in out["tables"].values() if v["tape"]
    )
    db_bytes = out["page_size"] * out["page_count"]
    avg_row = db_bytes / max(1, total_tape_ids + live_rows)
    est: JsonDict = {
        "live_rows": live_rows,
        "tape_rows": tape_rows,
        "avg_bytes_per_row_whole_store": round(avg_row),
        "carry_bytes_estimate": round((live_rows + tape_rows) * avg_row),
        "not_carried_rows_estimate": total_tape_ids - tape_rows,
    }
    m = out.get("measured") or {}
    if m.get("tape_bytes_per_s") and m.get("tape_bytes_per_row"):
        # Text bytes of the tape rows ≈ their on-disk payload; a copy reads,
        # writes and indexes — ×3 of the measured READ time is the estimate's
        # stated shape, not a tuned constant.
        text_bytes = tape_rows * m["tape_bytes_per_row"]
        est["carry_text_bytes_estimate"] = text_bytes
        est["copy_time_s_estimate_read_x3"] = round(3 * text_bytes / m["tape_bytes_per_s"])
    if m.get("live_rows_per_s"):
        est["live_copy_s_measured_in_memory"] = m["live_s"]
    out["estimate"] = est
    out["readers"] = list(BOOT_READERS)
    refusals = foreign_wal_refusals(out["files"])
    wal = out["files"].get("wal")
    if wal and (wal.get("frames") or 0) > 0:
        refusals.append(
            f"live WAL holds {wal['frames']} frames — expected while the bot is up; "
            "--apply checkpoints it (TRUNCATE) and refuses if that cannot complete"
        )
    out["refusals_now"] = refusals
    out["plan_elapsed_s"] = round(time.monotonic() - t0, 1)
    return out


# --------------------------------------------------------------- liveness


def liveness_refusals(
    store: Path,
    *,
    window_s: float | None,
    config_path: Path | None,
    include_processes: bool = True,
) -> list[str]:
    """The repair tool's evidence (shared code, not a copy) + the launch-site
    process predicate. Every item is a reason to refuse."""
    _src_on_path()
    from tools.ops.repair_phantom_fills import (
        bot_liveness_evidence,
        default_config_path,
        liveness_window_s,
    )

    window = window_s if window_s is not None else liveness_window_s(
        config_path or default_config_path()
    )
    evidence = list(bot_liveness_evidence(str(store), window_s=window))
    if include_processes:
        evidence.extend(process_evidence())
    return evidence


def process_evidence() -> list[str]:
    """PIDs of OUR launch-site processes (``ours_predicate.ps1`` —
    Test-CombomakerOurs), each a refusal. Not Windows / probe failure ⇒ a
    note, never a fabricated 'clear' — the rename backstop still applies."""
    if sys.platform != "win32":
        return []
    script = (
        f'. "{REPO / "tools" / "ops" / "ours_predicate.ps1"}"; '
        "Get-CimInstance Win32_Process | Where-Object { (Test-CombomakerOurs $_) } | "
        "ForEach-Object { \"$($_.ProcessId) $($_.CommandLine)\" }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort probe
        return [f"process probe FAILED ({exc!r}) — cannot rule out a live bot"]
    if out.returncode != 0:
        return [f"process probe rc={out.returncode}: {out.stderr.strip()[:200]}"]
    lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    return [f"our process alive: {ln[:160]}" for ln in lines]


# ------------------------------------------------------------------ apply


def checkpoint_truncate(store: Path, *, timeout_ms: int) -> JsonDict:
    """ONE TRUNCATE checkpoint on the (quiesced) store. Returns the pragma's
    verdict; the caller refuses unless busy=0 and every frame folded."""
    con = sqlite3.connect(str(store), timeout=timeout_ms / 1000.0)
    try:
        con.execute(f"PRAGMA busy_timeout={int(timeout_ms)}")
        row = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        con.close()
    busy, log_frames, ckpt = (int(row[0]), row[1], row[2]) if row else (1, None, None)
    wal = Path(str(store) + "-wal")
    return {
        "busy": busy,
        "wal_frames": log_frames,
        "checkpointed": ckpt,
        "wal_bytes_after": wal.stat().st_size if wal.exists() else 0,
    }


def _move_siblings(src: Path, dst: Path) -> list[str]:
    moved: list[str] = []
    for suffix in ("-wal", "-shm"):
        s = Path(str(src) + suffix)
        if s.exists():
            d = Path(str(dst) + suffix)
            os.replace(s, d)
            moved.append(f"{s.name} -> {d.name}")
    return moved


def _remove_with_siblings(path: Path) -> None:
    for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass


def create_fresh_schema(fresh: Path) -> JsonDict:
    """The FULL live schema from the REAL ``Store.open`` (DDL, every idempotent
    ADD COLUMN migration, every index, WAL mode) — never a hand-written or
    copied schema. Opened and closed once through the bot's own path, exactly
    as the next boot will open it. Returns the resulting sqlite_master."""
    _src_on_path()
    import asyncio
    import threading

    from combomaker.core.clock import SystemClock
    from combomaker.ops.persistence import Store

    async def _open_close() -> None:
        store = await Store.open(fresh, SystemClock())
        await store.close()

    # Its own event loop on its own thread: the CLI has no loop, the tests
    # call this from inside one (asyncio.run refuses a running loop).
    failure: list[BaseException] = []

    def _target() -> None:
        try:
            asyncio.run(_open_close())
        except BaseException as exc:  # noqa: BLE001 — re-raised on the caller's thread
            failure.append(exc)

    worker = threading.Thread(target=_target, name="rotate-fresh-schema")
    worker.start()
    worker.join()
    if failure:
        raise failure[0]
    con = connect_ro(fresh)
    try:
        return {"objects": schema_objects(con)}
    finally:
        con.close()


def _columns(con: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [str(r[1]) for r in con.execute(f'PRAGMA {schema}.table_info("{table}")')]


def build_fresh_store(
    archive: Path,
    fresh: Path,
    *,
    plan_: JsonDict,
) -> JsonDict:
    """Schema via the live ``Store.open`` (``create_fresh_schema``); tables the
    bot's DDL does not know (vitals-owned ``daily_ruin_anchors`` /
    ``daily_realized_events``) come from the archive's own DDL; then ONE
    transaction copying, BY COLUMN NAME (a fresh-only column takes its DDL
    default; an archive column the live code no longer has is a REFUSAL —
    data is never silently dropped): the LIVE tables whole with ids, the tape
    seed window, the leg-provenance rows and sqlite_sequence; quick_check;
    row-count verify."""
    objects: list[JsonDict] = plan_["objects"]
    tables = user_tables(objects)
    seed = plan_["seed"]["tables"]
    prov_ids: list[int] = list(plan_["leg_provenance"]["rfq_ids"])
    copied: dict[str, int] = {}
    t0 = time.monotonic()
    fresh_schema = create_fresh_schema(fresh)
    fresh_names = {(o["type"], o["name"]) for o in fresh_schema["objects"]}
    archive_only_tables: list[str] = []
    archive_only_indexes: list[str] = []
    defaulted: dict[str, list[str]] = {}
    dst = sqlite3.connect(f"file:{fresh.resolve().as_posix()}?mode=rw", uri=True)
    try:
        dst.execute("PRAGMA synchronous=OFF")  # bulk load; the bot sets NORMAL at open
        dst.execute("ATTACH DATABASE ? AS src", (f"file:{archive.resolve().as_posix()}?mode=ro",))
        # Tables the live DDL does not create: the archive's own DDL (+ its
        # indexes on them). Named in the manifest — the operator sees them.
        for o in objects:
            if o["type"] == "table" and ("table", o["name"]) not in fresh_names and o["sql"]:
                dst.execute(o["sql"])
                archive_only_tables.append(o["name"])
        for o in objects:
            if o["type"] == "index" and ("index", o["name"]) not in fresh_names and o["sql"]:
                dst.execute(o["sql"])
                archive_only_indexes.append(o["name"])
        # Column parity: every archive column must exist in the fresh table.
        missing: list[str] = []
        col_lists: dict[str, str] = {}
        for t in tables:
            src_cols = _columns(dst, "src", t)
            dst_cols = _columns(dst, "main", t)
            lost = [c for c in src_cols if c not in dst_cols]
            if lost:
                missing.append(f"{t}: {lost}")
            extra = [c for c in dst_cols if c not in src_cols]
            if extra:
                defaulted[t] = extra
            col_lists[t] = ", ".join(f'"{c}"' for c in src_cols)
        if missing:
            raise RuntimeError(
                "REFUSED: the archive holds columns the live Store DDL no longer creates "
                "(data would be dropped): " + "; ".join(missing)
            )
        dst.execute("BEGIN")
        # The live open stamped its own fills-verification watermark (0/now) into
        # store_meta; the archive's rows REPLACE it — that watermark is the
        # single most important carried row.
        for t in tables:
            if t not in TAPE_TABLES:
                n_pre = int(dst.execute(f'SELECT COUNT(*) FROM main."{t}"').fetchone()[0])  # noqa: S608
                if n_pre:
                    if t != "store_meta":
                        raise RuntimeError(
                            f"fresh store table {t} not empty after Store.open ({n_pre} rows)"
                        )
                    dst.execute('DELETE FROM main."store_meta"')
                cols = col_lists[t]
                cur = dst.execute(
                    f'INSERT INTO main."{t}" ({cols}) SELECT {cols} FROM src."{t}"'  # noqa: S608
                )
                copied[t] = cur.rowcount
        dec = seed.get("decisions")
        n = 0
        if dec and dec.get("first_id") is not None:
            cols = col_lists["decisions"]
            for kind in dec["kinds"]:
                cur = dst.execute(
                    f"INSERT INTO main.decisions ({cols}) SELECT {cols} FROM src.decisions"  # noqa: S608
                    " WHERE kind = ? AND id >= ?",
                    (kind, int(dec["first_id"])),
                )
                n += cur.rowcount
        copied["decisions"] = n
        rfq = seed.get("rfqs")
        n = 0
        rcols = col_lists.get("rfqs", "")
        if rfq and rfq.get("first_id") is not None:
            cur = dst.execute(
                f"INSERT INTO main.rfqs ({rcols}) SELECT {rcols} FROM src.rfqs WHERE id >= ?",  # noqa: S608
                (int(rfq["first_id"]),),
            )
            n += cur.rowcount
        first_rfq = None if not rfq else rfq.get("first_id")
        for i in range(0, len(prov_ids), 500):
            batch = [
                x for x in prov_ids[i : i + 500] if first_rfq is None or x < int(first_rfq)
            ]
            if not batch:
                continue
            marks = ",".join("?" * len(batch))
            cur = dst.execute(
                f"INSERT INTO main.rfqs ({rcols}) SELECT {rcols} FROM src.rfqs"  # noqa: S608
                f" WHERE id IN ({marks})",
                batch,
            )
            n += cur.rowcount
        copied["rfqs"] = n
        for t in TAPE_TABLES:
            if t in tables and t not in copied:
                copied[t] = 0
        # Sequences: the fresh ids continue above the archive's for EVERY
        # AUTOINCREMENT table, carried or not.
        for name, seq in plan_["sqlite_sequence"]:
            dst.execute("DELETE FROM sqlite_sequence WHERE name = ?", (name,))
            dst.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (name, int(seq)))
        dst.execute("COMMIT")
        dst.execute("DETACH DATABASE src")
        qc = dst.execute("PRAGMA quick_check").fetchone()[0]
        if qc != "ok":
            raise RuntimeError(f"fresh store quick_check: {qc}")
        # Verify every copied table's count against the copy's own rowcount.
        mismatches: list[str] = []
        for t, n_expected in copied.items():
            got = int(dst.execute(f'SELECT COUNT(*) FROM main."{t}"').fetchone()[0])  # noqa: S608
            if got != n_expected:
                mismatches.append(f"{t}: copied {n_expected} but counted {got}")
        for t, facts in plan_["tables"].items():
            if not facts["tape"] and copied.get(t) != facts["count"]:
                mismatches.append(
                    f"{t}: archive count {facts['count']} vs copied {copied.get(t)}"
                )
        if mismatches:
            raise RuntimeError("row-count verify failed: " + "; ".join(mismatches))
        dst.execute("PRAGMA synchronous=NORMAL")
        mode = dst.execute("PRAGMA journal_mode").fetchone()[0]
        dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        dst.close()
    return {
        "copied": copied,
        "schema_source": "Store.open (live DDL + migrations + indexes)",
        "archive_only_tables": archive_only_tables,
        "archive_only_indexes": archive_only_indexes,
        "columns_defaulted": defaulted,
        "journal_mode": str(mode),
        "build_s": round(time.monotonic() - t0, 2),
        "fresh_bytes": fresh.stat().st_size,
    }


def rotate(
    store: Path,
    *,
    now: datetime | None = None,
    window_s: int | None = None,
    archive_suffix: str | None = None,
    manifest_dir: Path | None = None,
    liveness_window_s_: float | None = None,
    config_path: Path | None = None,
    skip_process_probe: bool = False,
    measure: bool = False,
) -> JsonDict:
    """--apply. Refuse-first, rename-second, build-third, swap-last; any failure
    after the rename puts the archive back under the live name."""
    now = now or datetime.now(UTC)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    suffix = archive_suffix or f"archive-{now.strftime('%Y%m%d')}"
    archive = store.with_name(f"{store.name}.{suffix}")
    fresh_tmp = store.with_name(f"{store.name}.rotating-{stamp}")
    manifest: JsonDict = {
        "stamp": stamp,
        "store": str(store),
        "archive": str(archive),
        "fresh_tmp": str(fresh_tmp),
        "steps": [],
    }
    if not store.exists():
        raise SystemExit(f"REFUSED: no store at {store}")
    if archive.exists():
        raise SystemExit(f"REFUSED: archive name already exists: {archive}")
    # (1) liveness — before any connection.
    _src_on_path()
    from tools.ops.repair_phantom_fills import (
        bot_liveness_evidence,
        default_config_path,
        liveness_window_s,
    )

    window = (
        liveness_window_s_
        if liveness_window_s_ is not None
        else liveness_window_s(config_path or default_config_path())
    )
    evidence = list(bot_liveness_evidence(str(store), window_s=window))
    if not skip_process_probe:
        evidence.extend(process_evidence())
    manifest["liveness_window_s"] = window
    if evidence:
        raise SystemExit(
            "REFUSED: the bot may be ALIVE on this store's data_dir — "
            + "; ".join(evidence)
            + " — run STOP_BOT.bat (kills the watchdog first) before --apply"
        )
    manifest["steps"].append("liveness: no evidence of a live bot")
    # (2) plan (read-only) + foreign-WAL refusal.
    p = plan(store, now=now, window_s=window_s, measure=measure)
    foreign = foreign_wal_refusals(p["files"])
    if foreign:
        raise SystemExit("REFUSED: " + " | ".join(foreign))
    manifest["plan"] = {k: v for k, v in p.items() if k not in ("readers", "objects")}
    manifest["steps"].append("plan: computed read-only")
    # (3) checkpoint TRUNCATE — refuse unless complete.
    ck = checkpoint_truncate(store, timeout_ms=busy_timeout_ms())
    manifest["checkpoint"] = ck
    if ck["busy"] or ck["wal_bytes_after"] > 0 or (
        ck["wal_frames"] is not None and ck["checkpointed"] != ck["wal_frames"]
    ):
        raise SystemExit(
            f"REFUSED: WAL could not be fully checkpointed ({ck}) — a connection is "
            "still open on the store"
        )
    manifest["steps"].append("checkpoint TRUNCATE: complete, WAL at 0 bytes")
    # (4) rename — atomic; fails while any process holds the file.
    os.rename(store, archive)
    moved = _move_siblings(store, archive)
    manifest["steps"].append(f"renamed store -> {archive.name}; siblings {moved}")
    try:
        # (5) build + verify the fresh store at a temp name.
        _remove_with_siblings(fresh_tmp)
        built = build_fresh_store(archive, fresh_tmp, plan_=p)
        manifest["built"] = built
        manifest["steps"].append(f"fresh store built + verified in {built['build_s']} s")
        # (6) swap in.
        os.replace(fresh_tmp, store)
        _move_siblings(fresh_tmp, store)
        manifest["steps"].append("fresh store swapped onto the live name")
    except BaseException as exc:
        _remove_with_siblings(fresh_tmp)
        rolled: list[str] = []
        if archive.exists() and not store.exists():
            os.rename(archive, store)
            rolled = _move_siblings(archive, store)
            manifest["steps"].append(f"ROLLED BACK: archive renamed to live name; {rolled}")
        manifest["error"] = repr(exc)
        _write_manifest(manifest, manifest_dir or store.parent / "backups", stamp, failed=True)
        raise
    manifest["ok"] = True
    manifest_path = _manifest_path(manifest_dir or store.parent / "backups", stamp)
    manifest["manifest_path"] = str(manifest_path)
    manifest["next_steps"] = next_steps(store, archive, manifest_path)
    _write_manifest(manifest, manifest_dir or store.parent / "backups", stamp)
    return manifest


def next_steps(store: Path, archive: Path, manifest_path: Path) -> list[str]:
    """The exact operator sequence after a successful --apply."""
    return [
        f"1. {REPO / 'START_BOT.bat'}  (repo root; supervisor + monitors + prober; "
        "refuses if any combomaker process is already running)",
        "2. first window: sends/min in the 300-460 band; store_writer_stats queue near 0 / "
        "dropped_writes_delta 0; acceptance_tape_seed_result rows_scanned; no "
        "retained_floor_sweep_timeout",
        f"3. PYTHONPATH=src python tools/ops/rotate_store.py --verify --manifest {manifest_path}"
        f" --store {store}   (read-only post-check)",
        f"4. history lives in {archive} — point analysis tools at it explicitly "
        "(--store/--db); nothing in src/ or tools/ opens it by default",
    ]


def _manifest_path(directory: Path, stamp: str, *, failed: bool = False) -> Path:
    return directory / f"{stamp}-rotate_store_manifest{'-FAILED' if failed else ''}.json"


def _write_manifest(
    manifest: JsonDict, directory: Path, stamp: str, *, failed: bool = False
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = _manifest_path(directory, stamp, failed=failed)
    path.write_text(json.dumps(manifest, indent=1, default=str), encoding="utf-8")
    return path


# ----------------------------------------------------------------- verify


def verify(store: Path, manifest_path: Path) -> JsonDict:
    """--verify (read-only): the live store vs the manifest — carried LIVE
    tables never shrank, the fills watermark is the archive's, sequences
    continue above the archive's, the archive still holds its rows."""
    m = json.loads(manifest_path.read_text(encoding="utf-8"))
    copied: dict[str, int] = m["built"]["copied"]
    seqs = {name: int(seq) for name, seq in m["plan"]["sqlite_sequence"]}
    problems: list[str] = []
    out: JsonDict = {"store": str(store), "manifest": str(manifest_path), "tables": {}}
    con = connect_ro(store)
    try:
        for t, n in copied.items():
            got = int(con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])  # noqa: S608
            out["tables"][t] = {"carried": n, "now": got}
            if got < n:
                problems.append(f"{t}: {got} rows now < {n} carried")
        meta = dict(con.execute("SELECT key, value FROM store_meta").fetchall())
        out["store_meta"] = meta
        now_seqs = dict(con.execute("SELECT name, seq FROM sqlite_sequence").fetchall())
        out["sqlite_sequence"] = now_seqs
        for name, seq in seqs.items():
            if int(now_seqs.get(name, -1)) < seq:
                problems.append(f"sqlite_sequence[{name}] {now_seqs.get(name)} < carried {seq}")
    finally:
        con.close()
    archive = Path(m["archive"])
    out["archive_exists"] = archive.exists()
    if archive.exists():
        acon = connect_ro(archive)
        try:
            for t, facts in m["plan"]["tables"].items():
                if not facts["tape"]:
                    got = int(acon.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])  # noqa: S608
                    if got != facts["count"]:
                        problems.append(f"archive {t}: {got} != {facts['count']} at rotation")
            ameta = dict(acon.execute("SELECT key, value FROM store_meta").fetchall())
            if ameta != meta:
                problems.append(f"store_meta differs: live {meta} vs archive {ameta}")
        finally:
            acon.close()
    else:
        problems.append("archive missing")
    out["problems"] = problems
    out["ok"] = not problems
    return out


# ------------------------------------------------------------------- main


def _fmt_bytes(n: int | None) -> str:
    if n is None:
        return "-"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} PiB"


def print_plan(p: JsonDict) -> None:
    f = p["files"]
    print(f"== STORE {p['store']}")
    print(
        f"   {_fmt_bytes(f['store_bytes'])}  pages {p['page_count']:,} x {p['page_size']}  "
        f"freelist {p['freelist_count']:,}  journal {p['journal_mode']}"
        f"  hard links {f['hard_links']}"
    )
    w = f.get("wal")
    wal_txt = "absent" if not w else f"{_fmt_bytes(w['bytes'])} / {w.get('frames')} frames"
    print(f"   WAL: {wal_txt}   SHM: {_fmt_bytes(f.get('shm_bytes'))}")
    for o in f.get("other_names", []):
        ow = o.get("wal")
        ow_txt = "none" if not ow else f"{ow['bytes']} B / {ow.get('frames')} frames"
        print(f"   OTHER NAME {o['name']}  wal={ow_txt}  shm={o.get('shm_bytes')}")
    if f.get("hard_link_note"):
        print(f"   note: {f['hard_link_note']}")
    print("\n== TABLES (LIVE = carried whole; TAPE = seed window only)")
    for t, v in p["tables"].items():
        if v["tape"]:
            print(
                f"   TAPE {t:22s} ids [{v.get('min_id')}, {v.get('max_id')}]"
                f"  ~{v.get('rows_estimate', 0):,} rows"
                f"  {v.get('first_at', '')} .. {v.get('last_at', '')}"
            )
        else:
            print(f"   LIVE {t:22s} {v['count']:,} rows")
    print(f"   sqlite_sequence: {p['sqlite_sequence']}")
    s = p["seed"]
    print(f"\n== SEED WINDOW {s['window_s']} s  (cutoff {s['cutoff_iso']})")
    for t, v in s["tables"].items():
        print(f"   {t}: {json.dumps(v)}")
    lp = p["leg_provenance"]
    print(
        f"\n== LEG PROVENANCE: {len(lp['tickers'])} fills tickers without a ledger row -> "
        f"{len(lp['rfq_ids'])} rfqs rows carried; conflicting {len(lp.get('conflicting', []))}; "
        f"unresolvable {len(lp.get('unresolvable', []))}"
    )
    if p.get("measured"):
        print(f"\n== MEASURED (this box, now): {json.dumps(p['measured'])}")
    print(f"\n== ESTIMATE: {json.dumps(p['estimate'])}")
    print("\n== BOOT-TIME READERS OF STORE HISTORY")
    for r in p["readers"]:
        print(f"   - {r['reader']}\n       tables: {r['tables']}\n       carried: {r['carried']}")
    r = p.get("retention")
    if r:
        print(
            "\n== RETENTION ALTERNATIVE (dark; observe.tape_retention_enabled=false):"
            f" window {r['retention_window_s']:.0f} s = reader {r['reader_window_s']} s"
            f" + cadence {r['prune_cadence_s']} s + measured disorder {r['disorder_s']:.3f} s"
            f"  (cutoff {r['cutoff_iso']}; batch {r['batch_ids']:,} ids,"
            f" <= {r['max_batches_per_pass']:,} batches/pass,"
            f" batch bound {r['batch_time_bound_s']} s)"
        )
        for t, v in r["tables"].items():
            dis = r["disorder"].get(t, {}).get("disorder_s", 0.0)
            print(
                f"   {t:22s} prune_below_id {v.get('prune_below_id')}"
                f"  ~{v.get('rows_estimate', 0):,} rows older than the window"
                f"  (disorder {dis:.3f} s)"
            )
        pr = r["protected"]
        print(f"   protected rfqs rows (leg provenance): {len(pr['rfq_ids'])}")
    print("\n== REFUSALS THAT WOULD FIRE NOW")
    for r in p["refusals_now"] or ["(none from the files; liveness is checked by --apply)"]:
        print(f"   ! {r}")
    print(f"\nplan computed in {p['plan_elapsed_s']} s")


def main(argv: list[str] | None = None) -> int:
    # A Windows console without PYTHONIOENCODING=utf-8 is cp1252: never let an
    # unencodable character in a docstring or a table name crash the report.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--store", default=DEFAULT_STORE)
    ap.add_argument(
        "--seed-window-s",
        type=int,
        default=None,
        help="override the boot seed window (default: acceptance_seed.SEED_WINDOW_S)",
    )
    ap.add_argument("--config", help="bot launch YAML the liveness window derives from")
    ap.add_argument("--archive-suffix", help="default archive-YYYYMMDD")
    ap.add_argument("--manifest", help="--verify: the manifest written by --apply")
    ap.add_argument(
        "--no-measure", action="store_true", help="--dry-run: skip the throughput measurement"
    )
    ap.add_argument("--out", help="write the plan / manifest / verify JSON here")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = ap.parse_args(argv)
    store = Path(args.store)

    if args.verify:
        if not args.manifest:
            ap.error("--verify needs --manifest")
        result = verify(store, Path(args.manifest))
        print(json.dumps(result, indent=1, default=str))
    elif args.apply:
        result = rotate(
            store,
            window_s=args.seed_window_s,
            archive_suffix=args.archive_suffix,
            config_path=Path(args.config) if args.config else None,
        )
        shown = {k: v for k, v in result.items() if k not in ("plan", "next_steps")}
        print(json.dumps(shown, indent=1, default=str))
        print("\n== NEXT STEPS")
        for step in result.get("next_steps", []):
            print(f"   {step}")
    else:
        result = plan(store, window_s=args.seed_window_s, measure=not args.no_measure)
        print_plan(result)
        liveness = liveness_refusals(
            store, window_s=None, config_path=Path(args.config) if args.config else None
        )
        result["liveness_refusals_now"] = liveness
        for item in liveness:
            print(f"   ! --apply would REFUSE now: {item}")
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
        print("\nwrote", args.out)
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
