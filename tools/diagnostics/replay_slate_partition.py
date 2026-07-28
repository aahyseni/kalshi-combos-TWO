"""TAPE REPLAY of the SLATE axis alone — how much flow the partition releases.

Rule 8 (testing isolation): imports and calls the LIVE
``risk.exposure.partitioned_worst_case_cc`` — never a reimplementation — and
edits nothing. The store is opened READ-ONLY.

It reuses the loaders in ``replay_admission_fixes`` (one source of truth for the
book/ME/slate reconstruction) and reports the SLATE half in isolation, so the
number is not entangled with the concurrently-rebuilt entity axis:

  * how many ``skip_slate_cap`` refusals the ARMED partition admits,
  * the premium those refusals were carrying,
  * the projected send rate if the SLATE axis alone were armed (an UPPER BOUND:
    a decision that stops being refused here still has to clear the stages that
    never ran — candidate gate, EV, write budget).

FIDELITY LIMIT, stated up front: resting quotes at decision time are not
reconstructable from the store, so both the naive and the corrected number are
recomputed on the COMMITTED-BOOK + CANDIDATE basis. Their RATIO is therefore
exact; the logged naive number is printed beside them so the resting gap shows.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for _p in (str(REPO), str(REPO / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from combomaker.risk.exposure import LegRef, partitioned_worst_case_cc  # noqa: E402
from tools.diagnostics.replay_admission_fixes import (  # noqa: E402
    DB,
    RE_ENTITY,
    RE_SLATE,
    load_book,
    slates_of_legs,
    unit_of,
)


def main(window_ids: int = 400_000) -> None:
    positions, me = load_book()
    book_units = [
        (unit_of(p.legs, p.max_loss_cc, False), slates_of_legs(p.legs))
        for p in positions
    ]
    pre_key: dict[str, int] = {}
    for p in positions:
        from combomaker.risk.exposure import leg_entity_key

        for k in {leg_entity_key(x) for x in p.legs}:
            pre_key[k] = pre_key.get(k, 0) + p.max_loss_cc

    def is_me(event: str) -> bool | None:
        return me.get(event)

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    max_id = con.execute("select max(id) from decisions").fetchone()[0]
    rows = con.execute(
        "select d.kind, d.reasons_json, d.context_json, r.legs_json "
        "from decisions d left join rfqs r on r.rfq_id = d.rfq_id "
        "where d.id > ?",
        (max_id - window_ids,),
    ).fetchall()
    con.close()

    n_total = n_sent = 0
    slate_ref = slate_admit = 0
    slate_prem = released_prem = 0
    naive_sum = part_sum = pairs = 0
    clean_today = clean_slate_only = 0
    slate_only_blocker = 0

    for kind, reasons_json, ctx_json, legs_json in rows:
        n_total += 1
        reasons = json.loads(reasons_json)
        if kind == "quote_sent" or not reasons:
            n_sent += 1
            continue
        details = json.loads(ctx_json).get("details", []) or []
        slates: list[tuple[str, int, int]] = []
        cand_loss = 0
        for d in details:
            m = RE_SLATE.match(d)
            if m:
                slates.append((m.group(1), int(m.group(2)), int(m.group(4))))
                continue
            m = RE_ENTITY.match(d)
            if m:
                cand_loss = max(cand_loss, int(m.group(2)) - pre_key.get(m.group(1), 0))
        if not slates:
            continue
        legs: tuple[LegRef, ...] = ()
        if legs_json:
            legs = tuple(
                LegRef(x["market_ticker"], x.get("event_ticker"), x["side"])
                for x in json.loads(legs_json)
            )
        slate_ref += 1
        slate_prem += cand_loss
        cand_unit = unit_of(legs, cand_loss, False) if legs and cand_loss else None
        cand_slates = slates_of_legs(legs)
        ok = True
        for slate, _naive_logged, thr in slates:
            scoped = [u for u, sl in book_units if slate in sl]
            if cand_unit is not None and (slate in cand_slates or not cand_slates):
                scoped = [*scoped, cand_unit]
            if not scoped:
                continue
            per_game: dict[str, int] = {}
            for u in scoped:
                for g, _gl in u.legs_by_game:
                    per_game[g] = per_game.get(g, 0) + u.loss_cc
            naive_sum += sum(per_game.values())
            part = partitioned_worst_case_cc(scoped, is_me)
            part_sum += part
            pairs += 1
            if part > thr:
                ok = False
        if ok:
            slate_admit += 1
            released_prem += cand_loss
        rest = [r for r in reasons if not (r == "skip_slate_cap" and ok)]
        if not reasons:
            clean_today += 1
        if not rest:
            clean_slate_only += 1
        elif ok and rest == [r for r in reasons if r != "skip_slate_cap"]:
            slate_only_blocker += 0

    print(
        f"WINDOW decisions={n_total:,}  sent={n_sent:,}  "
        f"send_rate={n_sent / max(1, n_total):.3%}"
    )
    print(
        f"SLATE  refusals={slate_ref:,}  ADMITTED by the partition="
        f"{slate_admit:,} ({slate_admit / max(1, slate_ref):.1%})"
    )
    print(
        f"  premium carried by slate-refused decisions="
        f"${slate_prem / 10_000:,.2f}   released=${released_prem / 10_000:,.2f}"
    )
    if pairs:
        print(
            f"  per-refusal mean: naive sum-per-game "
            f"${naive_sum / pairs / 10_000:,.2f}  vs  once-counted joint worst "
            f"case ${part_sum / pairs / 10_000:,.2f}   "
            f"ratio={naive_sum / max(1, part_sum):.2f}x"
        )
    print(
        f"PROJECTED (SLATE lever alone) newly-clean decisions="
        f"{clean_slate_only:,}  send rate "
        f"{(n_sent + clean_slate_only) / max(1, n_total):.3%}  "
        f"(today {n_sent / max(1, n_total):.3%})"
    )


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 400_000)
