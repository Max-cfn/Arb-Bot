"""Main entry point — orchestrates all bot components."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from src.alerts.discord import DiscordClient
from src.config import load_config
from src.detector.binary_arb import BinaryArbDetector
from src.scanner.market_fetcher import extract_all_token_ids, fetch_active_markets
from src.scanner.orderbook_manager import OrderbookManager
from src.scanner.websocket_client import PolymarketWebSocket
from src.storage.db import Database
from src.execution.clob_executor import PolymarketClobExecutor
from src.execution.types import ExecutionMetrics
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

# Trading control (killswitch)
CONTROL_FILE = Path(os.getenv("POLY_CONTROL_FILE", "data/control.json"))

# One-shot execution (safety): automatically disable trading after N execution attempts
# - If MAX_ONE_SHOT_TRADES>0, trading will be disabled after that many attempts.
# - Back-compat: ONE_SHOT_TRADE=1 implies MAX_ONE_SHOT_TRADES=1 unless explicitly set.
ONE_SHOT_TRADE = os.getenv("ONE_SHOT_TRADE", "0").strip() in {"1", "true", "True", "yes", "YES"}
try:
    MAX_ONE_SHOT_TRADES = int(os.getenv("MAX_ONE_SHOT_TRADES", "1" if ONE_SHOT_TRADE else "0").strip())
except Exception:
    MAX_ONE_SHOT_TRADES = 1 if ONE_SHOT_TRADE else 0

# Execution mode: dry-run (default) or real
EXECUTION_MODE = os.getenv("EXECUTION_MODE", "dryrun").strip().lower()



def is_trading_enabled() -> bool:
    """Return whether trading/execution is enabled.

    This is intended to be toggled via Discord admin commands (or manually).

    control.json example:
      {"trading_enabled": true}
    """
    env_override = os.getenv("TRADING_ENABLED")
    if env_override is not None:
        return env_override.strip() not in {"0", "false", "False", "no", "NO"}

    try:
        data = json.loads(CONTROL_FILE.read_text())
        # allow a couple of keys for compatibility
        if "trading_enabled" in data:
            return bool(data["trading_enabled"])
        if "killswitch" in data:
            return str(data["killswitch"]).strip() not in {"1", "on", "true", "True"}
    except FileNotFoundError:
        return True
    except Exception:
        return True

    return True


def set_trading_enabled(enabled: bool) -> None:
    """Persist trading enabled flag to CONTROL_FILE.

    This is used for one-shot mode and emergency shutdown.
    """
    try:
        CONTROL_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONTROL_FILE.write_text(json.dumps({"trading_enabled": bool(enabled)}, indent=2) + "\n")
    except Exception as exc:
        logger.error("Failed to write control file %s: %s", CONTROL_FILE, exc)



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


def select_markets(
    markets: list[dict],
    max_markets: int,
    min_liquidity_usd: float,
    min_volume_usd: float,
) -> list[dict]:
    """Select a market universe.

    Aggressive OR filter: keep markets with liquidity>=min_liquidity_usd OR volume>=min_volume_usd.
    Then sort by volume descending (priority to activity) and take top max_markets.
    """
    filtered = [
        m for m in markets
        if (float(m.get("liquidity", 0) or 0) >= min_liquidity_usd)
        or (float(m.get("volume", 0) or 0) >= min_volume_usd)
    ]
    # Priority to 24h volume (activity) over static liquidity
    filtered.sort(
        key=lambda m: float(m.get("volume", 0) or 0),
        reverse=True,
    )
    return filtered[:max_markets]


async def periodic_market_refresh(
    ob_manager: OrderbookManager,
    ws_client: PolymarketWebSocket,
    max_markets: int,
    min_liquidity_usd: float,
    min_volume_usd: float,
) -> None:
    """Refresh the active market list periodically."""
    while True:
        await asyncio.sleep(MARKET_REFRESH_INTERVAL)
        try:
            # Fetch a larger pool, then filter/sort down to our watchlist.
            pool = await fetch_active_markets(50000)
            new_markets = select_markets(pool, max_markets, min_liquidity_usd, min_volume_usd)
            if new_markets:
                ob_manager.load_markets(new_markets)
                new_ids = extract_all_token_ids(new_markets)
                await ws_client.update_subscriptions(new_ids)
                logger.info("Refreshed markets: %d active (filtered from %d)", len(new_markets), len(pool))
        except Exception as exc:
            logger.error("Market refresh failed: %s", exc)
            try:
                await discord.send_ops(f"Market refresh failed: {exc}")
            except Exception:
                pass


async def daily_summary_task(
    discord: DiscordClient,
    db: Database,
    start_time: float,
) -> None:
    """Send a daily summary at midnight Europe/Paris.

    Includes:
    - rolling last-24h counts (to track trend)
    - since-local-midnight counts (the day summary)
    """
    tz = ZoneInfo(os.getenv("DAILY_SUMMARY_TZ", "Europe/Paris"))
    while True:
        now_local = datetime.now(tz)
        next_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        if now_local >= next_midnight:
            from datetime import timedelta
            next_midnight += timedelta(days=1)
        await asyncio.sleep((next_midnight - now_local).total_seconds())

        try:
            stats_24h = await db.get_stats_last_24h()

            midnight_local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
            midnight_utc = midnight_local.astimezone(timezone.utc)
            stats_today = await db.get_stats_since(midnight_utc.isoformat())

            uptime_s = int(time.time() - start_time)
            hours, _ = divmod(uptime_s, 3600)

            summary = {
                "Window": f"{tz.key} day summary",
                "Today total": stats_today.get("total", 0),
                "Today actionable": stats_today.get("actionable", 0),
                "Today avg edge %": stats_today.get("avg_edge", 0),
                "Today max edge %": stats_today.get("max_edge", 0),
                "Last 24h total": stats_24h.get("total", 0),
                "Last 24h actionable": stats_24h.get("actionable", 0),
                "Uptime (hours)": hours,
            }

            await discord.send_daily_summary(summary)
            logger.info("Daily summary sent: %s", summary)
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


async def periodic_6h_summary(
    discord: DiscordClient,
    db: Database,
) -> None:
    """Send a rolling 24h stats ping on fixed 6h boundaries, incl. delta vs last ping.

    Why:
    - The old implementation used `sleep(6h)` which drifts with restarts and runtime delays.
    - We want absolute times (e.g. 00:00/06:00/12:00/18:00 in the chosen TZ).

    State:
    - We persist the previous ping totals in `data/rolling_stats.json` so the next ping can
      reference the previous one. If that file isn't writable (permissions), deltas will
      always show "first ping".
    """
    from pathlib import Path
    from datetime import timedelta

    tz = ZoneInfo(os.getenv("ROLLING_STATS_TZ", os.getenv("DAILY_SUMMARY_TZ", "Europe/Paris")))
    state_path = Path(os.getenv("ROLLING_STATS_STATE", "data/rolling_stats.json"))

    def _load_state() -> dict:
        try:
            import json
            return json.loads(state_path.read_text())
        except Exception:
            return {"last_24h_total": None, "last_24h_actionable": None, "updated_at": None}

    def _save_state(st: dict) -> None:
        try:
            import json
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(st, indent=2) + "\n")
        except Exception as exc:
            logger.warning("Failed to persist rolling stats state (%s): %s", state_path, exc)

    def _next_boundary(now_local: datetime) -> datetime:
        # Next boundary at hour in {0,6,12,18}
        h = now_local.hour
        next_h = ((h // 6) + 1) * 6
        if next_h >= 24:
            base = now_local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        else:
            base = now_local.replace(hour=next_h, minute=0, second=0, microsecond=0)
        return base

    while True:
        now_local = datetime.now(tz)
        nxt = _next_boundary(now_local)
        await asyncio.sleep(max(0.0, (nxt - now_local).total_seconds()))

        try:
            stats_24h = await db.get_stats_last_24h()
            st = _load_state()

            last_total = st.get("last_24h_total")
            cur_total = stats_24h.get("total", 0)

            delta_pct = None
            if isinstance(last_total, (int, float)) and last_total and cur_total is not None:
                try:
                    delta_pct = ((cur_total - last_total) / float(last_total)) * 100.0
                except Exception:
                    delta_pct = None

            msg = {
                "Window": f"Rolling 24h (6h ping) | {tz.key} @ {datetime.now(tz).strftime('%H:%M')}",
                "Last 24h total": cur_total,
                "Last 24h actionable": stats_24h.get("actionable", 0),
                "Avg edge %": stats_24h.get("avg_edge", 0),
                "Max edge %": stats_24h.get("max_edge", 0),
            }
            if delta_pct is None:
                msg["Δ total vs prev"] = "n/a (first ping or state not writable)"
            else:
                msg["Δ total vs prev"] = f"{delta_pct:+.1f}%"

            await discord.send_daily_summary(msg)

            st["last_24h_total"] = cur_total
            st["last_24h_actionable"] = stats_24h.get("actionable", 0)
            st["updated_at"] = datetime.now(timezone.utc).isoformat()
            _save_state(st)
        except Exception as exc:
            logger.error("6h rolling summary failed: %s", exc)


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
        # Per docs: initial sub uses type=MARKET + assets_ids
        {"type": "MARKET", "assets_ids": probe_assets},
        {"type": "MARKET", "assets_ids": probe_assets, "custom_feature_enabled": True},
        # Some WS implementations also accept operation-based subscribe
        {"type": "MARKET", "operation": "subscribe", "assets_ids": probe_assets},
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


async def periodic_clob_http_probe(discord: DiscordClient) -> None:
    """Periodically probe the CLOB HTTP endpoint to detect network issues.

    Enabled by env: CLOB_HTTP_PROBE=1

    Behavior:
    - logs latency + status to OPS only on failures (to avoid spam)
    - helps diagnose PolyApiException(status_code=None, Request exception!)
    """
    if os.getenv("CLOB_HTTP_PROBE", "0").strip() not in {"1", "true", "True", "yes", "YES"}:
        return

    import aiohttp

    url = os.getenv("CLOB_BASE_URL", "https://clob.polymarket.com").rstrip("/") + "/"
    fail_streak = 0

    while True:
        await asyncio.sleep(60)
        t0 = time.time()
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    dt = (time.time() - t0) * 1000
                    if resp.status >= 400:
                        fail_streak += 1
                        await discord.send_ops(f"CLOB_HTTP_PROBE fail status={resp.status} latency_ms={dt:.0f} streak={fail_streak}")
                    else:
                        fail_streak = 0
        except Exception as exc:
            dt = (time.time() - t0) * 1000
            fail_streak += 1
            await discord.send_ops(f"CLOB_HTTP_PROBE exception latency_ms={dt:.0f} streak={fail_streak} err={exc}")


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
            now = time.time()
            uptime_s = int(now - start_time)
            h, rem = divmod(uptime_s, 3600)
            m, s = divmod(rem, 60)

            newest_age = int(now - stats.get("newest_update", 0.0)) if stats.get("newest_update") else None
            oldest_age = int(now - stats.get("oldest_update", 0.0)) if stats.get("oldest_update") else None

            # Keep OPS readable: summary only (no recent asset list / raw edges).
            lines = []
            lines.append(f"WS/OB debug | uptime={h:02d}h{m:02d}m{s:02d}s")
            lines.append(
                f"tracked_assets={stats.get('tracked_assets')} total_markets={stats.get('total_markets')} stale_books={stats.get('stale_books')}"
            )
            if newest_age is not None:
                lines.append(f"newest_book_age={newest_age}s")
            if oldest_age is not None:
                lines.append(f"oldest_book_age={oldest_age}s")

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

    # One-time debug: send a fake opportunity message to validate the Discord webhook.
    # Controlled by env var so we don't spam.
    if ("" + str(os.getenv("SEND_FAKE_OPP_ONCE", ""))).strip() == "1":
        try:
            from src.detector.base import ArbitrageOpportunity
            fake = ArbitrageOpportunity(
                market_id="DEBUG",
                market_question="DEBUG: webhook test opportunity (ignore)",
                yes_token_id="DEBUG_YES",
                no_token_id="DEBUG_NO",
                yes_ask_vwap=0.49,
                no_ask_vwap=0.49,
                combined_cost=0.98,
                gross_edge=0.02,
                gross_edge_percent=2.04,
                net_edge=0.02,
                net_edge_percent=2.04,
                size_usd=100.0,
                yes_liquidity=1000.0,
                no_liquidity=1000.0,
                max_safe_size=500.0,
                timestamp=datetime.now(timezone.utc),
                is_crypto_15min=False,
                verdict="ACTIONABLE",
            )
            await discord.send_opportunity(fake)
            await discord.send_ops("DEBUG: sent one fake opportunity (SEND_FAKE_OPP_ONCE=1)")
        except Exception as exc:
            logger.error("Failed to send fake opportunity: %s", exc)

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
    # Fetch a large pool then select a watchlist (more stable than taking the first N).
    pool = await fetch_active_markets(50000)
    markets = select_markets(
        pool,
        config.max_markets_watch,
        config.market_min_liquidity_usd,
        config.market_min_volume_usd,
    )
    if not markets:
        logger.error("No markets found after filtering. Exiting.")
        await discord.send_ops("CRITICAL: No markets found after filtering, bot cannot start")
        sys.exit(1)

    logger.info(
        "Loaded %d markets (filtered from %d)",
        len(markets),
        len(pool),
    )

    # --- Setup components ---
    ob_manager = OrderbookManager(markets)
    detector = BinaryArbDetector(config)
    target_size_usd = config.target_size_usd

    # --- Execution (REAL) ---
    # Always instantiate the executor so EXECUTION_MODE can be flipped without code changes.
    # The executor itself will refuse to trade if creds are missing/placeholders.
    executor = PolymarketClobExecutor()

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

                # Sample updates were useful during initial debugging, but are too noisy for ops.
                # Keeping this block disabled by default.
                # msg = (
                #     "WS sample update\n"
                #     f"asset_id={asset_id}\n"
                #     f"market_id={market.get('id') if market else ''}\n"
                #     f"question={market.get('question') if market else ''}\n"
                #     f"best_bid={best_bid}\n"
                #     f"best_ask={best_ask}"
                #     f"{extra}"
                # )
                # await discord.send_ops(msg)
                pass
            except Exception as exc:
                logger.error("Failed to send WS sample to OPS: %s", exc)

        for market in affected:
            opp = detector.detect(market, ob_manager, target_size_usd=target_size_usd)
            if not opp:
                continue

            # Latency baseline: stamp monotonic time at detection (ns)
            opp._t_detect_ns = time.monotonic_ns()  # type: ignore[attr-defined]

            # USER REQUEST: tiered edge thresholds by time-to-resolution
            # - < 15 min:  net > 2.0%
            # - 15 min–4h: net > 3.5%
            # - 4h–24h:    net > 5.0%
            try:
                end_raw = (opp.end_date or "").strip()
                if not end_raw:
                    continue
                end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                if hours_left <= 0:
                    continue
                if hours_left > 24:
                    continue

                if hours_left < 0.25:
                    min_net = 2.0
                elif hours_left < 4.0:
                    min_net = 3.5
                else:
                    min_net = 5.0

                if not (opp.net_edge_percent > min_net):
                    continue
            except Exception:
                continue

            # --- ALERT DEDUP (avoid spamming identical edges) ---
            # Only re-alert a market if the net edge changes by >= 0.001 percentage points.
            try:
                if not hasattr(on_orderbook_update, "_alert_last"):  # type: ignore[attr-defined]
                    on_orderbook_update._alert_last = {}  # type: ignore[attr-defined]

                last_info = on_orderbook_update._alert_last.get(opp.market_id)  # type: ignore[attr-defined]
                now_ts = time.time()
                if last_info is not None:
                    last_edge, _last_ts = last_info
                    if abs(float(opp.net_edge_percent) - float(last_edge)) < 0.001:
                        continue

                on_orderbook_update._alert_last[opp.market_id] = (float(opp.net_edge_percent), now_ts)  # type: ignore[attr-defined]
            except Exception:
                # On any dedup bookkeeping issue, fall back to alerting.
                pass

            # Enrich opportunity with fee diagnostics (fast: cached; only called on actual opps)
            try:
                from src.scanner.fee_rate import get_fee_rate_bps

                bps_yes = await get_fee_rate_bps(opp.yes_token_id)
                bps_no = await get_fee_rate_bps(opp.no_token_id)
                opp.fee_rate_bps_yes = int(bps_yes)
                opp.fee_rate_bps_no = int(bps_no)

                # Effective taker fee rate is price-dependent; docs show curve peaks ~1.56% at p=0.50.
                # We scale by (bps/1000) so fee_rate_bps=1000 matches the published table.
                def _fee_rate_percent(p: float, bps: int) -> float:
                    if bps <= 0:
                        return 0.0
                    p = max(0.0, min(1.0, float(p)))
                    curve = 0.25 * (p * (1.0 - p)) ** 2
                    return 100.0 * curve * (float(bps) / 1000.0)

                opp.taker_fee_rate_percent_yes = _fee_rate_percent(opp.yes_ask_vwap, opp.fee_rate_bps_yes)
                opp.taker_fee_rate_percent_no = _fee_rate_percent(opp.no_ask_vwap, opp.fee_rate_bps_no)
            except Exception:
                pass
            # --- Edge lifetime tracking (from >=1% down to <1%) ---
            edge_floor = float(os.getenv("OPPORTUNITY_EDGE_FLOOR_PERCENT", "1.0"))
            now_ts = time.time()

            if not hasattr(on_orderbook_update, "_edge_lifetime"):  # type: ignore[attr-defined]
                on_orderbook_update._edge_lifetime = {}  # type: ignore[attr-defined]

            state = on_orderbook_update._edge_lifetime.get(opp.market_id)  # type: ignore[attr-defined]
            edge_now = float(getattr(opp, "net_edge_percent", 0.0) or 0.0)

            if edge_now >= edge_floor and opp.verdict in {"ACTIONABLE", "MARGINAL"}:
                if not state or not state.get("active"):
                    on_orderbook_update._edge_lifetime[opp.market_id] = {  # type: ignore[attr-defined]
                        "active": True,
                        "start_ts": now_ts,
                        "last_ts": now_ts,
                        "last_edge": edge_now,
                    }
                else:
                    state["last_ts"] = now_ts
                    state["last_edge"] = edge_now
            else:
                # If we previously had an edge >= floor, and now it dropped below, emit expiry message.
                if state and state.get("active"):
                    duration_s = float(now_ts - float(state.get("start_ts", now_ts)))
                    last_edge = float(state.get("last_edge", edge_now))
                    asyncio.create_task(
                        discord.send_opportunity_expired(
                            opp,
                            duration_s=duration_s,
                            last_edge_percent=last_edge,
                        ),
                        name=f"alert-expire-{opp.market_id}",
                    )
                    state["active"] = False

            # Always emit DB log, but do NOT spam Discord with SKIP.
            asyncio.create_task(db.log_opportunity(opp), name=f"db-opp-{opp.market_id}")
            if opp.verdict != "SKIP":
                asyncio.create_task(discord.send_opportunity(opp), name=f"alert-opp-{opp.market_id}")

            # --- AUTOMATION: ATTEMPT EXECUTION IMMEDIATELY ---
            # If killswitch is OFF, we keep scanning/alerting but do not emit execution/trading actions.
            if not is_trading_enabled():
                continue

            try:
                if not hasattr(on_orderbook_update, "_exec_last"):  # type: ignore[attr-defined]
                    on_orderbook_update._exec_last = {}  # type: ignore[attr-defined]
                last = on_orderbook_update._exec_last.get(opp.market_id, 0)  # type: ignore[attr-defined]
                now_ts = time.time()
                dedup_s = float(os.getenv("EXEC_DEDUP_SECONDS", "0"))
                if now_ts - last > dedup_s:
                    on_orderbook_update._exec_last[opp.market_id] = now_ts  # type: ignore[attr-defined]

                    # Always define a run_id for both real and dry-run paths
                    run_id = f"{opp.market_id}-{int(now_ts)}"

                    t_detect_ns = getattr(opp, "_t_detect_ns", None)
                    t_send_ns = time.monotonic_ns()
                    t_submit_ns = t_send_ns  # alias for clarity
                    detect_to_send_ms = None
                    if isinstance(t_detect_ns, int):
                        detect_to_send_ms = (t_send_ns - t_detect_ns) / 1e6

                    logger.info(
                        "EXEC_DECISION market=%s edge=%.2f%% detect_to_send_ms=%s",
                        opp.market_id,
                        opp.net_edge_percent,
                        f"{detect_to_send_ms:.3f}" if detect_to_send_ms is not None else "n/a",
                    )

                    # One-shot safety: as soon as we decide to attempt an execution, flip trading OFF
                    # after MAX_ONE_SHOT_TRADES attempts.
                    if MAX_ONE_SHOT_TRADES > 0:
                        if not hasattr(on_orderbook_update, "_oneshot_count"):  # type: ignore[attr-defined]
                            on_orderbook_update._oneshot_count = 0  # type: ignore[attr-defined]
                        on_orderbook_update._oneshot_count += 1  # type: ignore[attr-defined]
                        n = int(on_orderbook_update._oneshot_count)  # type: ignore[attr-defined]

                        if n >= MAX_ONE_SHOT_TRADES:
                            set_trading_enabled(False)
                            asyncio.create_task(
                                discord.send_ops(
                                    f"ONE_SHOT_TRADE: disabled trading after execution attempt {n}/{MAX_ONE_SHOT_TRADES} (market {opp.market_id})."
                                ),
                                name="ops-oneshot",
                            )
                        else:
                            asyncio.create_task(
                                discord.send_ops(
                                    f"ONE_SHOT_TRADE: attempt {n}/{MAX_ONE_SHOT_TRADES} (market {opp.market_id}); trading remains enabled."
                                ),
                                name="ops-oneshot-progress",
                            )

                    if EXECUTION_MODE == "real":
                        metrics = ExecutionMetrics(t_detect_ns=t_detect_ns, t_submit_ns=t_submit_ns)

                        # Pre-compute an explicit estimate for: shares + attempted cost (USD) so #executions is actionable.
                        import math

                        min_order_usd = float(os.getenv("CLOB_MIN_ORDER_USD", "1.0"))
                        min_order_shares = float(os.getenv("CLOB_MIN_ORDER_SHARES", "5"))
                        min_shares = float(getattr(executor, "min_shares", 1.0))
                        cross_bps = float(getattr(executor, "cross_bps", 0.0))

                        yes_best = float(getattr(opp, "yes_best_ask", 0.0) or 0.0)
                        no_best = float(getattr(opp, "no_best_ask", 0.0) or 0.0)
                        cheaper = min(yes_best, no_best) if yes_best > 0 and no_best > 0 else max(yes_best, no_best)
                        shares_for_min_notional = int(math.ceil(min_order_usd / cheaper)) if cheaper and cheaper > 0 else 1
                        shares = int(max(math.ceil(min_shares), math.ceil(min_order_shares), shares_for_min_notional))

                        def _aggressive_buy_limit(p: float) -> float:
                            if p <= 0:
                                return p
                            bumped = p * (1.0 + (cross_bps / 10_000.0))
                            return float(min(0.9999, max(0.0001, bumped)))

                        yes_limit_est = _aggressive_buy_limit(yes_best)
                        no_limit_est = _aggressive_buy_limit(no_best)
                        attempted_cost_usd = float(shares * (yes_limit_est + no_limit_est))

                        # Send an immediate "attempt" message so #executions reflects real order attempts.
                        asyncio.create_task(
                            discord.send_execution(
                                opp,
                                note=(
                                    "REAL_EXEC attempt: placing YES+NO buy orders now\n"
                                    f"balance_usd=? (next msg) | attempted_cost_usd≈{attempted_cost_usd:.4f}\n"
                                    f"shares={shares} (YES+NO) | yes_limit≈{yes_limit_est:.4f} no_limit≈{no_limit_est:.4f}\n"
                                    f"inputs(best): yes={yes_best:.4f} no={no_best:.4f} | min_order_usd={min_order_usd} min_order_shares={min_order_shares} cross_bps={cross_bps}"
                                ),
                                run_id=run_id,
                                status="SUBMITTED",
                            ),
                            name=f"exec-real-attempt-{opp.market_id}",
                        )

                        async def _real_execute_and_report() -> None:
                            # Best-effort balance/allowance snapshot (for #executions visibility)
                            bal_summary = None
                            bal_usd = None
                            try:
                                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

                                client = executor._get_client()  # uses API creds; does NOT place orders
                                params = BalanceAllowanceParams(
                                    asset_type=AssetType.COLLATERAL,
                                    signature_type=int(getattr(executor, "signature_type", 0)),
                                )
                                try:
                                    await asyncio.to_thread(lambda: client.update_balance_allowance(params))
                                except Exception:
                                    pass
                                bal = await asyncio.to_thread(lambda: client.get_balance_allowance(params))

                                # CLOB returns collateral balance in micro-units (1e6 = $1)
                                if isinstance(bal, dict) and "balance" in bal:
                                    bal_usd = int(str(bal.get("balance") or "0")) / 1_000_000.0
                                    bal_summary = f"balance_usd={bal_usd:.6f} allowances_n={len(bal.get('allowances') or {}) if isinstance(bal.get('allowances'), dict) else 'n/a'}"
                                else:
                                    bal_summary = str(bal)[:500]
                            except Exception:
                                bal_summary = None

                            call_start_ns = time.monotonic_ns()
                            res = await executor.execute_two_leg(opp, run_id=run_id, metrics=metrics)
                            call_end_ns = time.monotonic_ns()
                            call_ms = (call_end_ns - call_start_ns) / 1e6

                            submit_to_ack_ms = None
                            try:
                                if res.metrics and isinstance(res.metrics.t_ack_ns, int) and isinstance(res.metrics.t_submit_ns, int):
                                    submit_to_ack_ms = (res.metrics.t_ack_ns - res.metrics.t_submit_ns) / 1e6
                            except Exception:
                                submit_to_ack_ms = None

                            await discord.send_execution(
                                opp,
                                note=(
                                    f"REAL_EXEC result status={res.status}\n"
                                    + (f"reason={getattr(res, 'reason_code', None) or 'n/a'}\n")
                                    + (f"detail={res.reason}\n" if getattr(res, 'reason', '') else "")
                                    + (
                                        f"balance_usd={bal_usd:.6f} | attempted_cost_usd≈{attempted_cost_usd:.4f}\n"
                                        if bal_usd is not None
                                        else f"attempted_cost_usd≈{attempted_cost_usd:.4f}\n"
                                    )
                                    + (
                                        f"shares={shares} | yes_limit≈{yes_limit_est:.4f} no_limit≈{no_limit_est:.4f}\n"
                                    )
                                    + (
                                        f"yes_filled_size={getattr(res, 'yes_filled_size', None)} | "
                                        f"no_filled_size={getattr(res, 'no_filled_size', None)}\n"
                                    )
                                    + (
                                        f"timings: detect→submit={f'{detect_to_send_ms:.1f}ms' if detect_to_send_ms is not None else 'n/a'} | "
                                        f"submit→ack={f'{submit_to_ack_ms:.1f}ms' if submit_to_ack_ms is not None else 'n/a'} | "
                                        f"exec_call={call_ms:.1f}ms\n"
                                    )
                                    + (f"balance/allowance={bal_summary}\n" if bal_summary else "")
                                ),
                                run_id=run_id,
                                status=res.status if res.status in {"SUBMITTED","WAITING","FILLED","CANCELLED","FAILED"} else "FAILED",
                            )

                        asyncio.create_task(_real_execute_and_report(), name=f"exec-real-{opp.market_id}")
                        continue

                    # Simulated state machine (dry-run for now): SUBMITTED -> WAITING -> CANCELLED
                    note0 = "Strategy: strict limit, send both ASAP; cancel fast; if single-fill => unwind immediately (dry-run)."
                    if detect_to_send_ms is not None:
                        note0 += f"\nLatency: detect→submit {detect_to_send_ms:.3f}ms"

                    asyncio.create_task(
                        discord.send_execution(opp, note=note0, run_id=run_id, status="SUBMITTED"),
                        name=f"exec-sub-{opp.market_id}",
                    )

                    async def _simulate_states() -> None:
                        # WAITING
                        await asyncio.sleep(1.5)
                        t_wait_ns = time.monotonic_ns()
                        submit_to_wait_ms = (t_wait_ns - t_submit_ns) / 1e6
                        asyncio.create_task(
                            discord.send_execution(
                                opp,
                                note=f"No fill within timeout => would cancel both orders.\nLatency: submit→waiting {submit_to_wait_ms:.3f}ms",
                                run_id=run_id,
                                status="WAITING",
                            ),
                            name=f"exec-wait-{opp.market_id}",
                        )
                        # CANCELLED
                        await asyncio.sleep(0.2)
                        t_cancel_ns = time.monotonic_ns()
                        submit_to_cancel_ms = (t_cancel_ns - t_submit_ns) / 1e6
                        asyncio.create_task(
                            discord.send_execution(
                                opp,
                                note=(
                                    "CANCELLED (simulated). If only one leg had filled, "
                                    "we would immediately unwind that leg (market/aggro limit)."
                                    f"\nLatency: submit→cancel {submit_to_cancel_ms:.3f}ms"
                                ),
                                run_id=run_id,
                                status="CANCELLED",
                            ),
                            name=f"exec-cancel-{opp.market_id}",
                        )

                    asyncio.create_task(_simulate_states(), name=f"dryrun-{opp.market_id}")
            except Exception as exc:
                logger.error("Execution attempt failed: %s", exc)

    async def on_ws_error(exc: Exception) -> None:
        await discord.send_ops(f"WebSocket error: {exc}")

    async def on_ws_debug(msg: str) -> None:
        # Keep debug visible in the OPS channel
        await discord.send_ops(msg)

    # --- WebSocket client ---
    asset_ids = extract_all_token_ids(markets)
    # De-duplicate asset IDs to avoid subscribing multiple times to the same asset.
    # Preserve order for stable chunking.
    asset_ids = list(dict.fromkeys(asset_ids))

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

    async def _send_running_health_once() -> None:
        # Give WS a moment to connect and start receiving books.
        await asyncio.sleep(20)
        try:
            stats = ob_manager.get_stats()
            await discord.send_health("Running", {
                "Markets": stats.get("total_markets"),
                "Assets tracked": stats.get("tracked_assets"),
                "Stale books": stats.get("stale_books"),
            })
        except Exception as exc:
            logger.error("One-shot health ping failed: %s", exc)

    # Fire-and-forget one-shot health ping (do NOT put it in the main task list,
    # otherwise it completes and triggers shutdown when we wait for FIRST_COMPLETED).
    asyncio.create_task(_send_running_health_once(), name="health_once")

    tasks = [
        asyncio.create_task(periodic_6h_summary(discord, db), name="summary_6h"),
        asyncio.create_task(ws_client.connect(), name="websocket"),
        asyncio.create_task(periodic_clob_http_probe(discord), name="clob_http_probe"),
        asyncio.create_task(
            periodic_ops_debug(discord, ob_manager, start_time),
            name="ops_debug",
        ),
        asyncio.create_task(
            periodic_market_refresh(
                ob_manager,
                ws_client,
                config.max_markets_watch,
                config.market_min_liquidity_usd,
                config.market_min_volume_usd,
            ),
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
            return_when=asyncio.FIRST_EXCEPTION,
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
