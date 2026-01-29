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
    """Manages one or more WebSocket connections to Polymarket CLOB."""

    def __init__(
        self,
        asset_ids: list[str],
        on_orderbook_update: Callable[[str, dict], Awaitable[None]],
        on_error: Callable[[Exception], Awaitable[None]] | None = None,
    ):
        self.asset_ids = asset_ids
        self.on_orderbook_update = on_orderbook_update
        self.on_error = on_error
        self._connections: list[websockets.WebSocketClientProtocol] = []
        self._running = False
        self._consecutive_failures = 0

    async def connect(self) -> None:
        """Start WebSocket connections (splits assets across connections if needed)."""
        self._running = True
        chunks = [
            self.asset_ids[i : i + MAX_ASSETS_PER_CONNECTION]
            for i in range(0, len(self.asset_ids), MAX_ASSETS_PER_CONNECTION)
        ]

        if not chunks:
            logger.warning("No asset IDs to subscribe to")
            return

        logger.info(
            "Starting %d WebSocket connection(s) for %d assets",
            len(chunks), len(self.asset_ids),
        )

        tasks = [self._run_connection(chunk) for chunk in chunks]
        await asyncio.gather(*tasks)

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
                    subscribe_msg = json.dumps({
                        "type": "subscribe",
                        "channel": "market",
                        # Polymarket payloads have used both spellings in the wild.
                        "assets_ids": asset_ids,
                        "asset_ids": asset_ids,
                    })
                    await ws.send(subscribe_msg)

                    # Listen
                    async for raw in ws:
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

        msg_type = msg.get("type")

        if msg_type == "book":
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

    async def update_subscriptions(self, new_asset_ids: list[str]) -> None:
        """Update the asset list (requires reconnect)."""
        self.asset_ids = new_asset_ids
        logger.info("Subscription list updated (%d assets). Reconnecting...", len(new_asset_ids))
        await self.close()
        # The connect loop will restart in main

    async def close(self) -> None:
        """Gracefully close all connections."""
        self._running = False
        for ws in self._connections:
            try:
                await ws.close()
            except Exception:
                pass
        self._connections.clear()
        logger.info("WebSocket connections closed")
