"""LEVER #5 — END-TO-END QUOTE-PATH THROUGHPUT (2026-07-27 review, finding B4).

``tools/diagnostics/bench_size_invariance.py`` times ``compute_inventory_skew``.
That is the classifier, not the quote. THIS script drives the REAL
``QuoteLifecycle.handle_rfq`` — pricing, quoting policy, the lifecycle's own
``_concentration_profile`` build, quote construction, the store write — and
reports **quotes/min**, which is the number the standing rule
("throughput never regresses") is stated in.

Three configurations, all of which can ship today:

    OFF      ``conc_enabled=False``  — the zero-cost rollback. The lifecycle
             does not even build the profile. This is byte-equivalent to the
             pre-Lever-#5 quote path.
    SHIPPED  ``conc_armed=False``    — computed + logged, never priced. THIS IS
             WHAT THE RESTART RUNS.
    ARMED    ``conc_armed=True``     — the cost the operator weighs when arming.

Hard rule 8: this drives the real lifecycle through the existing test rig; it
reimplements nothing.

    .venv/Scripts/python.exe tools/diagnostics/bench_conc_quote_path.py
"""

from __future__ import annotations

import asyncio
import dataclasses
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from combomaker.ops.logging import configure_logging  # noqa: E402

# A bench that prints 300 structlog lines per RFQ measures the LOGGER. Silence
# it: the quote path's own logging cost is identical in all three modes bar the
# handful of conc_* fields, which are measured with it.
configure_logging(json_output=False, level="CRITICAL")

from combomaker.core.conventions import Side  # noqa: E402
from combomaker.core.money import CentiCents  # noqa: E402
from combomaker.core.quantity import CentiContracts  # noqa: E402
from combomaker.ops.persistence import Store  # noqa: E402
from combomaker.risk.exposure import LegRef, OpenPosition  # noqa: E402
from combomaker.risk.limits import LimitChecker, RiskLimits  # noqa: E402
from combomaker.risk.skew import SkewParams  # noqa: E402

SHIPPED = SkewParams(enabled=True, pbook_armed=True, leg_axis_armed=True)
ARMED = dataclasses.replace(SHIPPED, conc_armed=True)
OFF = dataclasses.replace(SHIPPED, conc_enabled=False)

N_RFQ = 150       # inside the default max_open_quotes, so no eviction path
N_HELD = 38       # the measured live book: ~38 tickets / ~$411 premium
SERIES = ["KXMLBKS", "KXMLBGAME", "KXMLBHR", "KXMLBTOTAL", "KXMLBRFI"]


def held_positions():
    """A live-shaped committed book, so the loss-event Herfindahl, the three
    wall maps and the leg-axis shares are all realistic."""
    out = []
    for i in range(N_HELD):
        legs = []
        for j in range(2 + i % 3):
            ev = f"{SERIES[(i + j) % 5]}-26JUL2716{i % 12:02d}TM"
            legs.append(
                LegRef(
                    market_ticker=f"{ev}-PL{(i * 3 + j) % 20}-{j + 1}",
                    event_ticker=ev,
                    side="yes" if (i + j) % 2 == 0 else "no",
                )
            )
        out.append(
            OpenPosition(
                position_id=f"held{i}",
                combo_ticker=f"COMBO-{i}",
                collection=None,
                our_side=Side.NO,
                contracts=CentiContracts(2_000 + 137 * i),
                entry_price_cc=CentiCents(2_500 + 97 * (i % 40)),
                legs=tuple(legs),
            )
        )
    return out


async def run_mode(params: SkewParams, tmp: Path, tag: str) -> float:
    from tests.test_pricing_engine import (
        CROSS_EVENT_LEGS,  # noqa: PLC0415
        combo,  # noqa: PLC0415
    )
    from tests.test_quoting_policy import PolicyRig, _harness  # noqa: PLC0415

    h = await _harness()
    store = await Store.open(tmp / f"{tag}.sqlite3", h.clock)
    rig = PolicyRig(h, store, skew_params=params)
    # BENCH SEAM: the rig's default ``max_open_quotes`` is tiny, so without this
    # the loop measures the capacity REFUSAL path instead of the quote path.
    rig.lifecycle._limits = LimitChecker(  # noqa: SLF001
        RiskLimits(max_open_quotes=100_000)
    )
    for pos in held_positions():
        rig.exposure.add_position(pos)
    # Warm: JIT-free Python still needs the caches (loss-event book, leg-axis
    # profile, the steer centre) in the state a running bot has.
    for i in range(25):
        await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id=f"warm_{i}"))
    rfqs = [combo(CROSS_EVENT_LEGS, id=f"rfq_{i}") for i in range(N_RFQ)]
    t0 = time.perf_counter()
    for r in rfqs:
        await rig.lifecycle.handle_rfq(r)
    elapsed = time.perf_counter() - t0
    sent = len(rig.sender.created)
    await store.close()
    if sent <= 25:
        raise SystemExit(f"{tag}: only {sent} quotes sent — the bench measured a "
                         f"refusal path, not the quote path")
    return elapsed / N_RFQ


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    results: dict[str, list[float]] = {"OFF": [], "SHIPPED": [], "ARMED": []}
    # Interleave the modes across repetitions so a machine-state drift cannot
    # land entirely on one configuration.
    for rep in range(5):
        for tag, params in (("OFF", OFF), ("SHIPPED", SHIPPED), ("ARMED", ARMED)):
            results[tag].append(
                asyncio.run(run_mode(params, tmp, f"{tag}{rep}"))
            )
    print("=" * 74)
    print("LEVER #5 — QUOTE-PATH THROUGHPUT (real handle_rfq, not the classifier)")
    print("=" * 74)
    print(f"book: {N_HELD} held positions   RFQs per rep: {N_RFQ}   reps: 5")
    # MEDIAN, not best-of-N: a full handle_rfq is dominated by the sqlite
    # decision write, so the per-rep spread is tens of percent and a best-of-N
    # would report whichever mode happened to catch the quietest moment.
    print(f"  {'mode':<9}{'median us':>11}{'quotes/min':>13}{'vs OFF':>10}"
          f"{'  [min .. max] us':>22}")
    base = statistics.median(results["OFF"])
    for tag in ("OFF", "SHIPPED", "ARMED"):
        med = statistics.median(results[tag])
        print(f"  {tag:<9}{med * 1e6:11.1f}{60.0 / med:13,.0f}"
              f"{(med / base - 1) * 100:+9.2f}%"
              f"   [{min(results[tag]) * 1e6:8.1f} .. {max(results[tag]) * 1e6:8.1f}]")
    spread = (max(results["OFF"]) - min(results["OFF"])) / base
    print(f"  within-mode spread on OFF alone: {spread:.1%} of the median")
    print()
    print("READ THIS HONESTLY: at handle_rfq granularity the store write")
    print("dominates, so the steer's ~20us classifier cost is far below the")
    print("run-to-run noise here. The PRECISE per-candidate cost is measured by")
    print("tools/diagnostics/bench_size_invariance.py; this script's job is to")
    print("show what that cost is as a FRACTION of a real quote.")
    print()
    print("OFF is the byte-equivalent of the pre-Lever-#5 quote path: the")
    print("lifecycle never builds the profile and the classifier never computes")
    print("the steer. SHIPPED pays the full compute for the shadow read-out and")
    print("prices nothing; ARMED is what the operator is asked to authorise.")
    print("=" * 74)


if __name__ == "__main__":
    main()
