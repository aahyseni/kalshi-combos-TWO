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
from typing import Any

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

    gt = sub.add_parser(
        "ground-truth",
        help="Phase 2.5: record real RFQ round trips on DEMO into the "
        "ground-truth fixture (needs maker + requester demo credentials)",
    )
    gt.add_argument("--market", required=True, help="liquid open demo market ticker")
    gt.add_argument("--contracts", default="1.00", help="contracts_fp per RFQ")
    gt.add_argument(
        "--out", type=Path, default=Path("tests") / "fixtures" / "ground_truth"
    )

    cancel = sub.add_parser("cancel-all", help="cancel every open quote via REST")
    cancel.add_argument("--env", choices=[e.value for e in Env], default=Env.DEMO.value)

    report = sub.add_parser("report", help="daily report from the local store")
    report.add_argument("--env", choices=[e.value for e in Env], default=Env.DEMO.value)
    report.add_argument("--db", type=Path, default=None, help="sqlite path override")
    return parser


def _config_path(env: Env, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    return Path("config") / f"{env.value}.yaml"


async def _report(env: Env, db_override: Path | None) -> int:
    from combomaker.core.clock import SystemClock
    from combomaker.ops.persistence import Store
    from combomaker.ops.report import build_report, format_report

    config = load_config(_config_path(env, None), env=env)
    db_path = db_override or (config.data_dir / config.observe.db_filename)
    if not db_path.exists():
        print(f"no store at {db_path} — run observe/paper first", file=sys.stderr)
        return 2
    store = await Store.open(db_path, SystemClock())
    try:
        report = await build_report(store, env=str(env))
    finally:
        await store.close()
    print(format_report(report))
    return 0


async def _cancel_all(env: Env) -> int:
    from combomaker.core.clock import SystemClock
    from combomaker.exchange.auth import Credentials, RequestSigner
    from combomaker.exchange.rest import KalshiApiError, KalshiRestClient

    config = load_config(_config_path(env, None), env=env)
    signer = RequestSigner(Credentials.from_env(), SystemClock())
    cancelled = failed = 0
    async with KalshiRestClient(config.endpoints.rest_base_url, signer) as rest:
        payload = await rest.get_quotes(status="open")
        quotes = payload.get("quotes", []) or []
        for quote in quotes:
            quote_id = str(quote.get("id") or quote.get("quote_id") or "")
            if not quote_id:
                continue
            try:
                await rest.delete_quote(quote_id)
                cancelled += 1
            except KalshiApiError as exc:
                failed += 1
                print(f"delete {quote_id} failed: {exc}", file=sys.stderr)
    print(f"cancelled {cancelled} open quotes ({failed} failures)")
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "halt":
        args.kill_file.write_text("halt requested via CLI\n", encoding="utf-8")
        print(f"KILL file written: {args.kill_file}")
        return 0

    if args.command == "report":
        return asyncio.run(_report(Env(args.env), args.db))

    if args.command == "cancel-all":
        try:
            return asyncio.run(_cancel_all(Env(args.env)))
        except CredentialsError as exc:
            print(f"credentials error: {exc}", file=sys.stderr)
            return 2

    if args.command == "ground-truth":
        from combomaker.ops.ground_truth import GroundTruthError, run_ground_truth

        config = load_config(_config_path(Env.DEMO, None), env=Env.DEMO)
        try:
            derived = asyncio.run(
                run_ground_truth(
                    rest_base_url=config.endpoints.rest_base_url,
                    market_ticker=args.market,
                    contracts_fp=args.contracts,
                    out_dir=args.out,
                )
            )
        except (GroundTruthError, CredentialsError) as exc:
            print(f"ground-truth aborted: {exc}", file=sys.stderr)
            return 2
        print(
            f"recordings + {derived} written.\n"
            "REVIEW the evidence, then promote conventions.derived.json to "
            "conventions.json in the same directory to mark conventions verified."
        )
        return 0

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

    app: ObserveApp | Any
    if mode is Mode.OBSERVE:
        app = ObserveApp(config)
    else:
        from combomaker.core.conventions import ConventionsUnverifiedError
        from combomaker.ops.quote_app import QuoteApp

        try:
            app = QuoteApp(config)
        except (ConventionsUnverifiedError, RuntimeError) as exc:
            print(f"REFUSING TO START: {exc}", file=sys.stderr)
            return 3
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
