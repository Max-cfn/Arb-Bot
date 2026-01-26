"""Tests for VWAP calculation."""

import pytest

from src.detector.vwap import available_liquidity_usd, calculate_vwap


class TestCalculateVwap:
    def test_single_level_exact_fill(self):
        asks = [(0.50, 200)]  # 200 shares @ $0.50 = $100 available
        vwap = calculate_vwap(asks, 100)
        assert vwap == pytest.approx(0.50)

    def test_two_levels(self):
        # 100 shares @ 0.50 = $50, then need $50 more from 0.52
        # $50 / 0.52 ≈ 96.15 shares @ 0.52
        # total cost = $100, total shares = 100 + 96.15 = 196.15
        # VWAP = 100 / 196.15 ≈ 0.5098
        asks = [(0.50, 100), (0.52, 200)]
        vwap = calculate_vwap(asks, 100)
        assert vwap is not None
        assert 0.50 < vwap < 0.52

    def test_three_levels_partial_fill(self):
        asks = [(0.50, 50), (0.52, 100), (0.55, 200)]
        # $25 from first level, $52 from second, $23 from third
        vwap = calculate_vwap(asks, 100)
        assert vwap is not None
        assert 0.50 < vwap < 0.55

    def test_insufficient_liquidity_returns_none(self):
        asks = [(0.50, 10)]  # Only $5 available
        vwap = calculate_vwap(asks, 100)
        assert vwap is None

    def test_empty_orderbook_returns_none(self):
        assert calculate_vwap([], 100) is None

    def test_zero_size_returns_none(self):
        asks = [(0.50, 100)]
        assert calculate_vwap(asks, 0) is None

    def test_negative_size_returns_none(self):
        asks = [(0.50, 100)]
        assert calculate_vwap(asks, -10) is None

    def test_exact_liquidity_match(self):
        asks = [(0.60, 100)]  # $60 available
        vwap = calculate_vwap(asks, 60)
        assert vwap == pytest.approx(0.60)

    def test_vwap_with_depth(self):
        """VWAP must account for depth, not just best ask."""
        asks = [(0.50, 50), (0.52, 100), (0.55, 200)]
        # $100 fills: $25 @ 0.50, $52 @ 0.52, $23 @ 0.55 → VWAP ≈ 0.5213
        vwap = calculate_vwap(asks, 100)
        assert vwap is not None
        assert 0.50 < vwap < 0.55
        # Must be higher than best ask since we eat into depth
        assert vwap > 0.50


class TestAvailableLiquidity:
    def test_basic(self):
        asks = [(0.50, 100), (0.60, 200)]
        liq = available_liquidity_usd(asks)
        assert liq == pytest.approx(0.50 * 100 + 0.60 * 200)

    def test_empty(self):
        assert available_liquidity_usd([]) == 0.0
