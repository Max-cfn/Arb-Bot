//! EIP-712 signing and HTTP order submission to the Polymarket CLOB.
//!
//! Builds two CLOB orders (YES buy + NO buy), signs both via EIP-712,
//! and POSTs them in parallel for minimum latency.
//!
//! Uses k256 directly for ECDSA signing (avoids alloy-signer-local and its
//! broken alloy-consensus transitive dependency chain).

use std::str::FromStr;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use alloy_primitives::{Address, B256, U256};
use alloy_sol_types::{eip712_domain, sol, SolStruct};
use anyhow::{anyhow, Context, Result};
use base64::Engine as _;
use hmac::{Hmac, Mac};
use k256::ecdsa::{SigningKey, signature::hazmat::PrehashSigner};
use reqwest::Client;
use sha2::Sha256;
use tracing::{error, info};

use crate::cache::MetaCache;
use crate::types::{EngineConfig, RustArbOpportunity, RustExecutionResult};

// ---------------------------------------------------------------------------
// Polymarket CTF Exchange addresses (Polygon mainnet)
// ---------------------------------------------------------------------------

const EXCHANGE_ADDRESS: &str = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E";
const NEG_RISK_EXCHANGE_ADDRESS: &str = "0xC5d563A36AE78145C45a50134d48A1215220f80a";

// ---------------------------------------------------------------------------
// EIP-712 typed data definition (via alloy sol! macro)
// ---------------------------------------------------------------------------

sol! {
    #[derive(Debug)]
    struct Order {
        uint256 salt;
        address maker;
        address signer;
        address taker;
        uint256 tokenId;
        uint256 makerAmount;
        uint256 takerAmount;
        uint256 expiration;
        uint256 nonce;
        uint256 feeRateBps;
        uint8 side;
        uint8 signatureType;
    }
}

// ---------------------------------------------------------------------------
// FastExecutor
// ---------------------------------------------------------------------------

pub struct FastExecutor {
    signing_key: SigningKey,
    signer_address: Address,
    http: Client,
    funder_address: String,
    api_key: String,
    api_secret: String,
    api_passphrase: String,
    chain_id: u64,
    signature_type: u32,
    clob_base_url: String,
}

impl FastExecutor {
    pub fn new(config: &EngineConfig) -> Result<Self> {
        let key_hex = config.private_key.strip_prefix("0x")
            .unwrap_or(&config.private_key);
        let key_bytes = hex::decode(key_hex)
            .context("Failed to hex-decode private key")?;

        let signing_key = SigningKey::from_slice(&key_bytes)
            .context("Failed to parse private key as secp256k1")?;

        // Derive signer address from public key (keccak256 of uncompressed pubkey)
        let verifying_key = signing_key.verifying_key();
        let pubkey_bytes = verifying_key.to_encoded_point(false);
        // Skip the 0x04 prefix byte, take last 64 bytes
        let pubkey_uncompressed = &pubkey_bytes.as_bytes()[1..];
        let hash = alloy_primitives::keccak256(pubkey_uncompressed);
        let signer_address = Address::from_slice(&hash[12..]);

        let http = Client::builder()
            .pool_max_idle_per_host(10)
            .tcp_keepalive(Some(std::time::Duration::from_secs(30)))
            .timeout(std::time::Duration::from_secs(10))
            .build()
            .context("Failed to build HTTP client")?;

        info!(
            "executor: initialized signer_address={}",
            signer_address,
        );

        Ok(Self {
            signing_key,
            signer_address,
            http,
            funder_address: config.funder_address.clone(),
            api_key: config.api_key.clone(),
            api_secret: config.api_secret.clone(),
            api_passphrase: config.api_passphrase.clone(),
            chain_id: config.chain_id,
            signature_type: config.signature_type,
            clob_base_url: config.clob_base_url.trim_end_matches('/').to_string(),
        })
    }

    /// Execute a two-leg binary arb: sign + POST both orders in parallel.
    pub async fn execute(
        &self,
        opp: &RustArbOpportunity,
        config: &EngineConfig,
        meta_cache: &MetaCache,
    ) -> RustExecutionResult {
        let t_start = Instant::now();
        let run_id = format!("R{:x}", rand_u64());

        // Determine tick_size and neg_risk from cache
        let yes_meta = meta_cache.get(&opp.yes_token_id);
        let no_meta = meta_cache.get(&opp.no_token_id);
        let yes_neg_risk = yes_meta.as_ref().map(|m| m.neg_risk).unwrap_or(false);
        let no_neg_risk = no_meta.as_ref().map(|m| m.neg_risk).unwrap_or(false);
        let yes_tick = yes_meta.as_ref().map(|m| m.tick_size.as_str()).unwrap_or("0.01");
        let no_tick = no_meta.as_ref().map(|m| m.tick_size.as_str()).unwrap_or("0.01");

        // Compute aggressive limit prices (cross above best ask)
        let yes_limit = aggressive_buy_limit(opp.yes_best_ask, config.cross_bps);
        let no_limit = aggressive_buy_limit(opp.no_best_ask, config.cross_bps);

        // Compute share sizes
        let cheaper_price = yes_limit.min(no_limit);
        let shares_for_min_notional = if cheaper_price > 0.0 {
            (config.min_order_usd / cheaper_price).ceil() as u64
        } else {
            0
        };
        let shares_floor = config.min_order_shares.ceil() as u64;
        let shares = shares_for_min_notional.max(shares_floor);

        // Round prices to tick_size
        let yes_price_rounded = round_to_tick(yes_limit, yes_tick);
        let no_price_rounded = round_to_tick(no_limit, no_tick);

        // Build EIP-712 orders
        let yes_order = self.build_order(
            &opp.yes_token_id,
            yes_price_rounded,
            shares as f64,
        );
        let no_order = self.build_order(
            &opp.no_token_id,
            no_price_rounded,
            shares as f64,
        );

        // Sign both (synchronous ECDSA, very fast — <1ms each)
        let t_sign_start = Instant::now();
        let yes_sig_result = self.sign_order(&yes_order, yes_neg_risk);
        let no_sig_result = self.sign_order(&no_order, no_neg_risk);
        let sign_ms = t_sign_start.elapsed().as_secs_f64() * 1000.0;

        let yes_sig = match yes_sig_result {
            Ok(s) => s,
            Err(e) => {
                return make_error_result(&run_id, &format!("YES sign failed: {e}"), sign_ms);
            }
        };
        let no_sig = match no_sig_result {
            Ok(s) => s,
            Err(e) => {
                return make_error_result(&run_id, &format!("NO sign failed: {e}"), sign_ms);
            }
        };

        // Build POST bodies
        let yes_body = self.build_post_body(&yes_order, &yes_sig);
        let no_body = self.build_post_body(&no_order, &no_sig);

        // POST both in parallel
        let t_submit_start = Instant::now();
        let (yes_resp, no_resp) = tokio::join!(
            self.post_order(&yes_body),
            self.post_order(&no_body),
        );
        let submit_ms = t_submit_start.elapsed().as_secs_f64() * 1000.0;
        let total_ms = t_start.elapsed().as_secs_f64() * 1000.0;

        // Extract order IDs from responses
        let yes_order_id = match &yes_resp {
            Ok(v) => v.get("orderID")
                .or_else(|| v.get("id"))
                .and_then(|v| v.as_str())
                .map(|s| s.to_string()),
            Err(e) => {
                error!("YES POST failed: {}", e);
                None
            }
        };
        let no_order_id = match &no_resp {
            Ok(v) => v.get("orderID")
                .or_else(|| v.get("id"))
                .and_then(|v| v.as_str())
                .map(|s| s.to_string()),
            Err(e) => {
                error!("NO POST failed: {}", e);
                None
            }
        };

        let status = if yes_order_id.is_some() && no_order_id.is_some() {
            "SUBMITTED"
        } else if yes_order_id.is_some() || no_order_id.is_some() {
            "PARTIAL_SUBMIT"
        } else {
            "FAILED"
        };

        let reason = format!(
            "yes={} no={} sign={:.1}ms submit={:.1}ms total={:.1}ms",
            yes_order_id.as_deref().unwrap_or("NONE"),
            no_order_id.as_deref().unwrap_or("NONE"),
            sign_ms,
            submit_ms,
            total_ms,
        );

        info!(
            "executor: {} run={} {}",
            status, run_id, reason
        );

        RustExecutionResult {
            status: status.to_string(),
            run_id,
            yes_order_id,
            no_order_id,
            reason,
            reason_code: None,
            yes_filled_size: 0.0,
            no_filled_size: 0.0,
            sign_ms,
            submit_ms,
            total_ms,
        }
    }

    // -----------------------------------------------------------------------
    // Order construction
    // -----------------------------------------------------------------------

    fn build_order(
        &self,
        token_id: &str,
        price: f64,
        size: f64,
    ) -> Order {
        // Polymarket uses 6 decimals (USDC): 1 USDC = 1_000_000
        // makerAmount = size * price * 1e6 (USDC you pay)
        // takerAmount = size * 1e6           (shares you receive)
        let maker_amount = (size * price * 1_000_000.0) as u128;
        let taker_amount = (size * 1_000_000.0) as u128;

        let salt: u128 = rand_u128();

        let maker_addr = Address::from_str(&self.funder_address)
            .unwrap_or_default();

        let token_id_u256 = U256::from_str(token_id).unwrap_or_default();

        Order {
            salt: U256::from(salt),
            maker: maker_addr,
            signer: self.signer_address,
            taker: Address::ZERO,
            tokenId: token_id_u256,
            makerAmount: U256::from(maker_amount),
            takerAmount: U256::from(taker_amount),
            expiration: U256::ZERO, // No expiration
            nonce: U256::ZERO,
            feeRateBps: U256::ZERO,
            side: 0, // BUY = 0
            signatureType: self.signature_type as u8,
        }
    }

    // -----------------------------------------------------------------------
    // EIP-712 signing (using k256 directly)
    // -----------------------------------------------------------------------

    fn sign_order(&self, order: &Order, neg_risk: bool) -> Result<String> {
        let exchange_addr = if neg_risk {
            Address::from_str(NEG_RISK_EXCHANGE_ADDRESS)?
        } else {
            Address::from_str(EXCHANGE_ADDRESS)?
        };

        let domain = eip712_domain! {
            name: "ClobExchange",
            version: "1",
            chain_id: self.chain_id,
            verifying_contract: exchange_addr,
        };

        // Compute EIP-712 signing hash
        let signing_hash: B256 = order.eip712_signing_hash(&domain);

        // Sign the hash with k256 ECDSA (secp256k1)
        let (sig, recid) = self.signing_key
            .sign_prehash(signing_hash.as_ref())
            .map_err(|e| anyhow!("ECDSA sign failed: {e}"))?;

        // Build 65-byte Ethereum signature: r (32) + s (32) + v (1)
        let mut sig_bytes = [0u8; 65];
        sig_bytes[..64].copy_from_slice(&sig.to_bytes());
        sig_bytes[64] = recid.to_byte() + 27; // Ethereum v = recid + 27

        Ok(format!("0x{}", hex::encode(sig_bytes)))
    }

    // -----------------------------------------------------------------------
    // POST body construction
    // -----------------------------------------------------------------------

    fn build_post_body(
        &self,
        order: &Order,
        signature: &str,
    ) -> serde_json::Value {
        serde_json::json!({
            "order": {
                "salt": order.salt.to_string(),
                "maker": format!("{:?}", order.maker),
                "signer": format!("{:?}", order.signer),
                "taker": format!("{:?}", order.taker),
                "tokenId": order.tokenId.to_string(),
                "makerAmount": order.makerAmount.to_string(),
                "takerAmount": order.takerAmount.to_string(),
                "expiration": order.expiration.to_string(),
                "nonce": order.nonce.to_string(),
                "feeRateBps": order.feeRateBps.to_string(),
                "side": order.side.to_string(),
                "signatureType": order.signatureType.to_string(),
                "signature": signature,
            },
            "owner": self.funder_address,
            "orderType": "GTC",
        })
    }

    // -----------------------------------------------------------------------
    // HTTP submission with HMAC auth
    // -----------------------------------------------------------------------

    async fn post_order(&self, body: &serde_json::Value) -> Result<serde_json::Value> {
        let path = "/order";
        let url = format!("{}{}", self.clob_base_url, path);
        let body_str = serde_json::to_string(body)?;

        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs()
            .to_string();

        let hmac_sig = self.compute_hmac(&timestamp, "POST", path, &body_str)?;

        let resp = self
            .http
            .post(&url)
            .header("Content-Type", "application/json")
            .header("POLY-ADDRESS", &self.funder_address)
            .header("POLY-SIGNATURE", &hmac_sig)
            .header("POLY-TIMESTAMP", &timestamp)
            .header("POLY-API-KEY", &self.api_key)
            .header("POLY-PASSPHRASE", &self.api_passphrase)
            .body(body_str)
            .send()
            .await
            .context("HTTP POST failed")?;

        let status = resp.status();
        let resp_text = resp.text().await.unwrap_or_default();

        if !status.is_success() {
            return Err(anyhow!(
                "CLOB POST returned {}: {}",
                status,
                &resp_text[..resp_text.len().min(500)]
            ));
        }

        serde_json::from_str(&resp_text)
            .context("Failed to parse CLOB response JSON")
    }

    /// Compute HMAC-SHA256 L2 auth signature.
    /// Message format: timestamp + "\n" + method + "\n" + path + "\n" + body
    fn compute_hmac(
        &self,
        timestamp: &str,
        method: &str,
        path: &str,
        body: &str,
    ) -> Result<String> {
        let secret_bytes = base64::engine::general_purpose::STANDARD
            .decode(&self.api_secret)
            .context("Failed to base64-decode api_secret")?;

        let message = format!("{}\n{}\n{}\n{}", timestamp, method, path, body);

        let mut mac = Hmac::<Sha256>::new_from_slice(&secret_bytes)
            .map_err(|e| anyhow!("HMAC key error: {e}"))?;
        mac.update(message.as_bytes());
        let result = mac.finalize().into_bytes();

        Ok(base64::engine::general_purpose::STANDARD.encode(result))
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn aggressive_buy_limit(best_ask: f64, cross_bps: f64) -> f64 {
    if best_ask <= 0.0 {
        return best_ask;
    }
    let bumped = best_ask * (1.0 + cross_bps / 10_000.0);
    bumped.min(0.9999).max(0.0001)
}

fn round_to_tick(price: f64, tick_size: &str) -> f64 {
    let tick: f64 = tick_size.parse().unwrap_or(0.01);
    if tick <= 0.0 {
        return price;
    }
    (price / tick).round() * tick
}

fn make_error_result(run_id: &str, reason: &str, sign_ms: f64) -> RustExecutionResult {
    RustExecutionResult {
        status: "FAILED".to_string(),
        run_id: run_id.to_string(),
        yes_order_id: None,
        no_order_id: None,
        reason: reason.to_string(),
        reason_code: Some("SIGN_ERROR".to_string()),
        yes_filled_size: 0.0,
        no_filled_size: 0.0,
        sign_ms,
        submit_ms: 0.0,
        total_ms: sign_ms,
    }
}

/// Simple random u64 for run IDs (no need for cryptographic quality).
fn rand_u64() -> u64 {
    let t = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    t.as_nanos() as u64 ^ (t.subsec_nanos() as u64).wrapping_mul(0x517cc1b727220a95)
}

/// Simple random u128 for order salts.
fn rand_u128() -> u128 {
    let t = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    t.as_nanos() ^ ((t.subsec_nanos() as u128).wrapping_mul(0x6c62272e07bb014262b821756295c58d))
}
