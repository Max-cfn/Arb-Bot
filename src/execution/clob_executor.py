from __future__ import annotations

import os
import time
from typing import Optional

import aiohttp

from src.utils.logger import logger
from .types import ExecutionMetrics, ExecutionResult


class PolymarketClobExecutor:
    """CLOB executor scaffold.

    This module is intentionally strict:
    - If credentials are missing or left as placeholders, it refuses to place orders.
    - All timestamps use monotonic_ns for latency instrumentation.

    NOTE: You MUST replace the placeholder credentials in your environment before enabling real execution.
    """

    def __init__(self) -> None:
        self.base_url = os.getenv("CLOB_BASE_URL", "https://XXXXXX")  # TODO: replace
        self.api_key = os.getenv("CLOB_API_KEY", "XXXXXX")
        self.api_secret = os.getenv("CLOB_API_SECRET", "XXXXXX")
        self.api_passphrase = os.getenv("CLOB_API_PASSPHRASE", "XXXXXX")
        self.private_key = os.getenv("CLOB_PRIVATE_KEY", "XXXXXX")
        self.funder_address = os.getenv("CLOB_FUNDER_ADDRESS", "XXXXXX")

        self.order_timeout_s = float(os.getenv("CLOB_ORDER_TIMEOUT_S", "1.5"))
        self.poll_interval_s = float(os.getenv("CLOB_POLL_INTERVAL_S", "0.05"))

    def _is_placeholder(self, v: Optional[str]) -> bool:
        if not v:
            return True
        return ("XXXXXX" in v) or (v.strip() in {"", "changeme", "TODO"})

    def validate_ready(self) -> list[str]:
        missing: list[str] = []
        for k, v in {
            "CLOB_BASE_URL": self.base_url,
            "CLOB_API_KEY": self.api_key,
            "CLOB_API_SECRET": self.api_secret,
            "CLOB_API_PASSPHRASE": self.api_passphrase,
            "CLOB_PRIVATE_KEY": self.private_key,
            "CLOB_FUNDER_ADDRESS": self.funder_address,
        }.items():
            if self._is_placeholder(v):
                missing.append(k)
        return missing

    async def execute_two_leg(self, opp, run_id: str, metrics: ExecutionMetrics) -> ExecutionResult:
        """Place YES + NO orders and wait for fill/timeout.

        This is a scaffold: the concrete request signing + endpoints must be wired
        for your chosen auth method.
        """
        missing = self.validate_ready()
        if missing:
            return ExecutionResult(
                status="FAILED",
                run_id=run_id,
                reason=f"Missing/placeholder CLOB creds: {', '.join(missing)}",
                metrics=metrics,
            )

        metrics.t_submit_ns = time.monotonic_ns()

        # TODO: Implement signed order placement here.
        # Suggested milestones:
        # 1) POST /orders (YES) and POST /orders (NO)
        # 2) record metrics.t_ack_ns when both acks received
        # 3) poll fills for both order ids; record t_first_fill_ns/t_both_filled_ns
        # 4) if timeout: cancel both; record t_cancel_ns

        logger.warning("Real execution not yet wired: implement CLOB signing/endpoints in clob_executor.py")
        return ExecutionResult(
            status="FAILED",
            run_id=run_id,
            reason="CLOB executor not implemented yet (endpoints/signing).",
            metrics=metrics,
        )
