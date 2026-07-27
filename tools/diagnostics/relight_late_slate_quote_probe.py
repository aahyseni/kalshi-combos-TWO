"""RELIGHT GATE — can the CAPS still QUOTE tonight's REMAINING PREGAME games?

Operator standing rule (2026-07-23, feedback_validate_caps_quote): a risk cap
must be PROVEN to produce a non-zero quote against real trade sizes BEFORE going
live. A 100%-decline passes every safety test and is useless.

Scope: the games that have NOT started at run time (the 21:38 HOULAA / 21:40
BOSATH / 21:40 COLSD / 21:45 MILSF / 22:10 SEALAD slate). Everything earlier is
in play, and ``skip_inplay_leg`` on those is a CORRECT pregame-only refusal, not
a cap decline — grading the caps on in-play shapes proves nothing.

Reuses ``restart_gate2_quote_validation`` verbatim (hard rule 8: live modules
imported, never reimplemented) for wiring, rehydration and the drive; adds
(a) a deeper RFQ harvest, (b) the pure-late filter, and (c) an ACCUMULATION
LADDER that re-drives the available shapes WITHOUT releasing reservations, so
each successive check sees the prior ones folded in — the exact live path by
which a cap eventually refuses.

READ-ONLY: store mode=ro, PaperSender, GETs only. Never places an order.

    .venv/Scripts/python.exe tools/diagnostics/relight_late_slate_quote_probe.py
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from combomaker.rfq.models import Rfq, RfqParseError  # noqa: E402

import tools.diagnostics.restart_gate2_quote_validation as g2  # noqa: E402

SLOT = re.compile(r"-(\d{2}[A-Z]{3}\d{2})(\d{4})([A-Z]+)")


def slots_of(r: Rfq) -> set[str] | None:
    ks: set[str] = set()
    for t in r.leg_tickers:
        m = SLOT.search(t)
        if not m:
            return None
        ks.add(f"{m.group(2)}{m.group(3)}")
    return ks or None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", default="data/_relight_late_probe")
    ap.add_argument("--pages", type=int, default=30)
    ap.add_argument("--min-start", type=int, default=2130)
    ap.add_argument("--ladder", type=int, default=12)
    args = ap.parse_args()
    from combomaker.ops.logging import configure_logging

    configure_logging(json_output=False, level="warning")
    scratch = Path(args.scratch)

    w = await g2.wire(scratch)
    try:
        bank = w.balance.risk_bankroll_cc_or_none()
        print("=" * 100)
        print(f"BANKROLL  risk {g2.d(bank)}   cash {g2.d(w.balance.available_cash_cc_or_none())}")
        shim = g2._Shim(w.cfg)
        await g2.rehydrate(w, shim)
        print(f"positions rehydrated: {len(w.exposure.positions)}")

        # STAGE-2 EQUIVALENT — the live startup snapshot path. Without a USABLE
        # book-risk snapshot the portfolio caps fail CLOSED by design, so
        # skipping this would grade the harness, not the caps.
        pos = w.exposure.positions
        legs = sorted(
            {l.market_ticker for p in pos.values() for l in p.legs
             if l.market_ticker != p.combo_ticker}
        )
        await g2._await_books(w, legs, timeout_s=45.0)
        for _ in range(3):
            if all(w.lifecycle._marginals(t) is not None for t in legs):
                break
            w.lifecycle._maybe_resolve_settled_marginals()
            task = getattr(w.lifecycle, "_settled_task", None)
            if task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=25.0)
                except Exception:
                    pass
        w.lifecycle.recompute_book_risk()
        snap = w.lifecycle._book_risk
        print(f"book-risk snapshot usable: {None if snap is None else snap.usable}")
        if snap is not None and snap.usable:
            print(f"  n_positions {snap.n_positions}  p_book {snap.p_profit:.4f}"
                  f"  ev {g2.d(snap.ev_cc)}  p_ruin {snap.p_ruin:.4f}")
            print(f"  det gate {g2.d(min(int(snap.deterministic_max_loss_cc), int(snap.mutex_aware_det_max_cc or snap.deterministic_max_loss_cc)))}"
                  f"  governing ES99 {g2.d(snap.governing_model_es_99_cc)}")
        lim = w.limits.limits
        print("ENFORCED CAPS at this bankroll:")
        for name, frac, dollars in g2.cap_table(w, int(bank or 0)):
            print(f"   {name:<26} {frac:<10} {dollars}")

        # ---- deep harvest -------------------------------------------------
        seen: dict[str, Rfq] = {}
        cursor = ""
        for _ in range(args.pages):
            params: dict[str, object] = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            payload = await w.rest.get_rfqs(**params)  # type: ignore[arg-type]
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
        print("=" * 100)
        print(f"harvested {len(seen)} combo RFQs from the exchange tape")

        # QUOTABLE = every leg's game is (a) NOT started and (b) inside the
        # armed pregame horizon. Both come from the BOT'S OWN start-time
        # provider and the BOT'S OWN config knob — never a hand-set window.
        start_of = w.lifecycle._start_time_provider
        horizon_h = w.cfg.filters.max_pregame_hours_by_prefix.get("KXMLB", 0.0)
        now = w.clock.now()
        print(f"armed pregame horizon KXMLB = {horizon_h}h; "
              f"in-play cutoff = leg start")

        def hours_out(r: Rfq) -> float | None:
            hs = []
            for t in r.leg_tickers:
                s = start_of(t)
                if s is None:
                    return None
                hs.append((s - now).total_seconds() / 3600.0)
            return min(hs) if hs else None

        late: list[Rfq] = []
        for r in seen.values():
            h = hours_out(r)
            if h is None:
                continue
            maxh = max(
                ((start_of(t) - now).total_seconds() / 3600.0)  # type: ignore[operator]
                for t in r.leg_tickers
            )
            if h > 0.0 and maxh <= horizon_h:
                late.append(r)
        late.sort(key=lambda r: -len(r.legs))
        print(f"QUOTABLE shapes (every leg pregame AND inside the {horizon_h}h "
              f"horizon): {len(late)}")
        for r in late[:20]:
            ks = slots_of(r) or set()
            size = (f"target_cost {g2.d(r.target_cost_cc)}" if r.target_cost_cc
                    else f"contracts {int(r.contracts or 0)/100:.2f}")
            print(f"   {r.rfq_id[:8]}  {len(r.legs)}leg  {sorted(ks)}  {size}"
                  f"  (+{hours_out(r):.1f}h)")
        if not late:
            print("!! nothing to drive")
            return 1

        # ---- single-shot drives ------------------------------------------
        print("=" * 100)
        print("A. SINGLE-SHOT: each pregame shape through the REAL pricing +"
              " reservation path")
        for i, r in enumerate(late):
            await g2._drive(w, shim, f"shape #{i+1}", r)

        # ---- accumulation ladder -----------------------------------------
        print("=" * 100)
        print(f"B. ACCUMULATION LADDER — re-drive the same shapes WITHOUT"
              f" releasing, up to {args.ladder} grants, until a cap refuses")
        # Smallest-first, and DO NOT abort on a refusal: a big multi-game shape
        # that breaches per-combo on its own is a CORRECT decline, not the end
        # of the ladder. We want the point where ACCUMULATION starts refusing.
        ladder_pool = sorted(late, key=lambda r: len(r.legs))
        held: list[str] = []
        first_refusal: int | None = None
        for i in range(args.ladder):
            r = ladder_pool[i % len(ladder_pool)]
            rid = await g2._drive(w, shim, f"   ladder #{i+1}", r, keep=True)
            if rid:
                held.append(rid)
            elif first_refusal is None:
                first_refusal = i + 1
        for rid in held:
            w.reservation.release(rid)
        print("=" * 100)
        print(f"LADDER RESULT: {len(held)}/{args.ladder} reservations GRANTED and"
              f" HELD SIMULTANEOUSLY; first refusal at step {first_refusal}")
        print(f"VERDICT: {'CAPS CAN QUOTE (non-zero size granted on the real reservation path)' if held else 'CAPS ARE 100%-DECLINING - DO NOT RELIGHT'}")
        return 0 if held else 1
    finally:
        await w.scratch.close()
        await w.book_ws.stop()
        await w.rest.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
