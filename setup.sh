#!/usr/bin/env bash
set -euo pipefail

echo "=== Polymarket Arb Bot - Setup ==="

# System packages
echo "[1/5] Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3.12 python3.12-venv python3-pip sqlite3

# Virtual environment
echo "[2/5] Creating virtual environment..."
python3.12 -m venv venv
source venv/bin/activate

# Python deps
echo "[3/5] Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# Data directory
echo "[4/5] Creating data directory..."
mkdir -p data logs

# .env
echo "[5/5] Setting up environment..."
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from template. Edit it with your webhook URLs."
else
    echo ".env already exists, skipping."
fi

# systemd
echo ""
echo "To install as a systemd service:"
echo "  sudo cp systemd/polymarket-bot.service /etc/systemd/system/"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable polymarket-bot"
echo "  sudo systemctl start polymarket-bot"
echo ""
echo "=== Setup complete ==="
