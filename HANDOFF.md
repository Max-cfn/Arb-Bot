# HANDOFF — Polymarket Arb Bot (Rust Engine)
**Date:** 2026-02-23 | **Branch:** develop | **Statut:** production actif

## 1. ÉTAT SYSTÈME
- Service: polymarket-bot.service (systemd)
- Stats 9h: 340 opps, stale 36%, ws 115M msg
- Erreur principale: `not enough balance` (funding manquant, pas un bug)
- Signing: 0.08-0.30ms | HTTP: 27-100ms | Total: ~97ms

## 2. FICHIERS RUST (rust_engine/src/)
- types.rs (249L): EngineConfig, RustArbOpportunity, RustExecutionResult
- detector.rs (238L): VWAP, fees, detect_binary_arb()
- orderbook.rs (178L): OrderbookManager (DashMap, lock-free)
- cache.rs (124L): MetaCache (REST token metadata)
- ws_client.rs (332L): WS streaming, 250 assets/connexion max
- executor.rs (1654L): EIP-712 k256, HMAC auth, 3-phase exec
- lib.rs (522L): PyO3 HotPathEngine, callbacks Python

## 3. DETECTION
Condition: yes_vwap + no_vwap < 1.0 apres fees + buffer
Freshness: <8s standard / <2s crypto15min
Verdicts: ACTIONABLE(>=2%), MARGINAL(>=1%), SKIP(>=0.5%)
Fees standard: resolution 2% sur payout
Fees crypto15: +trading = 0.02*p*(1-p)*size par jambe
Buffer: 0.5%(liquid) / 1.0%(illiquid)

## 4. EXECUTION (3 phases)
Phase1: Sign+POST YES/NO en parallele (tokio::join) ~50ms
Phase2: Poll fills 50ms intervals pendant 200ms max
Phase3: Recovery = cancel open + unwind jambe remplie
Etats: SUCCESS|PARTIAL_SUBMIT|PARTIAL_FILL|ABORTED|SKIP_BALANCE|SKIP_COOLDOWN

## 5. CONFIG .env CRITIQUE
CLOB_FUNDER_ADDRESS = proxy Gnosis Safe (PAS l'EOA)
CLOB_SIGNATURE_TYPE = 2 (GNOSIS_SAFE pour Rabby)
CLOB_API_SECRET = base64 standard (+ et /)
TARGET_SIZE_USD=100 | MIN_EDGE_PERCENT=0.5
MAX_EDGE_DECAY_BPS=25 | CROSS_BPS=5
EXEC_COOLDOWN_MS=15000 | WAIT_FOR_BOTH_MS=500
UNWIND_MAX_LOSS_PCT=3.0 | PANIC_PARTIAL_COUNT=3

## 6. BUGS RESOLUS
- 401 Invalid signature: FUNDER=proxy Gnosis Safe
- API_SECRET decode: base64 standard (pas URL-safe)

## 6b. EN COURS
- not enough balance: alimenter proxy wallet USDC Polygon
- stale 36%: envisager gate sur max(snapshot_age, delta_age)
- opp counter stagne a 340: verifier fenetre glissante vs bug

## 7. PRIORITES
P0: Alimenter proxy wallet USDC -> deverrouille execution
P1: Freshness gate amelioree (max snapshot/delta age)
P1: min_liquidity_usd 100->50 pour +opps
P2: Log edge_detect vs edge_exec (mesurer decay reel)
P3: Colocation US East pour -latence HTTP (objectif <50ms)

## 8. COMMANDES
# Logs live
journalctl -u polymarket-bot -f | grep -E 'EXEC|SUCCESS|FAIL|STATS'
# Restart
sudo systemctl restart polymarket-bot
# Recompile
cd /opt/polymarket-bot/Arb-Bot/rust_engine && cargo build --release

## 9. ADRESSES CRITIQUES
CTF Exchange (Polygon): 0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E
NegRisk Exchange: 0xC5d563A36AE78145C45a50134d48A1215220f80a
Chain ID: 137 (Polygon mainnet)
Deps Rust: tokio, pyo3, k256, alloy-primitives, alloy-sol-types, reqwest, dashmap
