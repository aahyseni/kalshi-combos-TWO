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
    # FULL 142-cell table (2026-07-10 measurement, 1,033,852 batter-games
    # 2005-25, gamelog-validated; the original wiring received a truncated
    # 60-cell copy — restored in full from the measurement artifact).
    # 33 exact cells verified empirically == 1.0 pooled AND 2021-25.
    # +7 ('tb', N, 'hrr', 1) exact cells (WIRE-2 re-run, 2026-07-11 — see the
    # provenance block at the end of the table) = 149 cells / 40 exact.
    # +84 M2 zero-gaps cells (2026-07-12 re-run, provenance block below)
    # = 233 cells / 77 exact.
    ('hit', 1, 'hr', 1): (0.17209235086525787, 587975, 'measured'),
    ('hit', 1, 'hr', 2): (0.01053616225179642, 587975, 'measured'),
    ('hit', 1, 'hrr', 2): (0.7335583995918193, 587975, 'measured'),
    ('hit', 1, 'hrr', 3): (0.4692954632424848, 587975, 'measured'),
    ('hit', 1, 'hrr', 4): (0.27780092691015773, 587975, 'measured'),
    ('hit', 1, 'hrr', 5): (0.1563229729155151, 587975, 'measured'),
    ('hit', 1, 'tb', 2): (0.5797457374888388, 587975, 'measured'),
    ('hit', 1, 'tb', 3): (0.3321892937624899, 587975, 'measured'),
    ('hit', 1, 'tb', 4): (0.22755389259747438, 587975, 'measured'),
    ('hit', 1, 'tb', 5): (0.10891279391130575, 587975, 'measured'),
    ('hit', 1, 'tb', 6): (0.05331349121986479, 587975, 'measured'),
    ('hit', 2, 'hr', 1): (0.2550174817770709, 212507, 'measured'),
    ('hit', 2, 'hr', 2): (0.029151980875924088, 212507, 'measured'),
    ('hit', 2, 'hrr', 2): (1.0, 212507, 'exact'),
    ('hit', 2, 'hrr', 3): (0.8483155848983798, 212507, 'measured'),
    ('hit', 2, 'hrr', 4): (0.6098010889053066, 212507, 'measured'),
    ('hit', 2, 'hrr', 5): (0.3852014286588207, 212507, 'measured'),
    ('hit', 2, 'tb', 2): (1.0, 212507, 'exact'),
    ('hit', 2, 'tb', 3): (0.6647639842452249, 212507, 'measured'),
    ('hit', 2, 'tb', 4): (0.4084712503588117, 212507, 'measured'),
    ('hit', 2, 'tb', 5): (0.3013453674467194, 212507, 'measured'),
    ('hit', 2, 'tb', 6): (0.1475104349503781, 212507, 'measured'),
    ('hit', 3, 'hr', 1): (0.3291162790697674, 48375, 'measured'),
    ('hit', 3, 'hr', 2): (0.05647545219638243, 48375, 'measured'),
    ('hit', 3, 'hrr', 2): (1.0, 48375, 'exact'),
    ('hit', 3, 'hrr', 3): (1.0, 48375, 'exact'),
    ('hit', 3, 'hrr', 4): (0.9231834625322998, 48375, 'measured'),
    ('hit', 3, 'hrr', 5): (0.7508217054263566, 48375, 'measured'),
    ('hit', 3, 'tb', 2): (1.0, 48375, 'exact'),
    ('hit', 3, 'tb', 3): (1.0, 48375, 'exact'),
    ('hit', 3, 'tb', 4): (0.752062015503876, 48375, 'measured'),
    ('hit', 3, 'tb', 5): (0.5038759689922481, 48375, 'measured'),
    ('hit', 3, 'tb', 6): (0.38342118863049096, 48375, 'measured'),
    ('hr', 1, 'hit', 1): (1.0, 101186, 'exact'),
    ('hr', 1, 'hit', 2): (0.5355780443934932, 101186, 'measured'),
    ('hr', 1, 'hit', 3): (0.15734390133022355, 101186, 'measured'),
    ('hr', 1, 'hrr', 2): (1.0, 101186, 'exact'),
    ('hr', 1, 'hrr', 3): (1.0, 101186, 'exact'),
    ('hr', 1, 'hrr', 4): (0.7695333346510387, 101186, 'measured'),
    ('hr', 1, 'hrr', 5): (0.529391417785069, 101186, 'measured'),
    ('hr', 1, 'tb', 2): (1.0, 101186, 'exact'),
    ('hr', 1, 'tb', 3): (1.0, 101186, 'exact'),
    ('hr', 1, 'tb', 4): (1.0, 101186, 'exact'),
    ('hr', 1, 'tb', 5): (0.5355780443934932, 101186, 'measured'),
    ('hr', 1, 'tb', 6): (0.2827762733975056, 101186, 'measured'),
    ('hr', 2, 'hit', 1): (1.0, 6195, 'exact'),
    ('hr', 2, 'hit', 2): (1.0, 6195, 'exact'),
    ('hr', 2, 'hit', 3): (0.441000807102502, 6195, 'measured'),
    ('hr', 2, 'hrr', 2): (1.0, 6195, 'exact'),
    ('hr', 2, 'hrr', 3): (1.0, 6195, 'exact'),
    ('hr', 2, 'hrr', 4): (1.0, 6195, 'exact'),
    ('hr', 2, 'hrr', 5): (1.0, 6195, 'exact'),
    ('hr', 2, 'tb', 2): (1.0, 6195, 'exact'),
    ('hr', 2, 'tb', 3): (1.0, 6195, 'exact'),
    ('hr', 2, 'tb', 4): (1.0, 6195, 'exact'),
    ('hr', 2, 'tb', 5): (1.0, 6195, 'exact'),
    ('hr', 2, 'tb', 6): (1.0, 6195, 'exact'),
    ('hrr', 2, 'hit', 1): (0.9857186279461472, 437563, 'measured'),
    ('hrr', 2, 'hit', 2): (0.48566035062379587, 437563, 'measured'),
    ('hrr', 2, 'hit', 3): (0.11055550857819331, 437563, 'measured'),
    ('hrr', 2, 'hr', 1): (0.23124898586032183, 437563, 'measured'),
    ('hrr', 2, 'hr', 2): (0.014157961253579484, 437563, 'measured'),
    ('hrr', 2, 'tb', 2): (0.7176498012857577, 437563, 'measured'),
    ('hrr', 2, 'tb', 3): (0.4434927084785505, 437563, 'measured'),
    ('hrr', 2, 'tb', 4): (0.3057753969142729, 437563, 'measured'),
    ('hrr', 2, 'tb', 5): (0.14635149681303036, 437563, 'measured'),
    ('hrr', 2, 'tb', 6): (0.07163996955866926, 437563, 'measured'),
    ('hrr', 3, 'hit', 1): (0.9982490286450231, 276418, 'measured'),
    ('hrr', 3, 'hit', 2): (0.6521753286689, 276418, 'measured'),
    ('hrr', 3, 'hit', 3): (0.17500669276241054, 276418, 'measured'),
    ('hrr', 3, 'hr', 1): (0.3660615444725018, 276418, 'measured'),
    ('hrr', 3, 'hr', 2): (0.022411709801821878, 276418, 'measured'),
    ('hrr', 3, 'tb', 2): (0.8946125071449761, 276418, 'measured'),
    ('hrr', 3, 'tb', 3): (0.6550152305566208, 276418, 'measured'),
    ('hrr', 3, 'tb', 4): (0.4791909354672995, 276418, 'measured'),
    ('hrr', 3, 'tb', 5): (0.2313959293533706, 276418, 'measured'),
    ('hrr', 3, 'tb', 6): (0.1133934837818087, 276418, 'measured'),
    ('hrr', 4, 'hit', 1): (0.9998041280023504, 163372, 'measured'),
    ('hrr', 4, 'hit', 2): (0.7932020174815758, 163372, 'measured'),
    ('hrr', 4, 'hit', 3): (0.2733577357197072, 163372, 'measured'),
    ('hrr', 4, 'hr', 1): (0.47661778028058666, 163372, 'measured'),
    ('hrr', 4, 'hr', 2): (0.037919594544964866, 163372, 'measured'),
    ('hrr', 4, 'tb', 2): (0.9715006243419925, 163372, 'measured'),
    ('hrr', 4, 'tb', 3): (0.8152131332174424, 163372, 'measured'),
    ('hrr', 4, 'tb', 4): (0.6360147393678232, 163372, 'measured'),
    ('hrr', 4, 'tb', 5): (0.38741032735107606, 163372, 'measured'),
    ('hrr', 4, 'tb', 6): (0.19143427270278873, 163372, 'measured'),
    ('hrr', 5, 'hit', 1): (0.9999891203829625, 91915, 'measured'),
    ('hrr', 5, 'hit', 2): (0.8905836914540608, 91915, 'measured'),
    ('hrr', 5, 'hit', 3): (0.39515857041832125, 91915, 'measured'),
    ('hrr', 5, 'hr', 1): (0.5827884458467062, 91915, 'measured'),
    ('hrr', 5, 'hr', 2): (0.06739922754719034, 91915, 'measured'),
    ('hrr', 5, 'tb', 2): (0.9946472284175597, 91915, 'measured'),
    ('hrr', 5, 'tb', 3): (0.9197519447315454, 91915, 'measured'),
    ('hrr', 5, 'tb', 4): (0.7773159984768536, 91915, 'measured'),
    ('hrr', 5, 'tb', 5): (0.5695370722950552, 91915, 'measured'),
    ('hrr', 5, 'tb', 6): (0.31946907468857094, 91915, 'measured'),
    ('tb', 2, 'hit', 1): (1.0, 340876, 'exact'),
    ('tb', 2, 'hit', 2): (0.6234143794224293, 340876, 'measured'),
    ('tb', 2, 'hit', 3): (0.14191377509710276, 340876, 'measured'),
    ('tb', 2, 'hr', 1): (0.296841080040836, 340876, 'measured'),
    ('tb', 2, 'hr', 2): (0.018173764066698742, 340876, 'measured'),
    ('tb', 2, 'hrr', 2): (0.9212059517243807, 340876, 'measured'),
    ('tb', 2, 'hrr', 3): (0.7254456165878501, 340876, 'measured'),
    ('tb', 2, 'hrr', 4): (0.46561212875063074, 340876, 'measured'),
    ('tb', 2, 'hrr', 5): (0.2682001666295075, 340876, 'measured'),
    ('tb', 3, 'hit', 1): (1.0, 195319, 'exact'),
    ('tb', 3, 'hit', 2): (0.7232629698083648, 195319, 'measured'),
    ('tb', 3, 'hit', 3): (0.24767175748391093, 195319, 'measured'),
    ('tb', 3, 'hr', 1): (0.5180550791269667, 195319, 'measured'),
    ('tb', 3, 'hr', 2): (0.03171734444677681, 195319, 'measured'),
    ('tb', 3, 'hrr', 2): (0.993533655199955, 195319, 'measured'),
    ('tb', 3, 'hrr', 3): (0.9269861099022625, 195319, 'measured'),
    ('tb', 3, 'hrr', 4): (0.6818742672243868, 195319, 'measured'),
    ('tb', 3, 'hrr', 5): (0.43282527557482886, 195319, 'measured'),
    ('tb', 4, 'hit', 1): (1.0, 133796, 'exact'),
    ('tb', 4, 'hit', 2): (0.6487712637149092, 133796, 'measured'),
    ('tb', 4, 'hit', 3): (0.2719139585637837, 133796, 'measured'),
    ('tb', 4, 'hr', 1): (0.7562707405303597, 133796, 'measured'),
    ('tb', 4, 'hr', 2): (0.046301832640736645, 133796, 'measured'),
    ('tb', 4, 'hrr', 2): (1.0, 133796, 'exact'),
    ('tb', 4, 'hrr', 3): (0.9899922269724057, 133796, 'measured'),
    ('tb', 4, 'hrr', 4): (0.7766076713803103, 133796, 'measured'),
    ('tb', 4, 'hrr', 5): (0.5339995216598403, 133796, 'measured'),
    ('tb', 5, 'hit', 1): (1.0, 64038, 'exact'),
    ('tb', 5, 'hit', 2): (1.0, 64038, 'exact'),
    ('tb', 5, 'hit', 3): (0.3806333739342265, 64038, 'measured'),
    ('tb', 5, 'hr', 1): (0.846263156250976, 64038, 'measured'),
    ('tb', 5, 'hr', 2): (0.09673943595989881, 64038, 'measured'),
    ('tb', 5, 'hrr', 2): (1.0, 64038, 'exact'),
    ('tb', 5, 'hrr', 3): (0.9988132046597333, 64038, 'measured'),
    ('tb', 5, 'hrr', 4): (0.9883506667915924, 64038, 'measured'),
    ('tb', 5, 'hrr', 5): (0.8174677535213467, 64038, 'measured'),
    ('tb', 6, 'hit', 1): (1.0, 31347, 'exact'),
    ('tb', 6, 'hit', 2): (1.0, 31347, 'exact'),
    ('tb', 6, 'hit', 3): (0.5916993651705107, 31347, 'measured'),
    ('tb', 6, 'hr', 1): (0.9127827224295786, 31347, 'measured'),
    ('tb', 6, 'hr', 2): (0.19762656713561105, 31347, 'measured'),
    ('tb', 6, 'hrr', 2): (1.0, 31347, 'exact'),
    ('tb', 6, 'hrr', 3): (0.9999042970619199, 31347, 'measured'),
    ('tb', 6, 'hrr', 4): (0.9977031294860752, 31347, 'measured'),
    ('tb', 6, 'hrr', 5): (0.9367403579289885, 31347, 'measured'),
    # WIRE-2 (2026-07-11, S41): any total-bases rung ⟹ >=1 hit ⟹ HRR >= 1
    # (total bases are credited only on hits; HRR = H + R + RBI >= H). The
    # HRR-1 column was absent from the 2026-07-10 export's grid (hrr 2..5) —
    # these cells come from a RE-RUN of that export's exact join + population
    # (1,033,852 batter-games; job 24844262 tmp/ph4/wire2/tb_hrr1_cells.py),
    # each verified == 1.0 POOLED and on the 2021-25 era split; n's for
    # tb 2..6 reproduce the existing rows' conditioning counts exactly.
    # Rungs 2..7 are the live TB rung universe; 8 is tape-printed (109 legs)
    # and carries the identical arithmetic + measurement, so it is wired too.
    ('tb', 2, 'hrr', 1): (1.0, 340876, 'exact'),
    ('tb', 3, 'hrr', 1): (1.0, 195319, 'exact'),
    ('tb', 4, 'hrr', 1): (1.0, 133796, 'exact'),
    ('tb', 5, 'hrr', 1): (1.0, 64038, 'exact'),
    ('tb', 6, 'hrr', 1): (1.0, 31347, 'exact'),
    ('tb', 7, 'hrr', 1): (1.0, 14744, 'exact'),
    ('tb', 8, 'hrr', 1): (1.0, 8813, 'exact'),
    # M2 ZERO-GAPS wire (2026-07-12, job 24844262 tmp/zerogaps/mlb_wire_list.txt
    # sections 1-2; full precision mlb_measurements.json). Population = the
    # 2026-07-10 export join RE-RUN VERBATIM (1,033,852 batter-games 2005-25,
    # PA>=1, parsed_full x parsed_hrr 1:1, fatal-guarded; shipped-cell parity
    # float-exact this run). 37 EXACT cells (arithmetic containment from
    # official scoring — TB only via hits, max 4 TB/hit; HR = 1 hit + 4 TB +
    # >=1 R + >=1 RBI => HRR >= 3/HR; HRR = H+R+RBI >= H — each verified
    # == 1.0 POOLED AND 2021-25): closes hit_k=>hrr>=k, hr_k=>hrr>=3k,
    # hr2=>tb7/8, and the full hr-3 / hit-4 / tb-7 / tb-8 implication rows.
    # 47 MEASURED cells (all n >= MIN_CONDITIONAL_N; max era drift 0.019 in
    # p-space, cluster-boot hw95(p) <= 0.0026): includes the FULL
    # ('hrr', 1, *) reverse row — closes the S41-ny residual (tb-no x
    # hrr1-yes, 146 tape combos) and every no+yes mix against hit-4 / hr-3 /
    # tb-7 / tb-8 legs. 149 + 84 = 233 cells / 40 + 37 = 77 exact. The 29
    # sub-50k directions stay UNWIRED by decision (wire list section 8) —
    # their mixes keep declining UNKNOWN.
    ('hit', 1, 'hrr', 1): (1.0, 587975, 'exact'),
    ('hit', 2, 'hrr', 1): (1.0, 212507, 'exact'),
    ('hit', 3, 'hrr', 1): (1.0, 48375, 'exact'),
    ('hit', 4, 'hrr', 1): (1.0, 6658, 'exact'),
    ('hit', 4, 'hrr', 2): (1.0, 6658, 'exact'),
    ('hit', 4, 'hrr', 3): (1.0, 6658, 'exact'),
    ('hit', 4, 'hrr', 4): (1.0, 6658, 'exact'),
    ('hit', 4, 'tb', 2): (1.0, 6658, 'exact'),
    ('hit', 4, 'tb', 3): (1.0, 6658, 'exact'),
    ('hit', 4, 'tb', 4): (1.0, 6658, 'exact'),
    ('hr', 1, 'hrr', 1): (1.0, 101186, 'exact'),
    ('hr', 2, 'hrr', 1): (1.0, 6195, 'exact'),
    ('hr', 2, 'tb', 7): (1.0, 6195, 'exact'),
    ('hr', 2, 'tb', 8): (1.0, 6195, 'exact'),
    ('hr', 3, 'hit', 1): (1.0, 243, 'exact'),
    ('hr', 3, 'hit', 2): (1.0, 243, 'exact'),
    ('hr', 3, 'hit', 3): (1.0, 243, 'exact'),
    ('hr', 3, 'hrr', 1): (1.0, 243, 'exact'),
    ('hr', 3, 'hrr', 2): (1.0, 243, 'exact'),
    ('hr', 3, 'hrr', 3): (1.0, 243, 'exact'),
    ('hr', 3, 'hrr', 4): (1.0, 243, 'exact'),
    ('hr', 3, 'hrr', 5): (1.0, 243, 'exact'),
    ('hr', 3, 'tb', 2): (1.0, 243, 'exact'),
    ('hr', 3, 'tb', 3): (1.0, 243, 'exact'),
    ('hr', 3, 'tb', 4): (1.0, 243, 'exact'),
    ('hr', 3, 'tb', 5): (1.0, 243, 'exact'),
    ('hr', 3, 'tb', 6): (1.0, 243, 'exact'),
    ('hr', 3, 'tb', 7): (1.0, 243, 'exact'),
    ('hr', 3, 'tb', 8): (1.0, 243, 'exact'),
    ('tb', 7, 'hit', 1): (1.0, 14744, 'exact'),
    ('tb', 7, 'hit', 2): (1.0, 14744, 'exact'),
    ('tb', 7, 'hrr', 2): (1.0, 14744, 'exact'),
    ('tb', 7, 'hrr', 3): (1.0, 14744, 'exact'),
    ('tb', 8, 'hit', 1): (1.0, 8813, 'exact'),
    ('tb', 8, 'hit', 2): (1.0, 8813, 'exact'),
    ('tb', 8, 'hrr', 2): (1.0, 8813, 'exact'),
    ('tb', 8, 'hrr', 3): (1.0, 8813, 'exact'),
    ('hit', 1, 'hr', 3): (0.00041328287767337045, 587975, 'measured'),
    ('hit', 1, 'tb', 7): (0.02507589608401718, 587975, 'measured'),
    ('hit', 1, 'tb', 8): (0.014988732514137506, 587975, 'measured'),
    ('hit', 2, 'hr', 3): (0.0011434917438013807, 212507, 'measured'),
    ('hit', 2, 'tb', 7): (0.06938124391196525, 212507, 'measured'),
    ('hit', 2, 'tb', 8): (0.04147157505399822, 212507, 'measured'),
    ('hr', 1, 'hit', 4): (0.025912675666594193, 101186, 'measured'),
    ('hr', 1, 'tb', 7): (0.13906073962801177, 101186, 'measured'),
    ('hr', 1, 'tb', 8): (0.08569367303777202, 101186, 'measured'),
    ('hrr', 1, 'hit', 1): (0.9040956659993296, 650346, 'measured'),
    ('hrr', 1, 'hit', 2): (0.3267599093405664, 650346, 'measured'),
    ('hrr', 1, 'hit', 3): (0.07438348202341523, 650346, 'measured'),
    ('hrr', 1, 'hit', 4): (0.010237627355284726, 650346, 'measured'),
    ('hrr', 1, 'hr', 1): (0.1555879485689156, 650346, 'measured'),
    ('hrr', 1, 'hr', 2): (0.00952569862811488, 650346, 'measured'),
    ('hrr', 1, 'hr', 3): (0.0003736472585362253, 650346, 'measured'),
    ('hrr', 1, 'tb', 2): (0.5241456086452442, 650346, 'measured'),
    ('hrr', 1, 'tb', 3): (0.3003309007820453, 650346, 'measured'),
    ('hrr', 1, 'tb', 4): (0.20573048807865352, 650346, 'measured'),
    ('hrr', 1, 'tb', 5): (0.09846758494708971, 650346, 'measured'),
    ('hrr', 1, 'tb', 6): (0.048200496351173065, 650346, 'measured'),
    ('hrr', 1, 'tb', 7): (0.02267100897060949, 650346, 'measured'),
    ('hrr', 1, 'tb', 8): (0.013551248104854955, 650346, 'measured'),
    ('hrr', 2, 'hit', 4): (0.015216094596663794, 437563, 'measured'),
    ('hrr', 2, 'hr', 3): (0.0005553486012299943, 437563, 'measured'),
    ('hrr', 2, 'tb', 7): (0.03369571924500015, 437563, 'measured'),
    ('hrr', 2, 'tb', 8): (0.020141099681645843, 437563, 'measured'),
    ('hrr', 3, 'hit', 4): (0.024086709259165468, 276418, 'measured'),
    ('hrr', 3, 'hr', 3): (0.0008791033868995507, 276418, 'measured'),
    ('hrr', 3, 'tb', 7): (0.05333950755739496, 276418, 'measured'),
    ('hrr', 3, 'tb', 8): (0.031882873040105925, 276418, 'measured'),
    ('hrr', 4, 'hit', 4): (0.04075361751095659, 163372, 'measured'),
    ('hrr', 4, 'hr', 3): (0.0014874029821511643, 163372, 'measured'),
    ('hrr', 4, 'tb', 7): (0.09022965991724408, 163372, 'measured'),
    ('hrr', 4, 'tb', 8): (0.05394437235266753, 163372, 'measured'),
    ('hrr', 5, 'hit', 4): (0.06992329869988577, 91915, 'measured'),
    ('hrr', 5, 'hr', 3): (0.002643746940107708, 91915, 'measured'),
    ('hrr', 5, 'tb', 7): (0.15926671381167382, 91915, 'measured'),
    ('hrr', 5, 'tb', 8): (0.09587118533427623, 91915, 'measured'),
    ('tb', 2, 'hit', 4): (0.01953202924230512, 340876, 'measured'),
    ('tb', 2, 'hr', 3): (0.0007128691958366092, 340876, 'measured'),
    ('tb', 3, 'hit', 4): (0.034087825557165455, 195319, 'measured'),
    ('tb', 3, 'hr', 3): (0.001244118595733134, 195319, 'measured'),
    ('tb', 4, 'hit', 4): (0.04976232473317588, 133796, 'measured'),
    ('tb', 4, 'hr', 3): (0.0018161977936560136, 133796, 'measured'),
    ('tb', 5, 'hit', 4): (0.08519941284862113, 64038, 'measured'),
    ('tb', 5, 'hr', 3): (0.0037946219432212123, 64038, 'measured'),
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
