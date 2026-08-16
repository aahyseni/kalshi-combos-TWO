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
