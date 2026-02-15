"""Discord embed formatters for various alert types."""

from __future__ import annotations

from datetime import datetime, timezone
import math
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


def _format_plan_min_notional(opp: ArbitrageOpportunity, *, min_order_usd: float = 1.0) -> str:
    yes_p = float(getattr(opp, "yes_best_ask", 0.0) or 0.0)
    no_p = float(getattr(opp, "no_best_ask", 0.0) or 0.0)

    # Requirement: min 5 shares each side AND min $1 notional on EACH leg
    min_shares = 5
    shares_yes_usd = int(math.ceil(min_order_usd / yes_p)) if yes_p > 0 else 0
    shares_no_usd = int(math.ceil(min_order_usd / no_p)) if no_p > 0 else 0
    shares = max(min_shares, shares_yes_usd, shares_no_usd)

    yes_notional = shares * yes_p
    no_notional = shares * no_p
    total_cost = yes_notional + no_notional
    est_profit = shares * float(getattr(opp, "one_share_net_profit", 0.0) or 0.0)

    return (
        f"YES ask: ${yes_p:.4f}\n"
        f"NO ask:  ${no_p:.4f}\n"
        f"Shares: {shares} YES + {shares} NO\n"
        f"Notional YES: ${yes_notional:.4f}\n"
        f"Notional NO:  ${no_notional:.4f}\n"
        f"Total cost: ${total_cost:.4f}\n"
        f"Net edge (per 1+1): {float(getattr(opp, 'one_share_net_edge_percent', 0.0) or 0.0):.2f}% "
        f"(profit ${float(getattr(opp, 'one_share_net_profit', 0.0) or 0.0):.4f})\n"
        f"Est. profit @shares: ${est_profit:.4f}"
    )


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
                            f"YES_age={getattr(opp, 'yes_book_age_s', 0.0):.6f}s\n"
                            f"NO_age={getattr(opp, 'no_book_age_s', 0.0):.6f}s"
                        ),
                        "inline": True,
                    },
                    {
                        "name": "Plan (min 5 shares + $1/leg)",
                        "value": _format_plan_min_notional(opp, min_order_usd=1.0),
                        "inline": True,
                    },
                    {
                        "name": "Market rank",
                        "value": f"#{getattr(opp, 'market_rank_idx', 0)} of {getattr(opp, 'market_total_count', 0)}",
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


def format_opportunity_expired_embed(
    opp: ArbitrageOpportunity,
    *,
    duration_s: float,
    last_edge_percent: float | None = None,
) -> dict[str, Any]:
    """Follow-up message when an opportunity edge drops below the floor threshold."""
    edge = float(last_edge_percent) if last_edge_percent is not None else float(getattr(opp, "net_edge_percent", 0.0) or 0.0)
    duration_ms = duration_s * 1000.0
    return {
        "content": f"⏱️ **Edge dropped < 1%** (last seen {edge:.2f}%)",
        "embeds": [
            {
                "title": opp.market_question[:256],
                "url": (
                    f"https://polymarket.com/market/{opp.slug}"
                    if getattr(opp, "slug", "")
                    else None
                ),
                "color": EMBED_COLORS["info"],
                "fields": [
                    {
                        "name": "Edge lifetime",
                        "value": f"{duration_s:.6f}s ({duration_ms:.0f}ms)",
                        "inline": True,
                    },
                    {
                        "name": "Last observed (best asks)",
                        "value": (
                            f"YES: ${float(getattr(opp, 'yes_best_ask', 0.0) or 0.0):.4f}\n"
                            f"NO:  ${float(getattr(opp, 'no_best_ask', 0.0) or 0.0):.4f}"
                        ),
                        "inline": True,
                    },
                ],
                "footer": {"text": f"Market ID: {str(opp.market_id)[:16]}..."},
                "timestamp": datetime.now(timezone.utc).isoformat(),
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
    """Format a detailed execution message for Discord.

    Status Types:
    - SUBMITTED / PLACED / SENDING  → order just sent
    - FILLED / SUCCESS / COMPLETED  → both legs confirmed filled
    - FAILED / CANCELLED / REJECTED → order failed or unwound
    - WAITING                        → polling for fill
    - PAYOUT / CLAIMED               → future use
    """

    url = f"https://polymarket.com/market/{opp.slug}" if getattr(opp, "slug", "") else ""
    s_upper = status.upper()

    # ── colours & titles ──────────────────────────────────────────────────────
    if s_upper in ("SUBMITTED", "PLACED", "SENDING"):
        color = EMBED_COLORS["execution_sub"]
        title_prefix = "Order Submitted (FOK)"
        content_ping = ""
    elif s_upper in ("FILLED", "SUCCESS", "COMPLETED"):
        color = EMBED_COLORS["execution_fill"]
        title_prefix = "FILLED"
        content_ping = "@here "          # always ping on success
    elif s_upper in ("FAILED", "CANCELLED", "REJECTED", "ERROR"):
        color = EMBED_COLORS["execution_fail"]
        title_prefix = "Order Failed / Cancelled"
        content_ping = ""
    elif s_upper in ("PAYOUT", "CLAIMED"):
        color = EMBED_COLORS["execution_payout"]
        title_prefix = "Payout Received"
        content_ping = ""
    elif s_upper == "WAITING":
        color = EMBED_COLORS["warning"]
        title_prefix = "Waiting for Fill"
        content_ping = ""
    else:
        color = EMBED_COLORS["info"]
        title_prefix = "Execution Update"
        content_ping = ""

    title = f"{title_prefix} | {s_upper}"

    # ── fields ────────────────────────────────────────────────────────────────
    fields: list[dict[str, Any]] = []

    # 1. Market
    market_val = f"[{opp.market_question[:200]}]({url})" if url else opp.market_question[:200]
    market_val += f"\nID: `{str(opp.market_id)[:20]}`"
    fields.append({"name": "Market", "value": market_val, "inline": False})

    # 2. Strategy / Edge
    fields.append({
        "name": "Edge",
        "value": (
            f"Net: **{opp.net_edge_percent:.2f}%**\n"
            f"Gross: {opp.gross_edge_percent:.2f}%\n"
            f"Est. profit: **${float(getattr(opp, 'one_share_net_profit', 0) or 0):.4f}**/share"
        ),
        "inline": True,
    })

    # 3. Orders
    yes_ask = float(getattr(opp, "yes_best_ask", 0) or 0)
    no_ask = float(getattr(opp, "no_best_ask", 0) or 0)
    combined = float(getattr(opp, "combined_best_asks", yes_ask + no_ask) or (yes_ask + no_ask))
    fields.append({
        "name": "Orders (FOK limit)",
        "value": (
            f"BUY YES @ ${yes_ask:.4f}\n"
            f"BUY NO  @ ${no_ask:.4f}\n"
            f"Combined: ${combined:.4f}"
        ),
        "inline": True,
    })

    # 4. Status-specific detail
    if s_upper in ("SUBMITTED", "PLACED", "SENDING"):
        fields.append({
            "name": "Status",
            "value": "FOK orders sent. Will auto-cancel if not filled immediately.",
            "inline": False,
        })
    elif s_upper in ("FILLED", "SUCCESS", "COMPLETED"):
        fields.append({
            "name": "Result",
            "value": "**Both legs filled.** Arbitrage position locked.",
            "inline": False,
        })
    elif s_upper in ("FAILED", "CANCELLED", "REJECTED", "ERROR"):
        fields.append({
            "name": "Reason",
            "value": (note[:1000] if note else "Unknown error or timeout."),
            "inline": False,
        })
    elif s_upper == "PAYOUT":
        fields.append({
            "name": "Payout",
            "value": (note[:1000] if note else "Funds claimed."),
            "inline": False,
        })

    # 5. Note (extra detail / timings — only for non-error statuses where note wasn't consumed)
    if note and s_upper not in ("FAILED", "CANCELLED", "REJECTED", "ERROR", "PAYOUT"):
        fields.append({"name": "Detail", "value": note[:1000], "inline": False})

    footer_text = f"Run: {run_id}" if run_id else f"Market: {str(opp.market_id)[:12]}"

    return {
        "content": f"{content_ping}**{title}**" if content_ping else None,
        "embeds": [
            {
                "title": title,
                "url": url if url else None,
                "color": color,
                "fields": fields,
                "footer": {"text": footer_text},
                "timestamp": datetime.now(timezone.utc).isoformat(),
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
