"""Hermetic guard for the unit-test suite.

Lesson learned live (2026-07-05): a CLI test asserting "quote mode refuses to
start" went LIVE against demo the moment the conventions fixture was promoted
and a whitelist landed in demo.yaml — the gates it relied on opened, and
main()'s .env loading handed it real credentials. Unit tests must never be one
config change away from the network: strip every credential var and disable
.env loading for everything outside tests/integration.
"""

from __future__ import annotations

import pytest

_SENSITIVE = (
    "KALSHI_API_KEY_ID",
    "KALSHI_PRIVATE_KEY_PATH",
    "KALSHI_PRIVATE_KEY_PEM",
    "KALSHI_REQUESTER_API_KEY_ID",
    "KALSHI_REQUESTER_PRIVATE_KEY_PATH",
    "KALSHI_REQUESTER_PRIVATE_KEY_PEM",
    "SPORTSGAMEODDS_API_KEY",
)


@pytest.fixture(autouse=True)
def hermetic_env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    if "tests/integration" in str(request.node.fspath).replace("\\", "/"):
        yield
        return
    for name in _SENSITIVE:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COMBOMAKER_NO_DOTENV", "1")
    yield
