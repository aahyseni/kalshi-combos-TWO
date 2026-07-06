"""Calibrate the MLB NegBin runs dispersion from Retrosheet game logs.

k solves var = mu + mu^2/k on per-team final runs (method of moments),
pooled across home/away (the model can't see which side is home; the k band
covers the asymmetry, which is quantified here). Recent window 2021-2024
(pitch-clock era included), era check 2015-2019, 2020 short season skipped.

Run:  uv run python tools/calibrate_mlb_runs.py
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

HISTORY = Path(__file__).resolve().parent.parent / "data" / "history"


def load_runs(years: range) -> tuple[np.ndarray, np.ndarray]:
    visitor, home = [], []
    for path in sorted(HISTORY.glob("GL*.TXT")):
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            for fields in csv.reader(f):
                try:
                    year = int(fields[0][:4])
                    v, h = int(fields[9]), int(fields[10])
                except (IndexError, ValueError):
                    continue
                if year in years:
                    visitor.append(v)
                    home.append(h)
    return np.array(visitor, dtype=float), np.array(home, dtype=float)


def k_of(runs: np.ndarray) -> float:
    mu, var = float(np.mean(runs)), float(np.var(runs))
    return mu * mu / (var - mu)


def report(name: str, years: range) -> None:
    v, h = load_runs(years)
    pooled = np.concatenate([v, h])
    print(
        f"  {name:12s} n={len(pooled):6d}  mu={np.mean(pooled):.2f} "
        f"var={np.var(pooled):.2f}  k_pooled={k_of(pooled):5.2f}  "
        f"k_away={k_of(v):5.2f}  k_home={k_of(h):5.2f}"
    )


def main() -> None:
    print("MLB NegBin dispersion (Retrosheet final scores):")
    report("2021-2024", range(2021, 2025))
    report("2015-2019", range(2015, 2020))


if __name__ == "__main__":
    main()
