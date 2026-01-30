"""In-memory orderbook state management."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from src.utils.logger import logger

MAX_LEVELS = 10  # Keep only top 10 price levels per side


@dataclass
class Orderbook:
    bids: list[tuple[float, float]] = field(default_factory=list)
    asks: list[tuple[float, float]] = field(default_factory=list)
    last_update: float = 0.0


class OrderbookManager:
    """Holds current orderbook state for all tracked assets.

    Note: we keep per-asset last_update timestamps so we can debug whether the
    WebSocket stream is actually delivering book updates.
    """

    def __init__(self, markets: list[dict] | None = None):
        # asset_id -> Orderbook
        self._books: dict[str, Orderbook] = {}
        # asset_id -> market dict
        self._asset_to_market: dict[str, dict] = {}
        # market_id -> market dict
        self._markets: dict[str, dict] = {}

        if markets:
            self.load_markets(markets)

    def load_markets(self, markets: list[dict]) -> None:
        """Index markets by their token IDs."""
        self._markets.clear()
        self._asset_to_market.clear()
        for m in markets:
            mid = m["id"]
            self._markets[mid] = m
            for token in m.get("tokens", []):
                tid = token.get("token_id", "")
                if tid:
                    self._asset_to_market[tid] = m

    def update(self, asset_id: str, book: dict) -> None:
        """Update the orderbook for a given asset."""
        bids = sorted(book.get("bids", []), key=lambda x: -x[0])[:MAX_LEVELS]
        asks = sorted(book.get("asks", []), key=lambda x: x[0])[:MAX_LEVELS]

        self._books[asset_id] = Orderbook(
            bids=bids,
            asks=asks,
            last_update=time.time(),
        )

    def get_book(self, asset_id: str) -> Orderbook | None:
        """Return the orderbook for an asset, or None."""
        return self._books.get(asset_id)

    def get_markets_by_asset(self, asset_id: str) -> list[dict]:
        """Return markets that include the given asset ID."""
        market = self._asset_to_market.get(asset_id)
        return [market] if market else []

    def get_market_books(self, market: dict) -> tuple[Orderbook | None, Orderbook | None]:
        """Return (yes_book, no_book) for a market."""
        tokens = market.get("tokens", [])
        if len(tokens) < 2:
            return None, None

        yes_id = tokens[0].get("token_id", "")
        no_id = tokens[1].get("token_id", "")
        return self._books.get(yes_id), self._books.get(no_id)

    @property
    def tracked_assets(self) -> int:
        return len(self._books)

    @property
    def total_markets(self) -> int:
        return len(self._markets)

    def get_stats(self) -> dict:
        """Return summary statistics."""
        now = time.time()
        stale_count = sum(1 for b in self._books.values() if now - b.last_update > 60)
        newest_update = max((b.last_update for b in self._books.values()), default=0.0)
        oldest_update = min((b.last_update for b in self._books.values()), default=0.0)
        return {
            "tracked_assets": self.tracked_assets,
            "total_markets": self.total_markets,
            "stale_books": stale_count,
            "newest_update": newest_update,
            "oldest_update": oldest_update,
        }

    def get_recent_assets(self, n: int = 10) -> list[tuple[str, float, float | None, float | None]]:
        """Return the N most recently updated assets.

        Each item is (asset_id, last_update_epoch, best_bid, best_ask).
        """
        items = sorted(self._books.items(), key=lambda kv: kv[1].last_update, reverse=True)[:n]
        out: list[tuple[str, float, float | None, float | None]] = []
        for asset_id, book in items:
            best_bid = book.bids[0][0] if book.bids else None
            best_ask = book.asks[0][0] if book.asks else None
            out.append((asset_id, book.last_update, best_bid, best_ask))
        return out
