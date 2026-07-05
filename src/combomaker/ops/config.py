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


class ObserveConfig(StrictModel):
    rfq_poll_s: float = 30.0          # REST reconciliation cadence (no seq on WS)
    would_quote_width_cc: int = 600   # stub half-spread total ($0.06) for logging only
    db_filename: str = "combomaker.sqlite3"


class AppConfig(StrictModel):
    env: Env = Env.DEMO
    mode: Mode = Mode.OBSERVE
    endpoints: EndpointsConfig
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
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
