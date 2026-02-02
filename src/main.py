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

# One-shot execution (safety): automatically disable trading after first execution attempt
ONE_SHOT_TRADE = os.getenv("ONE_SHOT_TRADE", "0").strip() in {"1", "true", "True", "yes", "YES"}

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
    """Send a rolling 24h stats ping every 6 hours, including delta vs last ping."""
    from pathlib import Path

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
        except Exception:
            pass

    while True:
        await asyncio.sleep(6 * 3600)
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
                "Window": "Rolling 24h (6h ping)",
                "Last 24h total": cur_total,
                "Last 24h actionable": stats_24h.get("actionable", 0),
                "Avg edge %": stats_24h.get("avg_edge", 0),
                "Max edge %": stats_24h.get("max_edge", 0),
            }
            if delta_pct is None:
                msg["Δ total vs prev"] = "n/a (first ping)"
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
            # Always emit opportunity alert + DB log, but NEVER block the trading path on Discord/IO
            asyncio.create_task(discord.send_opportunity(opp), name=f"alert-opp-{opp.market_id}")
            asyncio.create_task(db.log_opportunity(opp), name=f"db-opp-{opp.market_id}")

            # --- AUTOMATION: ATTEMPT EXECUTION IMMEDIATELY ---
            # If killswitch is OFF, we keep scanning/alerting but do not emit execution/trading actions.
            if not is_trading_enabled():
                continue

            try:
                if not hasattr(on_orderbook_update, "_exec_last"):  # type: ignore[attr-defined]
                    on_orderbook_update._exec_last = {}  # type: ignore[attr-defined]
                last = on_orderbook_update._exec_last.get(opp.market_id, 0)  # type: ignore[attr-defined]
                now_ts = time.time()
                if now_ts - last > 60:
                    on_orderbook_update._exec_last[opp.market_id] = now_ts  # type: ignore[attr-defined]
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

                    # One-shot safety: as soon as we decide to attempt an execution, flip killswitch OFF.
                    if ONE_SHOT_TRADE:
                        set_trading_enabled(False)
                        asyncio.create_task(
                            discord.send_ops(f"ONE_SHOT_TRADE: disabled trading after first execution attempt (market {opp.market_id})."),
                            name="ops-oneshot",
                        )

                    if EXECUTION_MODE == "real":
                        metrics = ExecutionMetrics(t_detect_ns=t_detect_ns, t_submit_ns=t_submit_ns)
                        # NOTE: do NOT block on Discord here; real execution handles its own acks/fills.
                        res = await executor.execute_two_leg(opp, run_id=run_id, metrics=metrics)
                        # Minimal reporting (async)
                        asyncio.create_task(
                            discord.send_execution(
                                opp,
                                note=(
                                    f"REAL_EXEC status={res.status}\n"
                                    + (f"reason={res.reason}\n" if res.reason else "")
                                ),
                                run_id=run_id,
                                status=res.status if res.status in {"SUBMITTED","WAITING","FILLED","CANCELLED","FAILED"} else "FAILED",
                            ),
                            name=f"exec-real-report-{opp.market_id}",
                        )
                        return

                    # Simulated state machine (dry-run for now): SUBMITTED -> WAITING -> CANCELLED
                    run_id = f"{opp.market_id}-{int(now_ts)}"
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
