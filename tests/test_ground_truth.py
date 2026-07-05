from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from combomaker.ops.ground_truth import (
    GroundTruthError,
    derive_conventions,
    run_ground_truth,
)


def _pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


async def test_refuses_production_urls(tmp_path: Path) -> None:
    with pytest.raises(GroundTruthError, match="DEMO only"):
        await run_ground_truth(
            rest_base_url="https://external-api.kalshi.com/trade-api/v2",
            market_ticker="X",
            contracts_fp="1.00",
            out_dir=tmp_path,
        )


async def test_refuses_same_account_for_both_parties(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pem = _pem()
    monkeypatch.setenv("KALSHI_API_KEY_ID", "same-key")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PEM", pem)
    monkeypatch.setenv("KALSHI_REQUESTER_API_KEY_ID", "same-key")
    monkeypatch.setenv("KALSHI_REQUESTER_PRIVATE_KEY_PEM", pem)
    with pytest.raises(GroundTruthError, match="DIFFERENT demo accounts"):
        await run_ground_truth(
            rest_base_url="https://external-api.demo.kalshi.co/trade-api/v2",
            market_ticker="X",
            contracts_fp="1.00",
            out_dir=tmp_path,
        )


class TestDeriveConventions:
    def test_derives_sides_and_taker_flag_from_fills(self) -> None:
        yes_entries = [
            {
                "step": "fills_by_creator_order_id",
                "data": {
                    "fills": [
                        {"outcome_side": "yes", "is_taker": False, "yes_price_dollars": "0.48"}
                    ]
                },
            }
        ]
        no_entries = [
            {
                "step": "after_maker",
                "data": {"fills": {"fills": [{"outcome_side": "no", "is_taker": False}]}},
            }
        ]
        out = derive_conventions(yes_entries, no_entries)
        assert out["maker_side_on_yes_accept"] == "yes"
        assert out["maker_side_on_no_accept"] == "no"
        assert out["maker_is_taker_on_fill"] is False
        assert out["evidence"]["accept_yes_maker_fill"]["yes_price_dollars"] == "0.48"

    def test_missing_fills_yield_none_never_guesses(self) -> None:
        out = derive_conventions([], [])
        assert out["maker_side_on_yes_accept"] is None
        assert out["maker_side_on_no_accept"] is None
        assert out["maker_is_taker_on_fill"] is None
        assert out["combo_no_pays_complement"] is None
