"""CONFIRM-PATH WALL TIME, before and after the same-game pair scoping (B1).

    .venv/Scripts/python.exe -m tools.diagnostics.bench_confirm_window

WHAT IT MEASURES, and why in this shape. The candidate gate has a hard 3.0 s
budget: the exchange's confirm window. The work inside it is three stages, and
the benchmark measures each SEPARATELY because they scale with different things
and only one of them was fixed here:

  A  RHO RESOLUTION — the O(pairs) provider walk in
     ``_build_candidate_gate_inputs``. Measured BOTH ways on the same book:
     ALL-PAIRS (the pre-2026-07-27 "harmless superset") and SAME-GAME (the set
     ``build_book_model`` actually consumes, taken from the SHARED
     ``within_game_pair_tickers`` — never a second implementation). COLD (first
     accept of a process) and MEMO-WARM (every accept after it).
  B  THE REST OF THE BUILD — the marginal dict + the committed-only model the
     P(ruin) equity basis needs. FIXED-ish cost, does not scale with pairs. This
     is the term the deleted predictor divided by the pair count and thereby
     inflated ~1000x on a small book.
  C  THE MC — ``_worker_candidate_book_risk`` with the DICT-backed providers,
     i.e. byte-identically what the worker process runs. NOT the live-provider
     inline call: those are different compositions and only one of them ships.

ISOLATION (hard rule 8): imports and drives the real modules, edits nothing,
opens no socket, needs no running bot. The book is the LIVE open book read
read-only through ``tools.vitals.derive``.
"""

from __future__ import annotations

import argparse
import inspect
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))
os.environ["COMBOMAKER_NO_DOTENV"] = "1"

import logging as _logging  # noqa: E402

import structlog as _structlog  # noqa: E402

_structlog.configure(
    wrapper_class=_structlog.make_filtering_bound_logger(_logging.CRITICAL),
    logger_factory=_structlog.ReturnLoggerFactory(),
    cache_logger_on_first_use=False,
)
_logging.disable(_logging.CRITICAL)

from combomaker.ops.pricing_pool import (  # noqa: E402
    CandidateBookRiskInputs,
    _worker_candidate_book_risk,
)
from combomaker.rfq.lifecycle import EXCHANGE_CONFIRM_WINDOW_S  # noqa: E402
from combomaker.sim.book_model import (  # noqa: E402
    build_book_model,
    within_game_pair_tickers,
)
from combomaker.sim.book_risk import evaluate_candidate_book_risk  # noqa: E402
from tools.vitals.derive import live_open_positions  # noqa: E402
from tools.vitals.v_confirm import _live_rho_provider, _scaled_live_book  # noqa: E402

WINDOW_MS = EXCHANGE_CONFIRM_WINDOW_S * 1000.0
_SIG = inspect.signature(evaluate_candidate_book_risk).parameters


def _default(name: str):
    """A gate setting read off the SHIPPED signature — never typed here."""
    return _SIG[name].default


def _time_pairs(rho, pairs, cap: int) -> tuple[float, int]:
    """(ms per pair, n sampled). Samples at most ``cap`` pairs — the rate is what
    extrapolates; timing 556k pairs to learn a per-pair rate is not free."""
    sample = pairs[:cap] if len(pairs) > cap else pairs
    if not sample:
        return 0.0, 0
    t0 = time.perf_counter()
    for a, b in sample:
        rho(a, b)
    return (time.perf_counter() - t0) * 1000.0 / len(sample), len(sample)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bench-confirm-window", description=__doc__)
    ap.add_argument(
        "--mults",
        default="0.1,0.35,1,3,5",
        help="book multipliers of the LIVE open book (0.1/0.35 = the 10/35 "
        "position sizes the confirm-window tests use)",
    )
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--pair-sample-cap", type=int, default=4000)
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass

    n_samples = int(
        inspect.signature(evaluate_candidate_book_risk).parameters["n_samples"].default
    )
    rows, source = live_open_positions()
    print(f"live open book: {len(rows)} positions  [{source}]")
    print(f"candidate MC samples (from the shipped signature): {n_samples:,}")
    print(f"exchange confirm window: {WINDOW_MS:.0f} ms\n")

    header = (
        f"{'book':>6} {'pos':>5} {'legs':>5} {'ALLpairs':>9} {'SGpairs':>8} "
        f"{'waste%':>7} {'A_all':>9} {'A_sg':>8} {'B':>7} {'C_mc':>8} "
        f"{'BEFORE':>9} {'AFTER':>9} {'margin':>9}"
    )
    print(header)
    print("-" * len(header))

    rho = _live_rho_provider()
    cold_lines: list[str] = []
    for raw in args.mults.split(","):
        mult = float(raw)
        whole = max(1, int(round(mult))) if mult >= 1 else 1
        positions = _scaled_live_book(rows, whole)
        if mult < 1:
            positions = positions[: max(2, int(round(len(rows) * mult)))]
        candidate = positions[0]
        committed = tuple(positions[1:])
        merged = (*committed, candidate)

        # --- the marginal dict, exactly as the loop builds it ----------------
        tickers = sorted({leg.market_ticker for p in positions for leg in p.legs})
        marginals = {t: 0.45 for t in tickers}
        priced = marginals.__contains__

        all_pairs = [
            (tickers[i], tickers[j])
            for i in range(len(tickers))
            for j in range(i + 1, len(tickers))
        ]
        sg_pairs = within_game_pair_tickers(merged, priced)
        waste = (
            100.0 * (len(all_pairs) - len(sg_pairs)) / len(all_pairs)
            if all_pairs
            else 0.0
        )

        # --- STAGE A: rho resolution, both scopings, COLD and MEMO-WARM ------
        # COLD = the FIRST accept of a process (nothing memoised yet), which is
        # the case the live timeouts died in. WARM = every accept after it.
        rho.invalidate_sgp_cache()
        cold_all, _ = _time_pairs(rho, all_pairs, args.pair_sample_cap)
        rho.invalidate_sgp_cache()
        cold_sg, _ = _time_pairs(rho, sg_pairs, args.pair_sample_cap)
        _time_pairs(rho, all_pairs, args.pair_sample_cap)
        per_all, _ = _time_pairs(rho, all_pairs, args.pair_sample_cap)
        _time_pairs(rho, sg_pairs, args.pair_sample_cap)
        per_sg, _ = _time_pairs(rho, sg_pairs, args.pair_sample_cap)
        a_all_ms = per_all * len(all_pairs)
        a_sg_ms = per_sg * len(sg_pairs)
        cold_all_ms = cold_all * len(all_pairs)
        cold_sg_ms = cold_sg * len(sg_pairs)
        cold_lines.append(
            f"  {mult:g}x  COLD rho: ALL {cold_all_ms:10,.0f} ms  ->  "
            f"SAME-GAME {cold_sg_ms:8,.1f} ms   "
            f"({cold_all:.4f} vs {cold_sg:.4f} ms/pair)"
        )

        # --- STAGE B: the rest of the build (fixed-ish) ----------------------
        rho_dict = {frozenset(p): rho(*p) for p in sg_pairs if rho(*p) is not None}
        b_times = []
        for _ in range(args.reps):
            t0 = time.perf_counter()
            build_book_model(
                list(committed),
                marginals=marginals.get,
                within_game_rho=rho,
            )
            b_times.append((time.perf_counter() - t0) * 1000.0)
        b_times.sort()
        b_ms = b_times[len(b_times) // 2]

        # --- STAGE C: the MC, in the WORKER's own composition ----------------
        inputs = CandidateBookRiskInputs(
            committed=committed,
            candidate=candidate,
            reservations=(),
            marginals=marginals,
            within_game_rho_pairs=rho_dict,
            structural_cfg=None,
            n_samples=n_samples,
            seed=0,
            band="high",
            bankroll_cc=205_041.0,
            current_equity_cc=205_041.0,
            # The gate's OWN defaults, read off ``evaluate_candidate_book_risk``
            # rather than typed here — this bench must not invent a risk setting.
            ruin_floor_frac=_default("ruin_floor_frac"),
            ruin_prob_ci_z=_default("ruin_prob_ci_z"),
            portfolio_cvar_frac=_default("portfolio_cvar_frac"),
            portfolio_det_max_frac=_default("portfolio_det_max_frac"),
            portfolio_ruin_prob_budget=_default("portfolio_ruin_prob_budget"),
            absolute_notional_multiple=_default("absolute_notional_multiple"),
            hedge_cost_budget_cc=_default("hedge_cost_budget_cc"),
            allow_negative_ev_hedge=_default("allow_negative_ev_hedge"),
        )
        c_times = []
        for _ in range(args.reps):
            t0 = time.perf_counter()
            _worker_candidate_book_risk(inputs)
            c_times.append((time.perf_counter() - t0) * 1000.0)
        c_times.sort()
        c_ms = c_times[len(c_times) // 2]

        before = a_all_ms + b_ms + c_ms
        after = a_sg_ms + b_ms + c_ms
        label = f"{mult:g}x"
        print(
            f"{label:>6} {len(positions):5d} {len(tickers):5d} {len(all_pairs):9,d} "
            f"{len(sg_pairs):8,d} {waste:6.2f}% {a_all_ms:8.1f}ms {a_sg_ms:7.1f}ms "
            f"{b_ms:6.1f}ms {c_ms:7.1f}ms {before:8.1f}ms {after:8.1f}ms "
            f"{WINDOW_MS - after:+8.1f}ms"
        )

    print("\nCOLD rho (first accept of a process — the case the live timeouts died in):")
    for line in cold_lines:
        print(line)

    print(
        "\nA_all = rho at ALL pairs (pre-B1)   A_sg = rho at SAME-GAME pairs (post-B1)"
        "\nB     = marginals + committed-only model   C_mc = the worker MC, ONE attempt"
        "\nBEFORE/AFTER = A + B + C for ONE MC attempt; margin = window - AFTER."
        "\nThe confirm RTT reserve is NOT subtracted here (it is measured live and "
        "\nsubtracted by _confirm_window_reserve_ns) — this is the compute term only."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
