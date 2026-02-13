# Polymarket Arb-Bot (Single Service + Rust Hotpath)

Polymarket binary arbitrage bot with:
- **single systemd service** (`polymarket-bot`)
- **Rust hotpath** for WS market stream, detection, and order submission
- **Python control plane** for config loading, Discord alerting, DB logging, summaries, and ops

> Current production model: **one service only**. `polymarket-rust` standalone unit is not used.

---

## Current Architecture

```text
systemd: polymarket-bot (python -m src.main)
  ├─ Python (control plane)
  │   ├─ load .env / validate config
  │   ├─ fetch & rank markets (Gamma)
  │   ├─ Discord alerts (opportunities + executions + ops + health)
  │   ├─ SQLite logging / summaries
  │   └─ lifecycle / shutdown
  │
  └─ Rust (hotpath, embedded via PyO3 module: polymarket_engine)
      ├─ WS subscriptions (CLOB)
      ├─ orderbook updates
      ├─ binary arb detection
      └─ 2-leg submission (YES+NO) with HMAC auth
```

### Why this split
- Keep **latency-sensitive** path in Rust.
- Keep **business + alerting + observability** in Python.
- Keep operations simple: one process, one service.

---

## Important Runtime Behavior

- `RUST_HOTPATH_ENABLED=1` (default) => Rust detection/execution pipeline is used inside Python service.
- Alerts are still sent from Python Discord client.
- Opportunity alert plan enforces:
  - minimum **5 shares per leg**
  - minimum **$1 notional per leg**
- Opportunity embeds include market rank (`#X of Y`).
- Execution embeds now include:
  - status + reason details
  - reason_code (`POST_FAILED`, `PARTIAL_POST_FAILED`, ...)
  - timing in ms with 5 decimals
  - YES/NO quantities + notionals + total sum

---

## Service Management

### Main service
```bash
sudo systemctl status polymarket-bot
sudo systemctl restart polymarket-bot
journalctl -u polymarket-bot -f
```

### Rust standalone service (should stay disabled)
```bash
sudo systemctl status polymarket-rust
# expected in current architecture: inactive/disabled
```

---

## Setup

## 1) Python env
```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
mkdir -p data logs
cp .env.example .env
```

## 2) Rust toolchain + PyO3 wheel
```bash
# rustup + cargo installed
cd rust_engine
# build wheel for local venv python
PATH=/opt/polymarket-bot/Arb-Bot/venv/bin:$PATH \
  /opt/polymarket-bot/Arb-Bot/venv/bin/maturin build --release -i /opt/polymarket-bot/Arb-Bot/venv/bin/python

/opt/polymarket-bot/Arb-Bot/venv/bin/pip install --force-reinstall target/wheels/polymarket_engine-*.whl
```

---

## Critical `.env` Keys

### Trading / CLOB auth
- `CLOB_PRIVATE_KEY`
- `CLOB_FUNDER_ADDRESS`
- `CLOB_API_KEY`
- `CLOB_API_SECRET`
- `CLOB_API_PASSPHRASE`

### Execution sizing
- `CLOB_MIN_ORDER_USD` (default 1.0)
- `CLOB_MIN_ORDER_SHARES` (default 5)
- `CLOB_AGGRESSIVE_CROSS_BPS` (default 5)

### Strategy / filters
- `MIN_EDGE_PERCENT`
- `MIN_LIQUIDITY_USD`
- `MAX_MARKETS_WATCH`
- `MARKET_MIN_LIQUIDITY_USD`
- `MARKET_MIN_VOLUME_USD`
- `BUFFER_LIQUID_PERCENT`
- `BUFFER_ILLIQUID_PERCENT`
- `ILLIQUID_THRESHOLD_USD`
- `TARGET_SIZE_USD`

### Runtime mode
- `EXECUTION_MODE` (`dryrun` or `real`)
- `RUST_HOTPATH_ENABLED` (`1`/`0`)

---

## CLOB API Secret Format (very important)

`CLOB_API_SECRET` must be valid data for Rust HMAC decode.

- Rust currently decodes with **standard base64 decoder**.
- If your secret is url-safe base64 (`-` and `_`), execution can fail with:
  `Failed to base64-decode api_secret`

### Quick check
```bash
source venv/bin/activate
python - <<'PY'
from dotenv import load_dotenv
import os, base64, binascii
load_dotenv('/opt/polymarket-bot/Arb-Bot/.env', override=True)
s=os.getenv('CLOB_API_SECRET','').strip()
try:
    base64.b64decode(s, validate=True)
    print('OK_BASE64')
except binascii.Error:
    print('INVALID_BASE64')
PY
```

### If needed: convert base64url -> base64 standard
```bash
source venv/bin/activate
python - <<'PY'
from dotenv import dotenv_values
from pathlib import Path
import base64

p = Path('/opt/polymarket-bot/Arb-Bot/.env')
vals = dotenv_values(p)
s = (vals.get('CLOB_API_SECRET') or '').strip()
raw = base64.urlsafe_b64decode(s)
fixed = base64.b64encode(raw).decode()
text = p.read_text()
text = text.replace(f"CLOB_API_SECRET={s}", f"CLOB_API_SECRET={fixed}")
p.write_text(text)
print('normalized')
PY
```

---

## Generate CLOB API creds (from current wallet)

```bash
cd /opt/polymarket-bot/Arb-Bot
source venv/bin/activate

python - <<'PY'
import os
from dotenv import load_dotenv
from py_clob_client.client import ClobClient

load_dotenv('/opt/polymarket-bot/Arb-Bot/.env', override=True)

c = ClobClient(
    os.getenv('CLOB_BASE_URL','https://clob.polymarket.com'),
    key=os.getenv('CLOB_PRIVATE_KEY'),
    chain_id=int(os.getenv('CLOB_CHAIN_ID','137')),
    signature_type=int(os.getenv('CLOB_SIGNATURE_TYPE','0')),
    funder=os.getenv('CLOB_FUNDER_ADDRESS'),
)
creds = c.create_or_derive_api_creds()
print('CLOB_API_KEY=' + creds.api_key)
print('CLOB_API_SECRET=' + creds.api_secret)
print('CLOB_API_PASSPHRASE=' + creds.api_passphrase)
PY
```

Then update `.env` and restart service.

---

## Wallet switch playbook

If you change trading wallet, update and regenerate all wallet-bound auth:
1. `CLOB_PRIVATE_KEY`
2. `CLOB_FUNDER_ADDRESS`
3. regenerate `CLOB_API_KEY` / `CLOB_API_SECRET` / `CLOB_API_PASSPHRASE`
4. restart `polymarket-bot`

---

## Project Structure (current)

```text
src/
  main.py                  # entrypoint, single-service orchestration
  config.py                # .env loading + validation
  scanner/                 # legacy python scanner path (fallback / non-rust mode)
  detector/                # python types + detector components
  execution/               # python execution path (legacy / fallback)
  alerts/
    discord.py             # discord webhook client
    formatters.py          # embeds (opportunity/execution/ops/health)
  storage/
    db.py                  # sqlite logging
  utils/
    logger.py
    geoblock.py

rust_engine/
  src/lib.rs               # PyO3 module entrypoint (HotPathEngine)
  src/ws_client.rs         # WS stream + reconnects
  src/orderbook.rs         # in-memory books
  src/detector.rs          # arb detection logic
  src/executor.rs          # EIP-712 + CLOB POST submit
  src/cache.rs             # token metadata cache
  src/types.rs             # shared rust<->python types

systemd/
  polymarket-bot.service   # production unit (single service)
```

---

## Notes

- This repo has evolved from alert-only into real execution architecture.
- For production, always verify `journalctl` after deploys.
- If execution fails, first inspect `reason_code` + `reason_detail` in execution channel and service logs.
