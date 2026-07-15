"""Structural-inversion fit challenge (P1-4).

The structural inverters (``dixon_coles.invert``, ``margin_total.invert_means``,
``mlb_runs.invert_runs``) already REJECT a fit whose identifying-constraint
misfit is impossibly large — an exact system that will not solve to ~0, or an
over-identified system whose legs contradict any coherent scoreline. Those hard
bars raise ``StructuralError`` and send the combo down the copula fallback.

What was missing (this module) is the second half of the plan item: *persist*
the residual of a fit that PASSED the hard bar, and *challenge* — rather than
silently accept — a fit whose residual, while below the reject bar, is elevated
enough to signal a marginal/model inconsistency. An elevated-but-priceable fit
is not wrong to price, but it must not price at ordinary width: it is flagged so
the width path widens and the fit is recorded for offline audit of systematic
structural misfit against the live market.

Pure, side-effect-free, and inversion-math-independent: it classifies a
residual scalar; persistence lives in ``ops.persistence`` and the actual REJECT
enforcement stays inside the (pristine) inverters. The thresholds here MIRROR
those inverters — kept in sync by ``tests/test_fit_challenge.py`` which asserts
they equal the constants the live inverters enforce.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

# Hard reject bars — MUST equal the constants enforced inside the inverters:
#   dixon_coles.invert / mlb_runs.invert_runs / margin_total.invert_means.
# An exact-identified system (n_constraints == n_free params) should solve to
# ~0; anything above this is a genuine contradiction. An over-identified system
# is allowed a small priced misfit up to the larger bar.
REJECT_EXACT = 0.005
REJECT_OVERIDENTIFIED = 0.05

# Challenge band: a fit that PASSED the reject bar but whose residual is a
# meaningful fraction of it is inconsistent-but-priceable — record it and widen,
# never accept at ordinary width. Below this it is a clean fit.
CHALLENGE_FRACTION = 0.5


class FitVerdict(enum.Enum):
    ACCEPT = "accept"        # clean fit — price normally
    CHALLENGE = "challenge"  # elevated misfit — price but widen-flag + record
    REJECT = "reject"        # inconsistent — do not price structurally


@dataclass(frozen=True, slots=True)
class FitChallenge:
    verdict: FitVerdict
    residual: float
    exactly_identified: bool
    reject_bar: float        # the hard bar that applied to this system
    challenge_bar: float     # elevated-misfit threshold (< reject_bar)

    @property
    def priceable(self) -> bool:
        """True unless the fit is outright rejected."""
        return self.verdict is not FitVerdict.REJECT

    @property
    def should_widen(self) -> bool:
        """A challenged (elevated-misfit) fit must not price at ordinary width."""
        return self.verdict is FitVerdict.CHALLENGE

    def note(self) -> str:
        return (
            f"fit-challenge: verdict={self.verdict.value} residual={self.residual:.4f} "
            f"exact={self.exactly_identified} reject>={self.reject_bar:.4f} "
            f"challenge>={self.challenge_bar:.4f}"
        )


def classify_fit(residual: float, *, exactly_identified: bool) -> FitChallenge:
    """Classify a structural inversion residual into accept / challenge / reject.

    ``exactly_identified`` selects the applicable hard bar: an exactly-identified
    system (as many identifying leg constraints as free structural parameters)
    is held to the tight ``REJECT_EXACT`` bar; an over-identified system is
    allowed a priced misfit up to ``REJECT_OVERIDENTIFIED``.

    Fail-closed: a negative or non-finite residual is treated as REJECT (a
    sentinel that something upstream produced no honest misfit measurement).
    """
    import math

    reject_bar = REJECT_EXACT if exactly_identified else REJECT_OVERIDENTIFIED
    challenge_bar = reject_bar * CHALLENGE_FRACTION

    if not math.isfinite(residual) or residual < 0.0:
        verdict = FitVerdict.REJECT
    elif residual > reject_bar:
        verdict = FitVerdict.REJECT
    elif residual >= challenge_bar:
        verdict = FitVerdict.CHALLENGE
    else:
        verdict = FitVerdict.ACCEPT

    return FitChallenge(
        verdict=verdict,
        residual=residual,
        exactly_identified=exactly_identified,
        reject_bar=reject_bar,
        challenge_bar=challenge_bar,
    )


__all__ = [
    "CHALLENGE_FRACTION",
    "REJECT_EXACT",
    "REJECT_OVERIDENTIFIED",
    "FitChallenge",
    "FitVerdict",
    "classify_fit",
]
