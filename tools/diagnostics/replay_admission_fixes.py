"""TAPE REPLAY of FIX 1 (entity ticket-admission) + FIX 2 (slate partition).

Rule 8 (testing isolation): this script IMPORTS and CALLS the live modules
(``risk.entity_admission.certify_entity_admission``,
``risk.exposure.partitioned_worst_case_cc``) — never a reimplementation — and
edits nothing. The store is opened READ-ONLY.

WHAT IT REPLAYS. Every ``no_quote`` decision in a window, with its FULL reason
list and its per-cap detail strings (the store keeps both), plus the RFQ's exact
legs. The committed book is rebuilt from ``position_ledger`` and the ME facts
from ``data/metadata_cache.json``. For each decision it recomputes the reason set
the ARMED caps would have produced and counts how many decisions become CLEAN.

FIDELITY LIMITS, stated up front:
  * RESTING QUOTES at decision time are not reconstructable from the store, so
    the slate numbers computed here are the COMMITTED-BOOK + CANDIDATE
    aggregates. Both the naive and the partitioned number are recomputed on that
    same basis, so their RATIO is exact; the logged naive number is reported
    beside them so the resting-quote gap is visible.
  * "Projected send rate" is an UPPER BOUND: a decision that stops being refused
    by these two caps still has to clear the stages that never ran (candidate
    gate, EV, write budget).
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from fractions import Fraction
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for p in (str(REPO), str(REPO / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from combomaker.core.conventions import Conventions, Side  # noqa: E402
from combomaker.core.money import CentiCents  # noqa: E402
from combomaker.core.quantity import CentiContracts  # noqa: E402
from combomaker.pricing.grouping import game_key  # noqa: E402
from combomaker.risk.entity_admission import (  # noqa: E402
    certify_entity_admission,
    entity_loads,
    entity_tier,
    ticket_concentration,
)
from combomaker.risk.exposure import (  # noqa: E402
    LegRef,
    LossUnit,
    OpenPosition,
    leg_entity_key,
    partitioned_worst_case_cc,
)

ENTITY_FRAC = Fraction(3, 100)   # risk.entity_loss_frac, the RATIFIED wall
DB = REPO / "data" / "combomaker-prod-live-wc.sqlite3"
META = REPO / "data" / "metadata_cache.json"

CONV = Conventions(
    verified=True, source="replay",
    maker_side_on_yes_accept=Side.YES, maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True, maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)

RE_ENTITY = re.compile(
    r"^entity (\S+) ACCUMULATED loss (\d+)cc .*? > (\S+) bankroll = (\d+)cc"
)
RE_COMBO = re.compile(
    r"^combo (\S+) ACCUMULATED loss (\d+)cc .*? > (\S+) bankroll = (\d+)cc"
)
RE_SLATE = re.compile(r"^slate (\S+) loss (\d+)cc > (\S+) bankroll = (\d+)cc")


def _frac(text: str) -> tuple[int, int]:
    if "/" in text:
        n, d = text.split("/")
        return int(n), int(d)
    return int(text), 1


def load_book() -> tuple[list[OpenPosition], dict[str, bool]]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = con.execute(
        "select position_id, combo_ticker, collection_ticker, our_side, "
        "contracts_centi, entry_price_cc, legs_json from position_ledger "
        "where status='open'"
    ).fetchall()
    con.close()
    positions: list[OpenPosition] = []
    for pid, combo, coll, side, contracts, price, legs_json in rows:
        legs = tuple(
            LegRef(x["market_ticker"], x.get("event_ticker"), x["side"])
            for x in json.loads(legs_json)
        )
        positions.append(
            OpenPosition(
                position_id=pid, combo_ticker=combo, collection=coll,
                our_side=Side.NO if side == "no" else Side.YES,
                contracts=CentiContracts(int(contracts)),
                entry_price_cc=CentiCents(int(price)),
                legs=legs,
            )
        )
    meta = json.loads(META.read_text(encoding="utf-8"))
    me = {
        e["event_ticker"]: bool(e.get("mutually_exclusive"))
        for e in meta.get("events", [])
        if e.get("event_ticker")
    }
    return positions, me



_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_DATE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})")


def slate_of_ticker(event_ticker: str | None) -> str | None:
    """The live slate label (US/Eastern game day, ISO) read off the ticker's own
    embedded game date -- ``KXMLBSPREAD-26JUL271845TORWSH`` -> ``2026-07-27``.
    The bot derives the same label from the leg's START TIME, which the store
    does not keep; the ticker date is the same day by construction for every MLB
    series on the tape. None when no date is embedded (pooled UNKNOWN)."""
    if not event_ticker:
        return None
    m = _DATE.search(event_ticker)
    if not m or m.group(2) not in _MONTHS:
        return None
    return f"20{m.group(1)}-{_MONTHS[m.group(2)]:02d}-{int(m.group(3)):02d}"


def slates_of_legs(legs: tuple[LegRef, ...]) -> set[str]:
    out = {slate_of_ticker(x.event_ticker) for x in legs}
    return {s for s in out if s}


def unit_of(legs: tuple[LegRef, ...], loss_cc: int, resting: bool) -> LossUnit:
    by_game: dict[str, list[LegRef]] = {}
    for leg in legs:
        if leg.event_ticker:
            by_game.setdefault(game_key(leg.event_ticker), []).append(leg)
    return LossUnit(
        legs_by_game=tuple((g, tuple(gl)) for g, gl in by_game.items()),
        loss_cc=int(loss_cc), requires_all=True, resting=resting,
    )


def main(window_ids: int = 400_000) -> None:
    positions, me = load_book()
    book_losses = [p.max_loss_cc for p in positions]
    book_state = ticket_concentration(book_losses)
    pre_key: dict[str, int] = {}
    for p in positions:
        for k in {leg_entity_key(x) for x in p.legs}:
            pre_key[k] = pre_key.get(k, 0) + p.max_loss_cc
    print(
        f"BOOK  positions={len(positions)}  premium=${book_state.total_cc/10_000:,.2f}"
        f"  ticket N_eff={book_state.eff_n:.2f}  entity keys={len(pre_key)}"
        f"  attributed=${sum(pre_key.values())/10_000:,.2f}"
        f"  ({sum(pre_key.values())/max(1,book_state.total_cc):.2f}x)"
    )

    def is_me(event: str) -> bool | None:
        return me.get(event)

    # The committed book as ONCE-COUNTED loss events, each tagged with the
    # slate(s) it touches (a ticket spanning two slates is charged in full to
    # BOTH -- once within each).
    book_units = [
        (unit_of(p.legs, p.max_loss_cc, False), slates_of_legs(p.legs))
        for p in positions
    ]

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    max_id = con.execute("select max(id) from decisions").fetchone()[0]
    lo = max_id - window_ids
    rows = con.execute(
        "select d.at, d.kind, d.reasons_json, d.context_json, r.legs_json "
        "from decisions d left join rfqs r on r.rfq_id = d.rfq_id "
        "where d.id > ?",
        (lo,),
    ).fetchall()
    con.close()

    n_total = n_noquote = n_sent = 0
    ent_ref = slate_ref = 0
    ent_admit = slate_admit = 0
    ent_prem = slate_prem = 0
    released_losses: list[int] = []
    released_keys: dict[str, int] = {}
    released_events: list[tuple[int, list[str], int]] = []  # (L, keys, ceiling)
    becomes_clean = 0
    naive_sum = part_sum = 0
    n_slate_pairs = 0
    ent_reason_by_verdict: dict[str, int] = {}
    ceiling_blocked = 0

    for _at, kind, reasons_json, ctx_json, legs_json in rows:
        n_total += 1
        reasons = json.loads(reasons_json)
        if kind == "quote_sent" or not reasons:
            n_sent += 1
            continue
        n_noquote += 1
        details = json.loads(ctx_json).get("details", []) or []
        ents = []
        combo_thr = None
        slates = []
        for d in details:
            m = RE_ENTITY.match(d)
            if m:
                ents.append((m.group(1), int(m.group(2)), int(m.group(4))))
                continue
            m = RE_COMBO.match(d)
            if m:
                combo_thr = int(m.group(4))
                continue
            m = RE_SLATE.match(d)
            if m:
                slates.append((m.group(1), int(m.group(2)), int(m.group(4))))

        legs: tuple[LegRef, ...] = ()
        if legs_json:
            legs = tuple(
                LegRef(x["market_ticker"], x.get("event_ticker"), x["side"])
                for x in json.loads(legs_json)
            )

        # --- candidate premium L, solved from the accumulated-vs-pre state ----
        cand_loss = 0
        for key, acc, _thr in ents:
            cand_loss = max(cand_loss, acc - pre_key.get(key, 0))

        # combo_thr is only printed when the per-combo cap ALSO fired; otherwise
        # derive it from the entity threshold and the live 3% / 5% fractions.
        if combo_thr is None and ents:
            bankroll = ents[0][2] * 100 // 3
            combo_thr = bankroll * 5 // 100

        # ---------------- TIERED ENTITY: would the ARMED cap admit? ----------
        # The candidate's FULL per-key footprint, not just the breached keys:
        # the tiered rule is a NET judgement across ALL the combo's legs, so the
        # replay must feed it every leg the RFQ carried.
        entity_ok = True
        if ents:
            ent_ref += 1
            ent_prem += cand_loss
            bankroll = ents[0][2] * 100 // 3
            cand_keys = sorted({leg_entity_key(x) for x in legs}) or [
                k for k, _a, _t in ents
            ]
            post_by_key = {
                k: pre_key.get(k, 0) + cand_loss for k in cand_keys
            }
            for key, acc, _thr in ents:      # the LIVE accumulated number wins
                post_by_key[key] = acc
            loads = entity_loads(
                candidate_legs_by_position=[(cand_keys, cand_loss)],
                post_by_key=post_by_key,
                bankroll_cc=bankroll,
                decline_frac=ENTITY_FRAC,
            )
            for key, acc, _thr in ents:
                cert = certify_entity_admission(
                    key=key, loads=loads,
                    bankroll_cc=bankroll, ceiling_cc=combo_thr or 0,
                )
                if not (cert.certified and acc <= (combo_thr or 0)):
                    entity_ok = False
                    v = cert.verdict if cert.certified is False else "scale_ceiling"
                    ent_reason_by_verdict[v] = ent_reason_by_verdict.get(v, 0) + 1
                    if v == "over_size_ceiling" or v == "scale_ceiling":
                        ceiling_blocked += 1
                    break
            if entity_ok:
                ent_admit += 1
                released_losses.append(cand_loss)
                released_events.append(
                    (cand_loss, cand_keys, combo_thr or 0)
                )
                for key, acc, _t in ents:
                    released_keys[key] = max(released_keys.get(key, 0), acc)

        # ---------------- FIX 2: would the ARMED slate cap admit? ------------
        slate_ok = True
        if slates:
            slate_ref += 1
            slate_prem += cand_loss
            cand_unit = unit_of(legs, cand_loss, False) if legs else None
            cand_slates = slates_of_legs(legs)
            for slate, _naive_cc, thr in slates:
                # SLATE-SCOPED, both numbers on the same reconstructed basis so
                # the ratio is exact; the logged naive is reported alongside.
                scoped = [u for u, sl in book_units if slate in sl]
                if cand_unit is not None and (
                    slate in cand_slates or not cand_slates
                ):
                    scoped = [*scoped, cand_unit]
                if not scoped:
                    continue
                per_game: dict[str, int] = {}
                for u in scoped:
                    for g, _gl in u.legs_by_game:
                        per_game[g] = per_game.get(g, 0) + u.loss_cc
                naive_recon = sum(per_game.values())
                part = partitioned_worst_case_cc(scoped, is_me)
                naive_sum += naive_recon
                part_sum += part
                n_slate_pairs += 1
                if part > thr:
                    slate_ok = False
            if slate_ok:
                slate_admit += 1

        # ---------------- projected send: any OTHER enforced blocker? --------
        rest = [
            r for r in reasons
            if not (r == "skip_entity_loss_cap" and entity_ok)
            and not (r == "skip_slate_cap" and slate_ok)
        ]
        if not rest:
            becomes_clean += 1

    print(
        f"WINDOW decisions={n_total:,}  sent={n_sent:,}"
        f"  no_quote={n_noquote:,}  send_rate={n_sent/max(1,n_total):.3%}"
    )
    print(
        f"ENTITY refusals={ent_ref:,}  ADMITTED={ent_admit:,}"
        f" ({ent_admit/max(1,ent_ref):.1%})"
        f"  premium refused=${ent_prem/10_000:,.2f}"
        f"  premium released=${sum(released_losses)/10_000:,.2f}"
    )
    print(f"  refusal cause census: {ent_reason_by_verdict}")
    print(
        f"SLATE  refusals={slate_ref:,}  ADMITTED={slate_admit:,}"
        f" ({slate_admit/max(1,slate_ref):.1%})"
        f"  premium refused=${slate_prem/10_000:,.2f}"
    )
    if n_slate_pairs:
        print(
            f"  reconstructed naive sum-per-game=${naive_sum/n_slate_pairs/10_000:,.2f}"
            f"  once-counted joint worst case="
            f"${part_sum/n_slate_pairs/10_000:,.2f}"
            f"  ratio={naive_sum/max(1,part_sum):.2f}x"
        )
    print(
        f"PROJECTED clean decisions={becomes_clean:,}"
        f"  projected send rate="
        f"{(n_sent+becomes_clean)/max(1,n_total):.3%}  (today {n_sent/max(1,n_total):.3%})"
    )
    # --- CONCENTRATION IF THEY FILLED ---------------------------------------
    # A released QUOTE is not a fill: ~12.7% of quotes fill, and every fill
    # re-enters the SERIAL reservation chain where the ARMED caps re-bind
    # against the GROWN book.  So the honest counterfactual is the SERIAL one:
    # walk the released candidates in tape order, re-certify each against the
    # book as it stands, and stop admitting when the armed rule refuses.  That
    # is exactly what the live confirm path would do.
    live_losses = list(book_losses)
    live_key = dict(pre_key)
    accepted = 0
    for loss, keys, ceiling in released_events:
        if loss <= 0 or not ceiling:
            continue
        bankroll = ceiling * 100 // 5
        post_by_key = {k: live_key.get(k, 0) + loss for k in keys}
        loads = entity_loads(
            candidate_legs_by_position=[(keys, loss)],
            post_by_key=post_by_key,
            bankroll_cc=bankroll,
            decline_frac=ENTITY_FRAC,
        )
        wall = ENTITY_FRAC.numerator * bankroll // ENTITY_FRAC.denominator
        acc_ok = True
        for k in keys:
            if post_by_key[k] <= wall:
                continue                     # under the wall: nothing to certify
            c = certify_entity_admission(
                key=k, loads=loads, bankroll_cc=bankroll, ceiling_cc=ceiling
            )
            if not (c.certified and post_by_key[k] <= ceiling):
                acc_ok = False
                break
        if not acc_ok:
            continue
        live_losses.append(loss)
        for k in keys:
            live_key[k] = live_key.get(k, 0) + loss
        accepted += 1
    after = ticket_concentration(live_losses)
    print(
        f"SERIAL FILL SIM  accepted={accepted:,} of {len(released_events):,} released"
        f"  premium ${book_state.total_cc/10_000:,.2f} ->"
        f" ${after.total_cc/10_000:,.2f}"
    )
    print(
        f"  ticket N_eff {book_state.eff_n:.2f} -> {after.eff_n:.2f}"
        f"   peak ticket ${book_state.peak_cc/10_000:,.2f} ->"
        f" ${after.peak_cc/10_000:,.2f}"
        f"   worst ENTITY key ${max(live_key.values(), default=0)/10_000:,.2f}"
        f" (was ${max(pre_key.values(), default=0)/10_000:,.2f})"
    )
    naive_after = ticket_concentration(book_losses + released_losses)
    print(
        f"  [upper bound, every released quote filling at once:"
        f" premium ${naive_after.total_cc/10_000:,.2f},"
        f" N_eff {naive_after.eff_n:.1f},"
        f" peak ticket ${naive_after.peak_cc/10_000:,.2f},"
        f" worst ENTITY key ${max(released_keys.values(), default=0)/10_000:,.2f}]"
    )


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 400_000)
