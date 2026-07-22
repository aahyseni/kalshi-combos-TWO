"""New-family (OUTS/RBI/SB) flow-bound accounting — the honest law-#4 census.

Reuses the LIVE pricer (_build_pricer from mlb_backtest) and the shipped cache.
No re-gathering. Reports exactly what IS vs ISN'T validatable now.
"""
from __future__ import annotations

import pickle
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from combomaker.pricing.legtypes import LegType, classify_leg  # noqa: E402
from tools.backtests.mlb_backtest import _build_pricer  # noqa: E402

NEWFAM = {LegType.PLAYER_OUTS, LegType.PLAYER_RBI, LegType.PLAYER_SB}
NEWFAM_NAMES = {LegType.PLAYER_OUTS: "outs", LegType.PLAYER_RBI: "rbi", LegType.PLAYER_SB: "sb"}

OUT = Path("data/backtests/mlb_fresh")


def game_code(ticker: str) -> str:
    """Second hyphen-segment = game/event code (e.g. 26JUL161910NYMPHI)."""
    parts = ticker.split("-")
    return parts[1] if len(parts) > 1 else ticker


def main() -> None:
    inputs = pickle.load(open(OUT / "inputs.pkl", "rb"))
    outcomes = pickle.load(open(OUT / "outcomes.pkl", "rb"))
    fairs = pickle.load(open(OUT / "fairs.pkl", "rb"))
    printed = None
    pt = OUT / "printed_tickers.json"
    if pt.exists():
        import json
        printed = set(json.load(open(pt)))
    print(f"inputs={len(inputs)} outcomes={len(outcomes)} fairs={len(fairs)} "
          f"printed={len(printed) if printed else None}")

    # ---- 1. census: every combo carrying an outs/rbi/sb leg ----
    carriers = {}  # mt -> dict(legs, sides, types, newfam_types, is_same_game_newpair)
    for mt, rec in inputs.items():
        legs = rec["legs"]
        sides = rec["sides"]
        types = [classify_leg(t) for t in legs]
        nf = [t for t in types if t in NEWFAM]
        if not nf:
            continue
        # same-game new-family pair: is there ANOTHER leg in the same game as a new-family leg?
        gcs = [game_code(t) for t in legs]
        newfam_idx = [i for i, t in enumerate(types) if t in NEWFAM]
        same_game_partner = False
        for i in newfam_idx:
            for j in range(len(legs)):
                if j != i and gcs[j] == gcs[i]:
                    same_game_partner = True
        carriers[mt] = {
            "legs": legs, "sides": sides, "types": types,
            "newfam": [NEWFAM_NAMES[t] for t in nf],
            "newfam_types": nf,
            "same_game_partner": same_game_partner,
            "n_legs": len(legs),
            "gcs": gcs,
        }

    n_total = len(carriers)
    fam_counter = Counter()
    for c in carriers.values():
        for f in set(c["newfam"]):
            fam_counter[f] += 1
    print("\n===== 1. CENSUS: combos carrying an OUTS/RBI/SB leg =====")
    print(f"total carrier combos: {n_total}")
    print(f"  by family (combos containing >=1 leg of that family): {dict(fam_counter)}")
    # combos where a new-family leg shares its game with another leg (copula rho can engage)
    n_same_game = sum(1 for c in carriers.values() if c["same_game_partner"])
    print(f"  carriers with a same-GAME partner leg (rho can engage): {n_same_game}")
    print(f"  carriers where new-fam legs are all cross-game (independent): {n_total - n_same_game}")

    # ---- 2. pregame-priceable / printed / cleared / settled ----
    n_printed = n_cleared = n_settled = n_priced_fair = 0
    n_printed_and_cleared = 0
    for mt, c in carriers.items():
        if printed is not None and mt in printed:
            n_printed += 1
        o = outcomes.get(mt)
        if o:
            if o.get("clearings"):
                n_cleared += 1
                if printed is not None and mt in printed:
                    n_printed_and_cleared += 1
            if o.get("settle_yes") is not None:
                n_settled += 1
        f = fairs.get(mt)
        if f and f.get("fair_promoted") is not None:
            n_priced_fair += 1

    print("\n===== 2. FLOW BOUND: priceable / cleared / settled =====")
    print(f"  printed (appeared in tape as an actual RFQ ticker): {n_printed}")
    print(f"  have a stored promoted fair (fair_promoted != None): {n_priced_fair}")
    print(f"  cleared (>=1 real clearing print on the combo):      {n_cleared}")
    print(f"    of which also printed:                             {n_printed_and_cleared}")
    print(f"  settled (settle_yes known):                          {n_settled}")

    # list the cleared ones and settled ones explicitly (small N -> enumerate)
    cleared_list = []
    settled_list = []
    for mt, c in carriers.items():
        o = outcomes.get(mt)
        if not o:
            continue
        if o.get("clearings"):
            cleared_list.append((mt, c, o))
        if o.get("settle_yes") is not None:
            settled_list.append((mt, c, o))
    print(f"\n  CLEARED carriers ({len(cleared_list)}):")
    for mt, c, o in cleared_list:
        print(f"    {mt}")
        print(f"      fams={[classify_leg(t).value for t in c['legs']]} sides={c['sides']} "
              f"same_game={c['same_game_partner']}")
        print(f"      clearings={o['clearings']} settle_yes={o.get('settle_yes')}")
    print(f"\n  SETTLED carriers ({len(settled_list)}):")
    for mt, c, o in settled_list[:40]:
        print(f"    {mt} settle_yes={o.get('settle_yes')} cleared={bool(o.get('clearings'))} "
              f"same_game={c['same_game_partner']} fams={[classify_leg(t).value for t in c['legs']]}")
    if len(settled_list) > 40:
        print(f"    ... +{len(settled_list)-40} more")

    # ---- 3. price same-game new-family combos through the LIVE path ----
    print("\n===== 3. LIVE PRICING of same-game new-family carriers =====")
    price_combo, pair_records, _ = _build_pricer()

    # pick same-game carriers; for each, use the LAST marginal snapshot (freshest pregame)
    same_game_carriers = [(mt, c) for mt, c in carriers.items() if c["same_game_partner"]]
    print(f"same-game new-family carrier combos to price: {len(same_game_carriers)}")

    # bucket by the sorted set of families to characterize the shapes
    shape_counter = Counter()
    for mt, c in same_game_carriers:
        fams = tuple(sorted(classify_leg(t).value for t in c["legs"]))
        shape_counter[fams] += 1
    print("\n  distinct family-shapes among same-game carriers (top 25):")
    for fams, n in shape_counter.most_common(25):
        print(f"    x{n:4d}  {'+'.join(fams)}")

    # Now price a representative sample AND report which NEW rho engaged.
    # We report every DISTINCT (shape, new-family-pair-key) once with a worked fair.
    new_rho_engaged = Counter()   # pair_key that touches a new family -> count of combos
    priced_ok = priced_none = 0
    worked_examples = []  # (mt, fair, path, new_pair_keys, marginals)
    seen_shape = set()
    for mt, c in same_game_carriers:
        rec = inputs[mt]
        snaps = rec["snaps"]
        if not snaps:
            continue
        # freshest pregame marginal snapshot = last snap's prob vector
        marginals = snaps[-1][1]
        legs, sides = c["legs"], c["sides"]
        # pair records: which rho engaged, and specifically the new-family pairs
        try:
            pairs, n_cross = pair_records(legs, sides, marginals)
        except Exception as exc:  # noqa: BLE001
            pairs, n_cross = [], 0
            print(f"    pair_records error on {mt}: {exc}")
        types = c["types"]
        newfam_pair_keys = []
        for p in pairs:
            i, j = p["i"], p["j"]
            if types[i] in NEWFAM or types[j] in NEWFAM:
                newfam_pair_keys.append((p["key"], p.get("rho_promoted"), p.get("cat_promoted")))
                new_rho_engaged[p["key"]] += 1
        try:
            fair, path = price_combo(legs, sides, marginals, "promoted")
        except Exception as exc:  # noqa: BLE001
            fair, path = None, f"error: {exc}"
        if fair is None:
            priced_none += 1
        else:
            priced_ok += 1
        shape = tuple(sorted(classify_leg(t).value for t in legs))
        if shape not in seen_shape and newfam_pair_keys:
            seen_shape.add(shape)
            o = outcomes.get(mt, {})
            worked_examples.append({
                "mt": mt, "fair": fair, "path": path,
                "newfam_pairs": newfam_pair_keys,
                "marginals": [round(x, 4) for x in marginals],
                "legs": legs, "sides": sides,
                "cleared": o.get("clearings"), "settle_yes": o.get("settle_yes"),
                "shape": "+".join(shape),
            })

    print(f"\n  priced through live path: ok={priced_ok} none/declined={priced_none}")
    print("\n  NEW-FAMILY rho keys that engaged (pair_key -> #combos):")
    for k, n in new_rho_engaged.most_common():
        print(f"    x{n:5d}  {k}")

    print(f"\n  WORKED EXAMPLES (one per distinct new-family shape, {len(worked_examples)} total):")
    for w in worked_examples:
        print(f"\n    combo: {w['mt']}")
        print(f"      shape: {w['shape']}")
        print(f"      legs/sides: {list(zip([classify_leg(t).value for t in w['legs']], w['sides']))}")
        print(f"      marginals(selected-side snap): {w['marginals']}")
        print(f"      FAIR(promoted) = {w['fair']}  via {w['path']}")
        print(f"      new-family pairs engaged (key, rho_promoted, cat): {w['newfam_pairs']}")
        print(f"      cleared={w['cleared']}  settle_yes={w['settle_yes']}")


if __name__ == "__main__":
    main()
