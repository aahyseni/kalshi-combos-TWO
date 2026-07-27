"""SHIP GATE for the SOLVED deployment scale (operator standing rule).

"A risk cap must be proven to produce a non-zero quote against real trade sizes
BEFORE going live." A 100%-decline is a NO-SHIP.

This drives the LIVE path end to end on the LIVE book:
  1. rehydrate the real exposure book (mode=ro store, GETs only);
  2. SOLVE the deployment scale with the shipped ``QuoteLifecycle.solve_deploy_scale``
     — the production code, not a reimplementation;
  3. drive REAL harvested RFQ shapes (2-leg, 4-leg ML parlay, 4-6 leg K combo,
     cross-family, one touching an already-concentrated arm) through
     ``handle_rfq`` + the CONFIRM-path reservation, BOTH at s = 1 (today) and at
     the solved s, reporting price/size or the exact ReasonCode for each.

The drive, wiring, rehydration and shape harvest are IMPORTED VERBATIM from
``restart_gate2_quote_validation`` (hard rule 8) — the only thing this adds is
arming the solved scale between the two passes.

READ-ONLY: store mode=ro, PaperSender, GETs only. Never places an order.

    .venv/Scripts/python.exe tools/diagnostics/deploy_scale_ship_gate.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import tools.diagnostics.restart_gate2_quote_validation as g2  # noqa: E402
from combomaker.rfq.models import Rfq, RfqParseError  # noqa: E402
from combomaker.risk.deploy_scale import (  # noqa: E402
    DEPLOY_BUDGET_FIELDS,
    scale_deploy_budgets,
)
from combomaker.risk.limits import threshold_cc  # noqa: E402

d = g2.d


def arm(cfg: object, **kw: object) -> None:
    for k, v in kw.items():
        object.__setattr__(cfg, k, v)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", default="data/_deploy_scale_ship")
    ap.add_argument("--pages", type=int, default=20)
    ap.add_argument("--grid-points", type=int, default=16)
    ap.add_argument("--s-max", type=float, default=3.0)
    ap.add_argument("--samples", type=int, default=20_000)
    args = ap.parse_args()
    from combomaker.ops.logging import configure_logging

    configure_logging(json_output=False, level="warning")

    w = await g2.wire(Path(args.scratch))
    try:
        lc = w.lifecycle
        bank = w.balance.risk_bankroll_cc_or_none()
        print("=" * 100)
        print(f"BANKROLL (risk) {d(bank)}")
        shim = g2._Shim(w.cfg)
        await g2.rehydrate(w, shim)
        positions = list(w.exposure.positions.values())
        print(
            f"positions rehydrated: {len(positions)}   "
            f"premium-at-risk {d(sum(p.max_loss_cc for p in positions))}"
        )
        legs = sorted(
            {
                l.market_ticker
                for p in positions
                for l in p.legs
                if l.market_ticker != p.combo_ticker
            }
        )
        await g2._await_books(w, legs, timeout_s=45.0)
        for _ in range(3):
            if all(lc._marginals(t) is not None for t in legs):
                break
            lc._maybe_resolve_settled_marginals()
            task = getattr(lc, "_settled_task", None)
            if task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=25.0)
                except Exception:
                    pass
        lc.recompute_book_risk()
        snap = lc._book_risk
        print(f"book-risk snapshot usable: {None if snap is None else snap.usable}")

        # ---------------- SOLVE with the SHIPPED production code --------------
        arm(
            lc._config,
            deploy_scale_enabled=True,
            deploy_scale_s_max=args.s_max,
            deploy_scale_grid_points=args.grid_points,
            deploy_scale_mc_samples=args.samples,
        )
        lc.solve_deploy_scale()
        res = lc._deploy_scale
        print("=" * 100)
        print(
            f"SOLVED s = {res.scale:.4f}   solved={res.solved}  "
            f"evaluations={res.evaluations}  solve_ms={res.solve_ms:.0f}  "
            f"binding={list(res.binding)}"
        )
        print(f"    reason: {res.reason}")
        print(f"    live consumption value: {lc.deploy_scale_for_check():.4f}")

        lim = w.limits.limits
        scaled = scale_deploy_budgets(lim, res.scale)
        print("    DEPLOY budgets (the only fields that breathe):")
        for name in DEPLOY_BUDGET_FIELDS:
            a, b = getattr(lim, name), getattr(scaled, name)
            if a is None:
                print(f"        {name:<24} unarmed (never invented by a scale)")
                continue
            print(
                f"        {name:<24} {str(a):>12} -> {str(b):>12}   "
                f"{d(threshold_cc(a, bank))} -> {d(threshold_cc(b, bank))}"
            )
        print("    ENVELOPE + HALTS (invariant by construction):")
        for name in (
            "portfolio_cvar_frac", "portfolio_det_max_frac",
            "portfolio_ruin_prob_budget", "daily_loss_frac", "drawdown_frac",
            "hard_trip_frac",
        ):
            a, b = getattr(lim, name), getattr(scaled, name)
            flag = "OK" if a == b else "!! MOVED !!"
            print(f"        {name:<28} {str(a):>10} -> {str(b):>10}  {flag}")

        # ---------------- harvest real shapes ---------------------------------
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
        allowed = tuple(w.cfg.filters.allowed_leg_series_prefixes)

        def quotable(r: Rfq) -> bool:
            return all(
                any(t.startswith(p) for p in allowed) for t in r.leg_tickers
            )

        pool = [r for r in seen.values() if quotable(r)]
        print("=" * 100)
        print(f"harvested {len(seen)} combo RFQs; {len(pool)} on allowlisted series")

        prof = lc._leg_axis_profile_from(
            w.exposure.snapshot(lc._marginals, mass_acceptance=True)
        )
        ent_cc = {k: s * prof.total_entity_cc for k, s in prof.shares_by_entity.items()}
        tops = sorted(ent_cc.items(), key=lambda kv: -kv[1])[:10]
        hot = {k.split(":")[1] for k, _ in tops if k.count(":") >= 2}

        picks: list[tuple[str, Rfq]] = []

        def pick(label: str, pred) -> None:  # type: ignore[no-untyped-def]
            for r in pool:
                if r.rfq_id in {x.rfq_id for _, x in picks}:
                    continue
                if pred(r):
                    picks.append((label, r))
                    return

        pick("A. 2-leg", lambda r: len(r.legs) == 2)
        pick(
            "B. 4-leg ML parlay",
            lambda r: len(r.legs) == 4 and all("GAME" in t for t in r.leg_tickers),
        )
        pick("B'. 4-leg (any family)", lambda r: len(r.legs) == 4)
        pick(
            "C. 4-6 leg K combo",
            lambda r: 4 <= len(r.legs) <= 6 and any("KS" in t for t in r.leg_tickers),
        )
        pick(
            "C'. 5-6 leg (any family)", lambda r: 5 <= len(r.legs) <= 6
        )
        pick(
            "D. cross-family / cross-sport",
            lambda r: len({t.split("-", 1)[0] for t in r.leg_tickers}) >= 2,
        )
        pick(
            "E. touches a concentrated arm",
            lambda r: bool(hot)
            and any(any(e in t for e in hot) for t in r.leg_tickers),
        )
        if not picks:
            print("!! no quotable shapes harvested — the gate proves NOTHING")
            return 2

        # ---------------- drive at s = 1, then at the solved s ----------------
        results: dict[str, dict[str, bool]] = {}
        for label_pass, scale_on in (("PASS 1  s = 1.00 (today)", False),
                                     (f"PASS 2  s = {res.scale:.4f} (solved)", True)):
            arm(lc._config, deploy_scale_enabled=scale_on)
            print("=" * 100)
            print(f"{label_pass}   effective deploy_scale = "
                  f"{lc.deploy_scale_for_check():.4f}")
            for label, r in picks:
                # keep=True so ``_drive`` RETURNS the reservation id on a grant
                # (with keep=False it releases and returns None either way — the
                # first cut read that as "declined" and mis-scored the gate).
                rid = await g2._drive(w, shim, label, r, keep=True)
                results.setdefault(label, {})["scaled" if scale_on else "base"] = (
                    rid is not None
                )
                if rid:
                    w.reservation.release(rid)

        print("=" * 100)
        print("SHIP-GATE SUMMARY (reservation GRANTED on the confirm path?)")
        print(f"    {'shape':<34}{'s=1':>10}{'s=solved':>12}")
        n_ok = 0
        for label, _r in picks:
            row = results.get(label, {})
            b = row.get("base", False)
            s = row.get("scaled", False)
            n_ok += int(s)
            print(f"    {label:<34}{str(b):>10}{str(s):>12}")
        print(f"    GRANTED at the solved scale: {n_ok}/{len(picks)}")
        print("    VERDICT:", "SHIP" if n_ok > 0 else "NO-SHIP (100% decline)")
        return 0 if n_ok > 0 else 1
    finally:
        try:
            await w.rest.__aexit__(None, None, None)
        except Exception:
            pass
        await w.scratch.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
