"""Structural-inversion fit challenge (P1-4; derived bar 2026-09-04 item B).

The structural inverters (``dixon_coles.invert``, ``margin_total.invert_means``,
``mlb_runs.invert_runs``) REJECT a fit whose identifying-constraint misfit is
impossibly large — legs that contradict any coherent scoreline. Those hard bars
raise ``StructuralError`` and send the combo down the copula fallback.

This module is the second half: classify a fit that PASSED the hard bar into
ACCEPT / CHALLENGE, so an elevated-but-priceable misfit is *flagged and
recorded* (``ops.persistence.record_structural_fit``) rather than silently
accepted, and carry that verdict on the ``JointEstimate`` (``StructuralFitRecord``)
so the lifecycle can persist it off the pricing path.

Two classification modes:

* **Derived (Dixon-Coles, build 2026-09-04 item B).** ``classify_fit(...,
  resolution=...)``. There is ONE hard bar for every DC system — exact and
  over-identified alike: the pre-existing ``REJECT_OVERIDENTIFIED``. The
  ACCEPT bar is DERIVED from the market: a fit is only "inconsistent" when its
  residual exceeds what the identifying leg books can themselves resolve, so
  the accept bar is the SUM of the identifying legs' ``belief.uncertainty``
  (the exact quantity ``structural._price`` perturbs — wider books, looser
  bar; tighter books, stricter bar), floored at the over-identified regime's
  own accept boundary (``CHALLENGE_FRACTION * REJECT_OVERIDENTIFIED``: an
  exact pair is never held to a stricter bar than the same legs inside a
  triple) and capped at the hard bar. Between the accept bar and the hard bar
  the fit is CHALLENGE: priced, widened by the residual (the misfit width
  channel), recorded. No hand-set number survives: both edges are pre-existing
  anchors and the only live input is measured book state. This replaced the
  hand-set 0.005 exact-system bar that routed every club btts x over-2.5 pair
  (residual 0.006-0.018 — a Poisson cannot reproduce that market shape to
  better than ~0.6pp) to the copula at the stale World-Cup blend.

* **Legacy fixed bars.** ``classify_fit(...)`` with no ``resolution`` keeps the
  original ``REJECT_EXACT`` / ``REJECT_OVERIDENTIFIED`` + ``CHALLENGE_FRACTION``
  scheme. It still mirrors what ``margin_total.invert_means`` and
  ``mlb_runs.invert_runs`` enforce (drift-guarded by ``tests/test_fit_challenge``).

Pure, side-effect-free, inversion-math-independent.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass

# Hard reject bars.
#   REJECT_EXACT: the exact-system bar STILL enforced by margin_total.invert_means
#   and mlb_runs.invert_runs (legacy mode). NO LONGER enforced by dixon_coles
#   (build 2026-09-04): the DC solver holds every system to the ONE hard bar.
#   REJECT_OVERIDENTIFIED: the ONE hard bar for dixon_coles (all systems), and
#   the over-identified bar for the other two inverters.
REJECT_EXACT = 0.005
REJECT_OVERIDENTIFIED = 0.05

# Challenge band (legacy mode): a fit that PASSED the reject bar but whose
# residual is a meaningful fraction of it is inconsistent-but-priceable. In
# derived mode this same fraction of the hard bar is the FLOOR under the
# market-derived accept bar (the over-identified regime's accept boundary).
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
    challenge_bar: float     # accept/challenge boundary (< reject_bar)
    # Derived mode only: the identifying leg books' summed uncertainty the
    # accept bar was derived from. None = legacy fixed bars.
    resolution: float | None = None

    @property
    def priceable(self) -> bool:
        """True unless the fit is outright rejected."""
        return self.verdict is not FitVerdict.REJECT

    @property
    def should_widen(self) -> bool:
        """A challenged (elevated-misfit) fit must not price at ordinary width."""
        return self.verdict is FitVerdict.CHALLENGE

    def note(self) -> str:
        res = "" if self.resolution is None else f" resolution={self.resolution:.4f}"
        return (
            f"fit-challenge: verdict={self.verdict.value} residual={self.residual:.4f} "
            f"exact={self.exactly_identified} reject>={self.reject_bar:.4f} "
            f"challenge>={self.challenge_bar:.4f}{res}"
        )


def derived_accept_bar(resolution: float) -> float:
    """The market-derived ACCEPT bar: the identifying leg books' summed
    uncertainty, floored at the over-identified regime's accept boundary and
    capped at the hard bar. Monotone non-decreasing in ``resolution`` (wider
    books -> looser bar). A non-finite / negative resolution is treated as
    zero (the floor applies — never a looser bar from a broken input)."""
    if not math.isfinite(resolution) or resolution < 0.0:
        resolution = 0.0
    floor = CHALLENGE_FRACTION * REJECT_OVERIDENTIFIED
    return min(max(resolution, floor), REJECT_OVERIDENTIFIED)


def classify_fit(
    residual: float,
    *,
    exactly_identified: bool,
    resolution: float | None = None,
) -> FitChallenge:
    """Classify a structural inversion residual into accept / challenge / reject.

    ``resolution`` given -> DERIVED mode (Dixon-Coles): hard bar
    ``REJECT_OVERIDENTIFIED`` for every system; accept bar
    ``derived_accept_bar(resolution)``; CHALLENGE strictly above the accept
    bar, up to and including the hard bar.

    ``resolution`` None -> LEGACY mode: ``exactly_identified`` selects the hard
    bar (``REJECT_EXACT`` vs ``REJECT_OVERIDENTIFIED``); CHALLENGE from
    ``CHALLENGE_FRACTION`` of it (inclusive) up to the bar.

    Fail-closed in both modes: a negative or non-finite residual is REJECT (a
    sentinel that something upstream produced no honest misfit measurement —
    ``structural`` uses -1.0 for fallbacks that never reached an inversion).
    """
    if resolution is None:
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

    reject_bar = REJECT_OVERIDENTIFIED
    challenge_bar = derived_accept_bar(resolution)
    if not math.isfinite(residual) or residual < 0.0:
        verdict = FitVerdict.REJECT
    elif residual > reject_bar:
        verdict = FitVerdict.REJECT
    elif residual > challenge_bar:
        verdict = FitVerdict.CHALLENGE
    else:
        verdict = FitVerdict.ACCEPT
    return FitChallenge(
        verdict=verdict,
        residual=residual,
        exactly_identified=exactly_identified,
        reject_bar=reject_bar,
        challenge_bar=challenge_bar,
        resolution=float(resolution),
    )


# Route labels a StructuralFitRecord can carry (plain strings: they are
# persisted as-is and read back by tools; an enum would only add a mapping).
ROUTE_STRUCTURAL = "structural"   # DC scoreline cell priced
ROUTE_HYBRID = "hybrid"           # symmetric pair: DC-implied rho on market marginals
ROUTE_REJECT = "reject"           # inverter rejected; the engine decides copula/decline
ROUTE_COPULA = "copula"           # engine fell back to the copula
ROUTE_DECLINED = "declined"       # engine declined (tie x total pickoff guard)


@dataclass(frozen=True, slots=True)
class StructuralFitRecord:
    """The structural route verdict for ONE priced combo, carried on the
    ``JointEstimate`` (so it survives the joint memo and the ProcessPool
    boundary) and persisted by the lifecycle OFF the pricing path — never a
    store call from the engine. ``family`` = the combo's leg types, sorted and
    '|'-joined (``'btts|total'``); ``reason`` = the inverter's message when
    the fit was rejected or the combo never reached an inversion."""

    challenge: FitChallenge
    model: str
    family: str
    route: str
    n_legs: int
    reason: str = ""


__all__ = [
    "CHALLENGE_FRACTION",
    "REJECT_EXACT",
    "REJECT_OVERIDENTIFIED",
    "ROUTE_COPULA",
    "ROUTE_DECLINED",
    "ROUTE_HYBRID",
    "ROUTE_REJECT",
    "ROUTE_STRUCTURAL",
    "FitChallenge",
    "FitVerdict",
    "StructuralFitRecord",
    "classify_fit",
    "derived_accept_bar",
]
