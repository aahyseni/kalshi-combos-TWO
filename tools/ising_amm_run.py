"""Driver for the pairwise max-entropy (Ising) AMM prototype — additive, standalone.

Produces every number in docs/calibration/results_ising.md:
  1. copula-vs-Ising agreement across a rho sweep (2-leg exact; 3-leg triple gap)
  2. W_ij calibrated from history for concrete soccer cross-prop / game pairs
  3. a 3-leg coherent-pricing demo (triple priced from 3 pairwise weights)
  4. an online SGD-update demo (one trade nudges W toward the empirical corr)

Reuses the SHIPPED copula and the SHIPPED history loaders in
tools/calibrate_pairs_from_history.py so the empirical moments are identical
to the game-level calibration. Run:
  .venv/Scripts/python.exe tools/ising_amm_run.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

# Import the shipped copula and the shipped history loaders (additive reuse).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from calibrate_pairs_from_history import (  # noqa: E402
    implied_rho,
    load_matches,
    measure,
)

from combomaker.pricing.copula import (  # noqa: E402
    gaussian_copula_joint_prob,
    is_psd,
    nearest_psd,
)
from combomaker.pricing.ising_amm import IsingAMM, fit_ising  # noqa: E402


def sep(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


# ---------------------------------------------------------------------------
# 1. Copula-vs-Ising agreement across a rho sweep
# ---------------------------------------------------------------------------
def rho_sweep() -> None:
    sep("1. COPULA vs ISING  --  rho sweep (marginals fixed)")
    p2 = [0.55, 0.45]
    p3 = [0.55, 0.50, 0.45]
    print(f"2-leg marginals p = {p2}   3-leg marginals p = {p3}\n")
    print(
        f"{'rho':>6} | {'cop P(AB)':>10} {'ising P(AB)':>11} {'|d|':>9} "
        f"|| {'cop P(ABC)':>11} {'ising P(ABC)':>12} {'indep P(ABC)':>12} "
        f"{'d(cop-is)':>10}"
    )
    print("-" * 100)
    for rho in [-0.6, -0.4, -0.2, -0.05, 0.05, 0.2, 0.4, 0.6, 0.8]:
        # --- 2 legs ---
        corr2 = np.array([[1.0, rho], [rho, 1.0]])
        cop_ab = gaussian_copula_joint_prob(p2, corr2)
        m2 = fit_ising(p2, {(0, 1): cop_ab})
        is_ab = m2.pairwise(0, 1)

        # --- 3 legs, common rho on all three pairs ---
        corr3 = np.full((3, 3), rho)
        np.fill_diagonal(corr3, 1.0)
        if not is_psd(corr3):
            corr3 = nearest_psd(corr3)
        cop_pairs = {
            (0, 1): gaussian_copula_joint_prob([p3[0], p3[1]], corr3[np.ix_([0, 1], [0, 1])]),
            (0, 2): gaussian_copula_joint_prob([p3[0], p3[2]], corr3[np.ix_([0, 2], [0, 2])]),
            (1, 2): gaussian_copula_joint_prob([p3[1], p3[2]], corr3[np.ix_([1, 2], [1, 2])]),
        }
        cop_abc = gaussian_copula_joint_prob(p3, corr3)
        m3 = fit_ising(p3, cop_pairs)
        is_abc = m3.joint_all_yes()
        indep_abc = p3[0] * p3[1] * p3[2]

        print(
            f"{rho:>6.2f} | {cop_ab:>10.6f} {is_ab:>11.6f} {abs(cop_ab - is_ab):>9.2e} "
            f"|| {cop_abc:>11.6f} {is_abc:>12.6f} {indep_abc:>12.6f} "
            f"{cop_abc - is_abc:>10.6f}"
        )
    print(
        "\nNote: the Ising fits the SAME three pair-marginals the copula produces, "
        "then\nprices the triple from pairwise weights only. The copula's extra 3-way\n"
        "structure (Gaussian tail) is the residual d(cop-is)."
    )


# ---------------------------------------------------------------------------
# 2 + 3. Calibrate W_ij from soccer history; 3-leg coherent demo
# ---------------------------------------------------------------------------
def triple(matches, a: str, b: str, c: str) -> tuple[int, float]:
    rows = [m for m in matches if m[a] is not None and m[b] is not None and m[c] is not None]
    n = len(rows)
    p_abc = sum(1 for m in rows if m[a] and m[b] and m[c]) / n
    return n, p_abc


def calibrate_and_triple() -> None:
    matches = load_matches()
    sep(f"2. CALIBRATE W_ij FROM SOCCER CLUB HISTORY  ({len(matches)} games)")

    # Cross-prop / game-level pairs mirrored from calibrate_pairs_from_history.
    pairs = [
        ("btts x over2.5", "btts", "over25"),
        ("home_win x over2.5", "home_win", "over25"),
        ("btts x home_win", "btts", "home_win"),
    ]
    print(
        f"{'pair':22} {'n':>6} {'P(A)':>7} {'P(B)':>7} {'P(AB)':>8} "
        f"{'copula rho':>11} {'Ising W_ij':>11} {'Ising P(AB)':>12}"
    )
    print("-" * 92)
    fitted = {}
    for label, a, b in pairs:
        n, p_a, p_b, p_ab, rho = measure(matches, a, b)
        m = fit_ising([p_a, p_b], {(0, 1): p_ab})
        fitted[(a, b)] = (p_a, p_b, p_ab, rho, float(m.W[0, 1]), m.pairwise(0, 1))
        print(
            f"{label:22} {n:>6} {p_a:>7.4f} {p_b:>7.4f} {p_ab:>8.4f} "
            f"{rho:>11.4f} {m.W[0, 1]:>11.4f} {m.pairwise(0, 1):>12.6f}"
        )
    print(
        "\nEvery Ising P(AB) reproduces the empirical P(AB) to fit tolerance, so\n"
        "implied-copula-rho(Ising P(AB)) == the copula rho column by construction:\n"
        "the W_ij and the copula rho are two parameterizations of ONE pair-joint."
    )

    # ---- 3-leg coherent pricing: home_win & over2.5 & btts -------------
    sep("3. THREE-LEG COHERENT PRICING  --  home_win & over2.5 & btts")
    a, b, c = "home_win", "over25", "btts"
    rows = [m for m in matches if m[a] is not None and m[b] is not None and m[c] is not None]
    n = len(rows)
    p_a = sum(1 for m in rows if m[a]) / n
    p_b = sum(1 for m in rows if m[b]) / n
    p_c = sum(1 for m in rows if m[c]) / n
    p_ab = sum(1 for m in rows if m[a] and m[b]) / n
    p_ac = sum(1 for m in rows if m[a] and m[c]) / n
    p_bc = sum(1 for m in rows if m[b] and m[c]) / n
    p_abc_emp = sum(1 for m in rows if m[a] and m[b] and m[c]) / n

    print(f"n = {n} games")
    print(f"marginals:  P(home)={p_a:.4f}  P(over2.5)={p_b:.4f}  P(btts)={p_c:.4f}")
    print(f"pairwise :  P(h,o)={p_ab:.4f}  P(h,b)={p_ac:.4f}  P(o,b)={p_bc:.4f}")
    print(f"EMPIRICAL triple P(home & over2.5 & btts) = {p_abc_emp:.5f}\n")

    # (a) independent multiplication
    indep = p_a * p_b * p_c

    # (b) Ising fit to 3 marginals + 3 empirical pair-joints, price the triple
    m3 = fit_ising([p_a, p_b, p_c], {(0, 1): p_ab, (0, 2): p_ac, (1, 2): p_bc})
    ising_abc = m3.joint_all_yes()

    # (c) Gaussian copula: implied rho on each pair -> 3x3 corr -> triple CDF
    r_ab = implied_rho(p_a, p_b, p_ab)
    r_ac = implied_rho(p_a, p_c, p_ac)
    r_bc = implied_rho(p_b, p_c, p_bc)
    corr = np.array([[1.0, r_ab, r_ac], [r_ab, 1.0, r_bc], [r_ac, r_bc, 1.0]])
    psd_fixed = not is_psd(corr)
    if psd_fixed:
        corr = nearest_psd(corr)
    cop_abc = gaussian_copula_joint_prob([p_a, p_b, p_c], corr)

    print(f"implied copula rhos:  rho(h,o)={r_ab:+.4f}  rho(h,b)={r_ac:+.4f}  rho(o,b)={r_bc:+.4f}"
          + ("   [corr repaired to PSD]" if psd_fixed else ""))
    print(f"Ising W_ij        :  W(h,o)={m3.W[0,1]:+.4f}  W(h,b)={m3.W[0,2]:+.4f}  W(o,b)={m3.W[1,2]:+.4f}")
    print()
    print(f"{'method':40} {'P(triple)':>10} {'err vs emp':>12}")
    print("-" * 64)
    for name, val in [
        ("EMPIRICAL frequency", p_abc_emp),
        ("independent multiplication", indep),
        ("Ising pairwise max-ent", ising_abc),
        ("Gaussian copula (3 implied rhos)", cop_abc),
    ]:
        err = val - p_abc_emp
        print(f"{name:40} {val:>10.5f} {err:>+12.5f}")
    # verify coherence: the fitted 3-leg model reproduces all sub-marginals
    print("\nCoherence check (Ising sub-prices vs targets):")
    print(f"  marg: {[round(float(x),4) for x in m3.marginals()]}  (target {[round(p_a,4),round(p_b,4),round(p_c,4)]})")
    print(f"  pair: h,o={m3.pairwise(0,1):.4f} h,b={m3.pairwise(0,2):.4f} o,b={m3.pairwise(1,2):.4f}")
    print("  -> one phi vector prices all 7 sub-combinations self-consistently.")


# ---------------------------------------------------------------------------
# 4. Online SGD-update demo
# ---------------------------------------------------------------------------
def online_demo() -> None:
    sep("4. ONLINE SGD UPDATE  --  W_ij self-calibrates toward the empirical corr")
    matches = load_matches()
    a, b = "btts", "over25"
    _, p_a, p_b, p_ab_star, rho = measure(matches, a, b)
    print(f"pair: {a} x {b}   empirical target  P(A)={p_a:.4f} P(B)={p_b:.4f} P(AB)*={p_ab_star:.4f}")
    print(f"(copula rho for this pair = {rho:+.4f})\n")

    # Start from INDEPENDENCE: theta set to the marginals, W = 0.
    m = IsingAMM(2)
    # seed theta so marginals already match (logit); W starts at 0 -> P(AB)=p_a*p_b
    m.theta = np.array([np.log(p_a / (1 - p_a)), np.log(p_b / (1 - p_b))])
    eta = 4.0
    print(f"start (W=0, independent):  W01={m.W[0,1]:+.5f}  P(AB)^phi={m.pairwise(0,1):.5f} "
          f"(= P(A)P(B)={p_a*p_b:.5f})")
    print(f"\nstreaming the observed pair-moment, eta={eta}:")
    print(f"{'step':>4} {'W01':>10} {'P(AB)^phi':>11} {'resid=P(AB)^phi-P(AB)*':>24}")
    print("-" * 54)
    for step in range(1, 13):
        info = m.sgd_step(
            eta,
            target_marginals=[p_a, p_b],
            target_pairs={(0, 1): p_ab_star},
        )
        print(f"{step:>4} {m.W[0,1]:>10.5f} {m.pairwise(0,1):>11.6f} {info['resid_W_01']:>+24.2e}")
    m_fit = fit_ising([p_a, p_b], {(0, 1): p_ab_star})
    print(f"\nconverged  W01 -> {m.W[0,1]:+.5f}   batch-fit W01 = {m_fit.W[0,1]:+.5f}   "
          f"P(AB)^phi -> {m.pairwise(0,1):.5f}  (target {p_ab_star:.5f})")
    print("The single-trade SGD step provably equals the moment residual, so the\n"
          "weight walks straight to the value that reproduces the empirical joint.")


def main() -> None:
    rho_sweep()
    calibrate_and_triple()
    online_demo()


if __name__ == "__main__":
    main()
