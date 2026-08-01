"""V10/V11 — PRICING ORDER and THROUGHPUT FLOORS (operator ask 2026-07-27).

The eight-vital tier grades LIVENESS: does the bot quote at all, does a loop
stall, does a won auction survive. It deliberately left out the two properties
the operator names as the actual JOB — *"we need to quote better and have a
stronger working risk engine"*:

  V10 PRICING ORDER. For two otherwise-identical candidates, the one that LOWERS
      dollar-weighted book concentration must be quoted STRICTLY TIGHTER.
      ``risk/concentration_steer.assert_diversifier_tighter`` states it and
      ``tests/test_concentration_steer.py`` proves it for the STEER FUNCTION in
      isolation. That is not the property. The price the taker sees is the
      composition of SIX classifier components (directional, peak, P(book),
      leg-family, leg-entity, concentration) followed by a fee model and a grid
      snap, and every one of those is a place the order can invert. So this
      check varies the ONE discriminating variable the unit tests cannot: it
      holds the RFQ, the fair, the size and the TOTAL BOOK DOLLARS fixed and
      changes ONLY WHERE the book's risk already sits, then reads the price off
      ``handle_rfq`` — the real quote, after the real snap.

  V11 THROUGHPUT FLOORS. "Throughput never regresses" (operator, 2026-07-18) is
      a standing rule with no executable form; the det-max wall silently zeroed
      quoting for 2 h. Two floors, both derived from measurement:
        A  ABSOLUTE — the PEAK RFQ ARRIVAL RATE the exchange has actually sent
           us, read out of the store's ``rfqs`` table. Capacity below observed
           demand is a throughput miss by arithmetic.
        B  RELATIVE — the shipped configuration against the zero-cost rollback
           (``conc_enabled=False``, byte-equivalent to the pre-Lever-#5 path),
           with the tolerance taken from the WITHIN-MODE SPREAD MEASURED IN THE
           SAME RUN. A regression is only real when it exceeds the noise the
           machine itself is producing, and that noise is measured, not assumed.

ISOLATION (CLAUDE.md rule 8). Nothing here edits a live module. Both checks
IMPORT AND DRIVE the shipped objects — ``QuoteLifecycle.handle_rfq``, the
shipped ``SkewParams`` read out of the live YAML, the real open book read
READ-ONLY out of ``position_ledger`` — through the existing test harnesses.
"""

from __future__ import annotations

import asyncio
import sqlite3
import statistics
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.risk.exposure import LegRef, OpenPosition
from tools.vitals.result import PRESHIP, Probe, Result, Vital

V10 = Vital(
    key="V10",
    incident="the operator invariant has no END-TO-END detector (2026-07-27): "
    "the steer function is unit-tested, the QUOTE is not",
    name="PRICING ORDER — a diversifier is quoted strictly tighter",
    degraded="two books with IDENTICAL total premium at risk and identical "
    "position counts: one already loaded on the candidate's OWN keys, one "
    "loaded on keys the candidate never touches — the same RFQ against both",
    money="if the order inverts or collapses, we pay the SAME price for risk "
    "that concentrates the book as for risk that offsets it — the whole "
    "economic point of the steer is gone and we accumulate one-way inventory "
    "at a discount",
    tier=PRESHIP,
)

V11 = Vital(
    key="V11",
    incident="'throughput never regresses' (operator 2026-07-18) has never had "
    "an executable form — the det-max wall silently zeroed quoting for 2 h",
    name="THROUGHPUT FLOORS — capacity clears observed demand, and the shipped "
    "path is not slower than its own rollback",
    degraded="a live-shaped committed book under the SHIPPED skew "
    "configuration, driven through the real handle_rfq (pricing, policy, "
    "profile builds, quote construction, the store write)",
    money="every RFQ we cannot price in time is an auction we never entered: "
    "the tape's speed-miss bucket is pure lost revenue, not a risk decline",
    tier=PRESHIP,
)


# --------------------------------------------------------------------------- #
# The shipped skew configuration, READ out of the live YAML.
# --------------------------------------------------------------------------- #


def shipped_skew_params() -> tuple[Any, dict[str, Any]]:
    """``SkewParams`` exactly as ``quote_app`` builds them from the live config.

    KEEP IN SYNC WITH ``ops/quote_app.py`` (the ``skew_params = SkewParams(...)``
    block). Rule 8(c): this duplicates a field-for-field mapping, nothing else —
    every VALUE comes from the shipped ``SkewConfig`` pydantic model parsing the
    operator's own YAML, so an unset key resolves to the live code default and
    never to anything this gate invents.
    """
    from combomaker.ops.config import SkewConfig
    from combomaker.risk.skew import SkewParams
    from tools.vitals.derive import live_config

    cfg, _path = live_config()
    raw = ((cfg.get("pricing") or {}).get("skew") or {})
    known = {k: v for k, v in raw.items() if k in SkewConfig.model_fields}
    s = SkewConfig(**known)
    params = SkewParams(
        w_conc=s.w_conc,
        w_off=s.w_off,
        gamma=s.gamma,
        skew_max_widen_cc=s.skew_max_widen_cc,
        skew_max_tighten_cc=s.skew_max_tighten_cc,
        enabled=s.enabled,
        peak_enabled=s.peak_enabled,
        peak_widen_max_cc=s.peak_widen_max_cc,
        peak_tighten_max_cc=s.peak_tighten_max_cc,
        pbook_enabled=s.pbook_enabled,
        pbook_armed=s.pbook_armed,
        leg_axis_enabled=s.leg_axis_enabled,
        leg_axis_armed=s.leg_axis_armed,
        conc_enabled=s.conc_enabled,
        conc_armed=s.conc_armed,
        # 2026-08-01 skew settled-fact resolution (95b9a40): mirror the
        # quote_app.py passthrough so the gate exercises the shipped posture,
        # armed or dark, exactly as the live bot builds it.
        settled_fact_resolution=s.settled_fact_resolution,
    )
    armed = {
        "directional": s.enabled,
        "peak": s.peak_enabled,
        "pbook": s.pbook_armed,
        "leg_axis": s.leg_axis_armed,
        "concentration": s.conc_armed,
        "settled_fact_resolution": s.settled_fact_resolution,
    }
    return params, armed


# --------------------------------------------------------------------------- #
# V10 — the price of concentration, read off the real quote.
# --------------------------------------------------------------------------- #

# The RFQ the whole check quotes. Its legs are the harness's seeded markets, so
# the candidate the lifecycle builds lands on the key set (E1/M1 yes, E2/M2 no).
_CAND_LEGS = (LegRef("M1", "E1", "yes"), LegRef("M2", "E2", "no"))
# A key set the candidate can never touch. Same shape, same dollars.
_AWAY_LEGS = (LegRef("VITALSX1", "VITALSE1", "yes"), LegRef("VITALSX2", "VITALSE2", "no"))


def _price_axis_limits():
    """Every DOLLAR wall opened so the graded axis is the PRICE, not a refusal."""
    from fractions import Fraction

    from combomaker.risk.limits import RiskLimits

    wide = Fraction(1_000, 1)
    return RiskLimits(
        max_open_quotes=100_000,
        per_combo_loss_frac=wide,
        game_loss_frac=wide,
        slate_loss_frac=wide,
        directional_frac=wide,
        daily_loss_frac=wide,
        drawdown_frac=wide,
        hard_trip_frac=wide,
        portfolio_cvar_frac=wide,
        portfolio_det_max_frac=wide,
        portfolio_ruin_prob_budget=Fraction(99, 100),
        absolute_notional_multiple=1_000_000,
        fill_velocity_soft_frac=wide,
        fill_velocity_hard_frac=wide,
    )


def _load(pid: str, legs: tuple[LegRef, ...], *, contracts: int, price_cc: int):
    return OpenPosition(
        position_id=pid,
        combo_ticker=f"COMBO-{pid}",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(contracts),
        entry_price_cc=CentiCents(price_cc),
        legs=legs,
    )


def _warm_the_centre(rig) -> tuple[int, float]:
    """Give the steer's ``SteerCenter`` the dispersion a RUNNING bot's has.

    ``SteerCenter.centre`` standardises by the MEASURED dispersion of live
    scores, so with a single observation the deviation is exactly 0 and the
    steer is IDENTICALLY ZERO no matter how concentrated the candidate is. A rig
    that quotes one RFQ therefore measures the COLD-START state, not the steady
    state. This replays the book's OWN tickets through the SHIPPED
    ``_concentration_component`` with ``observe=True`` — the same function, the
    same profile, the same centre object the quote path uses — so the warm-up
    distribution is the live book's, not a distribution this gate invented.
    (``margin_cc``/``tick_cc`` are irrelevant here: the centre observes the RAW
    score, which is computed before the scale and the tick ladder.)"""
    from combomaker.risk.skew import _concentration_component

    snap = rig.exposure.snapshot(rig.lifecycle._marginals, mass_acceptance=False)  # noqa: SLF001
    profile = rig.lifecycle._concentration_profile(snap)  # noqa: SLF001
    for pos in list(rig.exposure.positions.values()):
        _concentration_component(pos, profile, 0, 0, True, ())
    return profile.centre.n, profile.centre.sd


async def _quote_against(book, params, tmp: Path, tag: str, bankroll_cc: int, *, warm: bool):
    """Drive the REAL ``handle_rfq`` once and return the quote it sent."""
    from combomaker.ops.persistence import Store
    from combomaker.risk.limits import LimitChecker, RiskLimits
    from tests.test_pricing_engine import CROSS_EVENT_LEGS, combo
    from tests.test_quoting_policy import PolicyRig, _harness
    from tests.test_risk_shadow_mode import _FixedBankroll

    h = await _harness()
    store = await Store.open(tmp / f"{tag}.sqlite3", h.clock)
    rig = PolicyRig(h, store, skew_params=params)
    # THE AXIS UNDER TEST IS PRICE, NOT REFUSAL (the 2026-07-23 lesson: prove
    # the axis you are grading is the binding one). The real book at $1,237
    # against a $2,050 bankroll trips game / slate / det-max on the FIRST quote,
    # so with default caps this check would grade the caps and never reach the
    # classifier. The DOLLAR walls are opened here and ONLY here; each of them
    # has its own vital (V1/V2) and its own enforcement path.
    rig.lifecycle._limits = LimitChecker(_price_axis_limits())  # noqa: SLF001
    rig.lifecycle._balance = _FixedBankroll(bankroll_cc)  # noqa: SLF001
    for pos in book:
        rig.exposure.add_position(pos)
    # Production takes exactly this step before it quotes a rehydrated book
    # (quote_app._startup_book_risk_snapshot); without it the P(book) axis reads
    # "never measured" and the steer under test is switched off.
    await rig.lifecycle.recompute_book_risk_offloop()
    centre_state = _warm_the_centre(rig) if warm else (0, 0.0)
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id=f"rfq_{tag}"))
    sent = list(rig.sender.created)
    await store.close()
    return sent, centre_state


async def _v10(probe: Probe, d: Any) -> tuple[bool, str, str, str]:
    from tools.vitals.derive import live_open_positions
    from tools.vitals.v_confirm import _scaled_live_book

    from combomaker.risk.skew import ticket_bucket

    rows, source = live_open_positions()
    base = _scaled_live_book(rows, 1)

    # THE LOAD IS SIZED BY THE BOOK, NOT BY THIS GATE. It is the premium of the
    # book's OWN HEAVIEST AND-bound loss event, so placing it on the candidate's
    # keys makes that bucket the joint-heaviest in the book — i.e. the candidate
    # genuinely CONCENTRATES — while placing it elsewhere leaves the candidate's
    # bucket empty — i.e. it genuinely DIVERSIFIES. Anything smaller does not
    # move the dollar-Herfindahl enough to be a test of the ordering; anything
    # larger would be a number a human chose.
    by_bucket: dict[tuple[str, ...], int] = {}
    for p in base:
        key = ticket_bucket(p.legs)
        by_bucket[key] = by_bucket.get(key, 0) + int(p.max_loss_cc)
    load_cc = max(by_bucket.values()) if by_bucket else 100_000
    price_cc = 5_000                       # mid-grid, so the size is the axis
    contracts = max(100, load_cc * 100 // price_cc)

    probe.note(
        f"base book = the REAL open book: {len(base)} positions in "
        f"{len(by_bucket)} AND-bound loss-event buckets [{source}]; the LOAD is "
        f"one ticket at the book's OWN HEAVIEST bucket premium "
        f"${load_cc / 10_000:,.2f} ({contracts / 100:,.0f} contracts @ "
        f"{price_cc / 100:.0f}c)"
    )

    on_keys = [*base, _load("vitals-on", _CAND_LEGS, contracts=contracts, price_cc=price_cc)]
    off_keys = [*base, _load("vitals-off", _AWAY_LEGS, contracts=contracts, price_cc=price_cc)]
    gross_on = sum(int(p.max_loss_cc) for p in on_keys)
    gross_off = sum(int(p.max_loss_cc) for p in off_keys)
    probe.note(
        f"CONTROL — the two books carry the SAME dollars and the SAME position "
        f"count: {len(on_keys)} positions / ${gross_on / 10_000:,.2f} loaded ON "
        f"the candidate's keys vs {len(off_keys)} / ${gross_off / 10_000:,.2f} "
        f"loaded AWAY from them. Only WHERE the risk sits differs."
    )

    shipped, armed = shipped_skew_params()
    probe.note(
        "armed in the live config: "
        + ", ".join(k for k, v in armed.items() if v)
        + "   |   SHADOW (computed, never priced): "
        + (", ".join(k for k, v in armed.items() if not v) or "none")
    )
    import dataclasses

    # THE SHIPPED CONFIGURATION IS WHAT IS GRADED. The other two rows are
    # reported, not graded: the cold-centre row is the state at every restart,
    # and the armed row is the change the operator is being asked to authorise.
    modes = [("SHIPPED warm", shipped, True), ("SHIPPED cold", shipped, False)]
    if not shipped.conc_armed and shipped.conc_enabled:
        forced = dataclasses.replace(shipped, conc_armed=True)
        modes.append(("+conc ARMED warm", forced, True))
        modes.append(("+conc ARMED cold", forced, False))

    failures: list[str] = []
    swings: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for label, params, warm in modes:
            key = label.replace(" ", "")[:9]
            conc_sent, cstate = await _quote_against(
                on_keys, params, tmp, f"on{key}", d.bankroll_cc, warm=warm
            )
            div_sent, _ = await _quote_against(
                off_keys, params, tmp, f"off{key}", d.bankroll_cc, warm=warm
            )
            # A refusal is INFINITE width — the same inequality, never a special
            # case (``concentration_steer.quote_rank_cc``).
            conc_bid = int(conc_sent[0]["no"]) if conc_sent else -1
            div_bid = int(div_sent[0]["no"]) if div_sent else -1
            swing = div_bid - conc_bid
            graded = label == "SHIPPED warm"
            swings.append(f"{label} {swing / 100:+.2f}c")
            probe.note(
                f"{label:<17} no_bid  concentrating {conc_bid}cc  vs  diversifying "
                f"{div_bid}cc  ->  swing {swing:+5d}cc ({swing / 100:+.2f}c)  "
                f"{'DIVERSIFIER TIGHTER' if swing > 0 else ('EQUAL - NO ORDER' if swing == 0 else 'ORDER INVERTED')}"
                f"   [centre n={cstate[0]} sd={cstate[1]:.4f}]"
                f"{'   <- GRADED' if graded else ''}"
            )
            if not graded:
                continue
            if not div_sent:
                failures.append(f"{label}: the REFUSAL landed on the DIVERSIFIER")
            elif swing < 0:
                failures.append(
                    f"{label}: ORDER INVERTED — the concentrating book was quoted "
                    f"{-swing}cc TIGHTER"
                )
            elif swing == 0:
                failures.append(
                    f"{label}: NO ORDER — identical price for concentrating and "
                    f"diversifying risk (the steer is economically inert here)"
                )

    ok = not failures
    return (
        ok,
        "; ".join(swings),
        "the same RFQ against a book loaded ON its keys must be quoted STRICTLY "
        "wider than against a book loaded AWAY from them (swing > 0), and the "
        "refusal may never land on the diversifier",
        "; ".join(failures),
    )


def check_pricing_order(probe: Probe, d: Any) -> Result:
    ok, measured, bound, detail = asyncio.run(_v10(probe, d))
    return probe.grade(ok, measured=measured, bound=bound, detail=detail)


# --------------------------------------------------------------------------- #
# V11 — throughput, against demand the exchange actually produced.
# --------------------------------------------------------------------------- #


def peak_rfq_arrivals_per_min(sample: int = 400_000) -> tuple[int, float, str]:
    """The PEAK and MEDIAN RFQ arrival rate the exchange has really sent us.

    READ-ONLY out of the production store's ``rfqs`` table (``seen_at`` is
    stamped on intake). Bounded to the most recent ``sample`` rows so the scan
    is O(sample) and cannot be perturbed by the live writer. Returns
    (peak/min, median busy minute, provenance)."""
    from tools.vitals.derive import DATA

    best: tuple[int, float, str] | None = None
    for db in sorted(DATA.glob("combomaker*.sqlite3")):
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            rows = conn.execute(
                "select seen_at from rfqs order by id desc limit ?", (sample,)
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        if not rows:
            continue
        per_min = Counter(r[0][:16] for r in rows)
        peak = max(per_min.values())
        med = statistics.median(per_min.values())
        src = (
            f"{db.name}:rfqs[last {len(rows):,} intakes, "
            f"{min(per_min)}Z..{max(per_min)}Z]"
        )
        if best is None or peak > best[0]:
            best = (peak, float(med), src)
    if best is None:
        raise SystemExit(
            "no rfqs rows in any store under data/ — the gate refuses to invent "
            "a demand rate"
        )
    return best


def live_speed_miss(sample: int = 300_000) -> tuple[int, int, str]:
    """The tape's own answer: quotes SENT vs auctions lost to SPEED.

    The SPEED bucket is ``tools/diagnostics/throughput_funnel.py``'s definition,
    imported from it so the two can never drift. READ-ONLY, bounded."""
    import json as _json

    from tools.diagnostics.throughput_funnel import SPEED
    from tools.vitals.derive import DATA

    best: tuple[int, int, str] | None = None
    for db in sorted(DATA.glob("combomaker*.sqlite3")):
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            rows = conn.execute(
                "select kind, reasons_json, at from decisions order by id desc limit ?",
                (sample,),
            ).fetchall()
        except sqlite3.Error:
            continue
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        if not rows:
            continue
        quoted = sum(1 for k, _r, _a in rows if k == "quote_sent")
        miss = sum(1 for _k, r, _a in rows if SPEED & set(_json.loads(r)))
        src = f"{db.name}:decisions[{rows[-1][2][:16]}Z..{rows[0][2][:16]}Z]"
        if best is None or quoted + miss > best[0] + best[1]:
            best = (quoted, miss, src)
    return best or (0, 0, "no decisions rows")


def check_throughput(probe: Probe, d: Any) -> Result:
    import dataclasses

    from tools.diagnostics.bench_conc_quote_path import run_mode

    peak, med_min, src = peak_rfq_arrivals_per_min()
    shipped, armed = shipped_skew_params()
    rollback = dataclasses.replace(shipped, conc_enabled=False)

    reps = 3
    results: dict[str, list[float]] = {"ROLLBACK": [], "SHIPPED": []}
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # Interleaved, so a machine-state drift cannot land on one mode.
        for rep in range(reps):
            for tag, params in (("ROLLBACK", rollback), ("SHIPPED", shipped)):
                results[tag].append(
                    asyncio.run(run_mode(params, tmp, f"v11{tag}{rep}"))
                )

    per_rfq = {k: statistics.median(v) for k, v in results.items()}
    qpm = {k: 60.0 / v for k, v in per_rfq.items()}
    # FLOOR B's tolerance is the noise the MACHINE produced in this same run —
    # measured, never assumed. Worst within-mode spread of the two modes.
    noise = max(
        (max(v) - min(v)) / statistics.median(v) for v in results.values()
    )
    regression = per_rfq["SHIPPED"] / per_rfq["ROLLBACK"] - 1.0

    probe.note(
        f"armed axes in the live config: "
        f"{', '.join(k for k, v in armed.items() if v) or 'NONE'}   "
        f"(shadow: {', '.join(k for k, v in armed.items() if not v) or 'none'})"
    )
    for tag in ("ROLLBACK", "SHIPPED"):
        probe.note(
            f"{tag:<9} {per_rfq[tag] * 1e6:9.1f}us/RFQ  = {qpm[tag]:11,.0f} "
            f"quotes/min   [{min(results[tag]) * 1e6:.1f} .. "
            f"{max(results[tag]) * 1e6:.1f}us over {reps} reps]"
        )
    probe.note(
        f"FLOOR A (absolute, measured demand): PEAK {peak:,} RFQ/min, median "
        f"busy minute {med_min:,.0f}/min   [{src}]"
    )
    probe.note(
        f"FLOOR B (relative): SHIPPED vs ROLLBACK {regression * 100:+.1f}% "
        f"against a MEASURED run-to-run noise floor of {noise * 100:.1f}%"
    )
    quoted, miss, msrc = live_speed_miss()
    probe.note(
        f"CORROBORATION on the live tape: {quoted:,} quote_sent vs {miss:,} "
        f"SPEED-bucket losses = "
        f"{100.0 * miss / max(1, quoted + miss):.1f}% of everything we did not "
        f"deliberately decline   [{msrc}]"
    )
    probe.note(
        "READ FLOOR A HONESTLY: this rig is ONE process and pays a synchronous "
        "store write, so its number is a LOWER BOUND on live capacity — the "
        "pricing pool's parallelism is the untested surplus. It is graded "
        "anyway because the alternative (assume the surplus) is exactly the "
        "mistake R5 made when it benchmarked the wrong path."
    )

    failures: list[str] = []
    if qpm["SHIPPED"] < peak:
        failures.append(
            f"single-process quote capacity {qpm['SHIPPED']:,.0f}/min is BELOW "
            f"the peak arrival rate the exchange has actually sent "
            f"({peak:,}/min) — the surplus is absorbed by the pricing pool's "
            f"parallelism, or it is a speed miss"
        )
    if regression > noise:
        failures.append(
            f"THROUGHPUT REGRESSION: the shipped path is {regression * 100:.1f}% "
            f"slower than its own zero-cost rollback, beyond the {noise * 100:.1f}% "
            f"noise measured in the same run"
        )

    ok = not failures
    return probe.grade(
        ok,
        measured=f"{qpm['SHIPPED']:,.0f} q/min shipped, {regression * 100:+.1f}% vs rollback",
        bound=f"FLOOR A: >= peak observed demand {peak:,} RFQ/min; FLOOR B: "
        f"shipped-vs-rollback regression <= the measured noise {noise * 100:.1f}%",
        detail="; ".join(failures),
    )


CHECKS = [(V10, check_pricing_order), (V11, check_throughput)]
