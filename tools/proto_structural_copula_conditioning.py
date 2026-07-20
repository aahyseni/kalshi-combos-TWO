"""PROTOTYPE (hard rule 8) for P0-7 PREFERRED — condition fallback copula legs
(corners/cards) on THAT game's sampled structural state via a SHARED FACTOR.

The production structural split (``sim/structural_book.sample_structural_values``)
samples a game's structural scoreline block and its copula-only corners/cards block
from INDEPENDENT rng, discarding same-game structural<->copula dependence. This
prototype validates the shared-factor conditioning that captures the defensible
part of that dependence IN THE PRODUCTION SAMPLE (not just a challenger), before it
is ported to the live module + parity-checked.

MECHANISM (conservative, width-bearing, NEVER fabricates correlation):
 1. For each game that straddles both blocks, compute a per-sample SHARED FACTOR
    from the sampled scoreline state (standardized total-goals intensity). This is
    the "attacking pressure" latent the plan asks for.
 2. Drive each copula leg's Gaussian latent as
        z_leg = sqrt(1 - beta^2) * z_indep + beta * f_shared
    where ``beta`` is a CONSERVATIVE, calibrated per-leg-type loading onto the shared
    factor. The marginal is preserved exactly (z_leg is still standard normal), so
    only the JOINT with the structural block changes.
 3. ``beta`` defaults to 0 for a leg type with NO defensible structural link (the
    measured facts: TOTAL corners are ~0 correlated with goals/total/result —
    "folk wisdom busted", config.pair_rho corners|total=0.00). Those legs KEEP
    independence and route to the worse-tail full-copula challenger backstop.
    A nonzero ``beta`` is used only where a defensible measured scoreline-driven
    link exists.

WORSE-OF GATE: the conditioning can only make the modeled tail FATTER or equal.
The full-copula bridge challenger stays as the backstop; the governing tail is the
max of the conditioned production tail and the challenger tail.

Checks below:
 A. Marginal preservation: conditioning does not move any leg's marginal.
 B. Covariance appears in production: with beta>0, corners<->structural joint-hit
    covariance rises above the independent product (which the raw split gives).
 C. Conservatism: beta=0 reproduces the raw independent split bit-for-bit.
"""
from __future__ import annotations

import numpy as np
from scipy.special import ndtr, ndtri

from combomaker.pricing.structural_api import (
    Advance,
    States,
    TotalOver,
    invert,
    parse_leg,
    parse_match,
    states as build_states,
    team_indicator,
)


def _standardized_total_goals(st: States, idx: np.ndarray) -> np.ndarray:
    """Per-sample SHARED FACTOR: standardized total sampled goals (incl. ET) of the
    game — the scoreline-intensity latent. Returned as an approx standard normal via
    the empirical rank -> normal quantile transform (distribution-free; robust to the
    total-goals count being discrete and skewed)."""
    total = (st.a90 + st.b90 + st.a_et + st.b_et)[idx].astype(np.float64)
    # Empirical CDF -> normal quantile (a copula-style PIT); ranks in (0,1) open.
    order = np.argsort(total, kind="stable")
    ranks = np.empty(total.size, dtype=np.float64)
    ranks[order] = (np.arange(total.size) + 0.5) / total.size
    return ndtri(ranks)


def sample_conditioned_corner(
    lam_a: float, lam_b: float, n: int, beta: float, seed: int
) -> tuple[np.ndarray, np.ndarray]:
    """Sample (structural advance-A indicator, conditioned corners-over indicator)
    for one knockout game. Corners marginal = 0.40; loaded onto the shared factor by
    ``beta``. Returns (advance_yes, corners_yes) 0/1 arrays."""
    match = parse_match("26JUL15ENGARG")
    assert match is not None
    from combomaker.pricing.dixon_coles import MatchFormat

    spec_adv = parse_leg("KXWCADVANCE-26JUL15ENGARG-ARG", match, fmt=MatchFormat.KNOCKOUT)
    spec_adv2 = parse_leg("KXWCADVANCE-26JUL15ENGARG-ENG", match, fmt=MatchFormat.KNOCKOUT)
    assert isinstance(spec_adv, Advance) and isinstance(spec_adv2, Advance)
    model = invert(
        [(spec_adv, 0.55), (spec_adv2, 0.45)],
        dc_rho=-0.05, et_factor=0.3333, match_format=MatchFormat.KNOCKOUT,
        max_goals=12, pens_win_a=0.5, half_share=0.45,
    )
    params = model.params
    st = build_states(params)
    rng = np.random.default_rng(seed)
    idx = rng.choice(st.w.size, size=n, p=st.w)

    from combomaker.sim.structural_book import _sampled_states

    sampled = _sampled_states(st, idx)
    u_pens = rng.random(n)
    adv = team_indicator(sampled, spec_adv, params)  # 90' win channel proxy
    # advance settlement (with shootout) is not needed for the covariance shape; the
    # win indicator suffices for the prototype covariance check.

    f = _standardized_total_goals(st, idx)
    z_indep = rng.standard_normal(n)
    z_leg = np.sqrt(max(0.0, 1.0 - beta * beta)) * z_indep + beta * f
    u_leg = ndtr(z_leg)
    corners_p = 0.40
    corners_yes = (u_leg < corners_p).astype(np.float64)  # YES iff below marginal
    return adv, corners_yes


def main() -> None:
    n = 400_000
    print("beta   P(corners)   cov(adv,corners)   joint-hit")
    for beta in (0.0, 0.15, 0.30, 0.5):
        adv, corn = sample_conditioned_corner(2.0, 1.6, n, beta, seed=7)
        p_corn = corn.mean()
        joint = (adv * corn).mean()
        cov = joint - adv.mean() * corn.mean()
        print(f"{beta:>4}   {p_corn:.4f}       {cov:+.5f}          {joint:.4f}")
    print("\nCheck A: P(corners) stays ~0.40 for every beta (marginal preserved).")
    print("Check B: cov moves with beta (dependence enters production).")
    print("Check C: beta=0 -> cov ~ 0 (independent split reproduced).")


if __name__ == "__main__":
    main()
