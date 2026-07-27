"""CHEAP ΔP(book) MARGINAL — prototype, FIDELITY test and BENCHMARK.

Phase 2 of 2 (phase 1 = ``pbook_cheap_marginal_collect.py``). Fully OFFLINE: it
replays the pickled REAL ``CandidateBookRiskInputs`` captured from the live wire.

THE QUESTION: ``candidate_delta_p_book`` (sim/book_risk.py:2381) is the measured
marginal effect of a fill on P(book P&L > 0). It costs a ~20k-sample portfolio MC
per candidate (~10^2 ms), so it can only run at CONFIRM (rare). The quote path runs
3,000+/min and prices instead off the leg-axis heuristic. Can the MEASURED marginal
be made cheap enough to price with?

THE STRUCTURE THAT MAKES IT POSSIBLE (established by reading the live model):
  * ``build_book_model`` builds a BLOCK-DIAGONAL correlation: cross-game rho is
    ``DEFAULT_CROSS_EVENT_RHO = 0.0`` (sim/book_model.py:85) and the TAIL-STRESS
    joint the gate samples is ONE equicorrelated block PER GAME carrying that
    game's most conservative pair rho (``_tail_stress_blocks``).
  * The gate's PRE/POST P&L ride the SAME sampled leg-value matrix (CRN), and
    ``post_pnl = pre_pnl + candidate_pnl`` EXACTLY (``book_pnl`` is a sum over
    positions). So
        Δp_book = mean(pre_pnl + cand_pnl > 0) − mean(pre_pnl > 0)
    is a pure function of (a) the book's P&L VECTOR and (b) the candidate's own
    per-scenario P&L vector on the SAME scenarios.
  * (a) is already computed every ~15s by the BookRiskPool snapshot worker.
  * (b) needs only the candidate's legs' sampled values. Legs already in the book's
    universe are a COLUMN LOOKUP. Legs on a game the book does not hold are
    INDEPENDENT of everything cached (cross-game rho = 0) ⇒ an exact fresh draw.
    Legs on a game the book DOES hold need the conditional law of a new member of
    an equicorrelated block given the cached members — a closed form:
        E[z_new | z_S] = rho·Σz_S / (1 + (k−1)rho),
        Var[z_new | z_S] = 1 − rho²·k / (1 + (k−1)rho).

TIERS (reported separately — a cheap signal must be honest about which regime it
is in): T0 = every candidate leg already cached; T1 = new legs, all on games the
book does not hold (exact); T2 = at least one new leg on a game the book holds
(the conditional approximation + the game-block rho re-collapse).

Hard rule 8 (testing isolation): no live module is edited. The truth column is the
live ``evaluate_candidate_book_risk``. The prototype reuses the live
``build_book_model`` / ``_select_sampler`` / ``position_to_combo`` / ``book_pnl``.
The ONE duplicated fragment is the Gaussian-copula draw (5 lines of
``sim/engine.sample_leg_values``), duplicated ONLY so the prototype can retain the
latent ``z`` the live signature does not return; it is PARITY-CHECKED bit-for-bit
against the live sampler on the same seeded rng before any number is reported.
KEEP IN SYNC WITH: ``sim/engine.sample_leg_values``.

    .venv/Scripts/python.exe tools/diagnostics/pbook_cheap_marginal_fidelity.py \
        --inputs data/_pbook_cheap/inputs.pkl
"""

from __future__ import annotations

import argparse
import math
import pickle
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from scipy.special import ndtr

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from combomaker.ops.pricing_pool import (  # noqa: E402
    _DictMarginals,
    _DictWithinGameRho,
    _worker_candidate_book_risk,
)
from combomaker.pricing.grouping import game_key  # noqa: E402
from combomaker.risk.exposure import OpenPosition  # noqa: E402
from combomaker.sim.book_model import build_book_model, position_to_combo  # noqa: E402
from combomaker.sim.book_risk import _select_sampler  # noqa: E402
from combomaker.sim.engine import (  # noqa: E402
    CC_PER_DOLLAR,
    book_pnl,
    sample_leg_values,
)

# --------------------------------------------------------------------------- #
# THE SNAPSHOT CACHE — everything the ~15s book-risk worker would keep.        #
# --------------------------------------------------------------------------- #


@dataclass
class BookCache:
    """One book snapshot's reusable CRN state. Built OFF the quote path."""

    n: int
    leg_index: dict[str, int]
    values: NDArray[np.float64]           # (n, L) sampled leg values
    pre_pnl: NDArray[np.float64]          # (n,) book P&L on those scenarios
    p_pre: float
    # per-GAME equicorrelated-block state for conditional new-leg draws
    game_of_leg: dict[str, str]           # ticker -> game key
    block_rho: dict[str, float]           # game key -> block rho (tail-stress band)
    z_sum: dict[str, NDArray[np.float64]]  # game key -> Σ z over cached legs
    z_k: dict[str, int]                   # game key -> #cached legs
    spare: NDArray[np.float64]            # (n, S) pre-drawn standard normals
    structural: bool = False
    # QUOTE-PATH FAST FORM (see cheap_delta_fast): pre_pnl sorted + the leg value
    # columns permuted into that order and bit-packed, so a candidate reduces to
    # k bitwise ANDs over n/8 bytes + two suffix popcounts.
    pre_sorted: NDArray[np.float64] = field(default_factory=lambda: np.zeros(0))
    packed: NDArray[np.uint8] = field(default_factory=lambda: np.zeros((0, 0), np.uint8))
    _spare_used: int = 0


def _draw_with_latent(
    legs: Any, corr: NDArray[np.float64], n: int, rng: np.random.Generator
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """KEEP IN SYNC WITH ``sim/engine.sample_leg_values`` — identical draw, but it
    also returns the latent ``z``. Parity-checked in ``_parity_check`` below."""
    corr_arr = np.asarray(corr, dtype=np.float64)
    try:
        chol = np.linalg.cholesky(corr_arr)
    except np.linalg.LinAlgError:
        chol = np.linalg.cholesky(corr_arr + 1e-12 * np.eye(corr_arr.shape[0]))
    z = rng.standard_normal((n, len(legs))) @ chol.T
    u = np.asarray(ndtr(z), dtype=np.float64)
    out = np.empty((n, len(legs)), dtype=np.float64)
    for j, leg in enumerate(legs):
        if leg.settlement is None:
            vals = np.array([0.0, 1.0])
            probs = np.array([1.0 - leg.p, leg.p])
        else:
            vals = np.array([v for v, _ in leg.settlement])
            probs = np.array([q for _, q in leg.settlement])
        cum = np.cumsum(probs)
        cum[-1] = np.inf
        out[:, j] = vals[np.searchsorted(cum, u[:, j], side="right")]
    return out, z


def _parity_check(model: Any, corr: NDArray[np.float64], n: int, seed: int) -> bool:
    a, _ = _draw_with_latent(model.legs, corr, n, np.random.default_rng(seed))
    b = sample_leg_values(model.legs, corr, n, np.random.default_rng(seed))
    return bool(np.array_equal(a, b))


def build_cache(
    pre_positions: list[OpenPosition],
    marginals: Any,
    rho: Any,
    structural_cfg: Any,
    *,
    n: int,
    seed: int,
    band: str = "high",
    spare_cols: int = 12,
    universe_positions: list[OpenPosition] | None = None,
) -> BookCache | None:
    """Build one snapshot's reusable CRN state.

    MODE A (``universe_positions=None``): the sampled leg universe is the BOOK's
    own legs — what ``compute_book_risk`` samples today. A candidate leg outside it
    must be drawn from a conditional law (approximate).

    MODE B (``universe_positions`` supplied): the universe is EXTENDED to cover the
    legs we expect to QUOTE (in production: the subscribed quotable leg set). The
    P&L vector is still the BOOK's (only ``pre_positions`` are scored), but every
    candidate leg is then a COLUMN LOOKUP — no conditional approximation anywhere,
    and the structural games are inverted ONCE over the full universe. The extra
    cost is a wider MC in the ~15s snapshot worker, entirely OFF the quote path."""
    model_positions = (
        pre_positions if universe_positions is None
        else [*pre_positions, *universe_positions]
    )
    model = build_book_model(
        model_positions, marginals=marginals, within_game_rho=rho
    )
    if model.unknown or not model.legs:
        return None
    corr = model.corr_tail_stress_for_band(band)
    bundle = _select_sampler(model, structural_cfg)
    rng = np.random.default_rng(seed)
    if bundle.structural:
        values = bundle.sampler(model.legs, corr, n, rng)
        z = np.zeros((n, len(model.legs)))
    else:
        values, z = _draw_with_latent(model.legs, corr, n, rng)

    def _modeled(p: OpenPosition) -> bool:
        return p.risk_modeled and all(
            leg.market_ticker in model.leg_index for leg in p.legs
        )

    combos = [position_to_combo(p, model.leg_index) for p in pre_positions if _modeled(p)]
    pre = book_pnl(values, combos) if combos else np.zeros(n)

    game_of_leg: dict[str, str] = {}
    for t, i in model.leg_index.items():
        ev = model.event_by_index.get(i)
        game_of_leg[t] = game_key(ev) if ev else f"__nogame__{i}"
    z_sum: dict[str, NDArray[np.float64]] = {}
    z_k: dict[str, int] = {}
    block_rho: dict[str, float] = {}
    members: dict[str, list[int]] = {}
    for t, i in model.leg_index.items():
        members.setdefault(game_of_leg[t], []).append(i)
    for g, idx in members.items():
        z_sum[g] = z[:, idx].sum(axis=1)
        z_k[g] = len(idx)
        block_rho[g] = float(corr[idx[0], idx[1]]) if len(idx) > 1 else 0.0

    order = np.argsort(pre, kind="stable")
    packed = np.packbits((values[order] > 0.5).T, axis=1)  # (L, ceil(n/8))
    return BookCache(
        n=n,
        leg_index=dict(model.leg_index),
        values=values,
        pre_pnl=pre,
        p_pre=float(np.mean(pre > 0.0)),
        game_of_leg=game_of_leg,
        block_rho=block_rho,
        z_sum=z_sum,
        z_k=z_k,
        spare=np.random.default_rng(seed + 1_000_003).standard_normal((n, spare_cols)),
        structural=bundle.structural,
        pre_sorted=pre[order],
        packed=packed,
    )


# --------------------------------------------------------------------------- #
# THE CHEAP MARGINAL                                                          #
# --------------------------------------------------------------------------- #


def _candidate_columns(
    cache: BookCache,
    candidate: OpenPosition,
    marg: dict[str, float],
    rho_pairs: dict[frozenset[str], tuple[float, float, float]] | None = None,
) -> tuple[list[NDArray[np.float64]], str] | None:
    """Per-leg sampled value columns for the candidate + its tier.

    A leg already in the cached universe is a COLUMN LOOKUP (exact). A leg the
    candidate INTRODUCES is drawn from the equicorrelated block's conditional law.
    NEW legs on the SAME game must share that game's block factor (they are
    conditionally correlated with EACH OTHER, not just with the cached members):

        z_new_i = mu_g + sqrt(d_g)·G_g + sqrt(1−rho_g)·eps_i
        mu_g    = rho_g·Σz_cached / (1 + (k−1)rho_g)      (0 for an uncached game)
        d_g     = rho_g(1 − rho_g) / (1 + (k−1)rho_g)     (= rho_g when k = 0)

    which reproduces exactly Var = 1 and the conditional covariance
    rho_g − rho_g²k/(1+(k−1)rho_g) between two new members.
    """
    cols: list[NDArray[np.float64]] = []
    tier = "T0"
    spare = 0
    # group the NEW legs by game so same-game newcomers share a block factor
    new_by_game: dict[str, list[int]] = {}
    game_of: list[str | None] = []
    for li, leg in enumerate(candidate.legs):
        if leg.market_ticker in cache.leg_index:
            game_of.append(None)
            continue
        g = game_key(leg.event_ticker) if leg.event_ticker else f"__nogame__{li}"
        game_of.append(g)
        new_by_game.setdefault(g, []).append(li)

    z_new: dict[int, NDArray[np.float64]] = {}
    for g, idxs in new_by_game.items():
        cached = g in cache.z_k
        tier = "T2" if cached else ("T1" if tier == "T0" else tier)
        if cached:
            rho = cache.block_rho.get(g, 0.0)
            k = cache.z_k[g]
        else:
            # the merged model would collapse this new game's block to the MAX
            # pair rho at band=high over the candidate's own legs in it.
            rho = 0.0
            if rho_pairs and len(idxs) > 1:
                bands = [
                    rho_pairs.get(
                        frozenset(
                            (
                                candidate.legs[a].market_ticker,
                                candidate.legs[b].market_ticker,
                            )
                        )
                    )
                    for ai, a in enumerate(idxs)
                    for b in idxs[ai + 1:]
                ]
                vals = [b[2] if b is not None else 0.40 for b in bands]  # flat high
                rho = max(vals) if vals else 0.0
            k = 0
        rho = max(-0.999, min(0.999, rho))
        denom = 1.0 + (k - 1) * rho if k else 1.0
        if k and denom > 1e-9:
            mu = rho * cache.z_sum[g] / denom
            d = max(0.0, rho * (1.0 - rho) / denom)
        else:
            mu = 0.0
            d = max(0.0, rho)
        if spare + len(idxs) + 1 > cache.spare.shape[1]:
            return None
        shared = cache.spare[:, spare]
        spare += 1
        idio_scale = math.sqrt(max(0.0, 1.0 - rho)) if (k or len(idxs) > 1) else 1.0
        if not k and len(idxs) == 1:
            idio_scale, d = 1.0, 0.0
        for li in idxs:
            eps = cache.spare[:, spare]
            spare += 1
            z_new[li] = mu + math.sqrt(d) * shared + idio_scale * eps

    for li, leg in enumerate(candidate.legs):
        t = leg.market_ticker
        j = cache.leg_index.get(t)
        if j is not None:
            cols.append(cache.values[:, j])
            continue
        p = marg.get(t)
        if p is None:
            return None
        cols.append((ndtr(z_new[li]) > (1.0 - p)).astype(np.float64))
    return cols, tier


def cheap_delta(
    cache: BookCache,
    candidate: OpenPosition,
    marg: dict[str, float],
    rho_pairs: dict[frozenset[str], tuple[float, float, float]] | None = None,
) -> tuple[float, str] | None:
    got = _candidate_columns(cache, candidate, marg, rho_pairs)
    if got is None:
        return None
    cols, tier = got
    payout = np.ones(cache.n)
    for leg, col in zip(candidate.legs, cols, strict=True):
        payout *= col if leg.side == "yes" else (1.0 - col)
    np.minimum(payout, 1.0, out=payout)
    payout *= float(CC_PER_DOLLAR)
    contracts = int(candidate.contracts) / 100.0
    price = float(candidate.entry_price_cc)
    if str(candidate.our_side) == "yes":
        per = payout - price
    else:
        per = (float(CC_PER_DOLLAR) - payout) - price
    cand = per * contracts
    post = cache.pre_pnl + cand
    return (
        float(np.mean(post > 0.0)) - cache.p_pre,
        tier,
        float(cand.mean()),
        float(np.mean(payout > 0.5 * float(CC_PER_DOLLAR))),
    )


_POPCNT = np.array([bin(i).count("1") for i in range(256)], dtype=np.int32)


def _suffix_popcount(packed_row: NDArray[np.uint8], i: int) -> int:
    """Popcount of bits at sample positions >= ``i`` (packbits is MSB-first)."""
    b0, rem = divmod(i, 8)
    total = int(_POPCNT[packed_row[b0 + 1:]].sum())
    if b0 < packed_row.size:
        total += int(_POPCNT[packed_row[b0] & (0xFF >> rem)])
    return total


def cheap_delta_fast(
    cache: BookCache, candidate: OpenPosition
) -> float | None:
    """QUOTE-PATH form for the T0 case (every candidate leg already cached).

    A binary-leg combo's P&L takes exactly TWO values — ``a`` when the parlay HITS
    and ``b`` when it misses — so with ``pre_pnl`` SORTED once per snapshot,
        Δp = [#(hit & pre > −a) + #(¬hit & pre > −b)] / n − p_pre
    and the two counts are SUFFIX POPCOUNTS of the bit-packed hit mask (the AND of
    the candidate's k packed leg columns). No float pass over n at all."""
    idx = []
    for leg in candidate.legs:
        j = cache.leg_index.get(leg.market_ticker)
        if j is None or leg.side != "yes":
            return None
        idx.append(j)
    hit = cache.packed[idx[0]]
    for j in idx[1:]:
        hit = hit & cache.packed[j]
    contracts = int(candidate.contracts) / 100.0
    price = float(candidate.entry_price_cc)
    if str(candidate.our_side) == "yes":
        a = (float(CC_PER_DOLLAR) - price) * contracts   # hit  ⇒ payout $1
        b = (0.0 - price) * contracts                    # miss ⇒ payout $0
    else:
        a = (0.0 - price) * contracts
        b = (float(CC_PER_DOLLAR) - price) * contracts
    n = cache.n
    i_a = int(np.searchsorted(cache.pre_sorted, -a, side="right"))
    i_b = int(np.searchsorted(cache.pre_sorted, -b, side="right"))
    n_hit_above_a = _suffix_popcount(hit, i_a)
    n_miss_above_b = (n - i_b) - _suffix_popcount(hit, i_b)
    return (n_hit_above_a + n_miss_above_b) / n - cache.p_pre


# --------------------------------------------------------------------------- #


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    return s[min(len(s) - 1, int(p * len(s)))]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", default="data/_pbook_cheap/inputs.pkl")
    ap.add_argument("--cache-n", type=int, default=20_000)
    ap.add_argument("--ref-n", type=int, default=40_000)
    ap.add_argument("--ref-reps", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--bench-iters", type=int, default=300)
    args = ap.parse_args()

    with open(args.inputs, "rb") as f:
        cases: list[dict[str, Any]] = pickle.load(f)
    if args.limit:
        cases = cases[: args.limit]
    print(f"cases: {len(cases)}")
    if not cases:
        return 1

    rows: list[dict[str, Any]] = []
    caches: dict[int, BookCache] = {}
    parity_done = False

    # ---- MODE B cache: one snapshot whose sampled leg universe covers the BOOK
    # plus every candidate leg in this sample (the production analogue: the
    # subscribed QUOTABLE leg set). Built ONCE, off the quote path.
    inp0 = cases[0]["inputs"]
    all_cands = [c["inputs"].candidate for c in cases]
    # The snapshot worker resolves marginals/rho for the WHOLE subscribed universe;
    # emulate that by unioning every case's resolved dicts.
    marg_all: dict[str, float] = {}
    rho_all: dict[Any, tuple[float, float, float]] = {}
    for c in cases:
        marg_all.update(c["inputs"].marginals)
        rho_all.update(c["inputs"].within_game_rho_pairs)
    t0 = time.perf_counter()
    cache_b = build_cache(
        [*inp0.committed, *inp0.reservations],
        _DictMarginals(marg_all),
        _DictWithinGameRho(rho_all),
        inp0.structural_cfg,
        n=args.cache_n,
        seed=inp0.seed,
        universe_positions=all_cands,
    )
    build_b_ms = (time.perf_counter() - t0) * 1e3
    if cache_b is not None:
        print(
            f"MODE B universe: {len(cache_b.leg_index)} legs   "
            f"snapshot build {build_b_ms:.0f} ms (off the quote path)"
        )

    for ci, case in enumerate(cases):
        inp = case["inputs"]
        marg = _DictMarginals(inp.marginals)
        rho = _DictWithinGameRho(inp.within_game_rho_pairs)
        pre_positions = [*inp.committed, *inp.reservations]

        # --- TRUTH: the LIVE gate, exactly as production runs it (n = 20k) -----
        t0 = time.perf_counter()
        truth = _worker_candidate_book_risk(inp)
        gate_ms = (time.perf_counter() - t0) * 1e3
        if truth.unknown:
            continue
        # --- REFERENCE: the SAME live evaluator, seed-averaged. K independent
        # ``ref_reps`` runs at ``ref_n`` samples each — statistically the same
        # precision as one K·ref_n run, at 1/K the peak memory (a 200k x 110-leg
        # value matrix thrashes; the averaged form does not). ``ref2`` is the
        # INDEPENDENT second half of the reps, so ref-vs-ref2 measures the residual
        # MC noise in the reference itself.
        import dataclasses as _dc

        reps = []
        for k in range(2 * args.ref_reps):
            r = _worker_candidate_book_risk(
                _dc.replace(inp, n_samples=args.ref_n, seed=inp.seed + 7919 * (k + 1))
            )
            reps.append(r)
        d_all = [r.candidate_delta_p_book for r in reps]
        ev_all = [r.candidate_ev_cc for r in reps]
        ppre_all = [r.pre.p_profit for r in reps]
        half = args.ref_reps

        class _Ref:
            candidate_delta_p_book = float(np.mean(d_all))
            candidate_ev_cc = float(np.mean(ev_all))

            class pre:
                p_profit = float(np.mean(ppre_all))

        ref = _Ref()
        ref_sem = float(np.std(d_all, ddof=1) / math.sqrt(len(d_all)))

        class _Ref2:
            candidate_delta_p_book = float(np.mean(d_all[:half]))

        class _Ref1:
            candidate_delta_p_book = float(np.mean(d_all[half:]))

        ref2 = _Ref2()
        ref1b = _Ref1()

        # --- CHEAP: one cache per distinct PRE book (here: one, reused) --------
        key = len(pre_positions)
        cache = caches.get(key)
        if cache is None:
            cache = build_cache(
                pre_positions, marg, rho, inp.structural_cfg,
                n=args.cache_n, seed=inp.seed,
            )
            if cache is None:
                continue
            caches[key] = cache
            if not parity_done:
                m = build_book_model(pre_positions, marginals=marg, within_game_rho=rho)
                ok = _parity_check(m, m.corr_tail_stress_for_band("high"), 2_000, 12345)
                print(f"copula-draw parity vs live sample_leg_values: {ok}")
                print(f"structural sampler active: {cache.structural}")
                print(f"MODE A universe: {len(cache.leg_index)} legs")
                parity_done = True

        got = cheap_delta(
            cache, inp.candidate, inp.marginals, inp.within_game_rho_pairs
        )
        if got is None:
            continue
        cheap, tier, cheap_ev, cheap_phit = got

        # --- MODE B: the EXTENDED-universe cache (every candidate is a lookup) --
        cheap_b = float("nan")
        cheap_b_ev = float("nan")
        tier_b = "-"
        if cache_b is not None:
            gb = cheap_delta(cache_b, inp.candidate, marg_all, rho_all)
            if gb is not None:
                cheap_b, tier_b, cheap_b_ev, _ = gb

        rows.append(
            {
                "rfq": case["rfq_id"][:8],
                "n_legs": case["n_legs"],
                "qty": case["risk_qty_cc"] / 100.0,
                "px": case["no_bid_cc"] / 100.0,
                "tier": tier,
                "truth20k": truth.candidate_delta_p_book,
                "ref": ref.candidate_delta_p_book,
                "ref2": ref2.candidate_delta_p_book,
                "ref1b": ref1b.candidate_delta_p_book,
                "ref_sem": ref_sem,
                "cheap": cheap,
                "cheapB": cheap_b,
                "gate_ms": gate_ms,
                "n_pre": truth.n_pre_positions,
                # DIAGNOSTICS: isolate WHERE any cheap-vs-truth gap comes from —
                # the PRE book reproduction vs the candidate's own marginal law.
                "p_pre_cheap": cache.p_pre,
                "p_pre_truth": ref.pre.p_profit,
                "ev_cheap": cheap_ev,
                "ev_cheapB": cheap_b_ev if cache_b is not None else float("nan"),
                "ev_truth": ref.candidate_ev_cc,
                "tierB": tier_b if cache_b is not None else "-",
            }
        )
        print(
            f"  [{ci + 1:>3}/{len(cases)}] {case['rfq_id'][:8]} {case['n_legs']}leg "
            f"{tier} truth20k={truth.candidate_delta_p_book:+.5f} "
            f"ref{2 * args.ref_reps}x{args.ref_n // 1000}k="
            f"{ref.candidate_delta_p_book:+.5f} "
            f"cheapA={cheap:+.5f} cheapB[{tier_b}]={cheap_b:+.5f}  |  "
            f"p_pre A{cache.p_pre:.4f}/B{(cache_b.p_pre if cache_b else 0):.4f}/"
            f"T{ref.pre.p_profit:.4f}  candEV A{cheap_ev:+.0f}/B{cheap_b_ev:+.0f}/"
            f"T{ref.candidate_ev_cc:+.0f}cc  gate={gate_ms:.0f}ms"
        )

    if not rows:
        print("no usable rows")
        return 1

    def report(name: str, est: str, truth: str, sub: list[dict[str, Any]]) -> None:
        if len(sub) < 2:
            print(f"\n{name}: n={len(sub)} (too few)")
            return
        e = np.array([r[est] for r in sub])
        t = np.array([r[truth] for r in sub])
        err = e - t
        corr = float(np.corrcoef(e, t)[0, 1]) if e.std() > 0 and t.std() > 0 else float("nan")
        both_nz = [(x, y) for x, y in zip(e, t) if x != 0 or y != 0]
        sign = float(np.mean([np.sign(x) == np.sign(y) for x, y in both_nz])) if both_nz else float("nan")
        print(
            f"\n{name}: n={len(sub)}\n"
            f"   pearson r        {corr:+.4f}\n"
            f"   spearman r       {_spearman(e, t):+.4f}\n"
            f"   sign agreement   {sign * 100:.1f}%\n"
            f"   err mean         {err.mean():+.6f}\n"
            f"   err |median|     {np.median(np.abs(err)):.6f}\n"
            f"   err |p90|        {np.quantile(np.abs(err), 0.9):.6f}\n"
            f"   err max|.|       {np.abs(err).max():.6f}\n"
            f"   truth sd         {t.std():.6f}"
        )

    print("\n" + "=" * 78)
    print("FIDELITY")
    print("=" * 78)
    report("MODE A (book-only universe) vs TRUTH(20k gate)", "cheap", "truth20k", rows)
    report("MODE A (book-only universe) vs REFERENCE", "cheap", "ref", rows)
    report("MODE B (extended universe) vs TRUTH(20k gate)", "cheapB", "truth20k", rows)
    report("MODE B (extended universe) vs REFERENCE", "cheapB", "ref", rows)
    report("GATE 20k vs REFERENCE  [the MC noise floor]", "truth20k", "ref", rows)
    report("REF half A vs REF half B  [pure MC noise]", "ref2", "ref1b", rows)
    print(
        f"\n   reference SEM of Δp_book: median "
        f"{np.median([r['ref_sem'] for r in rows]):.5f}"
    )
    for tier in ("T0", "T1", "T2"):
        sub = [r for r in rows if r["tier"] == tier]
        if sub:
            report(f"MODE A vs REFERENCE — tier {tier}", "cheap", "ref", sub)
    for k in (2, 3, 4, 5, 6):
        sub = [r for r in rows if r["n_legs"] == k]
        if len(sub) >= 4:
            report(f"MODE B vs REFERENCE — {k}-leg candidates", "cheapB", "ref", sub)

    # ---------------- BENCHMARK ------------------------------------------- #
    print("\n" + "=" * 78)
    print("BENCHMARK (per candidate, single core)")
    print("=" * 78)
    cache = cache_b if cache_b is not None else next(iter(caches.values()))
    print(f"  cache universe: {len(cache.leg_index)} legs, n={cache.n} scenarios")
    cand_list = [c["inputs"].candidate for c in cases[: len(rows)]]
    margs = cases[0]["inputs"].marginals
    rho_pairs_b = cases[0]["inputs"].within_game_rho_pairs

    for label, fn in (
        ("cheap_delta  (general, float pass over n)",
         lambda c: cheap_delta(cache, c, margs, rho_pairs_b)),
    ):
        # warm
        for c in cand_list[:5]:
            fn(c)
        t0 = time.perf_counter()
        it = 0
        while it < args.bench_iters:
            for c in cand_list:
                fn(c)
                it += 1
                if it >= args.bench_iters:
                    break
        us = (time.perf_counter() - t0) / it * 1e6
        print(f"  n={cache.n:>7}  {label:<48} {us:9.1f} us/candidate")

    t0_ok = [c for c in cand_list
             if all(l.market_ticker in cache.leg_index and l.side == "yes"
                    for l in c.legs)]
    if t0_ok:
        for c in t0_ok[:3]:
            cheap_delta_fast(cache, c)
        t0 = time.perf_counter()
        it = 0
        while it < args.bench_iters:
            for c in t0_ok:
                cheap_delta_fast(cache, c)
                it += 1
                if it >= args.bench_iters:
                    break
        us = (time.perf_counter() - t0) / it * 1e6
        print(f"  n={cache.n:>7}  {'cheap_delta_fast (T0 bitpacked)':<48} {us:9.1f} us/candidate")
    else:
        print("  (no all-cached candidate in this sample — fast form not benchmarked)")

    gms = [r["gate_ms"] for r in rows]
    print(f"\n  live gate MC (20k, full evaluate_candidate_book_risk): "
          f"median {statistics.median(gms):.0f} ms  "
          f"p90 {_pct(gms, 0.9):.0f} ms  n={len(gms)}")
    print(f"  ⇒ speedup vs the gate MC: "
          f"{statistics.median(gms) * 1000 / max(us, 1e-9):,.0f}x")
    return 0


def _spearman(a: NDArray[np.float64], b: NDArray[np.float64]) -> float:
    ra = np.argsort(np.argsort(a)).astype(float)
    rb = np.argsort(np.argsort(b)).astype(float)
    if ra.std() == 0 or rb.std() == 0:
        return float("nan")
    return float(np.corrcoef(ra, rb)[0, 1])


if __name__ == "__main__":
    raise SystemExit(main())
