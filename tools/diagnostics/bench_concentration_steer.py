"""LEVER #5 verification harness (operator directive 2026-07-27).

Two questions, answered with ACTUAL numbers off the LIVE modules (hard rule 8:
this script imports and calls the shipped code, it never reimplements it):

1. **THROUGHPUT** — what does the steer cost the QUOTE PATH? The Herfindahl
   marginal, the wall loads, the covariance read, the ladder. Benchmarked
   against the 7.16us heuristic it replaces and against the whole
   ``compute_inventory_skew`` call the lifecycle already made.

2. **BUDGET NEUTRALITY + MAGNITUDE** — mean/median ``applied_cc`` BEFORE
   (the pre-Lever-#5 composition) and AFTER, on the same synthetic flow, plus
   the implied fill-rate effect at the measured CMH-stratified elasticity
   (22% of fills lost per cent). Markups are FIXED, so the mean must not go
   negative.

Run: .venv/Scripts/python.exe tools/diagnostics/bench_concentration_steer.py
"""

from __future__ import annotations

import random
import statistics
import time

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.risk.concentration_steer import (
    SteerCenter,
    build_loss_event_book,
    elasticity_half_cc,
)
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
    max_event_worst_case_loss_dollars=100.0,
    max_event_gross_notional_dollars=500.0,
)
FILL_ELASTICITY = 0.22
TICK_CC = 10
MARGIN_CC = 200          # the measured MEDIAN live margin
BANKROLL_CC = 21_800_000  # measured live equity $2,179.74

# The measured live book: ~38 tickets / ~$411 premium / p_book ~0.58.
N_TICKETS = 38
BOOK_PREMIUM_CC = 4_110_000
# The ENFORCED walls at the live bankroll: game_loss_frac 8% and
# entity_loss_frac (the per-player wall) against $2,179.74 of equity.
GAME_WALL_CC = 0.08 * BANKROLL_CC
ENTITY_WALL_CC = 0.04 * BANKROLL_CC

SERIES = ["KXMLBKS", "KXMLBGAME", "KXMLBHR", "KXMLBTOTAL", "KXMLBRFI"]
GAMES = [f"26JUL2716{i:02d}TEAM{i}" for i in range(12)]
PLAYERS = [f"PL{i}" for i in range(20)]


def _leg(rng: random.Random) -> LegRef:
    s = rng.choice(SERIES)
    g = rng.choice(GAMES)
    p = rng.choice(PLAYERS)
    return LegRef(
        market_ticker=f"{s}-{g}-{p}-{rng.randint(1, 9)}",
        event_ticker=f"{s}-{g}",
        side=rng.choice(["yes", "no"]),
    )


def _ticket(rng: random.Random, pid: str, premium_cc: int) -> OpenPosition:
    n = rng.choice([2, 2, 3, 3, 3, 4])
    legs = tuple(_leg(rng) for _ in range(n))
    price = rng.randint(1_500, 8_000)
    contracts = max(100, int(premium_cc * 100 / price))
    return OpenPosition(
        position_id=pid,
        combo_ticker=f"COMBO-{pid}",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(contracts),
        entry_price_cc=CentiCents(price),
        legs=legs,
    )


def build_world(seed: int = 17):
    rng = random.Random(seed)
    book = ExposureBook(CONVENTIONS)
    for i in range(N_TICKETS):
        book.add_position(_ticket(rng, f"held{i}", BOOK_PREMIUM_CC // N_TICKETS))
    marginals: dict[str, float] = {}
    for pos in book.positions.values():
        for leg in pos.legs:
            marginals[leg.market_ticker] = rng.uniform(0.15, 0.85)
    return book, marginals, rng


def profiles(book: ExposureBook, snap, centre: SteerCenter):
    games = sorted({k for k in snap.worst_case_loss_by_game_cc})
    raw = {g: float(snap.worst_case_loss_by_game_cc[g]) for g in games}
    tot = sum(raw.values()) or 1.0
    pbook = PBookProfile(
        input_generation=book.position_generation,
        p_book=0.58,
        tail_share_by_game={g: v / tot for g, v in raw.items()},
        total_tail_cc=tot,
        game_budget_cc=GAME_WALL_CC,
    )
    fam = snap.committed_loss_by_family_cc
    ent = snap.committed_loss_by_entity_cc
    tf = float(sum(fam.values())) or 1.0
    te = float(sum(ent.values())) or 1.0
    legaxis = LegAxisProfile(
        shares_by_family={k: v / tf for k, v in fam.items()},
        total_family_cc=tf,
        shares_by_entity={k: v / te for k, v in ent.items()},
        total_entity_cc=te,
        family_budget_cc=GAME_WALL_CC,
        entity_budget_cc=GAME_WALL_CC,
        p_book=0.58,
    )
    conc = ConcentrationProfile(
        loss_events=build_loss_event_book(
            (ticket_bucket(p.legs), float(p.max_loss_cc))
            for p in book.positions.values()
        ),
        game_dollars_cc=raw,
        game_wall_cc=GAME_WALL_CC,
        family_dollars_cc={k: float(v) for k, v in fam.items()},
        family_wall_cc=GAME_WALL_CC,
        entity_dollars_cc={k: float(v) for k, v in ent.items()},
        entity_wall_cc=ENTITY_WALL_CC,
        fill_elasticity_per_cent=FILL_ELASTICITY,
        centre=centre,
    )
    return pbook, legaxis, conc


ARMED = SkewParams(enabled=True, pbook_armed=True, leg_axis_armed=True)


def main() -> None:
    book, marginals, rng = build_world()
    provider = marginals.get
    snap = book.snapshot(provider, mass_acceptance=True)
    centre = SteerCenter(half_life=256.0)
    pbook, legaxis, conc = profiles(book, snap, centre)
    gen = book.position_generation

    # A REALISTIC candidate mix: half land on a loss event the book already
    # holds (CONCENTRATORS — a repeat of a held ticket's leg set, which is
    # exactly the K-ladder/same-match shape), half are fresh AND-bound events
    # (DIVERSIFIERS). Anything else would only measure one side of the steer.
    held = list(book.positions.values())
    candidates = []
    for i in range(4_000):
        if i % 2 == 0:
            src = held[rng.randrange(len(held))]
            candidates.append(
                OpenPosition(
                    position_id=f"cand{i}",
                    combo_ticker=f"COMBO-cand{i}",
                    collection=None,
                    our_side=Side.NO,
                    contracts=CentiContracts(3_000),
                    entry_price_cc=CentiCents(4_000),
                    legs=src.legs,
                )
            )
        else:
            candidates.append(_ticket(rng, f"cand{i}", 120_000))

    def run(with_lever: bool, observe: bool):
        return [
            compute_inventory_skew(
                c, snap, provider, CONVENTIONS, LIMITS, ARMED,
                pbook_profile=pbook, pbook_book_generation=gen,
                leg_axis_profile=legaxis,
                conc_profile=(conc if with_lever else None),
                margin_cc=MARGIN_CC,
                tick_cc=TICK_CC,
                observe_centre=observe,
            )
            for c in candidates
        ]

    # Warm the live centre exactly as the running bot does (one observation
    # per quote), then measure with the centre frozen so BEFORE/AFTER compare
    # on identical inputs.
    run(True, True)

    t0 = time.perf_counter()
    before = run(False, False)
    t_before = (time.perf_counter() - t0) / len(candidates) * 1e6
    t0 = time.perf_counter()
    after = run(True, False)
    t_after = (time.perf_counter() - t0) / len(candidates) * 1e6

    b = [s.applied_cc for s in before]
    a = [s.applied_cc for s in after]
    hhi = [s.conc.hhi.relative for s in after if s.conc]

    def stats(xs):
        return (
            statistics.mean(xs),
            statistics.median(xs),
            statistics.median([abs(x) for x in xs]),
            min(xs),
            max(xs),
        )

    bm, bmed, bmabs, blo, bhi = stats(b)
    am, amed, amabs, alo, ahi = stats(a)
    sub_tick_before = sum(1 for x in b if x != 0 and abs(x) < TICK_CC) / len(b)
    sub_tick_after = sum(1 for x in a if x != 0 and abs(x) < TICK_CC) / len(a)
    off_lattice_after = sum(1 for x in a if x % TICK_CC != 0) / len(a)

    div = [s for s in after if s.conc and s.conc.hhi.relative > 0]
    con = [s for s in after if s.conc and s.conc.hhi.relative < 0]
    swing = (
        statistics.mean([s.applied_cc for s in div])
        - statistics.mean([s.applied_cc for s in con])
        if div and con
        else float("nan")
    )

    print("=" * 74)
    print("LEVER #5 — CONCENTRATION STEER: measured before/after")
    print("=" * 74)
    print(f"book: {N_TICKETS} tickets / ${BOOK_PREMIUM_CC/10_000:.2f} premium"
          f" / p_book 0.58 / margin {MARGIN_CC}cc / tick {TICK_CC}cc")
    print(f"effective AND-bound loss events: "
          f"{conc.loss_events.effective_events:.2f}  "
          f"(legs would have read ~"
          f"{sum(len(p.legs) for p in book.positions.values())})")
    print()
    print("applied_cc distribution (pricer frame; + = TIGHTER)")
    print(f"  {'':<10}{'mean':>9}{'median':>9}{'med|x|':>9}{'min':>8}{'max':>8}")
    print(f"  {'BEFORE':<10}{bm:9.2f}{bmed:9.1f}{bmabs:9.1f}{blo:8d}{bhi:8d}")
    print(f"  {'AFTER':<10}{am:9.2f}{amed:9.1f}{amabs:9.1f}{alo:8d}{ahi:8d}")
    print(f"  mean delta: {am - bm:+.2f}cc  "
          f"(NEGATIVE would mean the average quote WIDENED — markups are FIXED)")
    print()
    print("sub-tick annihilation (a steer < one tick is erased by snap_bid_down)")
    print(f"  BEFORE: {sub_tick_before:6.2%} of quotes carried a sub-tick steer")
    print(f"  AFTER:  {sub_tick_after:6.2%}   off-lattice: {off_lattice_after:.2%}")
    print()
    print("economic reality")
    print(f"  diversifier->concentrator swing: {swing:.1f}cc "
          f"({swing/100:.2f}c) on a {MARGIN_CC}cc margin "
          f"= {swing/MARGIN_CC:.1%} of markup")
    print(f"  hhi marginal: n={len(hhi)} "
          f"mean {statistics.mean(hhi):+.4f} "
          f"p10 {statistics.quantiles(hhi, n=10)[0]:+.4f} "
          f"p90 {statistics.quantiles(hhi, n=10)[-1]:+.4f}")
    print(f"  derived half-range bounds: elasticity "
          f"{elasticity_half_cc(FILL_ELASTICITY)}cc, margin {MARGIN_CC}cc")
    print()
    print("implied fill-rate effect @ measured elasticity "
          f"{FILL_ELASTICITY:.2f} fills lost per cent")
    print(f"  mean applied {am:+.2f}cc => "
          f"{FILL_ELASTICITY * am / 100:+.3%} expected fills "
          f"(first order, symmetric steer cancels)")
    print(f"  diversifier side  {statistics.mean([s.applied_cc for s in div]):+.1f}cc"
          f" => {FILL_ELASTICITY * statistics.mean([s.applied_cc for s in div]) / 100:+.2%}")
    if con:
        print(f"  concentrator side {statistics.mean([s.applied_cc for s in con]):+.1f}cc"
              f" => {FILL_ELASTICITY * statistics.mean([s.applied_cc for s in con]) / 100:+.2%}")
    print(f"  n diversifiers {len(div)} / n concentrators {len(con)}")
    print()
    print("QUOTE-PATH THROUGHPUT (full compute_inventory_skew, per candidate)")
    print(f"  BEFORE (pre-Lever-#5 composition): {t_before:7.2f} us")
    print(f"  AFTER  (+ Lever #5)              : {t_after:7.2f} us")
    print(f"  delta                            : {t_after - t_before:+7.2f} us"
          f"  (the 7.16us heuristic it replaces was itself dearer)")
    # The ANCHOR the delta must be judged against: the exposure snapshot the
    # SAME function (_quoting_policy) already takes once per quote.
    t0 = time.perf_counter()
    for _ in range(500):
        book.snapshot(provider, mass_acceptance=True)
    t_snap = (time.perf_counter() - t0) / 500 * 1e6
    print(f"  anchor: exposure.snapshot() in the same function: {t_snap:7.2f} us")
    print(f"  quoting-policy block: {t_snap + t_before:7.1f} -> "
          f"{t_snap + t_after:7.1f} us "
          f"({(t_after - t_before) / (t_snap + t_before):+.2%})")
    print("=" * 74)


if __name__ == "__main__":
    main()
