"""CLI entrypoint: ``combomaker run --env {demo,prod} --mode {observe,paper,quote}``.

The production guard lives in config (``assert_safe_to_run``); the CLI's job is
to make arming production LOUD: ``--confirm-live`` is the only way to set it,
and even then prod limits must be configured in the prod YAML.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from combomaker.exchange.auth import CredentialsError
from combomaker.ops.app import ObserveApp
from combomaker.ops.config import Env, Mode, ProdGuardError, load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="combomaker")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the maker")
    run.add_argument("--env", choices=[e.value for e in Env], default=Env.DEMO.value)
    run.add_argument("--mode", choices=[m.value for m in Mode], default=Mode.OBSERVE.value)
    run.add_argument("--config", type=Path, default=None, help="YAML config path")
    run.add_argument(
        "--confirm-live",
        action="store_true",
        help="required (with configured prod limits) to quote on production",
    )

    halt = sub.add_parser("halt", help="drop the KILL file (global halt)")
    halt.add_argument("--kill-file", type=Path, default=Path("KILL"))

    sub.add_parser("cancel-all", help="cancel all open quotes (Phase 5)")
    sub.add_parser("report", help="daily report (Phase 5)")
    return parser


def _config_path(env: Env, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    return Path("config") / f"{env.value}.yaml"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "halt":
        args.kill_file.write_text("halt requested via CLI\n", encoding="utf-8")
        print(f"KILL file written: {args.kill_file}")
        return 0

    if args.command in ("cancel-all", "report"):
        print(f"{args.command}: not implemented until Phase 5")
        return 2

    env = Env(args.env)
    mode = Mode(args.mode)
    try:
        config = load_config(
            _config_path(env, args.config),
            env=env,
            mode=mode,
            confirm_live=bool(args.confirm_live),
        )
        config.assert_safe_to_run()
    except ProdGuardError as exc:
        print(f"REFUSING TO START: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if mode is not Mode.OBSERVE:
        print(f"mode {mode} is not implemented yet (Phase 5); use --mode observe")
        return 2

    app = ObserveApp(config)
    try:
        asyncio.run(app.run())
    except CredentialsError as exc:
        print(
            f"credentials error: {exc}\n"
            "set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH (or KALSHI_PRIVATE_KEY_PEM)",
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        print("interrupted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
