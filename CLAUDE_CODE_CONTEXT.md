# CLAUDE CODE CONTEXT - Polymarket Arbitrage Bot

> **INSTRUCTION POUR CLAUDE CODE** : Ce fichier contient TOUTES les specifications du projet.
> Lis-le entièrement avant de commencer. Implémente le projet complet en suivant ces specs.

---

## 📋 RÉSUMÉ EXÉCUTIF

**Objectif** : Bot de détection d'arbitrage sur Polymarket (phase 1 = alerting only, pas d'exécution auto)

**Stack** : Python 3.12+ | asyncio | websockets | SQLite | Discord webhooks

**Infrastructure** : VM UpCloud Amsterdam (NL-AMS1) | Ubuntu 24.04

**Contrainte légale** : Utilisateur résident FR, Polymarket bloqué en France. Le bot tourne sur une VM Amsterdam avec IP statique non-bloquée.

---

## 🏗️ ARCHITECTURE

```
┌─────────────────────────────────────────────────────────────────┐
│                     VM AMSTERDAM (UpCloud NL-AMS1)              │
│                     Ubuntu 24.04 | Python 3.12                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│   ┌─────────────┐    ┌──────────────┐    ┌─────────────────┐   │
│   │  Scanner    │───▶│  Detector    │───▶│  Discord Alert  │   │
│   │  WebSocket  │    │  Arb Engine  │    │  (webhooks)     │   │
│   └─────────────┘    └──────────────┘    └─────────────────┘   │
│         │                   │                     │             │
│         ▼                   ▼                     ▼             │
│   ┌─────────────┐    ┌──────────────┐    ┌─────────────────┐   │
│   │  Orderbook  │    │  VWAP + Fee  │    │  SQLite Logs    │   │
│   │  Manager    │    │  Calculator  │    │  (historique)   │   │
│   └─────────────┘    └──────────────┘    └─────────────────┘   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 📁 STRUCTURE DES FICHIERS À CRÉER

```
polymarket-arb-bot/
├── README.md                    # Doc principale (comment run)
├── SETUP_WALLET.md              # Guide création wallet Polymarket
├── CLAUDE_CODE_CONTEXT.md       # CE FICHIER (ne pas modifier)
├── .env.example                 # Template variables d'environnement
├── .gitignore
├── requirements.txt
├── setup.sh                     # Script installation VM
│
├── src/
│   ├── __init__.py
│   ├── main.py                  # Entry point asyncio
│   ├── config.py                # Chargement .env + validation
│   │
│   ├── scanner/
│   │   ├── __init__.py
│   │   ├── websocket_client.py  # Connexion WebSocket Polymarket
│   │   ├── market_fetcher.py    # REST API pour liste des marchés
│   │   └── orderbook_manager.py # Maintien état orderbooks en mémoire
│   │
│   ├── detector/
│   │   ├── __init__.py
│   │   ├── base.py              # Classe abstraite BaseDetector
│   │   ├── binary_arb.py        # Détection arbitrage binaire (YES+NO < 1)
│   │   ├── multi_outcome_arb.py # Détection arbitrage multi-outcome
│   │   ├── vwap.py              # Calcul VWAP sur profondeur orderbook
│   │   └── fees.py              # Calcul fees Polymarket
│   │
│   ├── alerts/
│   │   ├── __init__.py
│   │   ├── discord.py           # Client Discord webhook
│   │   └── formatters.py        # Formatage messages (embeds)
│   │
│   ├── storage/
│   │   ├── __init__.py
│   │   └── db.py                # SQLite pour logs opportunités
│   │
│   └── utils/
│       ├── __init__.py
│       ├── logger.py            # Logging structuré
│       └── geoblock.py          # Vérification IP non bloquée
│
├── scripts/
│   ├── check_geoblock.py        # Test rapide si IP bloquée
│   ├── test_discord.py          # Test envoi webhooks
│   └── list_markets.py          # Liste tous les marchés actifs
│
├── tests/
│   ├── __init__.py
│   ├── test_binary_arb.py
│   ├── test_vwap.py
│   └── test_fees.py
│
└── systemd/
    └── polymarket-bot.service   # Service systemd pour run 24/7
```

---

## 🔧 SPECIFICATIONS TECHNIQUES

### 1. Configuration (.env)

```env
# === POLYMARKET API ===
# Phase 1 : read-only, pas besoin de private key
# Phase 2 : décommenter pour trading
# POLYMARKET_PRIVATE_KEY=0x...
# POLYMARKET_PROXY_WALLET=0x...

# === DISCORD WEBHOOKS ===
DISCORD_WEBHOOK_HEALTH=https://discord.com/api/webhooks/xxx/yyy
DISCORD_WEBHOOK_OPS=https://discord.com/api/webhooks/xxx/yyy
DISCORD_WEBHOOK_DAILY=https://discord.com/api/webhooks/xxx/yyy
DISCORD_WEBHOOK_OPPORTUNITIES=https://discord.com/api/webhooks/xxx/yyy

# === DETECTION PARAMS ===
MIN_EDGE_PERCENT=0.5          # Edge minimum pour alerter (%)
MIN_LIQUIDITY_USD=100         # Liquidité minimum par côté ($)
MAX_MARKETS_WATCH=500         # Nombre max de marchés à surveiller
SCAN_INTERVAL_SECONDS=1       # Intervalle entre scans

# === SAFETY ===
BUFFER_LIQUID_PERCENT=0.5     # Buffer sécurité marchés liquides
BUFFER_ILLIQUID_PERCENT=1.0   # Buffer sécurité marchés illiquides
ILLIQUID_THRESHOLD_USD=1000   # Seuil pour considérer illiquide

# === DATABASE ===
DB_PATH=data/opportunities.db
```

### 2. APIs Polymarket

**Endpoints à utiliser :**

```python
# REST - Liste des marchés
GAMMA_API = "https://gamma-api.polymarket.com"
# GET /events - Liste tous les events
# GET /markets - Liste tous les marchés

# REST - Trading (CLOB)
CLOB_API = "https://clob.polymarket.com"
# GET /book - Orderbook d'un token
# GET /midpoint - Midpoint price
# GET /price - Best bid/ask

# WebSocket - Real-time
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
# Subscribe to orderbook updates

# Geoblock check
GEOBLOCK_API = "https://polymarket.com/api/geoblock"
# GET - Retourne {blocked: bool, ip: str, country: str}
```

**Structure d'un marché Polymarket :**

```python
{
    "id": "0x...",                    # Market ID
    "question": "Will X happen?",     # Question
    "conditionId": "0x...",          # Condition ID
    "slug": "will-x-happen",
    "tokens": [
        {
            "token_id": "123...",     # Token ID pour YES
            "outcome": "Yes",
            "price": 0.65            # Prix actuel
        },
        {
            "token_id": "456...",     # Token ID pour NO
            "outcome": "No",
            "price": 0.36
        }
    ],
    "volume": 150000,                 # Volume total
    "liquidity": 25000,              # Liquidité
    "endDate": "2025-03-01T00:00:00Z"
}
```

### 3. Calcul VWAP (Volume-Weighted Average Price)

```python
def calculate_vwap(orderbook_side: list[tuple[float, float]], size_usd: float) -> float | None:
    """
    Calcule le prix moyen pondéré pour exécuter `size_usd` sur un côté de l'orderbook.
    
    Args:
        orderbook_side: Liste de (price, size) triée par prix (best first)
        size_usd: Taille en USD à exécuter
        
    Returns:
        VWAP ou None si liquidité insuffisante
    
    Example:
        asks = [(0.65, 100), (0.66, 200), (0.67, 150)]
        vwap = calculate_vwap(asks, 250)
        # Remplit 100 @ 0.65 + 150 @ 0.66 = 0.6560
    """
    remaining = size_usd
    total_cost = 0.0
    total_size = 0.0
    
    for price, available_size in orderbook_side:
        take_size = min(remaining, available_size * price)
        shares = take_size / price
        total_cost += take_size
        total_size += shares
        remaining -= take_size
        
        if remaining <= 0:
            break
    
    if remaining > 0:
        return None  # Liquidité insuffisante
        
    return total_cost / total_size if total_size > 0 else None
```

### 4. Calcul Fees Polymarket

```python
"""
FEES POLYMARKET (Janvier 2026):

1. Marchés standard : FEE-FREE (0% maker, 0% taker)
   
2. Marchés crypto 15-min : Variable
   - Taker fee max effectif : ~1.56% autour de 50c
   - Formule : fee = 0.02 * min(price, 1-price)
   - Makers peuvent recevoir des rebates

3. Fee de résolution : 2% sur les gains
   - Appliqué uniquement sur le profit lors de la résolution
   - PAS sur les trades (achat/vente)

Pour l'arbitrage binaire (YES + NO), seul le fee de résolution compte car :
- On achète YES + NO
- Un seul gagne (payout $1)
- Fee 2% sur le gain de $1 = $0.02 fixe
"""

def calculate_polymarket_fees(
    yes_price: float,
    no_price: float,
    size_usd: float,
    is_crypto_15min: bool = False
) -> dict:
    """
    Calcule les fees pour une position d'arbitrage binaire.
    
    Returns:
        {
            "trading_fee": float,      # Fee de trading (0 sauf crypto 15min)
            "resolution_fee": float,   # Fee de résolution (2% du payout)
            "total_fee": float,
            "net_cost": float,         # Coût total net
            "guaranteed_payout": float # Toujours 1.0 pour binaire
        }
    """
    gross_cost = (yes_price + no_price) * size_usd
    
    # Trading fees (0 pour marchés standard)
    if is_crypto_15min:
        # Fee sur chaque leg
        yes_fee = 0.02 * min(yes_price, 1 - yes_price) * size_usd
        no_fee = 0.02 * min(no_price, 1 - no_price) * size_usd
        trading_fee = yes_fee + no_fee
    else:
        trading_fee = 0.0
    
    # Resolution fee : 2% sur le payout ($1 par share)
    resolution_fee = 0.02 * size_usd
    
    total_fee = trading_fee + resolution_fee
    net_cost = gross_cost + trading_fee
    guaranteed_payout = size_usd  # $1 par share
    
    return {
        "trading_fee": trading_fee,
        "resolution_fee": resolution_fee,
        "total_fee": total_fee,
        "net_cost": net_cost,
        "guaranteed_payout": guaranteed_payout - resolution_fee,
        "gross_profit": size_usd - gross_cost,
        "net_profit": (guaranteed_payout - resolution_fee) - net_cost
    }
```

### 5. Détection Arbitrage Binaire

```python
"""
ARBITRAGE BINAIRE : Acheter YES + NO quand leur somme < 1

Condition : ask_YES + ask_NO < 1.0 - fees - buffer

Exemple :
- ask_YES = 0.48 (meilleur ask pour acheter YES)
- ask_NO = 0.49 (meilleur ask pour acheter NO)
- Somme = 0.97
- Payout garanti = $1.00
- Gross profit = $0.03 (3.09%)
- Net profit après fee résolution (2%) = $0.01 (1.03%)

IMPORTANT : 
- Utiliser VWAP, pas juste best ask (profondeur compte)
- Vérifier liquidité suffisante des deux côtés
- Appliquer buffer de sécurité
"""

@dataclass
class ArbitrageOpportunity:
    market_id: str
    market_question: str
    yes_token_id: str
    no_token_id: str
    
    # Prix
    yes_ask_vwap: float      # VWAP pour acheter YES
    no_ask_vwap: float       # VWAP pour acheter NO
    combined_cost: float     # yes_ask + no_ask
    
    # Profits
    gross_edge: float        # 1 - combined_cost
    gross_edge_percent: float
    net_edge: float          # Après fees
    net_edge_percent: float
    
    # Liquidité
    size_usd: float          # Taille analysée
    yes_liquidity: float     # Liquidité dispo côté YES
    no_liquidity: float      # Liquidité dispo côté NO
    max_safe_size: float     # Taille max recommandée
    
    # Meta
    timestamp: datetime
    is_crypto_15min: bool
    
    # Verdict
    verdict: str             # "✅ ACTIONABLE" | "⚠️ MARGINAL" | "❌ SKIP"


def detect_binary_arbitrage(
    market: dict,
    yes_orderbook: dict,
    no_orderbook: dict,
    target_size_usd: float = 100.0,
    min_edge_percent: float = 0.5,
    buffer_percent: float = 0.5
) -> ArbitrageOpportunity | None:
    """
    Détecte une opportunité d'arbitrage binaire.
    
    Args:
        market: Données du marché
        yes_orderbook: {"asks": [(price, size), ...], "bids": [...]}
        no_orderbook: {"asks": [(price, size), ...], "bids": [...]}
        target_size_usd: Taille cible pour le calcul
        min_edge_percent: Edge minimum pour considérer (après fees)
        buffer_percent: Buffer de sécurité additionnel
        
    Returns:
        ArbitrageOpportunity ou None si pas d'opportunité
    """
    # Calcul VWAP pour la taille cible
    yes_vwap = calculate_vwap(yes_orderbook["asks"], target_size_usd)
    no_vwap = calculate_vwap(no_orderbook["asks"], target_size_usd)
    
    if yes_vwap is None or no_vwap is None:
        return None  # Liquidité insuffisante
    
    combined_cost = yes_vwap + no_vwap
    gross_edge = 1.0 - combined_cost
    gross_edge_percent = (gross_edge / combined_cost) * 100
    
    # Calcul fees
    is_crypto = is_crypto_15min_market(market)
    fees = calculate_polymarket_fees(yes_vwap, no_vwap, target_size_usd, is_crypto)
    
    net_edge = fees["net_profit"] / target_size_usd
    net_edge_percent = (net_edge / combined_cost) * 100
    
    # Appliquer buffer
    adjusted_edge_percent = net_edge_percent - buffer_percent
    
    if adjusted_edge_percent < min_edge_percent:
        return None
    
    # Déterminer verdict
    if adjusted_edge_percent >= 2.0:
        verdict = "✅ ACTIONABLE"
    elif adjusted_edge_percent >= 1.0:
        verdict = "⚠️ MARGINAL"
    else:
        verdict = "❌ SKIP"
    
    return ArbitrageOpportunity(
        market_id=market["id"],
        market_question=market["question"],
        yes_token_id=market["tokens"][0]["token_id"],
        no_token_id=market["tokens"][1]["token_id"],
        yes_ask_vwap=yes_vwap,
        no_ask_vwap=no_vwap,
        combined_cost=combined_cost,
        gross_edge=gross_edge,
        gross_edge_percent=gross_edge_percent,
        net_edge=net_edge,
        net_edge_percent=net_edge_percent,
        size_usd=target_size_usd,
        yes_liquidity=sum(s for _, s in yes_orderbook["asks"][:10]),
        no_liquidity=sum(s for _, s in no_orderbook["asks"][:10]),
        max_safe_size=min(fees["yes_liquidity"], fees["no_liquidity"]) * 0.5,
        timestamp=datetime.utcnow(),
        is_crypto_15min=is_crypto,
        verdict=verdict
    )
```

### 6. Discord Webhooks

```python
"""
4 WEBHOOKS CONFIGURÉS :

1. HEALTH - Heartbeat toutes les 5 min
   - Status: 🟢 Running | 🔴 Down
   - Marchés surveillés
   - Latence WS
   - Mémoire/CPU

2. OPS - Erreurs et warnings
   - Reconnexions WebSocket
   - Rate limits
   - Erreurs API
   - Geoblock détecté

3. DAILY - Résumé quotidien (08h00 UTC)
   - Opportunités détectées (24h)
   - Meilleure opportunité
   - Stats (avg edge, volume scanné)
   - Uptime

4. OPPORTUNITIES - Alertes temps réel
   - Chaque opportunité détectée
   - Embed riche avec tous les détails
   - Mention @here si edge > 3%
"""

DISCORD_EMBED_COLORS = {
    "success": 0x00FF00,      # Vert
    "warning": 0xFFAA00,      # Orange
    "error": 0xFF0000,        # Rouge
    "info": 0x0099FF,         # Bleu
    "opportunity": 0xFFD700,  # Or
}

def format_opportunity_embed(opp: ArbitrageOpportunity) -> dict:
    """Formate une opportunité en embed Discord."""
    
    if opp.verdict == "✅ ACTIONABLE":
        color = DISCORD_EMBED_COLORS["opportunity"]
        ping = "@here " if opp.net_edge_percent >= 3.0 else ""
    elif opp.verdict == "⚠️ MARGINAL":
        color = DISCORD_EMBED_COLORS["warning"]
        ping = ""
    else:
        color = DISCORD_EMBED_COLORS["info"]
        ping = ""
    
    return {
        "content": f"{ping}🎯 **Arbitrage Detected**",
        "embeds": [{
            "title": opp.market_question[:256],
            "color": color,
            "fields": [
                {
                    "name": "💰 Edge",
                    "value": f"Gross: {opp.gross_edge_percent:.2f}%\nNet: {opp.net_edge_percent:.2f}%",
                    "inline": True
                },
                {
                    "name": "💵 Prices",
                    "value": f"YES: ${opp.yes_ask_vwap:.4f}\nNO: ${opp.no_ask_vwap:.4f}\nSum: ${opp.combined_cost:.4f}",
                    "inline": True
                },
                {
                    "name": "📊 Liquidity",
                    "value": f"YES: ${opp.yes_liquidity:,.0f}\nNO: ${opp.no_liquidity:,.0f}",
                    "inline": True
                },
                {
                    "name": "📐 Size Analysis",
                    "value": f"Target: ${opp.size_usd:,.0f}\nMax Safe: ${opp.max_safe_size:,.0f}",
                    "inline": True
                },
                {
                    "name": "🏷️ Type",
                    "value": "Crypto 15min" if opp.is_crypto_15min else "Standard",
                    "inline": True
                },
                {
                    "name": "✅ Verdict",
                    "value": opp.verdict,
                    "inline": True
                }
            ],
            "footer": {
                "text": f"Market ID: {opp.market_id[:16]}..."
            },
            "timestamp": opp.timestamp.isoformat()
        }]
    }
```

### 7. WebSocket Polymarket

```python
"""
WEBSOCKET POLYMARKET

URL: wss://ws-subscriptions-clob.polymarket.com/ws/market

Messages:
1. Subscribe à un marché:
{
    "type": "subscribe",
    "channel": "market",
    "assets_ids": ["token_id_yes", "token_id_no"]
}

2. Réponse orderbook update:
{
    "type": "book",
    "asset_id": "token_id",
    "bids": [{"price": "0.48", "size": "1500.5"}, ...],
    "asks": [{"price": "0.52", "size": "2000.0"}, ...],
    "timestamp": "1706123456789"
}

IMPORTANT:
- Max ~250 assets par connexion WebSocket
- Pour surveiller plus, ouvrir plusieurs connexions
- Heartbeat automatique (ping/pong)
- Reconnexion automatique si déconnexion
"""

import asyncio
import websockets
import json
from typing import Callable, Awaitable

class PolymarketWebSocket:
    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    MAX_ASSETS_PER_CONNECTION = 250
    RECONNECT_DELAY = 5  # seconds
    
    def __init__(
        self,
        asset_ids: list[str],
        on_orderbook_update: Callable[[str, dict], Awaitable[None]],
        on_error: Callable[[Exception], Awaitable[None]] | None = None
    ):
        self.asset_ids = asset_ids
        self.on_orderbook_update = on_orderbook_update
        self.on_error = on_error
        self._ws = None
        self._running = False
        
    async def connect(self):
        """Établit la connexion WebSocket."""
        self._running = True
        
        while self._running:
            try:
                async with websockets.connect(self.WS_URL) as ws:
                    self._ws = ws
                    
                    # Subscribe to assets
                    await self._subscribe()
                    
                    # Listen for messages
                    async for message in ws:
                        await self._handle_message(message)
                        
            except Exception as e:
                if self.on_error:
                    await self.on_error(e)
                    
                if self._running:
                    await asyncio.sleep(self.RECONNECT_DELAY)
                    
    async def _subscribe(self):
        """Envoie les subscriptions."""
        # Split en chunks si > 250 assets
        for i in range(0, len(self.asset_ids), self.MAX_ASSETS_PER_CONNECTION):
            chunk = self.asset_ids[i:i + self.MAX_ASSETS_PER_CONNECTION]
            msg = {
                "type": "subscribe",
                "channel": "market",
                "assets_ids": chunk
            }
            await self._ws.send(json.dumps(msg))
            
    async def _handle_message(self, raw_message: str):
        """Parse et dispatch les messages."""
        try:
            msg = json.loads(raw_message)
            
            if msg.get("type") == "book":
                asset_id = msg["asset_id"]
                orderbook = {
                    "bids": [(float(o["price"]), float(o["size"])) for o in msg.get("bids", [])],
                    "asks": [(float(o["price"]), float(o["size"])) for o in msg.get("asks", [])]
                }
                await self.on_orderbook_update(asset_id, orderbook)
                
        except json.JSONDecodeError:
            pass  # Ignore malformed messages
            
    async def close(self):
        """Ferme la connexion."""
        self._running = False
        if self._ws:
            await self._ws.close()
```

### 8. Main Loop

```python
"""
MAIN LOOP - Orchestration

1. Au démarrage:
   - Vérifier geoblock (exit si bloqué)
   - Charger config
   - Fetch liste marchés actifs
   - Initialiser DB
   - Envoyer health check Discord

2. Loop principal:
   - Maintenir connexions WebSocket
   - Sur chaque update orderbook:
     - Mettre à jour état local
     - Lancer détection arbitrage
     - Si opportunité: alerter Discord + log DB
   
3. Tâches périodiques:
   - Health check: toutes les 5 min
   - Refresh liste marchés: toutes les 15 min
   - Daily summary: 08h00 UTC
   
4. Graceful shutdown:
   - SIGTERM/SIGINT handler
   - Fermer WebSocket proprement
   - Flush DB
   - Envoyer status Discord
"""

async def main():
    # 1. Startup checks
    if not await check_geoblock():
        logger.error("IP is geoblocked! Exiting.")
        await discord.send_ops("🔴 **CRITICAL**: IP geoblocked, bot cannot start")
        return
    
    # 2. Initialize
    config = load_config()
    db = await init_database(config.db_path)
    markets = await fetch_active_markets()
    
    await discord.send_health("🟢 Bot starting", {
        "markets": len(markets),
        "ip": await get_public_ip()
    })
    
    # 3. Setup WebSocket
    asset_ids = extract_all_token_ids(markets)
    orderbook_manager = OrderbookManager()
    detector = BinaryArbDetector(config)
    
    async def on_orderbook_update(asset_id: str, book: dict):
        orderbook_manager.update(asset_id, book)
        
        # Check for arbitrage on affected markets
        affected_markets = orderbook_manager.get_markets_by_asset(asset_id)
        for market in affected_markets:
            opp = detector.detect(market, orderbook_manager)
            if opp:
                await discord.send_opportunity(opp)
                await db.log_opportunity(opp)
    
    ws_client = PolymarketWebSocket(
        asset_ids=asset_ids,
        on_orderbook_update=on_orderbook_update,
        on_error=lambda e: discord.send_ops(f"⚠️ WebSocket error: {e}")
    )
    
    # 4. Start tasks
    await asyncio.gather(
        ws_client.connect(),
        periodic_health_check(discord, orderbook_manager),
        periodic_market_refresh(markets, ws_client),
        daily_summary_task(discord, db),
    )
```

---

## 🧪 TESTS À IMPLÉMENTER

```python
# tests/test_binary_arb.py

def test_detects_obvious_arbitrage():
    """Edge case: somme < 0.95 devrait déclencher."""
    yes_book = {"asks": [(0.45, 1000)], "bids": []}
    no_book = {"asks": [(0.48, 1000)], "bids": []}
    
    opp = detect_binary_arbitrage(
        market=MOCK_MARKET,
        yes_orderbook=yes_book,
        no_orderbook=no_book,
        target_size_usd=100
    )
    
    assert opp is not None
    assert opp.combined_cost == 0.93
    assert opp.gross_edge_percent > 7.0
    assert opp.verdict == "✅ ACTIONABLE"

def test_no_arbitrage_when_sum_above_one():
    """Pas d'arb si somme >= 1."""
    yes_book = {"asks": [(0.55, 1000)], "bids": []}
    no_book = {"asks": [(0.48, 1000)], "bids": []}
    
    opp = detect_binary_arbitrage(...)
    assert opp is None

def test_vwap_calculation_with_depth():
    """VWAP doit prendre en compte la profondeur."""
    asks = [(0.50, 50), (0.52, 100), (0.55, 200)]
    
    # Pour $100, on prend 50@0.50 + 50@0.52 = VWAP ~0.51
    vwap = calculate_vwap(asks, 100)
    assert 0.50 < vwap < 0.52

def test_insufficient_liquidity_returns_none():
    """Si pas assez de liquidité, retourner None."""
    asks = [(0.50, 10)]  # Seulement $10 dispo
    
    vwap = calculate_vwap(asks, 100)  # Demande $100
    assert vwap is None
```

---

## 🚀 COMMANDES DE LANCEMENT

```bash
# Développement
python -m src.main

# Production (via systemd)
sudo systemctl start polymarket-bot
sudo systemctl status polymarket-bot
sudo journalctl -u polymarket-bot -f

# Tests
pytest tests/ -v

# Scripts utilitaires
python scripts/check_geoblock.py    # Vérifie si IP bloquée
python scripts/test_discord.py      # Teste les webhooks
python scripts/list_markets.py      # Liste marchés actifs
```

---

## ⚠️ POINTS D'ATTENTION

1. **Geoblock** : Toujours vérifier au démarrage. Si bloqué → exit immédiat.

2. **Rate Limits** : 
   - REST API : ~10 req/sec max
   - WebSocket : Pas de limite connue mais être raisonnable

3. **Reconnexion WebSocket** : 
   - Implémenter exponential backoff
   - Max 5 tentatives puis alerte Discord

4. **Mémoire** : 
   - Garder seulement top 10 niveaux de chaque orderbook
   - Purger opportunités > 24h de la DB

5. **Timezone** : Tout en UTC

6. **Logging** : 
   - Niveau INFO par défaut
   - DEBUG pour troubleshooting
   - Rotation logs (max 100MB)

---

## 📅 ROADMAP PHASES

### Phase 1 (ACTUELLE) - Alerting Only
- [x] Scanner WebSocket
- [x] Détection arbitrage binaire
- [x] Alertes Discord
- [x] Logs SQLite
- [ ] Multi-outcome detection

### Phase 2 - Semi-Automated
- [ ] Confirmation via réaction Discord
- [ ] Exécution après validation humaine
- [ ] Paper trading parallel

### Phase 3 - Full Auto (optionnel)
- [ ] Exécution automatique si edge > seuil
- [ ] Risk management (position limits)
- [ ] Kill switch Discord

---

## 📞 CONTACTS & RESSOURCES

- **Polymarket Docs** : https://docs.polymarket.com
- **py-clob-client** : https://github.com/Polymarket/py-clob-client
- **Gamma API** : https://gamma-api.polymarket.com
- **Discord.py Webhooks** : https://discordpy.readthedocs.io/en/stable/api.html#webhook

---

**FIN DU CONTEXTE - CLAUDE CODE PEUT MAINTENANT IMPLÉMENTER LE PROJET**
