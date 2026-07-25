"""TAIL-PROBABILITY book gate (operator anchor ratified 2026-07-25).

"More bets = more variance = more money": the TOTAL-book joint-tail budget
binds the PROBABILITY of a KILL-distance night instead of the ES99 average —
which at small N with ~50%-loss positions barely credits diversification
(the worst 1% is "most lose at once" even independent) and capped total
premium near the KILL distance regardless of variance. Pinned here:

- candidate gate: a ONE-WAY book (co-losing positions) still DECLINES under
  the probability form; a DIVERSIFIED book of the SAME total premium that
  the ES form blocks ADMITS (diversification buys capacity);
- flag off = byte-identical ES behavior (reason string pinned);
- quote-time cap: same pair of behaviors off the snapshot's loss-quantile
  envelope; legacy snapshots (no envelope) fall back to the ES form;
- snapshot: the envelope is populated, sized, monotone.
"""

from __future__ import annotations

from fractions import Fraction

from combomaker.core.conventions import Conventions, Side
from combomaker.core.quantity import CentiContracts
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.limits import DailyPnl, LimitChecker, RiskLimits
from combomaker.sim.book_risk import (
    compute_book_risk,
    evaluate_candidate_book_risk,
)
from combomaker.sim.book_model import build_book_model


def _pos(
    position_id: str,
    legs: tuple[LegRef, ...],
    *,
    contracts: int = 100,
    price_cc: int = 5_000,
) -> OpenPosition:
    return OpenPosition(
        position_id=position_id,
        combo_ticker=f"COMBO-{position_id}",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(contracts),
        entry_price_cc=price_cc,  # type: ignore[arg-type]
        legs=legs,
    )


def _leg(ticker: str, event: str) -> LegRef:
    return LegRef(market_ticker=ticker, event_ticker=event, side="yes")


# Marginals at 0.5: each sold parlay forfeits its premium ~half the time —
# the high-loss-probability regime where ES99 ~= the comonotone sum even for
# independent games (the exact shape the operator objected to).
P_HIT = 0.5
BANKROLL = 200_000  # $20 — KILL-scale threshold = cvar_frac x bankroll


def _one_way_book(n: int) -> list[OpenPosition]:
    """n positions ALL on one game/leg — they co-lose by construction."""
    return [_pos(f"c{i}", (_leg("A", "KXWCGAME-G1"),)) for i in range(n)]


def _diversified_book(n: int) -> list[OpenPosition]:
    """n positions on n INDEPENDENT games — same total premium as one-way."""
    return [
        _pos(f"c{i}", (_leg(f"L{i}", f"KXWCGAME-G{i}"),)) for i in range(n)
    ]


def _gate(committed, cand, **kw):
    return evaluate_candidate_book_risk(
        committed,
        cand,
        marginals=lambda t: P_HIT,
        n_samples=20_000,
        seed=11,
        bankroll_cc=BANKROLL,
        portfolio_cvar_frac=0.12,  # threshold = 24_000cc
        **kw,
    )


class TestCandidateGate:
    def test_diversified_book_admits_where_es_blocked(self) -> None:
        # 9 committed independent games + a 10th new-game candidate: total
        # premium 50_000cc >> the 24_000cc threshold, so the ES form blocks —
        # but P(loss >= 24_000) (>= 5 of 10 fair coins) ~ 62%?? No: each
        # position P&L is +-5_000cc (win +5_000 at p=0.5, lose -5_000), so
        # loss >= 24_000cc needs >= 8 losers of 10 (binomial tail ~ 5.5%).
        # With kill_tail_prob generous (0.10) the diversified book ADMITS
        # while the SAME book under the ES form is blocked.
        committed = _diversified_book(9)
        cand = _pos("cand", (_leg("L9", "KXWCGAME-G9X"),), price_cc=4_000)
        es_form = _gate(committed, cand)
        prob_form = _gate(
            committed, cand, tail_prob_gate=True, kill_tail_prob=0.10
        )
        assert not es_form.confirm
        assert es_form.decline_reason == "post_governing_model_es_over_budget"
        assert prob_form.confirm, prob_form.decline_reason

    def test_one_way_book_still_declines(self) -> None:
        # The SAME total premium all on ONE leg: everything loses together
        # ~50% of the time — far over any sane kill_tail_prob.
        committed = _one_way_book(9)
        cand = _pos("cand", (_leg("A", "KXWCGAME-G1"),), price_cc=4_000)
        prob_form = _gate(
            committed, cand, tail_prob_gate=True, kill_tail_prob=0.10
        )
        assert not prob_form.confirm
        assert prob_form.decline_reason == "post_kill_tail_prob_over_budget"

    def test_flag_off_is_byte_identical_es_form(self) -> None:
        committed = _diversified_book(9)
        cand = _pos("cand", (_leg("L9", "KXWCGAME-G9X"),), price_cc=4_000)
        default = _gate(committed, cand)
        explicit_off = _gate(committed, cand, tail_prob_gate=False)
        assert default.confirm == explicit_off.confirm
        assert default.decline_reason == explicit_off.decline_reason
        assert (
            default.post.governing_model_es_99_cc
            == explicit_off.post.governing_model_es_99_cc
        )


def _snapshot(positions):
    model = build_book_model(
        positions, marginals=lambda t: P_HIT, within_game_rho=None
    )
    return compute_book_risk(
        model, n_samples=20_000, seed=11, bankroll_cc=BANKROLL
    )


class TestSnapshotEnvelope:
    def test_envelope_populated_and_monotone(self) -> None:
        snap = _snapshot(_diversified_book(6))
        q = snap.loss_quantiles_cc
        assert len(q) == 1001
        assert all(q[i] <= q[i + 1] for i in range(len(q) - 1))
        # Envelope dominates the production ES99 quantile neighborhood: the
        # 99th-percentile loss point is at least the production VaR99.
        assert q[990] >= snap.var_99_cc - 1e-6


def _limits(**kw) -> RiskLimits:
    return RiskLimits(
        caps_shadow_mode=False,
        portfolio_cvar_frac=Fraction(12, 100),
        **kw,
    )


CONVENTIONS = Conventions(
    verified=True,
    source="test",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)


class TestQuoteTimeCap:
    def _breaches(self, limits: RiskLimits, snap, bankroll: int):
        checker = LimitChecker(limits)
        return [
            b.reason
            for b in checker.check(
                ExposureBook(CONVENTIONS),
                lambda t: P_HIT,
                DailyPnl(),
                risk_bankroll_cc=bankroll,
                book_risk=snap,
            )
            if "portfolio_cvar" in str(b.reason).lower()
        ]

    def test_prob_form_admits_diversified_blocks_one_way(self) -> None:
        div = _snapshot(_diversified_book(10))
        one_way = _snapshot(_one_way_book(10))
        prob_limits = _limits(
            portfolio_tail_prob_gate=True, portfolio_kill_tail_prob=0.10
        )
        es_limits = _limits()
        # ES form blocks BOTH books (total premium >> threshold)...
        assert self._breaches(es_limits, div, BANKROLL)
        assert self._breaches(es_limits, one_way, BANKROLL)
        # ...the probability form distinguishes them.
        assert not self._breaches(prob_limits, div, BANKROLL)
        assert self._breaches(prob_limits, one_way, BANKROLL)

    def test_legacy_snapshot_falls_back_to_es(self) -> None:
        snap = _snapshot(_diversified_book(10))
        object.__setattr__(snap, "loss_quantiles_cc", ())
        prob_limits = _limits(
            portfolio_tail_prob_gate=True, portfolio_kill_tail_prob=0.10
        )
        # No envelope => ES form governs (never a free pass).
        assert self._breaches(prob_limits, snap, BANKROLL)
