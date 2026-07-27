"""Re-cut the fidelity run's per-case lines: sign agreement on RESOLVABLE deltas.

A sign-agreement rate computed over ALL candidates is dominated by the ones whose
true dP(book) is inside the MC noise floor — there the "true" sign is itself a coin
flip, so neither the estimator nor a second run of the gate can agree with it. This
re-cuts the same rows by |d| band so the direction claim is honest.

    .venv/Scripts/python.exe tools/diagnostics/pbook_cheap_marginal_summarize.py FILE
"""

from __future__ import annotations

import re
import sys

import numpy as np

PAT = re.compile(
    r"\]\s+(\w+)\s+(\d+)leg\s+(T\d)\s+truth20k=([-+\d.]+)\s+ref\S*=([-+\d.]+)\s+"
    r"cheapA=([-+\d.]+)\s+cheapB\[(T\d|-)\]=([-+\d.]+)"
)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    rows = []
    for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
        m = PAT.search(line)
        if m:
            rows.append(
                {
                    "rfq": m.group(1),
                    "n_legs": int(m.group(2)),
                    "tierA": m.group(3),
                    "truth20k": float(m.group(4)),
                    "ref": float(m.group(5)),
                    "cheapA": float(m.group(6)),
                    "tierB": m.group(7),
                    "cheapB": float(m.group(8)),
                }
            )
    print(f"parsed {len(rows)} cases")
    if not rows:
        return 1
    ref = np.array([r["ref"] for r in rows])
    print(f"reference dp_book: mean {ref.mean():+.5f}  sd {ref.std():.5f}  "
          f"share > 0: {(ref > 0).mean() * 100:.1f}%  "
          f"|d| median {np.median(np.abs(ref)):.5f}")

    for est in ("cheapB", "cheapA", "truth20k"):
        e = np.array([r[est] for r in rows])
        print(f"\n=== {est} ===")
        for lo in (0.0, 0.001, 0.002, 0.004):
            keep = np.abs(ref) >= lo
            if keep.sum() < 3:
                continue
            agree = (np.sign(e[keep]) == np.sign(ref[keep])).mean()
            err = e[keep] - ref[keep]
            r = (np.corrcoef(e[keep], ref[keep])[0, 1]
                 if e[keep].std() > 0 else float("nan"))
            print(f"  |d_ref| >= {lo:.3f}  n={keep.sum():>3}  "
                  f"sign {agree * 100:5.1f}%   r {r:+.4f}   "
                  f"|err| med {np.median(np.abs(err)):.5f}  "
                  f"p90 {np.quantile(np.abs(err), 0.9):.5f}")
        for k, lab in ((2, "2-leg"), (3, "3+ leg")):
            keep = (np.array([r["n_legs"] for r in rows]) == 2) if k == 2 else (
                np.array([r["n_legs"] for r in rows]) > 2)
            if keep.sum() < 3:
                continue
            agree = (np.sign(e[keep]) == np.sign(ref[keep])).mean()
            err = e[keep] - ref[keep]
            print(f"  {lab:<14} n={keep.sum():>3}  sign {agree * 100:5.1f}%   "
                  f"|err| med {np.median(np.abs(err)):.5f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
