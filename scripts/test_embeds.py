
import asyncio
import os
import sys
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.alerts.discord import DiscordClient
from src.config import load_config
from src.detector.base import ArbitrageOpportunity

async def main():
    config = load_config()
    discord = DiscordClient(config)

    # Fake opportunity object
    opp = ArbitrageOpportunity(
        market_id="0xTEST_MARKET_ID_123456789",
        market_question="Will Bitcoin hit $100k by end of 2024?",
        condition_id="0xCONDITION_123",  # Missing arg fixed
        is_crypto_15min=False,           # Missing arg fixed
        yes_token_id="0xYES",
        no_token_id="0xNO",
        yes_ask_vwap=0.45,
        no_ask_vwap=0.52,
        combined_cost=0.97,
        gross_edge=0.03,
        gross_edge_percent=3.09,
        net_edge=0.028,
        net_edge_percent=2.88,
        size_usd=100.0,
        yes_liquidity=5000.0,
        no_liquidity=5000.0,
        max_safe_size=500.0,
        timestamp=datetime.now(timezone.utc),
        verdict="ACTIONABLE",
        slug="will-bitcoin-hit-100k-2024",
        end_date="2024-12-31T23:59:00Z",
        # New fields needed for detailed embed
        yes_best_ask=0.45,
        no_best_ask=0.52,
        combined_best_asks=0.97,
        one_share_net_edge_percent=3.09,
        one_share_net_profit=0.03
    )

    print("Sending TEST messages to 'executions' webhook...")

    # 1. SUBMITTED
    print("- Sending SUBMITTED...")
    await discord.send_execution(
        opp,
        status="SUBMITTED",
        run_id="run-123-start"
    )
    await asyncio.sleep(1)

    # 2. FILLED
    print("- Sending FILLED...")
    await discord.send_execution(
        opp,
        status="FILLED",
        run_id="run-123-fill",
        note="Order ID: 0x999...999"
    )
    await asyncio.sleep(1)

    # 3. FAILED
    print("- Sending FAILED...")
    await discord.send_execution(
        opp,
        status="FAILED",
        run_id="run-123-fail",
        note="Error: ClobClient error (timeout waiting for fill)"
    )
    await asyncio.sleep(1)

    # 4. PAYOUT (Future use)
    print("- Sending PAYOUT...")
    await discord.send_execution(
        opp,
        status="PAYOUT",
        run_id="run-123-pay",
        note="Claimed $103.00 (Profit: +$3.00)"
    )
    
    print("Done. Check Discord.")

if __name__ == "__main__":
    asyncio.run(main())
