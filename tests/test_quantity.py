import pytest

from combomaker.core.quantity import (
    CentiContracts,
    QuantityParseError,
    cost_micro_dollars,
    qty_from_contracts,
    qty_from_fp_str,
    qty_to_fp_str,
)


@pytest.mark.parametrize(
    ("wire", "centi"),
    [("13.00", 1_300), ("100.00", 10_000), ("0.01", 1), ("-54.00", -5_400), ("7", 700)],
)
def test_parse(wire: str, centi: int) -> None:
    assert qty_from_fp_str(wire) == centi


def test_sub_centi_rejected() -> None:
    with pytest.raises(QuantityParseError):
        qty_from_fp_str("0.005")


def test_garbage_rejected() -> None:
    with pytest.raises(QuantityParseError):
        qty_from_fp_str("lots")


def test_roundtrip_canonical_two_decimals() -> None:
    assert qty_to_fp_str(CentiContracts(1_300)) == "13.00"
    assert qty_to_fp_str(qty_from_contracts(7)) == "7.00"


def test_cost_unit_identity() -> None:
    # 1 contract (100 centi) at $0.56 (5600 cc) = $0.56 = 560_000 micro-dollars
    assert cost_micro_dollars(CentiContracts(100), 5_600) == 560_000
    # 2.50 contracts at $0.10 = $0.25
    assert cost_micro_dollars(CentiContracts(250), 1_000) == 250_000
