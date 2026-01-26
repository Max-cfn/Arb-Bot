"""Discord webhook client for sending alerts."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from src.alerts.formatters import (
    format_daily_summary_embed,
    format_health_embed,
    format_ops_embed,
    format_opportunity_embed,
)
from src.config import Config
from src.detector.base import ArbitrageOpportunity
from src.utils.logger import logger

# Rate-limit: Discord allows ~30 requests/min per webhook
RATE_LIMIT_DELAY = 2.0  # seconds between sends


class DiscordClient:
    """Sends alerts to Discord via webhooks."""

    def __init__(self, config: Config):
        self._webhooks = {
            "health": config.discord_webhook_health,
            "ops": config.discord_webhook_ops,
            "daily": config.discord_webhook_daily,
            "opportunities": config.discord_webhook_opportunities,
        }
        self._last_send: float = 0

    async def _send(self, webhook_key: str, payload: dict[str, Any]) -> bool:
        """Send a payload to the specified webhook."""
        url = self._webhooks.get(webhook_key, "")
        if not url:
            logger.debug("No webhook configured for %s, skipping", webhook_key)
            return False

        try:
            # Basic rate-limit guard
            now = asyncio.get_event_loop().time()
            elapsed = now - self._last_send
            if elapsed < RATE_LIMIT_DELAY:
                await asyncio.sleep(RATE_LIMIT_DELAY - elapsed)

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    self._last_send = asyncio.get_event_loop().time()
                    if resp.status == 204:
                        return True
                    if resp.status == 429:
                        retry_after = (await resp.json()).get("retry_after", 5)
                        logger.warning("Discord rate limited, retry after %.1fs", retry_after)
                        await asyncio.sleep(retry_after)
                        return await self._send(webhook_key, payload)
                    logger.warning(
                        "Discord webhook %s returned %d", webhook_key, resp.status
                    )
                    return False

        except Exception as exc:
            logger.error("Failed to send Discord %s: %s", webhook_key, exc)
            return False

    async def send_opportunity(self, opp: ArbitrageOpportunity) -> bool:
        """Send an arbitrage opportunity alert."""
        payload = format_opportunity_embed(opp)
        logger.info(
            "Sending opportunity alert: %s (net edge %.2f%%)",
            opp.market_question[:60], opp.net_edge_percent,
        )
        return await self._send("opportunities", payload)

    async def send_health(self, status: str, details: dict[str, Any] | None = None) -> bool:
        """Send a health-check message."""
        payload = format_health_embed(status, details or {})
        return await self._send("health", payload)

    async def send_ops(self, message: str) -> bool:
        """Send an ops/error alert."""
        payload = format_ops_embed(message)
        logger.info("Sending ops alert: %s", message[:100])
        return await self._send("ops", payload)

    async def send_daily_summary(self, stats: dict[str, Any]) -> bool:
        """Send the daily summary."""
        payload = format_daily_summary_embed(stats)
        return await self._send("daily", payload)
