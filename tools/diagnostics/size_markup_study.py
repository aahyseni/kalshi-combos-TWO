"""TASK C — SIZE-SCALED MARKUP: is adverse selection size-dependent FOR US?

MEASUREMENT ONLY. Reads the live store ``mode=ro``, writes nothing, quotes
nothing, and does not import the risk/quote path in a way that could mutate it.
Rule 8: it IMPORTS the live ``pricing.grouping.game_key`` (the ONE game key the
pricer and risk layer already aggregate on) rather than reimplementing it.

THE QUESTION (operator 2026-07-31): "market makers charge more when it's a
bigger dollar amount." Before typing any curve (NORTH STAR forbids a hand-set
number), measure whether OUR realized adverse selection actually gets worse
with ticket size.

THE RULERS (quiet-failure defense #5 — a biased fair cannot catch itself):
  1. OUR FAIR markout      : fair_at_fill_cc  -> fair_now_cc
  2. RAW KALSHI LEG MIDS   : raw_mid_at_fill_cc -> raw_mid_now_cc
  3. SETTLEMENT            : position_ledger realized P&L (the one ruler the
                             model cannot bend). Flagged: the local ledger was
                             measured 14.78%-wrong on dollars vs the exchange on
                             7/29, so it is a SECONDARY lens here, not primary.

SIGN CONVENTION. The book is sell-only: every fill is us BUYING NO. ``fair_cc``
and ``raw_mid_cc`` are the YES-side combo value. Our NO position gains when the
YES value FALLS, so

    markout_cents(h) = (value_at_fill_cc - value_at_h_cc) / 100

is POSITIVE when the market moved OUR WAY and NEGATIVE when we were adversely
selected.

INDEPENDENCE. Fills inside one game are not independent, and a combo links the
games it spans. Clusters are therefore the CONNECTED COMPONENTS of the
game-co-occurrence graph (union-find over every combo's leg game keys), and all
CIs are cluster bootstraps over those components. Fewer than ~10 components =>
HYPOTHESIS, never a finding.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from combomaker.pricing.grouping import game_key

DB_DEFAULT = (
    "file:C:/Users/aahys/kalshi-combos-TWO/data/combomaker-prod-live-wc.sqlite3?mode=ro"
)

CC_PER_DOLLAR = 10_000
CENTI = 100  # centi-contracts per contract


# --------------------------------------------------------------------------- #
# union-find over game keys -> independent clusters
# --------------------------------------------------------------------------- #
class _UF:
    def __init__(self) -> None:
        self.p: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


@dataclass
class Fill:
    at: str
    quote_id: str
    rfq_id: str | None
    combo_ticker: str
    contracts: float          # contracts (not centi)
    price_c: float            # our NO entry price, CENTS
    premium_usd: float        # contracts * price
    payout_usd: float         # contracts * $1  (max win on a NO that pays 1)
    n_legs: int | None
    req_cost_usd: float | None   # RFQ target_cost, the EX-ANTE size axis
    games: tuple[str, ...]
    cluster: str = ""
    cluster_game: str = ""
    cluster_day: str = ""
    mo: dict[tuple[str, float], float] | None = None  # (ruler,horizon) -> cents


def load(conn: sqlite3.Connection) -> list[Fill]:
    rows = conn.execute(
        "SELECT at, fill_ref, combo_ticker, our_side, contracts_centi, price_cc, raw_json "
        "FROM fills ORDER BY at"
    ).fetchall()

    fills: list[Fill] = []
    for at, fill_ref, ticker, side, cc_ct, price_cc, raw in rows:
        meta: dict[str, Any] = json.loads(raw)
        rfq_id = meta.get("rfq_id")
        contracts = cc_ct / CENTI
        price_c = price_cc / 100.0
        assert side == "no", f"sell-only assumption broken: {side}"
        fills.append(
            Fill(
                at=at,
                quote_id=fill_ref.split(":", 1)[1],
                rfq_id=rfq_id,
                combo_ticker=ticker,
                contracts=contracts,
                price_c=price_c,
                premium_usd=contracts * price_c / 100.0,
                payout_usd=contracts * 1.0,
                n_legs=None,
                req_cost_usd=None,
                games=(),
            )
        )

    # RFQ join: legs (games) + the requested size we priced against.
    for f in fills:
        if not f.rfq_id:
            continue
        r = conn.execute(
            "SELECT n_legs, legs_json, target_cost_cc, contracts_centi FROM rfqs "
            "WHERE rfq_id = ? LIMIT 1",
            (f.rfq_id,),
        ).fetchone()
        if r is None:
            continue
        f.n_legs = r[0]
        legs = json.loads(r[1])
        f.games = tuple(sorted({game_key(leg["event_ticker"]) for leg in legs}))
        if r[2] is not None:
            f.req_cost_usd = r[2] / CC_PER_DOLLAR

    # markouts, both rulers, every horizon
    mo_rows = conn.execute(
        "SELECT fill_ref, horizon_s, fair_at_fill_cc, fair_now_cc, "
        "raw_mid_at_fill_cc, raw_mid_now_cc FROM markouts WHERE fill_ref LIKE 'fill:%'"
    ).fetchall()
    by_ref: dict[str, dict[tuple[str, float], float]] = defaultdict(dict)
    for ref, h, fa, fn, ra, rn in mo_rows:
        qid = ref.split(":", 1)[1]
        if fa is not None and fn is not None:
            by_ref[qid][("fair", h)] = (fa - fn) / 100.0
        if ra is not None and rn is not None:
            by_ref[qid][("raw", h)] = (ra - rn) / 100.0
    for f in fills:
        f.mo = by_ref.get(f.quote_id, {})

    # CLUSTERS. A fill whose RFQ legs are unknown (168 of 514 — the RFQ row is
    # not in the store) has NO game key, so it must NOT be handed its own
    # singleton cluster: that silently manufactures independence and narrows
    # every CI. Two clusterings are carried and BOTH are reported:
    #   game  — connected components of the game-co-occurrence graph. Only the
    #           RFQ-joined fills qualify; the rest are dropped from this lens.
    #   day   — one cluster per trading day, ALL fills. A night's slate shares
    #           weather, slate-wide flow and (often) one counterparty, so this
    #           is the CONSERVATIVE lens and it is the one the verdict uses.
    uf = _UF()
    for f in fills:
        keys = list(f.games)
        for k in keys[1:]:
            uf.union(keys[0], k)
    for f in fills:
        f.cluster_game = uf.find(f.games[0]) if f.games else ""
        f.cluster_day = f.at[:10]
        f.cluster = f.cluster_day
    return fills


# --------------------------------------------------------------------------- #
# cluster bootstrap
# --------------------------------------------------------------------------- #
def cluster_bootstrap(
    items: list[tuple[str, float]],
    stat: Any = statistics.mean,
    reps: int = 20_000,
    seed: int = 20260731,
) -> tuple[float, float, float, int]:
    """items = [(cluster_id, value)] -> (point, lo95, hi95, n_clusters)."""
    if not items:
        return (float("nan"),) * 3 + (0,)  # type: ignore[return-value]
    groups: dict[str, list[float]] = defaultdict(list)
    for c, v in items:
        groups[c].append(v)
    keys = list(groups)
    point = stat([v for c, v in items])
    if len(keys) < 2:
        return point, float("nan"), float("nan"), len(keys)
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(reps):
        pool: list[float] = []
        for _ in range(len(keys)):
            pool.extend(groups[keys[rng.randrange(len(keys))]])
        if pool:
            draws.append(stat(pool))
    draws.sort()
    lo = draws[int(0.025 * len(draws))]
    hi = draws[int(0.975 * len(draws))]
    return point, lo, hi, len(keys)


def wmean(vals: list[tuple[float, float]]) -> float:
    """weighted mean of [(value, weight)]"""
    w = sum(x[1] for x in vals)
    return sum(v * wt for v, wt in vals) / w if w else float("nan")


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def size_buckets(vals: list[float], k: int = 4) -> list[float]:
    """k-quantile edges (interior) of the size axis."""
    s = sorted(vals)
    return [s[int(i * len(s) / k)] for i in range(1, k)]


def bucket_of(x: float, edges: list[float]) -> int:
    b = 0
    for e in edges:
        if x >= e:
            b += 1
    return b


def report(fills: list[Fill], reps: int, axis: str, clustering: str) -> None:
    def size_of(f: Fill) -> float | None:
        if axis == "fill":
            return f.premium_usd
        return f.req_cost_usd

    usable = [f for f in fills if size_of(f) is not None]
    if clustering == "game":
        usable = [f for f in usable if f.cluster_game]
        for f in usable:
            f.cluster = f.cluster_game
    else:
        for f in usable:
            f.cluster = f.cluster_day
    print(f"\n{'='*78}\nSIZE AXIS = {axis}   CLUSTERING = {clustering}   "
          f"(n={len(usable)} of {len(fills)} fills)")
    ncl = len({f.cluster for f in usable})
    print(f"independent clusters: {ncl}")
    if ncl < 10:
        print("  *** < 10 clusters -> ANY result below is a HYPOTHESIS, not a finding")
    if not usable:
        return

    sizes = [size_of(f) for f in usable]  # type: ignore[misc]
    edges = size_buckets(sizes, 4)  # type: ignore[arg-type]
    print(f"quartile edges (USD): {[round(e,2) for e in edges]}")

    for ruler in ("fair", "raw"):
        for h in (60.0, 300.0, 1800.0):
            groups: dict[int, list[tuple[str, float]]] = defaultdict(list)
            dollars: dict[int, list[tuple[str, float]]] = defaultdict(list)
            for f in usable:
                v = (f.mo or {}).get((ruler, h))
                if v is None:
                    continue
                b = bucket_of(size_of(f), edges)  # type: ignore[arg-type]
                groups[b].append((f.cluster, v))
                # dollar markout normalised by premium at risk -> unit-free
                if f.premium_usd > 0:
                    dollars[b].append(
                        (f.cluster, (v / 100.0) * f.contracts / f.premium_usd * 100.0)
                    )
            if not groups:
                continue
            print(f"\n-- ruler={ruler}  horizon={int(h)}s   markout in CENTS/contract "
                  f"(+ = moved our way, - = adversely selected)")
            print(f"{'bucket':<26}{'n':>5}{'clus':>6}{'mean':>9}{'lo95':>9}{'hi95':>9}"
                  f"{'median':>9}{'%prem':>9}")
            for b in sorted(groups):
                lo_e = 0.0 if b == 0 else edges[b - 1]
                hi_e = edges[b] if b < len(edges) else float("inf")
                lbl = f"[{lo_e:>7.2f},{hi_e:>8.2f})" if hi_e != float("inf") \
                    else f"[{lo_e:>7.2f},     inf)"
                pt, lo, hi, nc = cluster_bootstrap(groups[b], reps=reps)
                med = statistics.median(v for _, v in groups[b])
                pp = statistics.mean(v for _, v in dollars[b]) if dollars[b] else float("nan")
                print(f"{lbl:<26}{len(groups[b]):>5}{nc:>6}{pt:>9.3f}{lo:>9.3f}"
                      f"{hi:>9.3f}{med:>9.3f}{pp:>9.2f}")

            # GRADIENT: top quartile minus bottom quartile, bootstrapped jointly
            top, bot = max(groups), min(groups)
            if top != bot:
                pooled: dict[str, dict[int, list[float]]] = defaultdict(
                    lambda: defaultdict(list))
                for b in (bot, top):
                    for c, v in groups[b]:
                        pooled[c][b].append(v)
                keys = list(pooled)
                rng = random.Random(7 + int(h))

                def diff(sample: list[str], pooled=pooled, bot=bot, top=top) -> float | None:
                    a: list[float] = []
                    z: list[float] = []
                    for c in sample:
                        a.extend(pooled[c][bot])
                        z.extend(pooled[c][top])
                    if not a or not z:
                        return None
                    return statistics.mean(z) - statistics.mean(a)

                pt = diff(keys)
                draws = []
                for _ in range(reps):
                    s = [keys[rng.randrange(len(keys))] for _ in keys]
                    d = diff(s)
                    if d is not None:
                        draws.append(d)
                draws.sort()
                if draws and pt is not None:
                    lo = draws[int(0.025 * len(draws))]
                    hi = draws[int(0.975 * len(draws))]
                    sig = "SIGNIFICANT" if (lo > 0) == (hi > 0) else "not significant"
                    print(f"  GRADIENT top-Q minus bottom-Q = {pt:+.3f}c "
                          f"[{lo:+.3f}, {hi:+.3f}]  -> {sig}")

            # log-size slope (continuous, cluster-bootstrapped)
            pts = []
            for f in usable:
                v = (f.mo or {}).get((ruler, h))
                s = size_of(f)
                if v is None or s is None or s <= 0:
                    continue
                pts.append((f.cluster, math.log(s), v))
            if len(pts) > 10:
                byc: dict[str, list[tuple[float, float]]] = defaultdict(list)
                for c, x, y in pts:
                    byc[c].append((x, y))
                keys = list(byc)
                rng = random.Random(11 + int(h))

                def slope(sample: list[str], byc=byc) -> float | None:
                    xs: list[float] = []
                    ys: list[float] = []
                    for c in sample:
                        for x, y in byc[c]:
                            xs.append(x)
                            ys.append(y)
                    if len(xs) < 3:
                        return None
                    mx = statistics.mean(xs)
                    my = statistics.mean(ys)
                    den = sum((x - mx) ** 2 for x in xs)
                    if den == 0:
                        return None
                    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den

                pt = slope(keys)
                draws = []
                for _ in range(reps):
                    s = [keys[rng.randrange(len(keys))] for _ in keys]
                    d = slope(s)
                    if d is not None:
                        draws.append(d)
                draws.sort()
                if draws and pt is not None:
                    lo = draws[int(0.025 * len(draws))]
                    hi = draws[int(0.975 * len(draws))]
                    sig = "SIGNIFICANT" if (lo > 0) == (hi > 0) else "not significant"
                    print(f"  SLOPE d(markout)/d(ln size) = {pt:+.3f} c per e-fold "
                          f"[{lo:+.3f}, {hi:+.3f}]  -> {sig}")


def price_interaction(fills: list[Fill], reps: int) -> None:
    """The operator's constraint #4: cheap NO (<50c) is the profitable bucket.
    Does the SIZE axis correlate with the PRICE axis? If big tickets ARE the
    cheap-NO tickets, a notional markup taxes the money-maker."""
    print(f"\n{'='*78}\nSIZE x PRICE INTERACTION  (does a notional markup tax cheap NO?)")
    edges = size_buckets([f.premium_usd for f in fills], 4)
    tab: dict[tuple[int, str], list[Fill]] = defaultdict(list)
    for f in fills:
        b = bucket_of(f.premium_usd, edges)
        pb = "cheapNO<50c" if f.price_c < 50 else ("mid50-85c" if f.price_c < 85 else "richNO>=85c")
        tab[(b, pb)].append(f)
    print(f"{'size bucket':<26}{'cheapNO<50c':>14}{'mid50-85c':>14}{'richNO>=85c':>14}")
    for b in range(4):
        lo_e = 0.0 if b == 0 else edges[b - 1]
        hi_e = edges[b] if b < len(edges) else float("inf")
        lbl = f"[{lo_e:>7.2f},{hi_e:>8.2f})" if hi_e != float("inf") else f"[{lo_e:>7.2f},     inf)"
        cells = [len(tab[(b, p)]) for p in ("cheapNO<50c", "mid50-85c", "richNO>=85c")]
        tot = sum(cells) or 1
        print(f"{lbl:<26}" + "".join(f"{c:>6} ({100*c/tot:>4.0f}%)" for c in cells))
    # WHICH "dollar amount"?  The operator said "a bigger dollar amount", and a
    # combo has TWO: the PREMIUM we pay (= our max loss) and the SETTLEMENT
    # NOTIONAL (contracts x $1) the ticket is worth. They rank tickets
    # DIFFERENTLY, and the difference is exactly the cheap-NO axis: premium =
    # contracts x price, so at a fixed premium a 30c NO carries 3x the
    # contracts (and 3x the payout) of a 90c NO. A markup keyed on PAYOUT
    # therefore lands hardest on the cheap-NO tickets — the +77.9% bucket.
    print(f"\n{'-'*78}\nthe two 'dollar amounts' rank tickets differently:")
    pedges = size_buckets([f.payout_usd for f in fills], 4)
    print(f"{'PAYOUT bucket USD':<26}{'n':>6}{'cheapNO<50c':>14}{'mean price c':>14}"
          f"{'mean premium':>14}")
    for b in range(4):
        sel = [f for f in fills if bucket_of(f.payout_usd, pedges) == b]
        if not sel:
            continue
        lo_e = 0.0 if b == 0 else pedges[b - 1]
        hi_e = pedges[b] if b < len(pedges) else float("inf")
        lbl = f"[{lo_e:>7.2f},{hi_e:>8.2f})" if hi_e != float("inf") else f"[{lo_e:>7.2f},     inf)"
        cheap = sum(1 for f in sel if f.price_c < 50)
        print(f"{lbl:<26}{len(sel):>6}{100*cheap/len(sel):>13.0f}%"
              f"{statistics.mean(f.price_c for f in sel):>14.2f}"
              f"{statistics.mean(f.premium_usd for f in sel):>14.2f}")

    # mean price by size bucket, cluster-bootstrapped
    print(f"\n{'size bucket':<26}{'mean NO price c':>18}{'lo95':>9}{'hi95':>9}{'n':>6}")
    for b in range(4):
        items = [(f.cluster, f.price_c) for f in fills if bucket_of(f.premium_usd, edges) == b]
        pt, lo, hi, nc = cluster_bootstrap(items, reps=reps)
        lo_e = 0.0 if b == 0 else edges[b - 1]
        hi_e = edges[b] if b < len(edges) else float("inf")
        lbl = f"[{lo_e:>7.2f},{hi_e:>8.2f})" if hi_e != float("inf") else f"[{lo_e:>7.2f},     inf)"
        print(f"{lbl:<26}{pt:>18.2f}{lo:>9.2f}{hi:>9.2f}{len(items):>6}")


def price_stratified(fills: list[Fill], reps: int, horizon: float = 1800.0) -> None:
    """THE DECISIVE TEST. premium = contracts x price, so the size axis is
    MECHANICALLY entangled with the NO-price axis (measured: mean NO price
    52.3c -> 67.2c across size quartiles). A raw size gradient can therefore be
    a PRICE gradient wearing a size costume. Stratify on the NO price band and
    ask whether ANY size gradient survives inside a band."""
    print(f"\n{'='*78}\nPRICE-STRATIFIED SIZE GRADIENT  (horizon={int(horizon)}s, "
          f"clustering=day)")
    bands = (("cheapNO <50c", 0.0, 50.0), ("mid 50-85c", 50.0, 85.0),
             ("rich >=85c", 85.0, 101.0))
    for ruler in ("fair", "raw"):
        print(f"\n-- ruler={ruler}")
        print(f"{'price band':<16}{'size half':<12}{'n':>5}{'clus':>6}{'mean':>9}"
              f"{'lo95':>9}{'hi95':>9}")
        for lbl, lo_p, hi_p in bands:
            sel = [f for f in fills if lo_p <= f.price_c < hi_p
                   and (f.mo or {}).get((ruler, horizon)) is not None]
            if len(sel) < 20:
                print(f"{lbl:<16}{'(n<20 — no test)':<12}{len(sel):>5}")
                continue
            med = statistics.median(f.premium_usd for f in sel)
            halves: dict[str, list[tuple[str, float]]] = defaultdict(list)
            for f in sel:
                k = "small" if f.premium_usd < med else "large"
                halves[k].append((f.cluster_day, (f.mo or {})[(ruler, horizon)]))
            for k in ("small", "large"):
                pt, blo, bhi, nc = cluster_bootstrap(halves[k], reps=reps)
                print(f"{lbl if k=='small' else '':<16}{k+f' (<{med:.1f})' if k=='small' else k:<12}"
                      f"{len(halves[k]):>5}{nc:>6}{pt:>9.3f}{blo:>9.3f}{bhi:>9.3f}")
            # bootstrapped difference within the band
            pooled: dict[str, dict[str, list[float]]] = defaultdict(
                lambda: defaultdict(list))
            for k in ("small", "large"):
                for c, v in halves[k]:
                    pooled[c][k].append(v)
            keys = list(pooled)
            rng = random.Random(23)

            def d(sample: list[str], pooled=pooled) -> float | None:
                a: list[float] = []
                z: list[float] = []
                for c in sample:
                    a.extend(pooled[c]["small"])
                    z.extend(pooled[c]["large"])
                return statistics.mean(z) - statistics.mean(a) if a and z else None

            pt = d(keys)
            draws = [x for x in (d([keys[rng.randrange(len(keys))] for _ in keys])
                                 for _ in range(reps)) if x is not None]
            draws.sort()
            if draws and pt is not None:
                blo, bhi = draws[int(0.025 * len(draws))], draws[int(0.975 * len(draws))]
                sig = "SIGNIFICANT" if (blo > 0) == (bhi > 0) else "not significant"
                print(f"{'':<16}{'DIFF':<12}{'':>5}{'':>6}{pt:>9.3f}{blo:>9.3f}"
                      f"{bhi:>9.3f}   -> {sig}")


def settlement_lens(conn: sqlite3.Connection, fills: list[Fill], reps: int) -> None:
    """SECONDARY ruler: realized settlement P&L by ticket size.
    CAVEAT: position_ledger measured 14.78% wrong on dollars vs the exchange
    (2026-07-29 audit) — read as a direction check, never as a number."""
    print(f"\n{'='*78}\nSETTLEMENT LENS (position_ledger, SUSPECT STORE — direction only)")
    rows = conn.execute(
        "SELECT combo_ticker, opened_at, contracts_centi, entry_price_cc, cost_cc, "
        "realized_pnl_cc FROM position_ledger WHERE status='settled' "
        "AND realized_pnl_cc IS NOT NULL"
    ).fetchall()
    cl_by_ticker = {f.combo_ticker: f.cluster for f in fills}
    recs = []
    for ticker, _opened, cc_ct, _entry, cost_cc, pnl_cc in rows:
        cost = cost_cc / CC_PER_DOLLAR
        if cost <= 0:
            continue
        recs.append((cl_by_ticker.get(ticker, "orphan:" + ticker), cost, pnl_cc / CC_PER_DOLLAR))
    print(f"settled positions: {len(recs)}   matched to a fill cluster: "
          f"{sum(1 for r in recs if not r[0].startswith('orphan:'))}")
    if not recs:
        return
    edges = size_buckets([r[1] for r in recs], 4)
    print(f"{'cost bucket USD':<26}{'n':>5}{'clus':>6}{'ROI%':>9}{'lo95':>9}{'hi95':>9}"
          f"{'$pnl':>10}{'$cost':>10}")
    for b in range(4):
        sel = [r for r in recs if bucket_of(r[1], edges) == b]
        if not sel:
            continue
        items = [(c, 100.0 * p / cst) for c, cst, p in sel]
        pt, lo, hi, nc = cluster_bootstrap(items, reps=reps)
        lo_e = 0.0 if b == 0 else edges[b - 1]
        hi_e = edges[b] if b < len(edges) else float("inf")
        lbl = f"[{lo_e:>7.2f},{hi_e:>8.2f})" if hi_e != float("inf") else f"[{lo_e:>7.2f},     inf)"
        print(f"{lbl:<26}{len(sel):>5}{nc:>6}{pt:>9.2f}{lo:>9.2f}{hi:>9.2f}"
              f"{sum(r[2] for r in sel):>10.2f}{sum(r[1] for r in sel):>10.2f}")


def settlement_by_price(conn: sqlite3.Connection, fills: list[Fill], reps: int) -> None:
    """Settlement ROI on the PRICE axis and jointly with size — the operator's
    constraint #4 (cheap NO <50c returned +77.9%, rich NO >=85c only +1.1%)
    restated on this window, so the size verdict can be read against it."""
    print(f"\n{'='*78}\nSETTLEMENT ROI by NO-PRICE band, then size WITHIN band "
          f"(position_ledger, SUSPECT STORE)")
    rows = conn.execute(
        "SELECT combo_ticker, contracts_centi, entry_price_cc, cost_cc, realized_pnl_cc "
        "FROM position_ledger WHERE status='settled' AND realized_pnl_cc IS NOT NULL"
    ).fetchall()
    cl = {f.combo_ticker: f.cluster_day for f in fills}
    recs = []
    for ticker, _ct, entry_cc, cost_cc, pnl_cc in rows:
        if cost_cc <= 0:
            continue
        recs.append((cl.get(ticker, "unmatched"), entry_cc / 100.0,
                     cost_cc / CC_PER_DOLLAR, pnl_cc / CC_PER_DOLLAR))
    bands = (("cheapNO <50c", 0.0, 50.0), ("mid 50-85c", 50.0, 85.0),
             ("rich >=85c", 85.0, 101.0))
    print(f"{'band':<16}{'size half':<14}{'n':>5}{'clus':>6}{'ROI%':>9}{'lo95':>9}"
          f"{'hi95':>9}{'$pnl':>10}{'$cost':>10}")
    for lbl, lo_p, hi_p in bands:
        sel = [r for r in recs if lo_p <= r[1] < hi_p]
        if not sel:
            continue
        pt, blo, bhi, nc = cluster_bootstrap([(c, 100.0 * p / cst) for c, _, cst, p in sel],
                                             reps=reps)
        print(f"{lbl:<16}{'ALL':<14}{len(sel):>5}{nc:>6}{pt:>9.2f}{blo:>9.2f}{bhi:>9.2f}"
              f"{sum(r[3] for r in sel):>10.2f}{sum(r[2] for r in sel):>10.2f}")
        if len(sel) < 20:
            continue
        med = statistics.median(r[2] for r in sel)
        for k, pred in (("small", lambda c, m=med: c < m),
                        ("large", lambda c, m=med: c >= m)):
            sub = [r for r in sel if pred(r[2])]
            if not sub:
                continue
            pt, blo, bhi, nc = cluster_bootstrap(
                [(c, 100.0 * p / cst) for c, _, cst, p in sub], reps=reps)
            print(f"{'':<16}{k + f' (${med:.1f})':<14}{len(sub):>5}{nc:>6}{pt:>9.2f}"
                  f"{blo:>9.2f}{bhi:>9.2f}{sum(r[3] for r in sub):>10.2f}"
                  f"{sum(r[2] for r in sub):>10.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--reps", type=int, default=5000)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, uri=True)
    fills = load(conn)
    print(f"fills loaded: {len(fills)}")
    print(f"with rfq join: {sum(1 for f in fills if f.games)}")
    print(f"with target_cost: {sum(1 for f in fills if f.req_cost_usd is not None)}")
    prem = sorted(f.premium_usd for f in fills)
    print(f"premium USD: min {prem[0]:.2f} med {prem[len(prem)//2]:.2f} "
          f"p90 {prem[int(0.9*len(prem))]:.2f} max {prem[-1]:.2f} total {sum(prem):.2f}")

    for clustering in ("day", "game"):
        for axis in ("fill", "req"):
            report(fills, args.reps, axis=axis, clustering=clustering)
    for f in fills:
        f.cluster = f.cluster_day
    price_interaction(fills, args.reps)
    for h in (300.0, 1800.0):
        price_stratified(fills, args.reps, horizon=h)
    settlement_lens(conn, fills, args.reps)
    settlement_by_price(conn, fills, args.reps)


if __name__ == "__main__":
    main()
