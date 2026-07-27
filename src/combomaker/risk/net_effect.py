"""NET-EFFECT ADMISSION for the (family:entity x direction) axis — LEVER #3.

THE DEFECT (measured on the live tape, 2026-07-26/27). ``skip_entity_loss_cap``
judges a candidate by its WORST SINGLE LEG. A cross-sport ticket that pairs a
LoL/CS leg WITH four MLB legs is refused outright because ONE of the five
(family:entity x direction) keys it touches is already over its wall — even
though the other four are genuinely fresh exposure and the fill DILUTES the
book's dollar concentration. On 2026-07-26/27 that shape refused esports flow
by the thousand while the book sat one-way.

THE DOCTRINE IT EXTENDS. The operator's standing rule already covers the
adjacent case: certified risk-REDUCING fills bypass concentration caps
("hedges are +EV"), with certification by WAIVER STATE ENUMERATION, never
leg-sign heuristics (``risk/limits.py``'s ``WaiverCertificate`` /
``_waiver_covers``). This module is the same doctrine applied to
risk-DIVERSIFYING fills: a certificate, produced by exhaustive enumeration of
state, re-validated against the LIVE enforced walls at the point of
enforcement, fail-closed on every doubt.

WHAT IS ENUMERATED. Not the candidate's legs, and never their signs: the
COMPLETE per-key premium-at-risk state of the book — key by key, over the
UNION of every key either state touches — BEFORE the candidate and AFTER it.
Three exact inequalities are then read off that enumeration. A sign test can be
fooled by one leg; an enumeration over the whole key universe cannot.

DOLLARS, NEVER COUNTS (operator 2026-07-26: "90 unique independent bets
totalling $100 but 10 concentrated bets totalling $500 — we're diverse by
numbers but not dollars"). Every quantity here is premium-at-risk in integer
centi-cents, and the diversification measure is the DOLLAR-weighted effective
number of keys — the inverse Herfindahl of the premium shares:

    N_eff = (Σ_k w_k)^2 / Σ_k w_k^2

which is 90 only if the ninety bets carry equal dollars, and collapses toward
10 the moment the dollars do. Counting keys would score the operator's
counter-example as "diverse"; N_eff scores it as ~10.

THE THREE TESTS (all EXACT INTEGER inequalities — money never touches a binary
float, hard rule 5; cross-multiplied so no division and no rounding exists):

  T1  KEY SHARE MUST NOT RISE  — the breaching key's share of attributed
      premium after ≤ before:      post_key · pre_total  ≤  pre_key · post_total
      This is the scope-discipline test and the one that encodes the operator's
      own example. Under the book's comonotone attribution every key a combo
      touches receives the SAME dollars (the full combo loss), so a ticket
      that lands on the over-wall arm plus four fresh keys moves that arm from
      w/T to (w+L)/(T+5L) — DOWN. A ticket that only feeds the over-wall arm
      moves it from w/T to (w+L)/(T+L) — UP, and is refused, exactly as today.
      A key that was EMPTY (pre = 0) can never pass: 0-share can only rise, so
      a single fill may never plant more than the wall on a brand-new key.

  T2  THE PEAK MUST NOT RISE — the largest key's share after ≤ before:
      post_peak · pre_total ≤ pre_peak · post_total. Stops "improve the
      Herfindahl by fattening the runner-up until it becomes the new peak".

  T3  DOLLAR-WEIGHTED EFFECTIVE N MUST STRICTLY RISE:
      post_total^2 · pre_sumsq  >  pre_total^2 · post_sumsq
      (i.e. N_eff(post) > N_eff(pre), cross-multiplied). The net-effect test
      proper: the fill must leave the book measurably MORE spread in dollars,
      not merely not-worse.

WHAT THIS DOES **NOT** DO. It is a SHAPE waiver, never a SCALE waiver. The
absolute ceiling is re-validated by the caller against a LIVE enforced wall
(``risk/limits.py``: the per-COMBO wall — "one entity may carry at most what
one structure is allowed to carry", the entity cap's own stated design
rationale, so no new number is introduced). The book's SCALE stays governed by
the untouched hard protections: portfolio det-max, ruin, CVaR / KILL-tail,
slate, per-game and the halts. Nothing here can raise any of them.

FAIL-CLOSED. A degenerate state (empty book, non-positive totals), a key absent
from the post state, or any arithmetic doubt yields ``certified=False`` with a
verdict string naming the enumeration outcome — never a convenient default
(quiet-failure defense #2).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

__all__ = [
    "NetEffectCertificate",
    "certify_net_effect",
    "effective_n",
    "pre_state_from_post",
]


@dataclass(frozen=True, slots=True)
class NetEffectCertificate:
    """The enumerated pre/post concentration state of ONE axis, plus the verdict.

    Structurally satisfies ``risk.limits.ConcentrationCertificate`` (a Protocol
    there, so ``limits`` never has to import this module's dataclass to type
    the check site — the same shape ``WaiverCertificate`` uses).

    Every ``*_cc`` is integer centi-cents of PREMIUM AT RISK, attributed the way
    the exposure book attributes it (a combo's full max loss to EACH distinct
    key among its legs, deduped per position). ``verdict`` names the enumeration
    outcome and is carried into the breach detail so a refusal is readable.
    """

    key: str
    certified: bool
    verdict: str
    pre_total_cc: int
    post_total_cc: int
    pre_key_cc: int
    post_key_cc: int
    pre_sumsq: int
    post_sumsq: int
    pre_peak_cc: int
    post_peak_cc: int
    n_keys_pre: int
    n_keys_post: int

    @property
    def pre_eff_n(self) -> float:
        """Dollar-weighted effective N before the candidate (REPORTING ONLY —
        the decision is made on the exact integer cross-products above)."""
        return effective_n(self.pre_total_cc, self.pre_sumsq)

    @property
    def post_eff_n(self) -> float:
        """Dollar-weighted effective N after the candidate (REPORTING ONLY)."""
        return effective_n(self.post_total_cc, self.post_sumsq)


def effective_n(total_cc: int, sumsq: int) -> float:
    """(Σ w)^2 / Σ w^2 — the dollar-weighted effective number of keys.

    A float, and deliberately so: it is a REPORTING/telemetry number only. No
    admission decision reads it (the three tests are exact integer
    cross-products), so no money quantity is ever compared through a binary
    float. 0.0 on a degenerate state.
    """
    if total_cc <= 0 or sumsq <= 0:
        return 0.0
    return (float(total_cc) * float(total_cc)) / float(sumsq)


def pre_state_from_post(
    post_by_key: Mapping[str, int],
    candidate_keys_and_loss: Iterable[tuple[frozenset[str], int]],
) -> dict[str, int]:
    """The BEFORE-candidate per-key state, derived exactly from the AFTER state.

    The exposure snapshot the caps read already folds the candidate in (that is
    what makes it the ACCUMULATED axis), so the pre-state is obtained by
    removing exactly what the candidate contributed: each hypothetical
    position's ``max_loss_cc``, once per DISTINCT key among its legs — the
    identical dedup rule ``ExposureBook.snapshot`` applies when it builds
    ``loss_by_entity_cc``. Exact integer arithmetic; no second snapshot is
    taken (the hot path may not afford one) and no state is re-derived from
    positions, so pre + candidate == post by construction.

    Keys that fall to zero or below are DROPPED: a key that exists only because
    of the candidate was not part of the book's prior shape, and leaving a 0 in
    would inflate the pre-state key count without adding a dollar.
    """
    pre: dict[str, int] = dict(post_by_key)
    for keys, loss_cc in candidate_keys_and_loss:
        for key in keys:
            pre[key] = pre.get(key, 0) - int(loss_cc)
    return {k: v for k, v in pre.items() if v > 0}


def certify_net_effect(
    *,
    key: str,
    pre_by_key: Mapping[str, int],
    post_by_key: Mapping[str, int],
) -> NetEffectCertificate:
    """Enumerate the whole key universe and certify ``key``'s net effect.

    EXHAUSTIVE by construction: the sums below run over EVERY key present in
    either state, so a candidate cannot hide concentration in a key the caller
    forgot to name. Pure: no clock, no config, no I/O, no floats in any
    decision path.
    """
    pre_total = 0
    pre_sumsq = 0
    pre_peak = 0
    for value in pre_by_key.values():
        v = int(value)
        if v <= 0:
            continue
        pre_total += v
        pre_sumsq += v * v
        if v > pre_peak:
            pre_peak = v
    post_total = 0
    post_sumsq = 0
    post_peak = 0
    for value in post_by_key.values():
        v = int(value)
        if v <= 0:
            continue
        post_total += v
        post_sumsq += v * v
        if v > post_peak:
            post_peak = v

    pre_key = int(pre_by_key.get(key, 0))
    post_key = int(post_by_key.get(key, 0))

    def cert(certified: bool, verdict: str) -> NetEffectCertificate:
        return NetEffectCertificate(
            key=key,
            certified=certified,
            verdict=verdict,
            pre_total_cc=pre_total,
            post_total_cc=post_total,
            pre_key_cc=pre_key,
            post_key_cc=post_key,
            pre_sumsq=pre_sumsq,
            post_sumsq=post_sumsq,
            pre_peak_cc=pre_peak,
            post_peak_cc=post_peak,
            n_keys_pre=sum(1 for v in pre_by_key.values() if int(v) > 0),
            n_keys_post=sum(1 for v in post_by_key.values() if int(v) > 0),
        )

    # Fail-closed on a state that cannot support a ratio (UNKNOWN is never safe).
    if pre_total <= 0 or post_total <= 0 or pre_sumsq <= 0 or post_sumsq <= 0:
        return cert(False, "degenerate_state")
    if post_key <= 0:
        return cert(False, "key_absent_post")

    # T1 — the breaching key's DOLLAR SHARE must not rise.
    if post_key * pre_total > pre_key * post_total:
        return cert(False, "key_share_rises")
    # T2 — the book's PEAK dollar share must not rise.
    if post_peak * pre_total > pre_peak * post_total:
        return cert(False, "peak_share_rises")
    # T3 — dollar-weighted effective N must STRICTLY rise.
    if post_total * post_total * pre_sumsq <= pre_total * pre_total * post_sumsq:
        return cert(False, "eff_n_not_increased")
    return cert(True, "diversifying")
