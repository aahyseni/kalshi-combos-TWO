"""LIVE-BOOK MEASUREMENT of the KILL-anchored book gate (2026-07-29).

Rule 8 (testing isolation): this script IMPORTS and DRIVES the live modules —
``sim.book_model.build_book_model``, ``sim.book_risk.compute_book_risk`` and the
live ``risk.limits.LimitChecker`` — and edits nothing. The store is opened
READ-ONLY.

WHAT IT ANSWERS (the operator's four questions)

  1. P(KILL-night) at TODAY's live book, measured at the ratified 12% line off
     the same loss-quantile envelope ``risk/limits`` §(8a) gates on.
  2. The ADMISSIBLE PREMIUM under the re-anchored gate vs the $1,058.50 dollar
     wall in force today — found by GROWING the live book one realistic ticket
     at a time and asking the LIVE ``LimitChecker`` where each regime stops.
  3. Ticket count / EV / ES-per-premium / effective-N at each stop.
  4. Where the demoted det-max backstop sits relative to both.

THE ONE PROXY, stated plainly. The offline store has no leg order books, so the
per-leg marginals a live MC reads are unavailable. Each position's own traded
price IS a market-implied statement about its parlay: for a k-leg long-NO combo
bought at price P, the implied parlay-hit probability is (1 - P) and the implied
per-leg marginal is (1 - P) ** (1/k) under the same independence the book model
uses across games. That is the number used here. It is a CAPACITY study — how
much book each regime admits — not a pricing claim, and every regime below is
graded on the SAME marginals, so the comparison is apples to apples.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from fractions import Fraction
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for _p in (str(REPO), str(REPO / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from combomaker.core.conventions import Conventions, Side  # noqa: E402
from combomaker.core.money import CentiCents  # noqa: E402
from combomaker.core.quantity import CentiContracts  # noqa: E402
from combomaker.risk.exposure import (  # noqa: E402
    ExposureBook,
    LegRef,
    OpenPosition,
)
from combomaker.risk.limits import (  # noqa: E402
    DailyPnl,
    LimitChecker,
    ReasonCode,
    RiskLimits,
    threshold_cc,
)
from combomaker.sim.book_model import build_book_model  # noqa: E402
from combomaker.sim.book_risk import compute_book_risk  # noqa: E402

# The store lives in the PRODUCTION tree; this worktree carries no data dir.
# Same override the vitals tooling honors, so one env var points every
# read-only diagnostic at the tape (never hardcode a checkout's layout).
_DATA_DIR = Path(os.environ.get("VITALS_DATA_DIR") or (REPO / "data"))
DB_DEFAULT = _DATA_DIR / "combomaker-prod-live-wc.sqlite3"

CONV = Conventions(
    verified=True, source="live-measure",
    maker_side_on_yes_accept=Side.YES, maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True, maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)

# The RATIFIED anchors (layer 2 of the North Star) and the wall in force today.
KILL_FRAC = Fraction(12, 100)
KILL_TAIL_PROB = 0.02
DET_MAX_FRAC = Fraction(36, 100)      # live config, hand-set
CVAR_FRAC = Fraction(35, 100)         # live config, hand-set
RUIN_FLOOR_FRAC = Fraction(70, 100)   # ratified ruin floor -> backstop


def load_positions(db: Path) -> list[OpenPosition]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "select position_id, combo_ticker, collection_ticker, our_side, "
        "contracts_centi, entry_price_cc, legs_json from position_ledger "
        "where status='open'"
    ).fetchall()
    con.close()
    out: list[OpenPosition] = []
    for pid, ticker, coll, side, contracts, price, legs_json in rows:
        out.append(
            OpenPosition(
                position_id=pid, combo_ticker=ticker, collection=coll,
                our_side=Side.NO if side == "no" else Side.YES,
                contracts=CentiContracts(int(contracts)),
                entry_price_cc=CentiCents(int(price)),
                legs=tuple(
                    LegRef(x["market_ticker"], x.get("event_ticker"), x["side"])
                    for x in json.loads(legs_json)
                ),
            )
        )
    return out


def implied_marginals(positions: list[OpenPosition]) -> dict[str, float]:
    """Per-leg implied marginal from each position's own traded price (see the
    module docstring). Legs shared by several positions take the MEAN of the
    implied values, so one aggressive print cannot set a leg's probability."""
    acc: dict[str, list[float]] = {}
    for p in positions:
        k = max(1, len(p.legs))
        implied_hit = max(1e-4, min(0.9999, 1.0 - p.entry_price_cc / 10_000.0))
        per_leg = implied_hit ** (1.0 / k)
        for leg in p.legs:
            acc.setdefault(leg.market_ticker, []).append(per_leg)
    return {t: sum(v) / len(v) for t, v in acc.items()}


def effective_n(positions: list[OpenPosition]) -> float:
    """EFFECTIVE INDEPENDENT BETS = inverse Herfindahl of premium aggregated
    PER GAME. Model-free (it reads dollars, not a copula) and it collapses
    same-game tickets into one bet, which is the whole point: 40 tickets on 3
    games is 3 bets. N_eff = (sum w_g)^2 / sum w_g^2 with w_g the game's
    premium; equals the game count for an evenly spread book and 1 for a
    one-way one."""
    by_game: dict[str, int] = {}
    for p in positions:
        key = p.legs[0].event_ticker or p.legs[0].market_ticker if p.legs else "?"
        by_game[key] = by_game.get(key, 0) + p.max_loss_cc
    w = list(by_game.values())
    if not w:
        return 0.0
    return (sum(w) ** 2) / sum(x * x for x in w)


def measure(book: list[OpenPosition], marg: dict[str, float], bankroll: int,
            n_samples: int, seed: int):
    model = build_book_model(
        book, marginals=lambda t: marg.get(t, 0.25), within_game_rho=None
    )
    return compute_book_risk(
        model, n_samples=n_samples, seed=seed, bankroll_cc=bankroll
    )


def p_kill(snap, kill_line_cc: int) -> float:
    q = snap.loss_quantiles_cc
    if not q:
        return float("nan")
    return sum(1 for x in q if x >= kill_line_cc) / (len(q) - 1)


def _limits(*, armed: bool) -> RiskLimits:
    return RiskLimits(
        caps_shadow_mode=False,
        portfolio_tail_prob_gate=True,
        portfolio_kill_tail_prob=KILL_TAIL_PROB,
        portfolio_cvar_frac=CVAR_FRAC,
        portfolio_det_max_frac=DET_MAX_FRAC,
        hard_trip_frac=KILL_FRAC,
        kill_anchored_book_gate=armed,
        # Only the two PORTFOLIO axes are under study; the deploy-side caps
        # (per-combo / game / slate / directional) are the refusal layer and are
        # measured elsewhere. Set wide so this study isolates the envelope.
        per_combo_loss_frac=Fraction(1, 1),
        game_loss_frac=Fraction(1, 1),
        slate_loss_frac=Fraction(1, 1),
        directional_frac=Fraction(1, 1),
    )


def portfolio_breaches(limits: RiskLimits, snap, bankroll: int) -> set[str]:
    out = LimitChecker(limits).check(
        ExposureBook(CONV), lambda _t: 0.5, DailyPnl(),
        risk_bankroll_cc=bankroll, book_risk=snap,
    )
    return {
        b.reason.value
        for b in out
        if b.reason
        in (ReasonCode.SKIP_PORTFOLIO_CVAR, ReasonCode.SKIP_PORTFOLIO_DET_MAX)
    }


def synth_ticket(i: int, template: OpenPosition) -> OpenPosition:
    """One more realistic ticket, on a game the book does NOT already hold —
    the exact flow the operator complained is being turned away ("games we
    don't even have on our books yet"). Size and price are copied from a real
    live position so the growth path is the book's own ticket distribution."""
    return OpenPosition(
        position_id=f"synth-{i}",
        combo_ticker=f"SYNTH-{i}",
        collection=None,
        our_side=Side.NO,
        contracts=template.contracts,
        entry_price_cc=template.entry_price_cc,
        legs=tuple(
            LegRef(f"SYNTH{i}-L{j}", f"KXSYNTH-G{i}", "yes")
            for j in range(len(template.legs))
        ),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bankroll-cc", type=int, default=29_985_806,
                    help="live exchange_equity_cc (2026-07-31 17:23 boot; "
                         "pass the current boot's value for a fresh read)")
    ap.add_argument("--db", type=Path, default=DB_DEFAULT,
                    help="position store (read-only); default honors "
                         "VITALS_DATA_DIR")
    ap.add_argument("--samples", type=int, default=40_000)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--max-tickets", type=int, default=140)
    ap.add_argument(
        "--flow", choices=("median", "rotate"), default="rotate",
        help="shape of the arriving flow: 'rotate' cycles the book's OWN 24 "
             "live prints (the realistic mix); 'median' repeats the median "
             "print (a single-shape stress).",
    )
    args = ap.parse_args()

    bank = args.bankroll_cc
    kill_line = threshold_cc(KILL_FRAC, bank)
    wall = threshold_cc(DET_MAX_FRAC, bank)
    backstop = threshold_cc(RUIN_FLOOR_FRAC, bank)
    cvar_thr = threshold_cc(CVAR_FRAC, bank)

    live = load_positions(args.db)
    marg = implied_marginals(live)

    print("=" * 78)
    print("KILL-ANCHORED BOOK GATE — LIVE-BOOK MEASUREMENT")
    print("=" * 78)
    print(f"bankroll (exchange equity)      ${bank / 10_000:>10,.2f}")
    print(f"KILL line   hard_trip 0.12      ${kill_line / 10_000:>10,.2f}"
          f"   <- the ratified anchor")
    print(f"CVaR thr    cvar_frac 0.35      ${cvar_thr / 10_000:>10,.2f}"
          f"   <- what the gate ACTUALLY tests today")
    print(f"det-max wall det_max 0.36       ${wall / 10_000:>10,.2f}"
          f"   <- the dollar cap doing all the governing")
    print(f"backstop    ruin_floor 0.70     ${backstop / 10_000:>10,.2f}"
          f"   <- the demoted model-failure backstop")
    print(f"cvar/det ratio                  {cvar_thr / wall:>10.4f}"
          f"   <- fraction of the COMONOTONE MAX 2% must reach")
    print()

    snap = measure(live, marg, bank, args.samples, args.seed)
    premium = sum(p.max_loss_cc for p in live)
    print("--- TODAY'S LIVE BOOK " + "-" * 55)
    print(f"open positions                  {len(live)}")
    print(f"premium (= det-max)             ${premium / 10_000:>10,.2f}")
    print(f"comonotone det-max              "
          f"${snap.deterministic_max_loss_cc / 10_000:>10,.2f}"
          f"   ratio {snap.deterministic_max_loss_cc / max(1, premium):.6f}")
    print(f"governing ES_0.99               "
          f"${snap.governing_model_es_99_cc / 10_000:>10,.2f}")
    print(f"P(KILL-night) at the 12% line   {p_kill(snap, kill_line):>10.5f}"
          f"   budget {KILL_TAIL_PROB:.2f}")
    print(f"P(loss >= the 0.35 thr) today   {p_kill(snap, cvar_thr):>10.5f}"
          f"   <- what the armed gate tests")
    print(f"det-max utilisation vs wall     {premium / wall:>10.1%}")
    print(f"det-max utilisation vs backstop {premium / backstop:>10.1%}")
    print(f"effective independent bets      {effective_n(live):>10.2f}")
    print()

    # --- growth curve --------------------------------------------------------
    templates = sorted(live, key=lambda p: p.max_loss_cc)
    median = templates[len(templates) // 2]
    print("--- GROWTH: one more ticket at a time, on games we do not hold ----")
    print(f"synthetic ticket = live median print "
          f"(${median.max_loss_cc / 10_000:,.2f}, "
          f"{len(median.legs)} legs, px {median.entry_price_cc})")
    print()
    hdr = (f"{'n':>4} {'premium':>11} {'EV':>9} {'ES99':>9} {'ES/prem':>8} "
           f"{'P(KILL)':>8} {'N_eff':>7}  dollars-regime   re-anchored")
    print(hdr)
    print("-" * len(hdr))

    book = list(live)
    marg2 = dict(marg)
    last_ok: dict[str, dict[str, object]] = {}
    stopped_by: dict[str, list[str]] = {}
    for i in range(args.max_tickets):
        t = synth_ticket(i, templates[i % len(templates)]
                         if args.flow == "rotate" else median)
        src = templates[i % len(templates)] if args.flow == "rotate" else median
        per_leg = (
            (1.0 - src.entry_price_cc / 10_000.0) ** (1.0 / max(1, len(src.legs)))
        )
        for leg in t.legs:
            marg2[leg.market_ticker] = per_leg
        book.append(t)
        s = measure(book, marg2, bank, args.samples, args.seed)
        prem = sum(p.max_loss_cc for p in book)
        es = s.governing_model_es_99_cc
        pk = p_kill(s, kill_line)
        row = dict(n=len(book), premium=prem, ev=s.ev_cc, es=es, pk=pk,
                   neff=effective_n(book))
        dollars = portfolio_breaches(_limits(armed=False), s, bank)
        anchored = portfolio_breaches(_limits(armed=True), s, bank)
        # The regime's answer is the LAST book it ADMITTED, not the first it
        # refused — that is the admissible premium the operator asked for.
        if not dollars:
            last_ok["dollars"] = row
        elif "dollars" not in stopped_by:
            stopped_by["dollars"] = sorted(dollars)
        if not anchored:
            last_ok["anchored"] = row
        elif "anchored" not in stopped_by:
            stopped_by["anchored"] = sorted(anchored)
        if i % 5 == 0 or dollars or anchored:
            print(f"{len(book):>4} ${prem / 10_000:>10,.2f} "
                  f"${s.ev_cc / 10_000:>8,.2f} ${es / 10_000:>8,.2f} "
                  f"{es / max(1, prem):>8.3f} {pk:>8.4f} "
                  f"{effective_n(book):>7.2f}  "
                  f"{'REFUSED' if dollars else 'ok':<16} "
                  f"{'REFUSED' if anchored else 'ok'}")
        if anchored and dollars:
            break

    print()
    print("--- THE TWO REGIMES, SIDE BY SIDE " + "-" * 43)
    d = last_ok.get("dollars")
    a = last_ok.get("anchored")
    if not (d and a):
        print("no stop reached within --max-tickets; raise it")
        return 0
    rows = [
        ("tickets admitted", f"{d['n']:,}", f"{a['n']:,}"),
        ("premium", f"${d['premium'] / 10_000:,.2f}",
         f"${a['premium'] / 10_000:,.2f}"),
        ("EV", f"${d['ev'] / 10_000:,.2f}", f"${a['ev'] / 10_000:,.2f}"),
        ("ES_0.99", f"${d['es'] / 10_000:,.2f}", f"${a['es'] / 10_000:,.2f}"),
        ("ES per premium", f"{d['es'] / max(1, d['premium']):.3f}",
         f"{a['es'] / max(1, a['premium']):.3f}"),
        ("P(KILL-night)", f"{d['pk']:.4f}", f"{a['pk']:.4f}"),
        ("effective N", f"{d['neff']:.2f}", f"{a['neff']:.2f}"),
        ("stopped by", ",".join(stopped_by.get("dollars", ["--"])),
         ",".join(stopped_by.get("anchored", ["--"]))),
    ]
    print(f"{'':<24}{'enforce-dollars':>22}{'re-anchored':>26}")
    for label, dv, av in rows:
        print(f"{label:<24}{dv:>22}{av:>26}")
    print()
    ev_gain = (a["ev"] / max(1.0, float(d["ev"]))) - 1.0  # type: ignore[arg-type]
    d_ratio = float(d["es"]) / max(1.0, float(d["premium"]))
    a_ratio = float(a["es"]) / max(1.0, float(a["premium"]))
    print(f"EV change                       {ev_gain:>+10.1%}")
    print(f"ES-per-premium change           {a_ratio / max(1e-9, d_ratio) - 1:>+10.1%}")
    print(f"P(KILL-night) at the new stop   {a['pk']:>10.4f}"
          f"   budget {KILL_TAIL_PROB:.2f}  "
          f"{'INSIDE' if float(a['pk']) <= KILL_TAIL_PROB else 'OVER'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
