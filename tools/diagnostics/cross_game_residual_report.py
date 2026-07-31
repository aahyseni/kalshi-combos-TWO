"""CROSS-GAME RESIDUAL CORRELATION — per-slate report (2026-07-29).

INSTRUMENTATION ONLY. The operator ruled cross games INDEPENDENT ("one MLB game
doesn't affect another game, and that goes for any sport, same with esports"), so
``sim/book_model.DEFAULT_CROSS_EVENT_RHO = 0.0`` stands and NOTHING here gates.
The exposure this measures is not causal linkage, it is CORRELATED MODEL ERROR:
if our model runs rich it is rich on every game at once, and independent games
then behave like ONE bet in the tail. Measured sensitivity on the live book:
cross-rho 0.00 -> 0.25 moves ES99 $221.64 -> $280.65 and modeled EV +$10.84 ->
-$4.25 — the entire edge. So the assumption is made ANSWERABLE FROM SETTLEMENTS,
the one ruler the model cannot bend.

Rule 8: drives the live ``risk.cross_game_residual`` estimator; store read-only.

Each SETTLED position contributes (game, p_loss, lost):
  * ``p_loss`` = our own modeled probability that the parlay HITS at the price we
    traded — read from the position's entry price, which for a long-NO combo IS
    our fair statement (1 - price);
  * ``lost``   = the exchange's settlement (settled_value / realized P&L).
Slate = the trading day (America/New_York) the position settled on.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for _p in (str(REPO), str(REPO / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from combomaker.risk.cross_game_residual import (  # noqa: E402
    SettledLeg,
    pool_slates,
    slate_cross_game_rho,
)

DB = REPO / "data" / "combomaker-prod-live-wc.sqlite3"
EASTERN = timezone(timedelta(hours=-4))  # EDT — operator reads Eastern only


def load(db: Path) -> dict[str, list[SettledLeg]]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "select reconciled_at, opened_at, legs_json, entry_price_cc, "
        "our_side, realized_pnl_cc, settled_value from position_ledger "
        "where status='settled'"
    ).fetchall()
    con.close()
    import json

    slates: dict[str, list[SettledLeg]] = {}
    for rec_at, opened_at, legs_json, price, side, pnl, settled in rows:
        stamp = rec_at or opened_at
        if not stamp:
            continue
        try:
            when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            continue
        slate = when.astimezone(EASTERN).date().isoformat()
        legs = json.loads(legs_json)
        if not legs:
            continue
        # GAME KEY: the leg's event ticker is the game. A multi-game parlay is
        # attributed to its FIRST game — the estimator wants one draw per game
        # and a parlay cannot be split without inventing a decomposition.
        game = legs[0].get("event_ticker") or legs[0]["market_ticker"]
        # p_loss = our modeled P(parlay HITS) = 1 - the NO price we paid.
        p_loss = 1.0 - int(price) / 10_000.0
        if str(side) != "no":
            p_loss = int(price) / 10_000.0
        # LOST = we forfeited the premium. realized_pnl_cc is the ground truth.
        lost = int(pnl or 0) < 0
        slates.setdefault(slate, []).append(
            SettledLeg(game=str(game), p_loss=p_loss, lost=lost)
        )
    return slates


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB))
    args = ap.parse_args()

    slates = load(Path(args.db))
    per = {s: slate_cross_game_rho(s, legs) for s, legs in sorted(slates.items())}

    print("=" * 78)
    print("CROSS-GAME RESIDUAL CORRELATION — INSTRUMENTATION ONLY (never gates)")
    print("=" * 78)
    print(f"{'slate (ET)':<14}{'games':>7}{'pos':>6}{'drop':>6}"
          f"{'rho_hat':>10}{'se(H0)':>9}{'z':>8}{'mean resid':>12}")
    print("-" * 78)
    for slate, c in per.items():
        if not c.usable:
            print(f"{slate:<14}{c.games:>7}{c.positions:>6}"
                  f"{c.dropped_positions:>6}{'--':>10}{'--':>9}{'--':>8}"
                  f"{c.mean_residual:>12.3f}   (<2 games: not usable)")
            continue
        print(f"{slate:<14}{c.games:>7}{c.positions:>6}{c.dropped_positions:>6}"
              f"{c.rho_hat:>10.4f}{c.se_h0:>9.4f}{c.z_score:>8.2f}"
              f"{c.mean_residual:>12.3f}")
    rho, se, n = pool_slates(per)
    print("-" * 78)
    if n:
        print(f"POOLED over {n} usable slates:  rho_hat = {rho:+.4f}  "
              f"se = {se:.4f}  z = {rho / se:+.2f}")
        print(f"  operator's ruling (rho = 0) is {'CONSISTENT' if abs(rho / se) < 2 else 'CHALLENGED'} "
              f"with this read at |z| {'<' if abs(rho / se) < 2 else '>='} 2")
    else:
        print("POOLED: no usable slate (need >= 2 distinct games in a slate)")
    print()
    print("This number is REPORTED, never gated (operator ruling 2026-07-29).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
