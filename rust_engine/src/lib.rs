//! PyO3 module entry point for the Polymarket hot-path engine.
//!
//! Exposes `HotPathEngine`, `EngineConfig`, `RustArbOpportunity`, and
//! `RustExecutionResult` to Python via the `polymarket_engine` module.

pub mod cache;
pub mod detector;
pub mod executor;
pub mod orderbook;
pub mod types;
pub mod ws_client;

use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::PyString;
use tokio::sync::mpsc;
use tracing::{error, info, warn};

use cache::MetaCache;
use detector::detect_binary_arb;
use executor::FastExecutor;
use orderbook::OrderbookManager;
use types::{EngineConfig, MarketInfo, RustArbOpportunity, RustExecutionResult};
use ws_client::{run_ws_stream, WsEvent};

// ---------------------------------------------------------------------------
// HotPathEngine — the main class exposed to Python
// ---------------------------------------------------------------------------

#[pyclass]
pub struct HotPathEngine {
    config: EngineConfig,
    on_opportunity: Py<PyAny>,
    on_execution: Py<PyAny>,
    on_ws_event: Py<PyAny>,
    ob_manager: Arc<OrderbookManager>,
    meta_cache: Arc<MetaCache>,
}

#[pymethods]
impl HotPathEngine {
    #[new]
    fn new(
        config: EngineConfig,
        on_opportunity: Py<PyAny>,
        on_execution: Py<PyAny>,
        on_ws_event: Py<PyAny>,
    ) -> Self {
        let base_url = config.clob_base_url.clone();
        Self {
            config,
            on_opportunity,
            on_execution,
            on_ws_event,
            ob_manager: Arc::new(OrderbookManager::new()),
            meta_cache: Arc::new(MetaCache::new(&base_url)),
        }
    }

    /// Load markets from a JSON string. Returns the number of markets loaded.
    fn load_markets(&self, markets_json: &str) -> PyResult<usize> {
        let raw: Vec<serde_json::Value> = serde_json::from_str(markets_json)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Invalid JSON: {e}")))?;

        let mut markets = Vec::new();
        for val in &raw {
            if let Some(m) = parse_market_from_json(val) {
                markets.push(m);
            }
        }

        let count = markets.len();
        self.ob_manager.load_markets(markets);
        info!("lib: loaded {} markets into OrderbookManager", count);
        Ok(count)
    }

    /// Start the engine. Blocks the calling thread (releases the GIL).
    /// Spawns WebSocket connections, processes orderbook updates, runs
    /// detection, and calls Python callbacks on opportunities/executions.
    fn start(&self, py: Python<'_>) -> PyResult<()> {
        // Initialize tracing (best-effort, ignore if already set)
        let _ = tracing_subscriber::fmt()
            .with_env_filter("polymarket_engine=info")
            .try_init();

        // Clone everything we need to move into the async runtime
        let config = self.config.clone();
        let ob_manager = Arc::clone(&self.ob_manager);
        let meta_cache = Arc::clone(&self.meta_cache);
        // PyO3 0.24: Py<T> requires clone_ref(py) instead of clone()
        let on_opportunity = self.on_opportunity.clone_ref(py);
        let on_execution = self.on_execution.clone_ref(py);
        let on_ws_event = self.on_ws_event.clone_ref(py);

        // Release the GIL and run the tokio runtime
        py.allow_threads(move || {
            let rt = tokio::runtime::Builder::new_multi_thread()
                .enable_all()
                .worker_threads(4)
                .thread_name("pm-engine")
                .build()
                .expect("Failed to build tokio runtime");

            rt.block_on(async move {
                // Warm metadata cache
                let all_assets = ob_manager.get_all_asset_ids();
                if !all_assets.is_empty() {
                    info!("lib: warming MetaCache for {} assets", all_assets.len());
                    meta_cache.warm(&all_assets).await;
                }

                // Build executor (if credentials present)
                let fast_executor = if !config.private_key.is_empty()
                    && !config.private_key.contains("XXXXXX")
                {
                    match FastExecutor::new(&config) {
                        Ok(exec) => {
                            info!("lib: FastExecutor initialized");
                            Some(Arc::new(exec))
                        }
                        Err(e) => {
                            error!("lib: FastExecutor init failed: {} — running detection-only", e);
                            None
                        }
                    }
                } else {
                    warn!("lib: no private key configured — detection-only mode");
                    None
                };

                // Set up WS event channel
                let (event_tx, mut event_rx) = mpsc::unbounded_channel::<WsEvent>();

                // Spawn WS connections
                let ws_url = config.ws_url.clone();
                let ws_assets = ob_manager.get_all_asset_ids();
                let ws_obm = Arc::clone(&ob_manager);

                tokio::spawn(async move {
                    run_ws_stream(&ws_url, ws_assets, ws_obm, event_tx).await;
                });

                info!("lib: main event loop started");

                // Main event loop — process WS events
                while let Some(event) = event_rx.recv().await {
                    match event {
                        WsEvent::BookUpdate { asset_id } => {
                            // Look up which market this asset belongs to
                            let market = match ob_manager.get_market_for_asset(&asset_id) {
                                Some(m) => m,
                                None => continue,
                            };

                            // Get both sides of the orderbook
                            let (yes_book, no_book) = match ob_manager.get_market_books(&market) {
                                Some(books) => books,
                                None => continue,
                            };

                            // Compute book ages
                            let yes_age = yes_book.last_update.elapsed().as_secs_f64();
                            let no_age = no_book.last_update.elapsed().as_secs_f64();

                            // Run detection
                            let opp = detect_binary_arb(
                                &market,
                                &yes_book.asks,
                                &no_book.asks,
                                &yes_book.bids,
                                &no_book.bids,
                                yes_age,
                                no_age,
                                config.target_size_usd,
                                config.min_edge_percent,
                                config.min_liquidity_usd,
                                config.buffer_liquid_percent,
                                config.buffer_illiquid_percent,
                                config.illiquid_threshold_usd,
                            );

                            if let Some(opportunity) = opp {
                                // Call Python on_opportunity callback
                                let cb_opp = opportunity.clone();
                                Python::with_gil(|py| {
                                    if let Err(e) = on_opportunity.call1(py, (cb_opp,)) {
                                        error!("on_opportunity callback error: {}", e);
                                    }
                                });

                                // If executor is available and verdict is ACTIONABLE, execute
                                if opportunity.verdict == "ACTIONABLE" {
                                    if let Some(ref exec) = fast_executor {
                                        let exec = Arc::clone(exec);
                                        let cfg = config.clone();
                                        let mc = Arc::clone(&meta_cache);
                                        // Clone the callback ref under GIL for the spawned task
                                        let cb_exec = Python::with_gil(|py| {
                                            on_execution.clone_ref(py)
                                        });

                                        tokio::spawn(async move {
                                            let result = exec.execute(&opportunity, &cfg, &mc).await;
                                            Python::with_gil(|py| {
                                                if let Err(e) = cb_exec.call1(py, (result,)) {
                                                    error!("on_execution callback error: {}", e);
                                                }
                                            });
                                        });
                                    }
                                }
                            }
                        }
                        WsEvent::Connected => {
                            Python::with_gil(|py| {
                                let _ = on_ws_event.call1(
                                    py,
                                    (
                                        PyString::new(py, "connected"),
                                        PyString::new(py, ""),
                                    ),
                                );
                            });
                        }
                        WsEvent::Disconnected => {
                            Python::with_gil(|py| {
                                let _ = on_ws_event.call1(
                                    py,
                                    (
                                        PyString::new(py, "disconnected"),
                                        PyString::new(py, ""),
                                    ),
                                );
                            });
                        }
                        WsEvent::Error(detail) => {
                            Python::with_gil(|py| {
                                let _ = on_ws_event.call1(
                                    py,
                                    (
                                        PyString::new(py, "error"),
                                        PyString::new(py, &detail),
                                    ),
                                );
                            });
                        }
                    }
                }

                warn!("lib: event loop ended — all WS senders dropped");
            });
        });

        Ok(())
    }

    /// Get engine statistics: (tracked_assets, total_markets).
    fn get_stats(&self) -> PyResult<(usize, usize)> {
        Ok((
            self.ob_manager.tracked_assets(),
            self.ob_manager.total_markets(),
        ))
    }
}

// ---------------------------------------------------------------------------
// JSON market parser
// ---------------------------------------------------------------------------

fn parse_market_from_json(val: &serde_json::Value) -> Option<MarketInfo> {
    let id = val.get("id")?.as_str()?.to_string();
    let condition_id = val.get("condition_id")
        .or_else(|| val.get("conditionId"))
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let question = val.get("question").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let slug = val.get("slug").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let end_date = val.get("end_date")
        .or_else(|| val.get("endDate"))
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();

    // Tokens array: [0] = YES, [1] = NO
    let tokens = val.get("tokens")?.as_array()?;
    if tokens.len() < 2 {
        return None;
    }
    let yes_token = tokens[0].get("token_id")
        .or_else(|| tokens[0].get("tokenId"))
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let no_token = tokens[1].get("token_id")
        .or_else(|| tokens[1].get("tokenId"))
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();

    if yes_token.is_empty() || no_token.is_empty() {
        return None;
    }

    let volume = val
        .get("volume")
        .and_then(|v| v.as_f64().or_else(|| v.as_str().and_then(|s| s.parse().ok())))
        .unwrap_or(0.0);
    let liquidity = val
        .get("liquidity")
        .and_then(|v| v.as_f64().or_else(|| v.as_str().and_then(|s| s.parse().ok())))
        .unwrap_or(0.0);

    // Detect crypto 15-min markets by slug pattern
    let is_crypto_15min = slug.contains("-15-min")
        || slug.contains("15min")
        || val
            .get("is_crypto_15min")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);

    Some(MarketInfo {
        id,
        condition_id,
        question,
        slug,
        end_date,
        is_crypto_15min,
        yes_token,
        no_token,
        volume,
        liquidity,
    })
}

// ---------------------------------------------------------------------------
// PyO3 module definition
// ---------------------------------------------------------------------------

#[pymodule]
fn polymarket_engine(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<HotPathEngine>()?;
    m.add_class::<EngineConfig>()?;
    m.add_class::<RustArbOpportunity>()?;
    m.add_class::<RustExecutionResult>()?;
    Ok(())
}
