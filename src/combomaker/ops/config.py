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
        "moneyline|player_goal": 0.25,     # hand prior (uncalibrated)
        "btts|moneyline": -0.17,           # measured −0.20/−0.14 (home/away pooled)
        "moneyline|total": 0.23,           # measured +0.28/+0.18 (side asymmetry)
        "total|total": 0.95,               # nested thresholds: measured at the cap
        "corners|total": 0.00,             # measured −0.04 ≈ 0
        "btts|corners": 0.00,              # measured +0.01 ≈ 0
        "extras|total": 0.50,              # hand prior (MLB, uncalibrated)
    }
    typed_rho_uncertainty: float = 0.15
    untyped_rho_uncertainty: float = 0.30
    # Tighter bands for pairs backed by the n≈9k calibration; ml|total's band
    # also covers its measured home/away asymmetry. Uncalibrated pairs keep
    # the defaults above.
    pair_rho_uncertainty: dict[str, float] = {
        "moneyline|moneyline": 0.04,
        "btts|total": 0.08,
        "btts|moneyline": 0.08,
        "moneyline|total": 0.10,
        "total|total": 0.04,
        "corners|total": 0.08,
        "btts|corners": 0.08,
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


class PricingConfig(StrictModel):
    fee: FeeConfig = Field(default_factory=FeeConfig)
    correlation: CorrelationConfig = Field(default_factory=CorrelationConfig)
    quote: QuoteConfig = Field(default_factory=QuoteConfig)
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
