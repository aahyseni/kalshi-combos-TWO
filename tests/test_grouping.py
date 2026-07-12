"""pricing.grouping.game_key — the ONE public correlation/aggregation key.

Two guarantees pinned here:
1. PARITY: the public ``game_key`` is byte-identical to the private
   ``relationships._game_key`` the copula already correlates on (they are one
   definition — this test forbids future drift if that ever changes).
2. The documented behaviour: SERIES-GAMECODE -> GAMECODE, no-hyphen -> identity,
   and the load-bearing fact that different market FAMILIES of one match share
   the game code (so the risk book can cluster them).
"""

from __future__ import annotations

from combomaker.pricing.grouping import game_key
from combomaker.pricing.relationships import _game_key


class TestGameKeyParity:
    def test_public_is_the_same_object_as_private(self) -> None:
        # They are one definition today; identity is the strongest parity.
        assert game_key is _game_key

    def test_parity_over_a_vocabulary(self) -> None:
        samples = [
            "KXWCGAME-26JUL05MEXENG",
            "KXWCTOTAL-26JUL05MEXENG",
            "KXWC1HTOTAL-26JUL05MEXENG",
            "KXMLBGAME-26JUL092145COLSF",
            "KXMLBHR-26JUL092145COLSF-COLHGOODMAN15-1",
            "NOHYPHEN",
            "",
            "A-B-C",
        ]
        for t in samples:
            assert game_key(t) == _game_key(t)


class TestGameKeyBehaviour:
    def test_series_gamecode_yields_gamecode(self) -> None:
        assert game_key("KXWCGAME-26JUL05MEXENG") == "26JUL05MEXENG"

    def test_families_of_one_match_share_the_key(self) -> None:
        # The whole point: distinct EVENTS (series differ) but ONE game.
        g = "26JUL05MEXENG"
        assert game_key(f"KXWCGAME-{g}") == game_key(f"KXWCTOTAL-{g}") == g

    def test_no_hyphen_is_identity_fail_closed(self) -> None:
        # A synthetic/degenerate ticker never merges with another game.
        assert game_key("SYNTHETIC") == "SYNTHETIC"

    def test_only_first_hyphen_splits_the_series(self) -> None:
        # partition on the FIRST hyphen — a game code may itself contain none,
        # but a series prefix never does.
        assert game_key("KXWCGAME-26JUL05MEXENG") == "26JUL05MEXENG"
        assert game_key("PFX-a-b") == "a-b"
