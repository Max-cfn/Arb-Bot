//! EIP-712 signing and HTTP order submission to the Polymarket CLOB.
//!
//! Three-phase execution with complete-or-abort safety:
//!   Phase 1 — Sign + POST both legs in parallel (hot path, ~50ms)
//!   Phase 2 — Poll for fills (up to 500ms)
//!   Phase 3 — Recovery: cancel open → retry failed leg → unwind if needed
//!
//! Uses k256 directly for ECDSA signing (avoids alloy-signer-local and its
//! broken alloy-consensus transitive dependency chain).

use std::str::FromStr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use alloy_primitives::{Address, B256, U256};
use alloy_sol_types::{SolStruct, eip712_domain, sol};
use anyhow::{Context, Result, anyhow};
use base64::Engine as _;
use hmac::{Hmac, Mac};
use k256::ecdsa::{SigningKey, signature::hazmat::PrehashSigner};
use reqwest::Client;
use sha2::{Digest, Sha256};
use tokio::time::{Duration, sleep};
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
    /// Cached USDC collateral balance in micro-units (1 USDC = 1_000_000).
    /// Refreshed by `refresh_balance()` — reads in the hot path are free (atomic).
    pub balance_usdc: Arc<AtomicU64>,
}

impl FastExecutor {
    pub fn new(config: &EngineConfig) -> Result<Self> {
        let key_hex = config
            .private_key
            .strip_prefix("0x")
            .unwrap_or(&config.private_key);
        let key_bytes = hex::decode(key_hex).context("Failed to hex-decode private key")?;

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

        info!("executor: initialized signer_address={}", signer_address,);

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
            balance_usdc: Arc::new(AtomicU64::new(u64::MAX)), // MAX = "not yet fetched"
        })
    }

    /// Fetch and cache the USDC collateral balance from the CLOB.
    /// Safe to call from a background task concurrently with `execute()`.
    pub async fn refresh_balance(&self) {
        let path = "/balance-allowance?asset_type=0";
        let url = format!("{}{}", self.clob_base_url, path);
        let ts = now_unix_secs();
        let sig = match self.compute_hmac(&ts, "GET", path, "") {
            Ok(s) => s,
            Err(e) => {
                error!("refresh_balance: hmac failed: {}", e);
                return;
            }
        };
        let resp = self
            .http
            .get(&url)
            .header("POLY_ADDRESS", format!("{}", self.signer_address))
            .header("POLY_SIGNATURE", &sig)
            .header("POLY_TIMESTAMP", &ts)
            .header("POLY_API_KEY", &self.api_key)
            .header("POLY_PASSPHRASE", &self.api_passphrase)
            .send()
            .await;
        match resp {
            Err(e) => error!("refresh_balance: request failed: {}", e),
            Ok(r) => {
                if let Ok(body) = r.json::<serde_json::Value>().await {
                    // CLOB returns balance in micro-USDC (1e6 = $1)
                    let raw = body
                        .get("balance")
                        .and_then(|v| {
                            v.as_u64()
                                .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
                        })
                        .unwrap_or(0);
                    self.balance_usdc.store(raw, Ordering::Relaxed);
                    info!(
                        "balance refreshed: ${:.4} (raw={})",
                        raw as f64 / 1_000_000.0,
                        raw
                    );
                }
            }
        }
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

        let detect_to_sign_ms = delta_ms_from_unix_ns(opp.t_detect_ns).unwrap_or(0.0);

        // -----------------------------------------------------------------
        // Resolve market metadata
        // -----------------------------------------------------------------
        let yes_meta = meta_cache.get(&opp.yes_token_id);
        let no_meta = meta_cache.get(&opp.no_token_id);
        let yes_neg_risk = match yes_meta.as_ref() {
            Some(m) => m.neg_risk,
            None => {
                warn!(
                    "run={} yes_token={} absent from MetaCache — neg_risk defaults false \
                     (may cause invalid signature on NegRisk markets)",
                    run_id, opp.yes_token_id
                );
                false
            }
        };
        let no_neg_risk = match no_meta.as_ref() {
            Some(m) => m.neg_risk,
            None => {
                warn!(
                    "run={} no_token={} absent from MetaCache — neg_risk defaults false \
                     (may cause invalid signature on NegRisk markets)",
                    run_id, opp.no_token_id
                );
                false
            }
        };
        let yes_tick = yes_meta
            .as_ref()
            .map(|m| m.tick_size.as_str())
            .unwrap_or("0.01");
        let no_tick = no_meta
            .as_ref()
            .map(|m| m.tick_size.as_str())
            .unwrap_or("0.01");
        let yes_fee_bps = yes_meta.as_ref().map(|m| m.fee_rate_bps).unwrap_or(0);
        let no_fee_bps = no_meta.as_ref().map(|m| m.fee_rate_bps).unwrap_or(0);

        // Compute aggressive limit prices (cross above best ask)
        let yes_limit = aggressive_buy_limit(opp.yes_best_ask, config.cross_bps);
        let no_limit = aggressive_buy_limit(opp.no_best_ask, config.cross_bps);

        // Compute symmetric share size using both legs constraints.
        let yes_shares_for_min_notional = if yes_limit > 0.0 {
            (config.min_order_usd / yes_limit).ceil() as u64
        } else {
            0
        };
        let no_shares_for_min_notional = if no_limit > 0.0 {
            (config.min_order_usd / no_limit).ceil() as u64
        } else {
            0
        };
        let shares_floor = config.min_order_shares.ceil() as u64;
        let shares = yes_shares_for_min_notional
            .max(no_shares_for_min_notional)
            .max(shares_floor);

        // Round prices to tick_size
        let yes_price = round_to_tick(yes_limit, yes_tick);
        let no_price = round_to_tick(no_limit, no_tick);

        let detect_edge_pct = opp.net_edge_percent;
        let exec_edge_pct = estimate_net_edge_percent(yes_price, no_price, yes_fee_bps, no_fee_bps);
        let edge_decay_bps = ((detect_edge_pct - exec_edge_pct) * 100.0).max(0.0);

        // PRE-ATTEMPT LOG
        let _braw=self.balance_usdc.load(std::sync::atomic::Ordering::Relaxed);
        let _bstr=if _braw==u64::MAX{"NOT_FETCHED".to_string()}else{format!("${:.4}",_braw as f64/1_000_000.0)};
        info!("pre_attempt run={} mkt={} bal={} yes_ask={:.4} no_ask={:.4} edge={:.3}%",
            run_id,opp.market_id,_bstr,opp.yes_best_ask,opp.no_best_ask,detect_edge_pct);
        // -----------------------------------------------------------------
        // Balance guard — free atomic read, zero network cost on hot path.
        // sum_target_usd = shares * (yes_price + no_price) in micro-USDC.
        // We skip only when the cached balance is known and insufficient.
        // (u64::MAX means "not yet fetched" — let it through.)
        // -----------------------------------------------------------------
        let sum_target_usdc = (shares as f64 * (yes_price + no_price) * 1_000_000.0) as u64;
        let cached_bal = self.balance_usdc.load(Ordering::Relaxed);
        if cached_bal != u64::MAX && cached_bal < sum_target_usdc {
            let total_ms = t_start.elapsed().as_secs_f64() * 1000.0;
            return RustExecutionResult {
                status: "SKIPPED".into(),
                run_id,
                market_id: opp.market_id.clone(),
                yes_order_id: None,
                no_order_id: None,
                reason: format!(
                    "INSUFFICIENT_BALANCE cached=${:.4} needed=${:.4}",
                    cached_bal as f64 / 1_000_000.0,
                    sum_target_usdc as f64 / 1_000_000.0,
                ),
                reason_code: Some("INSUFFICIENT_BALANCE".into()),
                yes_filled_size: 0.0,
                no_filled_size: 0.0,
                yes_target_price: yes_price,
                no_target_price: no_price,
                yes_final_price: 0.0,
                no_final_price: 0.0,
                sign_ms: 0.0,
                submit_ms: 0.0,
                detect_to_sign_ms,
                detect_to_submit_ms: detect_to_sign_ms,
                total_ms,
            };
        }

        if exec_edge_pct < config.min_execution_edge_percent
            || edge_decay_bps > config.max_edge_decay_bps
        {
            let total_ms = t_start.elapsed().as_secs_f64() * 1000.0;
            return RustExecutionResult {
                status: "SKIPPED".into(),
                run_id,
                market_id: opp.market_id.clone(),
                yes_order_id: None,
                no_order_id: None,
                reason: format!(
                    "EDGE_GUARD detect_edge_pct={:.5} exec_edge_pct={:.5} edge_decay_bps={:.3}",
                    detect_edge_pct, exec_edge_pct, edge_decay_bps,
                ),
                reason_code: Some("EDGE_DECAY_GUARD".into()),
                yes_filled_size: 0.0,
                no_filled_size: 0.0,
                yes_target_price: yes_price,
                no_target_price: no_price,
                yes_final_price: 0.0,
                no_final_price: 0.0,
                sign_ms: 0.0,
                submit_ms: 0.0,
                detect_to_sign_ms,
                detect_to_submit_ms: detect_to_sign_ms,
                total_ms,
            };
        }

        let yes_order = self.build_order(
            &opp.yes_token_id,
            yes_price,
            shares as f64,
            yes_fee_bps,
            BUY,
        );
        let no_order = self.build_order(&opp.no_token_id, no_price, shares as f64, no_fee_bps, BUY);

        // Sign both legs concurrently to minimize detect->submit latency.
        let t_sign_start = Instant::now();
        let (yes_sig_res, no_sig_res) =
            tokio::join!(async { self.sign_order(&yes_order, yes_neg_risk) }, async {
                self.sign_order(&no_order, no_neg_risk)
            },);
        let sign_ms = t_sign_start.elapsed().as_secs_f64() * 1000.0;

        let yes_sig = match yes_sig_res {
            Ok(v) => v,
            Err(e) => {
                return make_error_result(
                    &run_id,
                    &opp.market_id,
                    &format!("YES sign failed: {e}"),
                    sign_ms,
                    detect_to_sign_ms,
                );
            }
        };
        let no_sig = match no_sig_res {
            Ok(v) => v,
            Err(e) => {
                return make_error_result(
                    &run_id,
                    &opp.market_id,
                    &format!("NO sign failed: {e}"),
                    sign_ms,
                    detect_to_sign_ms,
                );
            }
        };

        let yes_body = self.build_post_body(&yes_order, &yes_sig, "FOK");
        let no_body = self.build_post_body(&no_order, &no_sig, "FOK");

        let t_submit = Instant::now();
        let (yes_resp, no_resp) =
            tokio::join!(self.post_order(&yes_body), self.post_order(&no_body));
        let submit_ms = t_submit.elapsed().as_secs_f64() * 1000.0;
        let detect_to_submit_ms = delta_ms_from_unix_ns(opp.t_detect_ns)
            .unwrap_or(detect_to_sign_ms + sign_ms + submit_ms);

        // ATTEMPT TRACE
        let yj=yes_resp.as_ref().ok().and_then(extract_order_id);
        let nj=no_resp.as_ref().ok().and_then(extract_order_id);
        let ye=yes_resp.as_ref().err().map(|e|e.to_string()).unwrap_or_default();
        let ne=no_resp.as_ref().err().map(|e|e.to_string()).unwrap_or_default();
        info!("attempt_trace run={} mkt={} shares={} sm={:.1}ms YES_ok={} YES_oid={} YES_err={} NO_ok={} NO_oid={} NO_err={}",run_id,opp.market_id,shares,submit_ms,yes_resp.is_ok(),yj.as_deref().unwrap_or("-"),&ye[..ye.len().min(80)],no_resp.is_ok(),nj.as_deref().unwrap_or("-"),&ne[..ne.len().min(80)]);
        let yes_order_id = yes_resp.as_ref().ok().and_then(extract_order_id);
        let no_order_id = no_resp.as_ref().ok().and_then(extract_order_id);

        if yes_order_id.is_none() && no_order_id.is_none() {
            return make_terminal_result(
                "FAILED",
                &run_id,
                &opp.market_id,
                None,
                None,
                format!(
                    "FOK_BOTH_POST_FAILED yes_err={} no_err={}",
                    yes_resp.err().map(|e| e.to_string()).unwrap_or_default(),
                    no_resp.err().map(|e| e.to_string()).unwrap_or_default(),
                ),
                "FOK_POST_FAILED",
                0.0,
                0.0,
                yes_price,
                no_price,
                0.0,
                0.0,
                sign_ms,
                submit_ms,
                detect_to_sign_ms,
                detect_to_submit_ms,
                t_start.elapsed().as_secs_f64() * 1000.0,
            );
        }

        // FOK business invariant: if one leg POST fails, the surviving order
        // may have already been filled (FOK fills instantly).  We must check
        // the surviving order's fill status and unwind if necessary — simply
        // cancelling is not enough.
        if yes_order_id.is_none() || no_order_id.is_none() {
            let is_yes_surviving = yes_order_id.is_some();
            let surviving_oid = if is_yes_surviving {
                yes_order_id.clone().unwrap()
            } else {
                no_order_id.clone().unwrap()
            };

            // Try to cancel the surviving order first.
            if let Err(ref e) = self.cancel_orders(&[&surviving_oid]).await {
                error!(
                    "one_leg_failed: cancel {} failed: {} — will still poll for fills run={}",
                    surviving_oid, e, run_id
                );
            }

            // Wait briefly, then poll fill status with retries.
            // FOK orders fill atomically (all-or-nothing), so surviving_filled is
            // either 0 or `shares`.  We retry up to 4 times (50 ms apart) in case
            // the CLOB API is transiently unavailable right after the cancel.
            sleep(Duration::from_millis(80)).await;

            let mut surviving_filled = 0.0f64;
            let mut surviving_final_price = 0.0f64;
            let mut status_confirmed = false;

            for attempt in 0..4u8 {
                if let Some((_st, sz, px)) = self.get_order_status(&surviving_oid).await {
                    status_confirmed = true;
                    if sz > surviving_filled {
                        surviving_filled = sz;
                    }
                    if px > 0.0 {
                        surviving_final_price = px;
                    }
                    break; // got a definitive response — stop retrying
                }
                if attempt < 3 {
                    sleep(Duration::from_millis(50)).await;
                }
            }

            // If we could not determine the fill state after all retries, perform a
            // defensive unwind using the full order size.  FOK semantics guarantee the
            // order is either fully filled or not at all, so selling `shares` is the
            // correct conservative action and will be harmlessly rejected if the order
            // was actually cancelled.
            if !status_confirmed {
                let (uw_token, uw_price, uw_neg_risk, uw_fee_bps, uw_tick) =
                    if is_yes_surviving {
                        (&opp.yes_token_id, yes_price, yes_neg_risk, yes_fee_bps, yes_tick)
                    } else {
                        (&opp.no_token_id, no_price, no_neg_risk, no_fee_bps, no_tick)
                    };
                warn!(
                    "one_leg_failed: fill status UNKNOWN after 4 retries for {} — \
                     defensive unwind {} shares={} run={}",
                    surviving_oid,
                    if is_yes_surviving { "YES" } else { "NO" },
                    shares,
                    run_id,
                );
                let unwind_ok = self
                    .unwind_sell(
                        uw_token,
                        shares as f64,
                        uw_price,
                        uw_neg_risk,
                        uw_fee_bps,
                        uw_tick,
                        config.unwind_max_loss_pct,
                        config.unwind_min_price_floor,
                    )
                    .await;
                let rc = if unwind_ok {
                    "FOK_DEFENSIVE_UNWIND_OK"
                } else {
                    "FOK_DEFENSIVE_UNWIND_FAILED"
                };
                let (yf, nf, yfp, nfp) = if is_yes_surviving {
                    (shares as f64, 0.0, 0.0, 0.0)
                } else {
                    (0.0, shares as f64, 0.0, 0.0)
                };
                return make_terminal_result(
                    "CANCELLED",
                    &run_id,
                    &opp.market_id,
                    yes_order_id,
                    no_order_id,
                    format!(
                        "{rc} one_leg_post_failed surviving={} status_unknown",
                        if is_yes_surviving { "YES" } else { "NO" },
                    ),
                    rc,
                    yf,
                    nf,
                    yes_price,
                    no_price,
                    yfp,
                    nfp,
                    sign_ms,
                    submit_ms,
                    detect_to_sign_ms,
                    detect_to_submit_ms,
                    t_start.elapsed().as_secs_f64() * 1000.0,
                );
            }

            if surviving_filled > 0.0 {
                // The surviving order filled before we could cancel.
                // Try FAK retry on the failed leg, then unwind if retry fails.
                error!(
                    "one_leg_failed: surviving {} filled={:.4} — attempting recovery run={}",
                    if is_yes_surviving { "YES" } else { "NO" },
                    surviving_filled,
                    run_id
                );

                let (retry_token, retry_price, retry_fee_bps, retry_neg_risk) =
                    if is_yes_surviving {
                        (&opp.no_token_id, no_price, no_fee_bps, no_neg_risk)
                    } else {
                        (&opp.yes_token_id, yes_price, yes_fee_bps, yes_neg_risk)
                    };

                let retry_ok = self
                    .retry_buy(
                        retry_token,
                        retry_price,
                        surviving_filled,
                        retry_fee_bps,
                        retry_neg_risk,
                        Duration::from_millis(25),
                        200,
                        &run_id,
                    )
                    .await;

                if retry_ok {
                    let (yf, nf, yfp, nfp) = if is_yes_surviving {
                        (surviving_filled, surviving_filled, surviving_final_price, 0.0)
                    } else {
                        (surviving_filled, surviving_filled, 0.0, surviving_final_price)
                    };
                    return make_terminal_result(
                        "FILLED",
                        &run_id,
                        &opp.market_id,
                        yes_order_id,
                        no_order_id,
                        format!(
                            "PARTIAL_RETRY_SUCCESS one_leg_post_failed surviving={} filled={:.4}",
                            if is_yes_surviving { "YES" } else { "NO" },
                            surviving_filled
                        ),
                        "PARTIAL_RETRY_SUCCESS",
                        yf,
                        nf,
                        yes_price,
                        no_price,
                        yfp,
                        nfp,
                        sign_ms,
                        submit_ms,
                        detect_to_sign_ms,
                        detect_to_submit_ms,
                        t_start.elapsed().as_secs_f64() * 1000.0,
                    );
                }

                // FAK retry failed — emergency unwind the filled leg.
                let (unwind_token, unwind_price, unwind_neg_risk, unwind_fee_bps, unwind_tick) =
                    if is_yes_surviving {
                        (&opp.yes_token_id, yes_price, yes_neg_risk, yes_fee_bps, yes_tick)
                    } else {
                        (&opp.no_token_id, no_price, no_neg_risk, no_fee_bps, no_tick)
                    };

                error!(
                    "one_leg_failed: FAK retry failed — emergency unwind {}={:.4} run={}",
                    if is_yes_surviving { "YES" } else { "NO" },
                    surviving_filled,
                    run_id
                );

                let unwind_ok = self
                    .unwind_sell(
                        unwind_token,
                        surviving_filled,
                        unwind_price,
                        unwind_neg_risk,
                        unwind_fee_bps,
                        unwind_tick,
                        config.unwind_max_loss_pct,
                        config.unwind_min_price_floor,
                    )
                    .await;

                let rc = if unwind_ok {
                    "FOK_EMERGENCY_UNWIND_OK"
                } else {
                    "FOK_EMERGENCY_UNWIND_FAILED"
                };

                let (yf, nf, yfp, nfp) = if is_yes_surviving {
                    (surviving_filled, 0.0, surviving_final_price, 0.0)
                } else {
                    (0.0, surviving_filled, 0.0, surviving_final_price)
                };

                return make_terminal_result(
                    "CANCELLED",
                    &run_id,
                    &opp.market_id,
                    yes_order_id,
                    no_order_id,
                    format!(
                        "{rc} one_leg_post_failed surviving={} filled={:.4}",
                        if is_yes_surviving { "YES" } else { "NO" },
                        surviving_filled
                    ),
                    rc,
                    yf,
                    nf,
                    yes_price,
                    no_price,
                    yfp,
                    nfp,
                    sign_ms,
                    submit_ms,
                    detect_to_sign_ms,
                    detect_to_submit_ms,
                    t_start.elapsed().as_secs_f64() * 1000.0,
                );
            }

            // Surviving order was not filled — safe to just return CANCELLED.
            return make_terminal_result(
                "CANCELLED",
                &run_id,
                &opp.market_id,
                yes_order_id,
                no_order_id,
                "FOK_ONE_LEG_REJECTED_OR_FAILED".to_string(),
                "FOK_ONE_LEG_FAILED",
                0.0,
                0.0,
                yes_price,
                no_price,
                0.0,
                0.0,
                sign_ms,
                submit_ms,
                detect_to_sign_ms,
                detect_to_submit_ms,
                t_start.elapsed().as_secs_f64() * 1000.0,
            );
        }

        let yes_oid = yes_order_id.unwrap();
        let no_oid = no_order_id.unwrap();

        // -----------------------------------------------------------------
        // Phase 2 — Poll until both legs reach a terminal state (up to 500ms).
        // A single get_order_status call is a race against the exchange matching
        // engine: if the order is still in-flight the response will be empty.
        // We must keep polling until we see a terminal state on each leg.
        // -----------------------------------------------------------------
        let poll_interval = Duration::from_millis(25);
        let poll_deadline = Instant::now() + Duration::from_millis(500);

        let mut yes_state = String::new();
        let mut yes_filled = 0.0f64;
        let mut yes_final_price = 0.0f64;
        let mut no_state = String::new();
        let mut no_filled = 0.0f64;
        let mut no_final_price = 0.0f64;
        let mut yes_terminal = false;
        let mut no_terminal = false;

        while Instant::now() < poll_deadline {
            // Only poll legs that have not yet reached a terminal state.
            let (yes_s, no_s) = tokio::join!(
                async {
                    if yes_terminal { None } else { self.get_order_status(&yes_oid).await }
                },
                async {
                    if no_terminal { None } else { self.get_order_status(&no_oid).await }
                },
            );

            if let Some((st, sz, px)) = yes_s {
                // Keep max filled size seen across polls (fills are monotonic).
                if sz > yes_filled { yes_filled = sz; }
                if px > 0.0 { yes_final_price = px; }
                yes_state = st;
                yes_terminal = is_done(&yes_state);
            }
            if let Some((st, sz, px)) = no_s {
                if sz > no_filled { no_filled = sz; }
                if px > 0.0 { no_final_price = px; }
                no_state = st;
                no_terminal = is_done(&no_state);
            }

            // Both legs terminal → no need to wait longer.
            if yes_terminal && no_terminal {
                break;
            }
            sleep(poll_interval).await;
        }

        info!("poll_result run={} YES_state={} YES_filled={:.4} YES_term={} NO_state={} NO_filled={:.4} NO_term={}",
            run_id,yes_state,yes_filled,yes_terminal,no_state,no_filled,no_terminal);
        let yes_full = yes_filled + 0.000001 >= shares as f64 && yes_terminal;
        let no_full = no_filled + 0.000001 >= shares as f64 && no_terminal;

        // Happy path: both legs filled completely.
        if yes_full && no_full {
            return make_terminal_result(
                "FILLED",
                &run_id,
                &opp.market_id,
                Some(yes_oid),
                Some(no_oid),
                format!(
                    "FOK_BOTH_FILLED shares={shares} detect_edge_pct={:.5} exec_edge_pct={:.5}",
                    detect_edge_pct, exec_edge_pct,
                ),
                "FOK_BOTH_FILLED",
                yes_filled,
                no_filled,
                yes_price,
                no_price,
                yes_final_price,
                no_final_price,
                sign_ms,
                submit_ms,
                detect_to_sign_ms,
                detect_to_submit_ms,
                t_start.elapsed().as_secs_f64() * 1000.0,
            );
        }

        // -----------------------------------------------------------------
        // Phase 3 — Recovery: cancel open orders, then try to complete the
        // arb or safely unwind any one-sided fill.
        // -----------------------------------------------------------------

        // Cancel both legs.  Ignore errors: a filled FOK order cannot be
        // cancelled and the exchange will return an error — that is expected.
        let cancel_result = self.cancel_orders(&[&yes_oid, &no_oid]).await;
        if let Err(ref e) = cancel_result {
            error!(
                "cancel attempt run={} err={} (a leg may already be filled)",
                run_id, e
            );
        }

        // Brief pause so the exchange can update statuses after the cancel.
        sleep(Duration::from_millis(80)).await;

        // Re-fetch terminal statuses after cancel to get accurate fill sizes.
        let (yes_s2, no_s2) = tokio::join!(
            self.get_order_status(&yes_oid),
            self.get_order_status(&no_oid),
        );
        if let Some((st, sz, px)) = yes_s2 {
            if sz > yes_filled { yes_filled = sz; }
            if px > 0.0 { yes_final_price = px; }
            yes_state = st;
        }
        if let Some((st, sz, px)) = no_s2 {
            if sz > no_filled { no_filled = sz; }
            if px > 0.0 { no_final_price = px; }
            no_state = st;
        }

        let yes_has_fill = yes_filled > 0.0;
        let no_has_fill = no_filled > 0.0;

        // Case A: both partially/fully filled after cancel re-check.
        if yes_has_fill && no_has_fill {
            return make_terminal_result(
                "FILLED",
                &run_id,
                &opp.market_id,
                Some(yes_oid),
                Some(no_oid),
                format!(
                    "RECOVERY_BOTH_FILLED yes_filled={:.4} no_filled={:.4}",
                    yes_filled, no_filled
                ),
                "RECOVERY_BOTH_FILLED",
                yes_filled,
                no_filled,
                yes_price,
                no_price,
                yes_final_price,
                no_final_price,
                sign_ms,
                submit_ms,
                detect_to_sign_ms,
                detect_to_submit_ms,
                t_start.elapsed().as_secs_f64() * 1000.0,
            );
        }

        // Case B: exactly one leg filled → attempt FAK retry on the unfilled leg.
        if yes_has_fill && !no_has_fill {
            info!(
                "recovery: YES filled ({:.4}) NO empty — FAK retry on NO run={}",
                yes_filled, run_id
            );
            let retry_ok = self
                .retry_buy(
                    &opp.no_token_id,
                    no_price,
                    yes_filled,   // match the actual YES fill size
                    no_fee_bps,
                    no_neg_risk,
                    Duration::from_millis(25),
                    200,
                    &run_id,
                )
                .await;

            if retry_ok {
                return make_terminal_result(
                    "FILLED",
                    &run_id,
                    &opp.market_id,
                    Some(yes_oid),
                    Some(no_oid),
                    format!("PARTIAL_RETRY_SUCCESS YES filled NO retried={:.4}", yes_filled),
                    "PARTIAL_RETRY_SUCCESS",
                    yes_filled,
                    yes_filled,   // NO leg filled same size
                    yes_price,
                    no_price,
                    yes_final_price,
                    no_final_price,
                    sign_ms,
                    submit_ms,
                    detect_to_sign_ms,
                    detect_to_submit_ms,
                    t_start.elapsed().as_secs_f64() * 1000.0,
                );
            }

            // FAK retry failed → emergency market-sell of YES leg.
            error!(
                "recovery: FAK retry failed — emergency unwind YES={:.4} run={}",
                yes_filled, run_id
            );
            let unwind_ok = self
                .unwind_sell(
                    &opp.yes_token_id,
                    yes_filled,
                    yes_price,
                    yes_neg_risk,
                    yes_fee_bps,
                    yes_tick,
                    config.unwind_max_loss_pct,
                    config.unwind_min_price_floor,
                )
                .await;
            let rc = if unwind_ok {
                "FOK_EMERGENCY_UNWIND_OK"
            } else {
                "FOK_EMERGENCY_UNWIND_FAILED"
            };
            return make_terminal_result(
                "CANCELLED",
                &run_id,
                &opp.market_id,
                Some(yes_oid),
                Some(no_oid),
                format!(
                    "{rc} yes_state={yes_state} yes_filled={yes_filled:.6} \
                     no_state={no_state} no_filled={no_filled:.6}"
                ),
                rc,
                yes_filled,
                no_filled,
                yes_price,
                no_price,
                yes_final_price,
                no_final_price,
                sign_ms,
                submit_ms,
                detect_to_sign_ms,
                detect_to_submit_ms,
                t_start.elapsed().as_secs_f64() * 1000.0,
            );
        }

        if no_has_fill && !yes_has_fill {
            info!(
                "recovery: NO filled ({:.4}) YES empty — FAK retry on YES run={}",
                no_filled, run_id
            );
            let retry_ok = self
                .retry_buy(
                    &opp.yes_token_id,
                    yes_price,
                    no_filled,
                    yes_fee_bps,
                    yes_neg_risk,
                    Duration::from_millis(25),
                    200,
                    &run_id,
                )
                .await;

            if retry_ok {
                return make_terminal_result(
                    "FILLED",
                    &run_id,
                    &opp.market_id,
                    Some(yes_oid),
                    Some(no_oid),
                    format!("PARTIAL_RETRY_SUCCESS NO filled YES retried={:.4}", no_filled),
                    "PARTIAL_RETRY_SUCCESS",
                    no_filled,
                    no_filled,
                    yes_price,
                    no_price,
                    yes_final_price,
                    no_final_price,
                    sign_ms,
                    submit_ms,
                    detect_to_sign_ms,
                    detect_to_submit_ms,
                    t_start.elapsed().as_secs_f64() * 1000.0,
                );
            }

            error!(
                "recovery: FAK retry failed — emergency unwind NO={:.4} run={}",
                no_filled, run_id
            );
            let unwind_ok = self
                .unwind_sell(
                    &opp.no_token_id,
                    no_filled,
                    no_price,
                    no_neg_risk,
                    no_fee_bps,
                    no_tick,
                    config.unwind_max_loss_pct,
                    config.unwind_min_price_floor,
                )
                .await;
            let rc = if unwind_ok {
                "FOK_EMERGENCY_UNWIND_OK"
            } else {
                "FOK_EMERGENCY_UNWIND_FAILED"
            };
            return make_terminal_result(
                "CANCELLED",
                &run_id,
                &opp.market_id,
                Some(yes_oid),
                Some(no_oid),
                format!(
                    "{rc} yes_state={yes_state} yes_filled={yes_filled:.6} \
                     no_state={no_state} no_filled={no_filled:.6}"
                ),
                rc,
                yes_filled,
                no_filled,
                yes_price,
                no_price,
                yes_final_price,
                no_final_price,
                sign_ms,
                submit_ms,
                detect_to_sign_ms,
                detect_to_submit_ms,
                t_start.elapsed().as_secs_f64() * 1000.0,
            );
        }

        // Case C: neither leg has any fill.
        make_terminal_result(
            "CANCELLED",
            &run_id,
            &opp.market_id,
            Some(yes_oid),
            Some(no_oid),
            format!(
                "FOK_NO_FILLS yes_state={} yes_filled={:.6} no_state={} no_filled={:.6}",
                yes_state, yes_filled, no_state, no_filled,
            ),
            "FOK_NO_FILLS",
            yes_filled,
            no_filled,
            yes_price,
            no_price,
            yes_final_price,
            no_final_price,
            sign_ms,
            submit_ms,
            detect_to_sign_ms,
            detect_to_submit_ms,
            t_start.elapsed().as_secs_f64() * 1000.0,
        )
    }

    // =====================================================================
    // Recovery helpers
    // =====================================================================

    /// Attempt to buy the other leg with FAK. Returns true if filled.
    async fn retry_buy(
        &self,
        token_id: &str,
        price: f64,
        size: f64,
        fee_rate_bps: u32,
        neg_risk: bool,
        poll_interval: Duration,
        retry_duration_ms: u64,
        run_id: &str,
    ) -> bool {
        let order = self.build_order(token_id, price, size, fee_rate_bps, BUY);
        let sig = match self.sign_order(&order, neg_risk) {
            Ok(s) => s,
            Err(e) => {
                error!("retry sign failed: {} run={}", e, run_id);
                return false;
            }
        };
        let body = self.build_post_body(&order, &sig, "FAK");
        let resp = match self.post_order(&body).await {
            Ok(v) => v,
            Err(e) => {
                error!("retry POST failed: {} run={}", e, run_id);
                return false;
            }
        };
        let retry_oid = match extract_order_id(&resp) {
            Some(id) => id,
            None => {
                error!("retry: no orderID in response run={}", run_id);
                return false;
            }
        };

        let deadline = Instant::now() + Duration::from_millis(retry_duration_ms);
        while Instant::now() < deadline {
            if let Some((st, sz, _px)) = self.get_order_status(&retry_oid).await {
                if is_done(&st) && sz > 0.0 {
                    return true;
                }
                if is_done(&st) {
                    return false;
                } // terminal but no fill
            }
            sleep(poll_interval).await;
        }
        false
    }

    /// Sell shares to unwind a one-sided position using FAK.
    ///
    /// Three-attempt strategy with decreasing floor — never goes below `min_price_floor`:
    ///   Attempt 1 — floor = max(buy_price × (1 − max_loss_pct/100),   min_price_floor), poll 600 ms.
    ///   Attempt 2 — floor = max(buy_price × (1 − (max_loss_pct+2)/100), min_price_floor), poll 600 ms.
    ///               200 ms pause before this attempt.
    ///   Attempt 3 — floor = max(buy_price × (1 − (max_loss_pct+5)/100), min_price_floor), poll 600 ms.
    ///               500 ms pause before this attempt.
    ///
    /// Each attempt fills at the best available bid as long as it's above the floor.
    /// If all three fail the position remains open and the caller reports UNWIND_FAILED.
    ///
    /// Returns true only when a fill is confirmed (filled_size > 0 in terminal state).
    async fn unwind_sell(
        &self,
        token_id: &str,
        shares: f64,
        buy_price: f64,
        neg_risk: bool,
        fee_rate_bps: u32,
        tick_size: &str,
        max_loss_pct: f64,
        min_price_floor: f64,
    ) -> bool {
        if shares <= 0.0 {
            return false;
        }

        // Attempt 1: conservative floor (configured max loss %)
        let floor1 = (buy_price * (1.0 - max_loss_pct / 100.0)).max(min_price_floor);
        if self
            .try_unwind_once(token_id, shares, floor1, neg_risk, fee_rate_bps, tick_size)
            .await
        {
            return true;
        }

        // Attempt 2: widen floor by +2 percentage points, pause 200 ms first
        sleep(Duration::from_millis(200)).await;
        let floor2 = (buy_price * (1.0 - (max_loss_pct + 2.0) / 100.0)).max(min_price_floor);
        error!(
            "unwind attempt 1 failed (floor={:.4}), retrying with wider floor={:.4} token={}",
            floor1, floor2, token_id
        );
        if self
            .try_unwind_once(token_id, shares, floor2, neg_risk, fee_rate_bps, tick_size)
            .await
        {
            return true;
        }

        // Attempt 3: widen floor by +5 percentage points, pause 500 ms first
        sleep(Duration::from_millis(500)).await;
        let floor3 = (buy_price * (1.0 - (max_loss_pct + 5.0) / 100.0)).max(min_price_floor);
        error!(
            "unwind attempt 2 failed (floor={:.4}), final attempt floor={:.4} token={}",
            floor2, floor3, token_id
        );
        self.try_unwind_once(token_id, shares, floor3, neg_risk, fee_rate_bps, tick_size)
            .await
    }

    /// Submit one FAK sell and poll up to 600 ms for a confirmed fill.
    /// Returns true only if filled_size > 0 in a terminal state.
    ///
    /// Retries sign+POST up to 3 times (100 ms / 200 ms backoff) to handle
    /// transient network errors or temporary exchange availability issues.
    async fn try_unwind_once(
        &self,
        token_id: &str,
        shares: f64,
        floor_price: f64,
        neg_risk: bool,
        fee_rate_bps: u32,
        tick_size: &str,
    ) -> bool {
        let sell_price = round_to_tick(floor_price, tick_size);

        // Retry sign+POST up to 3 times with backoff (handles transient errors).
        let mut oid: Option<String> = None;
        for post_attempt in 0..3u8 {
            if post_attempt > 0 {
                sleep(Duration::from_millis(100 * post_attempt as u64)).await;
            }
            let order = self.build_order(token_id, sell_price, shares, fee_rate_bps, SELL);
            let sig = match self.sign_order(&order, neg_risk) {
                Ok(s) => s,
                Err(e) => {
                    error!("unwind sign failed attempt={}: {}", post_attempt + 1, e);
                    return false; // sign failure is structural, not transient
                }
            };
            let body = self.build_post_body(&order, &sig, "FAK");
            match self.post_order(&body).await {
                Ok(v) => {
                    let id = extract_order_id(&v);
                    info!(
                        "unwind sell submitted: oid={:?} floor={:.4} shares={:.4} post_attempt={}",
                        id, floor_price, shares, post_attempt + 1,
                    );
                    if let Some(s) = id {
                        oid = Some(s);
                        break;
                    }
                    error!("unwind sell: no orderID in response attempt={}", post_attempt + 1);
                }
                Err(e) => {
                    error!("unwind sell POST failed attempt={}: {}", post_attempt + 1, e);
                }
            }
        }

        let oid = match oid {
            Some(s) => s,
            None => {
                error!("unwind sell: all POST attempts failed floor={:.4} token={}", floor_price, token_id);
                return false;
            }
        };

        // Poll up to 600 ms to confirm the FAK actually filled.
        let deadline = Instant::now() + Duration::from_millis(600);
        while Instant::now() < deadline {
            if let Some((state, filled, _price)) = self.get_order_status(&oid).await {
                if is_done(&state) {
                    if filled > 0.0 {
                        info!(
                            "unwind sell confirmed: filled={:.4} state={}",
                            filled, state
                        );
                        return true;
                    } else {
                        // FAK reached terminal state with no fill (no matching bids at floor).
                        error!(
                            "unwind sell: FAK terminal with 0 fill state={} floor={:.4}",
                            state, floor_price
                        );
                        return false;
                    }
                }
            }
            sleep(Duration::from_millis(25)).await;
        }

        // Timeout without terminal state — treat as failure (cancel defensively).
        let _ = self.cancel_orders(&[&oid]).await;
        error!("unwind sell: poll timeout, order cancelled oid={}", oid);
        false
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

        let (sig, recid) = self
            .signing_key
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

        // Diagnostic logging: capture signing context so any future "invalid signature"
        // response contains enough info to distinguish key/passphrase mismatch,
        // clock skew, or payload divergence.
        let body_sha256 = hex::encode(&Sha256::digest(body_str.as_bytes())[..8]);
        let hmac_prefix = &hmac_sig[..hmac_sig.len().min(12)];
        let key_suffix = &self.api_key[self.api_key.len().saturating_sub(6)..];
        info!(
            "post_order: ts={} method=POST path={} body_sha256_prefix={} \
             hmac_prefix={}… api_key_suffix=…{} passphrase_len={} signer={}",
            timestamp,
            path,
            body_sha256,
            hmac_prefix,
            key_suffix,
            self.api_passphrase.len(),
            self.signer_address,
        );

        let resp = self
            .http
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
        // Capture x-request-id before consuming the body so errors are traceable
        // on the CLOB side (useful when escalating to Polymarket support).
        let x_request_id = resp
            .headers()
            .get("x-request-id")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("-")
            .to_string();
        let resp_text = resp.text().await.unwrap_or_default();

        if !status.is_success() {
            error!(
                "post_order ERROR: status={} x-request-id={} ts={} signer={} resp={}",
                status,
                x_request_id,
                timestamp,
                self.signer_address,
                &resp_text[..resp_text.len().min(500)],
            );
            return Err(anyhow!(
                "CLOB POST returned {}: {}",
                status,
                &resp_text[..resp_text.len().min(500)]
            ));
        }

        let parsed: serde_json::Value = serde_json::from_str(&resp_text)
            .context("Failed to parse CLOB response JSON")?;
        let oid = parsed.get("orderID").or_else(|| parsed.get("id"))
            .and_then(|v| v.as_str()).unwrap_or("-");
        info!("post_order OK: status={} xrid={} ts={} order_id={} body={}",
            status, x_request_id, timestamp, oid, &resp_text[..resp_text.len().min(300)]);
        Ok(parsed)
    }

    /// GET /data/order/{id} — fetch order status + filled size.
    async fn get_order_status(&self, order_id: &str) -> Option<(String, f64, f64)> {
        let path = format!("/data/order/{}", order_id);
        let url = format!("{}{}", self.clob_base_url, &path);

        let timestamp = now_unix_secs();
        let hmac_sig = self.compute_hmac(&timestamp, "GET", &path, "").ok()?;

        let resp = self
            .http
            .get(&url)
            .header("POLY_ADDRESS", format!("{}", self.signer_address))
            .header("POLY_SIGNATURE", &hmac_sig)
            .header("POLY_TIMESTAMP", &timestamp)
            .header("POLY_API_KEY", &self.api_key)
            .header("POLY_PASSPHRASE", &self.api_passphrase)
            .send()
            .await
            .ok()?;

        if !resp.status().is_success() {
            return None;
        }

        let body: serde_json::Value = resp.json().await.ok()?;

        let status = body
            .get("status")
            .or_else(|| body.get("state"))
            .and_then(|v| v.as_str())
            .map(|s| s.to_uppercase())
            .unwrap_or_default();

        // Polymarket uses various field names for filled size
        let filled = [
            "sizeMatched",
            "filledSize",
            "matchedSize",
            "filled",
            "filled_size",
            "size_matched",
        ]
        .iter()
        .find_map(|&k| body.get(k))
        .and_then(|v| {
            v.as_f64()
                .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
        })
        .unwrap_or(0.0);

        let avg_price = ["avgPrice", "averagePrice", "price", "filledPrice"]
            .iter()
            .find_map(|&k| body.get(k))
            .and_then(|v| {
                v.as_f64()
                    .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
            })
            .unwrap_or(0.0);

        Some((status, filled, avg_price))
    }

    /// DELETE /orders — batch cancel orders by ID.
    async fn cancel_orders(&self, order_ids: &[&str]) -> Result<()> {
        if order_ids.is_empty() {
            return Ok(());
        }

        let path = "/orders";
        let url = format!("{}{}", self.clob_base_url, path);
        let body = serde_json::json!(order_ids);
        let body_str = serde_json::to_string(&body)?;

        let timestamp = now_unix_secs();
        let hmac_sig = self.compute_hmac(&timestamp, "DELETE", path, &body_str)?;

        let resp = self
            .http
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
            return Err(anyhow!(
                "cancel_orders returned {}: {}",
                status,
                &text[..text.len().min(300)]
            ));
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

fn is_done(status: &str) -> bool {
    matches!(
        status,
        "FILLED" | "CANCELED" | "CANCELLED" | "REJECTED" | "FAILED" | "EXPIRED"
    )
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
    let now_ns = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .ok()?
        .as_nanos() as u64;
    if now_ns < start_ns {
        return None;
    }
    Some((now_ns - start_ns) as f64 / 1_000_000.0)
}

fn make_error_result(
    run_id: &str,
    market_id: &str,
    reason: &str,
    sign_ms: f64,
    detect_to_sign_ms: f64,
) -> RustExecutionResult {
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
        detect_to_sign_ms,
        detect_to_submit_ms: detect_to_sign_ms,
        total_ms: sign_ms,
    }
}

#[allow(clippy::too_many_arguments)]
fn make_terminal_result(
    status: &str,
    run_id: &str,
    market_id: &str,
    yes_order_id: Option<String>,
    no_order_id: Option<String>,
    reason: String,
    reason_code: &str,
    yes_filled_size: f64,
    no_filled_size: f64,
    yes_target_price: f64,
    no_target_price: f64,
    yes_final_price: f64,
    no_final_price: f64,
    sign_ms: f64,
    submit_ms: f64,
    detect_to_sign_ms: f64,
    detect_to_submit_ms: f64,
    total_ms: f64,
) -> RustExecutionResult {
    RustExecutionResult {
        status: status.to_string(),
        run_id: run_id.to_string(),
        market_id: market_id.to_string(),
        yes_order_id,
        no_order_id,
        reason,
        reason_code: Some(reason_code.to_string()),
        yes_filled_size,
        no_filled_size,
        yes_target_price,
        no_target_price,
        yes_final_price,
        no_final_price,
        sign_ms,
        submit_ms,
        detect_to_sign_ms,
        detect_to_submit_ms,
        total_ms,
    }
}

fn estimate_net_edge_percent(
    yes_price: f64,
    no_price: f64,
    yes_fee_bps: u32,
    no_fee_bps: u32,
) -> f64 {
    let combined = yes_price + no_price;
    if combined <= 0.0 {
        return -100.0;
    }
    let yes_fee = yes_price * (yes_fee_bps as f64) / 10_000.0;
    let no_fee = no_price * (no_fee_bps as f64) / 10_000.0;
    let net = 1.0 - combined - yes_fee - no_fee;
    (net / combined) * 100.0
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
