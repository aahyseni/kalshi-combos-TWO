"""RELIGHT GATE — can the caps QUOTE a 2-LEG pregame combo?

Why this exists: ``relight_late_slate_quote_probe`` drives its harvested shapes
in DESCENDING leg order and recomputes the book-risk snapshot exactly ONCE, at
setup. Every 2-leg shape therefore lands at the END of a >20-minute drive, long
after that single snapshot aged past ``book_risk_stale_after_s`` (30 s) — so all
224 of them declined on ``skip_portfolio_cvar``/``skip_portfolio_det_max``
(the fail-closed staleness path), and the run proved NOTHING about 2-leg.

``filters.min_legs`` is 2, so 2-leg IS eligible flow. This probe closes that
evidence gap the honest way: drive ONLY 2-leg pregame shapes, and call the LIVE
``QuoteLifecycle.recompute_book_risk()`` before each one — which is precisely
what the live maintenance tick does via ``_maybe_recompute_book_risk`` (throttled
to ``book_risk_stale_after_s / 2``). Nothing else differs from the late-slate
probe.

Hard rule 8: wiring, rehydration, harvest and the drive are IMPORTED from
``restart_gate2_quote_validation`` / ``relight_late_slate_quote_probe`` verbatim.
No live module is edited and no logic is reimplemented here.

READ-ONLY: store mode=ro, PaperSender, GETs only. Never places an order.

    .venv/Scripts/python.exe tools/diagnostics/relight_twoleg_quote_probe.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import tools.diagnostics.restart_gate2_quote_validation as g2  # noqa: E402
from combomaker.rfq.models import Rfq, RfqParseError  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", default="data/_relight_twoleg_probe")
    ap.add_argument("--pages", type=int, default=30)
    ap.add_argument("--drives", type=int, default=10)
    args = ap.parse_args()
    from combomaker.ops.logging import configure_logging

    configure_logging(json_output=False, level="warning")

    w = await g2.wire(Path(args.scratch))
    try:
        bank = w.balance.risk_bankroll_cc_or_none()
        print("=" * 100)
        print(f"BANKROLL  risk {g2.d(bank)}")
        shim = g2._Shim(w.cfg)
        await g2.rehydrate(w, shim)
        print(f"positions rehydrated: {len(w.exposure.positions)}")

        # Same startup-snapshot path as the late-slate probe (a usable snapshot
        # is a precondition; skipping it would grade the harness, not the caps).
        pos = w.exposure.positions
        legs = sorted(
            {
                l.market_ticker
                for p in pos.values()
                for l in p.legs
                if l.market_ticker != p.combo_ticker
            }
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

        # ---- harvest (identical to the late-slate probe) -------------------
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
        print(f"harvested {len(seen)} combo RFQs")

        start_of = w.lifecycle._start_time_provider
        horizon_h = w.cfg.filters.max_pregame_hours_by_prefix.get("KXMLB", 0.0)
        now = w.clock.now()

        def pregame_inside_horizon(r: Rfq) -> bool:
            for t in r.leg_tickers:
                s = start_of(t)
                if s is None:
                    return False
                h = (s - now).total_seconds() / 3600.0
                if h <= 0.0 or h > horizon_h:
                    return False
            return True

        two = [r for r in seen.values() if len(r.legs) == 2 and pregame_inside_horizon(r)]
        print(f"2-LEG pregame shapes inside the {horizon_h}h horizon: {len(two)}")
        if not two:
            print("  !! none available — cannot grade 2-leg")
            return 2

        quoted = 0
        print("=" * 100)
        print("EACH DRIVE PRECEDED BY recompute_book_risk() — the live maintenance tick")
        for i, r in enumerate(two[: args.drives]):
            # THE ONE DIFFERENCE FROM THE LATE-SLATE PROBE: refresh the snapshot
            # first, exactly as _maybe_recompute_book_risk does on the 0.5s tick.
            w.lifecycle.recompute_book_risk()
            # keep=True so the granted reservation id comes back; released
            # IMMEDIATELY below, so no accumulation (that is the ladder's job,
            # and accumulation is what made the late-slate run unreadable).
            rid = await g2._drive(w, shim, f"2-leg #{i + 1}", r, keep=True)
            if rid is not None:
                quoted += 1
                w.reservation.release(rid)
        print("=" * 100)
        print(f"2-LEG RESULT: {quoted}/{min(len(two), args.drives)} produced a "
              f"QUOTE + GRANTED reservation on a FRESH snapshot")
        print("VERDICT:", "2-LEG QUOTES" if quoted else "2-LEG 100%-DECLINED")
        return 0 if quoted else 1
    finally:
        await w.scratch.close()
        await w.book_ws.stop()
        await w.rest.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
