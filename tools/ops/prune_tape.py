"""TAPE RETENTION PRUNE — the CLI face of ``combomaker.ops.tape_retention``
(2026-09-05 design item 7, the alternative to a rotation). READ-ONLY by default.

The in-process nightly step (``observe.tape_retention_enabled``, default
False) is the LIVE path: it prunes only against an idle tape writer, one
bounded batch at a time, off the event loop. This tool is for the operator:

  --dry-run (default)  the DERIVED retention window (longest boot-time reader
                       window + the pass cadence + the tape's measured time
                       disorder), per tape table the id below which rows are
                       older than it and how many, the protected leg-provenance
                       rows, and what one pass may do (batch size, batch cap,
                       batch time bound). Nothing is written.
  --apply              REFUSES while the bot may be alive (the SAME liveness
                       evidence as tools/ops/repair_phantom_fills.py +
                       rotate_store.py's process probe — a live bot prunes
                       through its own step, never two writers), then runs
                       bounded passes until complete (each pass bounded exactly
                       as the in-process one), then ONE PASSIVE checkpoint, and
                       writes a JSON result.

``DELETE`` returns pages to the freelist: the FILE does not shrink (that is
``rotate_store.py --apply``, once); it stops growing past the window and the
B-trees the writer inserts into stay small. Never VACUUM the live store
(a rotation-sized outage with none of the rotation's safety).

Usage (worktree root):

  PYTHONPATH=src python tools/ops/prune_tape.py --dry-run [--out plan.json]
  PYTHONPATH=src python tools/ops/prune_tape.py --apply            # bot DOWN
"""

from __future__ import annotations

import argparse
import json
import sqlite3
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
from combomaker.ops.tape_retention import (  # noqa: E402 — after sys.path
    PruneResult,
    plan_prune,
    run_prune_pass,
)
from tools.ops.rotate_store import (  # noqa: E402 — after sys.path
    DEFAULT_STORE,
    connect_ro,
    liveness_refusals,
)

JsonDict = dict[str, Any]


def dry_run(store: Path, *, now: datetime | None = None) -> JsonDict:
    now = now or datetime.now(UTC)
    con = connect_ro(store)
    try:
        return plan_prune(con, now=now)
    finally:
        con.close()


def apply(
    store: Path,
    *,
    now: datetime | None = None,
    liveness_window_s: float | None = None,
    config_path: Path | None = None,
    skip_process_probe: bool = False,
) -> JsonDict:
    """Refuse-first; then passes until complete; then a PASSIVE checkpoint."""
    now = now or datetime.now(UTC)
    if not store.exists():
        raise SystemExit(f"REFUSED: no store at {store}")
    evidence = liveness_refusals(
        store,
        window_s=liveness_window_s,
        config_path=config_path,
        include_processes=not skip_process_probe,
    )
    if evidence:
        raise SystemExit(
            "REFUSED: the bot may be ALIVE on this store's data_dir — "
            + "; ".join(evidence)
            + " — a live bot prunes through its own nightly step "
            "(observe.tape_retention_enabled); run STOP_BOT.bat before --apply"
        )
    t0 = time.monotonic()
    passes: list[PruneResult] = []
    while True:
        res = run_prune_pass(store, now=now)
        passes.append(res)
        if res.complete or res.rows_deleted == 0:
            break
    con = sqlite3.connect(str(store))
    try:
        ck = con.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    finally:
        con.close()
    return {
        "store": str(store),
        "started_at": now.isoformat(),
        "passes": [p.as_log_fields() for p in passes],
        "rows_deleted": sum(p.rows_deleted for p in passes),
        "complete": passes[-1].complete,
        "checkpoint_passive": list(ck) if ck else None,
        "elapsed_s": round(time.monotonic() - t0, 3),
    }


def print_plan(p: JsonDict) -> None:
    print(
        f"== RETENTION WINDOW {p['retention_window_s']:.0f} s = reader {p['reader_window_s']} s"
        f" ({', '.join(p['reader_windows_s'])}) + cadence {p['prune_cadence_s']} s"
        f" + measured disorder {p['disorder_s']:.3f} s"
    )
    print(f"   cutoff {p['cutoff_iso']}   (now {p['now']})")
    print(
        f"   one pass: batches of {p['batch_ids']:,} ids, <= {p['max_batches_per_pass']:,}"
        f" batches, a batch slower than {p['batch_time_bound_s']} s stops the pass"
    )
    print("\n== TAPE TABLES")
    for t, v in p["tables"].items():
        dis = p["disorder"].get(t, {})
        print(
            f"   {t:22s} ids [{v.get('min_id')}, {v.get('max_id')}]"
            f"  first_keep {v.get('first_keep_id')}  prune_below {v.get('prune_below_id')}"
            f"  ~{v.get('rows_estimate', 0):,} rows to prune"
            f"  disorder {dis.get('disorder_s', 0.0):.3f} s over {dis.get('rows', 0):,} rows"
        )
    pr = p["protected"]
    print(
        f"\n== PROTECTED (Store.held_positions tape fallback): {len(pr['tickers'])} fills"
        f" tickers without a ledger row -> {len(pr['rfq_ids'])} rfqs rows never pruned;"
        f" conflicting {len(pr['conflicting'])}; unresolvable {len(pr['unresolvable'])}"
    )


def main(argv: list[str] | None = None) -> int:
    # A Windows console without PYTHONIOENCODING=utf-8 is cp1252: never let an
    # unencodable character in a docstring or a table name crash the report.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--store", default=DEFAULT_STORE)
    ap.add_argument("--config", help="bot launch YAML the liveness window derives from")
    ap.add_argument("--out", help="write the plan / result JSON here")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    store = Path(args.store)
    if args.apply:
        result = apply(store, config_path=Path(args.config) if args.config else None)
        print(json.dumps(result, indent=1, default=str))
    else:
        result = dry_run(store)
        print_plan(result)
        for item in liveness_refusals(
            store, window_s=None, config_path=Path(args.config) if args.config else None
        ):
            print(f"   ! --apply would REFUSE now: {item}")
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
        print("\nwrote", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
