# SETUP WALLET - Guide Création Wallet Polymarket

> **Temps estimé** : 30-45 minutes
> **Prérequis** : Navigateur web, ~$10-20 en crypto pour les frais initiaux

---

## ⚠️ AVERTISSEMENT LÉGAL

Tu es résident fiscal français. Polymarket est officiellement bloqué en France depuis novembre 2024.

**Ce que ça implique :**
- Utiliser Polymarket via VPN/VPS viole leurs ToS
- Ton compte peut être placé en "close-only mode" si détecté
- Utilise uniquement du capital que tu peux perdre
- Pas de KYC = pas de lien direct avec ton identité

**Recommandations :**
- Wallet fresh (jamais utilisé sur exchanges KYC français)
- Petit capital (100€ max comme prévu)
- Jamais de retrait direct vers exchange français

---

## 🔐 Architecture Wallet Recommandée

```
┌─────────────────────────────────────────────────────────────┐
│                    TON SETUP OPTIMAL                        │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  [Wallet Perso]                     [Wallet Bot]            │
│  (Metamask/Rabby)                   (Fresh wallet)          │
│       │                                   │                 │
│       │ Funding initial                   │                 │
│       ▼                                   │                 │
│  ┌─────────┐     Bridge USDC         ┌─────────┐           │
│  │ Polygon │ ──────────────────────▶ │ Polygon │           │
│  │ (USDC)  │                         │ (USDC)  │           │
│  └─────────┘                         └─────────┘           │
│                                           │                 │
│                                           ▼                 │
│                                    ┌─────────────┐          │
│                                    │ Polymarket  │          │
│                                    │ Proxy Wallet│          │
│                                    └─────────────┘          │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**Pourquoi 2 wallets ?**
- Séparation des fonds (sécurité)
- Wallet bot = jetable si problème
- Pas de lien avec tes autres activités crypto

---

## 1️⃣ Créer un Nouveau Wallet (Fresh)

### Option A : Rabby Wallet (Recommandé)

1. Installe [Rabby Wallet](https://rabby.io/) (extension Chrome/Firefox)
2. **Create New Address** → Note ta **seed phrase** (12/24 mots)
3. **IMPORTANT** : Sauvegarde la seed phrase offline (papier, jamais digital)

### Option B : Metamask

1. Installe [Metamask](https://metamask.io/)
2. **Create a new wallet**
3. Sauvegarde la seed phrase

### Exporter la Private Key

Tu auras besoin de la **private key** pour le bot (Phase 2).

**Rabby** : Settings → Manage Address → Export Private Key
**Metamask** : Account Details → Export Private Key

Format : `0x...` (64 caractères hex après le 0x)

---

## 2️⃣ Ajouter le Réseau Polygon

Polymarket fonctionne sur **Polygon (MATIC)**.

### Ajout automatique

Va sur [Chainlist.org](https://chainlist.org/chain/137) et clique "Add to Wallet"

### Ajout manuel

| Paramètre | Valeur |
|-----------|--------|
| Network Name | Polygon Mainnet |
| RPC URL | `https://polygon-rpc.com` |
| Chain ID | 137 |
| Symbol | MATIC |
| Block Explorer | `https://polygonscan.com` |

---

## 3️⃣ Obtenir du MATIC (Gas Fees)

Tu as besoin d'un peu de MATIC pour payer les frais de transaction sur Polygon.

**Montant recommandé** : 2-5 MATIC (~$1-2)

### Option A : Bridge depuis Ethereum (si tu as déjà de l'ETH)

1. Va sur [Polygon Bridge](https://wallet.polygon.technology/bridge)
2. Connecte ton wallet
3. Bridge ETH → MATIC

### Option B : Acheter directement sur un exchange

1. Acheter MATIC sur Binance/Kraken/etc.
2. Retirer vers ton wallet Polygon
   - **Réseau** : Polygon (pas ERC20 !)
   - **Adresse** : Ton adresse wallet

### Option C : Faucet (gratuit mais limité)

- [Polygon Faucet](https://faucet.polygon.technology/) - 0.001 MATIC
- Pas suffisant pour trading, juste pour tests

---

## 4️⃣ Obtenir du USDC sur Polygon

Polymarket utilise **USDC** (sur Polygon) comme monnaie.

**Montant** : 100€ ≈ $105-110 USDC

### Option A : Bridge USDC depuis Ethereum

1. Va sur [Polygon Bridge](https://wallet.polygon.technology/bridge)
2. Sélectionne USDC
3. Bridge vers Polygon

### Option B : Acheter USDC et retirer sur Polygon

1. Acheter USDC sur Binance/Kraken
2. Retirer vers ton wallet
   - **Réseau** : Polygon
   - **Adresse** : Ton adresse wallet

### Option C : Swap sur DEX

1. Va sur [QuickSwap](https://quickswap.exchange/) ou [Uniswap](https://app.uniswap.org/)
2. Connecte ton wallet (Polygon network)
3. Swap MATIC → USDC

### Vérifier ton solde USDC

- Adresse USDC sur Polygon : `0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359`
- Vérifie sur [Polygonscan](https://polygonscan.com/address/TON_ADRESSE)

---

## 5️⃣ Connecter à Polymarket (via VPS)

> **IMPORTANT** : Fais cette étape depuis ta VM Amsterdam, PAS depuis la France.

### SSH vers ta VM

```bash
ssh root@<IP_VM>
```

### Option A : Utiliser le script de génération de credentials

Le bot inclut un script pour générer les API credentials :

```bash
cd /opt/polymarket-bot
source venv/bin/activate
python scripts/generate_api_creds.py
```

Ce script va :
1. Vérifier que l'IP n'est pas bloquée
2. Créer ou dériver les API credentials
3. Afficher les valeurs à mettre dans `.env`

### Option B : Via l'interface web Polymarket

1. Depuis ta VM, installe un navigateur GUI (optionnel, plus complexe)
2. Ou utilise un tunnel SSH avec forwarding

```bash
# Depuis ton PC local
ssh -D 8080 root@<IP_VM>
# Configure ton navigateur pour utiliser SOCKS proxy localhost:8080
```

3. Va sur [polymarket.com](https://polymarket.com)
4. Connecte ton wallet (Rabby/Metamask)
5. Dépose des fonds via le bouton "Deposit"

---

## 6️⃣ Comprendre le Proxy Wallet Polymarket

Quand tu te connectes à Polymarket, ils créent un **Proxy Wallet** pour toi.

```
[Ton Wallet]          [Proxy Wallet]           [Polymarket]
     │                      │                       │
     │── Approve USDC ──────│                       │
     │                      │                       │
     │── Deposit ──────────▶│                       │
     │                      │── Trade ─────────────▶│
     │                      │◀── Payout ───────────│
     │◀── Withdraw ────────│                       │
```

**Pourquoi ?**
- Gasless trading (Polymarket paie le gas)
- Signature simplifiée
- Tu gardes le contrôle de tes fonds

### Trouver ton Proxy Wallet

1. Va sur [reveal.polymarket.com](https://reveal.polymarket.com) (depuis VM)
2. Connecte ton wallet
3. Note l'adresse du Proxy Wallet : `0x...`

---

## 7️⃣ Configurer le Bot (Phase 2 - Trading)

> **Note** : Phase 1 (alerting) ne nécessite pas ces credentials.
> Configure-les maintenant pour être prêt pour Phase 2.

### Générer les API Credentials

```python
# scripts/generate_api_creds.py
from py_clob_client.client import ClobClient

# Ta private key (JAMAIS commit dans git !)
PRIVATE_KEY = "0x..."  # Depuis ton wallet

client = ClobClient(
    host="https://clob.polymarket.com",
    key=PRIVATE_KEY,
    chain_id=137
)

# Créer ou dériver les credentials
creds = client.create_or_derive_api_creds()

print("Add these to your .env file:")
print(f"POLY_API_KEY={creds.api_key}")
print(f"POLY_API_SECRET={creds.api_secret}")
print(f"POLY_API_PASSPHRASE={creds.api_passphrase}")
```

### Mettre à jour .env

```env
# === POLYMARKET CREDENTIALS (Phase 2) ===
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_PROXY_WALLET=0x...
POLY_API_KEY=...
POLY_API_SECRET=...
POLY_API_PASSPHRASE=...
```

---

## 8️⃣ Approuver les Contrats (One-time)

Avant de pouvoir trader, tu dois approuver Polymarket à utiliser tes USDC.

### Via le site Polymarket

1. Connecte-toi sur polymarket.com (depuis VM)
2. Clique sur "Deposit"
3. Approuve la transaction dans ton wallet
4. Dépose tes USDC

### Via script Python

```python
# scripts/approve_contracts.py
from py_clob_client.client import ClobClient

client = ClobClient(
    host="https://clob.polymarket.com",
    key="0x...",  # Ta private key
    chain_id=137,
    signature_type=1,  # 1 pour email/Magic, 0 pour EOA
    funder="0x..."  # Ton proxy wallet
)

# Approuver USDC
client.set_allowances()
print("✅ Allowances set!")
```

---

## 9️⃣ Vérifier la Configuration

### Test de connexion API

```bash
cd /opt/polymarket-bot
source venv/bin/activate
python -c "
from py_clob_client.client import ClobClient

client = ClobClient('https://clob.polymarket.com')
print('Server OK:', client.get_ok())
print('Server time:', client.get_server_time())
"
```

### Test de balance (Phase 2)

```python
# Nécessite credentials configurés
client = ClobClient(
    host="https://clob.polymarket.com",
    key=PRIVATE_KEY,
    chain_id=137,
    creds=api_creds,
    signature_type=1,
    funder=PROXY_WALLET
)

balance = client.get_balance()
print(f"Balance: ${balance}")
```

---

## 🔒 Sécurité des Credentials

### ❌ NE JAMAIS FAIRE

- Commit la private key dans Git
- Partager les credentials par Discord/Email
- Utiliser la même seed que ton wallet principal
- Stocker la seed phrase en digital (photo, notes, cloud)

### ✅ BONNES PRATIQUES

- `.env` dans `.gitignore`
- Seed phrase sur papier, lieu sûr
- Private key uniquement sur la VM
- Permissions restrictives : `chmod 600 .env`

### Exemple .gitignore

```gitignore
# Secrets
.env
*.pem
*_secret*
*_private*

# Local data
data/
logs/
*.db

# Python
__pycache__/
*.pyc
venv/
```

---

## 💰 Récapitulatif des Coûts

| Élément | Coût estimé |
|---------|-------------|
| MATIC (gas) | ~$2 |
| USDC (trading) | $100 (ton budget) |
| Bridge fees | ~$1-5 |
| **Total initial** | **~$105-110** |

---

## ❓ FAQ

### Q: Puis-je utiliser mon wallet Binance/Kraken directement ?
**R** : Non, tu dois utiliser un wallet non-custodial (Metamask, Rabby). Les exchanges centralisés ne supportent pas les interactions avec Polymarket.

### Q: Et si mon compte est flaggé ?
**R** : Polymarket peut mettre ton compte en "close-only mode". Tu pourras retirer tes fonds mais plus trader. C'est pourquoi on utilise un petit capital.

### Q: Comment retirer mes gains ?
**R** : Via Polymarket (Withdraw vers ton wallet Polygon), puis bridge vers Ethereum si besoin, puis vers un exchange. Évite les retraits directs vers exchanges français pour minimiser les traces.

### Q: Faut-il faire du KYC ?
**R** : Polymarket n'a pas de KYC obligatoire actuellement. C'est un avantage pour la privacy mais aussi un risque réglementaire.

---

## ✅ Checklist Wallet

- [ ] Nouveau wallet créé (seed phrase sauvegardée)
- [ ] Private key exportée (stockée de manière sécurisée)
- [ ] Polygon network ajouté
- [ ] MATIC obtenu (~2-5 MATIC)
- [ ] USDC obtenu (~$100)
- [ ] Connecté à Polymarket (depuis VM Amsterdam)
- [ ] Proxy wallet identifié
- [ ] API credentials générés
- [ ] .env configuré avec credentials
- [ ] Allowances approuvées

---

**🎉 Ton wallet est prêt pour Polymarket !**
