from pathlib import Path

import pytest

from combomaker.ops.config import (
    AppConfig,
    ConfigError,
    EndpointsConfig,
    Env,
    Mode,
    ProdGuardError,
    load_config,
)


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


class TestLoading:
    def test_defaults_to_demo_observe(self, tmp_path: Path) -> None:
        config = load_config(write_config(tmp_path, "{}"))
        assert config.env is Env.DEMO
        assert config.mode is Mode.OBSERVE
        assert "demo.kalshi.co" in config.endpoints.rest_base_url

    def test_prod_endpoints_derived_from_env(self, tmp_path: Path) -> None:
        config = load_config(write_config(tmp_path, "env: prod\n"))
        assert config.endpoints.rest_base_url == "https://external-api.kalshi.com/trade-api/v2"
        assert config.endpoints.ws_url == "wss://external-api-ws.kalshi.com/trade-api/ws/v2"

    def test_cli_env_override_wins_and_drives_endpoints(self, tmp_path: Path) -> None:
        config = load_config(write_config(tmp_path, "env: demo\n"), env=Env.PROD)
        assert config.env is Env.PROD
        assert ".com" in config.endpoints.rest_base_url

    def test_unknown_keys_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            load_config(write_config(tmp_path, "max_dialy_loss: 100\n"))

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            load_config(tmp_path / "nope.yaml")


class TestProdGuard:
    def base(self, env: Env, mode: Mode, *, confirm: bool, limits: bool) -> AppConfig:
        return AppConfig(
            env=env,
            mode=mode,
            endpoints=EndpointsConfig.for_env(env),
            safety={"prod_limits_configured": limits},  # type: ignore[arg-type]
            confirm_live=confirm,
        )

    def test_demo_quote_allowed(self) -> None:
        self.base(Env.DEMO, Mode.QUOTE, confirm=False, limits=False).assert_safe_to_run()

    def test_prod_observe_allowed(self) -> None:
        self.base(Env.PROD, Mode.OBSERVE, confirm=False, limits=False).assert_safe_to_run()

    def test_prod_quote_blocked_without_confirm_live(self) -> None:
        with pytest.raises(ProdGuardError, match="confirm-live"):
            self.base(Env.PROD, Mode.QUOTE, confirm=False, limits=True).assert_safe_to_run()

    def test_prod_quote_blocked_without_limits(self) -> None:
        with pytest.raises(ProdGuardError, match="limits"):
            self.base(Env.PROD, Mode.QUOTE, confirm=True, limits=False).assert_safe_to_run()

    def test_prod_quote_allowed_with_both(self) -> None:
        self.base(Env.PROD, Mode.QUOTE, confirm=True, limits=True).assert_safe_to_run()

    def test_confirm_live_never_comes_from_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "sneaky.yaml"
        path.write_text("env: prod\nmode: quote\nconfirm_live: true\n", encoding="utf-8")
        with pytest.raises(ValueError):
            # extra=forbid: confirm_live in YAML is an unknown key, not an override
            load_config(path)

    def test_repo_config_files_load(self) -> None:
        repo_config = Path(__file__).resolve().parents[1] / "config"
        demo = load_config(repo_config / "demo.yaml")
        assert demo.env is Env.DEMO
        prod = load_config(repo_config / "prod.yaml")
        assert prod.env is Env.PROD
        assert prod.safety.prod_limits_configured is False
