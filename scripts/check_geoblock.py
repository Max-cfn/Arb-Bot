#!/usr/bin/env python3
"""Quick check: is the current IP geoblocked by Polymarket?"""

import asyncio
import sys
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils.geoblock import check_geoblock


async def main() -> None:
    print("Checking Polymarket geoblock status...")
    result = await check_geoblock()

    ip = result.get("ip", "unknown")
    country = result.get("country", "unknown")
    blocked = result.get("blocked", True)

    print(f"  IP:       {ip}")
    print(f"  Country:  {country}")
    print(f"  Blocked:  {blocked}")
    print()

    if blocked:
        print("RESULT: Your IP is BLOCKED. Use a non-blocked VM/VPN.")
        sys.exit(1)
    else:
        print("RESULT: Your IP is NOT blocked. Good to go.")


if __name__ == "__main__":
    asyncio.run(main())
