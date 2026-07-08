"""The combo-trade poller must poll RECENTLY-seen combos (not a fixed
lexicographic slice), so coverage tracks whatever is actively RFQ'd — the fix
for the poller ossifying onto one persistent esports market as the seen set grew
past the batch size."""

from __future__ import annotations

from combomaker.ops.app import (
    _COMBO_POLL_FRESH,
    _COMBO_POLL_ROTATE,
    _COMBO_SEEN_CAP,
    ObserveApp,
)


def _app() -> ObserveApp:
    a = ObserveApp.__new__(ObserveApp)  # bypass full config init; we test 2 pure methods
    a._combo_tickers_seen = {}
    a._combo_poll_offset = 0
    return a


def test_mark_combo_seen_recency_and_cap() -> None:
    a = _app()
    for i in range(_COMBO_SEEN_CAP + 50):
        a._mark_combo_seen(f"T{i}")
    assert len(a._combo_tickers_seen) == _COMBO_SEEN_CAP          # capped
    assert "T0" not in a._combo_tickers_seen                       # oldest evicted
    assert f"T{_COMBO_SEEN_CAP + 49}" in a._combo_tickers_seen     # newest kept
    # re-seeing an old ticker moves it to most-recent (the tail the poller reads)
    oldest = next(iter(a._combo_tickers_seen))
    a._mark_combo_seen(oldest)
    assert list(a._combo_tickers_seen)[-1] == oldest


def test_poll_batch_freshest_first_and_lexicographic_slice_is_gone() -> None:
    a = _app()
    # seed with tickers whose lexicographic order is OPPOSITE their recency,
    # so a `sorted(...)[-20:]` would pick the WRONG (stale) ones.
    for i in range(200):
        a._mark_combo_seen(f"T{i:03d}")  # T199 newest, but T199 also sorts highest
    for i in range(200):
        a._mark_combo_seen(f"A{i:03d}")  # A* newest now, but sorts LOWEST
    batch = a._combo_poll_batch()
    # freshest-first: the most recently seen are the A* tail, never the T* that
    # sort highest — the exact ossification bug this fixes.
    assert batch[:_COMBO_POLL_FRESH] == [f"A{199 - i:03d}" for i in range(_COMBO_POLL_FRESH)]


def test_poll_batch_rotation_covers_every_recent_ticker() -> None:
    a = _app()
    n = 200
    for i in range(n):
        a._mark_combo_seen(f"T{i}")
    covered: set[str] = set()
    for _ in range(n // _COMBO_POLL_ROTATE + 3):
        covered |= set(a._combo_poll_batch())
    assert covered == {f"T{i}" for i in range(n)}  # rotation reaches all of them


def test_poll_batch_small_set() -> None:
    a = _app()
    a._mark_combo_seen("A")
    a._mark_combo_seen("B")
    assert set(a._combo_poll_batch()) == {"A", "B"}
