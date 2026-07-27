"""COLLECT real live CANDIDATE-GATE inputs for the cheap-ΔP(book) fidelity study.

Phase 1 of 2 (phase 2 = ``pbook_cheap_marginal_fidelity.py``, fully OFFLINE).

WHY a two-phase split: ``CandidateBookRiskInputs`` is picklable BY DESIGN (it is
what crosses the process boundary to the MC worker pool), so one read-only live
touch captures everything the fidelity study needs — the REAL committed book, the
REAL leg marginals, the REAL within-game pair rho bands, the REAL structural cfg,
the REAL candidate position (size + price from the live quote path) — and every
subsequent experiment runs offline, repeatably, with zero further exchange load.

READ-ONLY BY CONSTRUCTION (inherits ``restart_gate2_quote_validation``):
  * live store opened mode=ro, decision tape on a throwaway scratch DB
  * ``PaperSender`` — nothing is ever POSTed; exchange calls are GETs + a
    read-only orderbook WS subscription
  * every reservation taken by the quote path is RELEASED immediately

Hard rule 8 (testing isolation): no live module is edited and no live logic is
reimplemented. The wiring/rehydration is imported verbatim from the gate-2 probe;
the candidate inputs come from the lifecycle's OWN ``_build_candidate_gate_inputs``.

    .venv/Scripts/python.exe tools/diagnostics/pbook_cheap_marginal_collect.py \
        --max 40 --out data/_pbook_cheap/inputs.pkl
"""

from __future__ import annotations

import argparse
import asyncio
import pickle
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import tools.diagnostics.restart_gate2_quote_validation as g2  # noqa: E402
from combomaker.core.conventions import Side as _Side  # noqa: E402
from combomaker.ops.logging import configure_logging  # noqa: E402
from combomaker.ops.quote_app import QuoteApp  # noqa: E402
from combomaker.rfq.models import Rfq, RfqParseError  # noqa: E402


async def _harvest(w: g2.Wired, pages: int) -> list[Rfq]:
    seen: dict[str, Rfq] = {}
    cursor = ""
    for _ in range(pages):
        params: dict[str, Any] = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        payload = await w.rest.get_rfqs(**params)
        for row in payload.get("rfqs") or []:
            try:
                r = Rfq.from_ws(row)
            except RfqParseError:
                continue
            if r.is_combo:
                seen[r.rfq_id] = r
        cursor = str(payload.get("cursor") or "")
        if not cursor:
            break
    return list(seen.values())


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", default="data/_pbook_cheap")
    ap.add_argument("--out", default="data/_pbook_cheap/inputs.pkl")
    ap.add_argument("--pages", type=int, default=8)
    ap.add_argument("--max", type=int, default=40)
    ap.add_argument("--attempts", type=int, default=120)
    ap.add_argument("--sort", default="legs", choices=("legs", "mix"))
    ap.add_argument(
        "--relax", type=int, default=0,
        help="HARNESS-ONLY cap relaxation multiplier. The LIVE book is currently "
             "cap-saturated (skip_portfolio_cvar/det_max on every RFQ), so no quote "
             "constructs and no candidate can be captured. Relaxing the harness's "
             "OWN LimitChecker (PaperSender — nothing is ever sent) lets the REAL "
             "pricing path produce the REAL fair/bid and the REAL RFQ-requested "
             "size. It changes NOTHING about the ΔP(book) measurement, which is a "
             "pure function of (book, candidate, marginals, rho).",
    )
    args = ap.parse_args()

    configure_logging(json_output=False, level="warning")
    scratch = Path(args.scratch)
    w = await g2.wire(scratch)
    shim = g2._Shim(w.cfg)
    await g2.rehydrate(w, shim)
    lc = w.lifecycle

    if args.relax > 1:
        import dataclasses
        from fractions import Fraction as _F

        lim = w.limits.limits
        patch: dict[str, Any] = {}
        for fld in dataclasses.fields(lim):
            val = getattr(lim, fld.name)
            if val is None:
                continue
            if fld.name.endswith("_frac") and isinstance(val, _F):
                patch[fld.name] = val * args.relax
            elif fld.name.endswith(("_dollars", "_contracts")) and isinstance(
                val, (int, float)
            ):
                patch[fld.name] = type(val)(val * args.relax)
        w.limits.set_limits(dataclasses.replace(lim, **patch))
        print(f"HARNESS cap relaxation x{args.relax} applied ({len(patch)} fields)")

    print(f"committed positions rehydrated: {len(w.exposure.positions)}")
    legs = sorted({l.market_ticker for p in w.exposure.positions.values() for l in p.legs})
    have = sum(1 for t in legs if lc._marginals(t) is not None)
    print(f"committed legs: {len(legs)}  with a usable marginal: {have}")

    rfqs = await _harvest(w, args.pages)
    allowed = tuple(w.cfg.filters.allowed_leg_series_prefixes)
    cand = [
        r for r in rfqs
        if all(any(t.startswith(p) for p in allowed) for t in r.leg_tickers)
    ]
    print(f"harvested {len(rfqs)} combo RFQs; {len(cand)} allowlisted")
    if args.sort == "mix":
        # ROUND-ROBIN by leg count so the sample carries every shape the book
        # actually sees, not just the most numerous one.
        buckets: dict[int, list[Rfq]] = {}
        for r in cand:
            buckets.setdefault(len(r.legs), []).append(r)
        for b in buckets.values():
            b.sort(key=lambda r: r.rfq_id)
        mixed: list[Rfq] = []
        i = 0
        while any(len(b) > i for b in buckets.values()):
            for k in sorted(buckets):
                if len(buckets[k]) > i:
                    mixed.append(buckets[k][i])
            i += 1
        cand = mixed
    else:
        cand.sort(key=lambda r: (len(r.legs), r.rfq_id))

    # BATCH WARM: subscribe every candidate RFQ's legs ONCE (the harness shares the
    # live bot's read budget — a per-RFQ warm loop just collects 429s), then wait for
    # the read-only orderbook WS to deliver. Only RFQs whose legs are ALL priceable
    # afterwards are driven, so a decline is a RISK decline, never a warm artifact.
    pool = cand[: args.attempts]
    for i, r in enumerate(pool):
        for attempt in range(6):
            try:
                await QuoteApp._ensure_watched(shim, r, w.feed, w.metadata)
            except Exception:  # noqa: BLE001
                pass
            if all(w.metadata.peek(t) is not None for t in r.leg_tickers) and (
                w.metadata.peek(r.market_ticker) is not None
            ):
                break
            await asyncio.sleep(1.5 * (attempt + 1))
        if i % 10 == 0:
            print(f"  warmed {i + 1}/{len(pool)}")
    all_tickers = sorted({t for r in pool for t in r.leg_tickers})
    await g2._await_books(w, all_tickers, timeout_s=45.0)
    ok_books = sum(1 for t in all_tickers if w.feed.book(t).valid)
    print(f"  legs with a live book: {ok_books}/{len(all_tickers)}")

    # The portfolio-CVaR / det-max caps FAIL CLOSED without a book-risk snapshot
    # (the harness has no BookRiskPool / maintenance loop), so arm the snapshot the
    # same way the live startup path does — the LIVE inline recompute.
    committed_legs = sorted(
        {
            l.market_ticker
            for p in w.exposure.positions.values()
            for l in p.legs
            if l.market_ticker != p.combo_ticker
        }
    )
    subscribed = [t for t in committed_legs if t in w.feed._books]
    await g2._await_books(w, subscribed, timeout_s=30.0)
    for _ in range(6):
        lc._maybe_resolve_settled_marginals()
        task = getattr(lc, "_settled_task", None)
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=25.0)
            except Exception:  # noqa: BLE001
                pass
        if all(lc._marginals(t) is not None for t in committed_legs):
            break
    lc.recompute_book_risk()
    snap = lc._book_risk
    print(
        f"  book-risk snapshot: usable={None if snap is None else snap.usable} "
        f"n_pos={None if snap is None else snap.n_positions} "
        f"p_book={None if snap is None else round(snap.p_profit, 4)}"
    )

    reasons: list[str] = []
    orig_record = lc._record_skip

    async def spy(r, rs, ctx=None):  # type: ignore[no-untyped-def]
        reasons.extend(str(x) for x in rs)
        return await orig_record(r, rs, ctx)

    lc._record_skip = spy  # type: ignore[assignment]

    collected: list[dict[str, Any]] = []
    tried = 0
    for r in pool:
        if len(collected) >= args.max:
            break
        if not all(w.feed.book(t).valid for t in r.leg_tickers):
            continue
        tried += 1
        before = set(lc._open)
        try:
            await lc.handle_rfq(r)
        except Exception as exc:  # noqa: BLE001
            print(f"  handle_rfq EXC {r.rfq_id[:8]}: {exc!r}")
            continue
        new = [q for q in lc._open if q not in before]
        if not new:
            continue
        qid = new[0]
        st = lc._open[qid]
        c = st.constructed
        st.pending_fill = (_Side.NO, c.no_bid_cc, st.risk_qty)
        try:
            inputs = lc._build_candidate_gate_inputs(f"study:{qid}", st)
        except Exception as exc:  # noqa: BLE001
            print(f"  inputs EXC {r.rfq_id[:8]}: {exc!r}")
            lc._drop_quote(qid)
            continue
        finally:
            pass
        collected.append(
            {
                "rfq_id": r.rfq_id,
                "n_legs": len(r.legs),
                "leg_tickers": tuple(r.leg_tickers),
                "families": sorted({t.split("-", 1)[0] for t in r.leg_tickers}),
                "no_bid_cc": int(c.no_bid_cc),
                "fair_cc": int(c.fair_cc),
                "risk_qty_cc": int(st.risk_qty),
                "inputs": inputs,
            }
        )
        print(
            f"  [{len(collected):>3}] {r.rfq_id[:8]} {len(r.legs)}leg "
            f"qty={int(st.risk_qty)/100:.2f} no_bid={int(c.no_bid_cc)/100:.2f}c "
            f"n_committed={len(inputs.committed)} n_resv={len(inputs.reservations)} "
            f"n_marg={len(inputs.marginals)}"
        )
        lc._drop_quote(qid)

    lc._record_skip = orig_record  # type: ignore[assignment]
    from collections import Counter

    print(f"\ndriven {tried} RFQs with live books; quotes constructed {len(collected)}")
    print("decline reasons:")
    for reason, n in Counter(reasons).most_common(15):
        print(f"   {reason:<48} {n}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as f:
        pickle.dump(collected, f)
    print(f"\nwrote {len(collected)} candidate-gate input sets -> {out}")

    try:
        await w.rest.__aexit__(None, None, None)
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
