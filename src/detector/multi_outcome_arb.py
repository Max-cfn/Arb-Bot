"""Multi-outcome arbitrage detector (placeholder for Phase 2).

Multi-outcome arbitrage applies to events with 3+ outcomes where the
sum of asks across all outcomes is less than 1.0 minus fees.

This module is a stub; the full implementation is planned for a later phase.
"""

from __future__ import annotations

from src.detector.base import ArbitrageOpportunity, BaseDetector
from src.scanner.orderbook_manager import OrderbookManager
from src.utils.logger import logger


class MultiOutcomeArbDetector(BaseDetector):
    """Detect arbitrage across events with more than 2 outcomes.

    Currently a stub — returns None for all markets.
    """

    def detect(
        self,
        market: dict,
        orderbook_manager: OrderbookManager,
    ) -> ArbitrageOpportunity | None:
        # Multi-outcome detection not yet implemented
        return None
