"""Polymarket fee calculations (January 2026 fee structure).

Standard markets: 0% trading fees (fee-free).
Crypto 15-min markets: taker fee = 0.02 * min(price, 1-price).
Resolution fee: 2% on the payout (profit at resolution).
"""

from __future__ import annotations


def calculate_polymarket_fees(
    yes_price: float,
    no_price: float,
    size_usd: float,
    is_crypto_15min: bool = False,
) -> dict:
    """Calculate fees for a binary arbitrage position.

    Args:
        yes_price: VWAP ask price for YES shares.
        no_price: VWAP ask price for NO shares.
        size_usd: Dollar amount to deploy per side.
        is_crypto_15min: Whether this is a crypto 15-min market.

    Returns:
        Dict with fee breakdown and profit calculations.
    """
    gross_cost = (yes_price + no_price) * size_usd

    # Trading fees
    if is_crypto_15min:
        yes_fee = 0.02 * min(yes_price, 1.0 - yes_price) * size_usd
        no_fee = 0.02 * min(no_price, 1.0 - no_price) * size_usd
        trading_fee = yes_fee + no_fee
    else:
        trading_fee = 0.0

    # Resolution fee: 2% on the payout ($1 per share)
    resolution_fee = 0.02 * size_usd

    total_fee = trading_fee + resolution_fee
    net_cost = gross_cost + trading_fee
    guaranteed_payout = size_usd  # $1 per share
    gross_profit = size_usd - gross_cost
    net_profit = (guaranteed_payout - resolution_fee) - net_cost

    return {
        "trading_fee": trading_fee,
        "resolution_fee": resolution_fee,
        "total_fee": total_fee,
        "net_cost": net_cost,
        "guaranteed_payout": guaranteed_payout - resolution_fee,
        "gross_profit": gross_profit,
        "net_profit": net_profit,
    }
