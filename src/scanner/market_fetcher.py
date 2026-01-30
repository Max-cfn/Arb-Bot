"""REST API client to fetch active markets from Polymarket Gamma API."""

from __future__ import annotations

import aiohttp

from src.utils.logger import logger

GAMMA_API = "https://gamma-api.polymarket.com"


async def fetch_active_markets(max_markets: int = 500) -> list[dict]:
    """Fetch active binary markets from the Gamma API.

    Returns a list of market dicts with normalised token info.
    """
    markets: list[dict] = []
    offset = 0
    limit = 100  # API page size

    try:
        async with aiohttp.ClientSession() as session:
            while len(markets) < max_markets:
                url = (
                    f"{GAMMA_API}/markets"
                    f"?limit={limit}&offset={offset}"
                    f"&active=true&closed=false"
                )
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        logger.warning("Gamma API returned %d at offset %d", resp.status, offset)
                        break

                    batch = await resp.json()
                    if not batch:
                        break

                    for m in batch:
                        parsed = _parse_market(m)
                        if parsed:
                            markets.append(parsed)

                    offset += limit

    except Exception as exc:
        logger.error("Failed to fetch markets: %s", exc)

    markets = markets[:max_markets]
    logger.info("Fetched %d active binary markets", len(markets))
    return markets


def _parse_market(raw: dict) -> dict | None:
    """Parse a raw market from Gamma API into our normalised format.

    Only returns binary markets (exactly 2 outcomes).
    """
    tokens = raw.get("tokens", [])
    if not tokens:
        # Older format: clobTokenIds + outcomes as strings
        clob_ids = raw.get("clobTokenIds", "")
        outcomes_str = raw.get("outcomes", "")
        outcome_prices = raw.get("outcomePrices", "")

        if isinstance(clob_ids, str):
            clob_ids = [t.strip().strip('"') for t in clob_ids.strip("[]").split(",") if t.strip()]
        if isinstance(outcomes_str, str):
            outcomes_str = [o.strip().strip('"') for o in outcomes_str.strip("[]").split(",") if o.strip()]
        if isinstance(outcome_prices, str):
            outcome_prices = [p.strip().strip('"') for p in outcome_prices.strip("[]").split(",") if p.strip()]

        if len(clob_ids) != 2 or len(outcomes_str) != 2:
            return None

        tokens = []
        for i in range(2):
            price = 0.0
            if i < len(outcome_prices):
                try:
                    price = float(outcome_prices[i])
                except (ValueError, TypeError):
                    pass
            tokens.append({
                "token_id": clob_ids[i],
                "outcome": outcomes_str[i],
                "price": price,
            })

    if len(tokens) != 2:
        return None  # Not binary

    return {
        "id": raw.get("id", raw.get("conditionId", "")),
        "condition_id": raw.get("conditionId", ""),
        "question": raw.get("question", ""),
        "slug": raw.get("slug", ""),
        "tokens": [
            {
                "token_id": str(t.get("token_id", t.get("tokenId", ""))),
                "outcome": t.get("outcome", ""),
                "price": float(t.get("price", 0)),
            }
            for t in tokens
        ],
        "volume": float(raw.get("volume", 0) or 0),
        "liquidity": float(raw.get("liquidity", 0) or 0),
        "end_date": raw.get("endDate", raw.get("end_date", "")),
        "is_crypto_15min": _is_crypto_15min(raw),
    }


def _is_crypto_15min(raw: dict) -> bool:
    """Heuristic: detect crypto 15-minute markets by slug/question patterns."""
    slug = (raw.get("slug") or "").lower()
    question = (raw.get("question") or "").lower()
    tags = [t.lower() for t in (raw.get("tags") or [])]

    crypto_keywords = {"btc", "eth", "bitcoin", "ethereum", "crypto", "sol", "solana"}
    time_keywords = {"15 min", "15min", "15-min", "15 minute"}

    text = f"{slug} {question} {' '.join(tags)}"
    has_crypto = any(kw in text for kw in crypto_keywords)
    has_time = any(kw in text for kw in time_keywords)

    return has_crypto and has_time


def extract_all_token_ids(markets: list[dict]) -> list[str]:
    """Extract all token IDs from a list of markets."""
    ids: list[str] = []
    for m in markets:
        for token in m.get("tokens", []):
            tid = token.get("token_id", "")
            if tid:
                ids.append(tid)
    return ids
