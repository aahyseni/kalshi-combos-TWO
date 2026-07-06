from pathlib import Path

import pytest

from combomaker.ops.dotenv import load_dotenv


def write_env(tmp_path: Path, body: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def allow_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    # These tests exercise the loader itself with harmless CM_TEST_* vars and
    # explicit tmp paths — lift the suite-wide hermetic guard locally.
    monkeypatch.delenv("COMBOMAKER_NO_DOTENV", raising=False)


def test_loads_missing_vars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CM_TEST_A", raising=False)
    path = write_env(tmp_path, "CM_TEST_A=hello\n# comment\n\nCM_TEST_B='quoted'\n")
    monkeypatch.delenv("CM_TEST_B", raising=False)
    loaded = load_dotenv(path)
    import os

    assert os.environ["CM_TEST_A"] == "hello"
    assert os.environ["CM_TEST_B"] == "quoted"
    assert set(loaded) == {"CM_TEST_A", "CM_TEST_B"}
    monkeypatch.delenv("CM_TEST_A")
    monkeypatch.delenv("CM_TEST_B")


def test_never_overrides_existing_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CM_TEST_C", "real")
    path = write_env(tmp_path, "CM_TEST_C=from_file\n")
    loaded = load_dotenv(path)
    import os

    assert os.environ["CM_TEST_C"] == "real"
    assert loaded == []


def test_missing_file_is_silent(tmp_path: Path) -> None:
    assert load_dotenv(tmp_path / "absent.env") == []


def test_malformed_lines_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CM_TEST_D", raising=False)
    path = write_env(tmp_path, "no_equals_line\n=novalue\nCM_TEST_D=ok\n")
    loaded = load_dotenv(path)
    assert loaded == ["CM_TEST_D"]
    monkeypatch.delenv("CM_TEST_D")


def test_env_example_documents_all_credential_names() -> None:
    example = Path(__file__).resolve().parents[1] / ".env.example"
    text = example.read_text(encoding="utf-8")
    for name in (
        "KALSHI_API_KEY_ID",
        "KALSHI_PRIVATE_KEY_PATH",
        "KALSHI_REQUESTER_API_KEY_ID",
        "KALSHI_REQUESTER_PRIVATE_KEY_PATH",
        "SPORTSGAMEODDS_API_KEY",
    ):
        assert name in text
