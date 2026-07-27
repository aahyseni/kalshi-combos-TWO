"""BASELINE COST: the current leg-axis heuristic on the quote path.

The reference the cheap ΔP(book) marginal has to fit next to. Builds a
``LegAxisProfile`` whose key counts match the LIVE book captured in the fidelity
study's pickle (real family/entity keys, real premium shares) and times the live
``risk/skew._leg_axis_component`` on the real candidates.

Read-only, offline, no live module touched.
"""

from __future__ import annotations

import argparse
import pickle
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from combomaker.risk.exposure import leg_entity_key, leg_family_key  # noqa: E402
from combomaker.risk.skew import (  # noqa: E402
    LegAxisProfile,
    SkewParams,
    _leg_axis_component,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", default="data/_pbook_cheap/inputs_all.pkl")
    ap.add_argument("--iters", type=int, default=20_000)
    args = ap.parse_args()
    cases = pickle.load(open(args.inputs, "rb"))
    inp = cases[0]["inputs"]

    fam: dict[str, float] = {}
    ent: dict[str, float] = {}
    for pos in inp.committed:
        prem = float(pos.contracts) * float(pos.entry_price_cc) / 100.0
        for leg in pos.legs:
            fam[leg_family_key(leg)] = fam.get(leg_family_key(leg), 0.0) + prem
            ent[leg_entity_key(leg)] = ent.get(leg_entity_key(leg), 0.0) + prem
    tf, te = sum(fam.values()), sum(ent.values())
    profile = LegAxisProfile(
        shares_by_family={k: v / tf for k, v in fam.items()},
        total_family_cc=tf,
        shares_by_entity={k: v / te for k, v in ent.items()},
        total_entity_cc=te,
        family_budget_cc=tf * 1.5,
        entity_budget_cc=tf * 1.5,
        p_book=0.5887,
    )
    params = SkewParams(leg_axis_enabled=True, leg_axis_armed=True)
    cands = [c["inputs"].candidate for c in cases]
    print(
        f"live book: {len(inp.committed)} positions, "
        f"{len(fam)} family keys, {len(ent)} entity keys; "
        f"{len(cands)} real candidates"
    )

    for c in cands[:10]:
        _leg_axis_component(c, params, profile)
    t0 = time.perf_counter()
    it = 0
    while it < args.iters:
        for c in cands:
            _leg_axis_component(c, params, profile)
            it += 1
            if it >= args.iters:
                break
    us = (time.perf_counter() - t0) / it * 1e6
    print(f"_leg_axis_component: {us:.2f} us/candidate  (n={it})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
