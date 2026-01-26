#!/usr/bin/env python3
"""List active binary markets from Polymarket."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.scanner.market_fetcher import fetch_active_markets


async def main() -> None:
    max_markets = 50
    if len(sys.argv) > 1:
        max_markets = int(sys.argv[1])

    print(f"Fetching up to {max_markets} active binary markets...\n")
    markets = await fetch_active_markets(max_markets)

    if not markets:
        print("No markets found.")
        return

    for i, m in enumerate(markets, 1):
        tokens = m.get("tokens", [])
        yes_price = tokens[0].get("price", 0) if tokens else 0
        no_price = tokens[1].get("price", 0) if len(tokens) > 1 else 0
        liq = m.get("liquidity", 0)
        vol = m.get("volume", 0)
        crypto = " [CRYPTO-15m]" if m.get("is_crypto_15min") else ""

        print(f"{i:3d}. {m['question'][:80]}{crypto}")
        print(f"     YES={yes_price:.2f}  NO={no_price:.2f}  SUM={yes_price+no_price:.4f}  "
              f"Liq=${liq:,.0f}  Vol=${vol:,.0f}")
        print(f"     ID: {m['id'][:24]}...")
        print()

    print(f"Total: {len(markets)} markets")


if __name__ == "__main__":
    asyncio.run(main())
