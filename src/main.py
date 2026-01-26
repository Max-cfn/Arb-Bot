"""Main entry point — orchestrates all bot components."""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from datetime import datetime, timezone

from src.alerts.discord import DiscordClient
from src.config import load_config
from src.detector.binary_arb import BinaryArbDetector
from src.scanner.market_fetcher import extract_all_token_ids, fetch_active_markets
from src.scanner.orderbook_manager import OrderbookManager
from src.scanner.websocket_client import PolymarketWebSocket
from src.storage.db import Database
from src.utils.geoblock import check_geoblock, get_public_ip
from src.utils.logger import logger

# Intervals (seconds)
HEALTH_INTERVAL = 300        # 5 min
MARKET_REFRESH_INTERVAL = 900  # 15 min
DAILY_SUMMARY_HOUR = 8       # 08:00 UTC
DB_PURGE_INTERVAL = 3600     # 1 hour


async def periodic_health_check(
    discord: DiscordClient,
    ob_manager: OrderbookManager,
    start_time: float,
) -> None:
    """Send a health-check embed every HEALTH_INTERVAL seconds."""
    while True:
        await asyncio.sleep(HEALTH_INTERVAL)
        try:
            uptime_s = int(time.time() - start_time)
            hours, remainder = divmod(uptime_s, 3600)
            minutes, secs = divmod(remainder, 60)
            stats = ob_manager.get_stats()
            await discord.send_health("Running", {
                "Markets": stats["total_markets"],
                "Assets tracked": stats["tracked_assets"],
                "Stale books": stats["stale_books"],
                "Uptime": f"{hours}h{minutes}m{secs}s",
            })
        except Exception as exc:
            logger.error("Health check failed: %s", exc)


async def periodic_market_refresh(
    ob_manager: OrderbookManager,
    ws_client: PolymarketWebSocket,
    max_markets: int,
) -> None:
    """Refresh the active market list periodically."""
    while True:
        await asyncio.sleep(MARKET_REFRESH_INTERVAL)
        try:
            new_markets = await fetch_active_markets(max_markets)
            if new_markets:
                ob_manager.load_markets(new_markets)
                new_ids = extract_all_token_ids(new_markets)
                await ws_client.update_subscriptions(new_ids)
                logger.info("Refreshed markets: %d active", len(new_markets))
        except Exception as exc:
            logger.error("Market refresh failed: %s", exc)


async def daily_summary_task(
    discord: DiscordClient,
    db: Database,
    start_time: float,
) -> None:
    """Send a daily summary at DAILY_SUMMARY_HOUR UTC."""
    while True:
        now = datetime.now(timezone.utc)
        # Calculate seconds until next target hour
        target = now.replace(hour=DAILY_SUMMARY_HOUR, minute=0, second=0, microsecond=0)
        if now >= target:
            from datetime import timedelta
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        await asyncio.sleep(wait_seconds)

        try:
            stats = await db.get_stats_last_24h()
            uptime_s = int(time.time() - start_time)
            hours, _ = divmod(uptime_s, 3600)
            stats["Uptime (hours)"] = hours
            await discord.send_daily_summary(stats)
            logger.info("Daily summary sent: %s", stats)
        except Exception as exc:
            logger.error("Daily summary failed: %s", exc)


async def periodic_db_purge(db: Database) -> None:
    """Purge old records from the database."""
    while True:
        await asyncio.sleep(DB_PURGE_INTERVAL)
        try:
            await db.purge_old(hours=24)
        except Exception as exc:
            logger.error("DB purge failed: %s", exc)


async def main() -> None:
    """Main bot loop."""
    start_time = time.time()
    config = load_config()

    # Validate config
    errors = config.validate()
    if errors:
        for e in errors:
            logger.error("Config error: %s", e)
        sys.exit(1)

    discord = DiscordClient(config)

    # --- Geoblock check ---
    logger.info("Checking geoblock status...")
    geo = await check_geoblock()
    if geo["blocked"]:
        logger.critical("IP %s is GEOBLOCKED (country=%s). Exiting.", geo["ip"], geo["country"])
        await discord.send_ops(
            f"CRITICAL: IP {geo['ip']} geoblocked (country={geo['country']}), bot cannot start"
        )
        sys.exit(1)

    logger.info("Geoblock OK: ip=%s country=%s", geo["ip"], geo["country"])

    # --- Init database ---
    db = Database(config.db_path)
    await db.init()

    # --- Fetch markets ---
    logger.info("Fetching active markets...")
    markets = await fetch_active_markets(config.max_markets_watch)
    if not markets:
        logger.error("No markets found. Exiting.")
        await discord.send_ops("CRITICAL: No active markets found, bot cannot start")
        sys.exit(1)

    logger.info("Loaded %d markets", len(markets))

    # --- Setup components ---
    ob_manager = OrderbookManager(markets)
    detector = BinaryArbDetector(config)

    # Startup health message
    ip = geo["ip"]
    await discord.send_health("Starting", {
        "Markets": len(markets),
        "IP": ip,
        "Country": geo["country"],
        "Min edge": f"{config.min_edge_percent}%",
    })

    # --- Orderbook update callback ---
    async def on_orderbook_update(asset_id: str, book: dict) -> None:
        ob_manager.update(asset_id, book)
        affected = ob_manager.get_markets_by_asset(asset_id)
        for market in affected:
            opp = detector.detect(market, ob_manager)
            if opp:
                await discord.send_opportunity(opp)
                await db.log_opportunity(opp)

    async def on_ws_error(exc: Exception) -> None:
        await discord.send_ops(f"WebSocket error: {exc}")

    # --- WebSocket client ---
    asset_ids = extract_all_token_ids(markets)
    ws_client = PolymarketWebSocket(
        asset_ids=asset_ids,
        on_orderbook_update=on_orderbook_update,
        on_error=on_ws_error,
    )

    # --- Graceful shutdown ---
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    # --- Launch tasks ---
    logger.info("Starting bot (watching %d assets)...", len(asset_ids))

    tasks = [
        asyncio.create_task(ws_client.connect(), name="websocket"),
        asyncio.create_task(
            periodic_health_check(discord, ob_manager, start_time),
            name="health",
        ),
        asyncio.create_task(
            periodic_market_refresh(ob_manager, ws_client, config.max_markets_watch),
            name="market_refresh",
        ),
        asyncio.create_task(
            daily_summary_task(discord, db, start_time),
            name="daily_summary",
        ),
        asyncio.create_task(periodic_db_purge(db), name="db_purge"),
    ]

    # Wait for shutdown signal or task failure
    try:
        shutdown_task = asyncio.create_task(shutdown_event.wait())
        done, _ = await asyncio.wait(
            [*tasks, shutdown_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in done:
            if t != shutdown_task and t.exception():
                logger.error("Task %s failed: %s", t.get_name(), t.exception())
    finally:
        logger.info("Shutting down...")
        for t in tasks:
            t.cancel()
        await ws_client.close()
        await db.close()
        await discord.send_health("Stopped", {"reason": "shutdown"})
        logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
