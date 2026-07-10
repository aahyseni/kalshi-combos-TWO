"""MLB same-player cross-stat conditional table + implied-rho helper (DO-2).

SOURCE OF TRUTH: the 2026-07-10 same-player measurement pass (job 24844262) —
1,033,852 batter-games 2005-25, PA>=1, parsed_full x parsed_hrr 1:1 join;
HRR = H + R + RBI (strict). Key ``(famA, rungA, famB, rungB)`` maps to
``(P(famB >= rungB | famA >= rungA), n = count of famA >= rungA games,
marker)``. A Kalshi prop line suffix ``-N`` means "N or more" (floor_strike
N-0.5), so rungs here ARE the ticker suffixes.

- marker ``'exact'``: ARITHMETIC containment (famA>=rungA implies famB>=rungB
  — e.g. one HR is 1 hit + 4 TB + >=1 R + >=1 RBI), verified empirically
  == 1.0 pooled AND on the 2021-25 era split. Exact cells drive the
  relationships.py containment / impossible verdicts — never a copula rho.
- marker ``'measured'``: a PARTIAL implication. Cells with
  n >= MIN_CONDITIONAL_N price via the conditional table (joint =
  P(conditioning leg) x p_cond); sgp.py converts that joint into the
  equivalent Gaussian-copula rho at the LIVE marginals. Weaker cells decline
  UNKNOWN upstream (relationships.py) — never the distinct-player [D] rhos.

The table as DELIVERED by the measurement agent was TRUNCATED mid-cell after
``('hrr', 2, 'hit', 3)`` — the ``('hrr', 2, 'hr', 1)`` cell arrived with an
incomplete value/no n, and every later conditioning row (``('hrr', >=2, ...)``
except the three hit cells, all ``('tb', ...)`` rows) is ABSENT. Fail-closed
by construction: a same-player pair with no usable cell in EITHER direction
declines UNKNOWN in relationships.py. Extend this table only by re-running
the measurement export — never by hand.
"""

from __future__ import annotations

import numpy as np

from combomaker.pricing.copula import gaussian_copula_joint_prob
from combomaker.pricing.legtypes import LegType

# (famA, rungA, famB, rungB) -> (P(famB>=rungB | famA>=rungA), n, marker)
_Key = tuple[str, int, str, int]
_CellValue = tuple[float, int, str]

SAME_PLAYER_CONDITIONALS: dict[_Key, _CellValue] = {
    ("hit", 1, "hr", 1): (0.17209235086525787, 587975, "measured"),
    ("hit", 1, "hr", 2): (0.01053616225179642, 587975, "measured"),
    ("hit", 1, "hrr", 2): (0.7335583995918193, 587975, "measured"),
    ("hit", 1, "hrr", 3): (0.4692954632424848, 587975, "measured"),
    ("hit", 1, "hrr", 4): (0.27780092691015773, 587975, "measured"),
    ("hit", 1, "hrr", 5): (0.1563229729155151, 587975, "measured"),
    ("hit", 1, "tb", 2): (0.5797457374888388, 587975, "measured"),
    ("hit", 1, "tb", 3): (0.3321892937624899, 587975, "measured"),
    ("hit", 1, "tb", 4): (0.22755389259747438, 587975, "measured"),
    ("hit", 1, "tb", 5): (0.10891279391130575, 587975, "measured"),
    ("hit", 1, "tb", 6): (0.05331349121986479, 587975, "measured"),
    ("hit", 2, "hr", 1): (0.2550174817770709, 212507, "measured"),
    ("hit", 2, "hr", 2): (0.029151980875924088, 212507, "measured"),
    ("hit", 2, "hrr", 2): (1.0, 212507, "exact"),
    ("hit", 2, "hrr", 3): (0.8483155848983798, 212507, "measured"),
    ("hit", 2, "hrr", 4): (0.6098010889053066, 212507, "measured"),
    ("hit", 2, "hrr", 5): (0.3852014286588207, 212507, "measured"),
    ("hit", 2, "tb", 2): (1.0, 212507, "exact"),
    ("hit", 2, "tb", 3): (0.6647639842452249, 212507, "measured"),
    ("hit", 2, "tb", 4): (0.4084712503588117, 212507, "measured"),
    ("hit", 2, "tb", 5): (0.3013453674467194, 212507, "measured"),
    ("hit", 2, "tb", 6): (0.1475104349503781, 212507, "measured"),
    ("hit", 3, "hr", 1): (0.3291162790697674, 48375, "measured"),
    ("hit", 3, "hr", 2): (0.05647545219638243, 48375, "measured"),
    ("hit", 3, "hrr", 2): (1.0, 48375, "exact"),
    ("hit", 3, "hrr", 3): (1.0, 48375, "exact"),
    ("hit", 3, "hrr", 4): (0.9231834625322998, 48375, "measured"),
    ("hit", 3, "hrr", 5): (0.7508217054263566, 48375, "measured"),
    ("hit", 3, "tb", 2): (1.0, 48375, "exact"),
    ("hit", 3, "tb", 3): (1.0, 48375, "exact"),
    ("hit", 3, "tb", 4): (0.752062015503876, 48375, "measured"),
    ("hit", 3, "tb", 5): (0.5038759689922481, 48375, "measured"),
    ("hit", 3, "tb", 6): (0.38342118863049096, 48375, "measured"),
    ("hr", 1, "hit", 1): (1.0, 101186, "exact"),
    ("hr", 1, "hit", 2): (0.5355780443934932, 101186, "measured"),
    ("hr", 1, "hit", 3): (0.15734390133022355, 101186, "measured"),
    ("hr", 1, "hrr", 2): (1.0, 101186, "exact"),
    ("hr", 1, "hrr", 3): (1.0, 101186, "exact"),
    ("hr", 1, "hrr", 4): (0.7695333346510387, 101186, "measured"),
    ("hr", 1, "hrr", 5): (0.529391417785069, 101186, "measured"),
    ("hr", 1, "tb", 2): (1.0, 101186, "exact"),
    ("hr", 1, "tb", 3): (1.0, 101186, "exact"),
    ("hr", 1, "tb", 4): (1.0, 101186, "exact"),
    ("hr", 1, "tb", 5): (0.5355780443934932, 101186, "measured"),
    ("hr", 1, "tb", 6): (0.2827762733975056, 101186, "measured"),
    ("hr", 2, "hit", 1): (1.0, 6195, "exact"),
    ("hr", 2, "hit", 2): (1.0, 6195, "exact"),
    ("hr", 2, "hit", 3): (0.441000807102502, 6195, "measured"),
    ("hr", 2, "hrr", 2): (1.0, 6195, "exact"),
    ("hr", 2, "hrr", 3): (1.0, 6195, "exact"),
    ("hr", 2, "hrr", 4): (1.0, 6195, "exact"),
    ("hr", 2, "hrr", 5): (1.0, 6195, "exact"),
    ("hr", 2, "tb", 2): (1.0, 6195, "exact"),
    ("hr", 2, "tb", 3): (1.0, 6195, "exact"),
    ("hr", 2, "tb", 4): (1.0, 6195, "exact"),
    ("hr", 2, "tb", 5): (1.0, 6195, "exact"),
    ("hr", 2, "tb", 6): (1.0, 6195, "exact"),
    ("hrr", 2, "hit", 1): (0.9857186279461472, 437563, "measured"),
    ("hrr", 2, "hit", 2): (0.48566035062379587, 437563, "measured"),
    ("hrr", 2, "hit", 3): (0.11055550857819331, 437563, "measured"),
}

# A measured cell prices only when its conditioning sample is at least this
# large (operator-approved policy). Notably ('hit', 3, ...) rows sit at
# n=48,375 — just UNDER the bar — so HIT-3-conditioned cells never price;
# their reverse directions (n=101,186+) may.
MIN_CONDITIONAL_N = 50_000

# rho half-width for conditional-priced same-player pairs. Sampling error at
# n >= 50k is negligible (SE of p_cond < 0.005); the band prices the
# POOLED -> single-player TRANSFER: the conditional coupling varies with the
# batter's profile even after the live marginals absorb his rate differences.
# MEASURE-BEFORE-TIGHTEN: the per-player spread of the implied rho has not
# been measured yet — do not narrow this without that measurement.
SAME_PLAYER_RHO_BAND = 0.12

# The batter-stat leg types the same-player table covers, mapped to the
# table's family names. PLAYER_KS is deliberately ABSENT: a starter's Ks and
# a batter's stats are DIFFERENT entities, so their ticker player segments
# can never match — same-player KS x batter is structurally unreachable and
# needs no branch anywhere.
BATTER_FAMILIES: dict[LegType, str] = {
    LegType.PLAYER_HIT: "hit",
    LegType.PLAYER_HR: "hr",
    LegType.PLAYER_TB: "tb",
    LegType.PLAYER_HRR: "hrr",
}


def is_exact(fam_a: str, rung_a: int, fam_b: str, rung_b: int) -> bool:
    """True iff the DIRECTIONAL cell (famA>=rungA implies famB>=rungB) is an
    arithmetic containment ('exact' marker). Direction matters — callers must
    consult both orderings. Missing cells are False (fail-closed): an
    implication we did not verify is never treated as exact."""
    cell = SAME_PLAYER_CONDITIONALS.get((fam_a, rung_a, fam_b, rung_b))
    return cell is not None and cell[2] == "exact"


def strongest_measured_direction(
    fam_a: str, rung_a: int, fam_b: str, rung_b: int
) -> tuple[bool, float, int] | None:
    """The strongest usable MEASURED cell for an unordered same-player pair:
    ``(a_conditions, p_cond, n)`` with ``a_conditions`` True when the
    (fam_a, rung_a) leg is the conditioning event. Both directions encode the
    SAME joint count, so preferring the larger-n direction is a pure precision
    choice, not a model choice. 'exact' cells never qualify (the containment
    branch in relationships.py owns them); cells under MIN_CONDITIONAL_N never
    qualify (those pairs decline UNKNOWN upstream). None when neither
    direction is usable — the caller must fail closed, never guess."""
    best: tuple[bool, float, int] | None = None
    directions: tuple[tuple[bool, _Key], ...] = (
        (True, (fam_a, rung_a, fam_b, rung_b)),
        (False, (fam_b, rung_b, fam_a, rung_a)),
    )
    for a_conditions, cell_key in directions:
        cell = SAME_PLAYER_CONDITIONALS.get(cell_key)
        if cell is None or cell[2] != "measured" or cell[1] < MIN_CONDITIONAL_N:
            continue
        if best is None or cell[1] > best[2]:
            best = (a_conditions, cell[0], cell[1])
    return best


# Cap matching sgp._clamp: the copula never prices |rho| above 0.95, so the
# solve is bounded to the same interval (its edges are the "closest achievable
# coupling" answers when live marginals contradict the pooled conditional).
_RHO_CAP = 0.95
_SOLVE_ITERATIONS = 30
_SOLVE_TOL = 1e-10


def implied_rho(p_cond_event: float, p_other: float, p_cond: float) -> float | None:
    """The Gaussian-copula rho at which P(both YES) equals the conditional-table
    joint ``p_cond_event * p_cond`` for the LIVE marginals ``(p_cond_event,
    p_other)``. Monotone bisection on the SHIPPED copula integrator — the same
    code path the engine prices with — so the engine's joint reproduces the
    table's joint to integrator tolerance, and one rho prices all four YES/NO
    sign cases of the 2x2 consistently. Live marginals can contradict the
    pooled conditional (the target may exceed what any coupling allows); the
    solve then returns the capped +-0.95 — the closest achievable coupling,
    mirroring the Frechet clamp the pricer applies anyway. None on degenerate
    marginals or an out-of-range conditional (never price on garbage)."""
    if not (0.0 < p_cond_event < 1.0 and 0.0 < p_other < 1.0):
        return None
    if not 0.0 <= p_cond <= 1.0:
        return None
    target = p_cond_event * p_cond
    ps = [p_cond_event, p_other]

    def joint(rho: float) -> float:
        corr = np.array([[1.0, rho], [rho, 1.0]], dtype=np.float64)
        return gaussian_copula_joint_prob(ps, corr)

    lo, hi = -_RHO_CAP, _RHO_CAP
    if target <= joint(lo):
        return lo
    if target >= joint(hi):
        return hi
    for _ in range(_SOLVE_ITERATIONS):
        mid = (lo + hi) / 2.0
        p_mid = joint(mid)
        if abs(p_mid - target) < _SOLVE_TOL:
            return mid
        if p_mid < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0
