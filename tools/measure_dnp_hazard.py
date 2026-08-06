"""Regenerate the BASELINE_DNP_LEG_RESULTS table in pricing/dnp_scalar.py.

Measures the per-family DNP hazard (P(leg grades ``scalar``)) from OUR OWN
settled-leg corpus — exchange-truth graded results for every leg of every
settled live-era combo. Never type a rate by hand: re-run this tool on a
fresh corpus pull and paste the emitted block (same convention as the
conditional-table exports).

Input: a JSON mapping ``ticker -> {"result": "yes"|"no"|"scalar",
"status": ..., ...}`` — the shape produced by the forensics/sweep leg-result
pulls (paced public ``GET /markets/{ticker}``; e.g. the 2026-08-06 pull's
``night/leg_results.json``). Only ``status == "finalized"`` rows count.

Usage:
    python -m tools.measure_dnp_hazard <leg_results.json>

Rule 8: imports the live classifier (``classify_leg``) — the exact family
mapping the guard prices with — and edits nothing.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from combomaker.pricing.dnp_scalar import DNP_SCALAR_FAMILIES, DnpHazards
from combomaker.pricing.legtypes import classify_leg


def measure(leg_results: dict[str, dict]) -> dict[str, tuple[int, int]]:
    scalar: Counter[str] = Counter()
    total: Counter[str] = Counter()
    for ticker, row in leg_results.items():
        family = classify_leg(ticker)
        if family not in DNP_SCALAR_FAMILIES:
            continue
        if row.get("status") != "finalized":
            continue
        result = row.get("result")
        if result not in ("yes", "no", "scalar"):
            continue
        total[family.value] += 1
        if result == "scalar":
            scalar[family.value] += 1
    return {fam: (scalar[fam], total[fam]) for fam in sorted(total)}


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    counts = measure(json.loads(Path(sys.argv[1]).read_text()))
    hazards = DnpHazards(counts=counts)
    pooled = hazards.pooled()
    print("# Paste into pricing/dnp_scalar.py BASELINE_DNP_LEG_RESULTS")
    print("# (update the provenance comment with the corpus pull date/size).")
    print("BASELINE_DNP_LEG_RESULTS: Mapping[str, tuple[int, int]] = {")
    for fam, (s, t) in counts.items():
        print(f'    "{fam}": ({s}, {t}),')
    print("}")
    print()
    print(f"# pooled: {sum(s for s, _ in counts.values())}"
          f"/{sum(t for _, t in counts.values())}"
          f" = {100 * pooled:.2f}%" if pooled is not None else "# pooled: n/a")
    for fam in counts:
        rate = hazards.family_rate(fam)
        s, t = counts[fam]
        own = f"{100 * s / t:.2f}%" if t else "n/a"
        used = f"{100 * rate:.2f}%" if rate is not None else "n/a"
        thin = " (thin -> pooled)" if t and rate is not None and abs(rate - s / t) > 1e-12 else ""
        print(f"# {fam}: own {own}, used {used}{thin}")


if __name__ == "__main__":
    main()
