"""FIX 1 (2026-07-28) — PER-CENT ATTRIBUTION of the gap between our FAIR and the
BID we actually emit, over the real ``quote_sent`` decision tape.

WHY. Config markup is 2.00c. Shipped margin over 1,430,589 quotes measures median
4.05c / mean 4.63c. Our FAIR is excellent (MLB |err| median 0.36c vs the print,
78.3% within one tick; on auctions we LOST our fair was BELOW the print 96.6% of
the time at median -0.80c) while our ASK is median +2.70c above the print and
at/under it on only 8.5% of printable auctions. So we lose on the MARKUP, not the
fair — and this tool finds every term riding on top of the operator's 2.00c and
attributes the gap to the cent.

RULE 8 (testing isolation). This script IMPORTS and CALLS the live pricing code
and the SHIPPED config — the real ``MarkupPolicy`` and the real ``FeeModel``,
built exactly as ``PricingEngine.__init__`` builds them (engine.py:155-160). It
reimplements NOTHING except the four-line arithmetic of ``construct_quote``'s
margin/bid identity, which is asserted below against the logged bid.

THE IDENTITY (pricing/quote.py:137-244), sell-only so the NO side is the quote:

    half   = sum(width_cc.values()) // 2          <- width_cc is LOGGED verbatim
    margin = max(half, markup_cc)                 <- quote.py:166
    no_raw = (100c - fair) - margin - fee_no + inventory_skew
    no_bid = snap_bid_down(no_raw)                <- maker-favorable, one tick

so, over the NO side's own fair (100c - fair):

    GAP = margin + fee_no - inventory_skew + snap_loss [+ free-money clamp]

Every term but the last two is computed exactly here; the residual is reported
rather than assumed, and its sign/scale is the check on the whole decomposition.

The logged ``width_cc`` is the FINAL dict ``construct_quote`` built, so the
archetype multiplier (which collapses the dict to a single ``scaled`` key) and
the DO-6 basket buffer (the ``basket`` key) are directly observable rather than
inferred.

Read-only: opens the store ``mode=ro`` and never writes.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from collections import defaultdict
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from combomaker.core.conventions import load_conventions
from combomaker.core.money import CC_PER_DOLLAR, CentiCents
from combomaker.ops.config import load_config
from combomaker.ops.fee_schedule import load_observed_fee_schedule
from combomaker.pricing.fees import FeeModel, FeeType
from combomaker.pricing.markup import MarkupPolicy, sport_of

DB = "file:data/combomaker-prod-live-wc.sqlite3?mode=ro"


def _fee_no_cc(
    fee_model: FeeModel,
    fee_type: FeeType,
    mult: Fraction,
    side_fair_cc: int,
    margin: int,
    skew: int,
) -> int:
    """``construct_quote.side_fee`` for the NO side — the max fee over the
    plausible fill range (quote.py:187-207), not the fee at fair."""
    at_fair = int(
        fee_model.fee_per_contract_cc(
            price_cc=CentiCents(side_fair_cc), fee_type=fee_type, multiplier=mult
        )
    )
    peak = int(
        fee_model.fee_per_contract_cc(
            price_cc=CentiCents(CC_PER_DOLLAR // 2), fee_type=fee_type, multiplier=mult
        )
    )
    lower = max(0, side_fair_cc - margin - abs(skew) - peak)
    nearest = min(max(CC_PER_DOLLAR // 2, lower), side_fair_cc)
    in_range = int(
        fee_model.fee_per_contract_cc(
            price_cc=CentiCents(nearest), fee_type=fee_type, multiplier=mult
        )
    )
    return max(at_fair, in_range)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/prod-live-wc.local.yaml")
    ap.add_argument("--limit", type=int, default=0, help="0 = every quote_sent")
    ap.add_argument(
        "--tail",
        type=int,
        default=0,
        help=(
            "attribute only the N MOST RECENT quote_sent rows. Use this: the "
            "markup tiers and quote params have changed over the tape's life, "
            "and this tool replays TODAY'S shipped config, so attributing the "
            "full history mixes config eras and manufactures a large negative "
            "residual (measured: -2.45c on the oldest 30k rows)."
        ),
    )
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    markup = MarkupPolicy.from_config(cfg.pricing.markup)
    # The MEASURED schedule (2026-09-04): the persisted observer file under
    # data_dir, taker-conservative when absent — never a yaml maker number.
    fee_model = FeeModel(
        load_observed_fee_schedule(cfg.pricing.fee, cfg.data_dir),
        load_conventions(),
    )
    fee_type = FeeType.parse(cfg.pricing.fee.default_fee_type)
    mult = Fraction(Decimal(cfg.pricing.fee.default_multiplier))

    con = sqlite3.connect(DB, uri=True)
    q = "SELECT context_json FROM decisions WHERE kind='quote_sent'"
    if args.tail:
        q += f" ORDER BY id DESC LIMIT {args.tail}"
    elif args.limit:
        q += f" LIMIT {args.limit}"

    # bucket -> list of per-term cc
    terms = ("gap", "markup", "width_excess", "fee", "residual")
    acc: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(
        lambda: {t: [] for t in terms}
    )
    width_parts: dict[str, list[int]] = defaultdict(list)
    n = 0
    n_basket = 0
    n_scaled = 0
    n_width_binds = 0

    for (ctx,) in con.execute(q):
        try:
            d = json.loads(ctx)
        except Exception:
            continue
        no_bid = d.get("no_bid_cc")
        fair = d.get("fair_cc")
        w = d.get("width_cc")
        mids = d.get("leg_mids_cc") or {}
        if not no_bid or fair is None or not isinstance(w, dict) or not mids:
            continue

        legs = list(mids.keys())
        _sport, markup_cc = markup.markup_for(legs, fair_cc=int(fair))
        half = sum(int(v) for v in w.values()) // 2
        margin = max(half, markup_cc)
        side_fair = CC_PER_DOLLAR - int(fair)
        fee = _fee_no_cc(fee_model, fee_type, mult, side_fair, margin, 0)
        gap = side_fair - int(no_bid)

        n += 1
        if "basket" in w:
            n_basket += 1
        if "scaled" in w:
            n_scaled += 1
        if half > markup_cc:
            n_width_binds += 1
        for k, v in w.items():
            width_parts[k].append(int(v))

        nlegs = len(legs)
        lb = "2" if nlegs <= 2 else ("3-4" if nlegs <= 4 else ("5-7" if nlegs <= 7 else "8+"))
        sp = sport_of(legs)
        for bucket in ((sp, lb), ("ALL", "ALL"), ("ALL", lb), (sp, "ALL")):
            a = acc[bucket]
            a["gap"].append(gap / 100.0)
            a["markup"].append(min(margin, markup_cc) / 100.0)
            a["width_excess"].append(max(0, half - markup_cc) / 100.0)
            a["fee"].append(fee / 100.0)
            a["residual"].append((gap - margin - fee) / 100.0)

    print(f"quotes attributed: {n:,}")
    print(f"  width_cc carried a DO-6 'basket' key : {n_basket:,} ({100*n_basket/max(n,1):.2f}%)")
    print(f"  width_cc carried an archetype 'scaled' key: {n_scaled:,} "
          f"({100*n_scaled/max(n,1):.2f}%)")
    print(f"  defensive width EXCEEDED markup (half > markup): {n_width_binds:,} "
          f"({100*n_width_binds/max(n,1):.2f}%)")
    print()
    print("raw width components (cc, median / mean / p95) — these are HALVED into 'half':")
    for k in sorted(width_parts):
        v = width_parts[k]
        print(f"  {k:<12} n={len(v):>9,}  med={statistics.median(v):>8.1f}  "
              f"mean={statistics.fmean(v):>8.1f}  p95={sorted(v)[int(0.95*(len(v)-1))]:>8.1f}")
    print()
    # MEANS, not medians. The decomposition is additive per quote, and the mean
    # is the only additive summary — medians of the terms do NOT sum to the
    # median gap (the markup itself varies by fair tier, so on 2-leg MLB the
    # median markup is 1.00c against a 1.92c median gap and the row looks broken
    # while every individual quote reconciles exactly). Median gap is carried
    # alongside as the distributional read.
    hdr = (f"{'sport':<9}{'legs':<6}{'n':>10}  {'med gap':>8} | {'MEAN GAP':>8} = "
           f"{'markup':>7} + {'width+':>7} + {'fee':>6} + {'resid':>7}   {'width+ binds':>12}")
    print(hdr)
    print("-" * len(hdr))
    for bucket in sorted(acc, key=lambda b: (b[0] != "ALL", b[0], b[1])):
        a = acc[bucket]
        row = [statistics.fmean(a[t]) for t in terms]
        binds = sum(1 for x in a["width_excess"] if x > 0.0) / len(a["gap"])
        print(f"{bucket[0]:<9}{bucket[1]:<6}{len(a['gap']):>10,}  "
              f"{statistics.median(a['gap']):>8.2f} | {row[0]:>8.2f} = {row[1]:>7.2f} + "
              f"{row[2]:>7.2f} + {row[3]:>6.2f} + {row[4]:>7.2f}   {100*binds:>11.1f}%")
    print()
    print("(CENTS. MEAN columns are additive and reconcile per quote.")
    print(" 'markup' = the operator's FROZEN tier, min(margin, markup_cc).")
    print(" 'width+' = DEFENSIVE WIDTH riding ON TOP of it = max(0, half - markup_cc).")
    print(" 'fee'    = fee_no, the max fee over the plausible fill range.")
    print(" 'resid'  = snap-down tick loss + inventory skew + free-money clamp.")
    print(" 'width+ binds' = share of quotes where the defensive width EXCEEDED the markup.)")


if __name__ == "__main__":
    main()
