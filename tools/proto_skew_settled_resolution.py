"""PROTOTYPE — fact-resolve settled legs OUT of the skew's concentration input.

THE CONFIRMED DEFECT (two agents, blind, same numbers, 2026-08-01): at boot,
exposure rehydration feeds ALREADY-FINISHED games' positions into the
inventory-skew concentration input as if live. The 7/29 05:50Z boot rehydrated
8 positions + 2 reserved spanning 7 FINISHED 7/28 games -> applied skew stepped
median -21cc to -141cc (70.1% of quotes widened >= 50cc, p10 -669cc), flat all
day, never relaxing (finished games never un-concentrate). Channels measured on
that tape (this tool, part B): the ARMED leg-axis family/entity shares (settled
premium pins the family Herfindahl high for every candidate) + any directional/
util mass on a shared game key.

THE RULE (same semantics as the det-max settled-legs fix, 27de1e0 FIX 2): a leg
with an exchange-confirmed settlement is a FACT, not concentration.

  - a leg whose SELECTED side the exchange graded LOST  => the requires-all
    combo can no longer hit: the position's outcome is DETERMINED — realized
    P&L, ZERO concentration on every axis;
  - a leg whose SELECTED side the exchange graded WON   => that leg is a FACT
    and drops out; the REMAINING live legs keep the position's full loss/
    notional/delta concentration (partial resolution);
  - every leg graded selected-WON                        => the combo is
    DETERMINED (hit) — realized loss, ZERO concentration;
  - an UNRESOLVABLE leg (no graded fact, non-binary value) stays FULLY counted
    (fail-closed — UNKNOWN never buys tighter quotes);
  - a leg-less (reserved-from-exchange-figures) position is untouched: nothing
    is resolvable, it stays fully counted.

Facts come ONLY from the graded-settlement cache (``rfq.lifecycle._settled_fact``
-> ``marketdata.settled.SettledMarginalResolver.resolved``) — NEVER the feed
marginal (a market pinned at 0/100 is not a settlement; the exact trap the
det-max fix documented). Boot and intraday share this ONE resolution path: the
same provider, applied at snapshot build, so the two can never diverge again.

PARTS (hard rule 8 — prototype first, port second, parity third):

  A  property battery on a synthetic book, driving the LIVE ``ExposureBook``:
     settled => zero contribution; unresolved => full; mixed => partial (live
     legs only); dead => nothing; empty-legs => untouched; construction-order
     (boot vs intraday) equivalence.
  B  tape counterfactual: replay every ``inventory_skew_shadow`` event of a
     live tape through the LIVE ``compute_inventory_skew`` twice — RAW book
     (today's behaviour) vs RESOLVED book (the rule above) — book/facts/mids
     reconstructed from the position ledger + the tape's own
     ``settled_marginal_resolved`` stream + the decisions tape's leg mids.
     Reports the applied-skew distributions (logged / replayed-raw /
     replayed-resolved) and the counterfactual delta.
  C  PORT PARITY (run after the port): ``ExposureBook.snapshot(...,
     settled_facts=f)`` must equal this tool's strip-then-snapshot reference
     TO THE CENTI-CENT on every skew-consumed field, over part A's book grid
     and part B's reconstructed books.

Usage:
  .venv/Scripts/python.exe -m tools.proto_skew_settled_resolution --part A
  .venv/Scripts/python.exe -m tools.proto_skew_settled_resolution --part B \
      --tape data/live_20260729_0550.log [--stride 5]
  .venv/Scripts/python.exe -m tools.proto_skew_settled_resolution --part C \
      [--tape data/live_20260729_0550.log --stride 50]
"""

from __future__ import annotations

import argparse
import dataclasses
import inspect
import json
import math
import sqlite3
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.risk.exposure import (
    ExposureBook,
    ExposureSnapshot,
    LegRef,
    OpenPosition,
)
from combomaker.risk.skew import (
    LegAxisProfile,
    SkewLimits,
    SkewParams,
    compute_inventory_skew,
)

# ---------------------------------------------------------------------------
# The resolution rule — REFERENCE implementation. The port moves EXACTLY this
# rule into ``risk/exposure.py`` (``concentration_live_legs`` + the
# ``settled_facts`` kwarg on ``snapshot``); part C pins the two equal to the
# centi-cent on identical inputs.
# ---------------------------------------------------------------------------

SettledFacts = Callable[[str], float | None]


def reference_live_legs(
    legs: tuple[LegRef, ...], settled_facts: SettledFacts
) -> tuple[LegRef, ...] | None:
    """The live (unresolved) legs of a requires-all combo, or None when the
    combo's outcome is exchange-DETERMINED. Keep in sync with
    ``risk.exposure.concentration_live_legs`` (part C asserts parity)."""
    if not legs:
        return legs  # leg-less reserve: nothing resolvable, fully counted
    live: list[LegRef] = []
    for leg in legs:
        fact = settled_facts(leg.market_ticker)
        if fact != 0.0 and fact != 1.0:  # None / non-binary: UNRESOLVED
            live.append(leg)
            continue
        selected = fact if leg.side == "yes" else 1.0 - fact
        if selected == 0.0:
            return None  # selected side LOST: combo can no longer hit
        # selected == 1.0: graded FACT — drops out of concentration
    if not live:
        return None  # every leg graded selected-WON: combo determined (hit)
    return tuple(live) if len(live) != len(legs) else legs


def strip_book(
    positions: Iterable[OpenPosition], settled_facts: SettledFacts
) -> list[OpenPosition]:
    """Strip-then-snapshot reference: the book with every position's settled
    legs resolved out (determined positions dropped)."""
    out: list[OpenPosition] = []
    for pos in positions:
        live = reference_live_legs(pos.legs, settled_facts)
        if live is None:
            continue
        out.append(
            pos if live is pos.legs else dataclasses.replace(pos, legs=live)
        )
    return out


CONVENTIONS = Conventions(
    verified=True,
    source="proto",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)

# The LIVE SkewLimits (ops/quote_app.py builds these from risk config; values
# confirmed against config defaults + prod yaml 2026-08-01).
LIMITS = SkewLimits(
    max_event_delta_contracts=2500.0,
    max_event_worst_case_loss_dollars=1_000.0,
    max_event_gross_notional_dollars=5_000.0,
)
# The LIVE arming state (prod yaml 2026-08-01): skew enabled, leg-axis ARMED,
# pbook/conc disarmed. conc_enabled False here only to skip shadow-only work
# the replay never reads (it cannot change skew_cc while conc_armed is False).
PARAMS = SkewParams(enabled=True, leg_axis_armed=True, conc_enabled=False)

SKEW_FIELDS = (
    # Every ExposureSnapshot field the skew path consumes (compute_inventory_
    # skew + decide_widen_or_decline + _leg_axis_profile_from +
    # _concentration_profile's snap-derived maps).
    "delta_by_game",
    "worst_case_loss_by_game_cc",
    "gross_settlement_notional_by_game_cc",
    "committed_loss_by_family_cc",
    "committed_loss_by_entity_cc",
    "loss_by_entity_cc",
    "dir_entries_by_game",
    "committed_dir_entries_by_game",
)


def snapshot_fields(snap: ExposureSnapshot) -> dict[str, object]:
    return {f: getattr(snap, f) for f in SKEW_FIELDS}


def assert_snap_equal(a: ExposureSnapshot, b: ExposureSnapshot, ctx: str) -> None:
    fa, fb = snapshot_fields(a), snapshot_fields(b)
    for k in SKEW_FIELDS:
        if fa[k] != fb[k]:
            raise AssertionError(f"{ctx}: snapshot field {k} differs:\n  A={fa[k]}\n  B={fb[k]}")


# ---------------------------------------------------------------------------
# Part A — property battery (synthetic book, LIVE ExposureBook)
# ---------------------------------------------------------------------------


def _leg(t: str, ev: str, side: str = "yes") -> LegRef:
    return LegRef(market_ticker=t, event_ticker=ev, side=side)


def _pos(pid: str, legs: Sequence[LegRef], contracts: int = 1500, price: int = 3000) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=f"combo-{pid}",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(contracts),
        entry_price_cc=CentiCents(price),
        legs=tuple(legs),
    )


def _book(positions: Iterable[OpenPosition]) -> ExposureBook:
    book = ExposureBook(CONVENTIONS)
    for p in positions:
        book.add_position(p)
    return book


def _mk_marginals(mids: dict[str, float]) -> Callable[[str], float | None]:
    return lambda t: mids.get(t)


def part_a() -> None:
    g1, g2, g3 = "26JUL281840TEXTB", "26JUL291910ATLNYM", "26JUL292140BOSATH"
    e1, e2, e3 = f"KXMLBGAME-{g1}", f"KXMLBGAME-{g2}", f"KXMLBGAME-{g3}"
    # A: 2 legs, one on a FINISHED game whose selected side LOST -> DEAD.
    a = _pos("A", [_leg(f"KXMLBGAME-{g1}-TEX", e1), _leg(f"KXMLBTOTAL-{g2}-8", e2)])
    # B: 2 legs, finished leg selected side WON -> partial (live leg only).
    b = _pos("B", [_leg(f"KXMLBTOTAL-{g1}-7", e1, "no"), _leg(f"KXMLBGAME-{g2}-ATL", e2)])
    # C: fully live -> untouched.
    c = _pos("C", [_leg(f"KXMLBGAME-{g3}-BOS", e3), _leg(f"KXMLBKS-{g3}-BOSX-5", e3)])
    # D: every leg graded selected-WON -> combo HIT -> determined -> dropped.
    d = _pos("D", [_leg(f"KXMLBGAME-{g1}-TB", e1, "no"), _leg(f"KXMLBHR-{g1}-YES", e1)])
    # E: leg-less reserve (exchange figures only) -> untouched, fully counted.
    e = OpenPosition(
        position_id="E", combo_ticker="combo-E", collection=None, our_side=Side.NO,
        contracts=CentiContracts(1000), entry_price_cc=CentiCents(5000), legs=(),
    )
    facts_map = {
        f"KXMLBGAME-{g1}-TEX": 0.0,   # A leg1 selected yes -> LOST -> A dead
        f"KXMLBTOTAL-{g1}-7": 0.0,    # B leg1 selected no  -> WON  -> drops out
        f"KXMLBGAME-{g1}-TB": 1.0,    # D leg1 selected no... graded YES=1
        f"KXMLBHR-{g1}-YES": 1.0,     # D leg2 selected yes -> WON
    }
    # D leg1: side "no", fact 1.0 -> selected = 0.0 -> D is DETERMINED-NO
    # (cannot hit) — also dropped, exercising the any-leg-lost branch on a
    # multi-graded combo. Rebuild D so BOTH graded legs are selected-WON:
    d = _pos("D", [_leg(f"KXMLBGAME-{g1}-TB", e1, "no"), _leg(f"KXMLBHR-{g1}-YES", e1)])
    facts_map[f"KXMLBGAME-{g1}-TB"] = 0.0   # side no, fact 0 -> selected WON
    facts: SettledFacts = lambda t: facts_map.get(t)

    mids = {
        f"KXMLBTOTAL-{g2}-8": 0.6, f"KXMLBGAME-{g2}-ATL": 0.55,
        f"KXMLBGAME-{g3}-BOS": 0.5, f"KXMLBKS-{g3}-BOSX-5": 0.45,
        f"KXMLBGAME-{g1}-TEX": 1.0, f"KXMLBTOTAL-{g1}-7": 0.0,
        f"KXMLBGAME-{g1}-TB": 0.0, f"KXMLBHR-{g1}-YES": 1.0,
    }
    marginals = _mk_marginals(mids)

    raw = _book([a, b, c, d, e]).snapshot(marginals, mass_acceptance=True)
    resolved_book = _book(strip_book([a, b, c, d, e], facts))
    res = resolved_book.snapshot(marginals, mass_acceptance=True)

    # P1: settled legs contribute ZERO — the finished game vanishes entirely.
    assert g1 not in res.delta_by_game, res.delta_by_game
    assert g1 not in res.worst_case_loss_by_game_cc
    assert g1 not in res.gross_settlement_notional_by_game_cc
    assert g1 in raw.worst_case_loss_by_game_cc  # and it WAS there raw
    # P2: dead/determined positions contribute nothing anywhere; B keeps its
    # FULL loss on its live game (partial resolution: live legs only).
    expected_g2 = b.max_loss_cc  # A is dead — only B touches g2 now
    assert res.worst_case_loss_by_game_cc[g2] == expected_g2, (
        res.worst_case_loss_by_game_cc, expected_g2)
    # P3: unresolved position C identical raw vs resolved.
    assert res.worst_case_loss_by_game_cc[g3] == raw.worst_case_loss_by_game_cc[g3]
    assert res.delta_by_game[g3] == raw.delta_by_game[g3]
    # P4: family/entity keys — settled legs' keys carry NO premium.
    assert all(not k.startswith("KXMLBTOTAL:no") for k in res.committed_loss_by_family_cc)
    assert "KXMLBHR:yes" not in res.committed_loss_by_family_cc
    # B's live leg family keeps B's FULL premium.
    assert res.committed_loss_by_family_cc["KXMLBGAME:yes"] == (
        b.max_loss_cc + c.max_loss_cc)
    # P5: leg-less reserve untouched (its premium still counts).
    assert res.gross_notional_cc >= e.max_loss_cc
    # P6: UNKNOWN (facts provider returns garbage) => fully counted.
    garbage: SettledFacts = lambda t: 0.5
    assert strip_book([a, b, c], garbage) == [a, b, c]
    # P7: construction-order equivalence (boot vs intraday): same (positions,
    # facts) state => identical resolved snapshot, regardless of the order the
    # positions were added or when facts landed.
    for order in ([e, d, c, b, a], [c, a, e, b, d]):
        alt = _book(strip_book(order, facts)).snapshot(marginals, mass_acceptance=True)
        assert_snap_equal(res, alt, f"order {[p.position_id for p in order]}")
    # P8: no facts => strip is the identity => snapshots equal.
    none_facts: SettledFacts = lambda t: None
    ident = _book(strip_book([a, b, c, d, e], none_facts)).snapshot(
        marginals, mass_acceptance=True)
    assert_snap_equal(raw, ident, "no-facts identity")
    print("part A: 8/8 properties PASS")


# ---------------------------------------------------------------------------
# Part B — tape counterfactual
# ---------------------------------------------------------------------------


def _ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def load_ledger(db: Path) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "select position_id, opened_at, combo_ticker, our_side, contracts_centi,"
        " entry_price_cc, legs_json, status, reconciled_at from position_ledger"
    ).fetchall()
    con.close()
    out = []
    for pid, opened, ticker, side, ctr, price, legs_json, status, rec in rows:
        legs = tuple(
            LegRef(
                market_ticker=leg["market_ticker"],
                event_ticker=leg.get("event_ticker"),
                side=leg.get("side", "yes"),
            )
            for leg in json.loads(legs_json)
        )
        out.append(
            {
                "opened": _ts(opened),
                "closed": _ts(rec) if (status == "settled" and rec) else math.inf,
                "pos": OpenPosition(
                    position_id=pid,
                    combo_ticker=ticker,
                    collection=None,
                    our_side=Side.NO if side == "no" else Side.YES,
                    contracts=CentiContracts(int(ctr)),
                    entry_price_cc=CentiCents(int(price)),
                    legs=legs,
                ),
            }
        )
    return out


def load_quote_mids(db: Path, t0: float, t1: float) -> list[tuple[float, dict, str]]:
    """(ts, {ticker: prob}, rfq_id) from quote_sent decisions in the window."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    iso0 = datetime.utcfromtimestamp(t0).isoformat()
    iso1 = datetime.utcfromtimestamp(t1).isoformat()
    rows = con.execute(
        "select at, rfq_id, context_json from decisions where kind='quote_sent'"
        " and at >= ? and at <= ?",
        (iso0, iso1 + "Z"),
    ).fetchall()
    con.close()
    out = []
    for at, rfq_id, ctx in rows:
        try:
            d = json.loads(ctx)
            mids = {k: v / 10_000.0 for k, v in d.get("leg_mids_cc", {}).items()}
            out.append((_ts(at), mids, rfq_id, int(d.get("no_bid_cc", 0))))
        except Exception:
            continue
    out.sort(key=lambda r: r[0])
    return out


def load_rfq_legs(db: Path, t0: float, t1: float) -> dict[str, tuple]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    iso0 = datetime.utcfromtimestamp(t0 - 3600).isoformat()
    iso1 = datetime.utcfromtimestamp(t1).isoformat()
    rows = con.execute(
        "select rfq_id, legs_json, contracts_centi, target_cost_cc from rfqs"
        " where seen_at >= ? and seen_at <= ?",
        (iso0, iso1 + "Z"),
    ).fetchall()
    con.close()
    out: dict[str, tuple] = {}
    for rfq_id, legs_json, ctr, target in rows:
        legs = tuple(
            LegRef(
                market_ticker=leg["market_ticker"],
                event_ticker=leg.get("event_ticker"),
                side=leg.get("side", "yes"),
            )
            for leg in json.loads(legs_json)
        )
        out[rfq_id] = (legs, ctr, target)
    return out


def leg_axis_profile_from(snap: ExposureSnapshot) -> LegAxisProfile:
    """Keep in sync with QuoteLifecycle._leg_axis_profile_from (budgets are
    telemetry-only since 2026-07-27 and p_book no longer gates/scales the
    component, so fixed values here cannot move the replayed number)."""
    fam = snap.committed_loss_by_family_cc
    ent = snap.committed_loss_by_entity_cc
    total_fam = float(sum(fam.values()))
    total_ent = float(sum(ent.values()))
    fam_shares = {k: v / total_fam for k, v in fam.items()} if total_fam > 0 else {}
    ent_shares = {k: v / total_ent for k, v in ent.items()} if total_ent > 0 else {}
    return LegAxisProfile(
        shares_by_family=fam_shares,
        total_family_cc=total_fam,
        shares_by_entity=ent_shares,
        total_entity_cc=total_ent,
        hhi_family=math.fsum(s * s for s in fam_shares.values()),
        hhi_entity=math.fsum(s * s for s in ent_shares.values()),
        family_budget_cc=0.0,
        entity_budget_cc=0.0,
        p_book=0.5,
    )


def compose_applied(directional_raw: int, peak_cc: int, family_cc: int, entity_cc: int) -> int:
    """Keep in sync with compute_inventory_skew's pre-Lever-#5 composition
    (directional clamp + peak + armed leg-axis, then the documented overall
    clamp) — used ONLY to fold the tape's logged peak_cc into the replayed
    number (the peak profile is candidate-game-scoped and untouched by this
    fix, so the logged value is correct in both arms)."""
    directional = max(-PARAMS.skew_max_tighten_cc, min(PARAMS.skew_max_widen_cc, directional_raw))
    skew_cc = directional + peak_cc + family_cc + entity_cc
    skew_cc = max(
        -(PARAMS.skew_max_tighten_cc + PARAMS.peak_widen_max_cc),
        min(PARAMS.skew_max_widen_cc + PARAMS.peak_widen_max_cc, skew_cc),
    )
    return -skew_cc


def pct(xs: list, p: float):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[int(p / 100 * (len(xs) - 1))]


def dist_row(name: str, xs: list) -> str:
    if not xs:
        return f"| {name} | - | - | - | - | - | - |"
    return (
        f"| {name} | {len(xs)} | {pct(xs, 10)} | {pct(xs, 25)} | {pct(xs, 50)} |"
        f" {pct(xs, 75)} | {pct(xs, 90)} | {sum(xs) / len(xs):.1f} |"
    )


def part_b(tape: Path, db: Path, stride: int, ported: bool = False) -> None:
    print(f"tape={tape} stride={stride} ported_parity={ported}")
    t_parse0 = time.time()
    boot_ts: float | None = None
    fact_events: list[tuple[float, str, float]] = []
    skew_events: list[dict] = []
    n_seen = 0
    boot_games: set[str] = set()
    for line in open(tape, encoding="utf-8", errors="replace"):
        if '"exposure_rehydrated"' in line and boot_ts is None:
            d = json.loads(line[line.index("{"):])
            boot_ts = _ts(d["ts"])
            boot_games = set(d.get("games", []))
        elif '"settled_marginal_resolved"' in line:
            d = json.loads(line[line.index("{"):])
            fact_events.append((_ts(d["ts"]), d["ticker"], float(d["marginal"])))
        elif '"inventory_skew_shadow"' in line:
            n_seen += 1
            if (n_seen - 1) % stride:
                continue
            d = json.loads(line[line.index("{"):])
            skew_events.append(
                {
                    "ts": _ts(d["ts"]),
                    "rfq_id": d["rfq_id"],
                    "applied": d["applied_cc"],
                    "family": d["family_cc"],
                    "entity": d["entity_cc"],
                    "peak": d["peak_cc"],
                    "dir_raw": d["concentration_cc"] - d["offset_cc"],
                }
            )
    assert boot_ts is not None, "no exposure_rehydrated event on this tape"
    t0, t1 = boot_ts, skew_events[-1]["ts"] if skew_events else boot_ts
    print(
        f"parsed tape in {time.time() - t_parse0:.0f}s: boot={datetime.utcfromtimestamp(boot_ts)}Z"
        f" skew_events={len(skew_events)} (of {n_seen}) facts={len(fact_events)}"
    )

    ledger = load_ledger(db)
    # THE LEDGER IS NOT THE EXCHANGE (14.78% drift measured 2026-07-16): stale
    # forever-open rows on long-settled games would poison the boot book. A
    # position opened BEFORE boot enters the replay book only if it touches a
    # game the live rehydration actually reported (exposure_rehydrated.games)
    # — the tape's own census of what the EXCHANGE returned. Positions opened
    # DURING the window (this run's own fills) are kept unconditionally.
    from combomaker.pricing.grouping import game_key as _gk

    def _games_of(pos: OpenPosition) -> set[str]:
        return {_gk(leg.event_ticker) for leg in pos.legs if leg.event_ticker}

    if boot_games:
        before = len(ledger)
        ledger = [
            r
            for r in ledger
            if r["opened"] > boot_ts
            or not r["pos"].legs  # leg-less reserves: unknowable games, keep
            or r["pos"].position_id.startswith(("reserve:", "rehydrate:", "reconcile:"))
            or (_games_of(r["pos"]) & boot_games)
        ]
        print(f"boot-game census filter: {before} -> {len(ledger)} ledger rows")
    # PREWARM the mid map from the hour BEFORE boot (the previous run's sent
    # quotes): live-leg mids exist in the feed within seconds of boot, while
    # this replay would otherwise start blind. Finished games have no pre-boot
    # RFQ flow, so their legs correctly stay mid-less (husk -> graded fact),
    # matching the live provider's feed-first/fact-second order.
    prewarm = load_quote_mids(db, t0 - 3600, t0)
    mid_stream = load_quote_mids(db, t0, t1 + 5)
    rfq_legs = load_rfq_legs(db, t0, t1 + 5)
    print(
        f"ledger positions={len(ledger)} quote_sent rows={len(mid_stream)}"
        f" (+{len(prewarm)} prewarm) rfqs={len(rfq_legs)}"
    )

    cur_mids: dict[str, float] = {}
    for _t, mids, _r, _nb in prewarm:
        cur_mids.update(mids)
    # no_bid per rfq for target-cost sizing: indexed over the WHOLE window up
    # front — the quote_sent row lands milliseconds AFTER its skew event, so a
    # stream-ordered lookup would always miss its own quote.
    no_bid_by_rfq: dict[str, int] = {}
    for _t, _m, rid, nb in mid_stream:
        if rid not in no_bid_by_rfq and nb > 0:
            no_bid_by_rfq[rid] = nb
    facts_now: dict[str, float] = {}

    def facts(t: str) -> float | None:
        return facts_now.get(t)

    def marginal(t: str) -> float | None:
        # feed-first, graded-fact second — the live provider's order.
        p = cur_mids.get(t)
        if p is not None:
            return p
        return facts_now.get(t)

    mi = 0
    fi = 0
    snap_key: tuple | None = None
    cache: dict[str, object] = {}
    out_logged: list[int] = []
    out_raw: list[int] = []
    out_res: list[int] = []
    deltas_cc: list[int] = []
    fam_match = 0
    fam_total = 0
    skipped = 0
    parity_fail = 0
    t_run = time.time()
    for ev in skew_events:
        t = ev["ts"]
        while fi < len(fact_events) and fact_events[fi][0] <= t:
            facts_now[fact_events[fi][1]] = fact_events[fi][2]
            fi += 1
        while mi < len(mid_stream) and mid_stream[mi][0] <= t:
            cur_mids.update(mid_stream[mi][1])
            mi += 1
        rec = rfq_legs.get(ev["rfq_id"])
        if rec is None:
            skipped += 1
            continue
        legs, ctr, target = rec
        no_bid = no_bid_by_rfq.get(ev["rfq_id"], 0)
        if ctr is not None:
            qty = int(ctr)
        elif target is not None and 0 < no_bid < 9_900:
            qty = int(round(target / (1.0 - no_bid / 10_000.0)))  # award sizing
        else:
            skipped += 1
            continue
        key = (
            sum(1 for r in ledger if r["opened"] <= t < r["closed"]),
            fi,
            int(t // 60),
        )
        if key != snap_key:
            active = [r["pos"] for r in ledger if r["opened"] <= t < r["closed"]]
            if snap_key is None:
                fam0: dict[str, int] = defaultdict(int)
                for p in active:
                    seen0: set[str] = set()
                    for lg in p.legs:
                        fk0 = f"{lg.market_ticker.split('-', 1)[0]}:{lg.side}"
                        if fk0 not in seen0:
                            fam0[fk0] += p.max_loss_cc
                            seen0.add(fk0)
                tot0 = sum(fam0.values()) or 1
                print(f"  first book: {len(active)} positions; family shares:")
                for k, v in sorted(fam0.items(), key=lambda kv: -kv[1]):
                    print(f"    {k:24s} {v/10000:9.2f}$  {100*v/tot0:5.1f}%")
            raw_book = _book(active)
            res_book = _book(strip_book(active, facts))
            cache = {
                "raw_snap": raw_book.snapshot(marginal, mass_acceptance=True),
                "res_snap": res_book.snapshot(marginal, mass_acceptance=True),
            }
            cache["raw_prof"] = leg_axis_profile_from(cache["raw_snap"])
            cache["res_prof"] = leg_axis_profile_from(cache["res_snap"])
            if ported:
                sig = inspect.signature(ExposureBook.snapshot)
                assert "settled_facts" in sig.parameters, "port not present"
                ported_snap = raw_book.snapshot(
                    marginal, mass_acceptance=True, settled_facts=facts
                )
                try:
                    assert_snap_equal(ported_snap, cache["res_snap"], f"t={t}")
                except AssertionError as exc:
                    parity_fail += 1
                    if parity_fail <= 3:
                        print("PARITY FAIL:", exc)
            snap_key = key
        candidate = OpenPosition(
            position_id=f"skew:{ev['rfq_id']}",
            combo_ticker="replay",
            collection=None,
            our_side=Side.NO,
            contracts=CentiContracts(qty),
            entry_price_cc=CentiCents(no_bid if no_bid > 0 else 5000),
            legs=legs,
        )
        arms = {}
        for arm in ("raw", "res"):
            snap = cache[f"{arm}_snap"]
            skew = compute_inventory_skew(
                candidate,
                snap,
                marginal,
                CONVENTIONS,
                LIMITS,
                PARAMS,
                dir_entries_by_game=snap.dir_entries_by_game,
                committed_dir_entries_by_game=snap.committed_dir_entries_by_game,
                leg_axis_profile=cache[f"{arm}_prof"],
            )
            arms[arm] = compose_applied(
                skew.concentration_cc - skew.offset_cc,
                ev["peak"],
                skew.family_cc,
                skew.entity_cc,
            )
            if arm == "raw":
                fam_total += 1
                if skew.family_cc == ev["family"]:
                    fam_match += 1
                elif fam_total - fam_match <= 3:
                    print(
                        f"  fam mismatch @{datetime.utcfromtimestamp(t)}Z"
                        f" replayed={skew.family_cc} logged={ev['family']}"
                        f" rows={[r for r in skew.leg_axis_rows if not r[0].split(':')[1][0].isdigit()][:4]}"
                    )
        out_logged.append(ev["applied"])
        out_raw.append(arms["raw"])
        out_res.append(arms["res"])
        deltas_cc.append(arms["res"] - arms["raw"])
    print(f"replayed {len(out_raw)} events in {time.time() - t_run:.0f}s; skipped {skipped} (no rfq/qty record)")
    print(f"validation: replayed family_cc == logged family_cc on {fam_match}/{fam_total} "
          f"({100 * fam_match / max(1, fam_total):.1f}%)")
    print()
    print("| series | n | p10 | p25 | p50 | p75 | p90 | mean |")
    print("|---|---|---|---|---|---|---|---|")
    print(dist_row("logged applied_cc", out_logged))
    print(dist_row("replayed RAW applied_cc", out_raw))
    print(dist_row("replayed RESOLVED applied_cc", out_res))
    print(dist_row("counterfactual delta (res-raw)", deltas_cc))
    wid_raw = sum(1 for x in out_raw if x <= -50)
    wid_res = sum(1 for x in out_res if x <= -50)
    n = max(1, len(out_raw))
    print(f"\nwidened >=50cc: raw {100 * wid_raw / n:.1f}%  resolved {100 * wid_res / n:.1f}%")
    tight = sum(1 for x in deltas_cc if x > 0)
    print(f"counterfactual tightens {100 * tight / n:.1f}% of quotes, median delta {pct(deltas_cc, 50)}cc")
    if ported:
        print(f"\nPORT PARITY over replay book states: {'PASS' if parity_fail == 0 else f'{parity_fail} FAILURES'}")
        if parity_fail:
            raise SystemExit(1)


# ---------------------------------------------------------------------------
# Part C — port parity (run after the port)
# ---------------------------------------------------------------------------


def part_c(tape: Path | None, db: Path, stride: int) -> None:
    sig = inspect.signature(ExposureBook.snapshot)
    assert "settled_facts" in sig.parameters, (
        "part C needs the ported snapshot(settled_facts=...) — run after the port"
    )
    from combomaker.risk.exposure import concentration_live_legs

    # C1: the helper IS the reference rule, leg for leg.
    g1, g2 = "26JUL281840TEXTB", "26JUL291910ATLNYM"
    e1, e2 = f"KXMLBGAME-{g1}", f"KXMLBGAME-{g2}"
    cases: list[tuple[tuple[LegRef, ...], dict[str, float]]] = [
        ((), {}),
        ((_leg("M1", e1),), {}),
        ((_leg("M1", e1),), {"M1": 0.0}),
        ((_leg("M1", e1),), {"M1": 1.0}),
        ((_leg("M1", e1, "no"),), {"M1": 0.0}),
        ((_leg("M1", e1, "no"),), {"M1": 1.0}),
        ((_leg("M1", e1), _leg("M2", e2)), {"M1": 1.0}),
        ((_leg("M1", e1), _leg("M2", e2)), {"M1": 0.0}),
        ((_leg("M1", e1), _leg("M2", e2)), {"M1": 1.0, "M2": 1.0}),
        ((_leg("M1", e1), _leg("M2", e2)), {"M1": 0.5}),
    ]
    for legs, fm in cases:
        f: SettledFacts = lambda t, fm=fm: fm.get(t)
        assert concentration_live_legs(legs, f) == reference_live_legs(legs, f), (legs, fm)
    print("C1: concentration_live_legs == reference rule on the case grid PASS")

    # C2: snapshot(settled_facts=None) is byte-identical to snapshot() on the
    # part-A book (flag-off = today's behaviour).
    import tools.proto_skew_settled_resolution as _self  # noqa

    # rebuild part A's book inline
    g3 = "26JUL292140BOSATH"
    e3 = f"KXMLBGAME-{g3}"
    a = _pos("A", [_leg(f"KXMLBGAME-{g1}-TEX", e1), _leg(f"KXMLBTOTAL-{g2}-8", e2)])
    b = _pos("B", [_leg(f"KXMLBTOTAL-{g1}-7", e1, "no"), _leg(f"KXMLBGAME-{g2}-ATL", e2)])
    c = _pos("C", [_leg(f"KXMLBGAME-{g3}-BOS", e3), _leg(f"KXMLBKS-{g3}-BOSX-5", e3)])
    mids = {
        f"KXMLBTOTAL-{g2}-8": 0.6, f"KXMLBGAME-{g2}-ATL": 0.55,
        f"KXMLBGAME-{g3}-BOS": 0.5, f"KXMLBKS-{g3}-BOSX-5": 0.45,
        f"KXMLBGAME-{g1}-TEX": 1.0, f"KXMLBTOTAL-{g1}-7": 0.0,
    }
    marginals = _mk_marginals(mids)
    book = _book([a, b, c])
    assert_snap_equal(
        book.snapshot(marginals, mass_acceptance=True),
        book.snapshot(marginals, mass_acceptance=True, settled_facts=None),
        "C2 flag-off identity",
    )
    print("C2: snapshot(settled_facts=None) == snapshot() PASS")

    # C3: ported in-snapshot resolution == strip-then-snapshot reference on
    # the synthetic book with facts.
    facts_map = {f"KXMLBGAME-{g1}-TEX": 0.0, f"KXMLBTOTAL-{g1}-7": 0.0}
    facts: SettledFacts = lambda t: facts_map.get(t)
    ported = book.snapshot(marginals, mass_acceptance=True, settled_facts=facts)
    reference = _book(strip_book([a, b, c], facts)).snapshot(marginals, mass_acceptance=True)
    assert_snap_equal(ported, reference, "C3 synthetic parity")
    print("C3: ported snapshot == strip-then-snapshot reference (synthetic) PASS")

    # C4: the same parity over the real reconstructed tape books (full replay
    # with ported=True asserts at every book state).
    if tape is not None:
        part_b(tape, db, stride, ported=True)
        print("C4: tape-book parity PASS (see PORT PARITY line above)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", required=True, choices=["A", "B", "C"])
    ap.add_argument("--tape", type=Path, default=None)
    ap.add_argument("--db", type=Path, default=REPO / "data" / "combomaker-prod-live-wc.sqlite3")
    ap.add_argument("--stride", type=int, default=1)
    args = ap.parse_args()
    if args.part == "A":
        part_a()
    elif args.part == "B":
        assert args.tape is not None, "--tape required for part B"
        part_b(args.tape, args.db, args.stride)
    else:
        part_c(args.tape, args.db, args.stride)


if __name__ == "__main__":
    main()
