"""OFFLINE REPRO — 2026-07-19 00:04-00:21 ET "waiver: game 26JUL19ESPARG not
certifiable" declines (live_20260719_{husks,sizingup,autoscale}.log).

Reconstructs the waiver's enumeration inputs for the LIVE overnight book shape
(9 committed positions from the mode=ro DB, incl. cross-game combos carrying
exchange-graded FRAENG facts; the trimmed top-K resting-quote set shaped like
the overnight champ+Messi flow; the declined ARG-champ+Messi candidate) and
runs the repo's OWN waiver machinery (hard rule 8: ``sim.state_worst_case.
state_worst_case_by_game`` + ``sim.structural_book.build_game_plans`` — never a
reimplementation) under the marginal-availability scenarios the feed exposes at
midnight vs daytime.

Also runs the PRE-settled-work ``structural_book.build_game_plans`` (extracted
from commit dad3d91, the parent of a57afc3) on byte-identical inputs to settle
regression-vs-pre-existing.

Read-only: DB opened mode=ro; no live module touched; no config edited.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
from importlib import util as importlib_util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from combomaker.core.conventions import Side, load_conventions  # noqa: E402
from combomaker.core.money import CentiCents  # noqa: E402
from combomaker.core.quantity import CentiContracts  # noqa: E402
from combomaker.ops.config import load_config  # noqa: E402
from combomaker.pricing.legtypes import set_pricing_aliases  # noqa: E402
from combomaker.risk.exposure import LegRef, OpenPosition, OpenQuoteRisk  # noqa: E402
from combomaker.sim.state_worst_case import (  # noqa: E402
    entity_from_position,
    quote_from_open_quote,
    state_worst_case_by_game,
)
from combomaker.sim.structural_book import (  # noqa: E402
    StructuralConfigView,
    build_game_plans,
)

GAME = "26JUL19ESPARG"

# The 9 open combos at the 2026-07-19 04:17:06Z rehydrate (exposure_rehydrated
# positions=9, games FRAENG+ESPARG). Derived from the mode=ro DB fills/rfqs and
# cross-checked against the settled-resolver pending lists in the autoscale log
# (the feed-unreadable COMMITTED legs at 04:17:08Z) — every leg below that is
# ESPARG-family or KXMENWORLDCUP appears in those lists; no ES-champ / 1HTOTAL
# leg does (their combos settled early-NO when FRAENG graded).
OPEN_COMBOS = (
    "KXMVECROSSCATEGORY-S20263AB06A8E27D-39F452DE826",       # AR-champ + Messi
    "KXMVESPORTSMULTIGAMEEXTENDED-S202609506CD2247-39F452DE826",  # AR-champ + Messi
    "KXMVECROSSCATEGORY-S20264C8E1563778-8059CCF10B6",       # BTTS-F+BTTS-E+TOTF4+TOTE3
    "KXMVECROSSCATEGORY-S2026DEADDA0B72A-3F55FA29427",       # BTTS-F + BTTS-E
    "KXMVESPORTSMULTIGAMEEXTENDED-S202624628103551-82176C7F551",  # BTTS-F+BTTS-E+TOTE3
    "KXMVESPORTSMULTIGAMEEXTENDED-S2026B4E1E92A38D-3F55FA29427",  # BTTS-F + BTTS-E
    "KXMVESPORTSMULTIGAMEEXTENDED-S2026CFD2CA10A13-04EA5F03582",  # CORN-E9+Messi+TCORN
    "KXMVESPORTSMULTIGAMEEXTENDED-S2026FCEBC7CDB52-C304AAE1108",  # BTTS-E + GAME-ESP
    "KXMVESPORTSMULTIGAMEEXTENDED-S2026D313ECCECAA-359628F9CBD",  # BTTS F+E, TOTF3, TOTE3
)

# Exchange-graded FRAENG facts (settled_marginal_resolved lines, autoscale log).
SETTLED_FACTS = {
    "KXWCBTTS-26JUL18FRAENG-BTTS": 1.0,
    "KXWCTOTAL-26JUL18FRAENG-3": 1.0,
    "KXWCTOTAL-26JUL18FRAENG-4": 1.0,
}

# Feed-readable marginals at ~04:20Z. The decisions×rfqs join for 04:15-04:25Z
# proves the game-family books WERE readable (quote_sent on combos carrying
# BTTS ×1218, TOTAL-3 ×643, GAME-ESP ×154, champ-ES ×868 …), so the failing
# condition is NOT a missing second team-level target — it is ``invert``
# RAISING on the target set the trimmed universe assembles (swallowed into the
# copula fallback by ``_try_build_game`` ⇒ no plan ⇒ UNCERTIFIED_NO_PLAN).
TEAM_LEVEL = {
    "KXMENWORLDCUP-26-AR": 0.36,
    "KXMENWORLDCUP-26-ES": 0.64,
    "KXWCGAME-26JUL19ESPARG-ESP": 0.40,
    "KXWCBTTS-26JUL19ESPARG-BTTS": 0.52,
    "KXWCTOTAL-26JUL19ESPARG-3": 0.47,
}
# The overnight multi-scorer parlay flow (real tickers from the quoted-combo
# join). Five distinct ARG scorer markets: each PlayerScores marginal implies a
# thinning share q_i (share of the team's goals); five 1+ markets on one team
# sum past _MAX_TEAM_SHARE=0.95 and ``invert`` raises "player shares sum to…".
SCORERS_WIDE = {
    "KXWCGOAL-26JUL19ESPARG-ARGLMESSI10-1": 0.40,
    "KXWCGOAL-26JUL19ESPARG-ARGJALVAR19-1": 0.30,
    "KXWCGOAL-26JUL19ESPARG-ARGLMARTI10-1": 0.28,
    "KXWCGOAL-26JUL19ESPARG-ARGEFERNA8-1": 0.22,
    "KXWCGOAL-26JUL19ESPARG-ARGAMACA10-1": 0.20,
    "KXWCGOAL-26JUL19ESPARG-ESPLYAMAL10-1": 0.38,
    "KXWCGOAL-26JUL19ESPARG-ESPMOYARZ10-1": 0.30,
    "KXWCGOAL-26JUL19ESPARG-ESPDOLMO20-1": 0.25,
}
# The Messi-concentrated kept set (the flow is dominated by Messi combos): few
# distinct scorers -> the share system stays feasible -> the flicker CERTIFIED
# instants (04:15:23Z / 04:24:55Z over_budget lines).
SCORERS_NARROW = {
    "KXWCGOAL-26JUL19ESPARG-ARGLMESSI10-1": 0.40,
    "KXWCGOAL-26JUL19ESPARG-ESPLYAMAL10-1": 0.38,
}


def load_positions() -> list[OpenPosition]:
    con = sqlite3.connect(
        f"file:{REPO / 'data' / 'combomaker-prod-live-wc.sqlite3'}?mode=ro",
        uri=True,
    )
    cur = con.cursor()
    positions: list[OpenPosition] = []
    for combo in OPEN_COMBOS:
        cur.execute(
            "select legs_json from rfqs where market_ticker=? "
            "order by seen_at desc limit 1",
            (combo,),
        )
        legs_raw = json.loads(cur.fetchone()[0])
        cur.execute(
            "select sum(contracts_centi), max(price_cc) from fills "
            "where combo_ticker=?",
            (combo,),
        )
        qty, px = cur.fetchone()
        positions.append(
            OpenPosition(
                position_id=f"pos:{combo}",
                combo_ticker=combo,
                collection=None,
                our_side=Side.NO,
                contracts=CentiContracts(int(qty)),
                entry_price_cc=CentiCents(int(px)),
                legs=tuple(
                    LegRef(lg["market_ticker"], lg["event_ticker"], lg["side"])
                    for lg in legs_raw
                ),
            )
        )
    con.close()
    return positions


def flow_quote(i: int, goals: tuple[str, ...],
               champ: str = "KXMENWORLDCUP-26-AR") -> OpenQuoteRisk:
    """One resting overnight-flow quote (the trim's kept shape): champ leg +
    one-or-more player-goal legs — the sized-up parlay flow that dominates the
    ``worst_hit_loss_cc`` ranking."""
    return OpenQuoteRisk(
        quote_id=f"q{i}",
        rfq_id=f"r{i}",
        combo_ticker=f"KXMVESPORTSMULTIGAMEEXTENDED-SREPRO-{i:03d}",
        collection=None,
        yes_bid_cc=CentiCents(0),
        no_bid_cc=CentiCents(7500),
        contracts=CentiContracts(200_00 + i * 10_00),
        legs=(
            LegRef(champ, "KXMENWORLDCUP-26", "yes"),
            *(
                LegRef(g, "KXWCGOAL-26JUL19ESPARG", "yes")
                for g in goals
            ),
        ),
    )


def old_build_game_plans():
    """``build_game_plans`` from dad3d91 (pre settled-leg work) — the live seam
    extracted verbatim; every import it makes resolves against HEAD, whose only
    sim/structural_book difference IS the 0/1-marginal skip under test."""
    src = subprocess.run(
        ["git", "show", "dad3d91:src/combomaker/sim/structural_book.py"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout
    tmp = Path(tempfile.mkdtemp()) / "structural_book_dad3d91.py"
    tmp.write_text(src, encoding="utf-8")
    spec = importlib_util.spec_from_file_location("structural_book_dad3d91", tmp)
    assert spec is not None and spec.loader is not None
    mod = importlib_util.module_from_spec(spec)
    sys.modules["structural_book_dad3d91"] = mod  # dataclass slots need this
    spec.loader.exec_module(mod)
    return mod.build_game_plans


def diagnose_inversion(marginals: dict[str, float], cfg: StructuralConfigView) -> None:
    """Direct live-seam probe: which ESPARG-group legs become inversion targets
    under these marginals, and what ``invert`` says (the exact condition
    ``_try_build_game`` swallows into a copula fallback)."""
    from combomaker.pricing.dixon_coles import MatchFormat, StructuralError, invert
    from combomaker.pricing.structural_api import (
        parse_leg,
        parse_match,
        resolve_pricing_alias,
    )

    match = parse_match(GAME)
    assert match is not None
    targets = []
    for t, p in sorted(marginals.items()):
        if not 0.0 < p < 1.0:
            continue
        resolved = resolve_pricing_alias(t)
        parts = resolved.split("-")
        if len(parts) < 2 or parts[1] != GAME:
            continue
        spec = parse_leg(t, match, fmt=MatchFormat.KNOCKOUT)
        if isinstance(spec, str):
            continue
        targets.append((spec, p, t))
    for spec, p, t in targets:
        print(f"    target: {t} -> {type(spec).__name__} @ {p}")
    try:
        model = invert(
            [(s, p) for s, p, _ in targets], dc_rho=cfg.dc_rho,
            et_factor=cfg.et_factor, match_format=MatchFormat.KNOCKOUT,
            max_goals=cfg.max_goals, pens_win_a=cfg.pens_win_a,
            half_share=cfg.half_share,
        )
        print(f"    invert: OK ({model.notes[0]})")
    except StructuralError as exc:
        print(f"    invert -> StructuralError: {exc}")


def run_scenario(
    name: str,
    entities,
    quotes,
    marginals: dict[str, float],
    cfg: StructuralConfigView,
) -> None:
    result = state_worst_case_by_game(entities, quotes, marginals, None, cfg)
    got = result.get(GAME)
    print(f"\n=== {name} ===")
    for g in sorted(result):
        r = result[g]
        print(
            f"  {g}: certified={r.certified} n_states={r.n_states} "
            f"worst_cc={r.worst_case_cc} reason={r.uncertified_reason}"
        )
    diagnose_inversion(marginals, cfg)
    assert got is not None, f"{name}: game {GAME} not in result"


def main() -> None:
    config = load_config(
        REPO / "config" / "prod-live-wc.local.yaml", confirm_live=False
    )
    aliases = dict(config.pricing.leg_pricing_aliases)
    print("aliases from live config:", aliases)
    set_pricing_aliases(aliases)
    sc = config.pricing.structural
    cfg = StructuralConfigView(
        dc_rho=sc.dc_rho, et_factor=sc.et_factor, pens_win_a=sc.pens_win_prob,
        half_share=sc.half_share, max_goals=sc.max_goals,
        knockout_series=tuple(sc.knockout_series), enabled=sc.enabled,
        corners_et_loading=sc.corners_et_loading,
    )
    conventions = load_conventions()

    positions = load_positions()
    print(f"committed positions: {len(positions)}")
    esparg_committed = sorted(
        {
            leg.market_ticker
            for p in positions
            for leg in p.legs
            if "ESPARG" in leg.market_ticker or "MENWORLDCUP" in leg.market_ticker
        }
    )
    print("committed ESPARG-group legs:", *esparg_committed, sep="\n  ")

    candidate = OpenPosition(
        position_id="candidate:champ-messi",
        combo_ticker="KXMVESPORTSMULTIGAMEEXTENDED-SREPRO-CAND",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(400_00),
        entry_price_cc=CentiCents(7600),
        legs=(
            LegRef("KXMENWORLDCUP-26-AR", "KXMENWORLDCUP-26", "yes"),
            LegRef(
                "KXWCGOAL-26JUL19ESPARG-ARGLMESSI10-1",
                "KXWCGOAL-26JUL19ESPARG", "yes",
            ),
        ),
    )
    entities = tuple(entity_from_position(p) for p in positions) + (
        entity_from_position(candidate),
    )

    # Trimmed top-12 spanning the WIDE scorer flow (multi-scorer parlays): the
    # plan universe collects 5 distinct ARG + 3 distinct ESP scorer markets.
    wide = tuple(SCORERS_WIDE)
    kept_wide = tuple(
        quote_from_open_quote(
            flow_quote(i, (wide[i % len(wide)], wide[(i + 3) % len(wide)])),
            conventions,
        )
        for i in range(12)
    )
    # The Messi-concentrated kept set (fewest distinct scorers).
    kept_narrow = tuple(
        quote_from_open_quote(
            flow_quote(i, (list(SCORERS_NARROW)[i % 2],)), conventions
        )
        for i in range(12)
    )

    base = {**TEAM_LEVEL, **SETTLED_FACTS}
    fail_marginals = {**base, **SCORERS_WIDE}
    cert_marginals = {**base, **SCORERS_NARROW}
    thin_marginals = {  # a book-flap instant: only the champ-AR + props readable
        "KXMENWORLDCUP-26-AR": TEAM_LEVEL["KXMENWORLDCUP-26-AR"],
        **SCORERS_NARROW, **SETTLED_FACTS,
    }

    # S1 — the failing live shape: kept-12 span many distinct same-team scorer
    # markets; the per-team share system is infeasible -> invert raises ->
    # _try_build_game swallows it -> NO PLAN -> not certifiable.
    run_scenario("S1 FAIL SHAPE (kept-12 span 5 ARG scorers)",
                 entities, kept_wide, fail_marginals, cfg)

    # S2 — the observed 04:15:23Z/04:24:55Z flicker: kept set concentrated on
    # Messi/Yamal -> share system feasible -> certified (live over_budget).
    run_scenario("S2 FLICKER CERTIFIED (Messi-concentrated kept set)",
                 entities, kept_narrow, cert_marginals, cfg)

    # S3 — the second sub-cause the same emission covers: an instant where the
    # game-family books flap unreadable leaves ONE team-level target -> the
    # "cannot identify (lam_a, lam_b)" raise -> same no_structural_plan.
    run_scenario("S3 THIN-BOOK INSTANT (only champ-AR team-level readable)",
                 entities, kept_narrow, thin_marginals, cfg)

    # S4 — regression check: pre-settled-work build_game_plans (dad3d91) on
    # byte-identical universes — BOTH the failing (wide) and certifying
    # (narrow) shapes — with the settled facts as the new provider feeds them
    # (0/1) and as the OLD provider produced them (None).
    old_bgp = old_build_game_plans()
    print()
    for ulabel, kept, margs in (
        ("fail-shape", kept_wide, fail_marginals),
        ("cert-shape", kept_narrow, cert_marginals),
    ):
        uni_tickers: list[str] = []
        uni_events: list[str | None] = []
        seen: set[str] = set()
        holders = list(entities)
        for q in kept:
            holders.extend(q.hypotheticals)
        for h in holders:
            for leg in h.legs:
                if leg.market_ticker not in seen:
                    seen.add(leg.market_ticker)
                    uni_tickers.append(leg.market_ticker)
                    uni_events.append(leg.event_ticker)
        margs_nofacts = {t: p for t, p in margs.items() if t not in SETTLED_FACTS}
        outcomes = []
        for label, bgp in (("HEAD", build_game_plans), ("dad3d91", old_bgp)):
            for mlabel, m in (
                ("facts-as-0/1", margs),
                ("facts-as-None", margs_nofacts),
            ):
                plans, copula = bgp(
                    uni_tickers, uni_events,
                    [m.get(t) for t in uni_tickers], cfg,
                )
                games = sorted(
                    game_of
                    for plan in plans
                    if (ev := uni_events[plan.global_indices[0]]) is not None
                    for game_of in [ev]
                )
                outcomes.append(tuple(games))
                print(
                    f"S4 {ulabel} | {label:8s} | {mlabel:14s} -> plan events: "
                    f"{games or 'NONE'}; copula {len(copula)}/{len(uni_tickers)}"
                )
        assert len(set(outcomes)) == 1, "old/new plan sets DIVERGE — regression!"
        print(f"S4 {ulabel}: old and new code IDENTICAL on the same inputs")

if __name__ == "__main__":
    main()
