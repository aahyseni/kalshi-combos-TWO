"""LEVER #1 MEASUREMENT — solve the deployment scale ``s`` on the LIVE book.

Answers, with real numbers off the live rehydrated book:
  * what the envelope says TODAY (s = 1): EV, E[log], P(book), P(KILL-night),
    det-max, and every enforced cap's headroom;
  * the largest ``s`` for which the book SCALED BY s still satisfies
    P(KILL-night) <= the ratified ``portfolio_kill_tail_prob`` AND every other
    ENFORCED cap (bisection — every constraint is monotone in s);
  * WHICH constraint binds first (the one that stops s from growing).

Hard rule 8: every number comes from the LIVE modules
(``sim.book_risk.compute_book_risk`` + ``risk.limits.LimitChecker.check``).
Wiring / rehydration are imported verbatim from
``tools.diagnostics.restart_gate2_quote_validation``.

READ-ONLY: store mode=ro, PaperSender, GETs only. Never places an order.

    .venv/Scripts/python.exe tools/diagnostics/deploy_scale_probe.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import tools.diagnostics.restart_gate2_quote_validation as g2  # noqa: E402
from combomaker.risk.exposure import ExposureBook, OpenPosition  # noqa: E402
from combomaker.risk.limits import threshold_cc  # noqa: E402
from combomaker.sim.book_model import build_book_model  # noqa: E402
from combomaker.sim.book_risk import compute_book_risk  # noqa: E402

d = g2.d


def scaled_positions(positions: list[OpenPosition], s: float) -> list[OpenPosition]:
    out = []
    for p in positions:
        c = int(round(int(p.contracts) * s))
        if c <= 0:
            continue
        out.append(replace(p, contracts=type(p.contracts)(c)))
    return out


def tail_prob(snap, thr_cc: int, z: float) -> float:
    """EXACTLY the limits.py tail-probability form, read off the envelope."""
    from combomaker.risk.limits import _wilson_upper

    q = getattr(snap, "loss_quantiles_cc", ()) or ()
    n_mc = int(getattr(snap, "n_samples", 0) or 0)
    if not q or n_mc <= 0:
        return float("nan")
    n_grid = len(q)
    k_ge = sum(1 for x in q if x >= thr_cc)
    p_hat = k_ge / max(1, n_grid - 1)
    return _wilson_upper(min(1.0, p_hat), n_mc, z)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", default="data/_deploy_scale_probe")
    ap.add_argument("--samples", type=int, default=100_000)
    ap.add_argument("--iters", type=int, default=14)
    ap.add_argument("--smax", type=float, default=16.0)
    args = ap.parse_args()
    from combomaker.ops.logging import configure_logging

    configure_logging(json_output=False, level="warning")

    w = await g2.wire(Path(args.scratch))
    try:
        lc = w.lifecycle
        lim = w.limits.limits
        bank = w.balance.risk_bankroll_cc_or_none()
        print("=" * 100)
        print(f"BANKROLL (risk) {d(bank)}")
        shim = g2._Shim(w.cfg)
        await g2.rehydrate(w, shim)
        positions = list(w.exposure.positions.values())
        prem = sum(p.max_loss_cc for p in positions)
        print(f"positions rehydrated: {len(positions)}   premium-at-risk {d(prem)}")
        if not positions or bank is None:
            print("!! empty book or no bankroll — nothing to solve")
            return 1

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
        n_ok = sum(1 for t in legs if lc._marginals(t) is not None)
        print(f"leg marginals resolved: {n_ok}/{len(legs)}")

        cvar_thr = threshold_cc(lim.portfolio_cvar_frac, bank)
        det_thr = threshold_cc(lim.portfolio_det_max_frac, bank)
        kill_thr = threshold_cc(lim.hard_trip_frac, bank)
        print(
            f"ANCHORS  kill_tail_prob {lim.portfolio_kill_tail_prob}  "
            f"tail_prob_gate {lim.portfolio_tail_prob_gate}  ci_z {lim.portfolio_tail_prob_ci_z}"
        )
        print(
            f"         cvar_frac {lim.portfolio_cvar_frac} = {d(cvar_thr)}   "
            f"det_max_frac {lim.portfolio_det_max_frac} = {d(det_thr)}   "
            f"hard_trip {lim.hard_trip_frac} = {d(kill_thr)}"
        )
        print(f"         ruin_prob_budget {lim.portfolio_ruin_prob_budget}  ruin_floor {lc._config.ruin_floor_frac}")

        equity_basis = None

        def snap_at(s: float, samples: int):
            pos = scaled_positions(positions, s)
            model = build_book_model(
                pos, marginals=lc._marginals, within_game_rho=lc._within_game_rho
            )
            eq = lc._ruin_equity_basis_cc(model)
            return pos, compute_book_risk(
                model,
                n_samples=samples,
                seed=lc._config.book_risk_seed,
                band="high",
                bankroll_cc=bank,
                structural_cfg=lc._structural_cfg,
                current_equity_cc=eq,
                ruin_floor_frac=lc._config.ruin_floor_frac,
                ruin_prob_ci_z=lc._config.ruin_prob_ci_z,
                input_generation=w.exposure.position_generation,
                realized_pnl_cc=lc._realized_pnl_cc,
            )

        def cap_breaches(pos: list[OpenPosition]):
            """Every ENFORCED cap, re-run on the SCALED committed book."""
            tmp = ExposureBook(lc._conventions, is_me_event=w.exposure.is_me_event)
            for p in pos:
                tmp.add_position(p)
            _, snap = None, None
            return tmp

        def evaluate(s: float, samples: int):
            pos, snap = snap_at(s, samples)
            tmp = ExposureBook(lc._conventions, is_me_event=w.exposure.is_me_event)
            for p in pos:
                tmp.add_position(p)
            breaches = w.limits.check(
                tmp,
                lc._marginals,
                lc.daily_pnl,
                risk_bankroll_cc=bank,
                bankroll_source_configured=True,
                start_time_provider=lc._start_time_provider,
                halt_inputs=lc._halt_inputs(),
                book_risk=snap,
            )
            enforced = lc.partition_breaches(list(breaches))
            p_kill = tail_prob(snap, cvar_thr, lim.portfolio_tail_prob_ci_z)
            p_kill12 = tail_prob(snap, kill_thr, lim.portfolio_tail_prob_ci_z)
            det_c = snap.deterministic_max_loss_cc
            det_m = snap.mutex_aware_det_max_cc
            det_gate = det_c if det_m is None or not lim.portfolio_det_max_mutex_aware else min(det_c, float(det_m))
            return {
                "s": s,
                "npos": len(pos),
                "prem": sum(p.max_loss_cc for p in pos),
                "ev": snap.ev_cc,
                "std": snap.std_cc,
                "p_book": snap.p_profit,
                "p_kill_cvarthr": p_kill,
                "p_kill_12pct": p_kill12,
                "det_gate": det_gate,
                "p_ruin_upper": max(snap.p_ruin, getattr(snap, "p_ruin_upper", snap.p_ruin)),
                "es99": snap.governing_model_es_99_cc,
                "enforced": [str(b.reason) for b in enforced],
                "detail": [b.detail for b in enforced],
                "usable": snap.usable,
            }

        def ok(r) -> bool:
            return r["usable"] and not r["enforced"]

        t0 = time.perf_counter()
        base = evaluate(1.0, args.samples)
        t_one = time.perf_counter() - t0
        print("=" * 100)
        print(f"BASE (s=1.00)  one full MC+check = {t_one*1000:.0f} ms")
        for k in (
            "npos", "prem", "ev", "std", "p_book", "p_kill_cvarthr",
            "p_kill_12pct", "det_gate", "p_ruin_upper", "es99",
        ):
            v = base[k]
            if k in ("prem", "ev", "std", "det_gate", "es99"):
                print(f"    {k:<16} {d(v)}")
            else:
                print(f"    {k:<16} {v}")
        print(f"    enforced breaches: {base['enforced']}")
        for x in base["detail"]:
            print(f"        {x[:200]}")
        if not ok(base):
            print("!! the CURRENT book already breaches / is unusable — s solves to 1.0 (fail-safe)")

        # ---- bisection on s ------------------------------------------------
        print("=" * 100)
        print("SOLVE  largest s with P(KILL-night) <= anchor AND every enforced cap holding")
        lo, hi = 1.0, args.smax
        r_hi = evaluate(hi, max(20_000, args.samples // 4))
        print(f"    probe s={hi:5.2f}  feasible={ok(r_hi)}  binding={sorted(set(r_hi['enforced']))}")
        if ok(r_hi):
            print(f"    !! s_max bound {hi} is itself feasible — report as >= {hi}")
            lo = hi
        else:
            for i in range(args.iters):
                mid = 0.5 * (lo + hi)
                r = evaluate(mid, max(20_000, args.samples // 4))
                f = ok(r)
                print(
                    f"    s={mid:6.3f}  feasible={str(f):<5} "
                    f"P(kill@cvar)={r['p_kill_cvarthr']:.4f} "
                    f"P(loss>=12%)={r['p_kill_12pct']:.4f} "
                    f"det={d(r['det_gate'])}/{d(det_thr)} "
                    f"ruin={r['p_ruin_upper']:.4f} "
                    f"caps={sorted(set(r['enforced']))}"
                )
                if f:
                    lo = mid
                else:
                    hi = mid
        t_solve = time.perf_counter() - t0
        print(f"    SOLVED s = {lo:.4f}   (total solve wall time {t_solve:.1f}s)")

        # ---- full-fidelity report at s* ------------------------------------
        print("=" * 100)
        star = evaluate(lo, args.samples)
        just_over = evaluate(min(args.smax, lo * 1.02), max(20_000, args.samples // 4))
        print(f"AT s* = {lo:.4f}   (and the first-binding constraint just above)")
        for k in (
            "npos", "prem", "ev", "std", "p_book", "p_kill_cvarthr",
            "p_kill_12pct", "det_gate", "p_ruin_upper", "es99",
        ):
            v = star[k]
            if k in ("prem", "ev", "std", "det_gate", "es99"):
                print(f"    {k:<16} {d(v)}")
            else:
                print(f"    {k:<16} {v}")
        print(f"    enforced at s*      : {sorted(set(star['enforced']))}")
        print(f"    enforced at s*x1.02 : {sorted(set(just_over['enforced']))}")
        for x in just_over["detail"]:
            print(f"        {x[:220]}")

        # E[log] growth, both scales
        def elog(r) -> float:
            if bank is None or r["std"] <= 0:
                return float("nan")
            mu = r["ev"] / bank
            sd = r["std"] / bank
            return 1e4 * (mu - 0.5 * sd * sd)  # bp

        print(
            f"    E[log]  s=1 {elog(base):.1f}bp   s*={lo:.2f} {elog(star):.1f}bp   "
            f"EV/night {d(base['ev'])} -> {d(star['ev'])}   "
            f"p_book {base['p_book']:.4f} -> {star['p_book']:.4f}"
        )

        # ---- cap headroom table at s* --------------------------------------
        print("=" * 100)
        print("CAP HEADROOM at s* (name, fraction, $ threshold at the live bankroll)")
        for name, frac, thr in g2.cap_table(w, bank):
            print(f"    {name:<26} {frac:>10}  {thr:>14}")
        print(f"    {'portfolio det-max (live)':<26} {'':>10}  "
              f"{d(star['det_gate']):>14}  of {d(det_thr)} "
              f"({100.0 * star['det_gate'] / det_thr:.1f}% used at s*)")
        print(f"    {'P(loss >= cvar_thr)':<26} {'':>10}  "
              f"{star['p_kill_cvarthr']:>14.4f}  budget "
              f"{lim.portfolio_kill_tail_prob:.4f}")
        print(f"    {'P(loss >= hard_trip 12%)':<26} {'':>10}  "
              f"{star['p_kill_12pct']:>14.4f}  (REPORT-ONLY: the live gate binds "
              f"at cvar_frac, not at the KILL distance)")
        return 0
    finally:
        try:
            await w.rest.__aexit__(None, None, None)
        except Exception:
            pass
        await w.scratch.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
