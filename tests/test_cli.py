from combomaker.ops.cli import main


def test_prod_quote_without_confirm_live_refuses(capsys) -> None:  # type: ignore[no-untyped-def]
    code = main(["run", "--env", "prod", "--mode", "quote"])
    assert code == 3
    assert "REFUSING TO START" in capsys.readouterr().err


def test_prod_quote_with_flag_still_blocked_by_limits(capsys) -> None:  # type: ignore[no-untyped-def]
    # prod.yaml ships with prod_limits_configured: false — flag alone is not enough
    code = main(["run", "--env", "prod", "--mode", "quote", "--confirm-live"])
    assert code == 3
    assert "limits" in capsys.readouterr().err


def test_quote_mode_blocked_while_conventions_unverified(capsys) -> None:  # type: ignore[no-untyped-def]
    # Even on DEMO: quote mode requires the Phase 2.5 ground-truth fixture.
    code = main(["run", "--env", "demo", "--mode", "quote"])
    assert code == 3
    err = capsys.readouterr().err
    assert "ground-truth" in err or "whitelist" in err


def test_report_without_store_exits_2(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["report", "--db", "does/not/exist.sqlite3"]) == 2


def test_halt_writes_kill_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    kill = tmp_path / "KILL"
    assert main(["halt", "--kill-file", str(kill)]) == 0
    assert kill.exists()
