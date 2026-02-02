"""Discord embed formatters for various alert types."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from src.detector.base import ArbitrageOpportunity

EMBED_COLORS = {
    "success": 0x00FF00,       # Green
    "warning": 0xFFAA00,       # Orange
    "error": 0xFF0000,         # Red
    "info": 0x0099FF,          # Blue
    "opportunity": 0xFFD700,   # Gold
    "execution_sub": 0x3498DB, # Blue (Submitted)
    "execution_fill": 0x2ECC71,# Green (Filled)
    "execution_fail": 0xE74C3C,# Red (Failed)
    "execution_payout": 0x9B59B6, # Purple (Payout)
}

PARIS_TZ = ZoneInfo("Europe/Paris")


def _parse_end_date(end_date: str) -> datetime | None:
    if not end_date:
        return None
    try:
        dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _format_resolve_time_paris(end_date: str) -> str:
    dt = _parse_end_date(end_date)
    if not dt:
        return end_date or "(unknown)"

    paris = dt.astimezone(PARIS_TZ)
    return paris.strftime("%Y-%m-%d %H:%M") + " (Paris)"


def _format_time_left(end_date: str) -> str:
    dt = _parse_end_date(end_date)
    if not dt:
        return "(unknown)"

    now = datetime.now(timezone.utc)
    delta_s = (dt - now).total_seconds()
    if delta_s <= 0:
        return "0m"

    total_m = int(delta_s // 60)
    h, m = divmod(total_m, 60)
    if h >= 48:
        d, h2 = divmod(h, 24)
        return f"{d}d {h2}h"
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


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
        "content": f"{ping}**Arbitrage Detected {opp.net_edge_percent:.1f}%**",
        "embeds": [
            {
                "title": opp.market_question[:256],
                "url": (
                    f"https://polymarket.com/market/{opp.slug}"
                    if getattr(opp, "slug", "")
                    else None
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
                        "name": "Prices (VWAP)",
                        "value": (
                            f"YES: ${opp.yes_ask_vwap:.4f}\n"
                            f"NO: ${opp.no_ask_vwap:.4f}\n"
                            f"Sum: ${opp.combined_cost:.4f}"
                        ),
                        "inline": True,
                    },
                    {
                        "name": "Fees (taker est.)",
                        "value": (
                            f"YES: {getattr(opp, 'taker_fee_rate_percent_yes', 0.0):.2f}% (bps={getattr(opp, 'fee_rate_bps_yes', 0)})\n"
                            f"NO:  {getattr(opp, 'taker_fee_rate_percent_no', 0.0):.2f}% (bps={getattr(opp, 'fee_rate_bps_no', 0)})"
                        ),
                        "inline": True,
                    },
                    {
                        "name": "Data freshness",
                        "value": (
                            f"YES_age={getattr(opp, 'yes_book_age_s', 0.0):.3f}s\n"
                            f"NO_age={getattr(opp, 'no_book_age_s', 0.0):.3f}s"
                        ),
                        "inline": True,
                    },
                    {
                        "name": "Plan (1 YES + 1 NO)",
                        "value": (
                            f"YES ask: ${opp.yes_best_ask:.4f}\n"
                            f"NO ask: ${opp.no_best_ask:.4f}\n"
                            f"Sum: ${opp.combined_best_asks:.4f}\n"
                            f"Net: {opp.one_share_net_edge_percent:.2f}% (profit ${opp.one_share_net_profit:.4f})"
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
                        "name": "Resolves",
                        "value": _format_resolve_time_paris(getattr(opp, "end_date", "")),
                        "inline": True,
                    },
                    {
                        "name": "Time left",
                        "value": _format_time_left(getattr(opp, "end_date", "")),
                        "inline": True,
                    },
                    {
                        "name": "Verdict",
                        "value": opp.verdict,
                        "inline": True,
                    },
                ],
                "footer": {"text": f"Market ID: {str(opp.market_id)[:16]}..."},
                "timestamp": opp.timestamp.isoformat(),
            }
        ],
    }


def format_execution_embed(
    opp: ArbitrageOpportunity,
    note: str = "",
    *,
    run_id: str = "",
    status: str = "PLANNED",
) -> dict[str, Any]:
    """Format a Detailed Execution Message.
    
    Status Types:
    - PLANNED/SUBMITTED: Order placement details.
    - FILLED/SUCCESS: Success confirmation.
    - FAILED/CANCELLED: Failure/Cancellation details.
    - PAYOUT: Payout received (future use).
    """
    
    url = f"https://polymarket.com/market/{opp.slug}" if getattr(opp, "slug", "") else ""
    
    # Defaults
    color = EMBED_COLORS["info"]
    title_prefix = "ℹ️ Execution Update"
    
    s_upper = status.upper()
    
    if s_upper in ("SUBMITTED", "PLACED", "SENDING"):
        color = EMBED_COLORS["execution_sub"]
        title_prefix = "🛒 Order Placed"
    elif s_upper in ("FILLED", "SUCCESS", "COMPLETED"):
        color = EMBED_COLORS["execution_fill"]
        title_prefix = "✅ Order Filled"
    elif s_upper in ("FAILED", "CANCELLED", "REJECTED", "ERROR"):
        color = EMBED_COLORS["execution_fail"]
        title_prefix = "❌ Order Failed/Cancelled"
    elif s_upper in ("PAYOUT", "CLAIMED"):
        color = EMBED_COLORS["execution_payout"]
        title_prefix = "💰 Payout Received"
    elif s_upper == "WAITING":
        color = EMBED_COLORS["warning"]
        title_prefix = "⏳ Waiting for Fill"

    title = f"{title_prefix} | {s_upper}"
    
    # Common Fields
    fields = []
    
    # 1. Market Info
    fields.append({
        "name": "Market",
        "value": f"[{opp.market_question}]({url})\nID: `{str(opp.market_id)[:16]}...`",
        "inline": False
    })
    
    # 2. Strategy / Edge
    fields.append({
        "name": "Strategy",
        "value": f"Net Edge: **{opp.net_edge_percent:.2f}%**\nEst. Profit: ${opp.one_share_net_profit:.4f}/share",
        "inline": True
    })
    
    # 3. Order Details (What are we buying?)
    # Assuming standard arb: Buy YES + Buy NO
    fields.append({
        "name": "Orders (Limit)",
        "value": (
            f"🟢 **BUY YES** @ ${opp.yes_best_ask:.4f}\n"
            f"🔴 **BUY NO**  @ ${opp.no_best_ask:.4f}\n"
            f"Total Cost: ${opp.combined_best_asks:.4f}"
        ),
        "inline": True
    })

    # 4. Status Specifics
    if s_upper in ("SUBMITTED", "PLACED"):
        fields.append({
            "name": "Status",
            "value": "Orders submitted to CLOB. Waiting for confirmation...",
            "inline": False
        })
    elif s_upper == "FILLED":
        fields.append({
            "name": "Execution Result",
            "value": "✅ **Both legs filled.** Position secured.",
            "inline": False
        })
    elif s_upper in ("FAILED", "CANCELLED"):
        fields.append({
            "name": "Failure Reason",
            "value": note if note else "Unknown error or timeout.",
            "inline": False
        })
    elif s_upper == "PAYOUT":
        fields.append({
            "name": "Payout Details",
            "value": note if note else "Funds claimed successfully.",
            "inline": False
        })

    # 5. Technical / Debug Info
    footer_text = f"Run ID: {run_id}" if run_id else f"Market: {str(opp.market_id)[:10]}"
    if note and s_upper not in ("FAILED", "CANCELLED", "PAYOUT"):
        # If note wasn't used in main body, add it to footer or description
        pass # simplified for cleaner look, or could add field

    # Extra note field if provided and not already consumed
    if note and s_upper not in ("FAILED", "CANCELLED", "PAYOUT"):
         fields.append({
            "name": "Note",
            "value": note,
            "inline": False
        })

    return {
        "embeds": [
            {
                "title": title,
                "url": url if url else None,
                "color": color,
                "fields": fields,
                "footer": {"text": footer_text},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
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
