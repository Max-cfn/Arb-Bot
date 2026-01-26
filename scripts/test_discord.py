#!/usr/bin/env python3
"""Test Discord webhooks by sending a sample message to each configured channel."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alerts.discord import DiscordClient
from src.config import load_config


async def main() -> None:
    config = load_config()
    client = DiscordClient(config)

    print("Testing Discord webhooks...")

    results = {
        "health": await client.send_health("Test", {"source": "test_discord.py"}),
        "ops": await client.send_ops("Test alert from test_discord.py"),
        "daily": await client.send_daily_summary({
            "total": 42,
            "avg_edge": 1.5,
            "max_edge": 4.2,
            "actionable": 5,
        }),
    }

    for name, ok in results.items():
        status = "OK" if ok else "FAILED (check webhook URL)"
        print(f"  {name:15s} -> {status}")

    all_ok = all(results.values())
    print()
    if all_ok:
        print("All webhooks working.")
    else:
        print("Some webhooks failed. Check your .env configuration.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
