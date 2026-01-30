"""Discord embed formatters for various alert types."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.detector.base import ArbitrageOpportunity

EMBED_COLORS = {
    "success": 0x00FF00,
    "warning": 0xFFAA00,
    "error": 0xFF0000,
    "info": 0x0099FF,
    "opportunity": 0xFFD700,
}


def format_opportunity_embed(opp: ArbitrageOpportunity) -> dict[str, Any]:
    """Format an arbitrage opportunity as a Discord webhook payload."""
    if opp.verdict == "ACTIONABLE":
        color = EMBED_COLORS["opportunity"]
        ping = "@here " if opp.net_edge_percent >= 3.0 else ""
    elif opp.verdict == "MARGINAL":
        color = EMBED_COLORS["warning"]
        ping = ""
    else:
        color = EMBED_COLORS["info"]
        ping = ""

    return {
        "content": f"{ping}**Arbitrage Detected**",
        "embeds": [
            {
                "title": opp.market_question[:256],
                "url": (
                    f"https://polymarket.com/market/{opp.slug}" if getattr(opp, "slug", "") else None
                ),
                "color": color,
                "fields": [
                    {
                        "name": "Edge",
                        "value": (
                            f"Gross: {opp.gross_edge_percent:.2f}%\n"
                            f"Net: {opp.net_edge_percent:.2f}%"
                        ),
                        "inline": True,
                    },
                    {
                        "name": "Prices",
                        "value": (
                            f"YES: ${opp.yes_ask_vwap:.4f}\n"
                            f"NO: ${opp.no_ask_vwap:.4f}\n"
                            f"Sum: ${opp.combined_cost:.4f}"
                        ),
                        "inline": True,
                    },
                    {
                        "name": "Liquidity",
                        "value": (
                            f"YES: ${opp.yes_liquidity:,.0f}\n"
                            f"NO: ${opp.no_liquidity:,.0f}"
                        ),
                        "inline": True,
                    },
                    {
                        "name": "Size Analysis",
                        "value": (
                            f"Target: ${opp.size_usd:,.0f}\n"
                            f"Max Safe: ${opp.max_safe_size:,.0f}"
                        ),
                        "inline": True,
                    },
                    {
                        "name": "Type",
                        "value": "Crypto 15min" if opp.is_crypto_15min else "Standard",
                        "inline": True,
                    },
                    {
                        "name": "Market",
                        "value": (
                            f"https://polymarket.com/market/{opp.slug}"
                            if getattr(opp, "slug", "")
                            else "(no slug)"
                        ),
                        "inline": False,
                    },
                    {
                        "name": "Verdict",
                        "value": opp.verdict,
                        "inline": True,
                    },
                ],
                "footer": {"text": f"Market ID: {opp.market_id[:16]}..."},
                "timestamp": opp.timestamp.isoformat(),
            }
        ],
    }


def format_health_embed(
    status: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    """Format a health-check embed."""
    is_ok = "running" in status.lower() or "starting" in status.lower()
    color = EMBED_COLORS["success"] if is_ok else EMBED_COLORS["error"]

    fields = [
        {"name": k, "value": str(v), "inline": True}
        for k, v in details.items()
    ]

    return {
        "embeds": [
            {
                "title": f"Health: {status}",
                "color": color,
                "fields": fields,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }


def format_ops_embed(message: str) -> dict[str, Any]:
    """Format an ops/error embed."""
    return {
        "embeds": [
            {
                "title": "Ops Alert",
                "description": message,
                "color": EMBED_COLORS["warning"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }


def format_daily_summary_embed(stats: dict[str, Any]) -> dict[str, Any]:
    """Format the daily summary embed."""
    fields = [
        {"name": k, "value": str(v), "inline": True}
        for k, v in stats.items()
    ]

    return {
        "embeds": [
            {
                "title": "Daily Summary",
                "color": EMBED_COLORS["info"],
                "fields": fields,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }
