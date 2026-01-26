"""Tests for binary arbitrage detection."""

import pytest

from src.detector.binary_arb import detect_binary_arbitrage

MOCK_MARKET = {
    "id": "0xabc123",
    "question": "Will BTC reach $100k?",
    "condition_id": "0xdef456",
    "slug": "will-btc-reach-100k",
    "tokens": [
        {"token_id": "tok_yes", "outcome": "Yes", "price": 0.50},
        {"token_id": "tok_no", "outcome": "No", "price": 0.50},
    ],
    "volume": 100000,
    "liquidity": 20000,
    "is_crypto_15min": False,
}


class TestDetectBinaryArbitrage:
    def test_detects_obvious_arbitrage(self):
        """Large edge (sum=0.93) should produce ACTIONABLE."""
        yes_book = {"asks": [(0.45, 1000)], "bids": []}
        no_book = {"asks": [(0.48, 1000)], "bids": []}

        opp = detect_binary_arbitrage(
            market=MOCK_MARKET,
            yes_orderbook=yes_book,
            no_orderbook=no_book,
            target_size_usd=100,
            min_edge_percent=0.5,
            buffer_percent=0.5,
        )

        assert opp is not None
        assert opp.combined_cost == pytest.approx(0.93)
        assert opp.gross_edge_percent > 7.0
        assert opp.verdict == "ACTIONABLE"

    def test_no_arbitrage_when_sum_above_one(self):
        """No arb when YES + NO >= 1."""
        yes_book = {"asks": [(0.55, 1000)], "bids": []}
        no_book = {"asks": [(0.48, 1000)], "bids": []}

        opp = detect_binary_arbitrage(
            market=MOCK_MARKET,
            yes_orderbook=yes_book,
            no_orderbook=no_book,
            target_size_usd=100,
        )

        assert opp is None

    def test_no_arb_when_edge_below_threshold(self):
        """Small edge below min_edge + buffer should return None."""
        # sum = 0.98, gross edge = 2%, resolution fee eats it
        yes_book = {"asks": [(0.49, 1000)], "bids": []}
        no_book = {"asks": [(0.49, 1000)], "bids": []}

        opp = detect_binary_arbitrage(
            market=MOCK_MARKET,
            yes_orderbook=yes_book,
            no_orderbook=no_book,
            target_size_usd=100,
            min_edge_percent=0.5,
            buffer_percent=0.5,
        )

        assert opp is None

    def test_insufficient_liquidity(self):
        """Should return None when one side has too little liquidity."""
        yes_book = {"asks": [(0.45, 5)], "bids": []}  # Only ~$2.25
        no_book = {"asks": [(0.48, 1000)], "bids": []}

        opp = detect_binary_arbitrage(
            market=MOCK_MARKET,
            yes_orderbook=yes_book,
            no_orderbook=no_book,
            target_size_usd=100,
            min_liquidity_usd=100,
        )

        assert opp is None

    def test_empty_orderbook(self):
        """Empty orderbook should return None."""
        yes_book = {"asks": [], "bids": []}
        no_book = {"asks": [(0.48, 1000)], "bids": []}

        opp = detect_binary_arbitrage(
            market=MOCK_MARKET,
            yes_orderbook=yes_book,
            no_orderbook=no_book,
            target_size_usd=100,
        )

        assert opp is None

    def test_marginal_verdict(self):
        """Edge between 1% and 2% (after buffer) should be MARGINAL."""
        # Target: adjusted_edge between 1.0 and 2.0
        # Need net_edge_percent - buffer ∈ [1.0, 2.0)
        # With buffer=0.5, need net_edge ∈ [1.5, 2.5)
        yes_book = {"asks": [(0.44, 1000)], "bids": []}
        no_book = {"asks": [(0.50, 1000)], "bids": []}
        # sum=0.94, gross edge ~6.38%, after 2% resolution on 100 = net ~4.26%
        # adjusted = 4.26 - 0.5 = 3.76 → ACTIONABLE (too high)

        # Try tighter: sum=0.955
        yes_book = {"asks": [(0.47, 1000)], "bids": []}
        no_book = {"asks": [(0.49, 1000)], "bids": []}
        # sum=0.96, gross=4.17%, net profit=(98-96)=2, net_edge_pct=2/0.96*100=2.08%
        # adjusted=2.08-0.5=1.58 → MARGINAL

        opp = detect_binary_arbitrage(
            market=MOCK_MARKET,
            yes_orderbook=yes_book,
            no_orderbook=no_book,
            target_size_usd=100,
            min_edge_percent=0.5,
            buffer_percent=0.5,
        )

        assert opp is not None
        assert opp.verdict == "MARGINAL"

    def test_opportunity_fields(self):
        """Verify all fields are populated."""
        yes_book = {"asks": [(0.40, 1000)], "bids": [(0.39, 500)]}
        no_book = {"asks": [(0.45, 1000)], "bids": [(0.44, 500)]}

        opp = detect_binary_arbitrage(
            market=MOCK_MARKET,
            yes_orderbook=yes_book,
            no_orderbook=no_book,
            target_size_usd=100,
        )

        assert opp is not None
        assert opp.market_id == "0xabc123"
        assert opp.market_question == "Will BTC reach $100k?"
        assert opp.yes_token_id == "tok_yes"
        assert opp.no_token_id == "tok_no"
        assert opp.size_usd == 100.0
        assert opp.timestamp is not None
        assert opp.yes_liquidity > 0
        assert opp.no_liquidity > 0
        assert opp.max_safe_size > 0
