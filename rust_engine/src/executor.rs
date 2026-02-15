//! EIP-712 signing and HTTP order submission to the Polymarket CLOB.
//!
//! Three-phase execution with complete-or-abort safety:
//!   Phase 1 — Sign + POST both legs in parallel (hot path, ~50ms)
//!   Phase 2 — Poll for fills (up to 500ms)
//!   Phase 3 — Recovery: cancel open → market-unwind any one-sided fill
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
use tokio::time::{sleep, Duration};
use tracing::{error, info, warn};

use crate::cache::MetaCache;
use crate::types::{EngineConfig, RustArbOpportunity, RustExecutionResult};

// ---------------------------------------------------------------------------
// Polymarket CTF Exchange addresses (Polygon mainnet)
// ---------------------------------------------------------------------------

const EXCHANGE_ADDRESS: &str = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E";
const NEG_RISK_EXCHANGE_ADDRESS: &str = "0xC5d563A36AE78145C45a50134d48A1215220f80a";

const BUY: u8 = 0;
const SELL: u8 = 1;

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

    // =====================================================================
    // Main execution: 3-phase complete-or-abort
    // =====================================================================

    pub async fn execute(
        &self,
        opp: &RustArbOpportunity,
        config: &EngineConfig,
        meta_cache: &MetaCache,
    ) -> RustExecutionResult {
        let t_start = Instant::now();
        let run_id = format!("R{:x}", rand_u64());
        let detect_ns = opp.t_detect_ns;
        let timing_since_detect = |label: &str| {
            if detect_ns > 0 {
                if let Some(ms) = delta_ms_from_unix_ns(detect_ns) {
                    return format!(" {}={:.5}ms", label, ms);
                }
            }
            String::new()
        };

        // -----------------------------------------------------------------
        // Resolve market metadata
        // -----------------------------------------------------------------
        let yes_meta = meta_cache.get(&opp.yes_token_id);
        let no_meta = meta_cache.get(&opp.no_token_id);
        let yes_neg_risk = yes_meta.as_ref().map(|m| m.neg_risk).unwrap_or(false);
        let no_neg_risk = no_meta.as_ref().map(|m| m.neg_risk).unwrap_or(false);
        let yes_tick = yes_meta.as_ref().map(|m| m.tick_size.as_str()).unwrap_or("0.01");
        let no_tick = no_meta.as_ref().map(|m| m.tick_size.as_str()).unwrap_or("0.01");
        let yes_fee_bps = yes_meta.as_ref().map(|m| m.fee_rate_bps).unwrap_or(0);
        let no_fee_bps = no_meta.as_ref().map(|m| m.fee_rate_bps).unwrap_or(0);

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
        let yes_price = round_to_tick(yes_limit, yes_tick);
        let no_price = round_to_tick(no_limit, no_tick);

        // =================================================================
        // PHASE 1: Build → Sign → POST  (hot path)
        // =================================================================

        let yes_order = self.build_order(&opp.yes_token_id, yes_price, shares as f64, yes_fee_bps, BUY);
        let no_order = self.build_order(&opp.no_token_id, no_price, shares as f64, no_fee_bps, BUY);

        let t_sign_start = Instant::now();
        let yes_sig = match self.sign_order(&yes_order, yes_neg_risk) {
            Ok(s) => s,
            Err(e) => {
                return make_error_result(&run_id, &opp.market_id, &format!("YES sign failed: {e}"), t_sign_start.elapsed().as_secs_f64() * 1000.0);
            }
        };
        let no_sig = match self.sign_order(&no_order, no_neg_risk) {
            Ok(s) => s,
            Err(e) => {
                return make_error_result(&run_id, &opp.market_id, &format!("NO sign failed: {e}"), t_sign_start.elapsed().as_secs_f64() * 1000.0);
            }
        };
        let sign_ms = t_sign_start.elapsed().as_secs_f64() * 1000.0;

        let yes_body = self.build_post_body(&yes_order, &yes_sig, "FOK");
        let no_body = self.build_post_body(&no_order, &no_sig, "FOK");

        let t_submit = Instant::now();
        let (yes_resp, no_resp) = tokio::join!(
            self.post_order(&yes_body),
            self.post_order(&no_body),
        );
        let submit_ms = t_submit.elapsed().as_secs_f64() * 1000.0;

        let yes_order_id = match &yes_resp {
            Ok(v) => extract_order_id(v),
            Err(e) => { error!("YES POST failed: {}", e); None }
        };
        let no_order_id = match &no_resp {
            Ok(v) => extract_order_id(v),
            Err(e) => { error!("NO POST failed: {}", e); None }
        };

        // ----- Both POSTs failed -----
        if yes_order_id.is_none() && no_order_id.is_none() {
            let total_ms = t_start.elapsed().as_secs_f64() * 1000.0;
            return RustExecutionResult {
                status: "FAILED".into(),
                run_id,
                market_id: opp.market_id.clone(),
                yes_order_id: None,
                no_order_id: None,
                reason: format!(
                    "both POSTs failed yes_err={} no_err={}{}{}",
                    yes_resp.err().map(|e| e.to_string()).unwrap_or_default(),
                    no_resp.err().map(|e| e.to_string()).unwrap_or_default(),
                    timing_since_detect("detect_to_sign"),
                    timing_since_detect("detect_to_submit"),
                ),
                reason_code: Some("POST_FAILED".into()),
                yes_filled_size: 0.0,
                no_filled_size: 0.0,
                yes_target_price: yes_price,
                no_target_price: no_price,
                yes_final_price: 0.0,
                no_final_price: 0.0,
                sign_ms,
                submit_ms,
                total_ms,
            };
        }

        // ----- One POST failed: cancel the surviving order immediately -----
        if yes_order_id.is_none() || no_order_id.is_none() {
            let surviving = yes_order_id.as_deref().or(no_order_id.as_deref()).unwrap();
            info!("executor: partial POST, cancelling surviving order {}", surviving);
            let _ = self.cancel_orders(&[surviving]).await;
            let total_ms = t_start.elapsed().as_secs_f64() * 1000.0;
            return RustExecutionResult {
                status: "CANCELLED".into(),
                run_id,
                market_id: opp.market_id.clone(),
                yes_order_id: yes_order_id.clone(),
                no_order_id: no_order_id.clone(),
                reason: format!(
                    "one POST failed, cancelled surviving order{}{}",
                    timing_since_detect("detect_to_sign"),
                    timing_since_detect("detect_to_submit"),
                ),
                reason_code: Some("PARTIAL_POST_CANCELLED".into()),
                yes_filled_size: 0.0,
                no_filled_size: 0.0,
                yes_target_price: yes_price,
                no_target_price: no_price,
                yes_final_price: 0.0,
                no_final_price: 0.0,
                sign_ms,
                submit_ms,
                total_ms,
            };
        }

        // From here: both order IDs are present
        let yes_oid = yes_order_id.as_ref().unwrap().clone();
        let no_oid = no_order_id.as_ref().unwrap().clone();

        // =================================================================
        // PHASE 2: Wait for both fills  (up to wait_for_both_ms)
        // =================================================================

        let poll_interval = Duration::from_millis(config.poll_interval_ms);
        let wait_deadline = Instant::now() + Duration::from_millis(config.wait_for_both_ms);

        let mut yes_filled = 0.0_f64;
        let mut no_filled = 0.0_f64;
        let mut yes_final_price = 0.0_f64;
        let mut no_final_price = 0.0_f64;
        let mut yes_done = false;
        let mut no_done = false;

        while Instant::now() < wait_deadline {
            let (ys, ns) = tokio::join!(
                self.get_order_status(&yes_oid),
                self.get_order_status(&no_oid),
            );
            if let Some((st, sz, px)) = ys { yes_done = is_done(&st); yes_filled = sz; if px > 0.0 { yes_final_price = px; } }
            if let Some((st, sz, px)) = ns { no_done = is_done(&st); no_filled = sz; if px > 0.0 { no_final_price = px; } }

            // Both legs filled → success, fast return
            if yes_filled > 0.0 && no_filled > 0.0 && yes_done && no_done {
                let total_ms = t_start.elapsed().as_secs_f64() * 1000.0;
                info!("executor: FILLED run={} yes={:.2} no={:.2} total={:.1}ms", run_id, yes_filled, no_filled, total_ms);
                return RustExecutionResult {
                    status: "FILLED".into(),
                    run_id,
                    market_id: opp.market_id.clone(),
                    yes_order_id: Some(yes_oid),
                    no_order_id: Some(no_oid),
                    reason: format!(
                        "BOTH_FILLED yes={:.4} no={:.4}{}{}",
                        yes_filled,
                        no_filled,
                        timing_since_detect("detect_to_sign"),
                        timing_since_detect("detect_to_submit"),
                    ),
                    reason_code: Some("BOTH_FILLED".into()),
                    yes_filled_size: yes_filled,
                    no_filled_size: no_filled,
                    yes_target_price: yes_price,
                    no_target_price: no_price,
                    yes_final_price,
                    no_final_price,
                    sign_ms,
                    submit_ms,
                    total_ms,
                };
            }

            // Both orders reached terminal state but not both filled — skip to recovery
            if yes_done && no_done { break; }

            sleep(poll_interval).await;
        }

        // =================================================================
        // PHASE 3: Recovery — cancel, retry, or unwind
        // =================================================================

        // 3a: Cancel any orders still open
        {
            let mut to_cancel = Vec::new();
            if !yes_done { to_cancel.push(yes_oid.as_str()); }
            if !no_done { to_cancel.push(no_oid.as_str()); }
            if !to_cancel.is_empty() {
                info!("executor: cancelling {} open order(s) run={}", to_cancel.len(), run_id);
                if let Err(e) = self.cancel_orders(&to_cancel).await {
                    warn!("executor: cancel_orders error: {} run={}", e, run_id);
                }
            }
        }

        // 3b: Post-cancel re-check (50ms settle, then refresh statuses)
        sleep(Duration::from_millis(50)).await;
        {
            let (ys, ns) = tokio::join!(
                self.get_order_status(&yes_oid),
                self.get_order_status(&no_oid),
            );
            if let Some((_st, sz, px)) = ys { yes_filled = sz; if px > 0.0 { yes_final_price = px; } }
            if let Some((_st, sz, px)) = ns { no_filled = sz; if px > 0.0 { no_final_price = px; } }
        }

        // ----- Case A: Both legs have fills (possibly mismatched) -----
        if yes_filled > 0.0 && no_filled > 0.0 {
            let diff = yes_filled - no_filled;
            if diff.abs() > 0.001 {
                if diff > 0.0 {
                    info!("executor: trimming excess YES {:.4} run={}", diff, run_id);
                    let _ = self.unwind_sell(&opp.yes_token_id, diff, yes_price, yes_neg_risk, yes_fee_bps, config.unwind_max_loss_pct).await;
                } else {
                    info!("executor: trimming excess NO {:.4} run={}", -diff, run_id);
                    let _ = self.unwind_sell(&opp.no_token_id, -diff, no_price, no_neg_risk, no_fee_bps, config.unwind_max_loss_pct).await;
                }
            }
            let total_ms = t_start.elapsed().as_secs_f64() * 1000.0;
            info!("executor: FILLED(recovery) run={} yes={:.2} no={:.2} total={:.1}ms", run_id, yes_filled, no_filled, total_ms);
            return RustExecutionResult {
                status: "FILLED".into(),
                run_id,
                market_id: opp.market_id.clone(),
                yes_order_id: Some(yes_oid),
                no_order_id: Some(no_oid),
                reason: format!(
                    "RECOVERY_BOTH_FILLED yes={:.4} no={:.4}{}{}",
                    yes_filled,
                    no_filled,
                    timing_since_detect("detect_to_sign"),
                    timing_since_detect("detect_to_submit"),
                ),
                reason_code: Some("RECOVERY_BOTH_FILLED".into()),
                yes_filled_size: yes_filled,
                no_filled_size: no_filled,
                yes_target_price: yes_price,
                no_target_price: no_price,
                yes_final_price,
                no_final_price,
                sign_ms,
                submit_ms,
                total_ms,
            };
        }

        // ----- Case B: One-sided fill → immediate unwind -----
        if (yes_filled > 0.0) != (no_filled > 0.0) {
            let (filled_label, filled_token, filled_size, filled_price, filled_neg_risk, filled_fee_bps) =
                if yes_filled > 0.0 {
                    ("YES", &opp.yes_token_id, yes_filled, yes_price, yes_neg_risk, yes_fee_bps)
                } else {
                    ("NO", &opp.no_token_id, no_filled, no_price, no_neg_risk, no_fee_bps)
                };

            warn!("executor: one-sided fill {}={:.4}, unwinding run={}", filled_label, filled_size, run_id);
            let unwind_ok = self.unwind_sell(
                filled_token, filled_size, filled_price,
                filled_neg_risk, filled_fee_bps,
                config.unwind_max_loss_pct,
            ).await;

            let total_ms = t_start.elapsed().as_secs_f64() * 1000.0;
            let rc = if unwind_ok { "PARTIAL_UNWIND_OK" } else { "PARTIAL_UNWIND_FAILED" };
            info!("executor: CANCELLED({}) run={} total={:.1}ms", rc, run_id, total_ms);
            return RustExecutionResult {
                status: "CANCELLED".into(),
                run_id,
                market_id: opp.market_id.clone(),
                yes_order_id: Some(yes_oid),
                no_order_id: Some(no_oid),
                reason: format!(
                    "{} {}_filled={:.4} unwind={}{}{}",
                    rc,
                    filled_label,
                    filled_size,
                    unwind_ok,
                    timing_since_detect("detect_to_sign"),
                    timing_since_detect("detect_to_submit"),
                ),
                reason_code: Some(rc.into()),
                yes_filled_size: yes_filled,
                no_filled_size: no_filled,
                yes_target_price: yes_price,
                no_target_price: no_price,
                yes_final_price,
                no_final_price,
                sign_ms,
                submit_ms,
                total_ms,
            };
        }

        // ----- Case C: Nothing filled -----
        let total_ms = t_start.elapsed().as_secs_f64() * 1000.0;
        info!("executor: CANCELLED(no fills) run={} total={:.1}ms", run_id, total_ms);
        RustExecutionResult {
            status: "CANCELLED".into(),
            run_id,
            market_id: opp.market_id.clone(),
            yes_order_id: Some(yes_oid),
            no_order_id: Some(no_oid),
            reason: format!(
                "TIMEOUT_NO_FILLS{}{}",
                timing_since_detect("detect_to_sign"),
                timing_since_detect("detect_to_submit"),
            ),
            reason_code: Some("TIMEOUT_NO_FILLS".into()),
            yes_filled_size: 0.0,
            no_filled_size: 0.0,
            yes_target_price: yes_price,
            no_target_price: no_price,
            yes_final_price,
            no_final_price,
            sign_ms,
            submit_ms,
            total_ms,
        }
    }

    // =====================================================================
    // Recovery helpers
    // =====================================================================

    /// Sell shares to unwind a one-sided position. Uses marketable FAK with loss cap.
    async fn unwind_sell(
        &self,
        token_id: &str,
        shares: f64,
        buy_price: f64,
        neg_risk: bool,
        fee_rate_bps: u32,
        max_loss_pct: f64,
    ) -> bool {
        if shares <= 0.0 { return false; }

        // Marketable FAK sell while respecting max loss cap.
        let sell_price = (buy_price * (1.0 - max_loss_pct / 100.0)).max(0.0001);

        let order = self.build_order(token_id, sell_price, shares, fee_rate_bps, SELL);
        let sig = match self.sign_order(&order, neg_risk) {
            Ok(s) => s,
            Err(e) => { error!("unwind sign failed: {}", e); return false; }
        };
        let body = self.build_post_body(&order, &sig, "FAK");
        match self.post_order(&body).await {
            Ok(v) => {
                info!("unwind sell submitted: oid={:?}", extract_order_id(&v));
                true
            }
            Err(e) => {
                error!("unwind sell POST failed: {}", e);
                false
            }
        }
    }

    // =====================================================================
    // Order construction
    // =====================================================================

    fn build_order(
        &self,
        token_id: &str,
        price: f64,
        size: f64,
        fee_rate_bps: u32,
        side: u8,
    ) -> Order {
        // Polymarket uses 6 decimals (USDC): 1 USDC = 1_000_000
        // BUY:  makerAmount = price*size*1e6 (USDC you pay),   takerAmount = size*1e6 (shares)
        // SELL: makerAmount = size*1e6 (shares you give),      takerAmount = price*size*1e6 (USDC)
        let (maker_amount, taker_amount) = if side == BUY {
            (
                (size * price * 1_000_000.0) as u128,
                (size * 1_000_000.0) as u128,
            )
        } else {
            (
                (size * 1_000_000.0) as u128,
                (size * price * 1_000_000.0) as u128,
            )
        };

        let salt: u64 = rand_salt();
        let maker_addr = Address::from_str(&self.funder_address).unwrap_or_default();
        let token_id_u256 = U256::from_str(token_id).unwrap_or_default();

        Order {
            salt: U256::from(salt),
            maker: maker_addr,
            signer: self.signer_address,
            taker: Address::ZERO,
            tokenId: token_id_u256,
            makerAmount: U256::from(maker_amount),
            takerAmount: U256::from(taker_amount),
            expiration: U256::ZERO,
            nonce: U256::ZERO,
            feeRateBps: U256::from(fee_rate_bps),
            side,
            signatureType: self.signature_type as u8,
        }
    }

    // =====================================================================
    // EIP-712 signing (k256 direct)
    // =====================================================================

    fn sign_order(&self, order: &Order, neg_risk: bool) -> Result<String> {
        let exchange_addr = if neg_risk {
            Address::from_str(NEG_RISK_EXCHANGE_ADDRESS)?
        } else {
            Address::from_str(EXCHANGE_ADDRESS)?
        };

        let domain = eip712_domain! {
            name: "Polymarket CTF Exchange",
            version: "1",
            chain_id: self.chain_id,
            verifying_contract: exchange_addr,
        };

        let signing_hash: B256 = order.eip712_signing_hash(&domain);

        let (sig, recid) = self.signing_key
            .sign_prehash(signing_hash.as_ref())
            .map_err(|e| anyhow!("ECDSA sign failed: {e}"))?;

        let mut sig_bytes = [0u8; 65];
        sig_bytes[..64].copy_from_slice(&sig.to_bytes());
        sig_bytes[64] = recid.to_byte() + 27;

        Ok(format!("0x{}", hex::encode(sig_bytes)))
    }

    // =====================================================================
    // POST body construction
    // =====================================================================

    fn build_post_body(
        &self,
        order: &Order,
        signature: &str,
        order_type: &str,
    ) -> serde_json::Value {
        let salt_u64 = order.salt.as_limbs()[0];
        let side_str = if order.side == BUY { "BUY" } else { "SELL" };

        serde_json::json!({
            "order": {
                "salt": salt_u64,
                "maker": format!("{}", order.maker),
                "signer": format!("{}", order.signer),
                "taker": format!("{}", order.taker),
                "tokenId": order.tokenId.to_string(),
                "makerAmount": order.makerAmount.to_string(),
                "takerAmount": order.takerAmount.to_string(),
                "expiration": order.expiration.to_string(),
                "nonce": order.nonce.to_string(),
                "feeRateBps": order.feeRateBps.to_string(),
                "side": side_str,
                "signatureType": order.signatureType,
                "signature": signature,
            },
            "owner": self.api_key,
            "orderType": order_type,
        })
    }

    // =====================================================================
    // HTTP helpers
    // =====================================================================

    /// POST /order — submit a signed order to the CLOB.
    async fn post_order(&self, body: &serde_json::Value) -> Result<serde_json::Value> {
        let path = "/order";
        let url = format!("{}{}", self.clob_base_url, path);
        let body_str = serde_json::to_string(body)?;

        let timestamp = now_unix_secs();
        let hmac_sig = self.compute_hmac(&timestamp, "POST", path, &body_str)?;

        let resp = self.http
            .post(&url)
            .header("Content-Type", "application/json")
            .header("POLY_ADDRESS", format!("{}", self.signer_address))
            .header("POLY_SIGNATURE", &hmac_sig)
            .header("POLY_TIMESTAMP", &timestamp)
            .header("POLY_API_KEY", &self.api_key)
            .header("POLY_PASSPHRASE", &self.api_passphrase)
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

    /// GET /data/order/{id} — fetch order status + filled size.
    async fn get_order_status(&self, order_id: &str) -> Option<(String, f64, f64)> {
        let path = format!("/data/order/{}", order_id);
        let url = format!("{}{}", self.clob_base_url, &path);

        let timestamp = now_unix_secs();
        let hmac_sig = self.compute_hmac(&timestamp, "GET", &path, "").ok()?;

        let resp = self.http
            .get(&url)
            .header("POLY_ADDRESS", format!("{}", self.signer_address))
            .header("POLY_SIGNATURE", &hmac_sig)
            .header("POLY_TIMESTAMP", &timestamp)
            .header("POLY_API_KEY", &self.api_key)
            .header("POLY_PASSPHRASE", &self.api_passphrase)
            .send()
            .await
            .ok()?;

        if !resp.status().is_success() { return None; }

        let body: serde_json::Value = resp.json().await.ok()?;

        let status = body.get("status")
            .or_else(|| body.get("state"))
            .and_then(|v| v.as_str())
            .map(|s| s.to_uppercase())
            .unwrap_or_default();

        // Polymarket uses various field names for filled size
        let filled = ["sizeMatched", "filledSize", "matchedSize", "filled", "filled_size", "size_matched"]
            .iter()
            .find_map(|&k| body.get(k))
            .and_then(|v| v.as_f64().or_else(|| v.as_str().and_then(|s| s.parse().ok())))
            .unwrap_or(0.0);

        let avg_price = ["avgPrice", "averagePrice", "price", "filledPrice"]
            .iter()
            .find_map(|&k| body.get(k))
            .and_then(|v| v.as_f64().or_else(|| v.as_str().and_then(|s| s.parse().ok())))
            .unwrap_or(0.0);

        Some((status, filled, avg_price))
    }

    /// DELETE /orders — batch cancel orders by ID.
    async fn cancel_orders(&self, order_ids: &[&str]) -> Result<()> {
        if order_ids.is_empty() { return Ok(()); }

        let path = "/orders";
        let url = format!("{}{}", self.clob_base_url, path);
        let body = serde_json::json!(order_ids);
        let body_str = serde_json::to_string(&body)?;

        let timestamp = now_unix_secs();
        let hmac_sig = self.compute_hmac(&timestamp, "DELETE", path, &body_str)?;

        let resp = self.http
            .delete(&url)
            .header("Content-Type", "application/json")
            .header("POLY_ADDRESS", format!("{}", self.signer_address))
            .header("POLY_SIGNATURE", &hmac_sig)
            .header("POLY_TIMESTAMP", &timestamp)
            .header("POLY_API_KEY", &self.api_key)
            .header("POLY_PASSPHRASE", &self.api_passphrase)
            .body(body_str)
            .send()
            .await
            .context("cancel_orders HTTP DELETE failed")?;

        let status = resp.status();
        if !status.is_success() {
            let text = resp.text().await.unwrap_or_default();
            return Err(anyhow!("cancel_orders returned {}: {}", status, &text[..text.len().min(300)]));
        }
        Ok(())
    }

    /// Compute HMAC-SHA256 L2 auth signature.
    fn compute_hmac(
        &self,
        timestamp: &str,
        method: &str,
        path: &str,
        body: &str,
    ) -> Result<String> {
        let secret_bytes = base64::engine::general_purpose::URL_SAFE
            .decode(&self.api_secret)
            .context("Failed to base64-decode api_secret")?;

        let message = format!("{}{}{}{}", timestamp, method, path, body);

        let mut mac = Hmac::<Sha256>::new_from_slice(&secret_bytes)
            .map_err(|e| anyhow!("HMAC key error: {e}"))?;
        mac.update(message.as_bytes());
        let result = mac.finalize().into_bytes();

        Ok(base64::engine::general_purpose::URL_SAFE.encode(result))
    }
}

// ---------------------------------------------------------------------------
// Free-standing helpers
// ---------------------------------------------------------------------------

fn aggressive_buy_limit(best_ask: f64, cross_bps: f64) -> f64 {
    if best_ask <= 0.0 { return best_ask; }
    let bumped = best_ask * (1.0 + cross_bps / 10_000.0);
    bumped.min(0.9999).max(0.0001)
}

fn round_to_tick(price: f64, tick_size: &str) -> f64 {
    let tick: f64 = tick_size.parse().unwrap_or(0.01);
    if tick <= 0.0 { return price; }
    (price / tick).round() * tick
}

fn is_done(status: &str) -> bool {
    matches!(status, "FILLED" | "CANCELED" | "CANCELLED" | "REJECTED" | "FAILED" | "EXPIRED")
}

fn extract_order_id(v: &serde_json::Value) -> Option<String> {
    v.get("orderID")
        .or_else(|| v.get("id"))
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
}

fn now_unix_secs() -> String {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
        .to_string()
}

fn delta_ms_from_unix_ns(start_ns: u64) -> Option<f64> {
    let now_ns = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_nanos() as u64;
    if now_ns < start_ns {
        return None;
    }
    Some((now_ns - start_ns) as f64 / 1_000_000.0)
}

fn make_error_result(run_id: &str, market_id: &str, reason: &str, sign_ms: f64) -> RustExecutionResult {
    RustExecutionResult {
        status: "FAILED".into(),
        run_id: run_id.to_string(),
        market_id: market_id.to_string(),
        yes_order_id: None,
        no_order_id: None,
        reason: reason.to_string(),
        reason_code: Some("SIGN_ERROR".into()),
        yes_filled_size: 0.0,
        no_filled_size: 0.0,
        yes_target_price: 0.0,
        no_target_price: 0.0,
        yes_final_price: 0.0,
        no_final_price: 0.0,
        sign_ms,
        submit_ms: 0.0,
        total_ms: sign_ms,
    }
}

fn rand_u64() -> u64 {
    let t = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    t.as_nanos() as u64 ^ (t.subsec_nanos() as u64).wrapping_mul(0x517cc1b727220a95)
}

fn rand_salt() -> u64 {
    let t = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    let millis = t.as_millis() as u64;
    let factor = (t.subsec_nanos() as u64 % 1_000_000).wrapping_add(1);
    millis.wrapping_mul(factor)
}
