import pytest

from combomaker.rfq.models import Rfq, RfqParseError

COMBO_MSG = {
    "id": "rfq_123",
    "creator_id": "",
    "market_ticker": "KXMVE-COMBO-1",
    "event_ticker": "KXMVE-EV",
    "contracts_fp": "100.00",
    "created_ts": "2026-07-05T10:00:00Z",
    "mve_collection_ticker": "KXMVESPORTS",
    "mve_selected_legs": [
        {"event_ticker": "E1", "market_ticker": "M1", "side": "yes"},
        {"event_ticker": "E2", "market_ticker": "M2", "side": "no"},
    ],
}


class TestParsing:
    def test_combo_contracts_mode(self) -> None:
        rfq = Rfq.from_ws(COMBO_MSG)
        assert rfq.is_combo
        assert rfq.contracts == 10_000
        assert rfq.target_cost_cc is None
        assert rfq.leg_tickers == ("M1", "M2")
        assert rfq.legs[1].side == "no"
        assert rfq.all_leg_sides_known

    def test_target_cost_mode(self) -> None:
        msg = {**COMBO_MSG, "target_cost_dollars": "50.00"}
        del msg["contracts_fp"]
        rfq = Rfq.from_ws(msg)
        assert rfq.contracts is None
        assert rfq.target_cost_cc == 500_000

    def test_non_combo(self) -> None:
        msg = {
            "id": "rfq_9",
            "market_ticker": "FED-23DEC-T3.00",
            "created_ts": "2026-07-05T10:00:00Z",
            "contracts_fp": "10.00",
        }
        rfq = Rfq.from_ws(msg)
        assert not rfq.is_combo
        assert rfq.legs == ()

    def test_missing_required_field(self) -> None:
        msg = dict(COMBO_MSG)
        del msg["market_ticker"]
        with pytest.raises(RfqParseError):
            Rfq.from_ws(msg)

    def test_unknown_side_preserved_and_flagged(self) -> None:
        msg = {
            **COMBO_MSG,
            "mve_selected_legs": [{"market_ticker": "M1", "side": "long"}],
        }
        rfq = Rfq.from_ws(msg)
        assert not rfq.legs[0].side_known
        assert not rfq.all_leg_sides_known
        assert rfq.legs[0].side == "long"  # raw value preserved for the log

    def test_leg_without_ticker_rejected(self) -> None:
        msg = {**COMBO_MSG, "mve_selected_legs": [{"side": "yes"}]}
        with pytest.raises(RfqParseError):
            Rfq.from_ws(msg)

    def test_bad_settlement_value_is_ignored_not_fatal(self) -> None:
        msg = {
            **COMBO_MSG,
            "mve_selected_legs": [
                {"market_ticker": "M1", "side": "yes", "yes_settlement_value_dollars": "x"}
            ],
        }
        rfq = Rfq.from_ws(msg)
        assert rfq.legs[0].yes_settlement_value_cc is None

    def test_bad_contracts_fp_fatal(self) -> None:
        msg = {**COMBO_MSG, "contracts_fp": "10.005"}
        with pytest.raises(RfqParseError):
            Rfq.from_ws(msg)

    def test_zero_contracts_fp_means_target_cost_mode(self) -> None:
        # Live demo wire fact: target-cost RFQs carry contracts_fp "0.00"
        msg = {**COMBO_MSG, "contracts_fp": "0.00", "target_cost_dollars": "5.00"}
        rfq = Rfq.from_ws(msg)
        assert rfq.contracts is None
        assert rfq.target_cost_cc == 50_000

    def test_negative_contracts_fp_fatal(self) -> None:
        msg = {**COMBO_MSG, "contracts_fp": "-1.00"}
        with pytest.raises(RfqParseError):
            Rfq.from_ws(msg)
