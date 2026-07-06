"""Minimal .env loader — secrets stay in an untracked file, never in YAML.

Looked up at CLI startup: ``.env`` in the current working directory (the repo
root when you run ``uv run combomaker ...``). Existing environment variables
always win — the file only fills gaps, so CI/prod can still inject real env.
``.env`` is gitignored; ``.env.example`` documents the expected names.

Deliberately dependency-free: KEY=VALUE lines, ``#`` comments, optional
surrounding quotes. No interpolation, no multiline values — a private key
belongs in its own ``.pem`` file referenced by ``KALSHI_PRIVATE_KEY_PATH``,
not pasted into .env.
"""

from __future__ import annotations

import os
from pathlib import Path

from combomaker.ops.logging import get_logger

log = get_logger(__name__)


def load_dotenv(path: Path | None = None) -> list[str]:
    """Load ``path`` (default ``./.env``) into os.environ; returns the names
    actually set (existing variables are never overwritten)."""
    if os.environ.get("COMBOMAKER_NO_DOTENV"):
        return []  # hermetic-test guard: unit tests must never see real creds
    env_path = path or Path(".env")
    if not env_path.is_file():
        return []
    loaded: list[str] = []
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip("'\"")
        if not name or name in os.environ:
            continue
        os.environ[name] = value
        loaded.append(name)
    if loaded:
        # Names only — values are secrets and never reach a log line.
        log.info("dotenv_loaded", path=str(env_path), names=loaded)
    return loaded
