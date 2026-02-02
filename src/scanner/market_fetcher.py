"""REST API client to fetch active markets from Polymarket Gamma API."""

from __future__ import annotations

import re
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
                        body = ""
                        try:
                            body = (await resp.text())[:400]
                        except Exception:
                            body = ""
                        raise RuntimeError(f"Gamma API HTTP {resp.status} offset={offset} body={body}")

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
    """Heuristic: detect crypto 15-minute markets.

    Signals:
    - crypto keyword in slug/question/tags
    - either explicit "15 min" text OR slug contains 15m/15min OR question encodes a 15-minute window
      like "11:15AM-11:30AM ET".

    This flag drives:
    - stricter freshness gating (2s)
    - fee schedule differences
    """
    slug = (raw.get("slug") or "").lower()
    question = (raw.get("question") or "").lower()
    tags = [str(t).lower() for t in (raw.get("tags") or [])]

    crypto_keywords = {"btc", "eth", "bitcoin", "ethereum", "crypto", "sol", "solana", "xrp"}
    text = f"{slug} {question} {' '.join(tags)}"
    has_crypto = any(kw in text for kw in crypto_keywords)
    if not has_crypto:
        return False

    # 1) direct time keywords
    if any(k in text for k in {"15 min", "15min", "15-min", "15 minute"}):
        return True

    # 2) slug patterns used by Polymarket for 15m up/down markets
    if ("15m" in slug or "15min" in slug or "-15m-" in slug) and ("updown" in slug or "up-or-down" in slug or "up_or_down" in slug):
        return True

    # 3) question time window like "11:15AM-11:30AM ET" => infer 15 minutes
    m = re.search(r"(\d{1,2}):(\d{2})\s*(am|pm)\s*[-–]\s*(\d{1,2}):(\d{2})\s*(am|pm)", question)
    if m:
        h1, mm1, ap1, h2, mm2, ap2 = m.groups()
        h1 = int(h1) % 12 + (12 if ap1 == "pm" else 0)
        h2 = int(h2) % 12 + (12 if ap2 == "pm" else 0)
        t1 = h1 * 60 + int(mm1)
        t2 = h2 * 60 + int(mm2)
        if t2 < t1:
            t2 += 24 * 60
        if (t2 - t1) == 15:
            return True

    return False



def extract_all_token_ids(markets: list[dict]) -> list[str]:
    """Extract all token IDs from a list of markets."""
    ids: list[str] = []
    for m in markets:
        for token in m.get("tokens", []):
            tid = token.get("token_id", "")
            if tid:
                ids.append(tid)
    return ids
