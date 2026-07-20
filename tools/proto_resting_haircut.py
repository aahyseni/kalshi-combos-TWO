"""Quote-time resting-quote haircut — PROTOTYPE (hard rule 8: prototype-first).

OPERATOR-APPROVED DESIGN (2026-07-17): quote-time caps currently fold ALL
resting (open) quotes at 100% mass-acceptance worst case, DOUBLE-counting the
defense the confirm path (P0-2 serial provisional reservations + candidate MC
+ freshness + waiver) already enforces EXACTLY. The change: at QUOTE TIME ONLY,
the resting-quote contribution to every mass-acceptance fold is weighted at
``resting_quote_weight`` (armed 0.40; default 1.0 = today), with a BURST FLOOR:
never less than the FULL (100%) contribution of the ``resting_floor_count``
(default 3) largest resting quotes on the relevant axis.

THE COMPOSITION VALIDATED HERE (per axis, per bucket):

    value = min( full,  max( ceil(w*full + (1-w)*base),  floor_base + topK ) )

where ``base``  = the fold of committed positions (+ candidates) ONLY,
      ``full``  = the fold of committed + candidates + ALL resting at 100%
                  (exactly today's mass-acceptance value),
      ``topK``  = the comonotone sum of the K largest per-quote worst-case
                  contributions on that axis/bucket,
      ``floor_base`` = ``base`` on the additive axes (gross/notional/delta);
                  on the MUTEX-FOLDED axes (game loss, directional) it tracks
                  the COMBINED (base + ALL resting) book's netting regime:
                  the netted ``base`` iff the combined ME-event census nets
                  (exactly 1 explicit-True ME event), else the COMONOTONE/
                  summed base fold. FIX 2026-07-17: the fail-closed 1->2
                  ME-event transition is SUPERADDITIVE — fold(base+topK) can
                  exceed netted(base) + topK because the base UN-NETS inside
                  the combined fold — so a netted base term under-floors the
                  K-largest burst by up to the base's netting credit (found
                  on the canonical advance-hedge book; pinned in
                  tests/test_resting_haircut.py::TestMutexRegimeFloorPinned).

Properties proven by construction and FUZZED here:
  P1 MONOTONE in the resting set: ``full`` is monotone (E2), ``base`` is
     unaffected, ``topK`` (a top-K sum of non-negatives) is monotone, and
     min/max of monotone terms is monotone — adding a resting quote never
     decreases any bucket. Required for the F1 pre-gate lemma.
  P2 MONOTONE in the candidate/committed set: value = min(full, max(w*full +
     (1-w)*base, base + topK)) is monotone in BOTH ``base`` and ``full`` for
     w in [0,1] — so "already breached candidate-free => breached with any
     candidate" (the F1 lemma) survives the haircut.
  P3 FLOOR: the true full fold of the K largest resting quotes is <= min(full,
     topK) (fold monotonicity + branch-max subadditivity), and value >=
     min(full, max(., topK)) >= min(full, topK) — the burst floor holds.
  P4 DEFAULT PARITY: at w == 1 the blend equals ``full`` exactly and the min
     clamps the (possibly overshooting) comonotone topK term back to ``full``
     — bit-identical to today on the int axes.

PARTS
  A. MONOTONICITY FUZZ (P1): randomized books; composed aggregates for quote
     set S vs S + one more quote — every bucket non-decreasing, every axis.
  B. WEIGHT-1 PARITY (P4): composed aggregates at w=1 == the LIVE mass
     snapshot, int axes exact, float axes to 1e-9 rel.
  C. FLOOR (P3): composed contribution >= the LIVE full fold of ONLY the K
     largest resting quotes (the true 100% top-K contribution).
  D. POST-PORT: (D1) parity — live ``snapshot(resting_quote_weight=...)`` ==
     this prototype's composition on every part-A case; (D2) the F1 pre-gate
     lemma re-check with the haircut ARMED through the LIVE ``LimitChecker``
     (candidate-free armed breach on an allowlisted reason persists under ANY
     candidate, both checks armed).
  E. TAPE REPLAY (READ-ONLY, mode=ro): the 7/16-17 skip_game_loss_cap decline
     wall — parse each breach's loss/threshold from the recorded detail and
     estimate the flow unlock at weight 0.40 (all-resting assumption stated;
     the tape does not record the committed/resting decomposition).

Usage:
  ./.venv/Scripts/python.exe tools/proto_resting_haircut.py [--n 3000]
      [--since 2026-07-16T17:30] [--until 2026-07-17T23:59] [--skip-tape]
"""

from __future__ import annotations

import argparse
import collections
import json
import random
import re
import sqlite3
import zlib
from fractions import Fraction
from pathlib import Path
from typing import Any

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CC_PER_DOLLAR, CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.pricing.grouping import game_key
from combomaker.risk.exposure import (
    ExposureBook,
    LegRef,
    OpenPosition,
    OpenQuoteRisk,
    analytic_leg_deltas,
)
from combomaker.risk.limits import DailyPnl, LimitChecker, RiskLimits

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

GAMES = ["26JUL18AAABBB", "26JUL18CCCDDD", "26JUL19EEEFFF", "26JUL19GGGHHH"]


def me_event(game: str) -> str:
    return f"KXWCGAME-{game}"


def adv_event(game: str) -> str:
    # SECOND ME-flagged family on the SAME game (advance / 1H winner shape):
    # makes the fail-closed 1->2 ME-event transition REACHABLE in every fuzz
    # part. The original generator flagged exactly ONE ME family per game
    # (KXWCGAME True, KXWCTOTAL False), so all pre-2026-07-17 floor claims
    # were vacuous in the >=2-ME regime — the regime the floor was broken in.
    return f"KXWCADV-{game}"


def total_event(game: str) -> str:
    return f"KXWCTOTAL-{game}"


def is_me_event(event: str) -> bool | None:
    if event.startswith(("KXWCGAME-", "KXWCADV-")):
        return True
    if event.startswith("KXWCTOTAL-"):
        return False
    return None


def stable_hash(s: str) -> int:
    return zlib.crc32(s.encode())


# ---------------------------------------------------------------------------
# The PROTOTYPE composition (the thing being validated), built from LIVE
# primitives only: base/full/one-quote ExposureBook snapshots + the live
# per-quote worst-case figures (hypothetical_positions / analytic_leg_deltas).
# Keep in sync with risk/exposure.py::ExposureBook.snapshot's haircut branch —
# part D1 pins the parity.
# ---------------------------------------------------------------------------
def compose_cc(
    base: int, full: int, topk: int, weight: Fraction, floor_base: int | None = None
) -> int:
    """min(full, max(ceil-blend, floor_base+topK)) on the int-cc axes.

    ``floor_base`` (default: ``base``) is the burst floor's base term — on the
    mutex-folded axes the caller passes the COMONOTONE base fold whenever the
    combined (base + all resting) ME-event census fails closed (2026-07-17)."""
    num, den = weight.numerator, weight.denominator
    blend = -(-(num * full + (den - num) * base) // den)  # ceil, conservative
    fb = base if floor_base is None else floor_base
    return min(full, max(blend, fb + topk))


def compose_float(
    base: float, full: float, topk: float, w: float, floor_base: float | None = None
) -> float:
    fb = base if floor_base is None else floor_base
    return min(full, max(w * full + (1.0 - w) * base, fb + topk))


def topk_sum(values: list[int | float], k: int) -> int | float:
    return sum(sorted(values, reverse=True)[:k])


def book_with(
    positions: list[OpenPosition], quotes: list[OpenQuoteRisk], me: bool = True
) -> ExposureBook:
    book = ExposureBook(CONVENTIONS, is_me_event=is_me_event if me else None)
    for p in positions:
        book.add_position(p)
    for q in quotes:
        book.upsert_quote(q)
    return book


def quote_games(quote: OpenQuoteRisk) -> set[str]:
    return {game_key(leg.event_ticker) for leg in quote.legs if leg.event_ticker}


def game_me_census(
    positions: list[OpenPosition],
    quotes: list[OpenQuoteRisk],
    extra_positions: tuple[OpenPosition, ...],
    marginals: Any,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Per-game explicit-True ME-event census over the COMBINED requires-all
    entries, one per mutex-folded axis (loss, directional) — mirroring the
    live snapshot's entry lists exactly: EVERY long-NO position counts on the
    loss axis, only delta-computable ones on the directional axis; a quote's
    requires flag is its worst-loss hypothetical's side. Keep in sync with
    exposure.py::_me_event_census + snapshot's armed loop (part D1 pins it)."""
    loss: dict[str, set[str]] = collections.defaultdict(set)
    dirn: dict[str, set[str]] = collections.defaultdict(set)
    for p in [*positions, *extra_positions]:
        if p.our_side is not Side.NO:
            continue
        deltas = None if not p.risk_modeled else analytic_leg_deltas(p, marginals)
        for leg in p.legs:
            e = leg.event_ticker
            if e and is_me_event(e) is True:
                loss[game_key(e)].add(e)
                if deltas is not None:
                    dirn[game_key(e)].add(e)
    for q in quotes:
        hypos = q.hypothetical_positions(CONVENTIONS)
        if not hypos:
            continue
        worst = max(hypos, key=lambda h: h.max_loss_cc)
        if worst.our_side is not Side.NO:
            continue
        for leg in q.legs:
            e = leg.event_ticker
            if e and is_me_event(e) is True:
                loss[game_key(e)].add(e)
                dirn[game_key(e)].add(e)
    return loss, dirn


def haircut_composed(
    positions: list[OpenPosition],
    quotes: list[OpenQuoteRisk],
    marginals: Any,
    weight: Fraction,
    floor_count: int,
    extra_positions: tuple[OpenPosition, ...] = (),
) -> dict[str, Any]:
    """The composed quote-time aggregates, from live snapshots only."""
    base = book_with(positions, []).snapshot(
        marginals, mass_acceptance=True, extra_positions=extra_positions
    )
    full = book_with(positions, quotes).snapshot(
        marginals, mass_acceptance=True, extra_positions=extra_positions
    )
    # COMONOTONE base folds (is_me_event NOT wired => the mutex folds fail to
    # the comonotone/summed values): the burst floor's base term on the
    # mutex-folded axes whenever the COMBINED census fails closed (2026-07-17).
    base_como = book_with(positions, [], me=False).snapshot(
        marginals, mass_acceptance=True, extra_positions=extra_positions
    )
    loss_census, dir_census = game_me_census(
        positions, quotes, extra_positions, marginals
    )

    # Per-quote worst-case contributions on each axis (live figures).
    per_quote_loss: dict[str, list[int]] = collections.defaultdict(list)      # game
    per_quote_notional: dict[str, list[int]] = collections.defaultdict(list)  # game
    gross_contribs: list[int] = []
    per_market_mag: dict[str, list[float]] = collections.defaultdict(list)
    per_game_mag: dict[str, list[float]] = collections.defaultdict(list)
    per_game_dir: dict[str, list[float]] = collections.defaultdict(list)

    for quote in quotes:
        hypos = quote.hypothetical_positions(CONVENTIONS)
        if not hypos:
            continue
        worst_loss = max(h.max_loss_cc for h in hypos)
        worst_notional = max(h.gross_settlement_notional_cc for h in hypos)
        gross_contribs.append(worst_loss)
        for game in quote_games(quote):
            per_quote_loss[game].append(worst_loss)
            per_quote_notional[game].append(worst_notional)
        # Per-market |delta| bound (keep in sync with the snapshot's per_market
        # fold — max over hypos of |analytic delta|; parity-pinned in part D1).
        pm: dict[str, float] = collections.defaultdict(float)
        for hypo in hypos:
            deltas = analytic_leg_deltas(hypo, marginals)
            if deltas is None:
                continue
            for ticker, delta in deltas.items():
                pm[ticker] = max(pm[ticker], abs(delta))
        for ticker, mag in pm.items():
            per_market_mag[ticker].append(mag)
        legs_by_game: dict[str, list[LegRef]] = collections.defaultdict(list)
        for leg in quote.legs:
            if leg.event_ticker:
                legs_by_game[game_key(leg.event_ticker)].append(leg)
        for game, glegs in legs_by_game.items():
            gmag = sum(pm.get(g.market_ticker, 0.0) for g in glegs)
            per_game_mag[game].append(gmag)
            per_game_dir[game].append(gmag * CC_PER_DOLLAR)

    out: dict[str, Any] = {"unknown": full.unknown_marginals}

    # LOSS axis per game (int cc). Floor base term tracks the COMBINED book's
    # mutex regime: netted base iff the combined census nets (exactly 1 ME
    # event), else the comonotone base (2026-07-17 fix).
    loss: dict[str, int] = {}
    for game, full_v in full.worst_case_loss_by_game_cc.items():
        base_v = base.worst_case_loss_by_game_cc.get(game, 0)
        tk = int(topk_sum(per_quote_loss.get(game, []), floor_count))
        floor_v = base_v
        if len(loss_census.get(game, ())) != 1:
            floor_v = base_como.worst_case_loss_by_game_cc.get(game, 0)
        loss[game] = compose_cc(base_v, full_v, tk, weight, floor_base=floor_v)
    out["worst_case_loss_by_game_cc"] = loss

    # NOTIONAL axis per game (int cc).
    notional: dict[str, int] = {}
    for game, full_v in full.gross_settlement_notional_by_game_cc.items():
        base_v = base.gross_settlement_notional_by_game_cc.get(game, 0)
        tk = int(topk_sum(per_quote_notional.get(game, []), floor_count))
        notional[game] = compose_cc(base_v, full_v, tk, weight)
    out["gross_settlement_notional_by_game_cc"] = notional

    # GROSS premium axis (int cc, whole book).
    out["gross_notional_cc"] = compose_cc(
        base.gross_notional_cc,
        full.gross_notional_cc,
        int(topk_sum(gross_contribs, floor_count)),
        weight,
    )

    # DELTA axes (floats, sign-aligned magnitudes).
    w = float(weight)
    delta_market: dict[str, float] = {}
    for ticker, full_d in full.delta_by_market.items():
        base_d = base.delta_by_market.get(ticker, 0.0)
        mags = per_market_mag.get(ticker, [])
        composed = compose_float(
            abs(base_d), abs(base_d) + sum(mags),
            float(topk_sum(mags, floor_count)), w,
        )
        sign = 1.0 if base_d >= 0 else -1.0
        delta_market[ticker] = sign * composed if full_d or composed else full_d
    out["delta_by_market"] = delta_market
    delta_game: dict[str, float] = {}
    for game, full_d in full.delta_by_game.items():
        base_d = base.delta_by_game.get(game, 0.0)
        mags = per_game_mag.get(game, [])
        composed = compose_float(
            abs(base_d), abs(base_d) + sum(mags),
            float(topk_sum(mags, floor_count)), w,
        )
        sign = 1.0 if base_d >= 0 else -1.0
        delta_game[game] = sign * composed if full_d or composed else full_d
    out["delta_by_game"] = delta_game

    # DIRECTIONAL axis per game (int cc of a float fold; +/-2cc parity slack —
    # the base/full reads here are already int-truncated by the snapshot).
    # Same regime-tracked floor base term as the loss axis (2026-07-17 fix).
    directional: dict[str, int] = {}
    for game, full_v in full.directional_by_game_cc.items():
        base_v = base.directional_by_game_cc.get(game, 0)
        tk = float(topk_sum(per_game_dir.get(game, []), floor_count))
        floor_v = float(base_v)
        if len(dir_census.get(game, ())) != 1:
            floor_v = float(base_como.directional_by_game_cc.get(game, 0))
        directional[game] = int(
            compose_float(float(base_v), float(full_v), tk, w, floor_base=floor_v)
        )
    out["directional_by_game_cc"] = directional
    return out


# ---------------------------------------------------------------------------
# Randomized book generation (proto_pre_pricing_gate pattern)
# ---------------------------------------------------------------------------
def rand_legs(rng: random.Random) -> tuple[LegRef, ...]:
    n = rng.randint(1, 3)
    legs: list[LegRef] = []
    seen: set[str] = set()
    for _ in range(n):
        game = rng.choice(GAMES)
        r = rng.random()
        if r < 0.45:
            event = me_event(game)
            market = f"{event}-T{rng.randint(1, 3)}"
        elif r < 0.75:
            # Second ME family on the same game -> 2-ME fail-closed regime
            # reachable (2026-07-17 floor fix coverage).
            event = adv_event(game)
            market = f"{event}-T{rng.randint(1, 3)}"
        else:
            event = total_event(game)
            market = f"{event}-O{rng.choice(['15', '25', '35'])}"
        if market in seen:
            continue
        seen.add(market)
        legs.append(LegRef(market, event, rng.choice(["yes", "no"])))
    return tuple(legs)


def rand_positions(rng: random.Random) -> list[OpenPosition]:
    return [
        OpenPosition(
            position_id=f"p{i}",
            combo_ticker=f"COMBO-p{i}",
            collection=None,
            our_side=rng.choice([Side.YES, Side.NO]),
            contracts=Q(rng.randint(100, 20_000)),
            entry_price_cc=CC(rng.randint(500, 9_500)),
            legs=rand_legs(rng),
        )
        for i in range(rng.randint(0, 5))
    ]


def rand_quote(rng: random.Random, i: int) -> OpenQuoteRisk:
    return OpenQuoteRisk(
        quote_id=f"q{i}",
        rfq_id=f"rfq-q{i}",
        combo_ticker=f"COMBO-q{i}",
        collection=None,
        yes_bid_cc=CC(rng.choice([0, rng.randint(500, 9_500)])),
        no_bid_cc=CC(rng.randint(500, 9_500)),
        contracts=Q(rng.randint(100, 10_000)),
        legs=rand_legs(rng),
    )


def full_marginals(ticker: str) -> float | None:
    return 0.05 + (stable_hash(ticker) % 90) / 100.0


def rand_weight(rng: random.Random) -> Fraction:
    return rng.choice(
        [Fraction(2, 5), Fraction(1, 4), Fraction(3, 4), Fraction(1, 10)]
    )


def assert_bucketwise_le(
    smaller: dict[str, Any], larger: dict[str, Any], axes: list[str], case: int
) -> None:
    for axis in axes:
        small_map, large_map = smaller[axis], larger[axis]
        for bucket, v in small_map.items():
            v2 = large_map.get(bucket, 0)
            if axis.startswith("delta"):
                assert abs(v) <= abs(v2) + 1e-6, (
                    f"case {case}: {axis}[{bucket}] shrank {abs(v):.6f} -> {abs(v2):.6f}"
                )
            else:
                assert v <= v2 + (2 if axis == "directional_by_game_cc" else 0), (
                    f"case {case}: {axis}[{bucket}] shrank {v} -> {v2}"
                )


# ---------------------------------------------------------------------------
# Part A — monotonicity fuzz (P1)
# ---------------------------------------------------------------------------
def fuzz_monotonicity(n: int, seed: int) -> collections.Counter:
    rng = random.Random(seed)
    stats: collections.Counter = collections.Counter()
    axes = [
        "worst_case_loss_by_game_cc",
        "gross_settlement_notional_by_game_cc",
        "delta_by_market",
        "delta_by_game",
        "directional_by_game_cc",
    ]
    for case in range(n):
        positions = rand_positions(rng)
        quotes = [rand_quote(rng, i) for i in range(rng.randint(0, 6))]
        newq = rand_quote(rng, 99)
        weight = rand_weight(rng)
        floor = rng.choice([1, 2, 3])
        before = haircut_composed(positions, quotes, full_marginals, weight, floor)
        after = haircut_composed(
            positions, [*quotes, newq], full_marginals, weight, floor
        )
        assert before["gross_notional_cc"] <= after["gross_notional_cc"], (
            f"case {case}: gross shrank"
        )
        assert_bucketwise_le(before, after, axes, case)
        stats["cases"] += 1
    return stats


# ---------------------------------------------------------------------------
# Part B — weight-1 parity (P4): composed == today's live mass snapshot
# ---------------------------------------------------------------------------
def fuzz_weight1_parity(n: int, seed: int) -> collections.Counter:
    rng = random.Random(seed + 1)
    stats: collections.Counter = collections.Counter()
    for case in range(n):
        positions = rand_positions(rng)
        quotes = [rand_quote(rng, i) for i in range(rng.randint(0, 6))]
        floor = rng.choice([1, 3])
        composed = haircut_composed(
            positions, quotes, full_marginals, Fraction(1), floor
        )
        live = book_with(positions, quotes).snapshot(
            full_marginals, mass_acceptance=True
        )
        assert composed["gross_notional_cc"] == live.gross_notional_cc, f"case {case}"
        assert composed["worst_case_loss_by_game_cc"] == (
            live.worst_case_loss_by_game_cc
        ), f"case {case}: loss axis diverged at w=1"
        assert composed["gross_settlement_notional_by_game_cc"] == (
            live.gross_settlement_notional_by_game_cc
        ), f"case {case}: notional axis diverged at w=1"
        for ticker, d in live.delta_by_market.items():
            assert abs(composed["delta_by_market"][ticker] - d) < 1e-6, (
                f"case {case}: delta_by_market[{ticker}]"
            )
        for game, d in live.delta_by_game.items():
            assert abs(composed["delta_by_game"][game] - d) < 1e-6, (
                f"case {case}: delta_by_game[{game}]"
            )
        for game, d in live.directional_by_game_cc.items():
            assert abs(composed["directional_by_game_cc"][game] - d) <= 2, (
                f"case {case}: directional[{game}]"
            )
        stats["cases"] += 1
    return stats


# ---------------------------------------------------------------------------
# Part C — burst floor (P3): composed >= live full fold of the K largest
# ---------------------------------------------------------------------------
def fuzz_floor(n: int, seed: int) -> collections.Counter:
    rng = random.Random(seed + 2)
    stats: collections.Counter = collections.Counter()
    for case in range(n):
        positions = rand_positions(rng)
        quotes = [rand_quote(rng, i) for i in range(rng.randint(1, 6))]
        weight = rand_weight(rng)
        floor = rng.choice([1, 2, 3])
        composed = haircut_composed(positions, quotes, full_marginals, weight, floor)
        # The TRUE 100% contribution of the K largest resting quotes on the loss
        # axis: the live fold with ONLY those K quotes resting. K largest by
        # per-quote worst loss (the axis figure the floor is defined on).
        by_loss = sorted(
            quotes,
            key=lambda q: max(
                (h.max_loss_cc for h in q.hypothetical_positions(CONVENTIONS)),
                default=0,
            ),
            reverse=True,
        )[:floor]
        live_topk = book_with(positions, by_loss).snapshot(
            full_marginals, mass_acceptance=True
        )
        for game, v in live_topk.worst_case_loss_by_game_cc.items():
            assert composed["worst_case_loss_by_game_cc"].get(game, 0) >= v, (
                f"case {case}: floor violated on game {game}: composed "
                f"{composed['worst_case_loss_by_game_cc'].get(game, 0)} < top-K {v}"
            )
        # Directional-axis floor (2026-07-17: broke identically to the loss
        # axis at the 1->2 ME fail-closed transition). K largest BY LOSS is
        # the floor's definition on both mutex axes; +/-2cc int-trunc slack.
        for game, dv in live_topk.directional_by_game_cc.items():
            assert composed["directional_by_game_cc"].get(game, 0) >= dv - 2, (
                f"case {case}: directional floor violated on game {game}: "
                f"composed {composed['directional_by_game_cc'].get(game, 0)} "
                f"< top-K {dv}"
            )
        assert composed["gross_notional_cc"] >= live_topk.gross_notional_cc, (
            f"case {case}: gross floor violated"
        )
        stats["cases"] += 1
    return stats


# ---------------------------------------------------------------------------
# Part D (post-port) — D1 port parity; D2 the F1 lemma with the haircut ARMED
# ---------------------------------------------------------------------------
def port_available() -> bool:
    import inspect

    from combomaker.risk.exposure import ExposureBook as EB

    return "resting_quote_weight" in inspect.signature(EB.snapshot).parameters


def fuzz_port_parity(n: int, seed: int) -> collections.Counter:
    rng = random.Random(seed + 3)
    stats: collections.Counter = collections.Counter()
    for case in range(n):
        positions = rand_positions(rng)
        quotes = [rand_quote(rng, i) for i in range(rng.randint(0, 6))]
        weight = rand_weight(rng)
        floor = rng.choice([1, 2, 3])
        composed = haircut_composed(positions, quotes, full_marginals, weight, floor)
        live = book_with(positions, quotes).snapshot(
            full_marginals,
            mass_acceptance=True,
            resting_quote_weight=weight,
            resting_floor_count=floor,
        )
        assert composed["worst_case_loss_by_game_cc"] == (
            live.worst_case_loss_by_game_cc
        ), (
            f"case {case}: loss-axis port mismatch\nproto="
            f"{composed['worst_case_loss_by_game_cc']}\nlive ="
            f"{live.worst_case_loss_by_game_cc}"
        )
        assert composed["gross_settlement_notional_by_game_cc"] == (
            live.gross_settlement_notional_by_game_cc
        ), f"case {case}: notional-axis port mismatch"
        assert composed["gross_notional_cc"] == live.gross_notional_cc, (
            f"case {case}: gross port mismatch"
        )
        for ticker, d in live.delta_by_market.items():
            assert abs(composed["delta_by_market"][ticker] - d) < 1e-6, (
                f"case {case}: delta_by_market[{ticker}] port mismatch"
            )
        for game, d in live.delta_by_game.items():
            assert abs(composed["delta_by_game"][game] - d) < 1e-6, (
                f"case {case}: delta_by_game[{game}] port mismatch"
            )
        for game, d in live.directional_by_game_cc.items():
            assert abs(composed["directional_by_game_cc"][game] - d) <= 2, (
                f"case {case}: directional[{game}] port mismatch "
                f"(proto {composed['directional_by_game_cc'][game]} vs live {d})"
            )
        stats["cases"] += 1
    return stats


def fuzz_pre_gate_lemma_armed(n: int, seed: int) -> collections.Counter:
    """F1 lemma with the haircut ACTIVE at both ends: an ENFORCED allowlisted
    candidate-free breach (armed check) persists in the armed with-candidate
    check — the pre-gate stays zero-false-skip under the haircut."""
    from combomaker.risk.limits import monotone_pre_quote_breaches

    rng = random.Random(seed + 4)
    stats: collections.Counter = collections.Counter()
    for case in range(n):
        limits = RiskLimits(
            max_open_quotes=rng.randint(1, 8),
            max_event_worst_case_loss_dollars=rng.choice([2.0, 10.0, 1_000.0]),
            max_gross_notional_dollars=rng.choice([5.0, 50.0, 5_000.0]),
            game_loss_frac=Fraction(rng.choice([1, 8, 50]), 100),
            slate_loss_frac=Fraction(rng.choice([1, 8, 50]), 100),
            directional_frac=Fraction(rng.choice([1, 10, 50]), 100),
            absolute_notional_multiple=rng.choice([1, 3]),
            caps_shadow_mode=rng.random() < 0.1,
            resting_quote_weight=rand_weight(rng),
            resting_floor_count=rng.choice([1, 3]),
        )
        checker = LimitChecker(limits)
        book = book_with(
            rand_positions(rng), [rand_quote(rng, i) for i in range(rng.randint(0, 8))]
        )
        bankroll = rng.choice([None, 50_000, 500_000, 5_000_000])
        pnl = DailyPnl(realized_cc=rng.choice([0, 0, -100_000]))
        cand_quote = rand_quote(rng, 77)
        candidates = cand_quote.hypothetical_positions(CONVENTIONS)

        free = checker.check(
            book, full_marginals, pnl,
            adding_quote=True, risk_bankroll_cc=bankroll,
            apply_resting_haircut=True,
        )
        gate = monotone_pre_quote_breaches(free)
        stats["cases"] += 1
        if not gate:
            continue
        stats["gate_fired"] += 1
        withc = checker.check(
            book, full_marginals, pnl,
            candidate_positions=candidates, adding_quote=True,
            risk_bankroll_cc=bankroll, apply_resting_haircut=True,
        )
        enforced = [b for b in withc if not b.shadow]
        assert enforced, f"case {case}: armed gate fired but armed full check PASSED"
        reasons = {b.reason for b in enforced}
        for b in gate:
            assert b.reason in reasons, (
                f"case {case}: armed gate reason {b.reason} vanished with candidate"
            )
        stats["gate_reasons_persisted"] += len(gate)
    return stats


# ---------------------------------------------------------------------------
# Part E — tape replay (READ-ONLY): flow unlock estimate at weight 0.40
# ---------------------------------------------------------------------------
_GAME_LOSS_RE = re.compile(r"game (\S+) loss (\d+)cc > \S+ bankroll = (\d+)cc")


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


def tape_replay(since: str, until: str, weight: Fraction) -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    try:
        cur = con.cursor()
        d0 = first_id_at_or_after(cur, "decisions", "at", since)
        target = str(ReasonCode.SKIP_GAME_LOSS_CAP)
        kinds: collections.Counter = collections.Counter()
        no_quote = 0
        gl_declines = 0
        parsed = 0
        unlocked = 0
        ratios: list[float] = []
        cur.execute(
            "SELECT kind, reasons_json, context_json FROM decisions "
            "WHERE id>=? AND at<=?",
            (d0, until),
        )
        for kind, rj, cj in cur:
            kinds[kind] += 1
            if kind != "no_quote":
                continue
            no_quote += 1
            reasons = set(json.loads(rj))
            if target not in reasons:
                continue
            gl_declines += 1
            ctx = json.loads(cj)
            details = ctx.get("details") or []
            worst_ratio = None
            row_unlocked = None
            for detail in details:
                m = _GAME_LOSS_RE.search(str(detail))
                if not m:
                    continue
                loss_cc, thr_cc = int(m.group(2)), int(m.group(3))
                if thr_cc <= 0:
                    continue
                ratio = loss_cc / thr_cc
                worst_ratio = max(worst_ratio or 0.0, ratio)
                # ALL-RESTING assumption (base ~ 0, floor non-binding): the
                # haircut value is ceil(w*loss); unlocked iff <= thr. The tape
                # does not record the committed/resting decomposition, so this
                # is the OPTIMISTIC bound; the burst floor can only reduce it.
                w_loss = -(-(weight.numerator * loss_cc) // weight.denominator)
                ok = w_loss <= thr_cc
                row_unlocked = ok if row_unlocked is None else (row_unlocked and ok)
            if worst_ratio is not None:
                parsed += 1
                ratios.append(worst_ratio)
                if row_unlocked:
                    unlocked += 1
        print(f"\n== part E: tape replay {since} .. {until} (READ-ONLY) ==")
        print(f"decision kinds: {dict(kinds)}")
        print(
            f"no_quote rows: {no_quote}; carrying {target}: {gl_declines} "
            f"({100 * gl_declines / max(1, no_quote):.1f}% of no-quotes)"
        )
        if ratios:
            ratios.sort()

            def pct(p: float) -> float:
                return ratios[min(len(ratios) - 1, int(p * len(ratios)))]

            print(
                f"parsed breach details: {parsed}; loss/threshold ratio "
                f"p25={pct(0.25):.2f} p50={pct(0.50):.2f} p75={pct(0.75):.2f} "
                f"p95={pct(0.95):.2f} max={ratios[-1]:.2f}"
            )
            print(
                f"ESTIMATED unlock at weight {weight}: {unlocked}/{parsed} "
                f"({100 * unlocked / max(1, parsed):.1f}%) of game-loss declines "
                f"would clear (ALL-RESTING assumption: base=0, floor non-binding "
                f"-- optimistic bound; the top-{3} burst floor and any committed "
                f"base can only lower it)"
            )
    finally:
        con.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--since", default="2026-07-16T17:30")
    ap.add_argument("--until", default="2026-07-17T23:59")
    ap.add_argument("--skip-tape", action="store_true")
    args = ap.parse_args()

    print(f"== part A: monotonicity fuzz (n={args.n}, seed={args.seed}) ==")
    stats = fuzz_monotonicity(args.n, args.seed)
    print(f"OK: {dict(stats)} — adding a resting quote never decreased any bucket")

    nb = max(500, args.n // 3)
    print(f"\n== part B: weight-1 parity vs live snapshot (n={nb}) ==")
    stats = fuzz_weight1_parity(nb, args.seed)
    print(f"OK: {dict(stats)} — w=1 composition == today's mass snapshot")

    print(f"\n== part C: burst floor (n={nb}) ==")
    stats = fuzz_floor(nb, args.seed)
    print(f"OK: {dict(stats)} — composed >= live 100% fold of the K largest quotes")

    if port_available():
        print(f"\n== part D1: port parity (n={nb}) ==")
        stats = fuzz_port_parity(nb, args.seed)
        print(f"OK: {dict(stats)} — live snapshot(weight) == prototype composition")
        print(f"\n== part D2: F1 pre-gate lemma, haircut ARMED (n={args.n}) ==")
        stats = fuzz_pre_gate_lemma_armed(args.n, args.seed)
        assert stats["gate_fired"] > 0, "lemma fuzz never fired the gate"
        print(
            f"OK: {dict(stats)} — every armed candidate-free allowlisted breach "
            f"persisted under the candidate (0 violations)"
        )
    else:
        print("\n== part D: port not present yet (skipped) ==")

    if not args.skip_tape:
        tape_replay(args.since, args.until, Fraction(2, 5))


if __name__ == "__main__":
    main()
