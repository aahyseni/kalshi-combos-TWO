"""OOS gate: MLB NegBin runs model vs v1 copula vs independence.

Data: sportsbookreviewsonline season archives (mlb-odds-2015..2021.xlsx,
fetched 2026-07-06; the archive stops after 2021) — closing moneylines, run
lines (+/-1.5 with prices), closing O/U totals with prices, final scores.
Parsed with stdlib zip+xml (no xlsx dependency).

Setup:
  - dispersion k = 3.63, the 2015-2019 Retrosheet fit (train-era only; the
    2021-2024 fit is 3.62 — era-stable — but excluded to keep 2021 clean)
  - per TEST game: invert (mu_home, mu_away) from devigged closing ML +
    devigged closing O/U prob at the closing line (exactly identified)
  - marginals for ALL models are market-implied: p_hw (ML devig), p_over
    (O/U devig), p_cover (run-line devig) — no model-derived marginals
  - v1 copula uses the SHIPPED config exactly: mlb ml|total -0.05; the
    ml|runline and runline|total pairs have NO calibrated entry and fall
    back to the flat same-event prior 0.6 (that IS what v1 quotes today)
  - 2020 season skipped (60-game rule-flux season)

TEST = 2021 (most recent season with odds). Gate: the runs model must beat
the v1 copula on ALL metrics to flip mlb_runs.enabled.

Run:  uv run python tools/validate_mlb_runs_oos.py
"""

from __future__ import annotations

import math
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np

from combomaker.pricing.copula import gaussian_copula_joint_prob
from combomaker.pricing.dixon_coles import StructuralError, Team
from combomaker.pricing.margin_total import GameTotalOver, SpreadCover, TeamWins
from combomaker.pricing.mlb_runs import MlbShape, invert_runs, joint_probability

HISTORY = Path(__file__).resolve().parent.parent / "data" / "history"
TRAIN_K = 3.63          # Retrosheet 2015-2019 (train era only)
TEST_YEAR = 2021

RHO_ML_OVER = -0.05     # shipped mlb ml|total
RHO_FLAT = 0.6          # shipped same_event_rho: what v1 uses for uncal. pairs

_MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def read_rows(path: Path) -> list[list[str]]:
    with zipfile.ZipFile(path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.iter(f"{_MAIN_NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{_MAIN_NS}t")))
        sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
        rows: list[list[str]] = []
        for row in sheet.iter(f"{_MAIN_NS}row"):
            cells: dict[int, str] = {}
            for c in row:
                ref = c.get("r", "")
                col = 0
                for ch in ref:
                    if ch.isalpha():
                        col = col * 26 + ord(ch) - 64
                v = c.find(f"{_MAIN_NS}v")
                if v is None or v.text is None:
                    continue
                cells[col] = shared[int(v.text)] if c.get("t") == "s" else v.text
            if cells:
                rows.append([cells.get(i, "") for i in range(1, max(cells) + 1)])
        return rows


def american_prob(raw: str) -> float | None:
    m = re.fullmatch(r"-?\d+(?:\.0+)?", raw.strip())
    if m is None:
        return None
    ml = float(raw)
    if abs(ml) < 100:
        return None
    return 100.0 / (ml + 100.0) if ml > 0 else -ml / (-ml + 100.0)


def devig(p_a: float | None, p_b: float | None) -> float | None:
    if p_a is None or p_b is None:
        return None
    return p_a / (p_a + p_b)


@dataclass(frozen=True, slots=True)
class Game:
    p_home: float
    p_over: float
    p_home_cover: float
    total_line: float
    home_rl: float           # home run line (+/-1.5)
    home_runs: int
    away_runs: int


def _col(header: list[str], *names: str) -> int:
    squished = [h.replace(" ", "").lower() for h in header]
    for name in names:
        if name in squished:
            return squished.index(name) + 1
    raise KeyError(names)


def load_season(path: Path) -> list[Game]:
    rows = read_rows(path)
    header, data = rows[0], rows[1:]
    i_final = _col(header, "final")
    i_close = _col(header, "close")
    i_rl = _col(header, "runline")
    i_close_ou = _col(header, "closeou")

    def get(row: list[str], idx: int) -> str:
        return row[idx - 1] if len(row) >= idx else ""

    games: list[Game] = []
    for v_row, h_row in zip(data[0::2], data[1::2], strict=False):
        if get(v_row, 3) != "V" or get(h_row, 3) != "H":
            continue
        try:
            away = int(float(get(v_row, i_final)))
            home = int(float(get(h_row, i_final)))
            total_line = float(get(h_row, i_close_ou))
            home_rl = float(get(h_row, i_rl))
        except ValueError:
            continue
        p_home = devig(american_prob(get(h_row, i_close)), american_prob(get(v_row, i_close)))
        p_over = devig(
            american_prob(get(v_row, i_close_ou + 1)),
            american_prob(get(h_row, i_close_ou + 1)),
        )
        p_cover = devig(
            american_prob(get(h_row, i_rl + 1)), american_prob(get(v_row, i_rl + 1))
        )
        if p_home is None or p_cover is None or abs(home_rl) != 1.5:
            continue
        if p_over is None:
            p_over = 0.5
        games.append(
            Game(
                p_home=p_home,
                p_over=p_over,
                p_home_cover=p_cover,
                total_line=total_line,
                home_rl=home_rl,
                home_runs=home,
                away_runs=away,
            )
        )
    return games


def cell_ll2(pa: float, pb: float, ab: float, a: bool, b: bool) -> float:
    ab = min(min(pa, pb), max(ab, max(0.0, pa + pb - 1.0)))
    cells = {
        (True, True): ab,
        (True, False): pa - ab,
        (False, True): pb - ab,
        (False, False): 1.0 - pa - pb + ab,
    }
    return math.log(max(cells[(a, b)], 1e-12))


def copula_pair(pa: float, pb: float, rho: float) -> float:
    return gaussian_copula_joint_prob([pa, pb], np.array([[1.0, rho], [rho, 1.0]]))


def copula_cell3(marg: tuple[float, float, float], corr: np.ndarray,
                 signs: tuple[bool, bool, bool]) -> float:
    m = [p if s else 1.0 - p for p, s in zip(marg, signs, strict=True)]
    flip = np.array([1.0 if s else -1.0 for s in signs])
    return gaussian_copula_joint_prob(m, corr * np.outer(flip, flip))


def main() -> None:
    test = load_season(HISTORY / f"mlb-odds-{TEST_YEAR}.xlsx")
    print(f"test season {TEST_YEAR}: {len(test)} games with full closing prices")

    shape = MlbShape(dispersion_k=TRAIN_K)
    corr3 = np.array(
        [
            [1.0, RHO_FLAT, RHO_ML_OVER],
            [RHO_FLAT, 1.0, RHO_FLAT],
            [RHO_ML_OVER, RHO_FLAT, 1.0],
        ]
    )
    sums = {
        "pair hw x over": dict.fromkeys(("independence", "v1 copula", "structural"), 0.0),
        "pair hw x cover": dict.fromkeys(("independence", "v1 copula", "structural"), 0.0),
        "triple hw x cover x over": dict.fromkeys(
            ("independence", "v1 copula", "structural"), 0.0
        ),
    }
    n = skipped = 0
    for g in test:
        margin = g.home_runs - g.away_runs
        total = g.home_runs + g.away_runs
        if total == g.total_line:
            skipped += 1
            continue
        hw, over, cover = margin > 0, total > g.total_line, margin > -g.home_rl
        try:
            inv = invert_runs(
                [(TeamWins(Team.A), g.p_home), (GameTotalOver(g.total_line), g.p_over)],
                shape,
            )
        except StructuralError:
            skipped += 1
            continue
        n += 1
        cover_spec = SpreadCover(Team.A, -g.home_rl)

        def joint(*legs: tuple, _inv=inv) -> float:  # type: ignore[type-arg, no-untyped-def]
            return joint_probability(_inv.mu_a, _inv.mu_b, shape, list(legs))

        # pair hw x over
        sums["pair hw x over"]["independence"] += cell_ll2(
            g.p_home, g.p_over, g.p_home * g.p_over, hw, over
        )
        sums["pair hw x over"]["v1 copula"] += cell_ll2(
            g.p_home, g.p_over, copula_pair(g.p_home, g.p_over, RHO_ML_OVER), hw, over
        )
        sums["pair hw x over"]["structural"] += cell_ll2(
            g.p_home, g.p_over,
            joint((TeamWins(Team.A), True), (GameTotalOver(g.total_line), True)),
            hw, over,
        )

        # pair hw x cover (market-implied cover marginal for every model)
        sums["pair hw x cover"]["independence"] += cell_ll2(
            g.p_home, g.p_home_cover, g.p_home * g.p_home_cover, hw, cover
        )
        sums["pair hw x cover"]["v1 copula"] += cell_ll2(
            g.p_home, g.p_home_cover,
            copula_pair(g.p_home, g.p_home_cover, RHO_FLAT), hw, cover,
        )
        sums["pair hw x cover"]["structural"] += cell_ll2(
            g.p_home, g.p_home_cover,
            joint((TeamWins(Team.A), True), (cover_spec, True)), hw, cover,
        )

        # triple
        observed = (hw, cover, over)
        marg = (g.p_home, g.p_home_cover, g.p_over)
        p_ind = math.prod(p if s else 1 - p for p, s in zip(marg, observed, strict=True))
        sums["triple hw x cover x over"]["independence"] += math.log(max(p_ind, 1e-12))
        sums["triple hw x cover x over"]["v1 copula"] += math.log(
            max(copula_cell3(marg, corr3, observed), 1e-12)
        )
        p_struct = joint(
            (TeamWins(Team.A), observed[0]),
            (cover_spec, observed[1]),
            (GameTotalOver(g.total_line), observed[2]),
        )
        sums["triple hw x cover x over"]["structural"] += math.log(max(p_struct, 1e-12))

    print(f"scored n={n} ({skipped} skipped: pushes/missing/inversion refusals)")
    gate_pass = True
    for metric, models in sums.items():
        scores = {m: -ll / n for m, ll in models.items()}
        beats = scores["structural"] < scores["v1 copula"]
        gate_pass = gate_pass and beats
        line = "  ".join(f"{m}={v:.5f}" for m, v in scores.items())
        print(f"  {metric:26s}: {line}   "
              f"{'structural BEATS v1' if beats else 'structural does NOT beat v1'}")

    print(
        "\nGATE: "
        + (
            "PASS — flip mlb_runs.enabled=True with this evidence in NOTES.md"
            if gate_pass
            else "FAIL — mlb_runs stays disabled (directive point 4)"
        )
    )
    sys.exit(0 if gate_pass else 1)


if __name__ == "__main__":
    main()
