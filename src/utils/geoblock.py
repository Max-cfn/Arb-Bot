"""Geoblock check for Polymarket API access."""

from __future__ import annotations

import aiohttp

from src.utils.logger import logger

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"


async def check_geoblock() -> dict:
    """Check if the current IP is geoblocked by Polymarket.

    Returns:
        {"blocked": bool, "ip": str, "country": str}
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(GEOBLOCK_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    blocked = data.get("blocked", True)
                    ip = data.get("ip", "unknown")
                    country = data.get("country", "unknown")
                    logger.info(
                        "Geoblock check: blocked=%s ip=%s country=%s",
                        blocked, ip, country,
                    )
                    return {"blocked": blocked, "ip": ip, "country": country}

                logger.warning("Geoblock check returned status %d", resp.status)
                return {"blocked": True, "ip": "unknown", "country": "unknown"}

    except Exception as exc:
        logger.error("Geoblock check failed: %s", exc)
        return {"blocked": True, "ip": "unknown", "country": "error"}


async def get_public_ip() -> str:
    """Return the public IP from the geoblock endpoint."""
    result = await check_geoblock()
    return result.get("ip", "unknown")
