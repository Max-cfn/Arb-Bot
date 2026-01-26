# Polymarket Arbitrage Bot

Binary arbitrage detection bot for Polymarket. Phase 1: alerting only (no auto-execution).

Scans orderbooks via WebSocket, detects when YES + NO asks sum to less than $1 (after fees), and sends Discord alerts.

## Architecture

```
Scanner (WebSocket) --> Detector (VWAP + Fees) --> Discord Alerts
       |                       |                        |
  Orderbook Manager      Arb Engine              SQLite Logs
```

## Quick Start

### 1. Install

```bash
# Clone and setup
git clone <repo-url> && cd polymarket-arb-bot
chmod +x setup.sh && ./setup.sh
```

Or manually:

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
mkdir -p data logs
cp .env.example .env
```

### 2. Configure

Edit `.env` with your Discord webhook URLs:

```env
DISCORD_WEBHOOK_HEALTH=https://discord.com/api/webhooks/...
DISCORD_WEBHOOK_OPS=https://discord.com/api/webhooks/...
DISCORD_WEBHOOK_DAILY=https://discord.com/api/webhooks/...
DISCORD_WEBHOOK_OPPORTUNITIES=https://discord.com/api/webhooks/...
```

### 3. Verify

```bash
source venv/bin/activate
python scripts/check_geoblock.py    # Must NOT be blocked
python scripts/test_discord.py      # Test webhook delivery
python scripts/list_markets.py      # List active markets
```

### 4. Run

```bash
# Development
python -m src.main

# Production (systemd)
sudo cp systemd/polymarket-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-bot
sudo journalctl -u polymarket-bot -f
```

## Detection Logic

**Binary arbitrage**: buy YES + NO shares. If their combined ask price is below $1 after fees and buffer, there is a guaranteed profit at resolution.

```
Edge = 1.0 - (VWAP_YES_ask + VWAP_NO_ask)
Net  = Edge - resolution_fee(2%) - trading_fee - safety_buffer
```

The bot uses VWAP (Volume-Weighted Average Price) across orderbook depth, not just best ask.

## Discord Channels

| Webhook | Purpose | Frequency |
|---------|---------|-----------|
| HEALTH | Heartbeat + stats | Every 5 min |
| OPS | Errors, reconnections | On event |
| DAILY | 24h summary | 08:00 UTC |
| OPPORTUNITIES | Arbitrage alerts | Real-time |

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MIN_EDGE_PERCENT` | 0.5 | Minimum net edge to alert (%) |
| `MIN_LIQUIDITY_USD` | 100 | Minimum liquidity per side ($) |
| `MAX_MARKETS_WATCH` | 500 | Max markets to scan |
| `BUFFER_LIQUID_PERCENT` | 0.5 | Safety buffer for liquid markets |
| `BUFFER_ILLIQUID_PERCENT` | 1.0 | Safety buffer for illiquid markets |
| `ILLIQUID_THRESHOLD_USD` | 1000 | Threshold to classify as illiquid |

## Tests

```bash
pytest tests/ -v
```

## Project Structure

```
src/
  main.py                  # Entry point (asyncio)
  config.py                # .env loader + validation
  scanner/
    websocket_client.py    # Real-time orderbook via WebSocket
    market_fetcher.py      # REST API market list
    orderbook_manager.py   # In-memory orderbook state
  detector/
    vwap.py                # VWAP calculation
    fees.py                # Fee calculation
    binary_arb.py          # Binary arbitrage detector
    multi_outcome_arb.py   # Multi-outcome (stub, Phase 2)
  alerts/
    discord.py             # Webhook client
    formatters.py          # Embed formatting
  storage/
    db.py                  # SQLite opportunity logs
  utils/
    logger.py              # Rotating file + console logging
    geoblock.py            # IP geoblock check
scripts/                   # Utility scripts
tests/                     # Unit tests
systemd/                   # Service file for 24/7 operation
```

## Requirements

- Python 3.12+
- VM with non-blocked IP (e.g. Amsterdam)
- Discord server with webhook URLs
