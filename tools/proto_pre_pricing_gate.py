"""F1 monotone pre-pricing gate — PROTOTYPE (hard rule 8: prototype-first).

THE CLAIM TO VALIDATE (lens 3 F1 / synthesis B6): a candidate-FREE
``limits.check`` run BEFORE pricing may pre-decline an RFQ **only** on breach
reasons that are provably candidate-monotone — "already breached without the
candidate ⇒ breached with ANY candidate" — so the gate can never skip an RFQ
that today's full pipeline would have quoted (zero false skips), while skipping
the joint-pricing work the full pipeline would have thrown away anyway.

The candidate-monotone ALLOWLIST validated here (keep in sync with
``risk/limits.py::PRE_PRICING_MONOTONE_REASONS`` — parity-checked in part D):

  - SKIP_MAX_OPEN_QUOTES      pure count + ``adding_quote=True``: candidate-free
                              and with-candidate checks read the SAME count.
  - SKIP_GAME_LOSS_CAP        the per-game loss fold ``_mutex_game_worst_cc`` is
                              monotone in the entry set (E2 dominance): a
                              candidate only ADDS entries to a game, never moves
                              loss out of one, and the ME-count fold-switch only
                              ever moves TOWARD the (larger) comonotone sum.
  - SKIP_UTILIZATION_BACKSTOP Σ gross settlement notional: every candidate adds
                              a non-negative notional.
  - SKIP_BANKROLL_UNAVAILABLE candidate-independent (bankroll reading only).

DELIBERATE EXCLUSIONS:

  - SKIP_MASS_ACCEPTANCE_BREACH  the reason code spans the DELTA axes, where an
                                 opposite-side candidate can hedge |delta| back
                                 UNDER the cap (part B1 constructs it). Its
                                 loss/notional instances ARE monotone, but the
                                 reason alone can't tell the axes apart and
                                 details are never parsed (codebase rule).
  - SKIP_SLATE_CAP               a candidate leg with a KNOWN start re-buckets a
                                 game out of the breached slate (part B2
                                 constructs a true false-skip).
  - SKIP_DIRECTIONAL_CAP         plan-of-record conservatism: the P0-9 mutex
                                 directional fold is documented monotone (adding
                                 an entry never lowers it), but the lens-3
                                 allowlist omitted it and its decline volume is
                                 marginal — excluded until separately validated.
  - per-combo/size caps          candidate-only (a candidate-free check cannot
                                 emit them).
  - CVaR / det-max / ruin        synthesis: NOT the candidate-EV/CVaR credit
                                 paths.
  - halt-class breaches          escalation belongs to the maintenance tick.

PARTS
  A. MONOTONICITY FUZZ against the LIVE ``LimitChecker`` (rule 8: the real
     thing, never a reimplementation): randomized books/bankrolls/candidates —
     assert every allowlisted candidate-free ENFORCED breach persists (same
     reason) in the with-candidate check, for every generated candidate.
  B. NEGATIVE CONTROLS: constructed candidates that CLEAR each excluded
     non-monotone reason (proving the exclusions are load-bearing).
  C. TAPE REPLAY (READ-ONLY, mode=ro + timeout, id-binary-search — the
     hotpath_tape_stats.py pattern): on the recorded window, how many no_quote
     rows the gate would have pre-declined (the saved pricing work) and the
     quote_sent count (each provably un-skippable by the part-A lemma: a sent
     quote means the full check passed, and a passing full check bounds the
     candidate-free allowlisted subset to empty).
  D. PORT PARITY (after the port): the local reference gate below vs the LIVE
     ``risk.limits.monotone_pre_quote_breaches`` on every part-A case —
     identical fire/no-fire decisions and identical reason lists.

Usage:
  ./.venv/Scripts/python.exe tools/proto_pre_pricing_gate.py [--n 5000]
      [--since 2026-07-16T17:30] [--skip-tape]
"""

from __future__ import annotations

import argparse
import collections
import json
import random
import sqlite3
import zlib
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.risk.exposure import (
    ExposureBook,
    LegRef,
    OpenPosition,
    OpenQuoteRisk,
)
from combomaker.risk.limits import Breach, DailyPnl, LimitChecker, RiskLimits

CC = CentiCents
Q = CentiContracts

DB = Path(__file__).resolve().parents[1] / "data" / "combomaker-prod-live-wc.sqlite3"

CONVENTIONS = Conventions(
    verified=True,
    source="proto",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)


def stable_hash(s: str) -> int:
    """Deterministic across processes (str hash is salted per run)."""
    return zlib.crc32(s.encode())


# ---------------------------------------------------------------------------
# The PROTOTYPE gate (the thing being validated). Keep in sync with
# risk/limits.py::monotone_pre_quote_breaches — part D pins the parity.
# ---------------------------------------------------------------------------
PROTO_PRE_PRICING_MONOTONE_REASONS: frozenset[ReasonCode] = frozenset(
    {
        ReasonCode.SKIP_MAX_OPEN_QUOTES,
        ReasonCode.SKIP_GAME_LOSS_CAP,
        ReasonCode.SKIP_UTILIZATION_BACKSTOP,
        ReasonCode.SKIP_BANKROLL_UNAVAILABLE,
    }
)


def proto_gate(breaches: list[Breach]) -> list[Breach]:
    """Local reference: ENFORCED breaches on candidate-monotone reasons only."""
    return [
        b
        for b in breaches
        if not b.shadow and b.reason in PROTO_PRE_PRICING_MONOTONE_REASONS
    ]


# ---------------------------------------------------------------------------
# Part A — randomized monotonicity fuzz against the LIVE checker
# ---------------------------------------------------------------------------
GAMES = ["26JUL18AAABBB", "26JUL18CCCDDD", "26JUL19EEEFFF", "26JUL19GGGHHH"]


def me_event(game: str) -> str:
    return f"KXWCGAME-{game}"  # the ME result family


def total_event(game: str) -> str:
    return f"KXWCTOTAL-{game}"  # non-ME family


def is_me_event(event: str) -> bool | None:
    if event.startswith("KXWCGAME-"):
        return True
    if event.startswith("KXWCTOTAL-"):
        return False
    return None


def rand_legs(rng: random.Random) -> tuple[LegRef, ...]:
    n = rng.randint(1, 3)
    legs: list[LegRef] = []
    seen: set[str] = set()
    for _ in range(n):
        game = rng.choice(GAMES)
        if rng.random() < 0.6:
            event = me_event(game)
            market = f"{event}-T{rng.randint(1, 3)}"
        else:
            event = total_event(game)
            market = f"{event}-O{rng.choice(['15', '25', '35'])}"
        if market in seen:
            continue
        seen.add(market)
        legs.append(LegRef(market, event, rng.choice(["yes", "no"])))
    return tuple(legs)


def rand_book(rng: random.Random) -> ExposureBook:
    book = ExposureBook(CONVENTIONS, is_me_event=is_me_event)
    for i in range(rng.randint(0, 6)):
        book.add_position(
            OpenPosition(
                position_id=f"p{i}",
                combo_ticker=f"COMBO-p{i}",
                collection=None,
                our_side=rng.choice([Side.YES, Side.NO]),
                contracts=Q(rng.randint(100, 20_000)),
                entry_price_cc=CC(rng.randint(500, 9_500)),
                legs=rand_legs(rng),
            )
        )
    for i in range(rng.randint(0, 8)):
        book.upsert_quote(
            OpenQuoteRisk(
                quote_id=f"q{i}",
                rfq_id=f"rfq-q{i}",
                combo_ticker=f"COMBO-q{i}",
                collection=None,
                yes_bid_cc=CC(rng.choice([0, rng.randint(500, 9_500)])),
                no_bid_cc=CC(rng.randint(500, 9_500)),
                contracts=Q(rng.randint(100, 10_000)),
                legs=rand_legs(rng),
            )
        )
    return book


def rand_marginals(rng: random.Random) -> Any:
    holes = rng.random() < 0.15

    def provider(ticker: str) -> float | None:
        h = stable_hash(ticker)
        if holes and h % 7 == 0:
            return None
        return 0.05 + (h % 90) / 100.0

    return provider


def rand_candidates(rng: random.Random) -> list[OpenPosition]:
    """The exact candidate shape handle_rfq passes: an OpenQuoteRisk's
    hypothetical_positions — including hedge-shaped ones (the adversarial
    class that makes the EXCLUDED axes non-monotone)."""
    quote = OpenQuoteRisk(
        quote_id="cand",
        rfq_id="rfq-cand",
        combo_ticker="COMBO-cand",
        collection=None,
        yes_bid_cc=CC(rng.choice([0, rng.randint(500, 9_500)])),
        no_bid_cc=CC(rng.randint(500, 9_500)),
        contracts=Q(rng.randint(100, 30_000)),
        legs=rand_legs(rng),
    )
    return quote.hypothetical_positions(CONVENTIONS)


def rand_start_provider(rng: random.Random) -> Any:
    base = datetime(2026, 7, 18, 19, 0, tzinfo=UTC)
    known = rng.random() < 0.7

    def provider(market_ticker: str) -> datetime | None:
        h = stable_hash(market_ticker)
        if not known or h % 5 == 0:
            return None
        return base + timedelta(hours=h % 30)

    return provider


def fuzz_monotonicity(n: int, seed: int) -> collections.Counter:
    rng = random.Random(seed)
    stats: collections.Counter = collections.Counter()
    live_gate = None
    try:  # part D (post-port): the LIVE ported gate, if it exists yet
        from combomaker.risk.limits import monotone_pre_quote_breaches

        live_gate = monotone_pre_quote_breaches
    except ImportError:
        pass

    for case in range(n):
        # Tight caps so every cap trips at meaningful frequency.
        limits = RiskLimits(
            max_open_quotes=rng.randint(1, 8),
            max_event_worst_case_loss_dollars=rng.choice([2.0, 10.0, 1_000.0]),
            max_gross_notional_dollars=rng.choice([5.0, 50.0, 5_000.0]),
            max_market_delta_contracts=rng.choice([5.0, 50.0, 300.0]),
            max_event_delta_contracts=rng.choice([5.0, 50.0, 500.0]),
            game_loss_frac=Fraction(rng.choice([1, 8, 50]), 100),
            slate_loss_frac=Fraction(rng.choice([1, 8, 50]), 100),
            directional_frac=Fraction(rng.choice([1, 10, 50]), 100),
            absolute_notional_multiple=rng.choice([1, 3]),
            caps_shadow_mode=rng.random() < 0.1,
        )
        checker = LimitChecker(limits)
        book = rand_book(rng)
        marg = rand_marginals(rng)
        stp = rand_start_provider(rng)
        bankroll = rng.choice([None, 50_000, 500_000, 5_000_000])
        pnl = DailyPnl(realized_cc=rng.choice([0, 0, -100_000]))
        candidates = rand_candidates(rng)

        free = checker.check(
            book,
            marg,
            pnl,
            candidate_positions=None,
            adding_quote=True,
            risk_bankroll_cc=bankroll,
            bankroll_source_configured=True,
            start_time_provider=stp,
            halt_inputs=None,
            book_risk=None,
        )
        gate = proto_gate(free)
        stats["cases"] += 1
        if live_gate is not None:  # part D parity: identical decisions + reasons
            live = live_gate(free)
            assert [(b.reason, b.detail) for b in live] == [
                (b.reason, b.detail) for b in gate
            ], f"case {case}: live gate != prototype gate"
            stats["parity_checked"] += 1
        if not gate:
            continue
        stats["gate_fired"] += 1
        withc = checker.check(
            book,
            marg,
            pnl,
            candidate_positions=candidates,
            adding_quote=True,
            risk_bankroll_cc=bankroll,
            bankroll_source_configured=True,
            start_time_provider=stp,
            halt_inputs=None,
            book_risk=None,
        )
        enforced_withc = [b for b in withc if not b.shadow]
        # THE LEMMA: gate fired ⇒ today's full (with-candidate) check declines
        # too, on (at least) the same reasons — zero false skips possible.
        assert enforced_withc, f"case {case}: gate fired but full check PASSED"
        withc_reasons = {b.reason for b in enforced_withc}
        for b in gate:
            assert b.reason in withc_reasons, (
                f"case {case}: gate reason {b.reason} vanished with candidate "
                f"(withc={sorted(str(r) for r in withc_reasons)})"
            )
        stats["gate_reasons_persisted"] += len(gate)
    return stats


# ---------------------------------------------------------------------------
# Part B — negative controls: each EXCLUDED non-monotone reason CLEARED by a
# candidate (the exact false-skip the allowlist restriction prevents)
# ---------------------------------------------------------------------------
def half_marginal(_ticker: str) -> float | None:
    return 0.5


def negative_controls() -> list[str]:
    out: list[str] = []
    game_a, game_b = GAMES[0], GAMES[1]
    ev_a, ev_b = me_event(game_a), me_event(game_b)

    # B1 — MASS-ACCEPTANCE DELTA: +40ct delta breach on a market (cap 30),
    # hedged to +20ct by an opposite-side candidate ⇒ the breach CLEARS.
    limits1 = RiskLimits(max_market_delta_contracts=30.0)
    book1 = ExposureBook(CONVENTIONS, is_me_event=is_me_event)
    book1.add_position(
        OpenPosition(
            position_id="d",
            combo_ticker="C-d",
            collection=None,
            our_side=Side.YES,
            contracts=Q(4_000),
            entry_price_cc=CC(5_000),
            legs=(LegRef(f"{ev_a}-T1", ev_a, "yes"),),
        )
    )
    counter = OpenPosition(
        position_id="c",
        combo_ticker="C-c",
        collection=None,
        our_side=Side.NO,  # short the same outcome ⇒ opposite-sign delta
        contracts=Q(2_000),
        entry_price_cc=CC(5_000),
        legs=(LegRef(f"{ev_a}-T1", ev_a, "yes"),),
    )
    free1 = {
        b.reason
        for b in LimitChecker(limits1).check(
            book1, half_marginal, DailyPnl(), adding_quote=True
        )
    }
    withc1 = {
        b.reason
        for b in LimitChecker(limits1).check(
            book1,
            half_marginal,
            DailyPnl(),
            candidate_positions=[counter],
            adding_quote=True,
        )
    }
    if ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH in free1 and (
        ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH not in withc1
    ):
        out.append(
            "B1 mass-delta: candidate-free delta breach CLEARED by an "
            "opposite-side candidate (non-monotone axis proven)"
        )

    # B2 — SLATE: two unknown-start games pool into the UNKNOWN slate ($35 >
    # $30 cap); a candidate leg with a KNOWN start re-buckets game A out, so
    # UNKNOWN drops to $10 and the destination slate ($25.01) is under cap too
    # ⇒ NO slate breach remains — a true false-skip had the gate pre-declined.
    limits2 = RiskLimits(slate_loss_frac=Fraction(3, 100))  # $30 of a $1000 bankroll
    book2 = ExposureBook(CONVENTIONS, is_me_event=is_me_event)
    book2.add_position(
        OpenPosition(
            position_id="sA",
            combo_ticker="C-sA",
            collection=None,
            our_side=Side.NO,
            contracts=Q(5_000),
            entry_price_cc=CC(5_000),  # $25
            legs=(LegRef(f"{ev_a}-T1", ev_a, "yes"),),
        )
    )
    book2.add_position(
        OpenPosition(
            position_id="sB",
            combo_ticker="C-sB",
            collection=None,
            our_side=Side.NO,
            contracts=Q(2_000),
            entry_price_cc=CC(5_000),  # $10
            legs=(LegRef(f"{ev_b}-T1", ev_b, "yes"),),
        )
    )
    mover = OpenPosition(
        position_id="m",
        combo_ticker="C-m",
        collection=None,
        our_side=Side.NO,
        contracts=Q(100),
        entry_price_cc=CC(100),  # $0.01
        legs=(LegRef(f"{total_event(game_a)}-O25", total_event(game_a), "yes"),),
    )
    start = datetime(2026, 7, 18, 19, 0, tzinfo=UTC)

    def stp(market_ticker: str) -> datetime | None:
        # Book legs (KXWCGAME-*): start UNKNOWN; candidate leg (KXWCTOTAL-*):
        # KNOWN ⇒ re-buckets its game out of the UNKNOWN slate.
        return start if market_ticker.startswith("KXWCTOTAL-") else None

    free2 = {
        b.reason
        for b in LimitChecker(limits2).check(
            book2,
            half_marginal,
            DailyPnl(),
            adding_quote=True,
            risk_bankroll_cc=10_000_000,
            bankroll_source_configured=True,
            start_time_provider=stp,
        )
    }
    withc2 = {
        b.reason
        for b in LimitChecker(limits2).check(
            book2,
            half_marginal,
            DailyPnl(),
            candidate_positions=[mover],
            adding_quote=True,
            risk_bankroll_cc=10_000_000,
            bankroll_source_configured=True,
            start_time_provider=stp,
        )
    }
    if ReasonCode.SKIP_SLATE_CAP in free2 and ReasonCode.SKIP_SLATE_CAP not in withc2:
        out.append(
            "B2 slate: UNKNOWN-slate breach fully CLEARED by a known-start "
            "candidate leg re-bucketing its game (non-monotone corner proven)"
        )
    return out


# ---------------------------------------------------------------------------
# Part C — tape replay (READ-ONLY; hotpath_tape_stats.py access pattern)
# ---------------------------------------------------------------------------
def first_id_at_or_after(
    cur: sqlite3.Cursor, table: str, ts_col: str, target: str
) -> int:
    cur.execute(f"SELECT MIN(id), MAX(id) FROM {table}")  # noqa: S608
    lo, hi = cur.fetchone()
    if lo is None:
        return 0

    def at_of(i: int) -> str | None:
        cur.execute(
            f"SELECT {ts_col} FROM {table} WHERE id>=? ORDER BY id LIMIT 1",  # noqa: S608
            (i,),
        )
        r = cur.fetchone()
        return r[0] if r else None

    a, b = lo, hi
    while a < b:
        m = (a + b) // 2
        t = at_of(m)
        if t is None or t < target:
            a = m + 1
        else:
            b = m
    return a


def tape_replay(since: str) -> None:
    gate_names = {str(r) for r in PROTO_PRE_PRICING_MONOTONE_REASONS}
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    try:
        cur = con.cursor()
        d0 = first_id_at_or_after(cur, "decisions", "at", since)
        kinds: collections.Counter = collections.Counter()
        gate_hits: collections.Counter = collections.Counter()
        no_quote = 0
        would_skip = 0
        cur.execute("SELECT kind, reasons_json FROM decisions WHERE id>=?", (d0,))
        for kind, rj in cur:
            kinds[kind] += 1
            if kind != "no_quote":
                continue
            no_quote += 1
            rs = set(json.loads(rj))
            hit = rs & gate_names
            if hit:
                would_skip += 1
                for r in hit:
                    gate_hits[r] += 1
        print(f"\n== part C: tape replay since {since} (READ-ONLY) ==")
        print(f"decision kinds: {dict(kinds)}")
        qs = kinds.get("quote_sent", 0)
        print(
            f"no_quote rows: {no_quote}; gate would pre-decline {would_skip} "
            f"({100 * would_skip / max(1, no_quote):.1f}% of no-quotes) — the "
            f"joint-pricing work saved under binding caps"
        )
        print(f"per-reason gate hits: {dict(gate_hits.most_common())}")
        print(
            f"quote_sent rows: {qs} — ZERO false skips possible on these by the "
            f"part-A lemma (full check passed => allowlisted candidate-free "
            f"subset empty)"
        )
    finally:
        con.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--since", default="2026-07-16T17:30")
    ap.add_argument("--skip-tape", action="store_true")
    args = ap.parse_args()

    print(f"== part A: monotonicity fuzz (n={args.n}, seed={args.seed}) ==")
    stats = fuzz_monotonicity(args.n, args.seed)
    print(dict(stats))
    assert stats["gate_fired"] > 0, "fuzz never fired the gate — caps too loose"
    print(
        f"OK: {stats['gate_fired']} gate firings / {stats['cases']} cases — every "
        f"allowlisted candidate-free breach persisted under the candidate "
        f"({stats['gate_reasons_persisted']} reason persistences, 0 violations)"
    )
    if stats.get("parity_checked"):
        print(
            f"part D parity: live monotone_pre_quote_breaches identical to the "
            f"prototype on all {stats['parity_checked']} cases"
        )
    else:
        print("part D parity: live gate not ported yet (skipped)")

    print("\n== part B: negative controls (why the exclusions are exclusions) ==")
    controls = negative_controls()
    for line in controls:
        print(line)
    assert len(controls) == 2, (
        f"expected both excluded-reason counterexamples to demonstrate, got "
        f"{len(controls)}: {controls}"
    )

    if not args.skip_tape:
        tape_replay(args.since)


if __name__ == "__main__":
    main()
