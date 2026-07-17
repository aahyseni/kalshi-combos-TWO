"""ESPARG champion-leg PRICING ALIAS (2026-07-16, operator-directed).

The WC final lists NO ``KXWCADVANCE`` series (tape-verified); the "wins the
World Cup" flow arrives on ``KXMENWORLDCUP-26-{AR,ES}``, which at finals time
is settlement-identical to advancing the final (win incl. ET + pens). The
config-driven exact-ticker alias makes the pricing layer reason about the
synthetic ``KXWCADVANCE-26JUL19ESPARG-{ARG,ESP}`` legs while the exchange
identity (books, marginals, quoting, settlement) keeps the real ticker.

Covered here: validation (only-UNKNOWN keys, modeled targets, no chains, event
consistency), classification, markup sport tagging (the observed-live ZERO-
markup bug), game grouping (the ONE pricer/risk seam), relationships (grouping
follows the alias; mutual-exclusion metadata stays on the REAL event),
structural parity (aliased champion == literal advance leg to the cent),
risk-side game plans, and the process-boundary installs (engine + book-risk
pool initializer).
"""

from __future__ import annotations

import pytest

from combomaker.ops.config import PricingConfig, StructuralConfig
from combomaker.ops.pricing_pool import _book_risk_pool_init
from combomaker.pricing.grouping import game_key
from combomaker.pricing.legs import LegBelief
from combomaker.pricing.legtypes import (
    LegType,
    Sport,
    classify_leg,
    classify_sport,
    is_period_leg,
    resolve_pricing_alias,
    resolve_pricing_event_alias,
    set_pricing_aliases,
    validate_pricing_aliases,
)
from combomaker.pricing.markup import sport_of
from combomaker.pricing.relationships import (
    RelationshipKind,
    classify_legs,
)
from combomaker.pricing.structural import StructuralPricer
from combomaker.rfq.models import RfqLeg
from combomaker.sim.structural_book import StructuralConfigView, build_game_plans

GAME = "26JUL19ESPARG"
CHAMP_AR = "KXMENWORLDCUP-26-AR"
CHAMP_ES = "KXMENWORLDCUP-26-ES"
CHAMP_EVENT = "KXMENWORLDCUP-26"
SYN_ADV_ARG = f"KXWCADVANCE-{GAME}-ARG"
SYN_ADV_ESP = f"KXWCADVANCE-{GAME}-ESP"
SYN_EVENT = f"KXWCADVANCE-{GAME}"
BTTS = f"KXWCBTTS-{GAME}-BTTS"
BTTS_EVENT = f"KXWCBTTS-{GAME}"
TOTAL = f"KXWCTOTAL-{GAME}-3"
TOTAL_EVENT = f"KXWCTOTAL-{GAME}"

ALIASES = {CHAMP_AR: SYN_ADV_ARG, CHAMP_ES: SYN_ADV_ESP}


def leg(ticker: str, event: str | None, side: str = "yes") -> RfqLeg:
    return RfqLeg(ticker, event, side, None)


def belief(p: float, unc: float = 0.005) -> LegBelief:
    return LegBelief(p=p, uncertainty=unc, source="test")


class MappingProvider:
    def __init__(self, mapping: dict[str, bool]) -> None:
        self._m = mapping

    def event_mutually_exclusive(self, event_ticker: str) -> bool | None:
        return self._m.get(event_ticker)


# --- validation ---------------------------------------------------------------


def test_good_aliases_install_and_resolve() -> None:
    set_pricing_aliases(ALIASES)
    assert resolve_pricing_alias(CHAMP_AR) == SYN_ADV_ARG
    assert resolve_pricing_alias(CHAMP_ES) == SYN_ADV_ESP
    assert resolve_pricing_alias(BTTS) == BTTS  # identity when unaliased
    assert resolve_pricing_event_alias(CHAMP_EVENT) == SYN_EVENT
    assert resolve_pricing_event_alias(BTTS_EVENT) == BTTS_EVENT


def test_alias_key_must_classify_unknown() -> None:
    # An alias may never override a modeled family.
    with pytest.raises(ValueError, match="only|never override|classifies"):
        validate_pricing_aliases({BTTS: SYN_ADV_ARG})


def test_alias_target_must_be_modeled() -> None:
    with pytest.raises(ValueError, match="UNKNOWN"):
        validate_pricing_aliases({CHAMP_AR: "KXSOMENONSENSE-26-X"})


def test_alias_chain_and_self_alias_rejected() -> None:
    with pytest.raises(ValueError, match="chain"):
        validate_pricing_aliases(
            {CHAMP_AR: SYN_ADV_ARG, SYN_ADV_ARG: SYN_ADV_ESP}
        )
    with pytest.raises(ValueError, match="self-alias|empty"):
        validate_pricing_aliases({CHAMP_AR: CHAMP_AR})


def test_alias_needs_event_suffix_shape() -> None:
    with pytest.raises(ValueError, match="EVENT-SUFFIX"):
        validate_pricing_aliases({"KXMENWORLDCUP": SYN_ADV_ARG})


def test_alias_event_derivation_must_be_consistent() -> None:
    # Two keys of ONE real event must not map to two synthetic events.
    with pytest.raises(ValueError, match="multiple synthetic"):
        validate_pricing_aliases(
            {CHAMP_AR: SYN_ADV_ARG, CHAMP_ES: "KXWCADVANCE-26JUL18FRAENG-ESP"}
        )


def test_bad_alias_never_half_installs() -> None:
    set_pricing_aliases(ALIASES)
    with pytest.raises(ValueError):
        set_pricing_aliases({BTTS: SYN_ADV_ARG})
    # The failed install must not have cleared/half-replaced the registry
    # arbitrarily; what matters for safety: resolution is never the BAD mapping.
    assert resolve_pricing_alias(BTTS) == BTTS


def test_pricing_config_validates_aliases_at_load() -> None:
    ok = PricingConfig(leg_pricing_aliases=dict(ALIASES))
    assert ok.leg_pricing_aliases[CHAMP_AR] == SYN_ADV_ARG
    with pytest.raises(Exception, match="never override|classifies"):
        PricingConfig(leg_pricing_aliases={BTTS: SYN_ADV_ARG})


# --- classification / markup ---------------------------------------------------


def test_champion_classifies_unknown_without_alias() -> None:
    assert classify_leg(CHAMP_AR) is LegType.UNKNOWN
    assert classify_sport(CHAMP_AR) is Sport.UNKNOWN
    assert sport_of([CHAMP_AR, BTTS]) == "other"  # the observed-live 0-markup bug
    assert game_key(CHAMP_EVENT) == "26"  # would NOT join the final's game


def test_champion_classifies_as_advance_with_alias() -> None:
    set_pricing_aliases(ALIASES)
    assert classify_leg(CHAMP_AR) is LegType.ADVANCE
    assert classify_leg(CHAMP_ES) is LegType.ADVANCE
    assert classify_sport(CHAMP_AR) is Sport.SOCCER
    assert not is_period_leg(CHAMP_AR)
    # markup: the combo tags soccer, so the soccer markup (not 0) applies
    assert sport_of([CHAMP_AR, BTTS]) == "soccer"


def test_game_key_follows_event_alias() -> None:
    set_pricing_aliases(ALIASES)
    assert game_key(CHAMP_EVENT) == GAME
    assert game_key(BTTS_EVENT) == GAME  # unaliased events unchanged
    assert game_key("KXWCGAME-26JUL18FRAENG") == "26JUL18FRAENG"


# --- relationships --------------------------------------------------------------


def test_champion_leg_joins_the_finals_game_group() -> None:
    set_pricing_aliases(ALIASES)
    legs = [leg(CHAMP_AR, CHAMP_EVENT), leg(BTTS, BTTS_EVENT)]
    rel = classify_legs(legs, MappingProvider({}))
    assert rel.kind is RelationshipKind.OK
    assert (0, 1) in rel.same_event_groups


def test_champion_pair_me_uses_the_real_event() -> None:
    # AR + ES both-YES: mutual exclusion must key on the REAL exchange event
    # (KXMENWORLDCUP-26 metadata), not the synthetic one — and stays unfarmable
    # (metadata-based, not a tautology).
    set_pricing_aliases(ALIASES)
    legs = [leg(CHAMP_AR, CHAMP_EVENT), leg(CHAMP_ES, CHAMP_EVENT)]
    rel = classify_legs(legs, MappingProvider({CHAMP_EVENT: True}))
    assert rel.kind is RelationshipKind.IMPOSSIBLE
    assert not rel.farmable


# --- structural parity (the point of the feature) -------------------------------


def _pricer() -> StructuralPricer:
    return StructuralPricer(StructuralConfig(enabled=True))  # type: ignore[arg-type]


def test_aliased_champion_prices_structurally_and_matches_literal_advance() -> None:
    set_pricing_aliases(ALIASES)
    beliefs = [belief(0.55), belief(0.62)]
    sides = ["yes", "yes"]
    aliased_legs = [leg(CHAMP_AR, CHAMP_EVENT), leg(TOTAL, TOTAL_EVENT)]
    literal_legs = [leg(SYN_ADV_ARG, SYN_EVENT), leg(TOTAL, TOTAL_EVENT)]
    est_aliased, reason_aliased = _pricer().try_price(aliased_legs, beliefs, sides)
    est_literal, reason_literal = _pricer().try_price(literal_legs, beliefs, sides)
    assert reason_aliased is None and reason_literal is None
    assert est_aliased is not None and est_literal is not None
    # Bit-identical joint: the alias may not change the math, only the routing.
    assert est_aliased.p == est_literal.p
    assert est_aliased.uncertainty == est_literal.uncertainty


def test_champion_combo_falls_back_without_alias() -> None:
    beliefs = [belief(0.55), belief(0.62)]
    est, reason = _pricer().try_price(
        [leg(CHAMP_AR, CHAMP_EVENT), leg(TOTAL, TOTAL_EVENT)], beliefs, ["yes", "yes"]
    )
    assert est is None and reason is not None  # honest copula fallback pre-alias


# --- risk-side game plans (pricing/risk must move together) ----------------------


def test_build_game_plans_folds_aliased_champion_into_the_final() -> None:
    set_pricing_aliases(ALIASES)
    tickers = [CHAMP_AR, SYN_ADV_ESP]
    events = [CHAMP_EVENT, SYN_EVENT]
    plans, copula = build_game_plans(
        tickers, events, [0.55, 0.45], StructuralConfigView()
    )
    assert len(plans) == 1 and copula == []
    assert sorted(plans[0].global_indices) == [0, 1]


def test_build_game_plans_without_alias_champion_is_copula() -> None:
    tickers = [CHAMP_AR, SYN_ADV_ESP]
    events = [CHAMP_EVENT, SYN_EVENT]
    plans, copula = build_game_plans(
        tickers, events, [0.55, 0.45], StructuralConfigView()
    )
    assert 0 in copula  # ungrouped, unparseable champion leg stays copula


# --- process-boundary installs ---------------------------------------------------


def test_engine_init_installs_aliases() -> None:
    # The REAL ctor path: constructing an engine from a config carrying aliases
    # must land them in the process registry (this is exactly what each
    # pricing-pool worker does in its initializer — same ctor, same config).
    from combomaker.core.conventions import load_conventions
    from combomaker.ops.pricing_pool import _StubFeed, _StubMetadata
    from combomaker.pricing.engine import PricingEngine

    set_pricing_aliases({})
    PricingEngine(
        _StubFeed(),  # type: ignore[arg-type]
        _StubMetadata(),  # type: ignore[arg-type]
        load_conventions(),
        PricingConfig(leg_pricing_aliases=dict(ALIASES)),
        joint_memo_maxsize=0,
    )
    assert resolve_pricing_alias(CHAMP_AR) == SYN_ADV_ARG


def test_book_risk_pool_initializer_installs_aliases() -> None:
    _book_risk_pool_init(dict(ALIASES))
    assert resolve_pricing_alias(CHAMP_ES) == SYN_ADV_ESP
    assert game_key(CHAMP_EVENT) == GAME
    with pytest.raises(ValueError):
        _book_risk_pool_init({BTTS: SYN_ADV_ARG})  # fails LOUDLY, in-worker too


# --- review 2026-07-16 hardening (adversarial fleet findings) --------------------


def test_validator_rejects_duplicate_targets() -> None:
    # Two keys -> ONE synthetic ticker = both real legs settle as the same
    # synthetic leg (sign-flipped pricing, risk that nets a concentrated book
    # as hedged). The exact one-character copy-paste shape a game-day yaml
    # slip produces.
    with pytest.raises(ValueError, match="distinct"):
        validate_pricing_aliases({CHAMP_AR: SYN_ADV_ARG, CHAMP_ES: SYN_ADV_ARG})


def test_validator_rejects_noncanonical_keys_and_values() -> None:
    # Runtime resolution is an exact match against live UPPERCASE tickers; a
    # lowercase/whitespace entry validates but never fires (silently inert —
    # the zero-markup incident would recur with a green config load).
    with pytest.raises(ValueError, match="canonical"):
        validate_pricing_aliases({CHAMP_AR.lower(): SYN_ADV_ARG})
    with pytest.raises(ValueError, match="canonical"):
        validate_pricing_aliases({CHAMP_AR: SYN_ADV_ARG + " "})


def test_tripwire_verdicts_match_literal_advance_legs() -> None:
    # The pinned-impossible tripwire must reach the SAME verdict for an
    # aliased champion leg as for its literal synthetic equivalent — pre-fix
    # its team read came off the RAW ticker ('AR' vs 'ARG'), which both
    # false-tripped valid champion parlays (S18 diff_team) and missed real
    # impossibles (S15 same_team).
    from combomaker.pricing.tripwire import taxonomy_impossible

    set_pricing_aliases(ALIASES)
    reg_win = f"KXWCGAME-{GAME}-ARG"
    reg_event = f"KXWCGAME-{GAME}"

    def verdict(ticker_a: str, side_a: str, ticker_b: str, side_b: str):
        legs = [leg(ticker_a, CHAMP_EVENT if "MENWORLDCUP" in ticker_a else SYN_EVENT, side_a),
                leg(ticker_b, reg_event, side_b)]
        keys = [game_key(x.event_ticker or "") for x in legs]
        return taxonomy_impossible(legs, keys)

    # MISSED-TRIP case: reg win YES x same-team champion NO is truly
    # impossible (an ARG regulation win makes ARG champion) — S15 must fire
    # exactly as it does for the literal advance leg.
    aliased = verdict(CHAMP_AR, "no", reg_win, "yes")
    literal = verdict(SYN_ADV_ARG, "no", reg_win, "yes")
    assert literal is not None and aliased is not None
    assert aliased[0] == literal[0]

    # FALSE-TRIP case: champion YES x same-team reg win YES is a natural,
    # POSSIBLE finals parlay — no cell may fire (pre-fix S18's diff_team
    # matched because 'AR' != 'ARG').
    assert verdict(CHAMP_AR, "yes", reg_win, "yes") is None
    assert verdict(SYN_ADV_ARG, "yes", reg_win, "yes") is None


def test_build_game_plans_sibling_first_does_not_drop_the_final() -> None:
    # The derived EVENT alias folds unaliased sibling champion markets (an
    # eliminated team, e.g. -FR) into the final's game group; the game code is
    # now parsed from the GROUP KEY, so a sibling ordered FIRST degrades only
    # ITSELF to the copula, never the whole final.
    set_pricing_aliases(ALIASES)
    sibling = "KXMENWORLDCUP-26-FR"
    tickers = [sibling, CHAMP_AR, SYN_ADV_ESP]
    events = [CHAMP_EVENT, CHAMP_EVENT, SYN_EVENT]
    plans, copula = build_game_plans(
        tickers, events, [0.01, 0.55, 0.45], StructuralConfigView()
    )
    assert len(plans) == 1
    assert sorted(plans[0].global_indices) == [1, 2]
    assert copula == [0]


def test_ensure_workers_spawned_nonblocking_read() -> None:
    # timeout_s=0 must still READ once (the old deadline-guarded loop returned
    # [] at timeout 0, so the non-blocking per-run register recorded nothing).
    from combomaker.ops.process_group import _ensure_workers_spawned

    class _P:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    class _Fake:
        _processes = {123: _P(123), 456: _P(456)}

    assert sorted(_ensure_workers_spawned(_Fake(), 2, timeout_s=0.0)) == [123, 456]


# --- pregame schedule config plumbing (tier a2, the final-day gate fix) ----------
# The champion market's expected_expiration (14:00Z) precedes the final's ~19:00Z
# kickoff, so the expiry-minus-offset ESTIMATE declares champion legs in-play
# from ~09:30Z on final day. The operator-set explicit schedule entry is the
# designed precision tier for exactly this; here we pin the NEW config plumbing
# (validation + the gate consuming a config-built cache). Ladder mechanics are
# covered in tests/test_pregame_precision.py.


def test_scheduled_starts_config_validation() -> None:
    from combomaker.ops.config import FiltersConfig

    ok = FiltersConfig(
        pregame_scheduled_starts={CHAMP_EVENT: "2026-07-19T19:00:00+00:00"}
    )
    assert CHAMP_EVENT in ok.pregame_scheduled_starts
    with pytest.raises(Exception, match="naive"):
        FiltersConfig(pregame_scheduled_starts={CHAMP_EVENT: "2026-07-19T19:00:00"})
    with pytest.raises(Exception, match="unparseable"):
        FiltersConfig(pregame_scheduled_starts={CHAMP_EVENT: "final kickoff"})


def test_schedule_entry_overrides_the_broken_champion_estimate() -> None:
    from datetime import UTC, datetime, timedelta

    from combomaker.marketdata.metadata import MarketMeta, MetadataCache
    from combomaker.ops.config import FiltersConfig
    from combomaker.rfq.pregame import PregameGate
    from combomaker.rfq.schedule import ScheduleCache
    from tests.test_pregame_precision import FakeClock

    # Final day, 15:00Z: 4h BEFORE the 19:00Z kickoff, but AFTER the champion
    # market's 14:00Z expected_expiration (the broken anchor).
    now = datetime(2026, 7, 19, 15, 0, tzinfo=UTC)
    clock = FakeClock(start=now)
    meta = MetadataCache(None, clock)  # type: ignore[arg-type]
    meta._markets[CHAMP_AR] = MarketMeta(  # noqa: SLF001 (test seam)
        ticker=CHAMP_AR,
        status="active",
        grid=None,
        event_ticker=CHAMP_EVENT,
        close_time=None,
        expected_expiration_time=datetime(2026, 7, 19, 14, 0, tzinfo=UTC),
        raw={},
        fetched_mono_ns=clock.monotonic_ns(),
    )
    cfg = FiltersConfig(
        pregame_scheduled_starts={CHAMP_EVENT: "2026-07-19T19:00:00+00:00"}
    )
    # The same construction quote_app now performs from the config field.
    cache = ScheduleCache(
        {
            ev: datetime.fromisoformat(raw)
            for ev, raw in cfg.pregame_scheduled_starts.items()
        }
    )
    with_schedule = PregameGate(cfg, meta, clock, cache)
    resolved = with_schedule.leg_start(CHAMP_AR)
    assert resolved is not None and resolved.precise is True
    assert resolved.start == datetime(2026, 7, 19, 19, 0, tzinfo=UTC)
    assert clock.now() < resolved.start  # 15:00Z is PREGAME again

    # Without the entry the estimate anchors on 14:00Z − offset ⇒ already
    # "started" hours before kickoff (the bug this plumbing exists to fix).
    without = PregameGate(cfg, meta, clock, ScheduleCache())
    est = without.leg_start(CHAMP_AR)
    assert est is not None and est.precise is False
    assert est.start < now - timedelta(hours=1)


# --- verify follow-ups 2026-07-16 (adversarial fleet, round 2) --------------------


def test_validator_rejects_folding_two_real_events_into_one_synthetic() -> None:
    # SERIOUS (completeness sweep): two DIFFERENT real events aliased into ONE
    # synthetic event share a game key — the single pricer/risk seam — so the
    # game-loss cap, directional bound, game plans, copula regroup and the
    # waiver's scoreline enumeration would all cross-net unrelated legs (the
    # E2 failure family). A stale entry from a prior arming left beside a new
    # tournament's entries produces exactly this shape.
    with pytest.raises(ValueError, match="fold"):
        validate_pricing_aliases(
            {CHAMP_AR: SYN_ADV_ARG, "KXWOMENWORLDCUP-27-ES": SYN_ADV_ESP}
        )
    # The legitimate shape — ONE real event, one synthetic event — still passes.
    validate_pricing_aliases(ALIASES)


def test_advance_complement_family_all_four_sign_mixes() -> None:
    # Family 4: a knockout advances EXACTLY ONE of its two teams, so an
    # advance pair on one game is a COMPLEMENT. Without the family a
    # mixed-side pair fell to the structural path, where joint_probability
    # multiplies each leg's pens factor INDEPENDENTLY (q² instead of q on the
    # level-after-ET states — a ~3-5c underprice on a tight final).
    set_pricing_aliases(ALIASES)
    # Realistic metadata (champion event IS mutually exclusive): the ME pass
    # only sees >=2 YES legs, so every other mix reaches Family 4.
    provider = MappingProvider({CHAMP_EVENT: True})

    def rel(side_a: str, side_b: str):
        return classify_legs(
            [leg(CHAMP_AR, CHAMP_EVENT, side_a), leg(CHAMP_ES, CHAMP_EVENT, side_b)],
            provider,
        )

    both = rel("yes", "yes")
    assert both.kind is RelationshipKind.IMPOSSIBLE  # ME pass catches this one
    neither = rel("no", "no")  # yes_count=0: ONLY Family 4 catches it
    assert neither.kind is RelationshipKind.IMPOSSIBLE and neither.farmable
    mixed = rel("yes", "no")
    assert mixed.kind is RelationshipKind.CONTAINMENT
    assert mixed.containment == (0, 1)  # joint = P(the YES leg)
    flipped = rel("no", "yes")
    assert flipped.kind is RelationshipKind.CONTAINMENT
    assert flipped.containment == (1, 0)
    # Metadata claiming NOT-exclusive does not defeat the rule book: Family 4
    # still calls both-YES impossible (and farmable — it is rule-book logic,
    # not metadata).
    lax = MappingProvider({CHAMP_EVENT: False})
    both_lax = classify_legs(
        [leg(CHAMP_AR, CHAMP_EVENT, "yes"), leg(CHAMP_ES, CHAMP_EVENT, "yes")], lax
    )
    assert both_lax.kind is RelationshipKind.IMPOSSIBLE and both_lax.farmable


def test_advance_complement_family_real_advance_pair() -> None:
    # The same family covers REAL advance pairs — the pre-existing shape of
    # the pens bug (a semi's -ARG yes x -ENG no priced through independent
    # pens factors long before the alias existed).
    ev = "KXWCADVANCE-26JUL15ENGARG"
    provider = MappingProvider({ev: True})
    r = classify_legs(
        [leg(f"{ev}-ARG", ev, "yes"), leg(f"{ev}-ENG", ev, "no")],
        provider,
    )
    assert r.kind is RelationshipKind.CONTAINMENT
    assert r.containment == (0, 1)
    r2 = classify_legs(
        [leg(f"{ev}-ARG", ev, "no"), leg(f"{ev}-ENG", ev, "no")],
        provider,  # yes_count=0: the ME pass is silent, Family 4 is not
    )
    assert r2.kind is RelationshipKind.IMPOSSIBLE and r2.farmable


def test_waiver_settle_specs_include_the_aliased_champion_leg() -> None:
    # The waiver's marginal-free settlement must settle the champion leg from
    # the final's scoreline (pre-fix its RAW blob '26' failed the game-key
    # check and the leg stayed ADVERSARIAL — the alias's whole point lost in
    # the one place it matters most). The settle map keys on the REAL ticker
    # (the loss matrix looks legs up by exchange identity); an unaliased
    # sibling (eliminated team) stays out (fail-closed adversarial).
    from combomaker.risk.exposure import LegRef
    from combomaker.sim.state_worst_case import _settle_specs

    set_pricing_aliases(ALIASES)
    refs = [
        LegRef(CHAMP_AR, CHAMP_EVENT, "yes"),
        LegRef("KXMENWORLDCUP-26-FR", CHAMP_EVENT, "no"),
        LegRef(BTTS, BTTS_EVENT, "yes"),
    ]
    settle = _settle_specs(GAME, {}, refs, StructuralConfigView())
    assert CHAMP_AR in settle  # keyed on the REAL ticker
    assert BTTS in settle
    assert "KXMENWORLDCUP-26-FR" not in settle


def test_conditioning_knockout_flag_or_folds_over_aliased_legs() -> None:
    # The knockout flag reads the ALIAS-RESOLVED series and OR-folds over the
    # game's structural legs: with the raw last-write read, whichever champion
    # leg iterated LAST flipped the final's flag OFF and the knockout corners
    # leg silently lost its ET loading.
    import numpy as np

    from combomaker.sim.book_model import BookModel
    from combomaker.sim.book_risk import _build_conditioning
    from combomaker.sim.engine import LegModel

    set_pricing_aliases(ALIASES)
    corners = f"KXWCCORNERS-{GAME}-9"
    corners_ev = f"KXWCCORNERS-{GAME}"
    tickers = [corners, CHAMP_ES, CHAMP_AR]
    events = [corners_ev, CHAMP_EVENT, CHAMP_EVENT]
    cfg = StructuralConfigView()
    plans, copula_idx = build_game_plans(tickers, events, [0.40, 0.45, 0.55], cfg)
    assert len(plans) == 1 and copula_idx == [0]
    corr = np.eye(3)
    model = BookModel(
        (LegModel(p=0.40), LegModel(p=0.45), LegModel(p=0.55)),
        (),
        corr,
        corr.copy(),
        corr.copy(),
        {corners: 0, CHAMP_ES: 1, CHAMP_AR: 2},
        {0: corners_ev, 1: CHAMP_EVENT, 2: CHAMP_EVENT},
        False,
    )
    cond = _build_conditioning(model, plans, copula_idx, cfg)
    assert cond.loading_of_copula_index.get(0, 0.0) > 0.0


def test_maker_fee_prefix_and_schedule_key_canonical_validation() -> None:
    # Completeness sweep: game-day config keys that bypass canonical-form
    # validation fail SILENT — an empty fee prefix fees EVERY series, a
    # lowercase one fees none; a non-canonical schedule key leaves the
    # champion legs on the broken expiry estimate with a green config load.
    from combomaker.ops.config import FeeConfig, FiltersConfig

    with pytest.raises(ValueError, match="canonical"):
        FeeConfig(maker_fee_active_prefixes=("",))
    with pytest.raises(ValueError, match="canonical"):
        FeeConfig(maker_fee_active_prefixes=("kxwc ",))
    FeeConfig(maker_fee_active_prefixes=("KXWC",))  # canonical passes
    with pytest.raises(ValueError, match="canonical"):
        FiltersConfig(
            pregame_scheduled_starts={"kxmenworldcup-26 ": "2026-07-19T18:45:00+00:00"}
        )
    FiltersConfig(
        pregame_scheduled_starts={CHAMP_EVENT: "2026-07-19T18:45:00+00:00"}
    )
