"""A READ-ONLY SNAPSHOT of what the gate reads from the production store, so
rule 9 can run on a LIVE machine without touching the live drive.

    .venv/Scripts/python.exe -m tools.vitals.snapshot \\
        --store D:/kalshi-combos-TWO-data/combomaker-prod-live-wc.sqlite3 --out <dir>
    VITALS_DATA_DIR=<dir> .venv/Scripts/python.exe -m tools.vitals.gate
    VITALS_DATA_DIR=<dir> .venv/Scripts/python.exe -m tools.vitals.gate --tier pre-ship

WHY (2026-09-04, build "floor-point-estimate" finding O5 / review M1).
``derive.tape_facts`` keys its cache on a manifest of EVERY ``data/live_*.log``
INCLUDING the one the bot is writing, so on a live box the cache never matches
and every gate run rescans the whole tape (274 files / 266 GB that night —
the docstring's "10 GB" is history) from the drive the live store writes to;
the 19:29 ET boot was stall-killed inside such a scan's window. ``derive``
also globs ``data/combomaker*.sqlite3`` — the 213 GB live store — for two
SMALL tables. This tool copies exactly those tables (schema, rows, indexes;
read-only URI, one bounded ``SELECT *`` each) into a directory that holds NO
``live_*.log``, so ``derive`` takes the committed ``tape_facts.json`` cache
(provenance "cache (no tape on this machine)") and reads the bankroll and the
open book from the copy. Nothing under the data dir is written; the snapshot
file keeps the store's NAME so every provenance string the gate prints reads
the same as on the live store.

Scope: the fast and pre-ship tiers (``tools.vitals.gate``). The extended tier
(``gate_ext``: V10/V11) additionally reads bounded tails of ``rfqs`` and
``decisions``; those are not copied here.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

# Keep in sync with tools/vitals/derive.py: ``risk_bankroll_cc`` reads
# ``daily_ruin_anchors``; ``live_open_positions`` reads ``position_ledger``.
TABLES: tuple[str, ...] = ("daily_ruin_anchors", "position_ledger")

_FETCH = 2_000  # rows per fetchmany — a chunk size, not a threshold


def _copy_table(src: sqlite3.Connection, dst: sqlite3.Connection, table: str) -> int:
    row = src.execute(
        "select sql from sqlite_master where type = 'table' and name = ?", (table,)
    ).fetchone()
    if row is None or not row[0]:
        raise SystemExit(f"table {table!r} is not in the source store")
    dst.execute(row[0])
    n = 0
    cur = src.execute(f'select * from "{table}"')  # noqa: S608 — name from TABLES
    width = len(cur.description)
    marks = ",".join("?" * width)
    while True:
        chunk = cur.fetchmany(_FETCH)
        if not chunk:
            break
        dst.executemany(f'insert into "{table}" values ({marks})', chunk)  # noqa: S608
        n += len(chunk)
    for (index_sql,) in src.execute(
        "select sql from sqlite_master where type = 'index' and tbl_name = ? and sql is not null",
        (table,),
    ):
        dst.execute(index_sql)
    return n


def snapshot(stores: list[Path], out: Path) -> dict[str, object]:
    out.mkdir(parents=True, exist_ok=True)
    stray = sorted(p.name for p in out.glob("live_*.log"))
    if stray:
        raise SystemExit(
            f"{out} holds live_*.log files ({stray[:3]}...): the gate would scan "
            "them — point --out at a directory without tape"
        )
    taken: dict[str, object] = {
        "taken_at": datetime.now(UTC).isoformat(),
        "tables": list(TABLES),
        "stores": {},
    }
    for store in stores:
        target = out / store.name
        if target.exists():
            target.unlink()
        src = sqlite3.connect(f"file:{store.as_posix()}?mode=ro", uri=True, timeout=5)
        dst = sqlite3.connect(target.as_posix())
        counts: dict[str, int] = {}
        try:
            for table in TABLES:
                counts[table] = _copy_table(src, dst, table)
            dst.commit()
        finally:
            dst.close()
            src.close()
        taken["stores"][store.name] = {  # type: ignore[index]
            "source": store.as_posix(),
            "rows": counts,
            "bytes": target.stat().st_size,
        }
    (out / "snapshot_provenance.json").write_text(
        json.dumps(taken, indent=2, sort_keys=True), encoding="utf-8"
    )
    return taken


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vitals-snapshot", description=__doc__)
    ap.add_argument(
        "--store",
        action="append",
        required=True,
        help="a production store to snapshot (repeatable); READ-ONLY",
    )
    ap.add_argument("--out", required=True, help="snapshot directory (no live_*.log)")
    args = ap.parse_args(argv)
    stores = [Path(s) for s in args.store]
    for s in stores:
        if not s.exists():
            raise SystemExit(f"no such store: {s}")
    out = Path(args.out)
    taken = snapshot(stores, out)
    print(json.dumps(taken, indent=2, sort_keys=True))
    # What the gate will derive from the copy — through derive's own readers,
    # pointed at the snapshot (the env var is read at derive's import).
    os.environ["VITALS_DATA_DIR"] = str(out)
    from tools.vitals.derive import live_open_positions, risk_bankroll_cc

    bankroll, source = risk_bankroll_cc()
    rows, book_source = live_open_positions()
    print(f"bankroll : {bankroll / 10_000:,.2f} USD   [{source}]")
    print(f"open book: {len(rows)} positions   [{book_source}]")
    print(f"tape     : none in {out} — the gate uses tools/vitals/tape_facts.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
