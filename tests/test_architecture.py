"""Architecture guards.

Devig is quarantined: Kalshi-sourced leg probabilities must never pass through
a margin-removal model (CLAUDE.md decision #8). The only code allowed to import
``combomaker.pricing.devig`` is an external ``OddsSource`` adapter under
``combomaker.pricing.sources``. Everything Kalshi-side that needs simplex
renormalization uses ``combomaker.pricing.normalize`` instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
PACKAGE_ROOT = SRC_ROOT / "combomaker"

DEVIG_MODULE = "combomaker.pricing.devig"
ALLOWED_IMPORTER_PREFIXES = ("combomaker.pricing.sources",)


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC_ROOT).with_suffix("")
    parts = rel.parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_relative(importer: str, level: int, module: str | None) -> str:
    """Resolve a ``from ...x import y`` to an absolute module path."""
    base = importer.split(".")
    # level=1 strips the module name itself (current package), each extra level
    # strips one more package.
    base = base[: len(base) - level]
    if module:
        base.append(module)
    return ".".join(base)


def _imported_targets(path: Path) -> set[str]:
    """Every absolute module path this file imports, plus from-import members."""
    importer = _module_name(path)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = _resolve_relative(importer, node.level, node.module)
            else:
                base = node.module or ""
            targets.add(base)
            # `from pkg import member` may pull in a submodule: record pkg.member too
            targets.update(f"{base}.{alias.name}" for alias in node.names)
    return targets


def _all_source_modules() -> list[tuple[str, Path]]:
    return sorted(
        (_module_name(path), path) for path in PACKAGE_ROOT.rglob("*.py")
    )


def test_source_tree_is_nonempty() -> None:
    modules = _all_source_modules()
    assert any(name == DEVIG_MODULE for name, _ in modules), "devig module moved? update guard"


def test_devig_only_importable_from_external_odds_adapters() -> None:
    offenders: list[str] = []
    for name, path in _all_source_modules():
        if name == DEVIG_MODULE or name.startswith(ALLOWED_IMPORTER_PREFIXES):
            continue
        imports = _imported_targets(path)
        if any(t == DEVIG_MODULE or t.startswith(DEVIG_MODULE + ".") for t in imports):
            offenders.append(name)
    assert not offenders, (
        f"devig imported outside external OddsSource adapters: {offenders}. "
        "Kalshi-sourced probabilities must never pass through devig "
        "(CLAUDE.md decision #8); use combomaker.pricing.normalize instead."
    )


def test_normalize_module_does_not_depend_on_devig() -> None:
    imports = _imported_targets(PACKAGE_ROOT / "pricing" / "normalize.py")
    assert not any(t.startswith(DEVIG_MODULE) for t in imports)
