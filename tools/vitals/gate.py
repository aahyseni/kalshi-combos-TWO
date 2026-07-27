"""THE VITAL-SIGNS GATE.

    .venv/Scripts/python.exe -m tools.vitals.gate

One command. Independent checks. A readable table of MEASURED numbers against
DERIVED bounds, and for every red line, what it costs in MONEY.

WHY IT EXISTS. Between 2026-07-25 and 2026-07-27, eight changes produced SEVEN
regressions. The unit suite was 3,081 GREEN throughout, and reintroducing all
seven trips 17 test instances — every one of which was written by the commit
that fixed that same regression. Zero pre-existing detectors. Two of the seven
are invisible to the suite even today. The suite asserts the mechanism the
author was thinking about; it never varies the DISCRIMINATING VARIABLE:

    R1  the book is ALREADY over the wall        R5  WHICH PATH the benchmark ran on
    R2  the fan-out N (it fired one at a time)   R6  REAL OS processes (fakes all passed)
    R3  elapsed WALL TIME under a persistent 429 R7  the UNION of axes, not the survivor
    R4  the INTERLEAVING POINT (inside the await)

So every check below constructs a DEGRADED state — a book over a wall, an empty
token bucket, an exchange 429ing forever, a realistic fan-out wave, a book at
1x/3x/5x, an accept landing inside an await — and every threshold is measured,
observed, or a protocol fact. There are no tuned constants in this package.

ISOLATION. Nothing here edits a live module, opens a socket, needs a running
bot, or writes to the store. The store is read READ-ONLY, for the bankroll.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

# HERMETIC, the same guarantee tests/conftest.py gives the unit suite: strip
# every credential and disable .env loading, so the gate is never one config
# change away from the network.
for _var in (
    "KALSHI_API_KEY_ID",
    "KALSHI_PRIVATE_KEY_PATH",
    "KALSHI_PRIVATE_KEY_PEM",
    "KALSHI_REQUESTER_API_KEY_ID",
    "KALSHI_REQUESTER_PRIVATE_KEY_PATH",
    "KALSHI_REQUESTER_PRIVATE_KEY_PEM",
    "KALSHI_SUPERVISOR_API_KEY_ID",
    "KALSHI_SUPERVISOR_PRIVATE_KEY_PATH",
    "KALSHI_SUPERVISOR_PRIVATE_KEY_PEM",
    "SPORTSGAMEODDS_API_KEY",
):
    os.environ.pop(_var, None)
os.environ["COMBOMAKER_NO_DOTENV"] = "1"

# The checks deliberately drive the bot through 429 storms, quarantine waves and
# timeouts, so the real modules emit hundreds of real log lines. The GATE'S
# OUTPUT IS THE PRODUCT — a table you can read — so the bot's logger is muted
# here (and only here). Use --verbose to watch the machinery instead.
import logging as _logging  # noqa: E402

import structlog as _structlog  # noqa: E402


def _mute_bot_logging() -> None:
    _structlog.configure(
        wrapper_class=_structlog.make_filtering_bound_logger(_logging.CRITICAL),
        logger_factory=_structlog.ReturnLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    _logging.disable(_logging.CRITICAL)


from tools.vitals import v_confirm, v_liveness, v_quoting, v_supervision  # noqa: E402
from tools.vitals.derive import derive  # noqa: E402
from tools.vitals.result import FAST, PRESHIP, Result, run_check  # noqa: E402

ALL_CHECKS = [
    *v_quoting.CHECKS,
    *v_liveness.CHECKS,
    *v_confirm.CHECKS,
    *v_supervision.CHECKS,
]


def _rule(width: int = 118) -> str:
    return "-" * width


def _wrap(text: str, width: int, indent: str) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for word in words:
        if len(cur) + len(word) + 1 > width and cur:
            lines.append(indent + cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        lines.append(indent + cur)
    return lines


def render(results: list[Result], derived, elapsed: float, tier: str) -> str:
    out: list[str] = []
    out.append("")
    out.append("=" * 118)
    out.append("  VITAL SIGNS GATE — kalshi-combos-TWO      tier: " + tier)
    out.append("=" * 118)
    out.append("  DERIVED INPUTS (measured / observed / protocol — no tuned constants)")
    for line in derived.lines():
        out.append("    " + line)
    out.append(_rule())
    header = f"  {'KEY':<4} {'VITAL':<52} {'MEASURED':<34} {'VERDICT':<7} {'SEC':>6}"
    out.append(header)
    out.append(_rule())
    for r in results:
        mark = {"PASS": "PASS", "FAIL": "FAIL", "ERROR": "ERR "}[r.verdict]
        out.append(
            f"  {r.vital.key:<4} {r.vital.name[:52]:<52} {r.measured[:34]:<34} "
            f"{mark:<7} {r.seconds:6.2f}"
        )
        out.extend(_wrap(f"bound: {r.bound}", 100, " " * 11))
        out.extend(_wrap(f"degraded state: {r.vital.degraded}", 100, " " * 11))
        for note in r.notes:
            out.extend(_wrap(note, 100, " " * 13))
        out.append("")
    out.append(_rule())

    failures = [r for r in results if not r.passed]
    if failures:
        out.append("  RED — what this costs in MONEY:")
        for r in failures:
            out.append("")
            out.append(f"  [{r.vital.key}] {r.vital.name}")
            out.extend(_wrap(f"INCIDENT : {r.vital.incident}", 100, " " * 13))
            out.extend(_wrap(f"MONEY    : {r.vital.money}", 100, " " * 13))
            out.extend(_wrap(f"MEASURED : {r.measured}", 100, " " * 13))
            out.extend(_wrap(f"BOUND    : {r.bound}", 100, " " * 13))
            if r.detail:
                out.extend(_wrap(f"DETAIL   : {r.detail}", 100, " " * 13))
            if r.error:
                out.extend(_wrap(f"ERROR    : {r.error}", 100, " " * 13))
        out.append("")
        out.append(_rule())
    passed = sum(1 for r in results if r.passed)
    consequence = "DO NOT SHIP" if tier == PRESHIP else "DO NOT COMMIT"
    out.append(
        f"  {passed}/{len(results)} vital signs GREEN   "
        f"({'GATE PASS' if passed == len(results) else f'GATE FAIL — {consequence}'})"
        f"   total {elapsed:.1f}s"
    )
    out.append("=" * 118)
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vitals-gate", description=__doc__)
    ap.add_argument(
        "--tier",
        choices=(FAST, PRESHIP, "all"),
        default=FAST,
        help="fast = every commit; pre-ship = the slow measured ones; all = both",
    )
    ap.add_argument("--only", default="", help="comma-separated keys, e.g. V1,V5")
    ap.add_argument(
        "--verbose", action="store_true", help="leave the bot's own logging on"
    )
    ap.add_argument(
        "--refresh-tape",
        action="store_true",
        help="rescan data/live_*.log (~60s on the 10 GB tape) instead of the cache",
    )
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass
    if not args.verbose:
        _mute_bot_logging()

    derived = derive(refresh_tape=args.refresh_tape)
    only = {k.strip().upper() for k in args.only.split(",") if k.strip()}
    selected = [
        (v, fn)
        for v, fn in ALL_CHECKS
        if (args.tier == "all" or v.tier == args.tier)
        and (not only or v.key in only)
    ]
    if only:
        selected = [(v, fn) for v, fn in ALL_CHECKS if v.key in only]

    t0 = time.perf_counter()
    results = [run_check(v, fn, derived) for v, fn in selected]
    elapsed = time.perf_counter() - t0
    print(render(results, derived, elapsed, args.tier if not only else "selected"))
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
