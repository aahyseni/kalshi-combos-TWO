"""LIVE-BOOK MEASUREMENT of the slate cap: naive roll-up vs once-counted joint.

Rule 8 (testing isolation): this script IMPORTS and DRIVES the live modules —
``LimitChecker._slate_rollup`` and ``LimitChecker._slate_partition``, the exact
code the cap runs — and edits nothing. The store is opened READ-ONLY.

WHAT IT ANSWERS (the operator's question, in his units):

    "We need to fix the slate count it shouldn't be over counting, with 12 games
     in a day we should always be filling the $1500 cap we have."

so it reports, for the LIVE open book, the naive Σ-per-game number and the
once-counted ENUMERATED JOINT WORST CASE, each as a PERCENT OF THE RATIFIED
WALL, plus the headroom each leaves for a candidate.

FIDELITY LIMITS, stated up front:
  * RESTING QUOTES at this instant are not reconstructable from the store, so
    both numbers are the COMMITTED-BOOK aggregate. Both sides are computed on
    the same basis, so the RATIO is exact.
  * The bot buckets a game into a slate by its leg START TIME, which the store
    does not keep. We reconstruct the slate from the game date embedded in the
    event ticker (``…-26JUL281845TORWSH`` -> 2026-07-28), which is the same day
    by construction for every MLB series on the tape.
  * Mutual-exclusion facts come from ``data/metadata_cache.json`` — the same
    file the live book reads.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for _p in (str(REPO), str(REPO / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from combomaker.core.conventions import Conventions, Side  # noqa: E402
from combomaker.core.money import CentiCents  # noqa: E402
from combomaker.core.quantity import CentiContracts  # noqa: E402
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition  # noqa: E402
from combomaker.risk.limits import LimitChecker, RiskLimits  # noqa: E402

DB = REPO / "data" / "combomaker-prod-live-wc.sqlite3"
META = REPO / "data" / "metadata_cache.json"

CONV = Conventions(
    verified=True, source="live-measure",
    maker_side_on_yes_accept=Side.YES, maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True, maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)

# LIVE ratified values, read off the tape's own breach details (never retyped):
#   slate_loss_frac 13/20 = 0.65, threshold 14,965,958cc  =>  bankroll 23,024,551cc
SLATE_FRAC = Fraction(13, 20)
SLATE_THR_CC = 14_965_958
BANKROLL_CC = SLATE_THR_CC * SLATE_FRAC.denominator // SLATE_FRAC.numerator

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_DATE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})")


def start_of(market_ticker: str) -> datetime | None:
    """The game's start, reconstructed from the date embedded in the ticker.
    18:00 US/Eastern = 22:00 UTC — inside the day either way, so the slate label
    is the game date regardless of the exact first pitch."""
    m = _DATE.search(market_ticker)
    if not m or m.group(2) not in _MONTHS:
        return None
    return datetime(
        2000 + int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)),
        22, 0, tzinfo=UTC,
    )


def load() -> ExposureBook:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = con.execute(
        "select position_id, combo_ticker, collection_ticker, our_side, "
        "contracts_centi, entry_price_cc, legs_json from position_ledger "
        "where status='open'"
    ).fetchall()
    con.close()
    meta = json.loads(META.read_text(encoding="utf-8"))
    me = {
        e["event_ticker"]: bool(e.get("mutually_exclusive"))
        for e in meta.get("events", [])
        if e.get("event_ticker")
    }
    book = ExposureBook(CONV, is_me_event=lambda e: me.get(e))
    for pid, combo, coll, side, contracts, price, legs_json in rows:
        book.add_position(
            OpenPosition(
                position_id=pid, combo_ticker=combo, collection=coll,
                our_side=Side.NO if side == "no" else Side.YES,
                contracts=CentiContracts(int(contracts)),
                entry_price_cc=CentiCents(int(price)),
                legs=tuple(
                    LegRef(x["market_ticker"], x.get("event_ticker"), x["side"])
                    for x in json.loads(legs_json)
                ),
            )
        )
    return book


def main() -> None:
    book = load()
    premium = sum(p.max_loss_cc for p in book.positions.values())
    multi = sum(
        1
        for p in book.positions.values()
        if len({leg.event_ticker for leg in p.legs if leg.event_ticker}) > 1
    )
    checker = LimitChecker(
        RiskLimits(
            slate_loss_frac=SLATE_FRAC,
            slate_partition_armed=True,
            resting_quote_weight=Fraction(1, 100),
            resting_floor_count=3,
        )
    )
    snap = book.snapshot(lambda t: None, mass_acceptance=True, want_loss_units=True)
    naive = checker._slate_rollup(book, snap, [], start_of)
    part = checker._slate_partition(
        book, snap, [], start_of,
        limits=checker.limits, only_slates=set(naive), naive_by_slate=naive,
    )
    print(
        f"LIVE BOOK  positions={len(book.positions)}  "
        f"real premium at risk=${premium / 10_000:,.2f}  "
        f"multi-game tickets={multi} ({multi / max(1, len(book.positions)):.1%})"
    )
    print(
        f"WALL  slate_loss_frac={SLATE_FRAC} of bankroll "
        f"${BANKROLL_CC / 10_000:,.2f}  =  ${SLATE_THR_CC / 10_000:,.2f}"
    )
    print(f"{'slate':<14}{'NAIVE sum/game':>16}{'% of wall':>11}"
          f"{'ONCE-COUNTED':>16}{'% of wall':>11}{'ratio':>8}"
          f"{'headroom naive':>16}{'headroom fixed':>16}")
    for slate in sorted(naive):
        n = naive[slate]
        p = part.get(slate, n)
        print(
            f"{slate:<14}${n / 10_000:>14,.2f}{n / SLATE_THR_CC:>10.1%}"
            f"${p / 10_000:>14,.2f}{p / SLATE_THR_CC:>10.1%}"
            f"{n / max(1, p):>7.2f}x"
            f"${(SLATE_THR_CC - n) / 10_000:>14,.2f}"
            f"${(SLATE_THR_CC - p) / 10_000:>14,.2f}"
        )
    tn, tp = sum(naive.values()), sum(part.get(s, naive[s]) for s in naive)
    print(
        f"{'TOTAL':<14}${tn / 10_000:>14,.2f}{'':>10}"
        f"${tp / 10_000:>14,.2f}{'':>10}{tn / max(1, tp):>7.2f}x"
    )


if __name__ == "__main__":
    main()
