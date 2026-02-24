//! Shared types for the Polymarket hot-path engine.

use pyo3::prelude::*;
use serde::{Deserialize, Serialize};

/// Cached market metadata needed for order construction.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TokenMeta {
    pub token_id: String,
    pub tick_size: String,
    pub neg_risk: bool,
    pub fee_rate_bps: u32,
}

/// Market info as loaded from Gamma API.
#[derive(Debug, Clone)]
pub struct MarketInfo {
    pub id: String,
    pub condition_id: String,
    pub question: String,
    pub slug: String,
    pub end_date: String,
    pub is_crypto_15min: bool,
    pub yes_token: String,
    pub no_token: String,
    pub volume: f64,
    pub liquidity: f64,
}

/// A single price level in an orderbook.
#[derive(Debug, Clone, Copy)]
pub struct Level {
    pub price: f64,
    pub size: f64,
}

/// Detected arbitrage opportunity exposed to Python via PyO3.
#[derive(Debug, Clone)]
#[pyclass]
pub struct RustArbOpportunity {
    #[pyo3(get)]
    pub market_id: String,
    #[pyo3(get)]
    pub market_question: String,
    #[pyo3(get)]
    pub condition_id: String,
    #[pyo3(get)]
    pub slug: String,
    #[pyo3(get)]
    pub end_date: String,
    #[pyo3(get)]
    pub yes_token_id: String,
    #[pyo3(get)]
    pub no_token_id: String,
    #[pyo3(get)]
    pub is_crypto_15min: bool,
    #[pyo3(get)]
    pub yes_best_ask: f64,
    #[pyo3(get)]
    pub no_best_ask: f64,
    #[pyo3(get)]
    pub combined_best_asks: f64,
    #[pyo3(get)]
    pub yes_ask_vwap: f64,
    #[pyo3(get)]
    pub no_ask_vwap: f64,
    #[pyo3(get)]
    pub combined_cost: f64,
    #[pyo3(get)]
    pub gross_edge: f64,
    #[pyo3(get)]
    pub gross_edge_percent: f64,
    #[pyo3(get)]
    pub net_edge: f64,
    #[pyo3(get)]
    pub net_edge_percent: f64,
    #[pyo3(get)]
    pub one_share_net_profit: f64,
    #[pyo3(get)]
    pub one_share_net_edge_percent: f64,
    #[pyo3(get)]
    pub size_usd: f64,
    #[pyo3(get)]
    pub yes_liquidity: f64,
    #[pyo3(get)]
    pub no_liquidity: f64,
    #[pyo3(get)]
    pub max_safe_size: f64,
    #[pyo3(get)]
    pub yes_book_age_s: f64,
    #[pyo3(get)]
    pub no_book_age_s: f64,
    #[pyo3(get)]
    pub verdict: String,
    #[pyo3(get)]
    pub t_detect_ns: u64,
}

/// Result of a two-leg execution attempt.
#[derive(Debug, Clone)]
#[pyclass]
pub struct RustExecutionResult {
    #[pyo3(get)]
    pub status: String,
    #[pyo3(get)]
    pub run_id: String,
    #[pyo3(get)]
    pub market_id: String,
    #[pyo3(get)]
    pub yes_order_id: Option<String>,
    #[pyo3(get)]
    pub no_order_id: Option<String>,
    #[pyo3(get)]
    pub reason: String,
    #[pyo3(get)]
    pub reason_code: Option<String>,
    #[pyo3(get)]
    pub yes_filled_size: f64,
    #[pyo3(get)]
    pub no_filled_size: f64,
    #[pyo3(get)]
    pub yes_target_price: f64,
    #[pyo3(get)]
    pub no_target_price: f64,
    #[pyo3(get)]
    pub yes_final_price: f64,
    #[pyo3(get)]
    pub no_final_price: f64,
    #[pyo3(get)]
    pub sign_ms: f64,
    #[pyo3(get)]
    pub submit_ms: f64,
    #[pyo3(get)]
    pub detect_to_sign_ms: f64,
    #[pyo3(get)]
    pub detect_to_submit_ms: f64,
    #[pyo3(get)]
    pub total_ms: f64,
}

/// Configuration for the Rust engine, passed from Python.
#[derive(Debug, Clone)]
#[pyclass]
pub struct EngineConfig {
    #[pyo3(get, set)]
    pub clob_base_url: String,
    #[pyo3(get, set)]
    pub ws_url: String,
    #[pyo3(get, set)]
    pub private_key: String,
    #[pyo3(get, set)]
    pub funder_address: String,
    #[pyo3(get, set)]
    pub api_key: String,
    #[pyo3(get, set)]
    pub api_secret: String,
    #[pyo3(get, set)]
    pub api_passphrase: String,
    #[pyo3(get, set)]
    pub chain_id: u64,
    #[pyo3(get, set)]
    pub signature_type: u32,
    #[pyo3(get, set)]
    pub min_edge_percent: f64,
    #[pyo3(get, set)]
    pub min_liquidity_usd: f64,
    #[pyo3(get, set)]
    pub buffer_liquid_percent: f64,
    #[pyo3(get, set)]
    pub buffer_illiquid_percent: f64,
    #[pyo3(get, set)]
    pub illiquid_threshold_usd: f64,
    #[pyo3(get, set)]
    pub target_size_usd: f64,
    #[pyo3(get, set)]
    pub cross_bps: f64,
    #[pyo3(get, set)]
    pub min_order_usd: f64,
    #[pyo3(get, set)]
    pub min_order_shares: f64,
    #[pyo3(get, set)]
    pub min_execution_edge_percent: f64,
    #[pyo3(get, set)]
    pub max_edge_decay_bps: f64,
    #[pyo3(get, set)]
    pub trading_enabled: bool,
    #[pyo3(get, set)]
    pub trading_control_file: String,
    #[pyo3(get, set)]
    pub panic_partial_count: u32,
    #[pyo3(get, set)]
    pub panic_partial_window_s: u64,
    // Safety / complete-or-abort parameters
    #[pyo3(get, set)]
    pub wait_for_both_ms: u64,
    #[pyo3(get, set)]
    pub poll_interval_ms: u64,
    #[pyo3(get, set)]
    pub retry_duration_ms: u64,
    #[pyo3(get, set)]
    pub retry_slippage_pct: f64,
    #[pyo3(get, set)]
    pub unwind_max_loss_pct: f64,
    /// Minimum sell price floor for all unwind attempts.
    /// The bot will never submit a sell order below this price, regardless of how many
    /// retries have been attempted. Default: 0.10 ($0.10 per share).
    #[pyo3(get, set)]
    pub unwind_min_price_floor: f64,
    #[pyo3(get, set)]
    pub exec_cooldown_ms: u64,
}

#[pymethods]
impl EngineConfig {
    #[new]
    pub fn new() -> Self {
        Self {
            clob_base_url: "https://clob.polymarket.com".into(),
            ws_url: "wss://ws-subscriptions-clob.polymarket.com/ws/market".into(),
            private_key: String::new(),
            funder_address: String::new(),
            api_key: String::new(),
            api_secret: String::new(),
            api_passphrase: String::new(),
            chain_id: 137,
            signature_type: 0,
            min_edge_percent: 0.5,
            min_liquidity_usd: 100.0,
            buffer_liquid_percent: 0.5,
            buffer_illiquid_percent: 1.0,
            illiquid_threshold_usd: 1000.0,
            target_size_usd: 100.0,
            cross_bps: 5.0,
            min_order_usd: 1.0,
            min_order_shares: 5.0,
            min_execution_edge_percent: 0.5,
            max_edge_decay_bps: 25.0,
            trading_enabled: true,
            trading_control_file: String::new(),
            panic_partial_count: 3,
            panic_partial_window_s: 600,
            wait_for_both_ms: 500,
            poll_interval_ms: 50,
            retry_duration_ms: 200,
            retry_slippage_pct: 1.5,
            unwind_max_loss_pct: 3.0,
            unwind_min_price_floor: 0.10,
            exec_cooldown_ms: 15_000,
        }
    }
}
