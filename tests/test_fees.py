"""Tests for Polymarket fee calculations."""

import pytest

from src.detector.fees import calculate_polymarket_fees


class TestStandardMarketFees:
    def test_no_trading_fee(self):
        result = calculate_polymarket_fees(0.50, 0.50, 100, is_crypto_15min=False)
        assert result["trading_fee"] == 0.0

    def test_resolution_fee_is_2_percent(self):
        result = calculate_polymarket_fees(0.50, 0.50, 100)
        # Resolution fee = 2% of payout ($100 payout for 100 shares)
        assert result["resolution_fee"] == pytest.approx(2.0)

    def test_profitable_arb(self):
        # YES=0.45 NO=0.48 => cost=0.93*100=$93, payout=$100
        result = calculate_polymarket_fees(0.45, 0.48, 100)
        assert result["gross_profit"] == pytest.approx(7.0)
        # net_profit = (100 - 2) - 93 = 5.0
        assert result["net_profit"] == pytest.approx(5.0)

    def test_unprofitable_after_fees(self):
        # YES=0.50 NO=0.49 => cost=0.99*100=$99, payout=$100
        result = calculate_polymarket_fees(0.50, 0.49, 100)
        # gross = 1.0, but resolution = 2.0 => net = -1.0
        assert result["net_profit"] < 0

    def test_breakeven_scenario(self):
        # YES + NO = 0.98, so gross profit = $2, resolution fee = $2 => net = 0
        result = calculate_polymarket_fees(0.49, 0.49, 100)
        assert result["net_profit"] == pytest.approx(0.0)


class TestCrypto15MinFees:
    def test_trading_fee_applied(self):
        result = calculate_polymarket_fees(0.50, 0.50, 100, is_crypto_15min=True)
        # fee per side = 0.02 * min(0.5, 0.5) * 100 = 1.0
        assert result["trading_fee"] == pytest.approx(2.0)

    def test_asymmetric_prices(self):
        # YES=0.70, NO=0.25
        # yes_fee = 0.02 * min(0.70, 0.30) * 100 = 0.6
        # no_fee  = 0.02 * min(0.25, 0.75) * 100 = 0.5
        result = calculate_polymarket_fees(0.70, 0.25, 100, is_crypto_15min=True)
        assert result["trading_fee"] == pytest.approx(1.1)

    def test_total_fee_includes_both(self):
        result = calculate_polymarket_fees(0.50, 0.50, 100, is_crypto_15min=True)
        expected_total = result["trading_fee"] + result["resolution_fee"]
        assert result["total_fee"] == pytest.approx(expected_total)
