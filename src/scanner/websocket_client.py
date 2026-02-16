"""WebSocket client for Polymarket real-time orderbook updates."""

from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable

import websockets

from src.utils.logger import logger

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
MAX_ASSETS_PER_CONNECTION = 250
INITIAL_RECONNECT_DELAY = 2
MAX_RECONNECT_DELAY = 60
MAX_RECONNECT_ATTEMPTS = 5


class PolymarketWebSocket:
    """Manages one or more WebSocket connections to Polymarket CLOB.

    NOTE: We need to support refreshing subscriptions when the market universe changes.
    The old implementation passed a fixed `asset_ids` chunk into each connection task.
    After `update_subscriptions()`, tasks would reconnect with stale chunks.

    This implementation runs a small supervisor loop that can cancel/recreate
    connection tasks whenever subscriptions are refreshed.
    """

    def __init__(
        self,
        asset_ids: list[str],
        on_orderbook_update: Callable[[str, dict], Awaitable[None]],
        on_error: Callable[[Exception], Awaitable[None]] | None = None,
        on_debug: Callable[[str], Awaitable[None]] | None = None,
        debug_raw_messages: int = 3,
    ):
        self.asset_ids = asset_ids
        self.on_orderbook_update = on_orderbook_update
        self.on_error = on_error
        self.on_debug = on_debug
        self.debug_raw_messages = debug_raw_messages
        self._connections: list[websockets.WebSocketClientProtocol] = []
        self._running = False
        self._consecutive_failures = 0
        self._debug_raw_remaining = debug_raw_messages

        # refresh mechanism
        self._refresh_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    async def connect(self) -> None:
        """Start WebSocket connections and keep them alive.

        This function is long-running. It will re-chunk and resubscribe whenever
        `update_subscriptions()` is called.
        """
        self._running = True

        while self._running:
            chunks = [
                self.asset_ids[i : i + MAX_ASSETS_PER_CONNECTION]
                for i in range(0, len(self.asset_ids), MAX_ASSETS_PER_CONNECTION)
            ]

            if not chunks:
                logger.warning("No asset IDs to subscribe to")
                # Wait for refresh (or stop)
                await asyncio.sleep(2)
                continue

            logger.info(
                "Starting %d WebSocket connection(s) for %d assets",
                len(chunks), len(self.asset_ids),
            )

            # Spawn connection tasks with staggered start to avoid rate limits
            self._tasks = []
            for chunk in chunks:
                self._tasks.append(asyncio.create_task(self._run_connection(chunk)))
                await asyncio.sleep(1.5)  # Stagger connections

            # Wait until a refresh is requested or until any task fails/exits
            refresh_wait = asyncio.create_task(self._refresh_event.wait())
            done, pending = await asyncio.wait(
                [*self._tasks, refresh_wait],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # If we were asked to refresh, close sockets & restart loop
            if refresh_wait in done and self._refresh_event.is_set():
                self._refresh_event.clear()
                await self.close(stop=False)

            # Cancel any remaining tasks (we will recreate next loop iteration)
            for t in self._tasks:
                if not t.done():
                    t.cancel()
            for p in pending:
                if p is not refresh_wait and not p.done():
                    p.cancel()

            # Drain cancellations
            await asyncio.gather(*[t for t in self._tasks if t], return_exceptions=True)
            self._tasks = []

            # Small backoff to avoid tight loop on repeated failures
            if self._running:
                await asyncio.sleep(0.2)

    async def _run_connection(self, asset_ids: list[str]) -> None:
        """Run a single WebSocket connection with reconnection logic."""
        delay = INITIAL_RECONNECT_DELAY

        while self._running:
            ws: websockets.WebSocketClientProtocol | None = None
            try:
                async with websockets.connect(
                    WS_URL,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._connections.append(ws)
                    self._consecutive_failures = 0
                    delay = INITIAL_RECONNECT_DELAY
                    logger.info("WebSocket connected, subscribing to %d assets", len(asset_ids))

                    # Subscribe
                    # Per docs: initial subscription uses type=MARKET and assets_ids.
                    # (User channel needs auth; market channel is public.)
                    subscribe_msg = json.dumps({
                        "type": "MARKET",
                        "assets_ids": asset_ids,
                        # Optional custom features
                        "custom_feature_enabled": True,
                    })
                    await ws.send(subscribe_msg)

                    # Listen
                    async for raw in ws:
                        if self.on_debug and self._debug_raw_remaining > 0:
                            self._debug_raw_remaining -= 1
                            # Trim payload to avoid massive OPS spam
                            preview = raw if len(raw) <= 1200 else raw[:1200] + "…"

                            # Try to parse and summarize structure
                            summary_lines = []
                            try:
                                parsed = json.loads(raw)
                                if isinstance(parsed, list):
                                    summary_lines.append(f"kind=list len={len(parsed)}")
                                    first = parsed[0] if parsed else None
                                    if isinstance(first, dict):
                                        summary_lines.append(
                                            f"first.type={first.get('type')} keys={list(first.keys())[:20]}"
                                        )
                                    else:
                                        summary_lines.append(f"first.kind={type(first).__name__}")
                                elif isinstance(parsed, dict):
                                    summary_lines.append(f"kind=dict type={parsed.get('type')} keys={list(parsed.keys())[:30]}")
                                else:
                                    summary_lines.append(f"kind={type(parsed).__name__}")
                            except Exception as exc:
                                summary_lines.append(f"parse_error={exc}")

                            summary = " | ".join(summary_lines)
                            await self.on_debug(f"WS raw message (summary): {summary}\nWS raw message (preview):\n{preview}")

                        await self._handle_message(raw)

            except websockets.ConnectionClosed as exc:
                logger.warning("WebSocket closed: code=%s reason=%s", exc.code, exc.reason)
            except Exception as exc:
                self._consecutive_failures += 1
                logger.error("WebSocket error (#%d): %s", self._consecutive_failures, exc)
                if self.on_error:
                    await self.on_error(exc)

                if self._consecutive_failures >= MAX_RECONNECT_ATTEMPTS:
                    logger.critical(
                        "Max reconnection attempts (%d) reached", MAX_RECONNECT_ATTEMPTS
                    )
                    if self.on_error:
                        await self.on_error(
                            ConnectionError(
                                f"Max reconnect attempts ({MAX_RECONNECT_ATTEMPTS}) exceeded"
                            )
                        )
                    return
            finally:
                # Best-effort cleanup of the connection handle
                try:
                    if ws is not None and ws in self._connections:
                        self._connections.remove(ws)
                except Exception:
                    pass

            if self._running:
                logger.info("Reconnecting in %.1fs...", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_RECONNECT_DELAY)

    async def _handle_message(self, raw_message: str) -> None:
        """Parse and dispatch incoming messages."""
        try:
            msg = json.loads(raw_message)
        except json.JSONDecodeError:
            return

        # Polymarket sometimes batches messages (e.g. a JSON list of events)
        if isinstance(msg, list):
            for item in msg:
                if isinstance(item, dict):
                    await self._handle_message(json.dumps(item))
            return

        if not isinstance(msg, dict):
            return

        # Messages use event_type (docs) rather than type for events
        event_type = msg.get("event_type") or msg.get("type")

        # 1) Full book snapshots
        if event_type == "book":
            asset_id = msg.get("asset_id", "")
            orderbook = {
                "bids": [
                    (float(o["price"]), float(o["size"]))
                    for o in msg.get("bids", [])
                    if isinstance(o, dict) and "price" in o and "size" in o
                ],
                "asks": [
                    (float(o["price"]), float(o["size"]))
                    for o in msg.get("asks", [])
                    if isinstance(o, dict) and "price" in o and "size" in o
                ],
            }
            await self.on_orderbook_update(asset_id, orderbook)
            return

        # 2) Lightweight deltas / signals
        # We DO receive these (see ops-logs): price_change, best_bid_ask.
        # They are not full depth, but they can keep top-of-book fresher between snapshots.
        if event_type == "best_bid_ask":
            asset_id = msg.get("asset_id", "")
            try:
                best_bid = float(msg.get("best_bid") or 0.0)
                best_ask = float(msg.get("best_ask") or 0.0)
            except Exception:
                return

            # Represent as a 1-level book update; OrderbookManager truncates anyway.
            orderbook = {
                "bids": [(best_bid, 0.0)] if best_bid > 0 else [],
                "asks": [(best_ask, 0.0)] if best_ask > 0 else [],
            }
            await self.on_orderbook_update(asset_id, orderbook)
            return

        if event_type == "price_change":
            # price_changes contains best_bid/best_ask per asset_id
            pcs = msg.get("price_changes")
            if not isinstance(pcs, list):
                return
            for item in pcs:
                if not isinstance(item, dict):
                    continue
                asset_id = str(item.get("asset_id") or "")
                if not asset_id:
                    continue
                try:
                    best_bid = float(item.get("best_bid") or 0.0)
                    best_ask = float(item.get("best_ask") or 0.0)
                except Exception:
                    continue
                orderbook = {
                    "bids": [(best_bid, 0.0)] if best_bid > 0 else [],
                    "asks": [(best_ask, 0.0)] if best_ask > 0 else [],
                }
                await self.on_orderbook_update(asset_id, orderbook)
            return

    async def update_subscriptions(self, new_asset_ids: list[str]) -> None:
        """Update the asset list and trigger a re-chunk + resubscribe."""
        self.asset_ids = new_asset_ids
        logger.info(
            "Subscription list updated (%d assets). Triggering resubscribe...",
            len(new_asset_ids),
        )
        # Signal the supervisor loop to restart connections with fresh chunks.
        self._refresh_event.set()

    async def close(self, stop: bool = True) -> None:
        """Gracefully close all connections.

        Args:
            stop: if True, stop the reconnect loop; if False, only close current
                  sockets so the loop can reconnect with updated subscriptions.
        """
        if stop:
            self._running = False
        for ws in list(self._connections):
            try:
                await ws.close()
            except Exception:
                pass
        self._connections.clear()
        # Reset debug budget on reconnect so we can see first messages again
        self._debug_raw_remaining = self.debug_raw_messages
        logger.info("WebSocket connections closed")
