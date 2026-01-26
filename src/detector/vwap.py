"""Volume-Weighted Average Price calculation on orderbook depth."""

from __future__ import annotations


def calculate_vwap(
    orderbook_side: list[tuple[float, float]],
    size_usd: float,
) -> float | None:
    """Calculate the VWAP to execute *size_usd* dollars on one side of the book.

    Args:
        orderbook_side: List of (price, size_in_shares) sorted best-first.
            - For asks: ascending price (cheapest first).
            - For bids: descending price (highest first).
        size_usd: Dollar amount to fill.

    Returns:
        VWAP price, or None if insufficient liquidity.

    Example::

        asks = [(0.65, 100), (0.66, 200), (0.67, 150)]
        vwap = calculate_vwap(asks, 250)
        # Fills 100 shares @ 0.65 ($65) + ~280 shares @ 0.66 ($185) => VWAP ≈ 0.6566
    """
    if not orderbook_side or size_usd <= 0:
        return None

    remaining = size_usd
    total_cost = 0.0
    total_shares = 0.0

    for price, available_shares in orderbook_side:
        if price <= 0:
            continue

        available_usd = available_shares * price
        take_usd = min(remaining, available_usd)
        shares = take_usd / price

        total_cost += take_usd
        total_shares += shares
        remaining -= take_usd

        if remaining <= 1e-9:
            break

    if remaining > 1e-9:
        return None  # Insufficient liquidity

    return total_cost / total_shares if total_shares > 0 else None


def available_liquidity_usd(orderbook_side: list[tuple[float, float]]) -> float:
    """Return total USD liquidity available on the given side."""
    return sum(price * size for price, size in orderbook_side if price > 0)
