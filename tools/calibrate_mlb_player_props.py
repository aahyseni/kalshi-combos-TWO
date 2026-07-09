"""Calibrate MLB player-prop SGP correlation pairs from Retrosheet event files.

Domain: BASEBALL player props (batter home runs, starting-pitcher strikeouts).
These are UNCALIBRATED SGP pairs; the pricer's marginals come live from Kalshi
leg books, so here we calibrate ONLY the joint/correlation layer, exactly the
way tools/calibrate_pairs_from_history.py does for game-level pairs:

  over N historical (batter|pitcher)-games measure P(A), P(B), P(A|B) then invert
  the SAME Gaussian copula the pricer runs (combomaker.pricing.copula) to turn
  P(A&B) into a copula rho -> a drop-in `pair_rho`. 99% CI = binomial SE on
  P(A&B) pushed through the (monotone) rho solver.

Data path (robust, free): Retrosheet EVENT files
  https://www.retrosheet.org/events/<year>eve.zip   (play-by-play)
parsed directly (no Chadwick needed) for batter HR and pitcher K, joined to the
Retrosheet TEAM game logs (gl<year>.txt, already in data/history/) for official
final team scores / winner.

ADDITIVE ONLY: new file, imports the shipped copula, touches no src/ or config.

Run:
  .venv/Scripts/python.exe tools/calibrate_mlb_player_props.py 2022 2023 2024
"""

from __future__ import annotations

import csv
import io
import math
import sys
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

import numpy as np

from combomaker.pricing.copula import gaussian_copula_joint_prob

HISTORY = Path(__file__).resolve().parents[1] / "data" / "history"
_Z99 = 2.576

# Retrosheet play "basic play" tokens that are baserunning-only / no plate
# appearance -> excluded when deciding whether a batter had a PA.
_NON_PA_PREFIXES = ("SB", "CS", "PO", "WP", "PB", "BK", "DI", "OA", "NP", "FLE")


# --------------------------------------------------------------------------- #
# copula inversion (mirrors tools/calibrate_pairs_from_history.py)
# --------------------------------------------------------------------------- #
def implied_rho(p_a: float, p_b: float, p_ab: float) -> float:
    """The rho making copula(p_a, p_b; rho) == p_ab (monotone => bisection)."""

    def joint(rho: float) -> float:
        corr = np.array([[1.0, rho], [rho, 1.0]])
        return gaussian_copula_joint_prob([p_a, p_b], corr)

    lo, hi = -0.99, 0.99
    if p_ab <= joint(lo):
        return lo
    if p_ab >= joint(hi):
        return hi
    for _ in range(60):
        mid = (lo + hi) / 2
        if joint(mid) < p_ab:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def measure(records: list[dict], a: str, b: str) -> tuple[int, float, float, float, float]:
    rows = [m for m in records if m.get(a) is not None and m.get(b) is not None]
    n = len(rows)
    if n == 0:
        return 0, float("nan"), float("nan"), float("nan"), float("nan")
    p_a = sum(1 for m in rows if m[a]) / n
    p_b = sum(1 for m in rows if m[b]) / n
    p_ab = sum(1 for m in rows if m[a] and m[b]) / n
    return n, p_a, p_b, p_ab, implied_rho(p_a, p_b, p_ab)


def rho_ci99(records: list[dict], a: str, b: str) -> tuple[float, float]:
    """99% CI on implied rho: binomial SE on P(A&B) through the monotone solver."""
    n, p_a, p_b, p_ab, _ = measure(records, a, b)
    if n == 0:
        return float("nan"), float("nan")
    se = math.sqrt(max(p_ab * (1 - p_ab), 1e-12) / n)
    lo = implied_rho(p_a, p_b, max(0.0, p_ab - _Z99 * se))
    hi = implied_rho(p_a, p_b, min(1.0, p_ab + _Z99 * se))
    return lo, hi


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
def event_zip(year: int) -> Path:
    path = HISTORY / f"{year}eve.zip"
    if not path.exists():
        url = f"https://www.retrosheet.org/events/{year}eve.zip"
        print(f"  downloading {url}", file=sys.stderr)
        urllib.request.urlretrieve(url, path)  # noqa: S310
    return path


def load_gl_scores(years: list[int]) -> dict[str, tuple[str, str, int, int]]:
    """key = hometeam+date+gamenum  ->  (vis_team, home_team, vis_score, home_score)."""
    lookup: dict[str, tuple[str, str, int, int]] = {}
    for year in years:
        gl = HISTORY / f"gl{year}.txt"
        if not gl.exists():
            print(f"  WARN missing game log {gl.name}", file=sys.stderr)
            continue
        with open(gl, encoding="latin-1", newline="") as f:
            for fields in csv.reader(f):
                try:
                    date, gnum = fields[0], fields[1]
                    vis, home = fields[3], fields[6]
                    vs, hs = int(fields[9]), int(fields[10])
                except (IndexError, ValueError):
                    continue
                lookup[f"{home}{date}{gnum}"] = (vis, home, vs, hs)
    return lookup


def basic_play(event: str) -> str:
    """Retrosheet event -> basic play token (strip /modifiers and .advances)."""
    return event.split("/", 1)[0].split(".", 1)[0]


def parse_year(
    year: int, gl: dict[str, tuple[str, str, int, int]]
) -> tuple[list[dict], list[dict], dict[str, int]]:
    """Parse one season's event files.

    Returns (batter_game_records, starter_game_records, stats).
    Each batter record: game, batter, team_home(bool), hr(bool), team_runs, won.
    Each starter record: game, pitcher, team_home(bool), ks(int), team_runs,
      opp_runs, game_total, won.
    Scores/winner come from the joined official game log.
    """
    stats = defaultdict(int)
    batter_games: list[dict] = []
    starter_games: list[dict] = []

    zf = zipfile.ZipFile(event_zip(year))
    for name in zf.namelist():
        if not (name.endswith(".EVA") or name.endswith(".EVN")):
            continue
        text = zf.read(name).decode("latin-1")

        # per-game accumulators
        cur_id: str | None = None
        home_starter = vis_starter = None
        cur_pitcher = {"0": None, "1": None}  # side -> current pitcher id (0=vis,1=home)
        batter_hr: dict[tuple[str, str], bool] = {}  # (batter, side) -> homered
        batter_pa: set[tuple[str, str]] = set()
        pitcher_k: dict[str, int] = defaultdict(int)

        def flush() -> None:
            nonlocal cur_id
            if cur_id is None:
                return
            rec = gl.get(cur_id)
            stats["games_seen"] += 1
            if rec is None:
                stats["games_unjoined"] += 1
                return
            _vis, _home, vs, hs = rec
            if vs == hs:  # tie/suspended -> no winner defined
                stats["games_tie"] += 1
                return
            stats["games_joined"] += 1
            total = vs + hs
            home_won = hs > vs
            # batter-game rows
            for (batter, side) in batter_pa:
                is_home = side == "1"
                team_runs = hs if is_home else vs
                won = home_won if is_home else (not home_won)
                batter_games.append(
                    {
                        "year": year,
                        "game": cur_id,
                        "batter": batter,
                        "team_home": is_home,
                        "hr": batter_hr.get((batter, side), False),
                        "team_runs": team_runs,
                        "won": won,
                    }
                )
            # starter-game rows (one per side)
            for side, starter in (("1", home_starter), ("0", vis_starter)):
                if starter is None:
                    continue
                is_home = side == "1"
                team_runs = hs if is_home else vs
                opp_runs = vs if is_home else hs
                won = home_won if is_home else (not home_won)
                starter_games.append(
                    {
                        "year": year,
                        "game": cur_id,
                        "pitcher": starter,
                        "team_home": is_home,
                        "ks": pitcher_k.get(starter, 0),
                        "team_runs": team_runs,
                        "opp_runs": opp_runs,
                        "game_total": total,
                        "won": won,
                    }
                )

        for fields in csv.reader(io.StringIO(text)):
            if not fields:
                continue
            rt = fields[0]
            if rt == "id":
                flush()
                cur_id = fields[1]
                home_starter = vis_starter = None
                cur_pitcher = {"0": None, "1": None}
                batter_hr = {}
                batter_pa = set()
                pitcher_k = defaultdict(int)
            elif rt in ("start", "sub"):
                # start,pid,"name",team,order,pos
                try:
                    pid, side, pos = fields[1], fields[3], fields[5]
                except IndexError:
                    continue
                if pos == "1":
                    cur_pitcher[side] = pid
                    if rt == "start":
                        if side == "1":
                            home_starter = pid
                        else:
                            vis_starter = pid
            elif rt == "play":
                # play,inning,half,batter,count,pitches,event
                try:
                    half, batter, event = fields[2], fields[3], fields[6]
                except IndexError:
                    continue
                base = basic_play(event)
                if base == "" or base.startswith(_NON_PA_PREFIXES):
                    continue  # not a completed plate appearance
                side = half  # 0 = visitor batting, 1 = home batting
                batter_pa.add((batter, side))
                if base == "HR":
                    batter_hr[(batter, side)] = True
                if base.startswith("K"):
                    # defense pitcher = opposite side of the batting half
                    defside = "0" if side == "1" else "1"
                    p = cur_pitcher[defside]
                    if p is not None:
                        pitcher_k[p] += 1
        flush()
    return batter_games, starter_games, dict(stats)


# --------------------------------------------------------------------------- #
# derive booleans / lines
# --------------------------------------------------------------------------- #
def median(vals: list[int]) -> float:
    s = sorted(vals)
    return s[len(s) // 2]


def build_batter_records(batter_games: list[dict]) -> list[dict]:
    # season median of TEAM runs (per team-game; each game contributes both teams)
    team_runs_by_year: dict[int, list[int]] = defaultdict(list)
    seen_team_game: set[tuple] = set()
    for r in batter_games:
        key = (r["game"], r["team_home"])
        if key not in seen_team_game:
            seen_team_game.add(key)
            team_runs_by_year[r["year"]].append(r["team_runs"])
    med = {y: median(v) for y, v in team_runs_by_year.items()}
    out = []
    for r in batter_games:
        m = med[r["year"]]
        tr = r["team_runs"]
        out.append(
            {
                "hr": r["hr"],
                "team_over_med": None if tr == m else tr > m,
                "team_over_45": tr > 4.5,  # fixed line
                "won": r["won"],
                "year": r["year"],
            }
        )
    return out


def build_starter_records(starter_games: list[dict], min_starts: int = 5) -> list[dict]:
    # each starter's season median K over his starts (self-normalizing prop line)
    ks_by_pitcher_year: dict[tuple[str, int], list[int]] = defaultdict(list)
    for r in starter_games:
        ks_by_pitcher_year[(r["pitcher"], r["year"])].append(r["ks"])
    k_med: dict[tuple[str, int], float] = {}
    for key, v in ks_by_pitcher_year.items():
        if len(v) >= min_starts:
            k_med[key] = median(v)
    # season medians for game total and team runs (per team-game)
    total_by_year: dict[int, list[int]] = defaultdict(list)
    teamruns_by_year: dict[int, list[int]] = defaultdict(list)
    seen_game: set[str] = set()
    for r in starter_games:
        teamruns_by_year[r["year"]].append(r["opp_runs"])  # opp_runs spans all team-games
        if r["game"] not in seen_game:
            seen_game.add(r["game"])
            total_by_year[r["year"]].append(r["game_total"])
    tot_med = {y: median(v) for y, v in total_by_year.items()}
    tr_med = {y: median(v) for y, v in teamruns_by_year.items()}
    out = []
    for r in starter_games:
        key = (r["pitcher"], r["year"])
        if key not in k_med:  # pitcher w/o a stable season K line -> excluded
            continue
        km = k_med[key]
        ks = r["ks"]
        gt = r["game_total"]
        opp = r["opp_runs"]
        tm = tot_med[r["year"]]
        rm = tr_med[r["year"]]
        out.append(
            {
                "k_over": None if ks == km else ks > km,
                "total_over": None if gt == tm else gt > tm,
                "opp_over": None if opp == rm else opp > rm,
                "won": r["won"],
                "year": r["year"],
            }
        )
    return out


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def fmt_row(label: str, records: list[dict], a: str, b: str) -> tuple[str, dict]:
    n, p_a, p_b, p_ab, rho = measure(records, a, b)
    lo, hi = rho_ci99(records, a, b)
    line = (
        f"{label:52} {n:>7} {p_a:>7.3f} {p_b:>7.3f} {p_ab:>8.3f} "
        f"{rho:>+8.3f}  [{lo:>+6.3f},{hi:>+6.3f}]"
    )
    return line, {
        "label": label,
        "n": n,
        "p_a": p_a,
        "p_b": p_b,
        "p_ab": p_ab,
        "rho": rho,
        "lo": lo,
        "hi": hi,
    }


def main() -> None:
    years = [int(x) for x in sys.argv[1:]] or [2022, 2023, 2024]
    print(f"seasons: {years}", file=sys.stderr)
    gl = load_gl_scores(years)
    print(f"game-log rows loaded: {len(gl)}", file=sys.stderr)

    all_batter: list[dict] = []
    all_starter: list[dict] = []
    agg_stats: dict[str, int] = defaultdict(int)
    for y in years:
        bg, sg, st = parse_year(y, gl)
        all_batter += bg
        all_starter += sg
        for k, v in st.items():
            agg_stats[k] += v
        print(
            f"  {y}: joined={st.get('games_joined',0)} unjoined={st.get('games_unjoined',0)} "
            f"tie={st.get('games_tie',0)} batter-games={len(bg)} starter-games={len(sg)}",
            file=sys.stderr,
        )

    brec = build_batter_records(all_batter)
    srec = build_starter_records(all_starter)

    print(f"\nParse stats: {dict(agg_stats)}")
    print(f"batter-game rows: {len(brec)}   starter-game rows (stable K line): {len(srec)}")
    hdr = (
        f"{'pair':52} {'n':>7} {'P(A)':>7} {'P(B)':>7} {'P(AB)':>8} {'rho':>8}  {'99% CI':>16}"
    )

    results: dict[str, dict] = {}
    print("\n=== BATTER props (unit = batter-game with >=1 PA) ===")
    print(hdr)
    for label, a, b in [
        ("1  HR x team-runs OVER (season median)", "hr", "team_over_med"),
        ("1b HR x team OVER 4.5 (fixed line)", "hr", "team_over_45"),
        ("2  HR x team WINS (moneyline)", "hr", "won"),
    ]:
        line, res = fmt_row(label, brec, a, b)
        print(line)
        results[label] = res

    print("\n=== STARTING-PITCHER props (unit = starter-game, >=5 starts) ===")
    print(hdr)
    for label, a, b in [
        ("3  K OVER x GAME total OVER (median)", "k_over", "total_over"),
        ("3b K OVER x OPP team total OVER (median)", "k_over", "opp_over"),
        ("4  K OVER x pitcher's team WINS", "k_over", "won"),
    ]:
        line, res = fmt_row(label, srec, a, b)
        print(line)
        results[label] = res

    # win-prob lift per HR (pair 2 colour)
    with_hr = [r for r in brec if r["hr"]]
    no_hr = [r for r in brec if not r["hr"]]
    if with_hr and no_hr:
        p_win_hr = sum(1 for r in with_hr if r["won"]) / len(with_hr)
        p_win_no = sum(1 for r in no_hr if r["won"]) / len(no_hr)
        print(
            f"\nwin-prob lift: P(team wins | batter homered)={p_win_hr:.3f} "
            f"vs P(win | no HR)={p_win_no:.3f}  lift={p_win_hr - p_win_no:+.3f}"
        )
        results["_win_lift"] = {"p_win_hr": p_win_hr, "p_win_no": p_win_no}

    # opp-UNDER framing note: gaussian-copula rho is exactly antisymmetric under
    # complementing one leg, so K_over x opp_UNDER rho = -(K_over x opp_OVER rho).
    opp = results.get("3b K OVER x OPP team total OVER (median)")
    if opp and not math.isnan(opp["rho"]):
        print(
            f"\nK OVER x OPP team total UNDER (the pitcher-controlled half): "
            f"rho = {-opp['rho']:+.3f}  (= -rho of the OVER framing; expect POSITIVE)"
        )

    # era robustness on the two headline pairs
    def era(records: list[dict], a: str, b: str, cut: int) -> str:
        early = [r for r in records if r["year"] < cut]
        late = [r for r in records if r["year"] >= cut]
        _, _, _, _, re_ = measure(early, a, b)
        _, _, _, _, rl_ = measure(late, a, b)
        return f"<{cut} rho={re_:+.3f} (n={len(early)})  >={cut} rho={rl_:+.3f} (n={len(late)})"

    if len({r["year"] for r in brec}) > 1:
        cut = sorted({r["year"] for r in brec})[len({r["year"] for r in brec}) // 2]
        print("\nera-stability:")
        print(f"  HR x team-over : {era(brec, 'hr', 'team_over_med', cut)}")
        print(f"  K  x total-over: {era(srec, 'k_over', 'total_over', cut)}")


if __name__ == "__main__":
    main()
