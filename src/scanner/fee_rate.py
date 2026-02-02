"""Fetch and cache Polymarket CLOB fee-rate per token.

Docs:
- https://docs.polymarket.com/developers/market-makers/maker-rebates-program
  GET https://clob.polymarket.com/fee-rate?token_id={token_id}
  -> {"fee_rate_bps": 0|1000|...}

We keep this lightweight to avoid slowing the hot path.
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict, Tuple

import aiohttp

from src.utils.logger import logger

FEE_RATE_URL = "https://clob.polymarket.com/fee-rate"

# token_id -> (fee_rate_bps, expires_at)
_cache: Dict[str, Tuple[int, float]] = {}
_lock = asyncio.Lock()

# Default TTL: fee-rate should be stable; keep a short TTL to be safe.
DEFAULT_TTL_S = 300.0


async def get_fee_rate_bps(token_id: str, *, ttl_s: float = DEFAULT_TTL_S) -> int:
    token_id = (token_id or "").strip()
    if not token_id:
        return 0

    now = time.time()
    hit = _cache.get(token_id)
    if hit and hit[1] > now:
        return hit[0]

    async with _lock:
        # Re-check inside lock
        hit = _cache.get(token_id)
        if hit and hit[1] > time.time():
            return hit[0]

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    FEE_RATE_URL,
                    params={"token_id": token_id},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        logger.warning("fee-rate fetch failed (%s): %s", resp.status, token_id)
                        fee = 0
                    else:
                        data = await resp.json()
                        fee = int(data.get("fee_rate_bps") or 0)

            _cache[token_id] = (fee, time.time() + ttl_s)
            return fee

        except Exception as exc:
            logger.warning("fee-rate fetch exception for %s: %s", token_id, exc)
            # Fail closed-ish: assume 0 fee rather than blocking detection.
            _cache[token_id] = (0, time.time() + min(ttl_s, 60.0))
            return 0
