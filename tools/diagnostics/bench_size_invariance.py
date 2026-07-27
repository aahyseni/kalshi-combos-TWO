"""PROBLEM 2 verification harness (operator directive 2026-07-27).

Answers the questions the size-invariance repair owes, with ACTUAL numbers off
the LIVE modules (hard rule 8: this script imports and calls the shipped code,
it never reimplements it):

1. **THROUGHPUT ON THE QUOTE PATH**, on a LIVE-SHAPED book: 16 family keys, 79
   entity keys, 8 tail games, 8 peak games - the exact key counts measured on
   ``data/live_20260727_1023.log`` at 13:00 ET. HEAD's ``risk/skew.py`` is
   loaded verbatim from git as a second module, so this is a real A/B and not a
   recollection.

   **2026-07-27 REVIEW FINDING B4 - THIS BENCH USED TO LIE.** It never passed
   ``conc_profile``, so it benchmarked a composition that is not what ships: it
   printed "HEAD 19.97us -> NEW 15.05us, -24.6%" and would have told the
   operator throughput IMPROVED while the shipping path had regressed. It now
   measures FOUR configurations - the three that can ship plus HEAD - and the
   one labelled SHIPPED is the one the restart actually runs:

     HEAD     the code the live process is running (95588f7)
     OFF      ``conc_enabled=False`` - the zero-cost rollback
     SHIPPED  ``conc_enabled=True, conc_armed=False`` - computed + logged,
              never priced. THIS IS WHAT SHIPS.
     ARMED    ``conc_armed=True`` - the cost the operator weighs when arming.

   The lifecycle-side profile build is measured separately below, because
   ``compute_inventory_skew`` is not the whole per-quote cost of the steer.

2. **THE OPERATOR'S RULE, ON THE SAME BOOK.** The identical candidate against
   the identical book COMPOSITION scaled 1x / 3x / 10x / 100x - HEAD's numbers
   move, the repaired ones do not - checked in the SHIPPED and the ARMED
   composition, since both must obey it.

Run:
    .venv/Scripts/python.exe tools/diagnostics/bench_size_invariance.py
"""

from __future__ import annotations

import dataclasses
import importlib.util
import math
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.risk.concentration_steer import SteerCenter, build_loss_event_book
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.skew import (
    ConcentrationProfile,
    LegAxisProfile,
    PBookProfile,
    SkewLimits,
    SkewParams,
    compute_inventory_skew,
    ticket_bucket,
)

REPO = Path(__file__).resolve().parents[2]

CONVENTIONS = Conventions(
    verified=True,
    source="bench",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)
LIMITS = SkewLimits(
    max_event_delta_contracts=500.0,
    max_event_worst_case_loss_dollars=1_000.0,
    max_event_gross_notional_dollars=5_000.0,
)
# THE THREE CONFIGURATIONS THAT CAN SHIP. ``SHIPPED`` is the live YAML shape
# (prod-live-wc has pbook_armed and leg_axis_armed true) with the Lever-#5 steer
# in its shipped state: SHADOW (2026-07-27 review B3).
SHIPPED = SkewParams(enabled=True, pbook_armed=True, leg_axis_armed=True)
ARMED = dataclasses.replace(SHIPPED, conc_armed=True)
OFF = dataclasses.replace(SHIPPED, conc_enabled=False)
GEN = 40

# Measured live key counts (window B, 13:00 ET, det_max $596.17).
N_FAMILY, N_ENTITY, N_GAMES = 16, 79, 8
# The ENFORCED walls at the live bankroll (they do NOT scale with the book -
# that asymmetry is exactly what used to leak book size into price).
GAME_WALL_CC = 2_761_344.0
ENTITY_WALL_CC = 1_035_504.0
FILL_ELASTICITY = 0.22      # measured CMH-stratified fill-rate elasticity
MARGIN_CC = 200             # measured MEDIAN live margin
TICK_CC = 10


def load_head_skew():
    """HEAD's ``risk/skew.py``, imported as its own module for a true A/B."""
    src = subprocess.run(
        ["git", "show", "HEAD:src/combomaker/risk/skew.py"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout
    tmp = Path(tempfile.mkdtemp()) / "head_skew.py"
    tmp.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("head_skew", tmp)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["head_skew"] = mod
    spec.loader.exec_module(mod)
    return mod


def leg(market: str, event: str, side: str = "yes") -> LegRef:
    return LegRef(market_ticker=market, event_ticker=event, side=side)


def build(scale: float, centre: SteerCenter | None = None):
    """A live-shaped book at ``scale`` x the committed size, plus the profiles.

    Composition is FIXED; only the dollar size scales - the exact experiment the
    operator's rule is about."""
    book = ExposureBook(CONVENTIONS)
    fams = ["KXMLBKS", "KXMLBGAME", "KXMLBTOTAL", "KXMLBHIT", "KXMLBTB",
            "KXMLBRBI", "KXMLBOUTS", "KXLOLGAME"]
    games = [f"KXMLB-G{i}" for i in range(N_GAMES)]
    marginals: dict[str, float] = {}
    # 8 families x 2 sides = 16 family keys; 79 distinct entities; 8 games -
    # the measured live window-B shape. Base contract counts are large so the
    # integer rounding of ``scale`` cannot perturb the COMPOSITION (which must
    # be held fixed - only the SIZE is the experiment).
    for i in range(79):
        f = fams[i % len(fams)]
        g = games[i % len(games)]
        side = "yes" if i % 2 == 0 else "no"
        t = f"{f}-26JUL27{g[-2:]}-ENT{i:03d}-{i % 4}"
        marginals[t] = 0.5
        book.add_position(
            OpenPosition(
                position_id=f"p{i}",
                combo_ticker=f"C{i}",
                collection=None,
                our_side=Side.NO,
                contracts=CentiContracts(int(round((100_000 + 9_000 * i) * scale))),
                entry_price_cc=CentiCents(5_000),
                legs=(leg(t, g, side),),
            )
        )
    snap = book.snapshot(lambda t: marginals.get(t), mass_acceptance=False)
    fam = snap.committed_loss_by_family_cc
    ent = snap.committed_loss_by_entity_cc
    tf, te = float(sum(fam.values())), float(sum(ent.values()))
    gm = {k: float(v) for k, v in snap.worst_case_loss_by_game_cc.items()}
    tg = sum(gm.values())
    fam_shares = {k: v / tf for k, v in fam.items()}
    ent_shares = {k: v / te for k, v in ent.items()}
    tail_shares = {k: v / tg for k, v in gm.items()}
    leg_axis = LegAxisProfile(
        shares_by_family=fam_shares,
        total_family_cc=tf,
        shares_by_entity=ent_shares,
        total_entity_cc=te,
        family_budget_cc=GAME_WALL_CC,    # the enforced walls do NOT scale
        entity_budget_cc=ENTITY_WALL_CC,
        p_book=0.5632,                    # measured, window B
        # Published exactly as the lifecycle publishes it (once per position
        # generation, off the quote path).
        hhi_family=math.fsum(s * s for s in fam_shares.values()),
        hhi_entity=math.fsum(s * s for s in ent_shares.values()),
    )
    pbook = PBookProfile(
        input_generation=GEN,
        p_book=0.5632,
        tail_share_by_game=tail_shares,
        total_tail_cc=tg,
        game_budget_cc=GAME_WALL_CC,
        tail_hhi=math.fsum(s * s for s in tail_shares.values()),
    )
    # LEVER #5 PROFILE, built exactly as ``lifecycle._concentration_profile``
    # does (keep in sync with rfq/lifecycle.py::_concentration_profile).
    conc = ConcentrationProfile(
        loss_events=build_loss_event_book(
            (ticket_bucket(p.legs), float(p.max_loss_cc))
            for p in book.positions.values()
        ),
        game_dollars_cc=gm,
        game_wall_cc=GAME_WALL_CC,
        family_dollars_cc={k: float(v) for k, v in fam.items()},
        family_wall_cc=GAME_WALL_CC,
        entity_dollars_cc={k: float(v) for k, v in ent.items()},
        entity_wall_cc=ENTITY_WALL_CC,
        fill_elasticity_per_cent=FILL_ELASTICITY,
        centre=centre if centre is not None else SteerCenter(half_life=256.0),
    )
    cand = OpenPosition(
        position_id="cand",
        combo_ticker="CAND",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(2_000),
        entry_price_cc=CentiCents(5_000),
        legs=(leg("KXMLBKS-26JUL27G0-ENT000-0", games[0], "yes"),
              leg("KXMLBGAME-26JUL27G1-ENT009-1", games[1], "no")),
    )
    return book, snap, marginals, leg_axis, pbook, conc, cand


def new_skew(params, snap, marginals, leg_axis, pbook, conc, cand, *, observe=False):
    """The SHIPPING call shape - every profile the lifecycle passes, including
    ``conc_profile``. Omitting it was the B4 defect."""
    return compute_inventory_skew(
        cand, snap, lambda t: marginals.get(t), CONVENTIONS, LIMITS, params,
        pbook_profile=pbook, pbook_book_generation=GEN,
        leg_axis_profile=leg_axis,
        conc_profile=conc,
        margin_cc=MARGIN_CC, tick_cc=TICK_CC,
        observe_centre=observe,
    )


def head_skew(head, snap, marginals, leg_axis, pbook, cand):
    return head.compute_inventory_skew(
        cand, snap, lambda t: marginals.get(t), CONVENTIONS,
        head.SkewLimits(500.0, 1_000.0, 5_000.0),
        head.SkewParams(enabled=True, pbook_armed=True, leg_axis_armed=True),
        pbook_profile=head.PBookProfile(
            input_generation=GEN, p_book=pbook.p_book,
            tail_share_by_game=pbook.tail_share_by_game,
            total_tail_cc=pbook.total_tail_cc,
            game_budget_cc=pbook.game_budget_cc,
        ),
        pbook_book_generation=GEN,
        leg_axis_profile=head.LegAxisProfile(
            shares_by_family=leg_axis.shares_by_family,
            total_family_cc=leg_axis.total_family_cc,
            shares_by_entity=leg_axis.shares_by_entity,
            total_entity_cc=leg_axis.total_entity_cc,
            budget_cc=leg_axis.family_budget_cc,
            p_book=leg_axis.p_book,
        ),
    )


def timeit(fn, n=4_000, reps=5):
    """BEST-of-``reps`` medians: a Windows scheduler hiccup must not be read as
    a throughput regression."""
    fn()
    best = float("inf")
    for _ in range(reps):
        t0 = time.perf_counter()
        for _ in range(n):
            fn()
        best = min(best, (time.perf_counter() - t0) / n * 1e6)
    return best


def main() -> None:
    head = load_head_skew()
    print("=" * 78)
    print("PROBLEM 2 - size-invariance repair + LEVER #5 arming: LIVE modules")
    print("=" * 78)
    centre = SteerCenter(half_life=256.0)
    base = build(1.0, centre)
    _book, snap, marginals, leg_axis, pbook, conc, cand = base
    print(f"book shape: {len(snap.committed_loss_by_family_cc)} family keys / "
          f"{len(snap.committed_loss_by_entity_cc)} entity keys / "
          f"{len(snap.worst_case_loss_by_game_cc)} games "
          f"(live window B: {N_FAMILY}/{N_ENTITY}/{N_GAMES})")
    # Warm the live centre exactly as the running bot does (one observation per
    # quote) so the ARMED magnitudes below are the real ones. It has to be VARIED
    # flow: warming on one repeated candidate leaves the centre with sd = 0,
    # which is not a book the bot ever sees and makes ``squash`` degenerate.
    games_w = [f"KXMLB-G{i}" for i in range(N_GAMES)]
    warm = [
        OpenPosition(
            position_id=f"w{i}", combo_ticker=f"W{i}", collection=None,
            our_side=Side.NO, contracts=CentiContracts(1_000 + 300 * i),
            entry_price_cc=CentiCents(3_000 + 100 * (i % 17)),
            legs=(leg(f"KXMLBKS-26JUL27G{i % N_GAMES}-ENT{i % 79:03d}-{i % 4}",
                      games_w[i % N_GAMES]),),
        )
        for i in range(40)
    ] + [cand]
    for _ in range(20):
        for w in warm:
            new_skew(ARMED, snap, marginals, leg_axis, pbook, conc, w,
                     observe=True)

    print()
    print("QUOTE-PATH THROUGHPUT - compute_inventory_skew, per candidate")
    print("  (B4 fix: every NEW row passes conc_profile - what actually ships)")
    t_head = timeit(lambda: head_skew(head, snap, marginals, leg_axis, pbook, cand))
    t_off = timeit(
        lambda: new_skew(OFF, snap, marginals, leg_axis, pbook, conc, cand))
    t_shadow = timeit(
        lambda: new_skew(SHIPPED, snap, marginals, leg_axis, pbook, conc, cand))
    t_armed = timeit(
        lambda: new_skew(ARMED, snap, marginals, leg_axis, pbook, conc, cand))
    rows = [
        ("HEAD    (live process, 95588f7)", t_head),
        ("OFF     (conc_enabled=False)   ", t_off),
        ("SHIPPED (conc computed, SHADOW)", t_shadow),
        ("ARMED   (conc prices)          ", t_armed),
    ]
    for label, t in rows:
        print(f"  {label}: {t:8.2f} us   "
              f"vs HEAD {t - t_head:+7.2f} us ({(t / t_head - 1) * 100:+6.1f}%)")
    print(f"  cost of COMPUTING the steer (SHIPPED - OFF): "
          f"{t_shadow - t_off:+.2f} us")
    print(f"  cost of ARMING it          (ARMED - SHIPPED): "
          f"{t_armed - t_shadow:+.2f} us")

    # THE ANCHOR the delta must be judged against: the exposure snapshot the
    # SAME function (_quoting_policy) already takes once per quote.
    t_snap = timeit(
        lambda: _book.snapshot(lambda t: marginals.get(t), mass_acceptance=False),
        n=400, reps=3,
    )
    print(f"  anchor: exposure.snapshot() in the same function: {t_snap:8.2f} us")
    print("  quoting-policy block (snapshot + skew):")
    for label, t in rows:
        print(f"    {label}: {t_snap + t:8.1f} us  "
              f"({(t - t_head) / (t_snap + t_head) * 100:+.2f}% vs HEAD)")

    # THE OTHER HALF OF THE PER-QUOTE COST: the lifecycle builds the profile on
    # every quote (three dict copies + the generation-keyed loss-event cache
    # read). Keep in sync with rfq/lifecycle.py::_concentration_profile.
    fam = snap.committed_loss_by_family_cc
    ent = snap.committed_loss_by_entity_cc
    gm = {k: float(v) for k, v in snap.worst_case_loss_by_game_cc.items()}
    events = conc.loss_events

    def build_profile():
        return ConcentrationProfile(
            loss_events=events,
            game_dollars_cc={
                k: float(v) for k, v in snap.worst_case_loss_by_game_cc.items()
            },
            game_wall_cc=GAME_WALL_CC,
            family_dollars_cc={k: float(v) for k, v in fam.items()},
            family_wall_cc=GAME_WALL_CC,
            entity_dollars_cc={k: float(v) for k, v in ent.items()},
            entity_wall_cc=ENTITY_WALL_CC,
            fill_elasticity_per_cent=FILL_ELASTICITY,
            centre=centre,
        )

    t_profile = timeit(build_profile)
    _ = gm
    print(f"  lifecycle-side profile build (SHIPPED/ARMED only, 0 when OFF): "
          f"{t_profile:8.2f} us")
    print(f"  => TOTAL per quote vs HEAD:  SHIPPED "
          f"{t_shadow + t_profile - t_head:+7.2f} us   ARMED "
          f"{t_armed + t_profile - t_head:+7.2f} us")

    print()
    print("THE OPERATOR'S RULE - identical candidate, book COMPOSITION fixed,")
    print("book SIZE scaled. HEAD moves; the repair does not - in BOTH the")
    print("SHIPPED (shadow) and the ARMED composition.")
    print(f"  {'scale':>7} {'det-ish $':>11} | {'HEAD':>7} | {'SHIP':>7}"
          f" {'ARMED':>7} | {'conc_cc':>8}")
    head_vals, ship_vals, armed_vals, conc_vals = set(), set(), set(), set()
    for scale in (0.25, 1.0, 3.0, 10.0, 100.0):
        _b, sn, mg, la, pb, cn, cd = build(scale, centre)
        h = head_skew(head, sn, mg, la, pb, cd)
        s = new_skew(SHIPPED, sn, mg, la, pb, cn, cd)
        a = new_skew(ARMED, sn, mg, la, pb, cn, cd)
        gross = sum(sn.committed_loss_by_family_cc.values()) / 1e4
        head_vals.add((h.peak_cc, h.pbook_cc, h.family_cc, h.entity_cc))
        ship_vals.add((s.peak_cc, s.pbook_cc, s.family_cc, s.entity_cc))
        armed_vals.add((a.peak_cc, a.pbook_cc, a.family_cc, a.entity_cc))
        conc_vals.add(a.conc.skew_cc if a.conc else None)
        print(f"  {scale:7.2f} {gross:11,.0f} | {h.applied_cc:7} |"
              f" {s.applied_cc:7} {a.applied_cc:7} |"
              f" {(a.conc.skew_cc if a.conc else 0):8}")
    print(f"  distinct readings across the sweep: HEAD {len(head_vals)}"
          f"   SHIPPED {len(ship_vals)}   ARMED {len(armed_vals)}"
          f"   conc {len(conc_vals)}")
    assert len(ship_vals) == 1, ship_vals
    assert len(armed_vals) == 1, armed_vals
    assert len(conc_vals) == 1, conc_vals
    print("  => the repaired concentration axes AND the Lever-#5 steer are")
    print("     EXACTLY invariant to book size")

    print()
    print("ORDERING - a diversifier must be strictly TIGHTER than a concentrator")
    _b, sn, mg, la, pb, cn, _c = build(1.0, centre)
    games = [f"KXMLB-G{i}" for i in range(N_GAMES)]

    def cand_on(ticker, game):
        return OpenPosition(
            position_id="x", combo_ticker="X", collection=None,
            our_side=Side.NO, contracts=CentiContracts(2_000),
            entry_price_cc=CentiCents(5_000), legs=(leg(ticker, game),),
        )

    for label, params in (("SHIPPED", SHIPPED), ("ARMED  ", ARMED)):
        c = new_skew(params, sn, mg, la, pb, cn,
                     cand_on("KXMLBKS-26JUL27G0-ENT072-0", games[0]))
        d = new_skew(params, sn, mg, la, pb, cn,
                     cand_on("KXNEW-26JUL27G9-ENT999-0", "KXMLB-G99"))
        print(f"  {label}  concentrator {c.applied_cc:6}   "
              f"diversifier {d.applied_cc:6}   gap {d.applied_cc - c.applied_cc:6}")
        assert d.applied_cc > c.applied_cc
    print("  => strictly tighter in both. OK")

    print()
    print("SMOOTHNESS - 40-step geometric sweep of book size, 0.25x .. 1800x")
    for label, params in (("SHIPPED", SHIPPED), ("ARMED  ", ARMED)):
        vals = []
        for i in range(40):
            _b, sn, mg, la, pb, cn, cd = build(0.25 * (1.25 ** i), centre)
            n = new_skew(params, sn, mg, la, pb, cn, cd)
            vals.append(n.applied_cc)
        print(f"  {label}  distinct applied_cc: {len(set(vals))}  "
              f"(min {min(vals)}, max {max(vals)})  => no step, no cliff")
        assert len(set(vals)) == 1
    print("=" * 78)


if __name__ == "__main__":
    main()
