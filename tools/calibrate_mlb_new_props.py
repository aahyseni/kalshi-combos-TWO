"""Calibrate the three newly combo-eligible MLB prop families from Retrosheet.

Families: KXMLBOUTS (starter outs recorded), KXMLBRBI (batter RBI 1+/2+/3+),
KXMLBSB (batter stolen bases 1+). Method EXACTLY mirrors the shipped pipeline
(results_baseball.md / 2026-07-09 measurement tranche / phase2_wire_list.txt):

  Rank 1: raw joint frequencies P(A), P(B), P(A&B) over the Retrosheet corpus.
  Rank 2: invert the SHIPPED copula (combomaker.pricing.copula, via the
          existing tool's implied_rho) by bisection to a drop-in pair_rho;
          99% CI = binomial SE on P(A&B) pushed through the monotone solver.
  Frames: all-PA batter-game frame (the existing ml|prop anchor convention)
          with a STARTERS(lineup) variant to quantify the Kalshi settlement
          frame gap; self-season-median line for OUTS (the KS convention) plus
          a fixed-rung ladder for flatness; same-player cross-family measured
          as conditional cells (conditionals_mlb.py format — NEVER a rho);
          teammate/opponent = ':same'/':opp' per shipped sgp.py semantics.
  OOS:    era split train 2005-2019 vs holdout 2020-2025 (the split the
          existing MLB tools use); flag drift beyond the pooled 99% CI.

ADDITIVE ONLY (CLAUDE.md rule 8): new file under tools/, imports the shipped
copula through tools/calibrate_mlb_player_props.py and the LIVE
legtypes.pair_key for staged key generation. Touches no src/ or config.

Run:
  .venv/Scripts/python.exe tools/calibrate_mlb_new_props.py selftest
  .venv/Scripts/python.exe tools/calibrate_mlb_new_props.py parse 2005 2025
  .venv/Scripts/python.exe tools/calibrate_mlb_new_props.py report 2005 2025
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import math
import pickle
import re
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

# reuse the existing parser module's machinery (shipped-copula inversion, CI,
# event-zip cache) — never reimplemented.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from calibrate_mlb_player_props import (  # noqa: E402
    _Z99,
    event_zip,
    implied_rho,
)
from combomaker.pricing.legtypes import LegType, pair_key  # noqa: E402

HISTORY = Path(__file__).resolve().parents[1] / "data" / "history"
CACHE_DIR = HISTORY / "newprops_cache"
RESULTS_JSON = HISTORY / "newprops_results.json"

# staged LegType string values (naming convention of the shipped siblings —
# PLAYER_HR = "player_hr" etc.; families named in staged_mlb_props.md).
OUTS = "player_outs"
RBI = "player_rbi"
SB = "player_sb"

_NON_PA_PREFIXES = ("SB", "CS", "PO", "WP", "PB", "BK", "DI", "OA", "NP", "FLE")
_BASERUNNING_BASIC = ("SB", "CS", "POCS", "PO", "WP", "PB", "BK", "DI", "OA", "FLE")

_ADV_RE = re.compile(r"^([B123])([-X])([123H])((?:\([^)]*\))*)$")
_GRP_RE = re.compile(r"\(([^)]*)\)")
_CHUNK_RE = re.compile(r"([0-9E]+)(?:\(([B123])\))?")
_ERR_RE = re.compile(r"E\d|^E")


# --------------------------------------------------------------------------- #
# event-string parsing
# --------------------------------------------------------------------------- #
def split_event(ev: str) -> tuple[str, list[str], str]:
    """event -> (basic, modifiers, advance-string)."""
    ev = ev.replace("#", "").replace("!", "").replace("?", "")
    if "." in ev:
        main, adv = ev.split(".", 1)
    else:
        main, adv = ev, ""
    parts = main.split("/")
    return parts[0], parts[1:], adv


def parse_advances(adv: str) -> list[tuple[str, str, bool, list[str]]]:
    """advance-string -> [(runner, dest, is_out, paren-groups)]."""
    res = []
    for tok in adv.split(";"):
        tok = tok.strip()
        if not tok:
            continue
        m = _ADV_RE.match(tok)
        if not m:
            continue
        runner, op, dest, grptext = m.groups()
        groups = _GRP_RE.findall(grptext)
        is_out = op == "X"
        if is_out and any(_ERR_RE.search(g) for g in groups):
            is_out = False  # error negates the out -> runner safe at dest
        res.append((runner, dest, is_out, groups))
    return res


def _parse_baserunning_part(part: str, out: dict) -> None:
    """SB2 / CS2(26) / PO1(13) / POCS2(1361) micro-events (may be ';'-chained)."""
    for piece in part.split(";"):
        piece = piece.strip()
        if not piece:
            continue
        if piece.startswith("SB"):
            base = piece[2:3]  # '2','3','H'
            if base in ("2", "3", "H"):
                out["sb"].append(base)
        elif piece.startswith("POCS"):
            base = piece[4:5]
            grps = _GRP_RE.findall(piece)
            safe = any(_ERR_RE.search(g) for g in grps)
            if base in ("2", "3", "H"):
                out["cs"].append((base, not safe))
        elif piece.startswith("CS"):
            base = piece[2:3]
            grps = _GRP_RE.findall(piece)
            safe = any(_ERR_RE.search(g) for g in grps)
            if base in ("2", "3", "H"):
                out["cs"].append((base, not safe))
        elif piece.startswith("PO"):
            base = piece[2:3]
            grps = _GRP_RE.findall(piece)
            safe = any(_ERR_RE.search(g) for g in grps)
            if base in ("1", "2", "3"):
                out["po"].append((base, not safe))


def parse_basic(basic: str) -> dict:
    """Basic-play token -> structured outcome (batter fate, outs, SB/CS/PO)."""
    out: dict = {
        "pa": not (basic == "" or basic.startswith(_NON_PA_PREFIXES)),
        "k": False,
        "hit": 0,  # 1=S 2=D 3=T 4=HR
        "batter_out": False,
        "batter_dest": None,  # implicit destination if safe
        "chunk_outs": [],  # runner ids put out in the basic numeric play
        "sb": [],
        "cs": [],
        "po": [],
        "baserunning_primary": basic.startswith(_BASERUNNING_BASIC),
        "secondary_baserunning": False,
        "error_play": False,
    }
    if basic == "" or basic == "NP" or basic == "99":
        out["pa"] = False
        return out

    primary, secondary = (basic.split("+", 1) + [None])[:2] if "+" in basic else (basic, None)

    # ---- primary ----
    if primary.startswith(_BASERUNNING_BASIC):
        _parse_baserunning_part(primary, out)
    elif primary[0].isdigit():
        chunks = _CHUNK_RE.findall(primary)
        chunks = [c for c in chunks if c[0]]
        got_batter = False
        for i, (fld, r) in enumerate(chunks):
            if "E" in fld:
                out["error_play"] = True
                out["batter_dest"] = "1"
                got_batter = True
                continue
            if r == "B":
                out["batter_out"] = True
                got_batter = True
            elif r:
                out["chunk_outs"].append(r)
            elif i == len(chunks) - 1 and not got_batter:
                out["batter_out"] = True
                got_batter = True
        if not got_batter and not out["batter_out"]:
            out["batter_dest"] = "1"  # e.g. '54(1)' force out, batter safe
    elif primary.startswith("K"):
        out["k"] = True
        out["batter_out"] = True  # negated if advances show B safe
    elif primary.startswith("HP"):
        out["batter_dest"] = "1"
    elif primary.startswith("HR") or primary == "H":
        out["hit"] = 4
        out["batter_dest"] = "H"
    elif primary.startswith("W") or primary.startswith("IW") or primary == "I":
        out["batter_dest"] = "1"
    elif primary.startswith("E"):
        out["error_play"] = True
        out["batter_dest"] = "1"
    elif primary.startswith("FC"):
        out["batter_dest"] = "1"
    elif primary.startswith("DGR") or primary.startswith("D"):
        out["hit"] = 2
        out["batter_dest"] = "2"
    elif primary.startswith("T"):
        out["hit"] = 3
        out["batter_dest"] = "3"
    elif primary.startswith("S"):
        out["hit"] = 1
        out["batter_dest"] = "1"
    elif primary == "C":
        out["batter_dest"] = "1"
    # ---- secondary (K+SB2, W+WP, ...) ----
    if secondary:
        if secondary.startswith(("SB", "CS", "PO")):
            _parse_baserunning_part(secondary, out)
            out["secondary_baserunning"] = True
        elif secondary.startswith(("WP", "PB", "DI", "OA", "BK")):
            out["secondary_baserunning"] = True
        elif secondary.startswith("E"):
            out["error_play"] = True
    return out


# --------------------------------------------------------------------------- #
# game-log loading (official team totals for validation + scores)
# --------------------------------------------------------------------------- #
def load_gl_full(years: list[int]) -> dict[str, dict]:
    """key = home+date+gamenum -> official per-game fields (0-based GL indices)."""
    lookup: dict[str, dict] = {}
    for year in years:
        gl = HISTORY / f"gl{year}.txt"
        if not gl.exists():
            print(f"  WARN missing game log {gl.name}", file=sys.stderr)
            continue
        with open(gl, encoding="latin-1", newline="") as f:
            for x in csv.reader(f):
                try:
                    rec = {
                        "vis": x[3], "home": x[6],
                        "vs": int(x[9]), "hs": int(x[10]),
                        "outs_len": int(x[11]) if x[11] else None,
                        "vis_hr": int(x[25]), "vis_rbi": int(x[26]),
                        "vis_sb": int(x[33]),
                        "vis_po": int(x[43]),
                        "home_hr": int(x[53]), "home_rbi": int(x[54]),
                        "home_sb": int(x[61]),
                        "home_po": int(x[71]),
                    }
                except (IndexError, ValueError):
                    continue
                lookup[f"{x[6]}{x[0]}{x[1]}"] = rec
    return lookup


def load_rosters(years: list[int]) -> dict[str, str]:
    """player id -> 'First Last' from the .ROS files inside the event zips."""
    names: dict[str, str] = {}
    for year in years:
        zf = zipfile.ZipFile(event_zip(year))
        for name in zf.namelist():
            if not name.upper().endswith(".ROS"):
                continue
            for line in zf.read(name).decode("latin-1").splitlines():
                p = line.split(",")
                if len(p) >= 3:
                    names.setdefault(p[0], f"{p[2]} {p[1]}")
    return names


# --------------------------------------------------------------------------- #
# full-game event parse (batters: HIT/HR/TB/RBI/SB/R; starters: K/outs; RFI)
# --------------------------------------------------------------------------- #
def parse_year_full(year: int, gl: dict[str, dict]) -> dict:
    """Parse one season -> dict with batter_games, starter_games, game_rows, stats."""
    stats: dict[str, int] = defaultdict(int)
    batter_games: list[dict] = []
    starter_games: list[dict] = []
    game_rows: list[dict] = []

    zf = zipfile.ZipFile(event_zip(year))
    for name in zf.namelist():
        if not (name.endswith(".EVA") or name.endswith(".EVN")):
            continue
        text = zf.read(name).decode("latin-1")

        cur_id: str | None = None
        starters = {"0": None, "1": None}          # starting pitchers by side
        cur_pitcher = {"0": None, "1": None}
        lineup: dict[str, dict[str, str]] = {"0": {}, "1": {}}  # side -> order -> pid
        start_pids: set[tuple[str, str]] = set()   # (pid, side) in starting lineup
        bases: dict[str, str | None] = {"1": None, "2": None, "3": None}
        cur_half: tuple[int, str] | None = None
        half_outs: dict[tuple[int, str], int] = {}
        runs_by_inning_side: dict[tuple[int, str], int] = defaultdict(int)
        bat: dict[tuple[str, str], dict] = {}      # (pid, side) -> batter accum
        pit: dict[str, dict] = {}                  # pid -> pitcher accum
        sb_no_owner = 0
        pending_radj: list[tuple[str, str]] = []

        def bat_rec(pid: str, side: str) -> dict:
            key = (pid, side)
            if key not in bat:
                bat[key] = {"pa": 0, "hit": 0, "hr": 0, "tb": 0,
                            "rbi": 0, "sb": 0, "r": 0}
            return bat[key]

        def pit_rec(pid: str) -> dict:
            if pid not in pit:
                pit[pid] = {"k": 0, "outs": 0}
            return pit[pid]

        def flush() -> None:
            nonlocal cur_id, sb_no_owner
            if cur_id is None:
                return
            stats["games_seen"] += 1
            rec = gl.get(cur_id)
            if rec is None:
                stats["games_unjoined"] += 1
                return
            vs, hs = rec["vs"], rec["hs"]
            if vs == hs:
                stats["games_tie"] += 1
                return
            stats["games_joined"] += 1
            home_won = hs > vs

            # ---- validation vs official game log ----
            p_runs = {"0": sum(v for (i, s), v in runs_by_inning_side.items() if s == "0"),
                      "1": sum(v for (i, s), v in runs_by_inning_side.items() if s == "1")}
            runs_ok = p_runs["0"] == vs and p_runs["1"] == hs
            stats["runs_games"] += 1
            stats["runs_ok"] += int(runs_ok)

            p_rbi = {"0": 0, "1": 0}
            p_sb = {"0": 0, "1": 0}
            p_hr = {"0": 0, "1": 0}
            for (pid, side), b in bat.items():
                p_rbi[side] += b["rbi"]
                p_sb[side] += b["sb"]
                p_hr[side] += b["hr"]
            rbi_ok = p_rbi["0"] == rec["vis_rbi"] and p_rbi["1"] == rec["home_rbi"]
            sb_ok = (p_sb["0"] == rec["vis_sb"] and p_sb["1"] == rec["home_sb"]
                     and sb_no_owner == 0)
            hr_ok = p_hr["0"] == rec["vis_hr"] and p_hr["1"] == rec["home_hr"]
            stats["rbi_games"] += 1
            stats["rbi_ok"] += int(rbi_ok)
            stats["sb_games"] += 1
            stats["sb_ok"] += int(sb_ok)
            stats["hr_games"] += 1
            stats["hr_ok"] += int(hr_ok)

            total_outs = sum(v["outs"] for v in pit.values())
            outs_ok = rec["outs_len"] is not None and total_outs == rec["outs_len"]
            stats["outs_games"] += 1
            stats["outs_ok"] += int(outs_ok)
            # defensive putouts split (visitor defense = outs while home bats)
            outs_def = {"0": 0, "1": 0}
            for (i, s), v in half_outs.items():
                outs_def["0" if s == "1" else "1"] += v
            po_ok = (outs_def["0"] == rec["vis_po"] and outs_def["1"] == rec["home_po"])
            stats["po_games"] += 1
            stats["po_ok"] += int(po_ok)

            # half-inning 3-out check (skip each side's final half-inning)
            last_inning = {s: max((i for (i, ss) in half_outs if ss == s), default=0)
                           for s in ("0", "1")}
            for (i, s), o in half_outs.items():
                if i == last_inning[s]:
                    continue
                stats["half_innings"] += 1
                if o != 3:
                    stats["half_innings_bad"] += 1

            rfi = (runs_by_inning_side.get((1, "0"), 0)
                   + runs_by_inning_side.get((1, "1"), 0)) > 0

            game_rows.append({
                "year": year, "game": cur_id, "vs": vs, "hs": hs,
                "rfi": rfi, "runs_ok": runs_ok, "rbi_ok": rbi_ok,
                "sb_ok": sb_ok, "outs_ok": outs_ok, "po_ok": po_ok,
            })

            for (pid, side), b in bat.items():
                if b["pa"] == 0:
                    if b["sb"] > 0:
                        stats["sb_outside_pa_frame"] += b["sb"]
                    continue
                is_home = side == "1"
                team_runs = hs if is_home else vs
                opp_runs = vs if is_home else hs
                batter_games.append({
                    "year": year, "game": cur_id, "pid": pid,
                    "started": (pid, side) in start_pids,
                    "hit": b["hit"], "hr": b["hr"], "tb": b["tb"],
                    "rbi": b["rbi"], "sb": b["sb"],
                    "hrr": b["hit"] + b["r"] + b["rbi"],
                    "team_home": is_home, "team_runs": team_runs,
                    "opp_runs": opp_runs, "total": vs + hs,
                    "margin": team_runs - opp_runs,
                    "won": home_won if is_home else not home_won,
                    "rfi": rfi, "rbi_ok": rbi_ok, "sb_ok": sb_ok,
                })
            for side, spid in (("1", starters["1"]), ("0", starters["0"])):
                if spid is None or spid not in pit:
                    continue
                is_home = side == "1"
                team_runs = hs if is_home else vs
                opp_runs = vs if is_home else hs
                starter_games.append({
                    "year": year, "game": cur_id, "pid": spid,
                    "ks": pit[spid]["k"], "outs": pit[spid]["outs"],
                    "team_home": is_home, "team_runs": team_runs,
                    "opp_runs": opp_runs, "total": vs + hs,
                    "margin": team_runs - opp_runs,
                    "won": home_won if is_home else not home_won,
                    "rfi": rfi, "outs_ok": outs_ok,
                })

        for fields in csv.reader(io.StringIO(text)):
            if not fields:
                continue
            rt = fields[0]
            if rt == "id":
                flush()
                cur_id = fields[1]
                starters = {"0": None, "1": None}
                cur_pitcher = {"0": None, "1": None}
                lineup = {"0": {}, "1": {}}
                start_pids = set()
                bases = {"1": None, "2": None, "3": None}
                cur_half = None
                half_outs = {}
                runs_by_inning_side = defaultdict(int)
                bat = {}
                pit = {}
                sb_no_owner = 0
                pending_radj = []
            elif rt in ("start", "sub"):
                try:
                    pid, side, order, pos = fields[1], fields[3], fields[4], fields[5]
                except IndexError:
                    continue
                if rt == "start":
                    start_pids.add((pid, side))
                # lineup slot replacement (covers pinch runners on base)
                old = lineup[side].get(order)
                if old and old != pid:
                    for b in ("1", "2", "3"):
                        if bases[b] == old:
                            bases[b] = pid
                lineup[side][order] = pid
                if pos == "1":
                    cur_pitcher[side] = pid
                    if rt == "start":
                        starters[side] = pid
            elif rt == "radj":
                # extra-innings placed runner (2020+): radj,playerid,base.
                # Arrives BEFORE the half's first play — defer past the
                # half-inning base reset.
                try:
                    pending_radj.append((fields[1], fields[2]))
                except IndexError:
                    pass
            elif rt == "play":
                try:
                    inning, half, batter, event = (
                        int(fields[1]), fields[2], fields[3], fields[6])
                except (IndexError, ValueError):
                    continue
                if event == "NP":
                    continue
                hkey = (inning, half)
                if hkey != cur_half:
                    cur_half = hkey
                    bases = {"1": None, "2": None, "3": None}
                    for rpid, rbase in pending_radj:
                        if rbase in bases:
                            bases[rbase] = rpid
                    pending_radj = []
                    half_outs.setdefault(hkey, 0)
                defside = "0" if half == "1" else "1"
                pitcher = cur_pitcher[defside]

                basic, mods, adv = split_event(event)
                pb = parse_basic(basic)
                advs = parse_advances(adv)
                adv_by_runner = {a[0]: a for a in advs}

                if pb["pa"]:
                    br = bat_rec(batter, half)
                    br["pa"] += 1
                    if pb["hit"]:
                        br["hit"] += 1
                        br["tb"] += pb["hit"]  # S=1 D=2 T=3 HR=4 total bases
                        if pb["hit"] == 4:
                            br["hr"] += 1
                if pb["k"] and pitcher is not None:
                    pit_rec(pitcher)["k"] += 1

                outs = 0
                outed_runners: set[str] = set()

                # SB credits (occupant of the from-base) + IMPLIED advance:
                # SB2 moves 1->2 (SBH scores, no RBI) unless an explicit
                # advance for that runner overrides.
                sb_moves: list[tuple[str, str]] = []
                for dest_base in pb["sb"]:
                    frm = {"2": "1", "3": "2", "H": "3"}[dest_base]
                    owner = bases[frm]
                    if owner is not None:
                        bat_rec(owner, half)["sb"] += 1
                    else:
                        sb_no_owner += 1
                        stats["sb_unattributed"] += 1
                    if frm not in adv_by_runner:
                        sb_moves.append((frm, dest_base))

                # CS / PO outs (explicit advances take precedence)
                for dest_base, is_out in pb["cs"]:
                    frm = {"2": "1", "3": "2", "H": "3"}[dest_base]
                    if frm in adv_by_runner:
                        continue
                    if is_out:
                        outs += 1
                        outed_runners.add(frm)
                        bases[frm] = None
                    else:
                        if bases[frm] is not None and dest_base != "H":
                            bases[dest_base] = bases[frm]
                            bases[frm] = None
                for base_on, is_out in pb["po"]:
                    if base_on in adv_by_runner:
                        continue
                    if is_out:
                        outs += 1
                        outed_runners.add(base_on)
                        bases[base_on] = None

                # basic-play runner chunk outs
                for r in pb["chunk_outs"]:
                    if r in adv_by_runner:
                        continue  # explicit advance overrides
                    outs += 1
                    outed_runners.add(r)
                    if r in bases:
                        bases[r] = None

                # batter fate
                batter_out = pb["batter_out"]
                batter_dest = pb["batter_dest"]
                if "B" in adv_by_runner:
                    _, dest, is_out, groups = adv_by_runner["B"]
                    batter_out = is_out
                    batter_dest = None if is_out else dest
                if batter_out:
                    outs += 1

                # explicit runner advances (non-batter), process 3->1
                moves: list[tuple[str, str, list[str], bool]] = []
                for r in ("3", "2", "1"):
                    if r in adv_by_runner:
                        _, dest, is_out, groups = adv_by_runner[r]
                        if is_out:
                            if r not in outed_runners:
                                outs += 1
                                outed_runners.add(r)
                            bases[r] = None
                        else:
                            moves.append((r, dest, groups, True))
                # SB-implied moves (no explicit advance): dest order handled by sort
                for frm, dest_base in sb_moves:
                    if bases.get(frm) is not None:
                        moves.append((frm, dest_base, [], False))
                # implicit forces / HR clears
                explicit = set(adv_by_runner)
                if batter_dest == "H":  # HR: everyone unmentioned scores
                    for r in ("3", "2", "1"):
                        if r not in explicit and bases[r] is not None:
                            moves.append((r, "H", [], False))
                elif batter_dest == "1":
                    if "1" not in explicit and bases["1"] is not None:
                        moves.append(("1", "2", [], False))
                        if "2" not in explicit and bases["2"] is not None:
                            moves.append(("2", "3", [], False))
                            if "3" not in explicit and bases["3"] is not None:
                                moves.append(("3", "H", [], False))

                # runs + RBI determination
                gdp = any(m.startswith(("GDP", "GTP")) for m in mods)
                runner_of = dict(bases)

                def score_run(runner_id: str | None, groups: list[str],
                              self_run: bool = False) -> None:
                    runs_by_inning_side[(inning, half)] += 1
                    if runner_id is not None:
                        bat_rec(runner_id, half)["r"] += 1
                    flags = ";".join(groups)
                    no_rbi = ("NR" in flags or "NORBI" in flags)
                    explicit_rbi = "RBI" in groups
                    if explicit_rbi:
                        bat_rec(batter, half)["rbi"] += 1
                        return
                    if no_rbi:
                        return
                    if pb["baserunning_primary"] or pb["secondary_baserunning"]:
                        return
                    if gdp:
                        return
                    if self_run and pb["hit"] != 4:
                        return  # batter bats himself in only on a HR
                    # Error-aided runs: RBI is scorer judgment. Retrosheet's
                    # signal (GL-reconciled both eras): an error-aided run
                    # carrying (UR)/(TUR) = "the error caused it" -> no RBI;
                    # error-aided WITHOUT the unearned flag -> credited.
                    err_aided = pb["error_play"] or any(_ERR_RE.search(g) for g in groups)
                    if err_aided and "UR" in flags:
                        return
                    bat_rec(batter, half)["rbi"] += 1

                # apply moves two-phase (clear all sources, then place from the
                # pre-move snapshot) — order-independent, no chain overwrites
                for r, _dest, _groups, _explicit in moves:
                    if r in bases:
                        bases[r] = None
                for r, dest, groups, _explicit in moves:
                    pid_r = runner_of.get(r)
                    if dest == "H":
                        score_run(pid_r, groups)
                    else:
                        bases[dest] = pid_r
                # batter placement
                if not batter_out and batter_dest is not None:
                    if batter_dest == "H":
                        _, _, _, bgroups = adv_by_runner.get("B", ("B", "H", False, []))
                        score_run(batter, bgroups, self_run=True)
                    else:
                        bases[batter_dest] = batter

                half_outs[hkey] = half_outs.get(hkey, 0) + outs
                if pitcher is not None and outs:
                    pit_rec(pitcher)["outs"] += outs
        flush()

    return {
        "batter_games": batter_games,
        "starter_games": starter_games,
        "game_rows": game_rows,
        "stats": dict(stats),
    }


def parse_cached(year: int, gl: dict[str, dict], force: bool = False) -> dict:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{year}.pkl.gz"
    if cache.exists() and not force:
        with gzip.open(cache, "rb") as f:
            return pickle.load(f)  # noqa: S301
    res = parse_year_full(year, gl)
    with gzip.open(cache, "wb", compresslevel=5) as f:
        pickle.dump(res, f, protocol=pickle.HIGHEST_PROTOCOL)
    return res


# --------------------------------------------------------------------------- #
# measurement helpers (mirror the existing tool's measure/rho_ci99)
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
    return {"n": n, "p_a": p_a, "p_b": p_b, "p_ab": p_ab,
            "rho": rho, "lo": lo, "hi": hi}


def cluster_floor_ci(res: dict, n_clusters: int) -> tuple[float, float]:
    """Cluster-floor CI (results_baseball.md convention): binomial SE with
    n = distinct team-games (all batters in a game treated fully redundant)."""
    if res["n"] == 0 or n_clusters == 0:
        return float("nan"), float("nan")
    se = math.sqrt(max(res["p_ab"] * (1 - res["p_ab"]), 1e-12) / n_clusters)
    lo = implied_rho(res["p_a"], res["p_b"], max(0.0, res["p_ab"] - _Z99 * se))
    hi = implied_rho(res["p_a"], res["p_b"], min(1.0, res["p_ab"] + _Z99 * se))
    return lo, hi


def era_split(rows: list[dict], fa, fb, cut: int = 2020) -> dict:
    tr = [(fa(r), fb(r)) for r in rows if r["year"] < cut
          and fa(r) is not None and fb(r) is not None]
    ho = [(fa(r), fb(r)) for r in rows if r["year"] >= cut
          and fa(r) is not None and fb(r) is not None]
    return {"train": measure_bools(tr), "holdout": measure_bools(ho)}


def pair_stats(rows: list[dict], fa, fb) -> dict:
    """Full stat block: pooled + era split + drift flag."""
    valid = [(fa(r), fb(r)) for r in rows
             if fa(r) is not None and fb(r) is not None]
    res = measure_bools(valid)
    if res["n"] == 0:
        return res
    es = era_split(rows, fa, fb)
    res["train_rho"] = es["train"].get("rho", float("nan"))
    res["holdout_rho"] = es["holdout"].get("rho", float("nan"))
    res["holdout_n"] = es["holdout"].get("n", 0)
    hr_ = res["holdout_rho"]
    res["oos_flag"] = (not math.isnan(hr_)) and not (res["lo"] <= hr_ <= res["hi"])
    return res


def median(vals: list[int]) -> float:
    s = sorted(vals)
    return s[len(s) // 2]


def fmt(res: dict, label: str, extra: str = "") -> str:
    if res.get("n", 0) == 0:
        return f"{label:46s}  (no data)"
    drift = res.get("holdout_rho", float("nan")) - res.get("train_rho", float("nan"))
    flag = " OOS-FLAG" if res.get("oos_flag") else ""
    return (f"{label:46s} n={res['n']:>8} P(A)={res['p_a']:.4f} P(B)={res['p_b']:.4f} "
            f"P(AB)={res['p_ab']:.4f} rho={res['rho']:+.4f} "
            f"CI99=[{res['lo']:+.4f},{res['hi']:+.4f}] "
            f"era({res.get('train_rho', float('nan')):+.3f}->"
            f"{res.get('holdout_rho', float('nan')):+.3f} d={drift:+.3f}){flag} {extra}")


# --------------------------------------------------------------------------- #
# selftest — event-parser battery
# --------------------------------------------------------------------------- #
def selftest() -> None:
    cases = [
        # (event, exp_outs_ish) — via parse pieces; verified by construction below
        ("S8", dict(hit=1, dest="1", out=False, k=False)),
        ("K", dict(hit=0, dest=None, out=True, k=True)),
        ("K23", dict(out=True, k=True)),
        ("HR/78/F", dict(hit=4, dest="H")),
        ("W", dict(dest="1")),
        ("HP", dict(dest="1")),
        ("E6/G", dict(dest="1", err=True)),
        ("FC5/G", dict(dest="1")),
        ("63/G", dict(out=True)),
        ("64(1)3/GDP", dict(out=True, chunk=["1"])),
        ("8(B)84(2)/LDP", dict(out=True, chunk=["2"])),
        ("54(1)/FO", dict(out=False, dest="1", chunk=["1"])),
        ("SB2", dict(sb=["2"], pa=False)),
        ("CS2(24)", dict(cs=[("2", True)], pa=False)),
        ("CS2(2E4)", dict(cs=[("2", False)], pa=False)),
        ("PO1(13)", dict(po=[("1", True)], pa=False)),
        ("POCS2(1361)", dict(cs=[("2", True)], pa=False)),
        ("K+SB2", dict(k=True, out=True, sb=["2"])),
        ("K+WP", dict(k=True, out=True, sec=True)),
        ("W+WP", dict(dest="1", sec=True)),
        ("SB3;SB2", dict(sb=["3", "2"], pa=False)),
    ]
    bad = 0
    for ev, exp in cases:
        basic, mods, adv = split_event(ev)
        p = parse_basic(basic)
        ok = True
        if "hit" in exp and p["hit"] != exp["hit"]:
            ok = False
        if "dest" in exp and p["batter_dest"] != exp["dest"]:
            ok = False
        if "out" in exp and p["batter_out"] != exp["out"]:
            ok = False
        if "k" in exp and p["k"] != exp["k"]:
            ok = False
        if "sb" in exp and p["sb"] != exp["sb"]:
            ok = False
        if "cs" in exp and p["cs"] != exp["cs"]:
            ok = False
        if "po" in exp and p["po"] != exp["po"]:
            ok = False
        if "chunk" in exp and p["chunk_outs"] != exp["chunk"]:
            ok = False
        if "pa" in exp and p["pa"] != exp["pa"]:
            ok = False
        if "err" in exp and p["error_play"] != exp["err"]:
            ok = False
        if "sec" in exp and p["secondary_baserunning"] != exp["sec"]:
            ok = False
        print(("OK  " if ok else "FAIL") + f"  {ev:20s} -> {p}")
        bad += 0 if ok else 1
    # advance parsing
    advs = parse_advances("3-H(UR)(NR);1X3(56);BX2(8E4)")
    assert advs[0] == ("3", "H", False, ["UR", "NR"]), advs[0]
    assert advs[1][2] is True, advs[1]
    assert advs[2][2] is False, advs[2]  # error negates the out
    print("advance-parse assertions OK")
    print(f"\nselftest: {len(cases) - bad}/{len(cases)} basic cases pass")
    if bad:
        sys.exit(1)


# --------------------------------------------------------------------------- #
# spot-check validation (known games + season aggregates)
# --------------------------------------------------------------------------- #
def spot_checks(data_by_year: dict[int, dict], names: dict[str, str]) -> list[str]:
    lines: list[str] = []

    def find_starter(year: int, game: str, pid_prefix: str | None = None):
        for r in data_by_year[year]["starter_games"]:
            if r["game"] == game and (pid_prefix is None or r["pid"].startswith(pid_prefix)):
                yield r

    def find_batters(year: int, game: str):
        for r in data_by_year[year]["batter_games"]:
            if r["game"] == game:
                yield r

    # 1) Scherzer 2016-05-11 (WAS201605110): 20 K, CG 27 outs
    if 2016 in data_by_year:
        for r in find_starter(2016, "WAS201605110", "schem"):
            lines.append(f"SPOT 1  Scherzer WAS201605110: parsed ks={r['ks']} outs={r['outs']}"
                         f"  (published: 20 K, 9 IP = 27 outs)")
    # 2) Rendon 2017-04-30 (WAS201704300): 10 RBI, 3 HR, 6 hits
    if 2017 in data_by_year:
        for r in find_batters(2017, "WAS201704300"):
            if r["pid"].startswith("rendo") or r["pid"].startswith("renda"):
                lines.append(f"SPOT 2  Rendon WAS201704300: parsed rbi={r['rbi']} hr={r['hr']} "
                             f"hit={r['hit']}  (published: 10 RBI, 3 HR, 6 H)")
    # 3) Elly De La Cruz 2023-07-08 (vs MIL): 3 SB incl. steal of home —
    #    locate the game dynamically (any CIN 2023-07 game with a 3-SB player)
    if 2023 in data_by_year:
        hits3 = [(r["game"], r["pid"], r["sb"]) for r in data_by_year[2023]["batter_games"]
                 if r["sb"] >= 3 and "202307" in r["game"]]
        for g, p, s in hits3:
            lines.append(f"SPOT 3  {g}: {names.get(p, p)} parsed SB={s}"
                         f"  (published: Elly De La Cruz 3 SB on 2023-07-08 incl. steal of home)")
    # 4) season aggregates
    aggs = [
        (2023, "acunr001", "sb", 73, "Acuna 2023 SB"),
        (2023, "acunr001", "hr", 41, "Acuna 2023 HR"),
        (2023, "acunr001", "rbi", 106, "Acuna 2023 RBI"),
        (2022, "judga001", "hr", 62, "Judge 2022 HR"),
        (2022, "judga001", "rbi", 131, "Judge 2022 RBI"),
    ]
    for year, pid, stat, official, label in aggs:
        if year not in data_by_year:
            continue
        tot = sum(r[stat] for r in data_by_year[year]["batter_games"] if r["pid"] == pid)
        lines.append(f"SPOT agg  {label}: parsed={tot} official={official}"
                     f"  ({'MATCH' if tot == official else 'MISMATCH'})")
    return lines


# --------------------------------------------------------------------------- #
# report — the full measurement pass
# --------------------------------------------------------------------------- #
def build_report(years: list[int]) -> None:
    gl = load_gl_full(years)
    print(f"game-log rows loaded: {len(gl)}", file=sys.stderr)
    data_by_year: dict[int, dict] = {}
    agg = defaultdict(int)
    for y in years:
        data_by_year[y] = parse_cached(y, gl)
        for k, v in data_by_year[y]["stats"].items():
            agg[k] += v
        print(f"  {y}: joined={data_by_year[y]['stats'].get('games_joined', 0)}",
              file=sys.stderr)

    names = load_rosters([min(2016, max(years))] + [y for y in (2016, 2017, 2022, 2023)
                                                    if y in years])
    out: dict = {"years": years, "parse_stats": dict(agg)}

    print("\n================ PARSE / RECONCILIATION EVIDENCE ================")
    for k in ("games_seen", "games_joined", "games_unjoined", "games_tie"):
        print(f"  {k:24s} {agg[k]}")

    def rate(okk: str, nk: str) -> str:
        return f"{agg[okk]}/{agg[nk]} = {100.0 * agg[okk] / max(agg[nk], 1):.2f}%"

    recon = {
        "team runs == official score": rate("runs_ok", "runs_games"),
        "team RBI == official GL": rate("rbi_ok", "rbi_games"),
        "team SB == official GL": rate("sb_ok", "sb_games"),
        "team HR == official GL (index sanity)": rate("hr_ok", "hr_games"),
        "game outs == GL length-in-outs": rate("outs_ok", "outs_games"),
        "defensive outs == GL putouts": rate("po_ok", "po_games"),
        "half-innings with exactly 3 outs": rate("half_innings_bad", "half_innings")
        .replace("=", "bad ="),
    }
    for k, v in recon.items():
        print(f"  {k:40s} {v}")
    print(f"  SB events unattributable to a runner: {agg['sb_unattributed']}")
    print(f"  SB outside all-PA frame (pinch-runner etc.): {agg['sb_outside_pa_frame']}")
    out["reconciliation"] = {k: v for k, v in recon.items()}
    out["reconciliation_raw"] = {k: agg[k] for k in list(agg)}

    print("\n---- spot checks vs published box scores ----")
    sc = spot_checks(data_by_year, names)
    for line in sc:
        print("  " + line)
    out["spot_checks"] = sc

    # ---------------- assemble record sets ----------------
    all_batter: list[dict] = []
    all_starter: list[dict] = []
    all_games: list[dict] = []
    for y in years:
        all_batter += data_by_year[y]["batter_games"]
        all_starter += data_by_year[y]["starter_games"]
        all_games += data_by_year[y]["game_rows"]

    # season-median game-total line (the repo convention; ties excluded)
    tot_by_year: dict[int, list[int]] = defaultdict(list)
    for g in all_games:
        tot_by_year[g["year"]].append(g["vs"] + g["hs"])
    tot_med = {y: median(v) for y, v in tot_by_year.items()}

    # self-season-median K and OUTS lines (>=5 starts, the KS convention)
    by_py: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for r in all_starter:
        by_py[(r["pid"], r["year"])].append(r)
    k_med: dict[tuple[str, int], float] = {}
    o_med: dict[tuple[str, int], float] = {}
    for key, rows_ in by_py.items():
        if len(rows_) >= 5:
            k_med[key] = median([r["ks"] for r in rows_])
            o_med[key] = median([r["outs"] for r in rows_])

    def srow(r: dict) -> dict:
        key = (r["pid"], r["year"])
        gt = r["total"]
        tm = tot_med[r["year"]]
        d = dict(r)
        d["k_over"] = None if key not in k_med or r["ks"] == k_med[key] else r["ks"] > k_med[key]
        d["o_over"] = None if key not in o_med or r["outs"] == o_med[key] else r["outs"] > o_med[key]
        d["total_over"] = None if gt == tm else gt > tm
        return d

    S = [srow(r) for r in all_starter]
    S_ok = [r for r in S if r["outs_ok"]]  # outs-reconciled games only
    n_games_ok = len({r["game"] for r in S_ok})

    def brow(r: dict) -> dict:
        gt = r["total"]
        tm = tot_med[r["year"]]
        d = dict(r)
        d["total_over"] = None if gt == tm else gt > tm
        return d

    B = [brow(r) for r in all_batter]
    B_rbi = [r for r in B if r["rbi_ok"]]      # RBI-reconciled games only
    B_sb = [r for r in B if r["sb_ok"]]        # SB-reconciled games only

    results: dict[str, dict] = {}

    def rec(name: str, res: dict, verdict_note: str = "") -> None:
        results[name] = {k: v for k, v in res.items()}
        if verdict_note:
            results[name]["note"] = verdict_note

    # =================================================================== #
    print("\n================ OUTS family (starter-game unit) ================")
    print(f"starter rows: {len(S)}  outs-reconciled: {len(S_ok)} "
          f"({100.0 * len(S_ok) / len(S):.2f}%)  distinct games: {n_games_ok}")

    r = pair_stats(S_ok, lambda x: x["o_over"], lambda x: x["k_over"])
    print(fmt(r, "OUTS over x KS over (SAME pitcher, self-med)"))
    rec("outs|ks:same", r)

    # opposing starters: both orderings, unit = game-ordering
    by_game: dict[str, list[dict]] = defaultdict(list)
    for x in S_ok:
        by_game[x["game"]].append(x)
    opp_rows = []
    for g, rows_ in by_game.items():
        if len(rows_) == 2:
            a, b = rows_
            opp_rows.append({"year": a["year"], "ao": a["o_over"], "bk": b["k_over"]})
            opp_rows.append({"year": a["year"], "ao": b["o_over"], "bk": a["k_over"]})
    r = pair_stats(opp_rows, lambda x: x["ao"], lambda x: x["bk"])
    print(fmt(r, "OUTS over x KS over (OPPOSING starters)"))
    rec("outs|ks:opp", r)

    r = pair_stats(S_ok, lambda x: x["o_over"], lambda x: x["total_over"])
    lo_c, hi_c = cluster_floor_ci(r, n_games_ok)
    print(fmt(r, "OUTS over x GAME total over", f"clusterCI=[{lo_c:+.4f},{hi_c:+.4f}]"))
    r["cluster_lo"], r["cluster_hi"] = lo_c, hi_c
    rec("outs|total", r)

    r = pair_stats(S_ok, lambda x: x["o_over"], lambda x: x["won"])
    print(fmt(r, "OUTS over x own team WINS (:same)"))
    rec("moneyline|outs:same", r)
    # :opp is the exact 2-way complement (copula antisymmetry) — verify directly
    r2 = pair_stats(S_ok, lambda x: x["o_over"], lambda x: not x["won"])
    print(f"  direct :opp measurement rho={r2['rho']:+.4f} "
          f"(negation check vs {-r['rho']:+.4f})")
    rec("moneyline|outs:opp", r2)

    for nrung in (2, 3, 4, 5):
        r = pair_stats(S_ok, lambda x: x["o_over"],
                       lambda x, n=nrung: x["margin"] >= n)
        print(fmt(r, f"OUTS over x own team wins by {nrung}+ (:same:r{nrung})"))
        rec(f"outs|spread:same:r{nrung}", r)
    for nrung in (2, 3, 4, 5):
        r = pair_stats(S_ok, lambda x: x["o_over"],
                       lambda x, n=nrung: x["margin"] <= -n)
        print(fmt(r, f"OUTS over x opp wins by {nrung}+ (:opp:r{nrung})"))
        rec(f"outs|spread:opp:r{nrung}", r)

    r = pair_stats(S_ok, lambda x: x["o_over"], lambda x: x["rfi"])
    print(fmt(r, "OUTS over x RFI (either team, inning 1)"))
    rec("outs|rfi", r)

    print("\n---- OUTS fixed-rung ladder (ALL starters, flatness check) ----")
    for nrung in (12, 15, 18, 21):
        r = pair_stats(S_ok, lambda x, n=nrung: x["outs"] >= n,
                       lambda x: x["total_over"])
        print(fmt(r, f"outs>={nrung} x total over"))
        rec(f"outs_r{nrung}|total", r)
    for nrung in (12, 15, 18, 21):
        r = pair_stats(S_ok, lambda x, n=nrung: x["outs"] >= n,
                       lambda x: x["k_over"])
        print(fmt(r, f"outs>={nrung} x KS over (same pitcher)"))
        rec(f"outs_r{nrung}|ks:same", r)

    # =================================================================== #
    print("\n================ RBI family (batter-game unit, all-PA frame) ================")
    n_tg_rbi = len({(r["game"], r["team_home"]) for r in B_rbi})
    print(f"batter rows: {len(B)}  RBI-reconciled rows: {len(B_rbi)} "
          f"({100.0 * len(B_rbi) / len(B):.2f}%)  distinct team-games: {n_tg_rbi}")

    # ---- same-player exact containment: HR(k) => RBI(k) ----
    print("\n---- same-player exact-containment verification (FULL corpus, all rows) ----")
    containment = {}
    for k in (1, 2, 3):
        base = [r for r in B if r["hr"] >= k]
        viol = [r for r in B if r["hr"] >= k and r["rbi"] < k]
        containment[f"hr{k}=>rbi{k}"] = (len(base), len(viol))
        print(f"  HR>={k} => RBI>={k}: n={len(base)}  violations={len(viol)}"
              + ("  EXACT" if not viol else "  ** NOT EXACT — investigate **"))
    for k in (1, 2, 3):
        base = [r for r in B if r["rbi"] >= k]
        viol = [r for r in B if r["rbi"] >= k and r["hrr"] < k]
        containment[f"rbi{k}=>hrr{k}"] = (len(base), len(viol))
        print(f"  RBI>={k} => HRR>={k} (arith: HRR=H+R+RBI): n={len(base)} "
              f"violations={len(viol)}" + ("  EXACT" if not viol else "  ** NOT EXACT **"))
    # SB=>HIT must NOT be a containment (walks/HBP reach base too)
    sb_no_hit = sum(1 for r in B if r["sb"] >= 1 and r["hit"] == 0)
    sb_any = sum(1 for r in B if r["sb"] >= 1)
    containment["sb1_and_hit0"] = (sb_any, sb_no_hit)
    print(f"  SB>=1 & HIT=0 rows: {sb_no_hit}/{sb_any} "
          f"({100.0 * sb_no_hit / max(sb_any, 1):.1f}%) — SB=>HIT is NOT a containment (as expected)")
    out["containment"] = containment

    # ---- same-player conditional cells (conditionals_mlb.py format) ----
    print("\n---- same-player conditional cells (RBI-reconciled rows; "
          "(famA,rungA,famB,rungB) -> P(B|A), n) ----")
    cells: dict[str, tuple[float, int]] = {}

    def cond_cell(fam_a: str, ka: int, fam_b: str, kb: int,
                  stat_a: str, stat_b: str, rows_: list[dict]) -> None:
        base = [r for r in rows_ if r[stat_a] >= ka]
        if not base:
            return
        p = sum(1 for r in base if r[stat_b] >= kb) / len(base)
        # era stability of the cell
        b_tr = [r for r in base if r["year"] < 2020]
        b_ho = [r for r in base if r["year"] >= 2020]
        p_tr = sum(1 for r in b_tr if r[stat_b] >= kb) / max(len(b_tr), 1)
        p_ho = sum(1 for r in b_ho if r[stat_b] >= kb) / max(len(b_ho), 1)
        key = f"('{fam_a}', {ka}, '{fam_b}', {kb})"
        cells[key] = (p, len(base))
        marker = "exact" if p == 1.0 and p_tr == 1.0 and p_ho == 1.0 else "measured"
        print(f"  ({fam_a},{ka},{fam_b},{kb}): P={p:.6f} n={len(base)} "
              f"era {p_tr:.4f}->{p_ho:.4f}  [{marker}]")

    for ka in (1, 2):
        for kb in (1, 2, 3):
            cond_cell("hr", ka, "rbi", kb, "hr", "rbi", B_rbi)
    for ka in (1, 2, 3):
        for kb in (1, 2):
            cond_cell("rbi", ka, "hr", kb, "rbi", "hr", B_rbi)
        for kb in (1, 2, 3):
            cond_cell("rbi", ka, "hit", kb, "rbi", "hit", B_rbi)
            cond_cell("hit", kb, "rbi", ka, "hit", "rbi", B_rbi)
        for kb in (2, 3, 4):
            cond_cell("rbi", ka, "tb", kb, "rbi", "tb", B_rbi)
        for kb in (2, 3, 4, 5):
            cond_cell("rbi", ka, "hrr", kb, "rbi", "hrr", B_rbi)
            cond_cell("hrr", kb, "rbi", ka, "hrr", "rbi", B_rbi)
    for kb in (1, 2, 3):
        cond_cell("sb", 1, "hit", kb, "sb", "hit", B_sb)
        cond_cell("hit", kb, "sb", 1, "hit", "sb", B_sb)
    out["conditional_cells"] = {k: v for k, v in cells.items()}

    # ---- team-context RBI pairs ----
    print("\n---- RBI x team context ----")
    for k in (1, 2, 3):
        r = pair_stats(B_rbi, lambda x, kk=k: x["rbi"] >= kk,
                       lambda x: x["total_over"])
        lo_c, hi_c = cluster_floor_ci(r, n_tg_rbi)
        print(fmt(r, f"RBI>={k} x GAME total over (:r{k})",
                  f"clusterCI=[{lo_c:+.4f},{hi_c:+.4f}]"))
        r["cluster_lo"], r["cluster_hi"] = lo_c, hi_c
        rec(f"rbi|total:r{k}", r)
    for k in (1, 2, 3):
        r = pair_stats(B_rbi, lambda x, kk=k: x["rbi"] >= kk, lambda x: x["won"])
        lo_c, hi_c = cluster_floor_ci(r, n_tg_rbi)
        print(fmt(r, f"RBI>={k} x own team WINS (:same:r{k})",
                  f"clusterCI=[{lo_c:+.4f},{hi_c:+.4f}]"))
        r["cluster_lo"], r["cluster_hi"] = lo_c, hi_c
        rec(f"moneyline|rbi:same:r{k}", r)
    r2 = pair_stats(B_rbi, lambda x: x["rbi"] >= 1, lambda x: not x["won"])
    print(f"  direct :opp (rbi1) rho={r2['rho']:+.4f} (negation check)")
    rec("moneyline|rbi:opp:r1", r2)

    # frame gap: starters-only (lineup) variant of the headline rows
    B_rbi_st = [r for r in B_rbi if r["started"]]
    r = pair_stats(B_rbi_st, lambda x: x["rbi"] >= 1, lambda x: x["total_over"])
    print(fmt(r, "  [frame] STARTERS-only RBI>=1 x total over"))
    rec("rbi|total:r1:STARTERS_FRAME", r)
    r = pair_stats(B_rbi_st, lambda x: x["rbi"] >= 1, lambda x: x["won"])
    print(fmt(r, "  [frame] STARTERS-only RBI>=1 x own team wins"))
    rec("moneyline|rbi:same:r1:STARTERS_FRAME", r)

    # ---- teammate / opposing RBI x HR (distinct players), combinatorial ----
    print("\n---- RBI x HR teammate/opposing frames (distinct players) ----")

    def team_pair_stats(rows_: list[dict], stat_a: str, ka: int,
                        stat_b: str, kb: int, frame: str) -> dict:
        """P over ordered distinct-player pairs; frame='teammate'|'opposing'."""
        tg: dict[tuple[str, bool], list[dict]] = defaultdict(list)
        for x in rows_:
            tg[(x["game"], x["team_home"])].append(x)
        stats_by_era = {"pool": [0, 0, 0, 0], "tr": [0, 0, 0, 0], "ho": [0, 0, 0, 0]}
        games = defaultdict(dict)
        for (g, home), players in tg.items():
            games[g][home] = players
        n_cluster = 0
        for g, sides in games.items():
            if frame == "teammate":
                for home, players in sides.items():
                    kcnt = len(players)
                    a = sum(1 for p in players if p[stat_a] >= ka)
                    b = sum(1 for p in players if p[stat_b] >= kb)
                    ab = sum(1 for p in players if p[stat_a] >= ka and p[stat_b] >= kb)
                    npairs = kcnt * (kcnt - 1)
                    hits_a = a * (kcnt - 1)
                    hits_b = b * (kcnt - 1)
                    hits_ab = a * b - ab
                    yr = players[0]["year"]
                    for tag in ("pool", "tr" if yr < 2020 else "ho"):
                        s = stats_by_era[tag]
                        s[0] += npairs
                        s[1] += hits_a
                        s[2] += hits_b
                        s[3] += hits_ab
                    n_cluster += 1
            else:
                if len(sides) != 2:
                    continue
                p1 = sides[True]
                p2 = sides[False]
                a1 = sum(1 for p in p1 if p[stat_a] >= ka)
                b2 = sum(1 for p in p2 if p[stat_b] >= kb)
                a2 = sum(1 for p in p2 if p[stat_a] >= ka)
                b1 = sum(1 for p in p1 if p[stat_b] >= kb)
                npairs = 2 * len(p1) * len(p2)
                hits_a = a1 * len(p2) + a2 * len(p1)
                hits_b = b2 * len(p1) + b1 * len(p2)
                hits_ab = a1 * b2 + a2 * b1
                yr = p1[0]["year"]
                for tag in ("pool", "tr" if yr < 2020 else "ho"):
                    s = stats_by_era[tag]
                    s[0] += npairs
                    s[1] += hits_a
                    s[2] += hits_b
                    s[3] += hits_ab
                n_cluster += 1

        def block(s):
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

        res = block(stats_by_era["pool"])
        if res["n"]:
            res["train_rho"] = block(stats_by_era["tr"]).get("rho", float("nan"))
            res["holdout_rho"] = block(stats_by_era["ho"]).get("rho", float("nan"))
            hr_ = res["holdout_rho"]
            res["oos_flag"] = (not math.isnan(hr_)) and not (res["lo"] <= hr_ <= res["hi"])
        return res

    r = team_pair_stats(B_rbi, "rbi", 1, "hr", 1, "teammate")
    print(fmt(r, "RBI>=1 x teammate HR>=1 (:same)", "(cluster-floor CI)"))
    rec("rbi|hr:same(teammate)", r)
    r = team_pair_stats(B_rbi, "rbi", 1, "hr", 1, "opposing")
    print(fmt(r, "RBI>=1 x opposing HR>=1 (:opp)", "(cluster-floor CI)"))
    rec("rbi|hr:opp(opposing)", r)

    # =================================================================== #
    print("\n================ SB family (batter-game unit, all-PA frame) ================")
    n_tg_sb = len({(r["game"], r["team_home"]) for r in B_sb})
    print(f"SB-reconciled rows: {len(B_sb)} ({100.0 * len(B_sb) / len(B):.2f}%)  "
          f"distinct team-games: {n_tg_sb}")

    r = pair_stats(B_sb, lambda x: x["sb"] >= 1, lambda x: x["hit"] >= 1)
    print(fmt(r, "SB>=1 x HIT>=1 (SAME player)"))
    rec("sb|hit:same_player", r)
    r = pair_stats(B_sb, lambda x: x["sb"] >= 1, lambda x: x["total_over"])
    lo_c, hi_c = cluster_floor_ci(r, n_tg_sb)
    print(fmt(r, "SB>=1 x GAME total over", f"clusterCI=[{lo_c:+.4f},{hi_c:+.4f}]"))
    r["cluster_lo"], r["cluster_hi"] = lo_c, hi_c
    rec("sb|total", r)
    r = pair_stats(B_sb, lambda x: x["sb"] >= 1, lambda x: x["won"])
    lo_c, hi_c = cluster_floor_ci(r, n_tg_sb)
    print(fmt(r, "SB>=1 x own team WINS (:same)", f"clusterCI=[{lo_c:+.4f},{hi_c:+.4f}]"))
    r["cluster_lo"], r["cluster_hi"] = lo_c, hi_c
    rec("moneyline|sb:same", r)
    r = team_pair_stats(B_sb, "sb", 1, "sb", 1, "teammate")
    print(fmt(r, "SB>=1 x teammate SB>=1 (:same)", "(cluster-floor CI)"))
    rec("sb|sb:same(teammate)", r)
    # starters-frame variant
    r = pair_stats([x for x in B_sb if x["started"]],
                   lambda x: x["sb"] >= 1, lambda x: x["won"])
    print(fmt(r, "  [frame] STARTERS-only SB>=1 x own team wins"))
    rec("moneyline|sb:same:STARTERS_FRAME", r)

    # =================================================================== #
    print("\n================ STAGED KEYS (EXECUTED via live legtypes.pair_key) ================")
    staged_keys = {
        "outs|ks": pair_key(LegType.PLAYER_KS, OUTS),  # type: ignore[arg-type]
        "outs|total": pair_key(LegType.TOTAL, OUTS),  # type: ignore[arg-type]
        "outs|ml": pair_key(LegType.MONEYLINE, OUTS),  # type: ignore[arg-type]
        "outs|spread": pair_key(LegType.SPREAD, OUTS),  # type: ignore[arg-type]
        "outs|rfi": pair_key(LegType.RFI, OUTS),  # type: ignore[arg-type]
        "rbi|total": pair_key(LegType.TOTAL, RBI),  # type: ignore[arg-type]
        "rbi|ml": pair_key(LegType.MONEYLINE, RBI),  # type: ignore[arg-type]
        "rbi|hr": pair_key(LegType.PLAYER_HR, RBI),  # type: ignore[arg-type]
        "rbi|hit": pair_key(LegType.PLAYER_HIT, RBI),  # type: ignore[arg-type]
        "rbi|tb": pair_key(LegType.PLAYER_TB, RBI),  # type: ignore[arg-type]
        "rbi|hrr": pair_key(LegType.PLAYER_HRR, RBI),  # type: ignore[arg-type]
        "sb|hit": pair_key(LegType.PLAYER_HIT, SB),  # type: ignore[arg-type]
        "sb|total": pair_key(LegType.TOTAL, SB),  # type: ignore[arg-type]
        "sb|ml": pair_key(LegType.MONEYLINE, SB),  # type: ignore[arg-type]
        "sb|sb": pair_key(SB, SB),  # type: ignore[arg-type]
        "outs|outs": pair_key(OUTS, OUTS),  # type: ignore[arg-type]
        "rbi|sb": pair_key(RBI, SB),  # type: ignore[arg-type]
        "rbi|ks": pair_key(LegType.PLAYER_KS, RBI),  # type: ignore[arg-type]
    }
    for label, keyv in sorted(staged_keys.items()):
        print(f"  {label:12s} -> {keyv}")
    out["staged_keys"] = staged_keys

    out["pairs"] = {k: {kk: vv for kk, vv in v.items() if not isinstance(vv, dict)}
                    for k, v in results.items()}
    RESULTS_JSON.write_text(json.dumps(out, indent=1, default=str))
    print(f"\nresults JSON -> {RESULTS_JSON}")


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "selftest":
        selftest()
        return
    if args and args[0] in ("parse", "report"):
        mode = args[0]
        yrs = [int(x) for x in args[1:]]
    else:
        mode = "report"
        yrs = [int(x) for x in args]
    if len(yrs) == 2 and yrs[1] > yrs[0] + 1:
        years = list(range(yrs[0], yrs[1] + 1))
    elif yrs:
        years = yrs
    else:
        years = list(range(2005, 2026))
    if mode == "parse":
        gl = load_gl_full(years)
        for y in years:
            res = parse_cached(y, gl)
            st = res["stats"]
            print(f"{y}: joined={st.get('games_joined', 0)} "
                  f"runs_ok={st.get('runs_ok', 0)}/{st.get('runs_games', 0)} "
                  f"rbi_ok={st.get('rbi_ok', 0)}/{st.get('rbi_games', 0)} "
                  f"sb_ok={st.get('sb_ok', 0)}/{st.get('sb_games', 0)} "
                  f"outs_ok={st.get('outs_ok', 0)}/{st.get('outs_games', 0)}")
        return
    build_report(years)


if __name__ == "__main__":
    main()
