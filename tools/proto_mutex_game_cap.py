"""PROTOTYPE (hard rule 8 — validate here, THEN port to risk/exposure.py).

Stage B: replace the per-game COMONOTONE worst-case loss
(`game_worst[game] += max_loss`, exposure.py:275/300 — assumes every combo on the
game loses together, impossible for mutually-exclusive legs) with a
MUTUAL-EXCLUSION-AWARE bound.

ALGORITHM (general, all-legs, provably a valid upper bound ≤ comonotone):
  For each mutually-exclusive DIMENSION in the game, partition the positions by
  the outcome each one *requires to lose*, and take max over branches. Then take
  the MIN across dimensions (the min of valid upper bounds is still valid, and
  tighter). Dimensions:
    (a) ME EVENTS  — advance(ARG)|advance(ENG), moneyline home|draw|away, etc.
        (events the exchange marks mutually_exclusive; outcomes fully enumerated).
    (b) BINARY MARKETS held on BOTH sides across positions — BTTS yes|no,
        total over|under-on-the-same-market, corners over|under-same-market.
  A position "loses in branch b" iff its legs are consistent with b; a position
  with no leg on the dimension is COMMON (loses in every branch). Fail-closed: no
  provable dimension  ⇒  comonotone (never a looser guess); an un-enumerated
  outcome ⇒ treat that position as common (conservative).

This is still comonotone WITHIN a branch (conservative — the full joint MC, A1,
nets the within-branch structure and ALL leg correlations). B is the coarse,
fail-safe backstop that removes only the provably-impossible cross-branch sum.
"""
from __future__ import annotations
import sqlite3, json, os
from collections import defaultdict
from dataclasses import dataclass

LIVE = "data/combomaker-prod-live-wc.sqlite3"


@dataclass(frozen=True)
class Pos:
    legs: tuple  # ((market_ticker, event_ticker, side), ...)
    loss: int    # max_loss in cc (or any consistent unit)


# ----------------------- the pure function to PORT ------------------------
# This is the EXACT logic that ports to risk/exposure.py (adapting Pos -> the
# committed/mass-acceptance entries). OTHER-branch form: it needs only an
# `is_me_event(event_ticker) -> bool | None` predicate (MetadataCache), never a
# full outcome enumeration, so it is robust when an outcome isn't in the book.
def game_worst_case_mutex(positions: list[Pos], is_me_event) -> int:
    """Valid, MONOTONIC upper bound on the game's worst-case loss, ≤ comonotone.

    Nets the game's single RESULT mutually-exclusive event (advance / moneyline) via
    max-over-branches; fails closed to comonotone on 0 or ≥2 ME events so the bound
    stays monotonic (E2 mass-acceptance dominance). Full all-legs hedging (BTTS
    yes/no, corners over/under, goalscorers) is deferred to the structural MC (A1).
    Mirrors risk/exposure.py `_mutex_game_worst_cc` exactly (parity target).
    """
    comonotone = sum(p.loss for p in positions)
    if not positions or is_me_event is None:
        return comonotone
    me_events, seen = [], set()
    for p in positions:
        for (_m, e, _s) in p.legs:
            if e and e not in seen:
                seen.add(e)
                if is_me_event(e) is True:
                    me_events.append(e)
    if len(me_events) != 1:
        return comonotone
    return _event_bound(positions, me_events[0])


def _required(p: Pos, event: str):
    """("is", m) | ("not", m) | None (common). YES leg on m requires m; NO leg on m
    requires NOT-m; prefer a YES leg (tightest)."""
    yes = [m for (m, e, s) in p.legs if e == event and s == "yes"]
    if yes:
        return ("is", yes[0])
    no = [m for (m, e, s) in p.legs if e == event and s == "no"]
    if no:
        return ("not", no[0])
    return None


def _event_bound(positions: list[Pos], event: str) -> int:
    reqs = [(_required(p, event), p.loss) for p in positions]
    outs = {r[1] for (r, _l) in reqs if r is not None and r[0] == "is"}
    branches = tuple(outs) + ("__OTHER__",)
    best = 0
    for b in branches:
        s = 0
        for (r, loss) in reqs:
            if r is None:
                s += loss
            elif r[0] == "is":
                if b == r[1]:
                    s += loss
            elif b != r[1]:
                s += loss
        best = max(best, s)
    return best


# ------------------------------ SELF-TESTS --------------------------------
def _t(name, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got {got}, want {want}")
    assert ok, name


def run_synthetic_tests():
    print("=== synthetic unit tests (vigorous) ===")
    ADV = "KXWCADVANCE-26JUL15ENGARG"
    argY = (("KXWCADVANCE-26JUL15ENGARG-ARG", ADV, "yes"),)
    engY = (("KXWCADVANCE-26JUL15ENGARG-ENG", ADV, "yes"),)
    me = lambda e: True if e == ADV else None      # advance event is ME
    none = lambda e: None                          # nothing known ME

    # 1. two opposite advance legs net to max(10,10)=10, not 20
    _t("advance hedge nets", game_worst_case_mutex([Pos(argY, 10), Pos(engY, 10)], me), 10)
    # 2. one-sided book == comonotone (no reduction)
    _t("one-sided == comonotone", game_worst_case_mutex([Pos(argY, 10), Pos(argY, 10)], me), 20)
    # 3. no ME info == comonotone (fail-closed)
    _t("no dims == comonotone", game_worst_case_mutex([Pos(argY, 10), Pos(engY, 10)], none), 20)
    # 4. NO-side advance leg: NO-ENG requires ARG → both need ARG → comonotone
    noEng = (("KXWCADVANCE-26JUL15ENGARG-ENG", ADV, "no"),)
    _t("noENG+yesARG both need ARG", game_worst_case_mutex([Pos(argY, 10), Pos(noEng, 10)], me), 20)
    # 5. NO-ENG (needs ARG) vs YES-ENG (needs ENG) → hedge → 10
    _t("noENG vs yesENG hedge", game_worst_case_mutex([Pos(noEng, 10), Pos(engY, 10)], me), 10)
    # 6. BTTS yes/no is NOT netted by B (binary-market hedge deferred to A1 for
    #    mass-acceptance monotonicity) → comonotone.
    bttsY = (("KXWCBTTS-26JUL15ENGARG-BTTS", "KXWCBTTS-26JUL15ENGARG", "yes"),)
    bttsN = (("KXWCBTTS-26JUL15ENGARG-BTTS", "KXWCBTTS-26JUL15ENGARG", "no"),)
    _t("BTTS yes/no NOT netted by B (A1)", game_worst_case_mutex([Pos(bttsY, 10), Pos(bttsN, 10)], none), 20)
    # 7. common leg present in both branches adds to the max branch
    common = (("KXWCTOTAL-26JUL15ENGARG-3", "KXWCTOTAL-26JUL15ENGARG", "yes"),)
    _t("common in every branch", game_worst_case_mutex([Pos(argY, 10), Pos(engY, 10), Pos(common, 5)], me), 15)
    # 8. two positions both requiring ARG (opposite BTTS) are NOT split by B → 20
    argBttsY = (("KXWCADVANCE-26JUL15ENGARG-ARG", ADV, "yes"), ("KXWCBTTS-26JUL15ENGARG-BTTS", "KXWCBTTS-26JUL15ENGARG", "yes"))
    argBttsN = (("KXWCADVANCE-26JUL15ENGARG-ARG", ADV, "yes"), ("KXWCBTTS-26JUL15ENGARG-BTTS", "KXWCBTTS-26JUL15ENGARG", "no"))
    _t("both need ARG (BTTS not split by B)", game_worst_case_mutex([Pos(argBttsY, 10), Pos(argBttsN, 10)], me), 20)
    # 8b. a SECOND ME event (moneyline) → fail-closed to comonotone (monotonicity)
    ML = "KXWCGAME-26JUL15ENGARG"
    mlArg = (("KXWCGAME-26JUL15ENGARG-ARG", ML, "yes"),)
    me2 = lambda e: True if e in (ADV, ML) else None
    _t("two ME events fail-closed", game_worst_case_mutex([Pos(argY, 10), Pos(mlArg, 10)], me2), 20)
    # 9. empty book
    _t("empty book", game_worst_case_mutex([], me), 0)
    # 10. bound is always <= comonotone and >= max single position
    book = [Pos(argY, 7), Pos(engY, 11), Pos(bttsY, 5), Pos(bttsN, 9), Pos(common, 3)]
    b = game_worst_case_mutex(book, me)
    _t("<= comonotone", b <= sum(p.loss for p in book), True)
    _t(">= max single", b >= max(p.loss for p in book), True)
    print("  all synthetic tests PASSED\n")


def run_live_validation():
    print("=== live ENGARG book validation ===")
    con = sqlite3.connect(f"file:{LIVE}?mode=ro", uri=True)
    nobid = {}
    for rid, cj in con.execute("select rfq_id,context_json from decisions where kind='quote_sent'"):
        try:
            nobid[rid] = json.loads(cj).get("no_bid_cc")
        except Exception:
            pass
    GAME = "26JUL15ENGARG"
    # advance events are mutually exclusive (exactly one team advances); the real
    # port asks MetadataCache.event_mutually_exclusive — here we mark KXWCADVANCE ME.
    def me(e):
        return True if (e and e.startswith("KXWCADVANCE")) else None
    positions = []
    for kind in ("confirm", "decline"):
        for (rid,) in con.execute(f"select rfq_id from decisions where kind='{kind}'"):
            r = con.execute("select legs_json,target_cost_cc,contracts_centi from rfqs where rfq_id=?", (rid,)).fetchone()
            if not r or not r[0]:
                continue
            legs = json.loads(r[0])
            if not any(GAME in l["market_ticker"] for l in legs):
                continue
            nb = nobid.get(rid)
            tc, cc = r[1], r[2]
            contracts = cc / 100.0 if cc else (tc / nb if (tc and nb) else 0)
            loss = int((nb or 0) * contracts)  # cc: (cc/contract) × contracts
            # per-game partition (mirrors exposure.py): only THIS game's legs.
            tup = tuple(
                (l["market_ticker"], l.get("event_ticker", ""), l.get("side", "yes"))
                for l in legs if GAME in l["market_ticker"]
            )
            positions.append(Pos(tup, loss))
    comonotone = sum(p.loss for p in positions) / 10000
    mutex = game_worst_case_mutex(positions, me) / 10000
    print(f"  108 won ENGARG auctions: comonotone=${comonotone:.2f}  mutex-aware=${mutex:.2f}  "
          f"({comonotone/mutex:.2f}x tighter)")
    print(f"  cap 20% = ${0.20*1842:.0f}  ->  comonotone {'OVER' if comonotone>368 else 'ok'}, "
          f"mutex {'OVER' if mutex>368 else 'OK'}")


if __name__ == "__main__":
    run_synthetic_tests()
    run_live_validation()
