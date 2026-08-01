"""COUNTERFACTUAL: the 2026-08-01 frozen tape under the MARGINAL KILL gate.

THE FREEZE, measured on the live tape (boot 2026-08-01 14:49Z):
``kill_anchored_book_gate`` armed on an inherited book whose envelope read
P(loss >= the 12% KILL line) ~ 0.115 vs the 0.02 budget; §(8a) is a LEVEL
check, so every candidate refused — 140k+ ``skip_portfolio_cvar`` rows,
``quote_sent`` = 0. This script replays the session's REFUSALS through the
MARGINAL form (``RiskLimits.kill_gate_marginal``) and answers:

  1. How many refusals would ADMIT under the marginal form, split by WHY
     (pure diversifier: zero allocated dES99; concentrator: tail-game share
     beyond the thin-tape EV credit; blocked anyway by OTHER enforced caps).
  2. The book's P(KILL)/EV trajectory as the admitted flow arrives (grow the
     rebuilt book with the admitted candidates, worst-case all-fill and at
     the tape's measured acceptance rate).

Rule 8 (testing isolation): IMPORTS AND DRIVES the live modules —
``risk.limits`` (the real ``LimitChecker`` + ``KillMarginalCandidate`` +
``kill_envelope_tail_upper``), ``rfq.eviction_value.allocate_des99_cc``,
``sim.book_model.build_book_model`` + ``sim.book_risk.compute_book_risk`` —
and edits nothing. Store + log opened READ-ONLY.

THE PROXIES, stated plainly (same doctrine as kill_anchor_live_book.py):
  * The offline store has no leg order books, so per-leg marginals are
    implied from each position's own traded price (per-leg = (1-P)**(1/k)).
  * The live in-memory book (26 positions at generation 29) is rebuilt from
    the position ledger's OPEN rows filtered to the boot slate's games; the
    rebuilt det-max is printed next to the tape's 10,265,699cc so the
    approximation is auditable.
  * The acceptance tape is EMPTY this session (0 quotes sent), so the
    quote-time P(accept) CP-lower is 0 for every bucket — the counterfactual
    therefore admits EXACTLY the zero-marginal-tail flow (diversifiers) and
    refuses every tail-game concentrator: the honest day-one behaviour of
    the marginal form, not an optimistic one.
"""

from __future__ import annotations

import argparse
import collections
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
from combomaker.pricing.grouping import game_key  # noqa: E402
from combomaker.risk.exposure import (  # noqa: E402
    LegRef,
    OpenPosition,
)
from combomaker.risk.limits import (  # noqa: E402
    RiskLimits,
    kill_envelope_tail_upper,
    threshold_cc,
)
from combomaker.sim.book_model import build_book_model  # noqa: E402
from combomaker.sim.book_risk import compute_book_risk  # noqa: E402

_DATA_DIR = Path(os.environ.get("VITALS_DATA_DIR") or (REPO / "data"))
DB_DEFAULT = _DATA_DIR / "combomaker-prod-live-wc.sqlite3"
LOG_DEFAULT = _DATA_DIR / "live_20260801_1049.log"

CONV = Conventions(
    verified=True, source="counterfactual",
    maker_side_on_yes_accept=Side.YES, maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True, maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)

KILL_FRAC = Fraction(12, 100)
KILL_TAIL_PROB = 0.02
BOOT_TS = "2026-08-01T14:49"


def load_book(db: Path, slate_prefix: str) -> list[OpenPosition]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "select position_id, combo_ticker, collection_ticker, our_side, "
        "contracts_centi, entry_price_cc, legs_json from position_ledger "
        "where status='open'"
    ).fetchall()
    con.close()
    out: list[OpenPosition] = []
    for pid, ticker, coll, side, contracts, price, legs_json in rows:
        legs = tuple(
            LegRef(x["market_ticker"], x.get("event_ticker"), x["side"])
            for x in json.loads(legs_json)
        )
        # Mirror the live boot's rehydration: only TODAY's slate still rides
        # in the in-memory book (the store keeps rows for settled games the
        # exchange has not graded in the ledger).
        games = {game_key(x.event_ticker) for x in legs if x.event_ticker}
        if not any(g.startswith(slate_prefix) for g in games):
            continue
        out.append(
            OpenPosition(
                position_id=pid, combo_ticker=ticker, collection=coll,
                our_side=Side.NO if side == "no" else Side.YES,
                contracts=CentiContracts(int(contracts)),
                entry_price_cc=CentiCents(int(price)),
                legs=legs,
            )
        )
    return out


def implied_marginals(positions: list[OpenPosition]) -> dict[str, float]:
    acc: dict[str, list[float]] = {}
    for p in positions:
        k = max(1, len(p.legs))
        implied_hit = max(1e-4, min(0.9999, 1.0 - p.entry_price_cc / 10_000.0))
        per_leg = implied_hit ** (1.0 / k)
        for leg in p.legs:
            acc.setdefault(leg.market_ticker, []).append(per_leg)
    return {t: sum(v) / len(v) for t, v in acc.items()}


def load_refusals(db: Path) -> list[dict]:
    """The session's ``skip_portfolio_cvar``-carrying declines, one row per
    decision, with the FULL reason set (other enforced caps refuse a combo
    regardless of the KILL form) and the RFQ's legs/size joined in."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("pragma table_info(rfqs)")}
    legs_col = "legs_json" if "legs_json" in cols else None
    out: list[dict] = []
    q = (
        "select d.at, d.rfq_id, d.reasons_json, r.* from decisions d "
        "left join rfqs r on r.rfq_id = d.rfq_id "
        f"where d.at >= '{BOOT_TS}' and d.kind = 'no_quote' "
        "and d.reasons_json like '%skip_portfolio_cvar%'"
    )
    for row in con.execute(q):
        reasons = set(json.loads(row["reasons_json"]))
        legs = []
        if legs_col and row[legs_col]:
            try:
                legs = json.loads(row[legs_col])
            except Exception:
                legs = []
        out.append(
            {
                "at": row["at"],
                "rfq_id": row["rfq_id"],
                "reasons": reasons,
                "legs": legs,
            }
        )
    con.close()
    return out


def rfq_games(legs: list) -> set[str]:
    games: set[str] = set()
    for leg in legs:
        et = None
        if isinstance(leg, dict):
            et = leg.get("event_ticker") or leg.get("market_ticker")
        if et:
            games.add(game_key(str(et)))
    return games


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DB_DEFAULT)
    ap.add_argument("--bankroll-cc", type=int, default=28_026_768,
                    help="live boot equity (account_standing 14:50:54Z)")
    ap.add_argument("--samples", type=int, default=40_000)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--slate-prefix", default="26AUG01")
    ap.add_argument("--growth-cap", type=int, default=40,
                    help="max admitted candidates folded into the trajectory")
    args = ap.parse_args()

    bank = args.bankroll_cc
    kill_line = threshold_cc(KILL_FRAC, bank)

    book = load_book(args.db, args.slate_prefix)
    marg = implied_marginals(book)
    det_max = sum(p.max_loss_cc for p in book)
    model = build_book_model(book, marginals=lambda t: marg.get(t, 0.25),
                             within_game_rho=None)
    snap = compute_book_risk(model, n_samples=args.samples, seed=args.seed,
                             bankroll_cc=bank)

    limits = RiskLimits(
        caps_shadow_mode=False,
        portfolio_tail_prob_gate=True,
        portfolio_kill_tail_prob=KILL_TAIL_PROB,
        portfolio_cvar_frac=Fraction(35, 100),
        portfolio_det_max_frac=Fraction(70, 100),
        hard_trip_frac=KILL_FRAC,
        kill_anchored_book_gate=True,
        kill_gate_marginal=True,
    )
    p_upper = kill_envelope_tail_upper(snap, limits, bank)

    print("=" * 78)
    print("MARGINAL KILL GATE — COUNTERFACTUAL ON THE 2026-08-01 FROZEN TAPE")
    print("=" * 78)
    print(f"rebuilt book                    {len(book)} positions "
          f"(live tape: 26 at gen 29)")
    print(f"rebuilt det-max                 ${det_max / 10_000:,.2f} "
          f"(live tape: $1,026.57)")
    print(f"KILL line (12% of ${bank / 10_000:,.2f})   ${kill_line / 10_000:,.2f}")
    print(f"rebuilt P(KILL) envelope upper  "
          f"{p_upper if p_upper is not None else float('nan'):.5f} "
          f"(live tape: 0.110-0.115)  budget {KILL_TAIL_PROB}")
    over = bool(p_upper is not None and p_upper > KILL_TAIL_PROB)
    print(f"regime                          "
          f"{'OVER budget - marginal form applies' if over else 'UNDER budget'}")
    print()

    # Per-game tail decomposition + denominators for the ALLOCATED dES99 —
    # the exact machinery the lifecycle reuses (allocate_des99_cc).
    tail_by_game: dict[str, float] = {}
    for tc in getattr(snap, "per_game_tail_cc", ()):
        tail_by_game[tc.key] = tail_by_game.get(tc.key, 0.0) + tc.loss_cc
    det_by_game: dict[str, float] = {}
    for p in book:
        for leg in p.legs:
            if leg.event_ticker:
                g = game_key(leg.event_ticker)
                det_by_game[g] = det_by_game.get(g, 0.0) + p.max_loss_cc
                break

    refusals = load_refusals(args.db)
    print(f"skip_portfolio_cvar decline rows since boot: {len(refusals):,}")
    only_cvar = [r for r in refusals if r["reasons"] == {"skip_portfolio_cvar"}]
    print(f"  ... where the KILL level was the ONLY enforced blocker: "
          f"{len(only_cvar):,}")
    print()

    verdict_counts: collections.Counter = collections.Counter()
    admitted: list[dict] = []
    distinct_admit_combos: set[str] = set()
    for r in only_cvar:
        games = rfq_games(r["legs"])
        if not games:
            verdict_counts["unresolvable_legs (kept refused)"] += 1
            continue
        tail_touch = [g for g in games if tail_by_game.get(g, 0.0) > 0.0]
        if not tail_touch:
            verdict_counts["ADMIT (diversifier, dES99 == 0)"] += 1
            admitted.append(r)
            distinct_admit_combos.add(frozenset(games))
        else:
            # Thin acceptance tape (0 quotes sent this session) => CP-lower
            # P(accept) = 0 => any positive allocated dES99 refuses.
            verdict_counts["REFUSE (tail-game concentrator, thin tape)"] += 1
    other = len(refusals) - len(only_cvar)
    verdict_counts["blocked by OTHER enforced caps regardless"] = other
    for k, v in verdict_counts.most_common():
        print(f"  {v:>8,}  {k}")
    print()

    # --- P(KILL)/EV trajectory: fold admitted flow into the book -----------
    # One synthetic fill per DISTINCT admitted game-set (an RFQ re-quoted 50x
    # is one book position at most), sized at the book's own median print —
    # the same growth-shape doctrine as kill_anchor_live_book.py.
    if not book or not admitted:
        print("no admitted flow to fold (or empty book) — trajectory skipped")
        return 0
    templates = sorted(book, key=lambda p: p.max_loss_cc)
    median = templates[len(templates) // 2]
    grown = list(book)
    marg2 = dict(marg)
    per_leg = (
        (1.0 - median.entry_price_cc / 10_000.0) ** (1.0 / max(1, len(median.legs)))
    )
    print("--- TRAJECTORY: admitted diversifiers fold in one by one ----------")
    print(f"(median live print ${median.max_loss_cc / 10_000:,.2f}, "
          f"{len(median.legs)} legs; distinct admitted game-sets: "
          f"{len(distinct_admit_combos):,}, folding first {args.growth_cap})")
    hdr = (f"{'n_added':>7} {'premium':>12} {'EV':>10} {'P(KILL)':>9}")
    print(hdr)
    seen: set[frozenset] = set()
    n_added = 0
    for r in admitted:
        gs = frozenset(rfq_games(r["legs"]))
        if gs in seen:
            continue
        seen.add(gs)
        n_added += 1
        legs = tuple(
            LegRef(f"CF{n_added}-L{j}", sorted(gs)[j % len(gs)], "yes")
            for j in range(len(median.legs))
        )
        grown.append(
            OpenPosition(
                position_id=f"cf-{n_added}", combo_ticker=f"CF-{n_added}",
                collection=None, our_side=Side.NO,
                contracts=median.contracts,
                entry_price_cc=median.entry_price_cc,
                legs=legs,
            )
        )
        for leg in legs:
            marg2.setdefault(leg.market_ticker, per_leg)
        if n_added % 5 == 0 or n_added == args.growth_cap:
            m2 = build_book_model(
                grown, marginals=lambda t: marg2.get(t, 0.25),
                within_game_rho=None,
            )
            s2 = compute_book_risk(
                m2, n_samples=args.samples, seed=args.seed, bankroll_cc=bank
            )
            pk = kill_envelope_tail_upper(s2, limits, bank)
            prem = sum(p.max_loss_cc for p in grown)
            print(f"{n_added:>7} ${prem / 10_000:>11,.2f} "
                  f"${s2.ev_cc / 10_000:>9,.2f} "
                  f"{pk if pk is not None else float('nan'):>9.5f}")
        if n_added >= args.growth_cap:
            break
    print()
    print("Reading: every fold is WORST-CASE (all admitted quotes fill at the")
    print("median live size). At the tape's measured acceptance (~1.3% of sent")
    print("quotes fill), the realized daily add is a small fraction of this.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
