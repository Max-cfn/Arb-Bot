"""Discord webhook client for sending alerts."""

from __future__ import annotations

import asyncio
import hashlib
import os
from typing import Any

import aiohttp

from src.alerts.formatters import (
    format_daily_summary_embed,
    format_health_embed,
    format_ops_embed,
    format_opportunity_embed,
    format_opportunity_expired_embed,
    format_execution_embed,
)
from src.config import Config
from src.detector.base import ArbitrageOpportunity
from src.utils.logger import logger

# Discord allows ~30 req/min per webhook.
# Tunable via env for live tuning without code changes:
# - DISCORD_RATE_LIMIT_DELAY (default 2.0)
# - DISCORD_PRIORITY_MIN_GAP (default 1.0)
# - DISCORD_DEDUP_WINDOW_SECONDS (default 12)
# - DISCORD_DEDUP_CACHE_SIZE (default 300)
RATE_LIMIT_DELAY = float(os.getenv("DISCORD_RATE_LIMIT_DELAY", "2.0"))
PRIORITY_MIN_GAP = float(os.getenv("DISCORD_PRIORITY_MIN_GAP", "1.0"))
DEDUP_WINDOW_S = float(os.getenv("DISCORD_DEDUP_WINDOW_SECONDS", "12"))
DEDUP_CACHE_SIZE = int(os.getenv("DISCORD_DEDUP_CACHE_SIZE", "300"))


class DiscordClient:
    """Sends alerts to Discord via webhooks."""

    def __init__(self, config: Config):
        # Support webhook sharding (comma-separated URLs) to spread bursts and reduce 429s.
        self._webhooks = {
            "health": self._parse_webhook_urls(config.discord_webhook_health),
            "ops": self._parse_webhook_urls(config.discord_webhook_ops),
            "daily": self._parse_webhook_urls(config.discord_webhook_daily),
            "opportunities": self._parse_webhook_urls(config.discord_webhook_opportunities),
            "executions": self._parse_webhook_urls(config.discord_webhook_executions),  # errors
            "executions_info": self._parse_webhook_urls(config.discord_webhook_executions_info),
        }
        self._last_send_by_url: dict[str, float] = {}
        # Per-url lock: serializes concurrent sends to a specific webhook URL.
        self._locks: dict[str, asyncio.Lock] = {}
        # Lightweight dedup for noisy repeated payloads.
        self._recent_hashes: dict[str, float] = {}

    def _parse_webhook_urls(self, raw: str) -> list[str]:
        return [u.strip() for u in (raw or "").split(",") if u.strip()]

    def _pick_webhook_url(self, webhook_key: str) -> str:
        urls = self._webhooks.get(webhook_key, [])
        if not urls:
            return ""
        # Least recently used URL first.
        return min(urls, key=lambda u: self._last_send_by_url.get(u, 0.0))

    def _lock(self, webhook_url: str) -> asyncio.Lock:
        if webhook_url not in self._locks:
            self._locks[webhook_url] = asyncio.Lock()
        return self._locks[webhook_url]

    def _dedup_key(self, webhook_key: str, payload: dict[str, Any]) -> str:
        try:
            import json
            body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        except Exception:
            body = str(payload)
        h = hashlib.sha1(body.encode("utf-8")).hexdigest()
        return f"{webhook_key}:{h}"

    def _should_skip_duplicate(self, webhook_key: str, payload: dict[str, Any], *, priority: bool) -> bool:
        # Never dedup financially critical status messages.
        if priority:
            return False
        now = asyncio.get_event_loop().time()
        k = self._dedup_key(webhook_key, payload)
        ts = self._recent_hashes.get(k)
        if ts is not None and (now - ts) < DEDUP_WINDOW_S:
            return True
        self._recent_hashes[k] = now
        # Trim cache.
        if len(self._recent_hashes) > DEDUP_CACHE_SIZE:
            oldest = sorted(self._recent_hashes.items(), key=lambda kv: kv[1])[: max(1, DEDUP_CACHE_SIZE // 10)]
            for ok, _ in oldest:
                self._recent_hashes.pop(ok, None)
        return False

    async def _send(
        self,
        webhook_key: str,
        payload: dict[str, Any],
        *,
        priority: bool = False,
        _retry: bool = False,
    ) -> bool:
        """Send a payload to the specified webhook.

        - Non-priority: waits for RATE_LIMIT_DELAY since last send on this webhook.
        - Priority: waits for PRIORITY_MIN_GAP (1 s) to avoid back-to-back 429s.
        - Per-webhook asyncio.Lock: only one request in-flight at a time per webhook.
        - 429 handling: respects retry_after from Discord, then retries with same
          priority flag (so no accidental 2 s re-pacing on a critical message).
        """
        if self._should_skip_duplicate(webhook_key, payload, priority=priority):
            logger.debug("Dedup skipped Discord payload on %s", webhook_key)
            return True

        url = self._pick_webhook_url(webhook_key)
        if not url:
            logger.debug("No webhook configured for %s, skipping", webhook_key)
            return False

        async with self._lock(url):
            try:
                now = asyncio.get_event_loop().time()
                last = self._last_send_by_url.get(url, 0.0)
                elapsed = now - last
                min_gap = PRIORITY_MIN_GAP if priority else RATE_LIMIT_DELAY
                if elapsed < min_gap:
                    await asyncio.sleep(min_gap - elapsed)

                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        self._last_send_by_url[url] = asyncio.get_event_loop().time()
                        if resp.status == 204:
                            return True
                        if resp.status == 429:
                            body = await resp.json(content_type=None)
                            retry_after = float(body.get("retry_after", 5))
                            logger.warning(
                                "Discord 429 on %s, retry after %.1fs (priority=%s)",
                                webhook_key, retry_after, priority,
                            )
                            await asyncio.sleep(retry_after)
                            # Release lock before recursing so the retry can re-acquire it.
                        else:
                            logger.warning(
                                "Discord webhook %s returned %d", webhook_key, resp.status
                            )
                            return False

            except Exception as exc:
                logger.error("Failed to send Discord %s: %s", webhook_key, exc)
                return False

        # Retry outside the lock (it will re-acquire on entry).
        if resp.status == 429:  # type: ignore[possibly-undefined]
            return await self._send(webhook_key, payload, priority=priority, _retry=True)
        return False

    async def send_opportunity(self, opp: ArbitrageOpportunity) -> bool:
        """Send an arbitrage opportunity alert."""
        payload = format_opportunity_embed(opp)
        logger.info(
            "Sending opportunity alert: %s (net edge %.2f%%)",
            opp.market_question[:60], opp.net_edge_percent,
        )
        return await self._send("opportunities", payload)

    async def send_opportunity_expired(
        self,
        opp: ArbitrageOpportunity,
        *,
        duration_s: float,
        last_edge_percent: float | None = None,
    ) -> bool:
        """Send a follow-up message when an opportunity seems to have expired."""
        payload = format_opportunity_expired_embed(opp, duration_s=duration_s, last_edge_percent=last_edge_percent)
        return await self._send("opportunities", payload)

    async def send_execution(
        self,
        opp: ArbitrageOpportunity,
        note: str = "",
        *,
        run_id: str = "",
        status: str = "PLANNED",
        extra_fields: list[dict] | None = None,
    ) -> bool:
        """Send execution status to the appropriate Discord channel.

        Priority (PRIORITY_MIN_GAP = 1 s) for all financially significant events:
        - SUBMITTED / PLACED / SENDING — order just sent
        - FILLED / SUCCESS / COMPLETED — trade confirmed, must never be lost
        - CANCELLED / FAILED / ERROR   — unwind events must surface fast

        Fallback chain: executions_info → executions → ops.
        """
        payload = format_execution_embed(opp, note=note, run_id=run_id, status=status, extra_fields=extra_fields)
        normalized = str(status).upper().strip()

        is_priority = normalized in {
            "SUBMITTED", "PLACED", "SENDING",
            "FILLED", "SUCCESS", "COMPLETED",
            "CANCELLED", "FAILED", "REJECTED", "ERROR",
            "PARTIAL_OPEN",  # critical: cancel not confirmed by CLOB
            "UNWIND_OK",     # position closed at a loss — surface immediately
        }

        # PARTIAL_OPEN is treated as an error — routes to the errors/executions channel.
        error_statuses = {"FAILED", "CANCELLED", "REJECTED", "ERROR", "PARTIAL_OPEN"}
        target = "executions" if normalized in error_statuses else "executions_info"

        ok = await self._send(target, payload, priority=is_priority)
        if not ok and target == "executions_info":
            ok = await self._send("executions", payload, priority=is_priority)
        if not ok:
            ok = await self._send("ops", payload, priority=is_priority)
        return ok

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
