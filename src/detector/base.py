"""Abstract base class for arbitrage detectors."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class ArbitrageOpportunity:
    """Represents a detected arbitrage opportunity."""

    market_id: str
    market_question: str

    yes_token_id: str
    no_token_id: str

    # For linking to Polymarket UI
    condition_id: str
    slug: str
    end_date: str

    # Prices
    yes_best_ask: float
    no_best_ask: float
    combined_best_asks: float

    # One-share plan (buy 1 YES + 1 NO)
    one_share_net_profit: float
    one_share_net_edge_percent: float

    # VWAP-based (size_usd per side)
    yes_ask_vwap: float
    no_ask_vwap: float
    combined_cost: float

    # Profits
    gross_edge: float
    gross_edge_percent: float
    net_edge: float
    net_edge_percent: float

    # Liquidity
    size_usd: float
    yes_liquidity: float
    no_liquidity: float
    max_safe_size: float

    # Meta
    timestamp: datetime
    is_crypto_15min: bool

    # Verdict
    verdict: str  # "ACTIONABLE" | "MARGINAL" | "SKIP"


class BaseDetector(ABC):
    """Interface for arbitrage detectors."""

    @abstractmethod
    def detect(self, market: dict, orderbook_manager) -> ArbitrageOpportunity | None:
        """Analyse a market and return an opportunity if found."""
        ...
