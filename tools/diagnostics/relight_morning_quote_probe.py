"""RELIGHT GATE (MORNING) — can the CAPS QUOTE the slate the operator relights into?

Operator standing rule (2026-07-23, feedback_validate_caps_quote): a risk cap must
be PROVEN to produce a non-zero quote against real trade sizes BEFORE going live.
A 100%-decline passes every safety test and is useless.

Why a new entry point instead of ``restart_gate2_quote_validation`` directly: that
tool's shape selector pins ``today = now(ET)`` and ``min_start >= 18:30 ET``, i.e.
TONIGHT's slate. A relight that happens the NEXT MORNING has no tonight slate — the
only quotable flow is the NEXT day's games, which are already inside the armed
``max_pregame_hours_by_prefix`` horizon. Selecting on ``today`` would harvest zero
shapes and prove nothing (the same evidence gap ``relight_twoleg_quote_probe``
was written to close for 2-leg).

Selection here is done the same way the BOT does it: every leg's start comes from
``QuoteLifecycle._start_time_provider`` (= ``RfqFilter.leg_start_time``) and the
horizon comes from the BOT'S OWN config knob. No hand-set window, no hardcoded date.

Shapes driven (the operator's four):
    A. 2-leg
    B. 4-leg ML parlay (all legs KXMLBGAME)
    C. 4-6 leg strikeout combo (carries KXMLBKS)
    D. one touching an already-concentrated arm of the CURRENT book
       (+ D' a concentration LADDER on one arm when the committed book carries
        nothing on a tomorrow arm — the honest way to manufacture the wall)

Hard rule 8: wiring, rehydration, the book-risk snapshot and the drive are IMPORTED
from ``restart_gate2_quote_validation`` verbatim. No live module is edited and no
live logic is reimplemented here; only the SELECTION of which real RFQ to drive.

READ-ONLY: live store mode=ro, PaperSender, GETs only. Never places an order.

    .venv/Scripts/python.exe tools/diagnostics/relight_morning_quote_probe.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import tools.diagnostics.restart_gate2_quote_validation as g2  # noqa: E402
from combomaker.rfq.models import Rfq, RfqParseError  # noqa: E402
from combomaker.risk.limits import threshold_cc  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", default="data/_relight_morning_probe")
    ap.add_argument("--pages", type=int, default=30)
    ap.add_argument("--ladder", type=int, default=8)
    args = ap.parse_args()
    from combomaker.ops.logging import configure_logging

    configure_logging(json_output=False, level="warning")

    w = await g2.wire(Path(args.scratch))
    try:
        bank = w.balance.risk_bankroll_cc_or_none()
        cash = w.balance.available_cash_cc_or_none()
        print("=" * 100)
        print(f"BANKROLL  risk {g2.d(bank)}   cash {g2.d(cash)}"
              f"   equity(pnl) {g2.d(w.balance.pnl_equity_cc_or_none())}")

        shim = g2._Shim(w.cfg)
        await g2.rehydrate(w, shim)
        pos = w.exposure.positions
        print(f"positions rehydrated: {len(pos)}"
              f"   risk_modeled={sum(1 for p in pos.values() if p.risk_modeled)}"
              f"   reserved={sum(1 for p in pos.values() if not p.risk_modeled)}")
        prem = sum(int(p.contracts) * int(p.entry_price_cc) // 100 for p in pos.values())
        print(f"  premium at risk (entry basis): {g2.d(prem)}")
        for p in sorted(pos.values(), key=lambda x: -(int(x.contracts) * int(x.entry_price_cc))):
            print(f"    {p.position_id[:16]:<16} {p.our_side.value:>3} "
                  f"{int(p.contracts)/100:8.2f}ct @ {int(p.entry_price_cc)/100:6.2f}c "
                  f"maxloss {g2.d(int(p.contracts)*int(p.entry_price_cc)//100):>10} "
                  f"legs={len(p.legs)}")

        # ---- the LIVE startup book-risk snapshot path ----------------------
        legs = sorted(
            {l.market_ticker for p in pos.values() for l in p.legs
             if l.market_ticker != p.combo_ticker}
        )
        await g2._await_books(w, legs, timeout_s=45.0)
        for _ in range(4):
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
        bankroll = int(bank or 0)
        lim = w.limits.limits
        det_thr = threshold_cc(lim.portfolio_det_max_frac, bankroll)
        cvar_thr = threshold_cc(lim.portfolio_cvar_frac, bankroll)
        print("-" * 100)
        print(f"book-risk snapshot usable: {None if snap is None else snap.usable}")
        if snap is not None and snap.usable:
            det = int(snap.deterministic_max_loss_cc)
            mdet = None if snap.mutex_aware_det_max_cc is None else int(snap.mutex_aware_det_max_cc)
            gate = det if mdet is None or not lim.portfolio_det_max_mutex_aware else min(det, mdet)
            print(f"  n_positions {snap.n_positions}")
            print(f"  det_max {g2.d(det)}  mutex_aware {g2.d(mdet)}  GATE {g2.d(gate)}")
            print(f"  det WALL {lim.portfolio_det_max_frac} x {g2.d(bankroll)} = {g2.d(det_thr)}"
                  f"   HEADROOM {g2.d(det_thr - gate)}"
                  f" ({100.0*(det_thr-gate)/det_thr if det_thr else 0.0:+.1f}% of wall)")
            print(f"  governing ES99 {g2.d(snap.governing_model_es_99_cc)} vs cvar wall {g2.d(cvar_thr)}")
            print(f"  p_book {snap.p_profit:.4f}  p_night {snap.p_night:.4f}"
                  f"  ev {g2.d(snap.ev_cc)}  p_ruin {snap.p_ruin:.4f} (upper {snap.p_ruin_upper:.4f})")
        print("ENFORCED CAPS at this bankroll:")
        for name, frac, dollars in g2.cap_table(w, bankroll):
            print(f"   {name:<26} {frac:<10} {dollars}")

        # ---- harvest -------------------------------------------------------
        seen: dict[str, Rfq] = {}
        cursor = ""
        for _ in range(args.pages):
            params: dict[str, object] = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            payload = await w.rest.get_rfqs(**params)  # type: ignore[arg-type]
            rows = payload.get("rfqs") or []
            for row in rows:
                try:
                    r = Rfq.from_ws(row)
                except RfqParseError:
                    continue
                if r.is_combo:
                    seen[r.rfq_id] = r
            cursor = str(payload.get("cursor") or "")
            if not cursor or not rows:
                break
        print("=" * 100)
        print(f"harvested {len(seen)} combo RFQs from the exchange tape")

        # QUOTABLE = every leg's game NOT started AND inside the armed horizon.
        # Both from the bot's own provider / config — never a hand-set window.
        start_of = w.lifecycle._start_time_provider
        now = w.clock.now()
        horizons = dict(w.cfg.filters.max_pregame_hours_by_prefix)

        def horizon_for(t: str) -> float:
            for pref, h in horizons.items():
                if t.startswith(pref):
                    return float(h)
            return 0.0

        quotable: list[Rfq] = []
        for r in seen.values():
            starts = [start_of(t) for t in r.leg_tickers] if start_of else []
            if not starts or any(s is None for s in starts):
                continue
            hs = [(s - now).total_seconds() / 3600.0 for s in starts]  # type: ignore[operator]
            if min(hs) <= 0.0:
                continue  # a leg is in play -> correct pregame refusal, not a cap
            if any(h > horizon_for(t) for h, t in zip(hs, r.leg_tickers, strict=True)):
                continue
            quotable.append(r)
        print(f"QUOTABLE shapes (every leg pregame AND inside its armed horizon): "
              f"{len(quotable)}   horizons={horizons}")
        if not quotable:
            print("!! nothing to drive")
            return 1

        # top concentrated entity keys in the CURRENT book (for shape D)
        prof = w.lifecycle._leg_axis_profile_from(
            w.exposure.snapshot(w.lifecycle._marginals, mass_acceptance=True)
        )
        ent_cc = {k: s * prof.total_entity_cc for k, s in prof.shares_by_entity.items()}
        tops = sorted(ent_cc.items(), key=lambda kv: -kv[1])[:10]
        print("top entity keys in the rehydrated book (premium-at-risk):")
        for k, v in tops:
            print(f"   {k:<48} {g2.d(v)}")
        hot = {k.split(":")[1] for k, _ in tops if k.count(":") >= 2}

        picks: list[tuple[str, Rfq]] = []

        def pick(label: str, pred) -> None:  # type: ignore[no-untyped-def]
            for r in quotable:
                if r.rfq_id in {x.rfq_id for _, x in picks}:
                    continue
                if pred(r):
                    picks.append((label, r))
                    return

        pick("A. 2-leg", lambda r: len(r.legs) == 2)
        pick("B. 4-leg ML parlay",
             lambda r: len(r.legs) == 4 and all("KXMLBGAME" in t for t in r.leg_tickers))
        pick("B'. 4-leg (any family)", lambda r: len(r.legs) == 4)
        pick("C. 4-6 leg strikeout combo",
             lambda r: 4 <= len(r.legs) <= 6 and any("KXMLBKS" in t for t in r.leg_tickers))
        pick("C'. any KS-carrying combo", lambda r: any("KXMLBKS" in t for t in r.leg_tickers))
        pick("D. touches an already-concentrated book arm",
             lambda r: bool(hot) and any(any(e in t for e in hot) for t in r.leg_tickers))

        print("=" * 100)
        print("DRIVE THE REAL PRICING + RESERVATION PATH")
        for label, r in picks:
            # a usable book-risk snapshot is a PRECONDITION of the portfolio caps
            # (they fail closed when stale); the live maintenance tick refreshes it
            # on the same cadence, so refresh before each shape.
            w.lifecycle.recompute_book_risk()
            await g2._drive(w, shim, label, r)

        # ---- D': concentration ladder on ONE arm ---------------------------
        arms: dict[str, list[Rfq]] = {}
        for r in quotable:
            for t in r.leg_tickers:
                if "KXMLBKS" in t:
                    parts = t.split("-")
                    if len(parts) >= 3:
                        arms.setdefault(parts[2], []).append(r)
        held: list[str] = []
        if arms:
            arm, rs = max(arms.items(), key=lambda kv: len(kv[1]))
            print("=" * 100)
            print(f"D'. CONCENTRATION LADDER on ONE arm: {arm}  ({len(rs)} quotable RFQs)")
            for i, r in enumerate(rs[: args.ladder]):
                w.lifecycle.recompute_book_risk()
                rid = await g2._drive(w, shim, f"   ladder #{i+1} ({arm})", r, keep=True)
                if rid:
                    held.append(rid)
            for rid in held:
                w.reservation.release(rid)
            print(f"   ladder: {len(held)}/{min(args.ladder, len(rs))} reservations GRANTED "
                  f"and held simultaneously before the wall bit")

        print("=" * 100)
        print(f"VERDICT: {'CAPS CAN QUOTE' if held else 'see per-shape results above'}")
        return 0
    finally:
        await w.scratch.close()
        await w.book_ws.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
