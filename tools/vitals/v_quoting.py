"""V1/V2 — QUOTING LIVENESS.

A cap that declines everything passes every safety test and is useless. Two
historical regressions bricked quoting outright while 3,000+ unit tests stayed
green, through two DIFFERENT mechanisms with the same economic outcome:

  R1 (2026-07-26) the entity cap scanned EVERY entity key in the book instead of
     the CANDIDATE's, so one already-over arm declined every quote: 3,994
     breaches, ZERO quotes sent.
  R3 (2026-07-26) withdraw-pending quotes counted against ``max_open_quotes``
     and the retry was unmetered, so a transient 429 became permanent and every
     new RFQ was refused for capacity.

So the degraded starting states here are "the book is ALREADY over a wall" and
"every open-quote slot is withdraw-pending under a 429ing exchange" — the exact
states the unit suite never constructs.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from fractions import Fraction
from pathlib import Path
from typing import Any

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition, leg_entity_key
from combomaker.risk.limits import DailyPnl, LimitChecker, RiskLimits, threshold_cc
from tools.vitals.result import FAST, Probe, Result, Vital

CONVENTIONS = Conventions(
    verified=True,
    source="vitals-gate",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)

# Real live tickers off the 7/25-7/26 book (the arms the entity bound was built
# for). Two rungs of one pitcher + a completely unrelated pitcher.
ARM_A_R4 = "KXMLBKS-26JUL261335TORBOS-TORHGREENE21-4"
ARM_A_R6 = "KXMLBKS-26JUL261335TORBOS-TORHGREENE21-6"
ARM_B_R5 = "KXMLBKS-26JUL261610SDMIA-SDDCEASE44-5"


V1 = Vital(
    key="V1",
    incident="R1 entity cap bricked quoting (2026-07-26)",
    name="CAP SCOPE — a wall refuses only the flow that WORSENS it",
    degraded="book ALREADY over the entity wall + a candidate DISJOINT from "
    "every over-wall key",
    money="every quote declined on an unrelated combo: 3,994 breaches, 0 quotes, "
    "zero revenue while the book still carries the risk",
    tier=FAST,
)

V2 = Vital(
    key="V2",
    incident="R1 entity cap + R3 withdraw-pending capacity brick (2026-07-26)",
    name="QUOTING LIVENESS — handle_rfq still yields a priced quote",
    degraded="(a) book over a wall  (b) every open slot withdraw-pending under a "
    "permanently-429ing exchange",
    money="quoting is bricked — we win no auctions at all while carrying inventory",
    tier=FAST,
)


def _pos(pid: str, ticker: str, *, contracts: int = 100, price_cc: int = 2_500):
    return OpenPosition(
        position_id=pid,
        combo_ticker=f"COMBO-{pid}",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(contracts * 100),
        entry_price_cc=CentiCents(price_cc),
        legs=(LegRef(ticker, "KX-G1", "yes"),),
    )


def _limits(frac: str, **over: Any) -> RiskLimits:
    kwargs: dict[str, Any] = dict(
        caps_shadow_mode=False,
        entity_loss_frac=Fraction(frac),
        # The OTHER dollar caps are opened up so the graded axis is the ENTITY
        # one and not an unrelated wall. They have their own checks.
        per_combo_loss_frac=Fraction(50, 100),
        game_loss_frac=Fraction(50, 100),
        slate_loss_frac=Fraction(90, 100),
        directional_frac=Fraction(90, 100),
    )
    kwargs.update(over)
    return RiskLimits(**kwargs)


def _breaches(book: ExposureBook, cand: OpenPosition, frac: str, bankroll_cc: int):
    return [
        b.reason
        for b in LimitChecker(_limits(frac)).check(
            book,
            lambda t: 0.5,
            DailyPnl(),
            candidate_positions=[cand],
            risk_bankroll_cc=bankroll_cc,
        )
    ]


def check_cap_scope(probe: Probe, d: Any) -> Result:
    """V1. Both directions in ONE assertion — the one-sided 'accumulate and trip'
    form is exactly what the pre-R1 suite had, and it passes on a cap that
    declines everything."""
    frac = d.entity_loss_frac
    bankroll = d.bankroll_cc
    wall_cc = threshold_cc(Fraction(frac), bankroll)

    # Construct the over-wall state WITH THE LIVE PREDICATE: scale until the
    # book's own snapshot reads over. The dollar figure is an OUTPUT.
    book = ExposureBook(CONVENTIONS)
    price = 1_000
    over_keys: list[str] = []
    for step in range(1, 40):
        book = ExposureBook(CONVENTIONS)
        price = 1_000 * step
        book.add_position(_pos("h1", ARM_A_R4, price_cc=price))
        book.add_position(_pos("h2", ARM_A_R6, price_cc=price))
        snap = book.snapshot(lambda t: 0.5, mass_acceptance=False)
        over_keys = [k for k, v in snap.loss_by_entity_cc.items() if v > wall_cc]
        if over_keys:
            break
    if not over_keys:
        return probe.grade(
            False,
            measured="could not construct an over-wall book",
            bound=f"wall = {frac} x bankroll = ${wall_cc / 10_000:,.2f}",
        )
    snap = book.snapshot(lambda t: 0.5, mass_acceptance=False)
    worst = max(snap.loss_by_entity_cc.values())

    disjoint = _pos("cand-disjoint", ARM_B_R5, price_cc=1_000)
    touching = _pos("cand-touching", ARM_A_R4, price_cc=1_000)
    assert leg_entity_key(disjoint.legs[0]) not in over_keys, "fixture is not disjoint"

    t0 = time.perf_counter()
    disjoint_reasons = _breaches(book, disjoint, frac, bankroll)
    per_check_us = (time.perf_counter() - t0) * 1e6
    touching_reasons = _breaches(book, touching, frac, bankroll)

    quoted = ReasonCode.SKIP_ENTITY_LOSS_CAP not in disjoint_reasons
    refused = ReasonCode.SKIP_ENTITY_LOSS_CAP in touching_reasons

    probe.note(
        f"over-wall book: {len(over_keys)} entity keys over, worst "
        f"${worst / 10_000:,.2f} vs wall ${wall_cc / 10_000:,.2f} "
        f"(2 positions @ {price / 100:.0f}c)"
    )
    probe.note(f"disjoint candidate breaches : {[str(r) for r in disjoint_reasons] or '[]'}")
    probe.note(f"touching candidate breaches : {[str(r) for r in touching_reasons] or '[]'}")
    probe.note(f"LimitChecker.check cost     : {per_check_us:.0f} us")

    return probe.grade(
        quoted and refused,
        measured=(
            f"disjoint {'ADMITTED' if quoted else 'REFUSED'} / "
            f"touching {'REFUSED' if refused else 'ADMITTED'}"
        ),
        bound=(
            f"entity wall {frac} x ${bankroll / 10_000:,.0f} = ${wall_cc / 10_000:,.2f}; "
            f"must admit disjoint AND refuse touching"
        ),
        detail=(
            ""
            if (quoted and refused)
            else (
                "a book-WIDE scan declines unrelated flow (R1 exactly)"
                if not quoted
                else "the wall no longer refuses flow that worsens it — the cap is dead"
            )
        ),
    )


# --------------------------------------------------------------------------- #
# V2 — end to end: does the bot actually SEND a priced quote?
# --------------------------------------------------------------------------- #


async def _rig_over_the_wall(tmp: Path, d: Any):
    from combomaker.ops.persistence import Store
    from tests.test_filters import Harness
    from tests.test_lifecycle import Rig
    from tests.test_pricing_engine import seed_event

    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    from tests.test_risk_shadow_mode import _FixedBankroll

    store = await Store.open(tmp / "vitals-quoting.sqlite3", h.clock)
    rig = Rig(h, store, risk_limits=_limits(d.entity_loss_frac))
    # The %-of-bankroll caps only COMPUTE when a bankroll source is wired — the
    # live app always wires one, so a rig without it would grade an axis that is
    # switched off. Same seam every risk test uses; the value is the LIVE equity.
    rig.lifecycle._balance = _FixedBankroll(d.bankroll_cc)  # noqa: SLF001
    return rig


async def _v2(probe: Probe, d: Any) -> tuple[bool, str, str, str]:
    from tests.test_pricing_engine import CROSS_EVENT_LEGS, combo
    from tests.test_withdraw_resolution import (
        _429,
        _FakeLister,
        _rig,
        _tick,
        _wire_lister,
    )

    failures: list[str] = []

    # ---- (a) the book is ALREADY over an entity wall on an unrelated arm ----
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        rig = await _rig_over_the_wall(tmp, d)
        wall_cc = threshold_cc(Fraction(d.entity_loss_frac), d.bankroll_cc)
        # Over the ENTITY wall and only just — constructed by the live predicate,
        # so no OTHER (gross / CVaR / game) axis is what refuses. This is the
        # 2026-07-23 lesson: prove the axis under test is the binding one.
        over: list[str] = []
        for step in range(1, 40):
            for pid in [p for p in list(rig.exposure.positions) if p.startswith("wall")]:
                rig.exposure.remove_position(pid)
            rig.exposure.add_position(_pos("wall1", ARM_A_R4, price_cc=1_000 * step))
            rig.exposure.add_position(_pos("wall2", ARM_A_R6, price_cc=1_000 * step))
            snap = rig.exposure.snapshot(lambda t: 0.5, mass_acceptance=False)
            over = [k for k, v in snap.loss_by_entity_cc.items() if v > wall_cc]
            if over:
                break
        # Production takes exactly this step before it quotes a rehydrated book
        # (quote_app._startup_book_risk_snapshot), and without it the portfolio
        # tail cap fails closed on "never measured" and would be the binding cap
        # instead of the axis under test.
        await rig.lifecycle.recompute_book_risk_offloop()
        await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS))
        sent = rig.sender.created
        bid = max(sent[0]["yes"], sent[0]["no"]) if sent else 0
        probe.note(
            f"(a) over-wall book ({len(over)} keys over ${wall_cc / 10_000:,.2f}) "
            f"-> quotes sent={len(sent)} best bid={bid}cc"
        )
        if not sent or bid <= 0:
            failures.append("(a) an over-wall book on an UNRELATED entity bricked quoting")
        await rig.lifecycle._store.close()  # noqa: SLF001

    # ---- (b) every open slot withdraw-pending, exchange 429s every DELETE ----
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # ``max_open_quotes`` is the axis: fill every slot, then jam them all
        # into the UNKNOWN withdrawal state with a permanently-429ing exchange.
        slots = 8
        rig = await _rig(tmp, "vitals-capacity.sqlite3", max_open_quotes=slots, quotes=slots)

        async def refuses(quote_id: str) -> dict[str, Any]:
            raise _429()

        rig.sender.delete_quote = refuses  # type: ignore[assignment]
        for rfq_id in list(rig.lifecycle._by_rfq):  # noqa: SLF001
            await rig.lifecycle.on_rfq_deleted(rfq_id, {})
        pending = sum(
            1
            for s in rig.lifecycle._open.values()  # noqa: SLF001
            if s.withdraw_pending_reason is not None
        )
        jammed_refused = await _rfq_is_refused(rig)

        # The exchange comes back and PROVES the withdrawals: the read-prover
        # resolves, the slots free, and quoting MUST come back. A transient 429
        # may never become permanent.
        resting: set[str] = set()
        _wire_lister(rig, _FakeLister(lambda: resting))
        for _ in range(4):
            await _tick(rig)
        open_after = rig.lifecycle.open_quote_count
        before = len(rig.sender.created)
        await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id="rfq_after"))
        recovered = len(rig.sender.created) > before
        probe.note(
            f"(b) {slots} slots, all withdraw-pending under a permanent 429 "
            f"(pending={pending}, new RFQ refused while jammed={jammed_refused}); after "
            f"the read-prover: open={open_after}, "
            f"new RFQ quoted={recovered}"
        )
        if not recovered:
            failures.append(
                "(b) a transient 429 became a PERMANENT capacity brick — the "
                "withdraw-pending set never drained and every new RFQ is refused"
            )
        await rig.lifecycle._store.close()  # noqa: SLF001

    ok = not failures
    return (
        ok,
        f"(a) quoted={not any(f.startswith('(a)') for f in failures)}  "
        f"(b) recovered={not any(f.startswith('(b)') for f in failures)}",
        "a quote with a positive bid must leave handle_rfq in BOTH degraded states",
        "; ".join(failures),
    )


async def _rfq_is_refused(rig: Any) -> bool:
    from tests.test_pricing_engine import CROSS_EVENT_LEGS, combo

    before = len(rig.sender.created)
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id="rfq_jammed"))
    return len(rig.sender.created) == before


def check_quoting_liveness(probe: Probe, d: Any) -> Result:
    ok, measured, bound, detail = asyncio.run(_v2(probe, d))
    return probe.grade(ok, measured=measured, bound=bound, detail=detail)


CHECKS = [(V1, check_cap_scope), (V2, check_quoting_liveness)]
