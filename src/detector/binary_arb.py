"""Binary arbitrage detector: buy YES + NO when their combined ask < 1."""

from __future__ import annotations

from datetime import datetime, timezone

from src.config import Config
from src.detector.base import ArbitrageOpportunity, BaseDetector
from src.detector.fees import calculate_polymarket_fees
from src.detector.vwap import available_liquidity_usd, calculate_vwap
from src.scanner.orderbook_manager import OrderbookManager
from src.utils.logger import logger


class BinaryArbDetector(BaseDetector):
    """Detects binary arbitrage: sum of asks for YES + NO < 1 after fees."""

    def __init__(self, config: Config):
        self.min_edge_percent = config.min_edge_percent
        self.min_liquidity_usd = config.min_liquidity_usd
        self.buffer_liquid = config.buffer_liquid_percent
        self.buffer_illiquid = config.buffer_illiquid_percent
        self.illiquid_threshold = config.illiquid_threshold_usd

    def detect(
        self,
        market: dict,
        orderbook_manager: OrderbookManager,
        target_size_usd: float = 100.0,
    ) -> ArbitrageOpportunity | None:
        """Check a single market for binary arbitrage."""
        tokens = market.get("tokens", [])
        if len(tokens) != 2:
            return None

        yes_book, no_book = orderbook_manager.get_market_books(market)
        if yes_book is None or no_book is None:
            return None
        if not yes_book.asks or not no_book.asks:
            return None

        return detect_binary_arbitrage(
            market=market,
            yes_orderbook={"asks": yes_book.asks, "bids": yes_book.bids},
            no_orderbook={"asks": no_book.asks, "bids": no_book.bids},
            target_size_usd=target_size_usd,
            min_edge_percent=self.min_edge_percent,
            buffer_percent=self._get_buffer(yes_book.asks, no_book.asks),
            min_liquidity_usd=self.min_liquidity_usd,
        )

    def _get_buffer(
        self,
        yes_asks: list[tuple[float, float]],
        no_asks: list[tuple[float, float]],
    ) -> float:
        """Choose buffer based on liquidity depth."""
        yes_liq = available_liquidity_usd(yes_asks)
        no_liq = available_liquidity_usd(no_asks)
        min_liq = min(yes_liq, no_liq)
        if min_liq < self.illiquid_threshold:
            return self.buffer_illiquid
        return self.buffer_liquid


def detect_binary_arbitrage(
    market: dict,
    yes_orderbook: dict,
    no_orderbook: dict,
    target_size_usd: float = 100.0,
    min_edge_percent: float = 0.5,
    buffer_percent: float = 0.5,
    min_liquidity_usd: float = 100.0,
) -> ArbitrageOpportunity | None:
    """Detect a binary arbitrage opportunity.

    Args:
        market: Market metadata dict.
        yes_orderbook: {"asks": [(price, size), ...], "bids": [...]}.
        no_orderbook: {"asks": [(price, size), ...], "bids": [...]}.
        target_size_usd: Size to evaluate.
        min_edge_percent: Minimum net edge after buffer.
        buffer_percent: Safety buffer (%).
        min_liquidity_usd: Minimum liquidity per side.

    Returns:
        ArbitrageOpportunity or None.
    """
    yes_asks = yes_orderbook.get("asks", [])
    no_asks = no_orderbook.get("asks", [])

    # Check minimum liquidity
    yes_liq = available_liquidity_usd(yes_asks)
    no_liq = available_liquidity_usd(no_asks)
    if yes_liq < min_liquidity_usd or no_liq < min_liquidity_usd:
        return None

    # VWAP for target size
    yes_vwap = calculate_vwap(yes_asks, target_size_usd)
    no_vwap = calculate_vwap(no_asks, target_size_usd)
    if yes_vwap is None or no_vwap is None:
        return None

    combined_cost = yes_vwap + no_vwap

    # Quick check: no arb if sum >= 1
    if combined_cost >= 1.0:
        return None

    gross_edge = 1.0 - combined_cost
    gross_edge_percent = (gross_edge / combined_cost) * 100

    # Fee calculation
    is_crypto = bool(market.get("is_crypto_15min", False))
    fees = calculate_polymarket_fees(yes_vwap, no_vwap, target_size_usd, is_crypto)

    net_edge = fees["net_profit"] / target_size_usd if target_size_usd > 0 else 0
    net_edge_percent = (net_edge / combined_cost) * 100 if combined_cost > 0 else 0

    # Apply safety buffer
    adjusted_edge = net_edge_percent - buffer_percent
    if adjusted_edge < min_edge_percent:
        return None

    # Verdict
    if adjusted_edge >= 2.0:
        verdict = "ACTIONABLE"
    elif adjusted_edge >= 1.0:
        verdict = "MARGINAL"
    else:
        verdict = "SKIP"

    tokens = market.get("tokens", [{}, {}])
    max_safe_size = min(yes_liq, no_liq) * 0.5

    return ArbitrageOpportunity(
        market_id=market.get("id", ""),
        market_question=market.get("question", ""),
        yes_token_id=tokens[0].get("token_id", ""),
        no_token_id=tokens[1].get("token_id", ""),
        yes_ask_vwap=yes_vwap,
        no_ask_vwap=no_vwap,
        combined_cost=combined_cost,
        gross_edge=gross_edge,
        gross_edge_percent=gross_edge_percent,
        net_edge=net_edge,
        net_edge_percent=net_edge_percent,
        size_usd=target_size_usd,
        yes_liquidity=yes_liq,
        no_liquidity=no_liq,
        max_safe_size=max_safe_size,
        timestamp=datetime.now(timezone.utc),
        is_crypto_15min=is_crypto,
        verdict=verdict,
    )
