"""Main entry point — orchestrates all bot components."""

from __future__ import annotations

import asyncio
import json
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

import websockets

# Intervals (seconds)
HEALTH_INTERVAL = 300        # 5 min
MARKET_REFRESH_INTERVAL = 900  # 15 min
DAILY_SUMMARY_HOUR = 8       # 08:00 UTC
DB_PURGE_INTERVAL = 3600     # 1 hour

# Debug: send periodic WS/OB stats to OPS to validate stream end-to-end
DEBUG_OPS_INTERVAL = 60      # 1 min
WS_PROBE_ON_START = True
WS_PROBE_TIMEOUT_S = 6.0
WS_PROBE_MAX_MSGS = 4


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


async def ws_probe_subscriptions(discord: DiscordClient, asset_ids: list[str]) -> None:
    """Try several subscribe payload variants and report what the WS returns.

    This is intentionally noisy but bounded (few messages, one-shot) and is meant
    to help us discover correct channel/field names.
    """
    if not asset_ids:
        return

    # Keep the probe small
    probe_assets = asset_ids[:10]

    variants: list[dict] = [
        {"type": "subscribe", "channel": "market", "assets_ids": probe_assets},
        {"type": "subscribe", "channel": "market", "asset_ids": probe_assets},
        {"type": "subscribe", "channel": "market", "assets_ids": probe_assets, "asset_ids": probe_assets},
        {"type": "subscribe", "channel": "book", "asset_ids": probe_assets},
        {"type": "subscribe", "channel": "orderbook", "asset_ids": probe_assets},
    ]

    async def _summarize(raw: str) -> str:
        try:
            parsed = json.loads(raw)
        except Exception as exc:
            return f"parse_error={exc} raw={raw[:200]}"

        if isinstance(parsed, list):
            first = parsed[0] if parsed else None
            if isinstance(first, dict):
                return f"kind=list len={len(parsed)} first.type={first.get('type')} first.keys={list(first.keys())[:15]}"
            return f"kind=list len={len(parsed)} first.kind={type(first).__name__}"

        if isinstance(parsed, dict):
            return f"kind=dict type={parsed.get('type')} keys={list(parsed.keys())[:20]}"

        return f"kind={type(parsed).__name__}"

    # Run sequentially to avoid interleaving outputs.
    for i, payload in enumerate(variants, 1):
        lines = [f"WS probe {i}/{len(variants)} payload={payload}"]
        try:
            async with websockets.connect(
                "wss://ws-subscriptions-clob.polymarket.com/ws/market",
                ping_interval=30,
                ping_timeout=10,
                close_timeout=5,
            ) as ws:
                await ws.send(json.dumps(payload))

                got = 0
                start = time.time()
                while got < WS_PROBE_MAX_MSGS and (time.time() - start) < WS_PROBE_TIMEOUT_S:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    except asyncio.TimeoutError:
                        lines.append("- timeout")
                        continue
                    got += 1
                    lines.append(f"- msg{got}: {await _summarize(raw)}")

        except Exception as exc:
            lines.append(f"- exception: {exc}")

        await discord.send_ops("\n".join(lines))
        await asyncio.sleep(1)


async def periodic_ops_debug(
    discord: DiscordClient,
    ob_manager: OrderbookManager,
    start_time: float,
) -> None:
    """Send periodic debug stats to OPS (low frequency).

    Useful to validate whether WebSocket book updates are actually flowing.
    """
    # Small delay to let the bot connect first
    await asyncio.sleep(10)

    while True:
        await asyncio.sleep(DEBUG_OPS_INTERVAL)
        try:
            stats = ob_manager.get_stats()
            recent = ob_manager.get_recent_assets(10)

            now = time.time()
            uptime_s = int(now - start_time)
            h, rem = divmod(uptime_s, 3600)
            m, s = divmod(rem, 60)

            newest_age = int(now - stats.get("newest_update", 0.0)) if stats.get("newest_update") else None

            lines = []
            lines.append(f"WS/OB debug | uptime={h:02d}h{m:02d}m{s:02d}s")
            lines.append(
                f"tracked_assets={stats.get('tracked_assets')} total_markets={stats.get('total_markets')} stale_books={stats.get('stale_books')}"
            )
            if newest_age is not None:
                lines.append(f"newest_book_age={newest_age}s")
            if not recent:
                lines.append("recent_assets: (none yet)")
            else:
                lines.append("recent_assets (asset_id | age_s | best_bid | best_ask):")
                for asset_id, last_upd, best_bid, best_ask in recent:
                    age = int(now - last_upd)
                    lines.append(f"- {asset_id} | {age}s | {best_bid} | {best_ask}")

                # Also try to compute a few "raw" (best-ask-based) edges at market level
                # even if they don't pass thresholds.
                lines.append("raw_edges (market_id | combined_best_asks | gross_edge | question):")
                seen = 0
                for asset_id, _, _, _ in recent:
                    mkts = ob_manager.get_markets_by_asset(asset_id)
                    if not mkts:
                        continue
                    mkt = mkts[0]
                    yes_book, no_book = ob_manager.get_market_books(mkt)
                    yes_ask = yes_book.asks[0][0] if yes_book and yes_book.asks else None
                    no_ask = no_book.asks[0][0] if no_book and no_book.asks else None
                    if yes_ask is None or no_ask is None:
                        continue
                    combined = yes_ask + no_ask
                    gross_edge = 1.0 - combined
                    lines.append(
                        f"- {mkt.get('id')} | {combined:.4f} | {gross_edge:.4f} | {mkt.get('question','')[:90]}"
                    )
                    seen += 1
                    if seen >= 5:
                        break

            msg = "\n".join(lines)
            await discord.send_ops(msg)
        except Exception as exc:
            logger.error("OPS debug failed: %s", exc)


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
    last_ops_sample: float = 0.0

    async def on_orderbook_update(asset_id: str, book: dict) -> None:
        nonlocal last_ops_sample
        ob_manager.update(asset_id, book)
        affected = ob_manager.get_markets_by_asset(asset_id)

        # Low-rate debug sample to OPS so you can visually confirm streaming orderbooks.
        # Sends at most once per minute.
        now = time.time()
        if now - last_ops_sample > 60:
            last_ops_sample = now
            try:
                market = affected[0] if affected else None
                book_obj = ob_manager.get_book(asset_id)
                best_bid = book_obj.bids[0] if book_obj and book_obj.bids else None
                best_ask = book_obj.asks[0] if book_obj and book_obj.asks else None

                extra = ""
                if market and market.get("tokens") and len(market.get("tokens", [])) >= 2:
                    try:
                        yes_book, no_book = ob_manager.get_market_books(market)
                        yes_ask = yes_book.asks[0][0] if yes_book and yes_book.asks else None
                        no_ask = no_book.asks[0][0] if no_book and no_book.asks else None
                        if yes_ask is not None and no_ask is not None:
                            combined = yes_ask + no_ask
                            extra = f"\ncombined_best_asks={combined:.4f} gross_edge={(1.0-combined):.4f}"
                    except Exception:
                        pass

                msg = (
                    "WS sample update\n"
                    f"asset_id={asset_id}\n"
                    f"market_id={market.get('id') if market else ''}\n"
                    f"question={market.get('question') if market else ''}\n"
                    f"best_bid={best_bid}\n"
                    f"best_ask={best_ask}"
                    f"{extra}"
                )
                await discord.send_ops(msg)
            except Exception as exc:
                logger.error("Failed to send WS sample to OPS: %s", exc)

        for market in affected:
            opp = detector.detect(market, ob_manager)
            if opp:
                await discord.send_opportunity(opp)
                await db.log_opportunity(opp)

    async def on_ws_error(exc: Exception) -> None:
        await discord.send_ops(f"WebSocket error: {exc}")

    async def on_ws_debug(msg: str) -> None:
        # Keep debug visible in the OPS channel
        await discord.send_ops(msg)

    # --- WebSocket client ---
    asset_ids = extract_all_token_ids(markets)

    if WS_PROBE_ON_START:
        # Fire-and-forget probe (bounded). Helps us discover correct subscribe format.
        asyncio.create_task(ws_probe_subscriptions(discord, asset_ids), name="ws_probe")

    ws_client = PolymarketWebSocket(
        asset_ids=asset_ids,
        on_orderbook_update=on_orderbook_update,
        on_error=on_ws_error,
        on_debug=on_ws_debug,
        debug_raw_messages=3,
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
            periodic_ops_debug(discord, ob_manager, start_time),
            name="ops_debug",
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
