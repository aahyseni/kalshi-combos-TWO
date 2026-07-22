"""Measure the REMAINING reachable MLB gap pairs (OUTS/RBI/SB families) that
still fall to the flat +0.6 default or sit on unmeasured labeled priors after
the 2026-07-21 new-props tranche was wired.

ZERO-GAPS mandate (docs/sport_onboarding_playbook.md Stage 3/7): every reachable
same-game pair must be MEASURED-to-standard or exact-arithmetic — none may price
the flat default or a guessed prior. This tool closes the gap ledger for:

  FLAT-GAPS (absent from config -> price +0.60):
    outs x {hit, hr, tb, hrr, rbi}   (pitcher x facing/same-team batter)
    outs x sb                        (pitcher x facing/same-team baserunner)
    outs x outs                      (opposing starters, like ks|ks)
    sb x {hr, tb, hrr}               (distinct-player teammate/opponent)
    sb x ks                          (baserunner x facing/same-team starter)
    rbi x spread, sb x spread        (team-signed, oriented like ml x prop)
    rbi x rfi, sb x rfi              (rfi orientation-free plain scalar)
    rbi x rbi                        (distinct-player teammate/opponent)
  LABELED-PRIOR TIGHTEN (present but a guess -> replace with measurement):
    hit x rbi, hrr x rbi, tb x rbi   (distinct-player teammate/opponent)
    hit x sb, rbi x sb               (distinct-player teammate/opponent)
    ks x rbi                         (facing pitcher x batter)
    sb x sb:opp                      (opponent split; teammate already measured)

METHOD — mirrors the shipped tranche EXACTLY (rule 8, additive-only):
  Rank 1 raw joint frequencies over Retrosheet 2005-2025 (REUSE the parsed
  batter-game / starter-game caches data/history/newprops_cache/*.pkl.gz).
  Rank 2 invert the SHIPPED copula (implied_rho from calibrate_mlb_player_props,
  which calls combomaker.pricing.copula.gaussian_copula_joint_prob) to a drop-in
  pair_rho. CI99 = binomial SE on P(A&B) through the monotone solver (_Z99).
  Era split train 2005-19 vs holdout 2020-25. Cluster-floor CI (n=distinct
  team-games) on every team-context / combinatorial pair. Band =
  max(0.04, CI99 half-width, |era shift|). Min-n gate 50,000 for conditional
  cells; cells under the gate stay UNMEASURED with numbers shown.

  ORIENTATION (measured DIRECTLY on both sides, NEVER negated):
    :opp  = the batter/baserunner bats AGAINST that starter (facing case)
    :same = the batter/baserunner is on the starter's OWN team
    outs|outs = opposing starters. rbi|rbi / sb|batter = distinct-player
    teammate(:same)/opponent(:opp) over ordered player pairs (hr|hr style).
    spread x {rbi,sb} team-signed (ml x prop style, per-rung where rbi runged).
    rfi x {rbi,sb} orientation-free scalar.

  RUNG grammar: OUTS and RBI are rung-keyed (rung = that leg's Kalshi line int).
  When both legs are rung-keyed the suffixes chain in pair_key leg order.
  ks/total/moneyline/rfi/sb/spread(the batter side) never carry props rungs;
  spread carries its OWN margin rung. Measure per-rung ONLY where the ladder is
  non-flat (tested); otherwise ONE entry. NO interpolation/extrapolation ever.

ADDITIVE ONLY (CLAUDE.md rule 8): new file under tools/, imports the shipped
copula through calibrate_mlb_player_props and the LIVE legtypes.pair_key. Reads
the existing parse caches; touches no src/ or config or tests.

Run:
  .venv/Scripts/python.exe tools/calibrate_mlb_gap_pairs.py report 2005 2025
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from calibrate_mlb_new_props import RBI, SB, load_gl_full, parse_cached  # noqa: E402
from calibrate_mlb_player_props import _Z99, implied_rho  # noqa: E402

from combomaker.pricing.legtypes import LegType, pair_key  # noqa: E402

OUTS = "player_outs"
HISTORY = Path(__file__).resolve().parents[1] / "data" / "history"
RESULTS_JSON = HISTORY / "gap_pairs_results.json"

_MIN_N_COND = 50_000  # conditional-cell min-n gate (playbook 4d)


# --------------------------------------------------------------------------- #
# measurement primitives (mirror calibrate_mlb_new_props.measure_bools etc.)
# --------------------------------------------------------------------------- #
def measure_bools(pairs: list[tuple[bool, bool]]) -> dict:
    n = len(pairs)
    if n == 0:
        return {"n": 0}
    a = sum(1 for x, _ in pairs if x)
    b = sum(1 for _, y in pairs if y)
    ab = sum(1 for x, y in pairs if x and y)
    p_a, p_b, p_ab = a / n, b / n, ab / n
    rho = implied_rho(p_a, p_b, p_ab)
    se = math.sqrt(max(p_ab * (1 - p_ab), 1e-12) / n)
    lo = implied_rho(p_a, p_b, max(0.0, p_ab - _Z99 * se))
    hi = implied_rho(p_a, p_b, min(1.0, p_ab + _Z99 * se))
    return {"n": n, "n_a": a, "n_b": b, "n_ab": ab,
            "p_a": p_a, "p_b": p_b, "p_ab": p_ab, "rho": rho, "lo": lo, "hi": hi}


def era_rho(rows: list[tuple[int, bool, bool]], cut: int = 2020) -> tuple[float, float, int, int]:
    """(train_rho, holdout_rho, n_train, n_holdout) on (year, a, b) triples."""
    tr = [(a, b) for y, a, b in rows if y < cut]
    ho = [(a, b) for y, a, b in rows if y >= cut]
    rt = measure_bools(tr)
    rh = measure_bools(ho)
    return (rt.get("rho", float("nan")), rh.get("rho", float("nan")),
            rt.get("n", 0), rh.get("n", 0))


def cluster_ci(res: dict, n_clusters: int) -> tuple[float, float]:
    if res.get("n", 0) == 0 or n_clusters == 0:
        return float("nan"), float("nan")
    se = math.sqrt(max(res["p_ab"] * (1 - res["p_ab"]), 1e-12) / n_clusters)
    lo = implied_rho(res["p_a"], res["p_b"], max(0.0, res["p_ab"] - _Z99 * se))
    hi = implied_rho(res["p_a"], res["p_b"], min(1.0, res["p_ab"] + _Z99 * se))
    return lo, hi


def band(res: dict, era_shift: float, cluster_hw: float | None = None) -> float:
    """band = max(0.04, CI99 half-width, |era shift|); cluster hw when supplied."""
    if res.get("n", 0) == 0:
        return float("nan")
    naive_hw = (res["hi"] - res["lo"]) / 2.0
    hw = cluster_hw if cluster_hw is not None else naive_hw
    return max(0.04, hw, abs(era_shift) if not math.isnan(era_shift) else 0.0)


def stat_block(triples: list[tuple[int, bool, bool]], n_clusters: int | None = None) -> dict:
    """Full block from (year, a, b) triples: pooled rho, CI99, era split, band."""
    res = measure_bools([(a, b) for _, a, b in triples])
    if res.get("n", 0) == 0:
        return res
    tr, ho, ntr, nho = era_rho(triples)
    res["train_rho"], res["holdout_rho"] = tr, ho
    res["n_train"], res["n_holdout"] = ntr, nho
    era_shift = ho - tr if not (math.isnan(ho) or math.isnan(tr)) else float("nan")
    res["era_shift"] = era_shift
    res["oos_flag"] = not (res["lo"] <= ho <= res["hi"]) if not math.isnan(ho) else False
    cluster_hw = None
    if n_clusters is not None:
        lo_c, hi_c = cluster_ci(res, n_clusters)
        res["cluster_lo"], res["cluster_hi"] = lo_c, hi_c
        res["n_clusters"] = n_clusters
        cluster_hw = (hi_c - lo_c) / 2.0
    res["band"] = band(res, era_shift, cluster_hw)
    return res


# --------------------------------------------------------------------------- #
# combinatorial distinct-player pair measurement (teammate / opponent).
# Mirrors calibrate_mlb_new_props.team_pair_stats: expand over ordered distinct
# player pairs, cluster CI on distinct team-games (teammate) / games (opposing).
# --------------------------------------------------------------------------- #
def team_pair_block(rows: list[dict], stat_a: str, ka: int, stat_b: str, kb: int,
                    frame: str) -> dict:
    """P over ordered distinct-player pairs; frame='teammate'|'opposing'.

    teammate: both players on the same team-half (all ordered pairs within it).
    opposing: player on one team x player on the other (both orderings).
    Cluster count = distinct team-halves (teammate) / games (opposing).
    """
    tg: dict[tuple[str, bool], list[dict]] = defaultdict(list)
    for x in rows:
        tg[(x["game"], x["team_home"])].append(x)
    games: dict[str, dict[bool, list[dict]]] = defaultdict(dict)
    for (g, home), players in tg.items():
        games[g][home] = players

    pool = [0, 0, 0, 0]  # npairs, hits_a, hits_b, hits_ab
    tr = [0, 0, 0, 0]
    ho = [0, 0, 0, 0]
    n_cluster = 0
    for sides in games.values():
        if frame == "teammate":
            for players in sides.values():
                kcnt = len(players)
                if kcnt < 2:
                    continue
                a = sum(1 for p in players if p[stat_a] >= ka)
                b = sum(1 for p in players if p[stat_b] >= kb)
                ab = sum(1 for p in players if p[stat_a] >= ka and p[stat_b] >= kb)
                npairs = kcnt * (kcnt - 1)
                hits_a = a * (kcnt - 1)
                hits_b = b * (kcnt - 1)
                hits_ab = a * b - ab  # ordered distinct pairs where A holds i, B holds j!=i
                yr = players[0]["year"]
                for s in (pool, tr if yr < 2020 else ho):
                    s[0] += npairs
                    s[1] += hits_a
                    s[2] += hits_b
                    s[3] += hits_ab
                n_cluster += 1
        else:  # opposing
            if len(sides) != 2:
                continue
            p1 = sides.get(True, [])
            p2 = sides.get(False, [])
            if not p1 or not p2:
                continue
            a1 = sum(1 for p in p1 if p[stat_a] >= ka)
            b2 = sum(1 for p in p2 if p[stat_b] >= kb)
            a2 = sum(1 for p in p2 if p[stat_a] >= ka)
            b1 = sum(1 for p in p1 if p[stat_b] >= kb)
            npairs = 2 * len(p1) * len(p2)
            hits_a = a1 * len(p2) + a2 * len(p1)
            hits_b = b2 * len(p1) + b1 * len(p2)
            hits_ab = a1 * b2 + a2 * b1
            yr = p1[0]["year"]
            for s in (pool, tr if yr < 2020 else ho):
                s[0] += npairs
                s[1] += hits_a
                s[2] += hits_b
                s[3] += hits_ab
            n_cluster += 1

    def block(s: list[int]) -> dict:
        n, ha, hb, hab = s
        if n == 0:
            return {"n": 0}
        p_a, p_b, p_ab = ha / n, hb / n, hab / n
        rho = implied_rho(p_a, p_b, p_ab)
        se = math.sqrt(max(p_ab * (1 - p_ab), 1e-12) / max(n_cluster, 1))
        return {"n": n, "p_a": p_a, "p_b": p_b, "p_ab": p_ab, "rho": rho,
                "lo": implied_rho(p_a, p_b, max(0.0, p_ab - _Z99 * se)),
                "hi": implied_rho(p_a, p_b, min(1.0, p_ab + _Z99 * se)),
                "n_cluster": n_cluster}

    res = block(pool)
    if res.get("n", 0) == 0:
        return res
    rtr, rho_ho = block(tr).get("rho", float("nan")), block(ho).get("rho", float("nan"))
    res["train_rho"], res["holdout_rho"] = rtr, rho_ho
    era_shift = rho_ho - rtr if not (math.isnan(rho_ho) or math.isnan(rtr)) else float("nan")
    res["era_shift"] = era_shift
    res["oos_flag"] = not (res["lo"] <= rho_ho <= res["hi"]) if not math.isnan(rho_ho) else False
    res["band"] = band(res, era_shift)  # cluster CI already baked into lo/hi
    return res


# --------------------------------------------------------------------------- #
# pitcher x batter join (facing :opp / same-team :same).
# For each game, batters on side S face the starter on side (1-S).
# --------------------------------------------------------------------------- #
def build_pitcher_batter_rows(batter_games: list[dict], starter_games: list[dict],
                              require_outs_ok: bool, require_rbi_ok: bool,
                              require_sb_ok: bool) -> tuple[list[dict], list[dict]]:
    """Return (facing_rows, same_rows): each row has the batter fields + the
    joined starter's ks/outs + year + game + a cluster id.

    facing: batter side != starter side (batter bats against that starter).
    same:   batter side == starter side (teammates).
    Only games where BOTH the batter row's stat and the starter's outs
    reconcile are used (per-stat selection, playbook conservative frame)."""
    # index starters by (game, side)  — side True=home, False=away
    st_by_game_side: dict[tuple[str, bool], dict] = {}
    for s in starter_games:
        if require_outs_ok and not s["outs_ok"]:
            continue
        st_by_game_side[(s["game"], s["team_home"])] = s
    facing: list[dict] = []
    same: list[dict] = []
    for b in batter_games:
        if require_rbi_ok and not b["rbi_ok"]:
            continue
        if require_sb_ok and not b["sb_ok"]:
            continue
        g = b["game"]
        opp = st_by_game_side.get((g, not b["team_home"]))
        mate = st_by_game_side.get((g, b["team_home"]))
        if opp is not None:
            facing.append({**b, "s_ks": opp["ks"], "s_outs": opp["outs"]})
        if mate is not None:
            same.append({**b, "s_ks": mate["ks"], "s_outs": mate["outs"]})
    return facing, same


# --------------------------------------------------------------------------- #
def fmt(name: str, res: dict, extra: str = "") -> str:
    if res.get("n", 0) == 0:
        return f"{name:44s}  (no data) {extra}"
    es = res.get("era_shift", float("nan"))
    flag = " OOS-FLAG" if res.get("oos_flag") else ""
    band_s = f" band={res['band']:.3f}" if "band" in res else ""
    nc = f" nclus={res['n_clusters']}" if "n_clusters" in res else (
        f" nclus={res['n_cluster']}" if "n_cluster" in res else "")
    return (f"{name:44s} n={res['n']:>10} P(A)={res.get('p_a', 0):.4f} "
            f"P(B)={res.get('p_b', 0):.4f} P(AB)={res.get('p_ab', 0):.4f} "
            f"rho={res['rho']:+.4f} CI99=[{res['lo']:+.4f},{res['hi']:+.4f}]"
            f"{nc} era({res.get('train_rho', float('nan')):+.3f}->"
            f"{res.get('holdout_rho', float('nan')):+.3f} d={es:+.3f})"
            f"{band_s}{flag} {extra}")


def build_report(years: list[int]) -> None:
    gl = load_gl_full(years)
    print(f"game-log rows loaded: {len(gl)}", file=sys.stderr)
    all_b: list[dict] = []
    all_s: list[dict] = []
    for y in years:
        d = parse_cached(y, gl)
        all_b += d["batter_games"]
        all_s += d["starter_games"]
        print(f"  {y}: batters={len(d['batter_games'])} starters={len(d['starter_games'])}",
              file=sys.stderr)

    results: dict[str, dict] = {}

    def rec(key: str, res: dict, note: str = "") -> None:
        clean = {k: v for k, v in res.items()}
        if note:
            clean["note"] = note
        results[key] = clean

    # reconciled subsets
    B_rbi = [r for r in all_b if r["rbi_ok"]]
    B_sb = [r for r in all_b if r["sb_ok"]]
    S_ok = [r for r in all_s if r["outs_ok"]]
    print(f"\nbatter rows {len(all_b)} | rbi_ok {len(B_rbi)} | sb_ok {len(B_sb)} | "
          f"starter rows {len(all_s)} | outs_ok {len(S_ok)}")

    # self-season-median OUTS line (>=5 starts, the KS/OUTS convention). Game
    # totals are not needed here — none of these gap pairs pair against TOTAL
    # (those were measured in the prior new-props tranche).
    by_py: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for r in all_s:
        by_py[(r["pid"], r["year"])].append(r)
    o_med: dict[tuple[str, int], float] = {}
    for kk, rows_ in by_py.items():
        if len(rows_) >= 5:
            o_med[kk] = sorted([r["outs"] for r in rows_])[len(rows_) // 2]

    # =================================================================== #
    print("\n================ OUTS x BATTER (pitcher x facing/same batter) ================")
    # facing = batter bats against the starter (:opp); same = teammate (:same).
    fac_rbi, sam_rbi = build_pitcher_batter_rows(B_rbi, all_s, True, True, False)
    fac_sb, sam_sb = build_pitcher_batter_rows(B_sb, all_s, True, False, True)
    print(f"joined facing(rbi_ok&outs_ok) rows: {len(fac_rbi)} | same: {len(sam_rbi)}")
    print(f"joined facing(sb_ok&outs_ok)  rows: {len(fac_sb)}  | same: {len(sam_sb)}")

    # attach o_over (starter outs over own season median) using a
    # (game, side) -> starter (pid, year) index. The joined row carries s_outs;
    # the median lookup needs the starter identity, rebuilt here.
    st_pid: dict[tuple[str, bool], tuple[str, int]] = {}
    for s in all_s:
        if s["outs_ok"]:
            st_pid[(s["game"], s["team_home"])] = (s["pid"], s["year"])

    def attach_o_over(rows: list[dict], facing: bool) -> list[dict]:
        out = []
        for r in rows:
            side = (not r["team_home"]) if facing else r["team_home"]
            key = st_pid.get((r["game"], side))
            if key is None or key not in o_med:
                continue
            med = o_med[key]
            if r["s_outs"] == med:
                continue  # median-tie excluded (KS convention)
            rr = dict(r)
            rr["o_over"] = r["s_outs"] > med
            out.append(rr)
        return out

    fac_rbi_o = attach_o_over(fac_rbi, True)
    sam_rbi_o = attach_o_over(sam_rbi, True)  # facing flag irrelevant here (same side)
    sam_rbi_o = attach_o_over(sam_rbi, False)
    fac_sb_o = attach_o_over(fac_sb, True)
    sam_sb_o = attach_o_over(sam_sb, False)

    # outs x {hit,hr,tb,hrr,rbi,sb} — facing(:opp) and same(:same)
    batter_stats = [("hit", "hit", 1), ("hr", "hr", 1), ("tb", "tb", 2),
                    ("hrr", "hrr", 2), ("rbi", "rbi", 1)]
    for lbl, stat, k in batter_stats:
        src_f = fac_rbi_o  # rbi_ok frame carries all of hit/hr/tb/hrr/rbi
        src_s = sam_rbi_o
        rf = stat_block([(r["year"], r["o_over"], r[stat] >= k) for r in src_f])
        print(fmt(f"outs>med x {lbl}>={k} :opp (FACING)", rf))
        rec(f"outs|{lbl}:opp", rf)
        rs = stat_block([(r["year"], r["o_over"], r[stat] >= k) for r in src_s])
        print(fmt(f"outs>med x {lbl}>={k} :same (TEAMMATE)", rs))
        rec(f"outs|{lbl}:same", rs)
    # outs x sb (sb_ok frame)
    rf = stat_block([(r["year"], r["o_over"], r["sb"] >= 1) for r in fac_sb_o])
    print(fmt("outs>med x sb>=1 :opp (FACING)", rf))
    rec("outs|sb:opp", rf)
    rs = stat_block([(r["year"], r["o_over"], r["sb"] >= 1) for r in sam_sb_o])
    print(fmt("outs>med x sb>=1 :same (TEAMMATE)", rs))
    rec("outs|sb:same", rs)

    # ---- outs x rbi PER-RUNG (rbi 1+/2+/3+) to test ladder flatness ----
    print("\n---- outs x rbi per-rung (ladder flatness test) ----")
    for k in (1, 2, 3):
        rf = stat_block([(r["year"], r["o_over"], r["rbi"] >= k) for r in fac_rbi_o])
        print(fmt(f"outs>med x rbi>={k} :opp:r{k} (FACING)", rf))
        rec(f"outs|rbi:opp:r{k}", rf)
    for k in (1, 2, 3):
        rs = stat_block([(r["year"], r["o_over"], r["rbi"] >= k) for r in sam_rbi_o])
        print(fmt(f"outs>med x rbi>={k} :same:r{k} (TEAMMATE)", rs))
        rec(f"outs|rbi:same:r{k}", rs)

    # =================================================================== #
    print("\n================ OUTS x OUTS (opposing starters) ================")
    by_game_s: dict[str, list[dict]] = defaultdict(list)
    for s in S_ok:
        by_game_s[s["game"]].append(s)
    # attach o_over to starters
    s_over: list[dict] = []
    for s in S_ok:
        key = (s["pid"], s["year"])
        if key not in o_med or s["outs"] == o_med[key]:
            continue
        ss = dict(s)
        ss["o_over"] = s["outs"] > o_med[key]
        s_over.append(ss)
    sog: dict[str, list[dict]] = defaultdict(list)
    for s in s_over:
        sog[s["game"]].append(s)
    opp_triples = []
    for rows_ in sog.values():
        if len(rows_) == 2:
            a, b = rows_
            opp_triples.append((a["year"], a["o_over"], b["o_over"]))
            opp_triples.append((a["year"], b["o_over"], a["o_over"]))
    r = stat_block(opp_triples, n_clusters=len(sog))
    print(fmt("outs>med x outs>med (OPPOSING starters)", r))
    rec("outs|outs", r)

    # =================================================================== #
    print("\n================ SB x BATTER (distinct-player teammate/opp) ================")
    for lbl, stat, k in [("hr", "hr", 1), ("tb", "tb", 2), ("hrr", "hrr", 2)]:
        rt = team_pair_block(B_sb, "sb", 1, stat, k, "teammate")
        print(fmt(f"sb>=1 x {lbl}>={k} :same (TEAMMATE)", rt, "(cluster CI)"))
        rec(f"sb|{lbl}:same", rt)
        ro = team_pair_block(B_sb, "sb", 1, stat, k, "opposing")
        print(fmt(f"sb>=1 x {lbl}>={k} :opp (OPPOSING)", ro, "(cluster CI)"))
        rec(f"sb|{lbl}:opp", ro)

    # =================================================================== #
    print("\n================ SB x KS (baserunner x facing/same starter) ================")
    # reuse pitcher-batter join on sb frame — s_ks is attached.
    rf = stat_block([(r["year"], r["sb"] >= 1, r["s_ks"] >= 1) for r in fac_sb])
    # ks over? use starter self-median for consistency with the ks convention.
    # Simpler robust proxy matching prior tranche: ks over own season median.
    k_med: dict[tuple[str, int], float] = {}
    for kk, rows_ in by_py.items():
        if len(rows_) >= 5:
            k_med[kk] = sorted([x["ks"] for x in rows_])[len(rows_) // 2]

    def attach_ks_over(rows: list[dict], facing: bool) -> list[dict]:
        out = []
        for r in rows:
            side = (not r["team_home"]) if facing else r["team_home"]
            key = st_pid.get((r["game"], side))
            if key is None or key not in k_med:
                continue
            med = k_med[key]
            if r["s_ks"] == med:
                continue
            rr = dict(r)
            rr["ks_over"] = r["s_ks"] > med
            out.append(rr)
        return out

    fac_sb_k = attach_ks_over(fac_sb, True)
    sam_sb_k = attach_ks_over(sam_sb, False)
    rf = stat_block([(r["year"], r["sb"] >= 1, r["ks_over"]) for r in fac_sb_k])
    print(fmt("sb>=1 x ks>med :opp (FACING)", rf))
    rec("sb|ks:opp", rf)
    rs = stat_block([(r["year"], r["sb"] >= 1, r["ks_over"]) for r in sam_sb_k])
    print(fmt("sb>=1 x ks>med :same (TEAMMATE)", rs))
    rec("sb|ks:same", rs)

    # =================================================================== #
    print("\n================ RBI/SB x SPREAD (team-signed, oriented) ================")
    # :same = the batter's team covers the margin (margin >= N). :opp = the
    # opponent covers (margin <= -N). Per spread rung r2..r5 (like ml x prop,
    # and rbi is rung-keyed so chain rbi rung x spread rung).
    n_tg_rbi = len({(r["game"], r["team_home"]) for r in B_rbi})
    n_tg_sb = len({(r["game"], r["team_home"]) for r in B_sb})
    for krbi in (1, 2, 3):
        for nspr in (2, 3, 4, 5):
            rs = stat_block([(r["year"], r["rbi"] >= krbi, r["margin"] >= nspr)
                             for r in B_rbi], n_clusters=n_tg_rbi)
            rec(f"rbi|spread:same:r{krbi}:r{nspr}", rs)
            ro = stat_block([(r["year"], r["rbi"] >= krbi, r["margin"] <= -nspr)
                             for r in B_rbi], n_clusters=n_tg_rbi)
            rec(f"rbi|spread:opp:r{krbi}:r{nspr}", ro)
    # print a representative slice (r1 rung ladder both sides)
    for nspr in (2, 3, 4, 5):
        print(fmt(f"rbi>=1 x spread(same) margin>={nspr}  :same:r1:r{nspr}",
                  results[f"rbi|spread:same:r1:r{nspr}"]))
    for nspr in (2, 3, 4, 5):
        print(fmt(f"rbi>=1 x spread(opp)  margin<=-{nspr} :opp:r1:r{nspr}",
                  results[f"rbi|spread:opp:r1:r{nspr}"]))
    # rbi 2+/3+ representative (spread r2 anchor)
    for krbi in (2, 3):
        print(fmt(f"rbi>={krbi} x spread(same) m>=2 :same:r{krbi}:r2",
                  results[f"rbi|spread:same:r{krbi}:r2"]))
        print(fmt(f"rbi>={krbi} x spread(opp)  m<=-2 :opp:r{krbi}:r2",
                  results[f"rbi|spread:opp:r{krbi}:r2"]))
    # sb x spread (1+ only), per spread rung
    for nspr in (2, 3, 4, 5):
        rs = stat_block([(r["year"], r["sb"] >= 1, r["margin"] >= nspr)
                         for r in B_sb], n_clusters=n_tg_sb)
        print(fmt(f"sb>=1 x spread(same) m>={nspr} :same:r{nspr}", rs))
        rec(f"sb|spread:same:r{nspr}", rs)
        ro = stat_block([(r["year"], r["sb"] >= 1, r["margin"] <= -nspr)
                         for r in B_sb], n_clusters=n_tg_sb)
        print(fmt(f"sb>=1 x spread(opp)  m<=-{nspr} :opp:r{nspr}", ro))
        rec(f"sb|spread:opp:r{nspr}", ro)

    # =================================================================== #
    print("\n================ RBI/SB x RFI (orientation-free scalar) ================")
    for k in (1, 2, 3):
        r = stat_block([(x["year"], x["rbi"] >= k, x["rfi"]) for x in B_rbi],
                       n_clusters=n_tg_rbi)
        print(fmt(f"rbi>={k} x rfi :r{k}", r))
        rec(f"rbi|rfi:r{k}", r)
    r = stat_block([(x["year"], x["sb"] >= 1, x["rfi"]) for x in B_sb], n_clusters=n_tg_sb)
    print(fmt("sb>=1 x rfi", r))
    rec("sb|rfi", r)

    # =================================================================== #
    print("\n================ RBI x RBI (distinct-player teammate/opp) ================")
    for k in (1, 2, 3):
        rt = team_pair_block(B_rbi, "rbi", k, "rbi", k, "teammate")
        print(fmt(f"rbi>={k} x rbi>={k} :same (TEAMMATE)", rt, "(cluster CI)"))
        rec(f"rbi|rbi:same:r{k}", rt)
        ro = team_pair_block(B_rbi, "rbi", k, "rbi", k, "opposing")
        print(fmt(f"rbi>={k} x rbi>={k} :opp (OPPOSING)", ro, "(cluster CI)"))
        rec(f"rbi|rbi:opp:r{k}", ro)

    # =================================================================== #
    print("\n================ LABELED-PRIOR TIGHTEN: distinct-player cross-family ==========")
    # hit x rbi, hrr x rbi, tb x rbi, hit x sb, rbi x sb — teammate/opponent.
    for lbl, sa, ka, sb, kb, rows_, tag in [
        ("hit x rbi", "hit", 1, "rbi", 1, B_rbi, "hit|rbi"),
        ("hrr x rbi", "hrr", 2, "rbi", 1, B_rbi, "hrr|rbi"),
        ("tb x rbi", "tb", 2, "rbi", 1, B_rbi, "rbi|tb"),
        ("hit x sb", "hit", 1, "sb", 1, B_sb, "hit|sb"),
        ("rbi x sb", "rbi", 1, "sb", 1, B_sb, "rbi|sb"),
    ]:
        rt = team_pair_block(rows_, sa, ka, sb, kb, "teammate")
        print(fmt(f"{lbl} :same (TEAMMATE)", rt, "(cluster CI)"))
        rec(f"{tag}:same", rt)
        ro = team_pair_block(rows_, sa, ka, sb, kb, "opposing")
        print(fmt(f"{lbl} :opp (OPPOSING)", ro, "(cluster CI)"))
        rec(f"{tag}:opp", ro)

    # =================================================================== #
    print("\n================ KS x RBI (facing pitcher x batter) ================")
    # ks is a starter stat; facing = batter faces the starter (:opp), same-team
    # (:same). Reuse the pitcher-batter join on rbi frame with attached s_ks.
    fac_rbi_k = attach_ks_over(fac_rbi, True)
    sam_rbi_k = attach_ks_over(sam_rbi, False)
    for k in (1, 2, 3):
        rf = stat_block([(r["year"], r["ks_over"], r["rbi"] >= k) for r in fac_rbi_k])
        print(fmt(f"ks>med x rbi>={k} :opp:r{k} (FACING)", rf))
        rec(f"ks|rbi:opp:r{k}", rf)
    rs = stat_block([(r["year"], r["ks_over"], r["rbi"] >= 1) for r in sam_rbi_k])
    print(fmt("ks>med x rbi>=1 :same (TEAMMATE)", rs))
    rec("ks|rbi:same", rs)

    # =================================================================== #
    print("\n================ SB x SB :opp (opponent split) ================")
    ro = team_pair_block(B_sb, "sb", 1, "sb", 1, "opposing")
    print(fmt("sb>=1 x sb>=1 :opp (OPPOSING)", ro, "(cluster CI)"))
    rec("sb|sb:opp", ro)

    # =================================================================== #
    print("\n================ STAGED KEYS (EXECUTED via live legtypes.pair_key) ==========")
    L = LegType
    keys = {
        "outs|hit": pair_key(OUTS, L.PLAYER_HIT),  # type: ignore[arg-type]
        "outs|hr": pair_key(OUTS, L.PLAYER_HR),  # type: ignore[arg-type]
        "outs|tb": pair_key(OUTS, L.PLAYER_TB),  # type: ignore[arg-type]
        "outs|hrr": pair_key(OUTS, L.PLAYER_HRR),  # type: ignore[arg-type]
        "outs|rbi": pair_key(OUTS, RBI),  # type: ignore[arg-type]
        "outs|sb": pair_key(OUTS, SB),  # type: ignore[arg-type]
        "outs|outs": pair_key(OUTS, OUTS),  # type: ignore[arg-type]
        "sb|hr": pair_key(SB, L.PLAYER_HR),  # type: ignore[arg-type]
        "sb|tb": pair_key(SB, L.PLAYER_TB),  # type: ignore[arg-type]
        "sb|hrr": pair_key(SB, L.PLAYER_HRR),  # type: ignore[arg-type]
        "sb|ks": pair_key(SB, L.PLAYER_KS),  # type: ignore[arg-type]
        "rbi|spread": pair_key(RBI, L.SPREAD),  # type: ignore[arg-type]
        "sb|spread": pair_key(SB, L.SPREAD),  # type: ignore[arg-type]
        "rbi|rfi": pair_key(RBI, L.RFI),  # type: ignore[arg-type]
        "sb|rfi": pair_key(SB, L.RFI),  # type: ignore[arg-type]
        "rbi|rbi": pair_key(RBI, RBI),  # type: ignore[arg-type]
        "hit|rbi": pair_key(L.PLAYER_HIT, RBI),  # type: ignore[arg-type]
        "hrr|rbi": pair_key(L.PLAYER_HRR, RBI),  # type: ignore[arg-type]
        "tb|rbi": pair_key(L.PLAYER_TB, RBI),  # type: ignore[arg-type]
        "hit|sb": pair_key(L.PLAYER_HIT, SB),  # type: ignore[arg-type]
        "rbi|sb": pair_key(RBI, SB),  # type: ignore[arg-type]
        "ks|rbi": pair_key(L.PLAYER_KS, RBI),  # type: ignore[arg-type]
        "sb|sb": pair_key(SB, SB),  # type: ignore[arg-type]
    }
    for lbl, kv in sorted(keys.items()):
        print(f"  {lbl:14s} -> {kv}")

    out = {"years": years, "min_n_cond": _MIN_N_COND, "pair_keys": keys,
           "results": {k: {kk: vv for kk, vv in v.items()} for k, v in results.items()}}
    RESULTS_JSON.write_text(json.dumps(out, indent=1, default=str))
    print(f"\nresults JSON -> {RESULTS_JSON}")


def main() -> None:
    args = sys.argv[1:]
    yrs = [int(x) for x in args[1:]] if args and args[0] == "report" else [int(x) for x in args]
    if len(yrs) == 2 and yrs[1] > yrs[0] + 1:
        years = list(range(yrs[0], yrs[1] + 1))
    elif yrs:
        years = yrs
    else:
        years = list(range(2005, 2026))
    build_report(years)


if __name__ == "__main__":
    main()
