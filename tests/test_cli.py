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


def test_quote_mode_blocked_when_conventions_unverified(  # type: ignore[no-untyped-def]
    capsys, monkeypatch, tmp_path
) -> None:
    # Pin the gate under test: force the fixture path somewhere empty so this
    # test stays a refusal test even though the repo's real fixture is
    # promoted. (Its old form went LIVE when the real gates opened.)
    import combomaker.core.conventions as conventions

    monkeypatch.setattr(conventions, "DEFAULT_FIXTURE_PATH", tmp_path / "absent.json")
    code = main(["run", "--env", "demo", "--mode", "quote"])
    assert code == 3
    assert "ground-truth" in capsys.readouterr().err


def test_quote_mode_blocked_without_whitelist(capsys, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Conventions verified (real promoted fixture) but no whitelist ⇒ refuse
    # at construction, before any credentials or network.
    config = tmp_path / "demo.yaml"
    config.write_text("env: demo\nmode: quote\n", encoding="utf-8")
    code = main(["run", "--env", "demo", "--mode", "quote", "--config", str(config)])
    assert code == 3
    assert "whitelist" in capsys.readouterr().err


def test_report_without_store_exits_2(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["report", "--db", "does/not/exist.sqlite3"]) == 2


def test_halt_writes_kill_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    kill = tmp_path / "KILL"
    assert main(["halt", "--kill-file", str(kill)]) == 0
    assert kill.exists()
