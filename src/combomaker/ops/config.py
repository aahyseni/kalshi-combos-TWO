"""Configuration: pydantic-validated YAML per environment; secrets via env only.

The production guard is hardcoded here, not in YAML: quoting on prod requires
BOTH the explicit ``--confirm-live`` CLI flag AND ``prod_limits_configured:
true`` in the prod config file. Demo is the default environment everywhere.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Env(StrEnum):
    DEMO = "demo"
    PROD = "prod"


class Mode(StrEnum):
    OBSERVE = "observe"  # log RFQ flow + would-quotes; sends nothing
    PAPER = "paper"      # full pipeline incl. risk, records hypothetical quotes; sends nothing
    QUOTE = "quote"      # sends real quotes


# Doc-verified base URLs (docs/api-notes/auth-env.md). Demo is .co, prod .com.
_ENDPOINTS: dict[Env, tuple[str, str]] = {
    Env.DEMO: (
        "https://external-api.demo.kalshi.co/trade-api/v2",
        "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
    ),
    Env.PROD: (
        "https://external-api.kalshi.com/trade-api/v2",
        "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
    ),
}


class StrictModel(BaseModel):
    """Reject unknown keys: a typo in a limit name must fail loudly, not silently."""

    model_config = ConfigDict(extra="forbid")


class EndpointsConfig(StrictModel):
    rest_base_url: str
    ws_url: str

    @classmethod
    def for_env(cls, env: Env) -> Self:
        rest, ws = _ENDPOINTS[env]
        return cls(rest_base_url=rest, ws_url=ws)


class LoggingConfig(StrictModel):
    json_output: bool = True
    level: str = "INFO"


class SafetyConfig(StrictModel):
    # Set true in prod.yaml only after limits have been reviewed by the human.
    prod_limits_configured: bool = False


class FiltersConfig(StrictModel):
    """Quote/no-quote gates. Every rejection carries a ReasonCode and is logged."""

    # Whitelist of mve_collection_ticker prefixes. Empty = observe everything
    # (observe/paper only — quote mode refuses to run with an empty whitelist).
    collection_whitelist: list[str] = []
    combos_only: bool = True          # skip single-market RFQs
    min_legs: int = 2
    max_legs: int = 6
    min_contracts: float = 1.0        # whole contracts
    max_contracts: float = 10_000.0
    min_target_cost_dollars: float = 1.0
    max_target_cost_dollars: float = 50_000.0
    max_leg_spread_cc: int = 800      # widest acceptable leg spread ($0.08)
    min_leg_depth_contracts: float = 1.0   # min size behind BOTH best bids
    min_time_to_close_s: float = 3600.0    # pregame-only default (1h before close)


class FeeConfig(StrictModel):
    """Coefficients as decimal strings (exact Fractions downstream). The
    authoritative fee-schedule PDF is bot-blocked; these defaults come from
    corroborated secondary sources and MUST be reconciled against real fills
    (quiet-failure defense #3) before production."""

    taker_coef: str = "0.07"
    maker_coef: str = "0.0175"
    # fee_type per combo series until fetched from GET /series: conservative
    # default; overrides keyed by series/collection ticker prefix.
    default_fee_type: str = "quadratic"
    default_multiplier: str = "1.0"


class CorrelationConfig(StrictModel):
    """Conservative priors; empirical calibration (with sample-size gates)
    updates these, never code."""

    same_event_rho: float = 0.6
    cross_event_rho: float = 0.0
    rho_uncertainty: float = 0.25
    # SGP structure: signed YES-YES priors per typed pair (legtypes.pair_key).
    # CALIBRATED 2026-07-06 from 8,982 matches (top-5 EU leagues 20/21-24/25,
    # tools/calibrate_pairs_from_history.py — implied rho solved through OUR
    # copula). Notable corrections vs the original hand priors: btts|moneyline
    # flipped SIGN (+0.05 → −0.17: decisive wins are clean-sheet-ish); both
    # corners pairs measured ≈ 0 (folk wisdom busted). player_goal and extras
    # pairs remain hand priors (no match-level data) with the wide band.
    pair_rho: dict[str, float] = {
        "moneyline|moneyline": -0.95,      # measured −0.99: P(both win) = 0 exactly
        "btts|total": 0.75,                # measured +0.746, n=8982
        "player_goal|total": 0.40,         # hand prior (uncalibrated)
        "btts|player_goal": 0.35,          # hand prior (uncalibrated)
        # Structurally implied ~+0.51 in BOTH scoreline-inversion worked
        # examples (fav ENG/Kane and dog POR/Ronaldo, 2026-07-06) — a star
        # scorer's goals ARE his team's goals. Global kept below the soccer
        # value (other sports unmeasured), wide band.
        "moneyline|player_goal": 0.40,
        "btts|moneyline": -0.17,           # measured −0.20/−0.14 (home/away pooled)
        "moneyline|total": 0.23,           # measured +0.28/+0.18 (side asymmetry)
        "total|total": 0.95,               # nested thresholds: measured at the cap
        "corners|total": 0.00,             # measured −0.04 ≈ 0
        "btts|corners": 0.00,              # measured +0.01 ≈ 0
        "extras|total": 0.50,              # hand prior (MLB, uncalibrated)
    }
    typed_rho_uncertainty: float = 0.15
    untyped_rho_uncertainty: float = 0.30
    # Sport-specific tables (calibrated 2026-07-06; the same pair correlates
    # DIFFERENTLY per sport: winner×over is +0.23 in soccer, 0.00 in NFL/NBA).
    # Sports absent here fall back to the global table above with wider bands.
    # WNBA inherits NBA's numbers (transfer assumption, wider band).
    pair_rho_by_sport: dict[str, dict[str, float]] = {
        # Soccer: moneyline|total is CONDITIONAL-MLE on per-game closing-line
        # marginals (directive-compliant; club n=7,228 train, rho +0.30 SE
        # 0.019, BEATS independence out-of-sample on held-out 23/24+24/25) —
        # band widened for home/away asymmetry + internationals. btts pairs
        # remain POOLED-method (no closing BTTS odds in the dataset) with
        # widened bands until a conditional/structural refit; btts|ml at least
        # measured identically in club and international data (−0.197 both).
        # btts|moneyline is ORIENTATION-CONDITIONAL (":fav"/":dog" resolved by
        # the ML leg's YES-side marginal, blended across 45–55c): the −0.19
        # calibration pooled favorites and dogs, but "winners keep clean
        # sheets" is a favorites-only effect — a dog can only win by scoring,
        # so dog-win×btts is ~0 (scoreline-model implied +0.04 on the live
        # SPA/POR example that the market priced at exactly our structural
        # fair). Plain entry retained for marginal-less callers.
        # moneyline|player_goal: structurally implied +0.51/+0.52 on both
        # worked examples (fav and dog) — orientation-insensitive, so a single
        # entry; replaces the 0.25 hand prior that made us auto-lose striker
        # SGP auctions.
        "soccer": {
            "moneyline|total": 0.28,
            "btts|total": 0.70,
            "btts|moneyline": -0.19,
            "btts|moneyline:fav": -0.19,
            "btts|moneyline:dog": 0.00,
            "moneyline|player_goal": 0.50,
            "btts|player_goal": 0.35,
            "total|total": 0.95,
            "corners|total": 0.00,
            "btts|corners": 0.00,
            "moneyline|moneyline": -0.95,
        },
        # NFL moneyline|total = 0.00 DOUBLY confirmed: pooled-vs-Vegas-lines
        # AND conditional-MLE (+0.02, SE 0.023) whose fit does NOT beat
        # independence out of sample — per the directive, a dependence that
        # loses to independence OOS is noise and must not ship.
        "nfl": {
            "moneyline|total": 0.00,
            "spread|total": 0.03,
            "moneyline|spread": 0.88,
            "extras|total": 0.20,
            "moneyline|moneyline": -0.95,
        },
        # NBA verified on MODERN data (hoopR/ESPN 2016-2025, n=12,567):
        # ml|total +0.008, era-split 2016-20 vs 21-25 drift +0.008 — the zero
        # correlation survived the 3PT revolution.
        "nba": {
            "moneyline|total": 0.01,
            "moneyline|moneyline": -0.95,
        },
        "wnba": {
            "moneyline|total": 0.01,
            "moneyline|moneyline": -0.95,
        },
        # MLB (Retrosheet 2015-2024, n=20,642): home winner is slightly
        # ANTI-correlated with over (−0.056: home wins often skip the bottom
        # 9th ⇒ fewer runs). extras|total uses the POST-2020 value (+0.10):
        # the ghost-runner rule structurally changed extras scoring
        # (pre-2020 −0.04 → post +0.10) — a measured rule-change break.
        "mlb": {
            "moneyline|total": -0.05,
            "extras|total": 0.10,
            "extras|moneyline": -0.04,
            "moneyline|moneyline": -0.95,
        },
    }
    # Band overrides: sport-prefixed keys ("nfl:moneyline|total") for
    # calibrated sport entries; unprefixed keys for the global table.
    pair_rho_uncertainty: dict[str, float] = {
        "moneyline|moneyline": 0.04,
        "soccer:moneyline|total": 0.10,     # conditional-MLE SE .019; band covers asymmetry
        "soccer:btts|total": 0.12,          # pooled-method: widened pending conditional refit
        "soccer:btts|moneyline": 0.10,      # pooled-method (but club==intl)
        "soccer:btts|moneyline:fav": 0.10,
        "soccer:btts|moneyline:dog": 0.10,  # structural implication, 1 live validation
        "soccer:moneyline|player_goal": 0.12,   # structural implication ×2 examples
        "soccer:btts|player_goal": 0.20,    # implied 0.31 (fav) ↔ 0.68 (dog): band must span
        "moneyline|player_goal": 0.20,      # global fallback: non-soccer scorers unmeasured
        "soccer:total|total": 0.04,
        "soccer:corners|total": 0.08,
        "soccer:btts|corners": 0.08,
        "soccer:moneyline|moneyline": 0.04,
        "nfl:moneyline|total": 0.05,
        "nfl:spread|total": 0.05,
        "nfl:moneyline|spread": 0.05,
        "nfl:extras|total": 0.10,
        "nfl:moneyline|moneyline": 0.04,
        "nba:moneyline|total": 0.05,
        "nba:moneyline|moneyline": 0.04,
        "wnba:moneyline|total": 0.12,       # NBA transfer: wider until measured
        "wnba:moneyline|moneyline": 0.04,
        "mlb:moneyline|total": 0.06,
        "mlb:extras|total": 0.10,           # post-rule-change sample is smaller
        "mlb:extras|moneyline": 0.08,
        "mlb:moneyline|moneyline": 0.04,
    }


class QuoteConfig(StrictModel):
    base_width_cc: int = 200
    per_leg_width_cc: int = 100
    # legs component = per_leg × n^convexity — model error compounds
    # multiplicatively with leg count, so width should too (1.0 = linear).
    leg_count_convexity: float = 1.0
    uncertainty_width_scale: float = 1.0
    size_width_cc_per_100: int = 50
    time_wide_threshold_s: float = 21_600.0
    time_width_cc: int = 200
    in_play_extra_cc: int = 800
    min_capture_cc: int = 100
    free_money_margin_cc: int = 100
    # Longshots: absolute uncertainty shrinks with tiny fairs (gradient ∝ P),
    # which is anti-conservative in RELATIVE terms for whoever shorts the
    # longshot — floor it as a fraction of fair below the threshold.
    longshot_fair_threshold: float = 0.15
    longshot_min_rel_uncertainty: float = 0.25
    # Favorites stacks: well-estimated products, price-insensitive flow —
    # tighten to win it (1.0 = off; validate via markouts before lowering).
    favorite_leg_threshold: float = 0.65
    favorite_width_multiplier: float = 1.0


class ExternalOddsConfig(StrictModel):
    """SportsGameOdds adapter (docs/api-notes/sportsgameodds.md). OFF by
    default; free tier is 2,500 objects/month so the poller is budget-gated."""

    enabled: bool = False
    leagues: list[str] = []               # e.g. ["NBA", "NFL"]
    weight: float = 0.3                   # blend weight vs Kalshi book's 1.0
    poll_interval_s: float = 3_600.0
    max_events_per_league: int = 10
    max_age_s: float = 900.0              # cached marginal expiry
    devig_method: str = "power"
    base_uncertainty: float = 0.01
    # Explicit Kalshi ticker → "eventID|oddID" table; unmapped = Kalshi-only.
    mapping: dict[str, str] = {}


class StructuralConfig(StrictModel):
    """Dixon-Coles scoreline pricer for soccer SGPs (pricing/dixon_coles.py).

    ``enabled`` may be flipped ON only by an out-of-sample gate result
    (tools/validate_structural_oos.py): a structural fit that does not beat
    the v1 copula on held-out seasons is noise and must not ship.

    GATE PASSED 2026-07-06 (8,980 club games, train <2024 / test 23/24+24/25):
    structural beats the SHIPPED v1 copula on all three OOS joint-log-loss
    metrics — hw×over 1.24657 vs 1.24734, hw×btts 1.26330 vs 1.26724, and the
    3-leg triple 1.70607 vs 1.74775 (independence 1.94197). The margin GROWS
    with combo complexity: coherent scorelines beat pairwise rho stitching
    most where the maker quotes most."""

    enabled: bool = True
    max_goals: int = 12
    # DC low-score adjustment: FITTED on train-season scorelines through the
    # production inversion (grid MLE, tools/validate_structural_oos.py).
    dc_rho: float = -0.05
    dc_rho_band: float = 0.08
    # ET intensity as a fraction of regulation scoring rate (30min pro-rata =
    # 1/3); band edges re-price the joint for the model-form width.
    et_factor: float = 0.3333
    et_factor_low: float = 0.25
    et_factor_high: float = 0.40
    # P(named team wins a shootout | level after ET) for Advance legs.
    # Kalshi's knockout game market includes pens (rules text 2026-07-06);
    # 0.5 prior with a band covering first-kicker/keeper effects.
    pens_win_prob: float = 0.5
    pens_band: float = 0.10
    misfit_uncertainty_scale: float = 1.0
    # Series prefixes whose matches are knockout format (ET+pens possible).
    # Settlement windows per family are RULE-BOOK (operator-provided Kalshi
    # rules text 2026-07-06): knockout game market = advance incl pens;
    # BTTS/totals = regulation; player props = full game excl pens.
    # Remaining assumption: this maps SERIES, not match phase — a group-stage
    # KXWC match would be misclassified (fine for the current knockout
    # rounds; revisit for the next tournament's group stage).
    knockout_series: list[str] = ["KXWC"]


class MarginTotalConfig(StrictModel):
    """Bivariate-normal (margin, total) structural pricer for NFL/NBA/WNBA
    (pricing/margin_total.py). Per-game means invert from live prices; the
    sport shapes below are calibrated from RECENT seasons
    (tools/calibrate_margin_total.py, 2026-07-06 — NFL 2020-2025 closing-line
    residuals; NBA 2022-2026 & WNBA 2021-2026 team-fixed-effects residuals,
    the FE method validated against NFL's line residuals). A sport enters
    ``enabled_sports`` ONLY via an OOS gate (directive point 4).

    NFL GATE PASSED 2026-07-06 (tools/validate_margin_total_oos.py; train
    2015-2023, test 2024-2025 n=562): structural beats the shipped v1 copula
    on all three OOS metrics — hw×over 1.29275 vs 1.29293, hw×cover 0.96260
    vs 0.99940 (exact comonotone geometry vs the 0.88 approximation), triple
    1.65544 vs 1.69217.

    WNBA enabled 2026-07-06 by OPERATOR REQUEST (season live now): the
    geometry is the NFL-gated one, the WNBA shape is calibrated on 1,338
    recent games through 2026-07-05, and its rho≈0 means ml×total prices
    within noise of the incumbent v1 — the upgrade is coherent spread/
    team-total joints. Confirmation gate from prod-shadow settlements as
    data accrues. NBA stays disabled until its season approaches (odds
    source or shadow gate)."""

    enabled_sports: list[str] = ["nfl", "wnba"]
    params: dict[str, dict[str, float]] = {
        "nfl": {"sigma_margin": 12.66, "sigma_total": 13.06, "rho": 0.026},
        "nba": {"sigma_margin": 13.71, "sigma_total": 18.42, "rho": 0.000},
        "wnba": {"sigma_margin": 12.04, "sigma_total": 16.55, "rho": -0.019},
    }
    sigma_band_frac: float = 0.05   # FE-vs-line method gap on NFL was ~3%
    rho_band: float = 0.05
    # Normal approximation of discrete scores (NFL key numbers worst):
    # flat probability adder when any margin-flavored leg is present.
    discreteness_unc: dict[str, float] = {"nfl": 0.010, "nba": 0.004, "wnba": 0.005}
    misfit_uncertainty_scale: float = 1.0


class MlbRunsConfig(StrictModel):
    """NegBin runs model for MLB SGPs (pricing/mlb_runs.py). Dispersion k
    calibrated from Retrosheet final scores (tools/calibrate_mlb_runs.py,
    2026-07-06): k=3.62 on 2021-2024, k=3.63 on 2015-2019 — era-stable; the
    band covers the unmodeled home/away asymmetry (k 3.37 away / 3.91 home —
    tickers don't reveal the home side).

    GATE PASSED 2026-07-06 (tools/validate_mlb_runs_oos.py; SBR closing-odds
    archive, k from the 2015-2019 train era, test = 2021 season n=2,351):
    structural beats the shipped v1 copula on all three OOS joint-log-loss
    metrics — hw×over 1.36134 vs 1.36300, hw×runline-cover 1.00824 vs
    1.12151 (v1 has NO calibrated ml|spread for MLB and its flat 0.6 prior
    is badly wrong), triple 1.71126 vs 1.88090. Also measured: v1's pooled
    ml|total −0.05 LOSES to independence OOS (1.36300 vs 1.36209) — the
    runs grid supersedes it for same-game combos. Confirmation re-gate on
    prod-shadow settlements as they accrue."""

    enabled: bool = True
    # 2021-2025 window (Retrosheet gl2025 fetched 2026-07-06); was 3.62 on
    # 2021-2024, 3.63 on 2015-2019 — drift well inside the band.
    dispersion_k: float = 3.54
    k_band: float = 0.30
    misfit_uncertainty_scale: float = 1.0


class PricingConfig(StrictModel):
    fee: FeeConfig = Field(default_factory=FeeConfig)
    correlation: CorrelationConfig = Field(default_factory=CorrelationConfig)
    quote: QuoteConfig = Field(default_factory=QuoteConfig)
    structural: StructuralConfig = Field(default_factory=StructuralConfig)
    margin_total: MarginTotalConfig = Field(default_factory=MarginTotalConfig)
    mlb_runs: MlbRunsConfig = Field(default_factory=MlbRunsConfig)
    external_odds: ExternalOddsConfig = Field(default_factory=ExternalOddsConfig)
    max_source_disagreement: float = 0.08


class RiskConfig(StrictModel):
    """Limits + last-look + in-play policies. Enforced pre-quote AND pre-confirm."""

    max_contracts_per_quote: float = 100.0
    max_notional_per_quote_dollars: float = 500.0
    max_market_delta_contracts: float = 300.0
    max_event_delta_contracts: float = 500.0
    max_gross_notional_dollars: float = 5_000.0
    max_open_quotes: int = 20
    max_daily_loss_dollars: float = 500.0
    max_event_worst_case_loss_dollars: float = 1_000.0
    # last look
    leg_move_tolerance_cc: int = 150
    joint_move_tolerance_cc: int = 200
    max_leg_age_s: float = 2.0
    # in-play detection
    velocity_window_s: float = 5.0
    velocity_threshold_cc: int = 300
    update_count_threshold: int = 25
    in_play_cooldown_s: float = 30.0


class ObserveConfig(StrictModel):
    rfq_poll_s: float = 30.0          # REST reconciliation cadence (no seq on WS)
    would_quote_width_cc: int = 600   # stub half-spread total ($0.06) for logging only
    db_filename: str = ""             # "" = auto: combomaker-{env}.sqlite3

    def db_name_for(self, env: Env) -> str:
        # Demo and prod data must never share a store — shadow analytics on
        # prod flow would silently blend with demo bot noise.
        return self.db_filename or f"combomaker-{env.value}.sqlite3"


class AppConfig(StrictModel):
    env: Env = Env.DEMO
    mode: Mode = Mode.OBSERVE
    endpoints: EndpointsConfig
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    observe: ObserveConfig = Field(default_factory=ObserveConfig)
    data_dir: Path = Path("data")
    kill_file: Path = Path("KILL")
    # confirm_live comes only from the CLI flag --confirm-live, never from YAML:
    # a file can't accidentally arm production.
    confirm_live: bool = Field(default=False, exclude=True)

    def assert_safe_to_run(self) -> None:
        """Hardcoded production guard. Raises ProdGuardError on violation."""
        if self.env is Env.PROD and self.mode is Mode.QUOTE:
            if not self.confirm_live:
                raise ProdGuardError(
                    "quoting on production requires the explicit --confirm-live flag"
                )
            if not self.safety.prod_limits_configured:
                raise ProdGuardError(
                    "quoting on production requires safety.prod_limits_configured: true "
                    "in the prod config, set after limits are reviewed"
                )


class ProdGuardError(RuntimeError):
    pass


class ConfigError(ValueError):
    pass


def load_config(
    path: Path,
    *,
    env: Env | None = None,
    mode: Mode | None = None,
    confirm_live: bool = False,
) -> AppConfig:
    """Load YAML config; CLI values override the file's env/mode."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping: {path}")
    if "confirm_live" in raw:
        # A file must never be able to arm production; only the CLI flag can.
        raise ConfigError("confirm_live cannot be set from config files; use --confirm-live")

    if env is not None:
        raw["env"] = str(env)
    resolved_env = Env(raw.get("env", Env.DEMO))
    if mode is not None:
        raw["mode"] = str(mode)
    raw.setdefault("endpoints", EndpointsConfig.for_env(resolved_env).model_dump())

    config = AppConfig.model_validate(raw)
    return config.model_copy(update={"confirm_live": confirm_live})
