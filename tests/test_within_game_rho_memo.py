"""The within-game rho PAIR MEMO is byte-identical, and it is the reason the
confirm path stopped losing auctions (2026-07-27).

MEASURED CAUSE. The candidate gate resolves rho for EVERY unordered pair of
EVERY leg ticker in the book — O(T^2). Benchmarked at ~0.126 ms/pair, that was
730-790 ms of SYNCHRONOUS event-loop time per accept at the live book (T=108
tickers, 5,778 pairs) and it was the DOMINANT term: 23 of 24 auctions lost to
the clock in one session never reached a Monte Carlo sample at all.

WHY A MEMO IS LEGITIMATE HERE, and not an approximation: the expensive branch is
``build_sgp_correlation(<two synthetic legs>, [[0,1]], params, marginals=None)``
— a pure function of the ordered ticker pair and the provider's immutable
``SgpParams``. No clock, no live book, and marginals are literally None on that
branch. The NESTED-LADDER branch, which DOES read live marginals, is deliberately
left uncached; these tests pin both halves of that split.
"""

from __future__ import annotations

from combomaker.pricing.sgp import SgpParams
from combomaker.sim.within_game_rho import SgpWithinGameRho


def _params() -> SgpParams:
    return SgpParams(
        pair_rho={"moneyline|total": 0.18},
        default_rho=0.12,
        cross_event_rho=0.02,
        typed_uncertainty=0.10,
        untyped_uncertainty=0.25,
    )


A = "KXMLBGAME-26JUL27ATLNYM-ATL"
B = "KXMLBGAME-26JUL27ATLNYM-NYM"
C = "KXMLBTOTAL-26JUL27ATLNYM-T8.5"


def test_memo_returns_byte_identical_bands() -> None:
    cold = SgpWithinGameRho(_params())
    warm = SgpWithinGameRho(_params())
    pairs = [(A, B), (A, C), (B, C), (B, A), (C, A)]
    # Cold provider: every call recomputes (cache cleared between calls).
    expected = []
    for a, b in pairs:
        cold.invalidate_sgp_cache()
        expected.append(cold(a, b))
    # Warm provider: the memo serves repeats. The values must be IDENTICAL —
    # this is a memo, not an approximation.
    for (a, b), want in zip(pairs, expected, strict=True):
        assert warm(a, b) == want
        assert warm(a, b) == want  # second call is the cache hit
    assert warm.sgp_cache_size > 0


def test_self_pair_and_repeat_calls_are_stable() -> None:
    provider = SgpWithinGameRho(_params())
    assert provider(A, A) is None  # degenerate self-pair: no off-diagonal
    first = provider(A, C)
    for _ in range(50):
        assert provider(A, C) == first


def test_memo_never_serves_a_marginal_dependent_nested_band() -> None:
    # THE CACHE-SAFETY INVARIANT. The nested-ladder branch reads LIVE marginals,
    # so its band legitimately MOVES with the market; only the marginal-free
    # branch may be memoised. Sweep a realistic ticker mix and assert that NO
    # ``same_nested_ladder`` pair ever lands in the cache, and that every nested
    # pair still tracks a marginal change instead of being frozen.
    from combomaker.pricing.sgp import same_nested_ladder

    base = "KXMLBHRR-26JUL27ATLNYM-ACUNA"
    tickers = [
        f"{base}-1",
        f"{base}-2",
        f"{base}-3",
        "KXMLBGAME-26JUL27ATLNYM-ATL",
        "KXMLBTOTAL-26JUL27ATLNYM-T8.5",
    ]
    provider = SgpWithinGameRho(_params())
    provider.bind_marginals(lambda t: 0.60)

    nested_pairs = []
    for i, a in enumerate(tickers):
        for b in tickers[i + 1 :]:
            provider(a, b)
            if same_nested_ladder(a, b):
                nested_pairs.append((a, b))
    assert nested_pairs, "fixture must contain at least one nested-ladder pair"
    for a, b in nested_pairs:
        assert (a, b) not in provider._sgp_cache  # noqa: SLF001
        assert (b, a) not in provider._sgp_cache  # noqa: SLF001

    # A nested pair RE-READS the live marginals on EVERY call — it is never
    # served from a frozen entry. (Its resolved value is the comonotone constant
    # that nesting forces, so the observable proof is the read, not the number.)
    a, b = nested_pairs[0]
    reads: list[str] = []

    def counting(t: str) -> float:
        reads.append(t)
        return 0.40

    provider.bind_marginals(counting)
    provider(a, b)
    provider(a, b)
    assert len(reads) == 4  # two legs, two calls — no call was short-circuited


def test_memo_bound_clears_instead_of_growing_without_limit() -> None:
    provider = SgpWithinGameRho(_params(), sgp_cache_max_pairs=4)
    for i in range(12):
        provider(f"KXMLBGAME-26JUL27ATLNYM-T{i}", "KXMLBGAME-26JUL27ATLNYM-ATL")
    assert provider.sgp_cache_size <= 4


def test_memo_removes_the_repeat_cost_that_blew_the_confirm_window() -> None:
    # The live shape: a ticker universe whose pair count is resolved once per
    # accept. Book-to-book the set barely moves, so a memo makes each accept pay
    # only for genuinely NEW pairs. This asserts the SHAPE of that win (repeat
    # resolution is free), not a wall-clock number.
    import time

    tickers = [f"KXMLBGAME-26JUL27ATLNYM-M{i}" for i in range(60)]
    provider = SgpWithinGameRho(_params())

    def sweep() -> float:
        t0 = time.perf_counter()
        for i in range(len(tickers)):
            for j in range(i + 1, len(tickers)):
                provider(tickers[i], tickers[j])
        return time.perf_counter() - t0

    cold = sweep()
    warm = sweep()
    assert warm < cold / 5.0, f"cold={cold * 1e3:.1f}ms warm={warm * 1e3:.1f}ms"
