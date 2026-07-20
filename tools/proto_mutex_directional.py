"""PROTOTYPE (hard rule 8 — validate here, THEN port to risk/exposure.py).

P0-9: Directional-cap hedge semantics (mutex credit, subset-dominance proof).

PROBLEM. The R2 directional cap (risk/limits.py item 4) binds on
``snapshot.delta_by_game`` — a sum of ``analytic_leg_deltas`` (INDEPENDENCE
proxies). Summed signed independence deltas do NOT recognize an opposing-advance
HEDGE: holding long-NO on ARG-advance and long-NO on ENG-advance is short two
mutually-exclusive outcomes (exactly ONE team advances), so both cannot resolve
adverse the concentrated way — but the independence sum treats them as ordinary
same-game concentration and over-states the directional bet. ``skip_directional_
cap`` is therefore the largest LEGITIMATE quote blocker post-fanout.

FIX (mirrors Stage B ``_mutex_game_worst_cc`` EXACTLY — the monotone pattern).
Award hedge credit by folding the per-game DIRECTIONAL magnitudes through the
SAME single-ME-event max-over-branches bound the loss axis uses:
  - each entry carries the game's DIRECTIONAL magnitude (|Σ this-game leg deltas|,
    in contracts-equivalent) and the outcome it "requires to lose" on the game's
    single RESULT mutually-exclusive event;
  - opposing-advance entries land in DIFFERENT branches ⇒ max-over-branches ⇒
    they NET instead of summing (the hedge credit);
  - fail closed to the plain SUMMED magnitude (the old comonotone directional
    bound) on 0 or >=2 ME events — never a looser guess.

This is a MONOTONIC HARD directional/model-sensitivity backstop, NOT a raised
limit: the bound is provably >= the largest single directional entry and <= the
summed magnitude, and adding any entry never lowers it — so the all-accepted
mass-acceptance snapshot dominates every realizable accepted subset (E2). Richer
all-legs / cross-market hedge credit that would BREAK monotonicity stays in the
candidate-aware structural MC (P0-1), never here.

Mirrors risk/exposure.py ``_mutex_directional_game_cc`` exactly (parity target).
Run: .venv/Scripts/python.exe tools/proto_mutex_directional.py
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

LIVE = "data/combomaker-prod-live-wc.sqlite3"


@dataclass(frozen=True)
class DirEntry:
    """One position/hypothetical's contribution to a game's directional bound.

    ``legs`` = ((market_ticker, event_ticker, side), ...) — ONLY this game's legs.
    ``magnitude`` = the game's directional magnitude for the entry, |Σ deltas| in
    contracts-equivalent (already absolute — a nonneg magnitude, like ``loss``).
    ``requires`` = True iff the entry loses iff every one of its legs is satisfied
    (a long-NO combo); a non-NO / unknown side is COMMON (pressure in every branch).
    """

    legs: tuple
    magnitude: float
    requires: bool


# ----------------------- the pure function to PORT ------------------------
# EXACT logic that ports to risk/exposure.py. Needs only an
# ``is_me_event(event_ticker) -> bool | None`` predicate (MetadataCache), never a
# full outcome enumeration — robust when an outcome isn't in the book.
def game_directional_mutex(entries: list[DirEntry], is_me_event) -> float:
    """Valid, MONOTONIC upper bound on the game's directional magnitude,
    <= the summed magnitude. Nets the game's single RESULT mutually-exclusive
    event (advance / moneyline) via max-over-branches; fails closed to the summed
    magnitude on 0 or >=2 ME events so the bound stays monotonic (E2 dominance).
    Full all-legs hedge credit is deferred to the candidate-aware MC (P0-1).
    """
    summed = sum(e.magnitude for e in entries)
    if not entries or is_me_event is None:
        return summed
    me_events, seen = [], set()
    for e in entries:
        if not e.requires:
            continue
        for (_m, ev, _s) in e.legs:
            if ev and ev not in seen:
                seen.add(ev)
                if is_me_event(ev) is True:
                    me_events.append(ev)
    if len(me_events) != 1:
        return summed
    return _event_bound(entries, me_events[0])


def _required(entry: DirEntry, event: str):
    """("is", m) | ("not", m) | None (common). YES leg on m requires m; NO leg on m
    requires NOT-m; prefer a YES leg (tightest). Only a ``requires`` entry can net —
    a common entry adds pressure in every branch."""
    if not entry.requires:
        return None
    yes = [m for (m, e, s) in entry.legs if e == event and s == "yes"]
    if yes:
        return ("is", yes[0])
    no = [m for (m, e, s) in entry.legs if e == event and s == "no"]
    if no:
        return ("not", no[0])
    return None


def _event_bound(entries: list[DirEntry], event: str) -> float:
    reqs = [(_required(e, event), e.magnitude) for e in entries]
    outs = {r[1] for (r, _m) in reqs if r is not None and r[0] == "is"}
    branches = tuple(outs) + ("__OTHER__",)
    best = 0.0
    for b in branches:
        s = 0.0
        for (r, mag) in reqs:
            if r is None:
                s += mag
            elif r[0] == "is":
                if b == r[1]:
                    s += mag
            elif b != r[1]:
                s += mag
        best = max(best, s)
    return best


# ------------------------------ SELF-TESTS --------------------------------
def _t(name, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got {got}, want {want}")
    assert ok, name


def run_synthetic_tests():
    print("=== synthetic unit tests (directional) ===")
    ADV = "KXWCADVANCE-26JUL15ENGARG"
    argY = (("KXWCADVANCE-26JUL15ENGARG-ARG", ADV, "yes"),)
    engY = (("KXWCADVANCE-26JUL15ENGARG-ENG", ADV, "yes"),)
    noEng = (("KXWCADVANCE-26JUL15ENGARG-ENG", ADV, "no"),)
    common = (("KXWCTOTAL-26JUL15ENGARG-3", "KXWCTOTAL-26JUL15ENGARG", "yes"),)
    me = lambda e: True if e == ADV else None
    none = lambda e: None

    # ARG concentration INCREASES direction (two same-outcome entries sum).
    _t("ARG concentration sums", game_directional_mutex([DirEntry(argY, 10, True), DirEntry(argY, 10, True)], me), 20)
    # ENG balance gets justified credit (opposite outcomes net to max).
    _t("ENG balance nets", game_directional_mutex([DirEntry(argY, 10, True), DirEntry(engY, 10, True)], me), 10)
    # No ME info ⇒ fail closed to the summed magnitude.
    _t("no dims == summed", game_directional_mutex([DirEntry(argY, 10, True), DirEntry(engY, 10, True)], none), 20)
    # NO-ENG requires ARG → both need ARG → summed (no hedge).
    _t("noENG+ARG both need ARG", game_directional_mutex([DirEntry(argY, 10, True), DirEntry(noEng, 10, True)], me), 20)
    # NO-ENG (needs ARG) vs YES-ENG (needs ENG) → hedge → max.
    _t("noENG vs yesENG hedge", game_directional_mutex([DirEntry(noEng, 10, True), DirEntry(engY, 10, True)], me), 10)
    # A common (non-advance) leg loses/pressures in every branch → added to max branch.
    _t("common in every branch", game_directional_mutex([DirEntry(argY, 10, True), DirEntry(engY, 10, True), DirEntry(common, 5, True)], me), 15)
    # Non-NO side (requires False) ⇒ common ⇒ no netting.
    _t("non-NO side is common", game_directional_mutex([DirEntry(argY, 10, False), DirEntry(engY, 10, False)], me), 20)
    # A SECOND ME event (moneyline) ⇒ fail closed (unproven multiple-ME structure).
    ML = "KXWCGAME-26JUL15ENGARG"
    mlArg = (("KXWCGAME-26JUL15ENGARG-ARG", ML, "yes"),)
    me2 = lambda e: True if e in (ADV, ML) else None
    _t("two ME events fail closed", game_directional_mutex([DirEntry(argY, 10, True), DirEntry(mlArg, 10, True)], me2), 20)
    _t("empty", game_directional_mutex([], me), 0)
    # Bounds: >= max single, <= summed.
    book = [DirEntry(argY, 7, True), DirEntry(engY, 11, True), DirEntry(common, 5, True)]
    b = game_directional_mutex(book, me)
    _t("<= summed", b <= sum(e.magnitude for e in book), True)
    _t(">= max single", b >= max(e.magnitude for e in book), True)
    print("  all synthetic tests PASSED\n")


def run_monotonicity_check():
    print("=== monotonicity (mass-acceptance dominance underpinning) ===")
    import itertools
    import random
    ADV = "KXWCADVANCE-26JUL15ENGARG"
    ML = "KXWCGAME-26JUL15ENGARG"
    legsets = [
        (("KXWCADVANCE-26JUL15ENGARG-ARG", ADV, "yes"),),
        (("KXWCADVANCE-26JUL15ENGARG-ENG", ADV, "yes"),),
        (("KXWCADVANCE-26JUL15ENGARG-ENG", ADV, "no"),),
        (("KXWCTOTAL-26JUL15ENGARG-3", "KXWCTOTAL-26JUL15ENGARG", "yes"),),
        (("KXWCGAME-26JUL15ENGARG-ARG", ML, "yes"),),
    ]
    me = lambda e: True if e == ADV else None
    rng = random.Random(0xF00D)
    failures = 0
    for _ in range(20000):
        n = rng.randint(0, 7)
        entries = [DirEntry(rng.choice(legsets), rng.randint(1, 500), rng.random() < 0.5) for _ in range(n)]
        extra = DirEntry(rng.choice(legsets), rng.randint(1, 500), rng.random() < 0.5)
        base = game_directional_mutex(list(entries), me)
        more = game_directional_mutex([*entries, extra], me)
        if more < base - 1e-9:
            failures += 1
    print(f"  monotonic (adding entry never lowers bound): {'PASS' if failures == 0 else f'FAIL x{failures}'} over 20000 random books\n")
    assert failures == 0

    # Explicit subset-dominance: the all-accepted bound >= every accepted subset.
    print("=== all-accepted dominates every accepted subset ===")
    me = lambda e: True if e == ADV else None
    all_entries = [
        DirEntry(legsets[0], 12, True),
        DirEntry(legsets[1], 9, True),
        DirEntry(legsets[2], 7, True),
        DirEntry(legsets[3], 5, True),
        DirEntry(legsets[0], 3, False),
    ]
    full = game_directional_mutex(all_entries, me)
    worst_subset = 0.0
    for r in range(len(all_entries) + 1):
        for combo in itertools.combinations(all_entries, r):
            worst_subset = max(worst_subset, game_directional_mutex(list(combo), me))
    print(f"  all-accepted={full}  worst-subset={worst_subset}  dominates={full >= worst_subset}")
    assert full >= worst_subset
    print("  PASS\n")


def run_live_validation():
    print("=== live ENGARG book validation ===")
    try:
        con = sqlite3.connect(f"file:{LIVE}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        print(f"  (skipped — live db unavailable: {exc})\n")
        return
    GAME = "26JUL15ENGARG"

    def me(e):
        return True if (e and e.startswith("KXWCADVANCE")) else None

    nobid = {}
    for rid, cj in con.execute("select rfq_id,context_json from decisions where kind='quote_sent'"):
        try:
            nobid[rid] = json.loads(cj).get("no_bid_cc")
        except Exception:
            pass
    entries = []
    for kind in ("confirm", "decline"):
        for (rid,) in con.execute(f"select rfq_id from decisions where kind='{kind}'"):
            r = con.execute(
                "select legs_json,target_cost_cc,contracts_centi from rfqs where rfq_id=?",
                (rid,),
            ).fetchone()
            if not r or not r[0]:
                continue
            legs = json.loads(r[0])
            if not any(GAME in leg["market_ticker"] for leg in legs):
                continue
            nb = nobid.get(rid)
            tc, cc = r[1], r[2]
            contracts = cc / 100.0 if cc else (tc / nb if (tc and nb) else 0)
            # directional magnitude proxy: the position's contracts (|delta| bounded
            # by contracts for a whole-combo adverse move). Only THIS game's legs.
            tup = tuple(
                (leg["market_ticker"], leg.get("event_ticker", ""), leg.get("side", "yes"))
                for leg in legs
                if GAME in leg["market_ticker"]
            )
            entries.append(DirEntry(tup, contracts, True))
    summed = sum(e.magnitude for e in entries)
    mutex = game_directional_mutex(entries, me)
    ratio = (summed / mutex) if mutex else float("nan")
    print(f"  {len(entries)} ENGARG entries: summed-directional={summed:.1f}ct  "
          f"mutex-aware={mutex:.1f}ct  ({ratio:.2f}x tighter)\n")


if __name__ == "__main__":
    run_synthetic_tests()
    run_monotonicity_check()
    run_live_validation()
