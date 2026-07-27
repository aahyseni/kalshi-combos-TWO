"""READ-ONLY verdict on the pre-kill ``p_book 0.0 / EV -$71.91 / 7 positions``.

Question: was the MC telling the truth (the modelled book was ALREADY a
determined loser awaiting settlement), or is p_book 0.0 a DEFECT?

Method — no model, no MC, just facts:
  1. read every OPEN position_ledger row (sqlite mode=ro);
  2. ask the exchange, read-only, for each distinct LEG market's ``status`` and
     ``result`` (GET /markets/{ticker});
  3. for each position, evaluate the parlay against the settled legs. We are
     SELL-ONLY: every position is long NO on the combo, so
         combo HITS (all legs settle our way)  -> we pay $1.00, lose (100-entry)
         any leg settles AGAINST                -> combo dead, we keep the premium
  4. print, per position, DETERMINED-LOSER / DETERMINED-WINNER / UNDETERMINED.

Nothing is written. No orders. GETs only.

    .venv/Scripts/python.exe tools/diagnostics/pbook_zero_verdict.py
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from combomaker.core.clock import SystemClock  # noqa: E402
from combomaker.exchange.auth import Credentials, RequestSigner  # noqa: E402
from combomaker.exchange.rest import KalshiRestClient  # noqa: E402
from combomaker.ops.dotenv import load_dotenv  # noqa: E402

PROD_REST = "https://external-api.kalshi.com/trade-api/v2"
DB = "data/combomaker-prod-live-wc.sqlite3"

# The games the kill-time book model actually carried (from the live tape's
# book_risk_snapshot top_tail_games + the 20:07 exposure_rehydrated list).
KILL_TAIL_GAMES = ("26JUL261335TORBOS", "26JUL261920NYYPHI", "26JUL261335ATLBAL")


def d(cc: int) -> str:
    return f"${cc/10000:,.2f}"


async def main() -> int:
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = list(
        db.execute(
            "select position_id, combo_ticker, our_side, contracts_centi,"
            " entry_price_cc, cost_cc, legs_json from position_ledger"
            " where status='open'"
        )
    )
    positions = []
    tickers: set[str] = set()
    for pid, ct, side, qty, ep, cost, legs_json in rows:
        legs = json.loads(legs_json)
        positions.append(
            {"pid": pid, "ct": ct, "side": side, "qty": qty, "ep": ep,
             "cost": cost, "legs": legs}
        )
        for l in legs:
            tickers.add(l["market_ticker"])

    print(f"open ledger rows: {len(positions)}   distinct leg markets: {len(tickers)}")

    load_dotenv()
    signer = RequestSigner(Credentials.for_env("prod"), SystemClock())
    meta: dict[str, dict] = {}
    async with KalshiRestClient(PROD_REST, signer) as rest:
        sem = asyncio.Semaphore(4)

        async def one(t: str) -> None:
            async with sem:
                for attempt in range(6):
                    try:
                        p = await rest.get_market(t)
                        m = p.get("market", p)
                        meta[t] = {
                            "status": str(m.get("status", "")),
                            "result": str(m.get("result", "")),
                            "close": str(m.get("close_time", "")),
                        }
                        return
                    except Exception as exc:
                        if attempt == 5:
                            meta[t] = {"status": f"ERR {exc!r}"[:60], "result": "",
                                       "close": ""}
                            return
                        await asyncio.sleep(1.0 + attempt)
                await asyncio.sleep(0.05)

        await asyncio.gather(*(one(t) for t in sorted(tickers)))
        bal = await rest.get_balance()

    print(f"balance: {json.dumps(bal)}")
    by_status: dict[str, int] = {}
    for m in meta.values():
        by_status[m["status"]] = by_status.get(m["status"], 0) + 1
    print("leg market statuses:", by_status)
    print()

    def leg_verdict(l: dict) -> str | None:
        """True/False/None = leg settled OUR way / against us / undetermined.

        Our combo needs each leg to land on the side named in ``l['side']``.
        Kalshi ``result`` is 'yes'/'no' once settled.
        """
        m = meta.get(l["market_ticker"])
        if m is None:
            return None
        res = (m["result"] or "").lower()
        if res not in ("yes", "no"):
            return None
        return "HIT" if res == l["side"].lower() else "MISS"

    det_loss_cc = 0
    det_win_cc = 0
    undet_cc = 0
    n_loser = n_winner = n_undet = 0
    print(f"{'verdict':<12}{'game(s)':<44}{'qty':>9}{'entry':>8}{'P&L if settled now':>20}")
    print("-" * 96)
    for p in sorted(positions, key=lambda x: -x["cost"]):
        vs = [leg_verdict(l) for l in p["legs"]]
        games = sorted({l["event_ticker"].split("-", 1)[-1] for l in p["legs"]})
        gtxt = ",".join(g[7:] for g in games)[:42]
        # WE ARE LONG NO ON THE COMBO (sell-only): we PAID ``cost`` for the NO.
        #   combo MISSES (any leg against) -> NO settles $1.00 -> gain qty*(100-entry)
        #   combo HITS   (every leg lands) -> NO settles $0.00 -> lose the premium
        if any(v == "MISS" for v in vs):
            pnl = p["qty"] * (10_000 - p["ep"]) // 100
            verdict = "WINNER"
            det_win_cc += pnl
            n_winner += 1
        elif all(v == "HIT" for v in vs):
            pnl = -p["cost"]
            verdict = "LOSER"
            det_loss_cc += pnl
            n_loser += 1
        else:
            pnl = 0
            verdict = "undetermined"
            undet_cc += p["cost"]
            n_undet += 1
            p["max_gain"] = p["qty"] * (10_000 - p["ep"]) // 100
        mark = "  <-- KILL-TIME TAIL GAME" if any(
            g in KILL_TAIL_GAMES for g in games
        ) else ""
        print(f"{verdict:<12}{gtxt:<44}{p['qty']/100:>9.2f}{p['ep']/100:>8.2f}"
              f"{d(pnl):>20}{mark}")

    print("-" * 96)
    print(f"DETERMINED LOSERS  : {n_loser:>3}   realized {d(det_loss_cc)}")
    print(f"DETERMINED WINNERS : {n_winner:>3}   realized {d(det_win_cc)}")
    print(f"UNDETERMINED       : {n_undet:>3}   premium at risk {d(undet_cc)}")
    print(f"NET on determined  : {d(det_loss_cc + det_win_cc)}")

    # ------------------------------------------------------------------
    # THE KILL-TIME QUESTION: could the SAMPLED sub-book (n_positions=7) have
    # been positive in ANY scenario?  book_risk.p_profit = P(book_loc > 0) over
    # the sampled positions ONLY (reserved holdings sit outside it, in
    # deterministic_max_loss / reserved_loss_cc).  The tape's last snapshot:
    #   per_game_tail_cc  TORBOS 792,117 | NYYPHI 308,720 | ATLBAL 167,090
    # ------------------------------------------------------------------
    print()
    print("=" * 96)
    print("KILL-TIME (20:11:29Z) SAMPLED SUB-BOOK — was p_book 0.0 arithmetically forced?")
    print("=" * 96)
    # MEMBERSHIP IS PINNED BY THE TAPE, not guessed. book_risk logs a
    # per-game tail = the mutex-aware worst case of the SAMPLED positions on
    # that game, so each snapshot's tail identifies its members exactly:
    #
    #  20:07:24 n=2  ATLBAL 167,090  SEATEX  53,487
    #                 -> $16.71 = the (ATL ML yes + TOTAL-4 yes) premium
    #                 -> $5.35  = the (LGILBERT36-5 + JDEGROM48-5 + TOTAL-7) premium
    #  20:10:22 n=4  + NYYPHI 350,020 ($35.00 = the NYY+TOTAL-8 premium) + a LOL arm
    #  20:10:54 n=5  NYYPHI 308,720 = 350,020 - 41,300, i.e. the SECOND NYYPHI
    #                 position ($5.87, max gain 10.00 x 41.30c = $4.13) netted
    #                 against the first: the two are MUTEX on the game winner
    #  20:11:12 n=7  + TORBOS 307,707 = $30.77 (BOS ML + TOTAL-11 no)
    #  20:11:29 n=7  TORBOS 792,117 = $48.44 + $30.77 -> BOTH big TORBOS arms in
    #
    # The huge ATLBAL strikeout position ($81.47) is NOT a member: were it
    # sampled, ATLBAL's tail could not have stayed at exactly $16.71 all run.
    SAMPLED = [
        ("TORBOS  BOS ML yes + TOTAL-8 no",  48_4400 // 100 * 100, 6_700, 7_230),
        ("TORBOS  BOS ML yes + TOTAL-11 no", 0, 5_189, 5_930),
        ("ATLBAL  ATL ML yes + TOTAL-4 yes", 0, 3_100, 5_390),
        ("SEATEX  LGILBERT36-5+JDEGROM48-5+TOTAL-7", 0, 841, 6_360),
        ("NYYPHI  NYY ML yes + TOTAL-8 yes", 0, 4_300, 8_140),
        ("NYYPHI  PHI ML yes + TOTAL-10 no", 0, 1_000, 5_870),
        ("LOL     4-leg KXLOLGAME parlay (20:10:12 fill)", 0, 2_184, 7_830),
    ]
    # resolve each pinned member back to its REAL ledger row by (qty, entry)
    locked_loss = 0
    max_gain = 0
    print(f"\n  {'sampled member':<46}{'premium':>10}{'settled':>14}{'best case':>12}")
    print("  " + "-" * 82)
    for label, _pad, qty, ep in SAMPLED:
        row = next(
            (p for p in positions if p["qty"] == qty and p["ep"] == ep), None
        )
        if row is None:
            print(f"  {label:<46}{'NOT FOUND':>10}")
            continue
        vs = [leg_verdict(l) for l in row["legs"]]
        gain = row["qty"] * (10_000 - row["ep"]) // 100
        if all(v == "HIT" for v in vs):
            locked_loss += row["cost"]
            print(f"  {label:<46}{d(row['cost']):>10}{'LOSER':>14}"
                  f"{d(-row['cost']):>12}")
        elif any(v == "MISS" for v in vs):
            max_gain += gain
            print(f"  {label:<46}{d(row['cost']):>10}{'WINNER':>14}{d(gain):>12}")
        else:
            max_gain += gain
            print(f"  {label:<46}{d(row['cost']):>10}{'undetermined':>14}"
                  f"{d(gain):>12}")

    realized = 47_440  # realized_pnl_cc carried on the kill-time snapshot
    print("  " + "-" * 82)
    print(f"  LOCKED LOSS on arms already decided AGAINST us : {d(-locked_loss)}")
    print(f"  MAX ATTAINABLE GAIN on every other member      : {d(max_gain)}")
    print(f"  realized_pnl_cc carried on that snapshot       : {d(realized)}")
    best = -locked_loss + max_gain + realized
    print(f"  ==> BEST CASE for the sampled sub-book         : {d(best)}")
    print(f"      tape reported  ev {d(-719140)}   p_book 0.0   p_night 0.0")
    print()
    if best < 0:
        print("  VERDICT: p_book 0.0 is CORRECT AND ARITHMETICALLY FORCED.")
        print("  Even the single most favourable scenario leaves the sampled")
        print("  sub-book negative, so P(book > 0) is exactly 0. And EV")
        print(f"  ({d(-719140)}) sits within {d(abs(best - (-719140)))} of that best")
        print("  case, i.e. the MC judged nearly every member near-certain -")
        print("  which the settlements above confirm it was right about.")
    else:
        print("  VERDICT: a profitable scenario EXISTS -> p_book 0.0 is a DEFECT.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
