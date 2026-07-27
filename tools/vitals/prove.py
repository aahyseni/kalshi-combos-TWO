"""PROOF BY CONSTRUCTION — a gate that cannot catch the bugs it was built for is
worthless, so this reintroduces the historical defects and shows the gate goes RED.

    .venv/Scripts/python.exe -m tools.vitals.prove

Every mutation is applied to a SCRATCH COPY of the repo under the system temp
dir. The working tree is never touched, the live bot is never signalled, and the
scratch copy points at the real ``data/`` READ-ONLY via ``VITALS_DATA_DIR`` (so
the 10 GB tape is not duplicated). Each mutation names the incident it recreates,
the expected RED vital, and the exact edit.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Mutation:
    key: str
    incident: str
    expect_red: tuple[str, ...]
    rel_path: str
    old: str
    new: str
    note: str


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        key="R1",
        incident="ENTITY CAP scanned every key in the book, not the candidate's "
        "(live 2026-07-26: 3,994 breaches, ZERO quotes sent)",
        expect_red=("V1", "V2"),
        rel_path="src/combomaker/risk/limits.py",
        old="""            candidate_keys: set[str] = set()
            for position in candidates:
                for leg in position.legs:
                    candidate_keys.add(leg_entity_key(leg))""",
        new="""            candidate_keys: set[str] = set(snapshot.loss_by_entity_cc)""",
        note="the first cut's book-wide scan, restored verbatim",
    ),
    Mutation(
        key="R3",
        incident="B3 RETRY DRIVER was unmetered — 400 write tok/s against a 300 "
        "tok/s ceiling, so a transient 429 sustained itself forever",
        expect_red=("V4",),
        rel_path="src/combomaker/rfq/lifecycle.py",
        old="""        budget: WriteBudget,
        cost: int,
        remaining_s: Callable[[], float | None],
    ) -> bool:""",
        new="""        budget: WriteBudget,
        cost: int,
        remaining_s: Callable[[], float | None],
    ) -> bool:
        return True  # MUTATION R3: no gate, no budget, no backoff""",
        note="the token gate becomes a no-op; every retry sends",
    ),
    Mutation(
        key="R4",
        incident="ACCEPTED-QUOTE GUARD was a SNAPSHOT taken before the resolver's "
        "awaits (live: proven_gone=2 on a quote that then logged confirm_ok)",
        expect_red=("V5",),
        rel_path="src/combomaker/rfq/lifecycle.py",
        old="""                live = self._open.get(quote_id)
                if live is None or live.accepted:""",
        new="""                live = self._open.get(quote_id)
                if live is None:  # MUTATION R4: the post-await re-check removed""",
        note="drops the re-check, leaving only the pre-await snapshot",
    ),
    Mutation(
        key="R5",
        incident="a candidate-gate TIMEOUT recorded as DECLINE_CANDIDATE_RISK — a "
        "stopwatch wearing a risk reason code (live: 70 deadline events filed "
        "as risk declines, invisible to every decline analysis)",
        expect_red=("V7",),
        rel_path="src/combomaker/rfq/lifecycle.py",
        old="    candidate_gate_timeout_fallback: bool = True",
        new="    candidate_gate_timeout_fallback: bool = False  # MUTATION R5",
        note="restores the pre-2026-07-27 behaviour: expiry resolves to "
        "DECLINE_CANDIDATE_RISK",
    ),
    Mutation(
        key="R6",
        incident="AUTO-RELIGHTER ran the boot bound from LAUNCH against the 30 s "
        "wedge anchor, while every recorded boot takes 33.6-48.3 s — it "
        "abandoned a HEALTHY child and left it ORPHANED",
        expect_red=("V9",),
        rel_path="src/combomaker/ops/relight.py",
        old="                if beat_seen and self._heartbeat.is_wedged(self._wedge_timeout_s):",
        new="                if self._heartbeat.is_wedged(self._wedge_timeout_s):  # MUTATION R6",
        note="removes the beat_seen latch, so the bound runs from launch",
    ),
    Mutation(
        key="R7",
        incident="SUPERVISOR DEMOTION removed the liveness axis and left the "
        "STARTUP WINDOW uncovered — ProgressReader deliberately does not latch "
        "on an empty ledger, and that window spans the startup reconcile",
        expect_red=("V8",),
        rel_path="src/combomaker/ops/supervisor.py",
        old="""        if not self._progress.seen and self.heartbeat_wedged():""",
        new="""        if False:  # MUTATION R7: startup-window liveness fallback removed""",
        note="the union loses its only axis during startup",
    ),
    Mutation(
        key="R8",
        incident="B1: the candidate gate resolved rho for EVERY unordered ticker "
        "pair on the theory that 'resolving all is a harmless superset' — 96.0% "
        "of it discarded, 3,829 ms of cold rho on the live book before a single "
        "MC sample",
        expect_red=("V6",),
        rel_path="src/combomaker/sim/book_model.py",
        old="""    members_by_game = within_game_index_members(
        game_of_index, len(ticker_by_index)
    )""",
        new="""    members_by_game = {  # MUTATION R8: the "harmless superset", restored
        "ALL": list(range(len(ticker_by_index)))
    }""",
        note="the same-game grouping collapses to one block, so every unordered "
        "pair is resolved again",
    ),
    Mutation(
        key="R9",
        incident="B2: the PREDICTIVE guard could reach a state where the candidate "
        "MC never ran again (one poisoned ms/pair sample skipped the build, a "
        "skipped build produced no new sample, the max never decayed) — the "
        "joint-tail / P(ruin) / delta-P(book) gate DARK, permanently",
        expect_red=("V7",),
        rel_path="src/combomaker/rfq/lifecycle.py",
        old="""            mc0_ns = self._clock.monotonic_ns()
            try:
                result = await self._run_candidate_mc(""",
        new="""            mc0_ns = self._clock.monotonic_ns()
            if True:  # MUTATION R9: the predictor, permanently poisoned
                return await self._candidate_gate_fallback(
                    quote_id,
                    state,
                    reservation_id=reservation_id,
                    cause="predicted_over_budget",
                    detail="candidate gate: MC refinement did not fit",
                )
            try:
                result = await self._run_candidate_mc(""",
        note="the guard's terminal state: the MC is never started, so a candidate "
        "the risk model REFUSES is confirmed by the latency fallback",
    ),
)

_COPY = ("src", "tests", "tools", "config", "pyproject.toml")


def _make_scratch(root: Path, name: str) -> Path:
    dst = root / name
    dst.mkdir(parents=True, exist_ok=True)
    for item in _COPY:
        src = REPO / item
        if not src.exists():
            continue
        if src.is_dir():
            shutil.copytree(
                src,
                dst / item,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "backtests"),
            )
        else:
            shutil.copy2(src, dst / item)
    return dst


def _run_gate(cwd: Path, keys: tuple[str, ...]) -> tuple[int, str]:
    env = dict(os.environ)
    env["VITALS_DATA_DIR"] = str(REPO / "data")
    env["PYTHONPATH"] = str(cwd)
    proc = subprocess.run(
        [
            str(REPO / ".venv" / "Scripts" / "python.exe"),
            "-m",
            "tools.vitals.gate",
            "--only",
            ",".join(keys),
        ],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout + proc.stderr


def _verdicts(output: str, keys: tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in output.splitlines():
        stripped = line.strip()
        for key in keys:
            if stripped.startswith(key + " "):
                if " PASS " in line:
                    out[key] = "PASS"
                elif " FAIL " in line:
                    out[key] = "FAIL"
                elif " ERR" in line:
                    out[key] = "ERROR"
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vitals-prove")
    ap.add_argument("--only", default="", help="comma-separated mutation keys")
    ap.add_argument("--keep", action="store_true", help="keep the scratch trees")
    args = ap.parse_args(argv)
    wanted = {k.strip().upper() for k in args.only.split(",") if k.strip()}
    mutations = [m for m in MUTATIONS if not wanted or m.key in wanted]

    root = Path(tempfile.mkdtemp(prefix="vitals-prove-"))
    print(f"scratch root: {root}")
    rows: list[tuple[str, str, str, str, bool]] = []
    t_all = time.perf_counter()

    for mut in mutations:
        t0 = time.perf_counter()
        scratch = _make_scratch(root, f"mut-{mut.key}")
        target = scratch / mut.rel_path
        text = target.read_text(encoding="utf-8")
        if text.count(mut.old) != 1:
            rows.append(
                (
                    mut.key,
                    ",".join(mut.expect_red),
                    "-",
                    f"ANCHOR MISSING ({text.count(mut.old)} matches) in {mut.rel_path}",
                    False,
                )
            )
            continue
        target.write_text(text.replace(mut.old, mut.new), encoding="utf-8")

        code, out = _run_gate(scratch, mut.expect_red)
        got = _verdicts(out, mut.expect_red)
        caught = all(got.get(k) in ("FAIL", "ERROR") for k in mut.expect_red)
        rows.append(
            (
                mut.key,
                ",".join(mut.expect_red),
                ", ".join(f"{k}={got.get(k, '?')}" for k in mut.expect_red),
                f"{time.perf_counter() - t0:.1f}s",
                caught,
            )
        )
        print(f"  {mut.key:<3} mutated {mut.rel_path} -> gate exit {code}, {got}")
        if not caught:
            print("    ---- gate output (NOT CAUGHT) ----")
            print("\n".join(out.splitlines()[-40:]))

    # ...and the CLEAN working tree must be GREEN on the same vitals.
    clean_keys = tuple(sorted({k for m in mutations for k in m.expect_red}))
    t0 = time.perf_counter()
    code, out = _run_gate(REPO, clean_keys)
    clean = _verdicts(out, clean_keys)
    clean_green = all(v == "PASS" for v in clean.values()) and len(clean) == len(
        clean_keys
    )

    width = 104
    print()
    print("=" * width)
    print("  REGRESSION CORPUS — the gate vs the defects it was built for")
    print("=" * width)
    print(f"  {'MUT':<5} {'EXPECT RED':<12} {'GATE SAID':<34} {'SEC':<8} {'CAUGHT':<7}")
    print("-" * width)
    for key, expect, got, secs, caught in rows:
        print(f"  {key:<5} {expect:<12} {got[:34]:<34} {secs:<8} {'YES' if caught else 'NO':<7}")
    print("-" * width)
    print(
        f"  CLEAN working tree, same vitals: "
        f"{', '.join(f'{k}={v}' for k, v in sorted(clean.items()))}  "
        f"-> {'ALL GREEN' if clean_green else 'NOT GREEN'}  "
        f"({time.perf_counter() - t0:.1f}s)"
    )
    caught_n = sum(1 for r in rows if r[4])
    print(
        f"  {caught_n}/{len(rows)} historical defects caught; "
        f"total {time.perf_counter() - t_all:.1f}s"
    )
    print("=" * width)

    if not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    return 0 if (caught_n == len(rows) and clean_green) else 1


if __name__ == "__main__":
    sys.exit(main())
