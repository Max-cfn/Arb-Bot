"""Configuration loader with .env support and validation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    # Discord webhooks
    discord_webhook_health: str = ""
    discord_webhook_ops: str = ""
    discord_webhook_daily: str = ""
    discord_webhook_opportunities: str = ""
    discord_webhook_executions: str = ""  # errors / failures
    discord_webhook_executions_info: str = ""  # submitted / filled / cashout style updates

    # Detection params
    min_edge_percent: float = 0.5
    min_liquidity_usd: float = 100.0
    max_markets_watch: int = 500
    scan_interval_seconds: float = 1.0
    target_size_usd: float = 100.0

    # Market universe selection (aggressive scanning)
    market_min_liquidity_usd: float = 500.0
    market_min_volume_usd: float = 500.0

    # Safety buffers
    buffer_liquid_percent: float = 0.5
    buffer_illiquid_percent: float = 1.0
    illiquid_threshold_usd: float = 1000.0

    # Database
    db_path: str = "data/opportunities.db"

    # API endpoints
    gamma_api: str = "https://gamma-api.polymarket.com"
    clob_api: str = "https://clob.polymarket.com"
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    geoblock_api: str = "https://polymarket.com/api/geoblock"

    def validate(self) -> list[str]:
        """Return a list of validation errors (empty = valid)."""
        errors: list[str] = []
        if not self.discord_webhook_opportunities:
            errors.append("DISCORD_WEBHOOK_OPPORTUNITIES is required")
        if self.min_edge_percent < 0:
            errors.append("MIN_EDGE_PERCENT must be >= 0")
        if self.min_liquidity_usd < 0:
            errors.append("MIN_LIQUIDITY_USD must be >= 0")
        if self.max_markets_watch < 1:
            errors.append("MAX_MARKETS_WATCH must be >= 1")
        return errors


def load_config(env_path: str | None = None) -> Config:
    """Load configuration from environment / .env file."""
    if env_path:
        load_dotenv(env_path)
    else:
        load_dotenv()

    cfg = Config(
        discord_webhook_health=os.getenv("DISCORD_WEBHOOK_HEALTH", ""),
        discord_webhook_ops=os.getenv("DISCORD_WEBHOOK_OPS", ""),
        discord_webhook_daily=os.getenv("DISCORD_WEBHOOK_DAILY", ""),
        discord_webhook_opportunities=os.getenv("DISCORD_WEBHOOK_OPPORTUNITIES", ""),
        discord_webhook_executions=os.getenv("DISCORD_WEBHOOK_EXECUTIONS", ""),
        discord_webhook_executions_info=os.getenv("DISCORD_WEBHOOK_EXECUTIONS_INFO", ""),
        min_edge_percent=float(os.getenv("MIN_EDGE_PERCENT", "0.5")),
        min_liquidity_usd=float(os.getenv("MIN_LIQUIDITY_USD", "100")),
        max_markets_watch=int(os.getenv("MAX_MARKETS_WATCH", "500")),
        scan_interval_seconds=float(os.getenv("SCAN_INTERVAL_SECONDS", "1")),
        target_size_usd=float(os.getenv("TARGET_SIZE_USD", "100")),
        market_min_liquidity_usd=float(os.getenv("MARKET_MIN_LIQUIDITY_USD", "500")),
        market_min_volume_usd=float(os.getenv("MARKET_MIN_VOLUME_USD", "500")),
        buffer_liquid_percent=float(os.getenv("BUFFER_LIQUID_PERCENT", "0.5")),
        buffer_illiquid_percent=float(os.getenv("BUFFER_ILLIQUID_PERCENT", "1.0")),
        illiquid_threshold_usd=float(os.getenv("ILLIQUID_THRESHOLD_USD", "1000")),
        db_path=os.getenv("DB_PATH", "data/opportunities.db"),
    )

    # Ensure data directory exists
    Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)

    return cfg
