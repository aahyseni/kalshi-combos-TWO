"""BYTE-IDENTITY GOLDEN for the KILL-anchored book gate (2026-07-29).

Rule 8 (testing isolation): imports and DRIVES the live ``LimitChecker`` and the
live ``evaluate_candidate_book_risk`` gate; edits nothing.

WHY IT EXISTS. The re-anchor ships behind ONE arming flag defaulting to SHADOW,
and "shadow is byte-identical to today" is the load-bearing claim. A test that
compares the new code against itself with the flag off proves nothing. So this
script is run TWICE:

    # at HEAD, BEFORE the change
    python -m tools.diagnostics.kill_anchor_shadow_golden
        --emit tests/fixtures/kill_anchor_shadow_golden.json
    # after the change (flag defaults False)
    python -m tools.diagnostics.kill_anchor_shadow_golden
        --verify tests/fixtures/kill_anchor_shadow_golden.json

The golden records the EXACT breach tuples (reason, detail, shadow) the
pre-change checker produced on 20,000 randomized (limits x snapshot x bankroll)
cases plus the exact candidate-gate verdicts on 2,000 randomized books, so a
post-change run that differs anywhere fails loudly. ``tests/test_kill_anchored_
book_gate.py`` re-verifies the same file in the suite.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from fractions import Fraction
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for _p in (str(REPO), str(REPO / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from combomaker.core.conventions import Conventions, Side  # noqa: E402
from combomaker.core.quantity import CentiContracts  # noqa: E402
from combomaker.risk.exposure import (  # noqa: E402
    ExposureBook,
    LegRef,
    OpenPosition,
)
from combomaker.risk.limits import (  # noqa: E402
    DailyPnl,
    LimitChecker,
    RiskLimits,
)
from combomaker.sim.book_risk import evaluate_candidate_book_risk  # noqa: E402

CONVENTIONS = Conventions(
    verified=True,
    source="shadow-golden",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)


class FakeSnapshot:
    """A ``PortfolioRisk`` stand-in with every field the CVaR/det-max/ruin axes
    read. Deliberately a plain object (not the real dataclass) so the fuzz can
    reach shapes a real MC would take many minutes to produce."""

    def __init__(
        self,
        *,
        usable: bool,
        es: float,
        det: float,
        mutex: float | None,
        credit: float,
        p_ruin: float,
        quantiles: tuple[float, ...],
        n_samples: int,
    ) -> None:
        self.usable = usable
        self.governing_model_es_99_cc = es
        self.deterministic_max_loss_cc = det
        self.mutex_aware_det_max_cc = mutex
        self.det_max_hedge_credit_cc = credit
        self.p_ruin = p_ruin
        self.p_ruin_upper = p_ruin
        self.loss_quantiles_cc = quantiles
        self.n_samples = n_samples


def _rand_quantiles(rng: random.Random, top: float) -> tuple[float, ...]:
    """A monotone 1001-point loss-quantile envelope shaped like a real one."""
    if top <= 0:
        return ()
    pts = sorted(rng.random() ** rng.uniform(0.5, 4.0) * top for _ in range(1001))
    return tuple(pts)


def _case(rng: random.Random) -> tuple[RiskLimits, FakeSnapshot, int]:
    bankroll = rng.randrange(50_000, 60_000_000)
    limits = RiskLimits(
        caps_shadow_mode=rng.random() < 0.2,
        portfolio_cvar_frac=Fraction(rng.randrange(2, 60), 100),
        portfolio_det_max_frac=Fraction(rng.randrange(2, 90), 100),
        hard_trip_frac=Fraction(rng.randrange(2, 30), 100),
        portfolio_tail_prob_gate=rng.random() < 0.6,
        portfolio_kill_tail_prob=rng.choice([0.005, 0.01, 0.02, 0.05, 0.1]),
        portfolio_tail_prob_ci_z=rng.choice([0.0, 1.645, 2.326]),
        portfolio_det_max_mutex_aware=rng.random() < 0.8,
        det_max_hedge_credit=rng.random() < 0.5,
        portfolio_ruin_prob_budget=Fraction(rng.randrange(1, 20), 100),
    )
    top = rng.uniform(0.0, 1.4) * bankroll
    det = rng.uniform(0.0, 1.4) * bankroll
    mutex = None if rng.random() < 0.15 else det * rng.uniform(0.3, 1.0)
    snap = FakeSnapshot(
        usable=rng.random() < 0.85,
        es=rng.uniform(0.0, 0.9) * bankroll,
        det=det,
        mutex=mutex,
        credit=rng.uniform(0.0, 0.4) * det,
        p_ruin=rng.choice([0.0, 0.0, 0.001, 0.02, 0.05, 0.2]),
        quantiles=() if rng.random() < 0.1 else _rand_quantiles(rng, top),
        n_samples=0 if rng.random() < 0.05 else rng.choice([2_000, 20_000]),
    )
    return limits, snap, bankroll


def limits_rows(n: int, seed: int) -> list[list[list[object]]]:
    rng = random.Random(seed)
    out: list[list[list[object]]] = []
    for _ in range(n):
        limits, snap, bankroll = _case(rng)
        breaches = LimitChecker(limits).check(
            ExposureBook(CONVENTIONS),
            lambda _t: 0.5,
            DailyPnl(),
            risk_bankroll_cc=bankroll,
            book_risk=snap,
        )
        out.append(
            [[b.reason.value, b.detail, bool(b.shadow)] for b in breaches]
        )
    return out


# --- candidate gate ---------------------------------------------------------

def _pos(pid: str, legs: tuple[LegRef, ...], price_cc: int) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=f"COMBO-{pid}",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(100),
        entry_price_cc=price_cc,  # type: ignore[arg-type]
        legs=legs,
    )


def gate_rows(n: int, seed: int) -> list[list[object]]:
    rng = random.Random(seed)
    out: list[list[object]] = []
    for _ in range(n):
        n_games = rng.randrange(1, 7)
        n_pos = rng.randrange(1, 9)
        committed = [
            _pos(
                f"c{i}",
                (
                    LegRef(
                        market_ticker=f"L{rng.randrange(n_games)}",
                        event_ticker=f"KXG-G{rng.randrange(n_games)}",
                        side="yes",
                    ),
                ),
                rng.randrange(1_000, 8_000),
            )
            for i in range(n_pos)
        ]
        cand = _pos(
            "cand",
            (
                LegRef(
                    market_ticker=f"L{rng.randrange(n_games)}",
                    event_ticker=f"KXG-G{rng.randrange(n_games)}",
                    side="yes",
                ),
            ),
            rng.randrange(1_000, 8_000),
        )
        verdict = evaluate_candidate_book_risk(
            committed,
            cand,
            marginals=lambda _t: 0.5,
            n_samples=2_000,
            seed=7,
            bankroll_cc=rng.randrange(100_000, 4_000_000),
            portfolio_cvar_frac=rng.uniform(0.05, 0.5),
            portfolio_det_max_frac=rng.uniform(0.05, 0.9),
            tail_prob_gate=rng.random() < 0.6,
            kill_tail_prob=rng.choice([0.01, 0.02, 0.05]),
            det_max_hedge_credit=rng.random() < 0.5,
        )
        out.append([bool(verdict.confirm), verdict.decline_reason])
    return out


def build(limits_n: int, gate_n: int) -> dict[str, object]:
    return {
        "limits_cases": limits_n,
        "gate_cases": gate_n,
        "limits": limits_rows(limits_n, seed=20260729),
        "gate": gate_rows(gate_n, seed=915),
    }


def digest(payload: dict[str, object]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit")
    ap.add_argument("--verify")
    ap.add_argument("--limits-cases", type=int, default=20_000)
    ap.add_argument("--gate-cases", type=int, default=2_000)
    args = ap.parse_args()

    payload = build(args.limits_cases, args.gate_cases)
    print(f"limits cases {args.limits_cases}  gate cases {args.gate_cases}")
    print(f"digest {digest(payload)}")

    if args.emit:
        p = Path(args.emit)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, separators=(",", ":")))
        print(f"WROTE {p}")
        return 0
    if args.verify:
        gold = json.loads(Path(args.verify).read_text())
        if gold == payload:
            print("SHADOW BYTE-IDENTICAL: PASS")
            return 0
        bad = sum(
            1
            for a, b in zip(gold["limits"], payload["limits"], strict=False)
            if a != b
        )
        bad_g = sum(
            1 for a, b in zip(gold["gate"], payload["gate"], strict=False) if a != b
        )
        print(f"MISMATCH: limits {bad} rows, gate {bad_g} rows")
        for a, b in zip(gold["limits"], payload["limits"], strict=False):
            if a != b:
                print("  gold:", a)
                print("  new :", b)
                break
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
