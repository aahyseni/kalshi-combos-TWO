"""MarkupPolicy — sport tag + per-sport markup lookup, DARK unless the master
switch AND the sport are both enabled. This is the seam the engine reads to pass
markup_cc into construct_quote (margin = max(width, markup))."""

from __future__ import annotations

from combomaker.ops.config import MarkupConfig, SportMarkupConfig
from combomaker.pricing.markup import MarkupPolicy, sport_of


def test_sport_of_requires_all_legs_same_sport() -> None:
    assert sport_of(["KXWCADVANCE-26JUL14FRAESP-FRA"]) == "soccer"
    assert sport_of(["KXWCADVANCE-x", "KXWCGOAL-y"]) == "soccer"  # all-WC combo
    assert sport_of(["KXMLBGAME-26JUL08NYYTB-NYY"]) == "mlb"
    # FAIL-SAFE: a mixed-sport combo (or any unknown leg) tags 'other' => markup 0,
    # so a sport's markup never leaks onto another sport's leg.
    assert sport_of(["KXWCADVANCE-x", "KXMLBGAME-y"]) == "other"
    assert sport_of(["KXWCADVANCE-x", "KXNFLGAME-y"]) == "other"
    assert sport_of(["KXNFLGAME-x"]) == "other"
    assert sport_of([]) == "other"


def test_dark_by_default() -> None:
    p = MarkupPolicy.from_config(MarkupConfig())
    assert not p.enabled
    assert p.markup_cc("soccer") == 0
    assert p.markup_cc("mlb") == 0


def test_master_switch_gates_every_sport() -> None:
    cfg = MarkupConfig(
        enabled=False,  # master off overrides an enabled sport
        soccer=SportMarkupConfig(enabled=True, markup_cc=400),
    )
    assert MarkupPolicy.from_config(cfg).markup_cc("soccer") == 0


def test_per_sport_toggle() -> None:
    cfg = MarkupConfig(
        enabled=True,
        soccer=SportMarkupConfig(enabled=True, markup_cc=400),
        mlb=SportMarkupConfig(enabled=False, markup_cc=250),
    )
    p = MarkupPolicy.from_config(cfg)
    assert p.markup_cc("soccer") == 400
    assert p.markup_cc("mlb") == 0  # sport toggled off, even with a number set
    assert p.markup_cc("other") == 0


def test_zero_markup_not_registered() -> None:
    cfg = MarkupConfig(enabled=True, soccer=SportMarkupConfig(enabled=True, markup_cc=0))
    assert MarkupPolicy.from_config(cfg).markup_cc("soccer") == 0


def test_markup_for_returns_sport_and_cc() -> None:
    cfg = MarkupConfig(enabled=True, soccer=SportMarkupConfig(enabled=True, markup_cc=400))
    p = MarkupPolicy.from_config(cfg)
    assert p.markup_for(["KXWCADVANCE-x-FRA"]) == ("soccer", 400)
    assert p.markup_for(["KXMLBGAME-x-NYY"]) == ("mlb", 0)  # mlb dark
    assert p.markup_for(["KXNFLGAME-x"]) == ("other", 0)
    # mixed-sport combo => 'other' => no soccer-markup leak onto the MLB leg
    assert p.markup_for(["KXWCADVANCE-x", "KXMLBGAME-y"]) == ("other", 0)


# --- #37 corners edge-floor: per-series markup adders -------------------------


def _adder_cfg(**kw: object) -> MarkupConfig:
    return MarkupConfig(
        enabled=True,
        soccer=SportMarkupConfig(enabled=True, markup_cc=100),
        series_adders_cc={"KXWCCORNERS": 300, "KXWCTCORNERS": 300},
        **kw,  # type: ignore[arg-type]
    )


def test_corners_leg_adds_edge_floor_once() -> None:
    p = MarkupPolicy.from_config(_adder_cfg())
    sport, cc = p.markup_for(
        ["KXWCADVANCE-26JUL19ESPARG-ARG", "KXWCCORNERS-26JUL19ESPARG-9"]
    )
    assert (sport, cc) == ("soccer", 400)  # 1c sport markup + 3c corners floor


def test_two_corners_series_max_not_sum() -> None:
    p = MarkupPolicy.from_config(_adder_cfg())
    _, cc = p.markup_for(
        ["KXWCCORNERS-26JUL19ESPARG-9", "KXWCTCORNERS-26JUL19ESPARG-ESP5"]
    )
    assert cc == 400  # one defensive floor per combo, never 100+300+300


def test_no_corners_leg_unchanged() -> None:
    p = MarkupPolicy.from_config(_adder_cfg())
    _, cc = p.markup_for(
        ["KXWCADVANCE-26JUL19ESPARG-ARG", "KXWCTOTAL-26JUL19ESPARG-3"]
    )
    assert cc == 100


def test_adder_never_wakes_a_dark_sport() -> None:
    # Sport markup dark (master off) ⇒ adder must NOT apply — dark stays
    # bit-identical dark (the markup=0 parity invariant).
    cfg = MarkupConfig(
        enabled=False,
        soccer=SportMarkupConfig(enabled=True, markup_cc=100),
        series_adders_cc={"KXWCCORNERS": 300},
    )
    p = MarkupPolicy.from_config(cfg)
    _, cc = p.markup_for(["KXWCCORNERS-26JUL19ESPARG-9"])
    assert cc == 0


def test_adder_not_applied_when_sport_markup_zero() -> None:
    cfg = MarkupConfig(
        enabled=True,
        soccer=SportMarkupConfig(enabled=True, markup_cc=0),
        series_adders_cc={"KXWCCORNERS": 300},
    )
    p = MarkupPolicy.from_config(cfg)
    _, cc = p.markup_for(["KXWCCORNERS-26JUL19ESPARG-9"])
    assert cc == 0


def test_adder_rejected_negative() -> None:
    import pytest

    with pytest.raises(ValueError):
        MarkupConfig(enabled=True, series_adders_cc={"KXWCCORNERS": -1})


# --- fair-tiered markup (2026-07-16 operator: pad longshots, keep mains tight) --


def _tier_cfg() -> MarkupConfig:
    from combomaker.ops.config import MarkupTier

    return MarkupConfig(
        enabled=True,
        soccer=SportMarkupConfig(
            enabled=True,
            markup_cc=100,
            tiers=[
                MarkupTier(fair_below_cc=200, markup_cc=500),
                MarkupTier(fair_below_cc=1000, markup_cc=400),
                MarkupTier(fair_below_cc=3500, markup_cc=200),
            ],
        ),
        series_adders_cc={"KXWCCORNERS": 300},
    )


LEGS = ["KXWCADVANCE-26JUL19ESPARG-ARG", "KXWCTOTAL-26JUL19ESPARG-3"]


def test_tier_selection_by_fair() -> None:
    p = MarkupPolicy.from_config(_tier_cfg())
    assert p.markup_for(LEGS, fair_cc=150)[1] == 500    # deep longshot
    assert p.markup_for(LEGS, fair_cc=948)[1] == 400    # longshot
    assert p.markup_for(LEGS, fair_cc=2443)[1] == 200   # mid
    assert p.markup_for(LEGS, fair_cc=4077)[1] == 100   # main -> flat base


def test_tier_boundary_is_strict() -> None:
    # fair == fair_below_cc is NOT below the bound -> next tier / flat.
    p = MarkupPolicy.from_config(_tier_cfg())
    assert p.markup_for(LEGS, fair_cc=1000)[1] == 200
    assert p.markup_for(LEGS, fair_cc=3500)[1] == 100


def test_no_fair_falls_back_to_flat() -> None:
    p = MarkupPolicy.from_config(_tier_cfg())
    assert p.markup_for(LEGS)[1] == 100


def test_tiers_stack_with_series_adder() -> None:
    p = MarkupPolicy.from_config(_tier_cfg())
    legs = ["KXWCCORNERS-26JUL19ESPARG-9", "KXWCTOTAL-26JUL19ESPARG-3"]
    assert p.markup_for(legs, fair_cc=948)[1] == 700  # 4c tier + 3c corners floor


def test_tiers_dark_when_disabled() -> None:
    cfg = _tier_cfg().model_copy(update={"enabled": False})
    p = MarkupPolicy.from_config(cfg)
    assert p.markup_for(LEGS, fair_cc=150)[1] == 0


def test_tier_validation_rejects_unsorted() -> None:
    import pytest

    from combomaker.ops.config import MarkupTier

    with pytest.raises(ValueError):
        SportMarkupConfig(
            enabled=True, markup_cc=100,
            tiers=[
                MarkupTier(fair_below_cc=1000, markup_cc=400),
                MarkupTier(fair_below_cc=200, markup_cc=500),
            ],
        )


# --- 2026-08-16 ML-parlay override (the composition tilt's entry ticket) -------


def _ml_cfg(**kw) -> MarkupPolicy:
    from combomaker.ops.config import MarkupTier

    return MarkupPolicy.from_config(
        MarkupConfig(
            enabled=True,
            ml_parlay_cc=100,
            mlb=SportMarkupConfig(
                enabled=True,
                markup_cc=200,
                tiers=[MarkupTier(fair_below_cc=1000, markup_cc=400),
                       MarkupTier(fair_below_cc=2000, markup_cc=300)],
            ),
            soccer=SportMarkupConfig(enabled=True, markup_cc=200),
            **kw,
        )
    )


ML_A = "KXMLBGAME-26AUG16NYYBOS-NYY"
ML_B = "KXMLBGAME-26AUG16SEAHOU-SEA"
ML_C = "KXMLBGAME-26AUG16KCLAA-KC"


def test_ml_parlay_override_replaces_longshot_tier() -> None:
    p = _ml_cfg()
    # A 3-leg cross-game ML parlay at 8c fair: the 4c tier would price it;
    # the override prices 1c (the measured field-clearing level).
    assert p.markup_for([ML_A, ML_B, ML_C], fair_cc=800) == ("mlb", 100)
    # Above the fair bound: normal base applies (mains are not the class).
    assert p.markup_for([ML_A, ML_B], fair_cc=4000) == ("mlb", 200)


def test_ml_parlay_override_never_touches_prop_or_same_game_shapes() -> None:
    p = _ml_cfg()
    # A prop leg keeps the full longshot tier (the whale's cell).
    prop = "KXMLBKS-26AUG16SEAHOU-HOULCASTILLO58-6"
    assert p.markup_for([ML_A, prop], fair_cc=800) == ("mlb", 400)
    # Same-game ML pair (repeated game code) keeps the tier.
    same_game = "KXMLBGAME-26AUG16NYYBOS-BOS"
    assert p.markup_for([ML_A, same_game], fair_cc=800) == ("mlb", 400)
    # A single leg is not a parlay.
    assert p.markup_for([ML_A], fair_cc=800) == ("mlb", 400)


def test_ml_parlay_override_applies_across_all_sports() -> None:
    # 2026-08-26 operator: "for ML parlays make the markup 0.6c across all
    # sports" — the MLB/soccer restriction is gone. Esports ML parlays and
    # cross-sport (mixed) ML parlays ride the razor; dark/unknown legs
    # still zero out upstream (sport_of / mixed known-sports requirement).
    p = _ml_cfg(
        esports=SportMarkupConfig(enabled=True, markup_cc=300),
        mixed=SportMarkupConfig(enabled=True, markup_cc=300),
    )
    esports_a = "KXLOLGAME-26AUG27T1GENG-T1"
    esports_b = "KXLOLGAME-26AUG27DKKT-DK"
    assert p.markup_for([esports_a, esports_b], fair_cc=800) == ("esports", 100)
    # Cross-sport ML parlay (MLB x esports) resolves 'mixed' and rides it.
    assert p.markup_for([ML_A, esports_a], fair_cc=800) == ("mixed", 100)
    # A prop leg still keeps the full tier/base (shape guard unchanged).
    prop = "KXMLBKS-26AUG16SEAHOU-HOULCASTILLO58-6"
    assert p.markup_for([prop, esports_a], fair_cc=800) == ("mixed", 300)
    # An unknown-sport leg still zeroes the combo (fail-safe untouched).
    assert p.markup_for([esports_a, "KXBBLGAME-26SEP20BERBAY-BER"], fair_cc=800)[1] == 0


def test_ml_parlay_override_dark_by_default() -> None:
    from combomaker.ops.config import MarkupTier

    cfg = MarkupConfig(
        enabled=True,
        mlb=SportMarkupConfig(
            enabled=True,
            markup_cc=200,
            tiers=[MarkupTier(fair_below_cc=1000, markup_cc=400)],
        ),
    )
    p = MarkupPolicy.from_config(cfg)
    assert p.ml_parlay_cc == 0
    # Byte-identical: the tier still prices the shape.
    assert p.markup_for([ML_A, ML_B], fair_cc=800) == ("mlb", 400)


# --- 2026-08-19 thin-auction margin bump (fair >= 35c pools go begging) --------


def _thin_cfg(**kw) -> MarkupPolicy:
    from combomaker.ops.config import MarkupTier

    return MarkupPolicy.from_config(
        MarkupConfig(
            enabled=True,
            thin_auction_bonus_cc=100,
            mlb=SportMarkupConfig(
                enabled=True,
                markup_cc=200,
                tiers=[MarkupTier(fair_below_cc=1000, markup_cc=400)],
            ),
            soccer=SportMarkupConfig(enabled=True, markup_cc=200),
            esports=SportMarkupConfig(enabled=True, markup_cc=300),
            **kw,
        )
    )


SOCCER_LEGS = ["KXWCADVANCE-26JUL19ESPARG-ARG", "KXWCTOTAL-26JUL19ESPARG-3"]


def test_thin_auction_bonus_applies_at_and_above_bound() -> None:
    p = _thin_cfg()
    # The 35-65c pool (80% expiry) and the >=65c pool (88%) both get +1c.
    assert p.markup_for([ML_A, ML_B], fair_cc=3500) == ("mlb", 300)
    assert p.markup_for([ML_A, ML_B], fair_cc=6500) == ("mlb", 300)
    assert p.markup_for(SOCCER_LEGS, fair_cc=3500) == ("soccer", 300)
    assert p.markup_for(SOCCER_LEGS, fair_cc=6500) == ("soccer", 300)


def test_thin_auction_bonus_not_below_bound() -> None:
    # The razor/main pools (fair < 35c bound) keep today's markup exactly.
    p = _thin_cfg()
    assert p.markup_for([ML_A, ML_B], fair_cc=3499) == ("mlb", 200)
    assert p.markup_for([ML_A, ML_B], fair_cc=800) == ("mlb", 400)  # tier, no bonus
    assert p.markup_for(SOCCER_LEGS, fair_cc=150) == ("soccer", 200)


def test_thin_auction_bonus_not_for_other_sports() -> None:
    # Evidence is MLB/soccer auction pools only — esports keeps its base.
    p = _thin_cfg()
    assert p.markup_for(["KXLOLGAME-26AUG19T1GEN-T1"], fair_cc=6500) == ("esports", 300)


def test_thin_auction_disjoint_from_ml_parlay_override() -> None:
    # Both configured: the razor override owns fair < 3500, the bonus owns
    # fair >= 3500 — disjoint by construction (same default bound), pinned.
    p = _thin_cfg(ml_parlay_cc=100)
    assert p.ml_parlay_fair_below_cc == p.thin_auction_fair_min_cc == 3500
    assert p.markup_for([ML_A, ML_B], fair_cc=800) == ("mlb", 100)   # razor untouched
    assert p.markup_for([ML_A, ML_B], fair_cc=3500) == ("mlb", 300)  # bonus, no razor


def test_thin_auction_dark_default_is_byte_identical() -> None:
    from combomaker.ops.config import MarkupTier

    kw = dict(
        enabled=True,
        mlb=SportMarkupConfig(
            enabled=True,
            markup_cc=200,
            tiers=[MarkupTier(fair_below_cc=1000, markup_cc=400)],
        ),
        soccer=SportMarkupConfig(enabled=True, markup_cc=200),
        esports=SportMarkupConfig(enabled=True, markup_cc=300),
        ml_parlay_cc=100,
    )
    unset = MarkupPolicy.from_config(MarkupConfig(**kw))  # type: ignore[arg-type]
    assert unset.thin_auction_bonus_cc == 0
    explicit_zero = MarkupPolicy.from_config(
        MarkupConfig(thin_auction_bonus_cc=0, **kw)  # type: ignore[arg-type]
    )
    for legs in ([ML_A, ML_B], SOCCER_LEGS, ["KXLOLGAME-26AUG19T1GEN-T1"], [ML_A]):
        for fair in (None, 150, 800, 2000, 3499, 3500, 5000, 6500):
            assert unset.markup_for(legs, fair_cc=fair) == explicit_zero.markup_for(
                legs, fair_cc=fair
            )


def test_thin_auction_composes_additively_with_tier() -> None:
    from combomaker.ops.config import MarkupTier

    # A tier whose bound sits ABOVE the thin floor: bonus adds ON TOP of the
    # selected tier, not just the flat base.
    p = MarkupPolicy.from_config(
        MarkupConfig(
            enabled=True,
            thin_auction_bonus_cc=100,
            mlb=SportMarkupConfig(
                enabled=True,
                markup_cc=200,
                tiers=[MarkupTier(fair_below_cc=5000, markup_cc=300)],
            ),
        )
    )
    assert p.markup_for([ML_A, ML_B], fair_cc=4000) == ("mlb", 400)  # 3c tier + 1c
    assert p.markup_for([ML_A, ML_B], fair_cc=6500) == ("mlb", 300)  # 2c flat + 1c
