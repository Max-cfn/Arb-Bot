"""SQLite storage for logging arbitrage opportunities."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from src.detector.base import ArbitrageOpportunity
from src.utils.logger import logger

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    market_id TEXT NOT NULL,
    market_question TEXT,
    yes_ask_vwap REAL,
    no_ask_vwap REAL,
    combined_cost REAL,
    gross_edge_percent REAL,
    net_edge_percent REAL,
    size_usd REAL,
    yes_liquidity REAL,
    no_liquidity REAL,
    max_safe_size REAL,
    is_crypto_15min INTEGER,
    verdict TEXT
);
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_opp_timestamp ON opportunities(timestamp);
CREATE INDEX IF NOT EXISTS idx_opp_market ON opportunities(market_id);
"""


class Database:
    """Async SQLite wrapper for opportunity logging."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Create the database and tables."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute(CREATE_TABLE_SQL)
        await self._db.executescript(CREATE_INDEX_SQL)
        await self._db.commit()
        logger.info("Database initialised at %s", self.db_path)

    async def log_opportunity(self, opp: ArbitrageOpportunity) -> None:
        """Insert an opportunity record."""
        if self._db is None:
            return
        await self._db.execute(
            """
            INSERT INTO opportunities (
                timestamp, market_id, market_question,
                yes_ask_vwap, no_ask_vwap, combined_cost,
                gross_edge_percent, net_edge_percent,
                size_usd, yes_liquidity, no_liquidity, max_safe_size,
                is_crypto_15min, verdict
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                opp.timestamp.isoformat(),
                opp.market_id,
                opp.market_question,
                opp.yes_ask_vwap,
                opp.no_ask_vwap,
                opp.combined_cost,
                opp.gross_edge_percent,
                opp.net_edge_percent,
                opp.size_usd,
                opp.yes_liquidity,
                opp.no_liquidity,
                opp.max_safe_size,
                int(opp.is_crypto_15min),
                opp.verdict,
            ),
        )
        await self._db.commit()

    async def get_stats_last_24h(self) -> dict:
        """Return summary stats for the last 24 hours."""
        if self._db is None:
            return {}
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        cursor = await self._db.execute(
            """
            SELECT
                COUNT(*) as total,
                COALESCE(AVG(net_edge_percent), 0) as avg_edge,
                COALESCE(MAX(net_edge_percent), 0) as max_edge,
                SUM(CASE WHEN verdict = 'ACTIONABLE' THEN 1 ELSE 0 END) as actionable
            FROM opportunities
            WHERE timestamp > ?
            """,
            (cutoff,),
        )
        row = await cursor.fetchone()
        if not row:
            return {"total": 0, "avg_edge": 0, "max_edge": 0, "actionable": 0}
        return {
            "total": row[0],
            "avg_edge": round(row[1], 2),
            "max_edge": round(row[2], 2),
            "actionable": row[3],
        }

    async def purge_old(self, hours: int = 24) -> int:
        """Delete records older than *hours*. Returns rows deleted."""
        if self._db is None:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        cursor = await self._db.execute(
            "DELETE FROM opportunities WHERE timestamp < ?", (cutoff,)
        )
        await self._db.commit()
        deleted = cursor.rowcount
        if deleted:
            logger.info("Purged %d old opportunity records", deleted)
        return deleted

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None
