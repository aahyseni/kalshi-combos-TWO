"""THE EXTENDED TIER — V10 PRICING ORDER + V11 THROUGHPUT FLOORS.

    .venv/Scripts/python.exe -m tools.vitals.gate_ext

Same machinery, same derived inputs, same table as ``tools.vitals.gate``; it is
a separate entry point ONLY so that adding these two checks cannot collide with
a concurrent workflow editing ``gate.py``. Fold ``v_pricing.CHECKS`` into
``gate.ALL_CHECKS`` when the tree is quiet and delete this file.
"""

from __future__ import annotations

import sys
import time

from tools.vitals import gate as _gate  # imports do the hermetic env strip
from tools.vitals import v_pricing
from tools.vitals.derive import derive
from tools.vitals.result import run_check


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass
    if "--verbose" not in args:
        _gate._mute_bot_logging()  # noqa: SLF001
    only = {a.upper() for a in args if a.upper().startswith("V")}
    derived = derive()
    selected = [(v, fn) for v, fn in v_pricing.CHECKS if not only or v.key in only]
    t0 = time.perf_counter()
    results = [run_check(v, fn, derived) for v, fn in selected]
    print(_gate.render(results, derived, time.perf_counter() - t0, "extended"))
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
