//! WebSocket streaming client for Polymarket CLOB orderbook data.
//! Connects to wss://ws-subscriptions-clob.polymarket.com/ws/market,
//! subscribes to "book" channel, and feeds updates into OrderbookManager.

use std::sync::Arc;
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use tokio::sync::mpsc;
use tokio::time::sleep;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message;
use tracing::{error, info, warn};

use crate::orderbook::OrderbookManager;
use crate::types::Level;

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/// Events emitted by the WebSocket layer for the main loop to consume.
#[derive(Debug, Clone)]
pub enum WsEvent {
    /// An orderbook was updated for the given asset.
    BookUpdate { asset_id: String },
    /// Connection established.
    Connected,
    /// Connection lost.
    Disconnected,
    /// Non-fatal error.
    Error(String),
}

// ---------------------------------------------------------------------------
// Internal WS message types
// ---------------------------------------------------------------------------

#[derive(Deserialize, Debug)]
struct WsMessage {
    event_type: Option<String>,
    // book snapshot fields
    asset_id: Option<String>,
    bids: Option<Vec<WsLevel>>,
    asks: Option<Vec<WsLevel>>,
    // price_change fields
    price_changes: Option<Vec<PriceChange>>,
}

#[derive(Deserialize, Debug)]
struct WsLevel {
    price: String,
    size: String,
}

#[derive(Deserialize, Debug)]
struct PriceChange {
    asset_id: String,
    best_bid: Option<String>,
    best_ask: Option<String>,
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/// Maximum assets per WebSocket connection (Polymarket limit).
const MAX_ASSETS_PER_WS: usize = 250;

/// Delay between staggered connection starts.
const STAGGER_DELAY_MS: u64 = 500;

/// Maximum reconnection attempts before giving up on a chunk.
const MAX_RECONNECT_ATTEMPTS: u32 = 10;

/// Base delay for exponential backoff reconnect.
const RECONNECT_BASE_DELAY_MS: u64 = 2000;

// ---------------------------------------------------------------------------
// Public entry point
// ---------------------------------------------------------------------------

/// Spawn WebSocket connections for all asset IDs, chunked into groups of 250.
/// Each connection feeds orderbook updates into `ob_manager` and emits `WsEvent`s
/// on the provided `event_tx` channel.
pub async fn run_ws_stream(
    ws_url: &str,
    asset_ids: Vec<String>,
    ob_manager: Arc<OrderbookManager>,
    event_tx: mpsc::UnboundedSender<WsEvent>,
) {
    if asset_ids.is_empty() {
        warn!("ws_client: no asset IDs to subscribe — doing nothing");
        return;
    }

    let chunks: Vec<Vec<String>> = asset_ids
        .chunks(MAX_ASSETS_PER_WS)
        .map(|c| c.to_vec())
        .collect();

    info!(
        "ws_client: spawning {} WS connection(s) for {} assets",
        chunks.len(),
        asset_ids.len()
    );

    let mut handles = Vec::new();

    for (i, chunk) in chunks.into_iter().enumerate() {
        let url = ws_url.to_string();
        let obm = Arc::clone(&ob_manager);
        let tx = event_tx.clone();

        let handle = tokio::spawn(async move {
            // Stagger connection starts
            if i > 0 {
                sleep(Duration::from_millis(STAGGER_DELAY_MS * i as u64)).await;
            }
            run_single_ws(url, chunk, obm, tx, i).await;
        });
        handles.push(handle);
    }

    // Wait for all connections (they reconnect internally; this blocks forever
    // under normal operation).
    for h in handles {
        let _ = h.await;
    }
}

// ---------------------------------------------------------------------------
// Single WS connection with reconnection logic
// ---------------------------------------------------------------------------

async fn run_single_ws(
    ws_url: String,
    asset_ids: Vec<String>,
    ob_manager: Arc<OrderbookManager>,
    event_tx: mpsc::UnboundedSender<WsEvent>,
    conn_idx: usize,
) {
    let mut attempt: u32 = 0;

    loop {
        attempt += 1;
        if attempt > MAX_RECONNECT_ATTEMPTS {
            error!(
                "ws[{}]: exceeded {} reconnect attempts — giving up",
                conn_idx, MAX_RECONNECT_ATTEMPTS
            );
            let _ = event_tx.send(WsEvent::Error(format!(
                "ws[{}]: max reconnects exceeded",
                conn_idx
            )));
            return;
        }

        info!(
            "ws[{}]: connecting (attempt {}/{}) to {} with {} assets",
            conn_idx,
            attempt,
            MAX_RECONNECT_ATTEMPTS,
            ws_url,
            asset_ids.len()
        );

        match connect_async(&ws_url).await {
            Ok((ws_stream, _response)) => {
                let _ = event_tx.send(WsEvent::Connected);
                attempt = 0; // Reset on successful connect

                let (mut write, mut read) = ws_stream.split();

                // Subscribe to book channel
                let subscribe_msg = serde_json::json!({
                    "type": "subscribe",
                    "assets_ids": asset_ids,
                    "channels": ["book"]
                });

                if let Err(e) = write.send(Message::Text(subscribe_msg.to_string().into())).await {
                    error!("ws[{}]: failed to send subscribe: {}", conn_idx, e);
                    let _ = event_tx.send(WsEvent::Error(format!("subscribe failed: {}", e)));
                    backoff_sleep(attempt).await;
                    continue;
                }

                info!("ws[{}]: subscribed to {} assets", conn_idx, asset_ids.len());

                // Read loop
                while let Some(msg_result) = read.next().await {
                    match msg_result {
                        Ok(Message::Text(text)) => {
                            process_message(&text, &ob_manager, &event_tx);
                        }
                        Ok(Message::Binary(data)) => {
                            if let Ok(text) = String::from_utf8(data.into()) {
                                process_message(&text, &ob_manager, &event_tx);
                            }
                        }
                        Ok(Message::Ping(payload)) => {
                            let _ = write.send(Message::Pong(payload)).await;
                        }
                        Ok(Message::Close(_)) => {
                            warn!("ws[{}]: server sent close frame", conn_idx);
                            break;
                        }
                        Err(e) => {
                            error!("ws[{}]: read error: {}", conn_idx, e);
                            break;
                        }
                        _ => {}
                    }
                }

                let _ = event_tx.send(WsEvent::Disconnected);
                warn!("ws[{}]: disconnected — will reconnect", conn_idx);
            }
            Err(e) => {
                error!("ws[{}]: connect failed: {}", conn_idx, e);
                let _ = event_tx.send(WsEvent::Error(format!("connect failed: {}", e)));
            }
        }

        backoff_sleep(attempt).await;
    }
}

// ---------------------------------------------------------------------------
// Message processing
// ---------------------------------------------------------------------------

fn process_message(
    text: &str,
    ob_manager: &Arc<OrderbookManager>,
    event_tx: &mpsc::UnboundedSender<WsEvent>,
) {
    // Try parsing as a JSON array first (Polymarket sometimes batches messages)
    if text.starts_with('[') {
        if let Ok(msgs) = serde_json::from_str::<Vec<WsMessage>>(text) {
            for msg in msgs {
                handle_ws_message(msg, ob_manager, event_tx);
            }
            return;
        }
    }

    // Single message
    match serde_json::from_str::<WsMessage>(text) {
        Ok(msg) => handle_ws_message(msg, ob_manager, event_tx),
        Err(_) => {
            // Ignore unparseable messages (heartbeats, acks, etc.)
        }
    }
}

fn handle_ws_message(
    msg: WsMessage,
    ob_manager: &Arc<OrderbookManager>,
    event_tx: &mpsc::UnboundedSender<WsEvent>,
) {
    let event_type = msg.event_type.as_deref().unwrap_or("");

    match event_type {
        "book" => {
            if let Some(asset_id) = &msg.asset_id {
                let bids = parse_levels(&msg.bids);
                let asks = parse_levels(&msg.asks);

                // Full book snapshot → use update_full
                ob_manager.update_full(asset_id, bids, asks);
                let _ = event_tx.send(WsEvent::BookUpdate {
                    asset_id: asset_id.clone(),
                });
            }
        }
        "price_change" => {
            if let Some(changes) = &msg.price_changes {
                for pc in changes {
                    let mut bids = Vec::new();
                    let mut asks = Vec::new();

                    if let Some(ref bb) = pc.best_bid {
                        if let Ok(p) = bb.parse::<f64>() {
                            bids.push(Level { price: p, size: 0.0 });
                        }
                    }
                    if let Some(ref ba) = pc.best_ask {
                        if let Ok(p) = ba.parse::<f64>() {
                            asks.push(Level { price: p, size: 0.0 });
                        }
                    }

                    // Top-of-book delta → use update (merge logic)
                    ob_manager.update(&pc.asset_id, bids, asks);
                    let _ = event_tx.send(WsEvent::BookUpdate {
                        asset_id: pc.asset_id.clone(),
                    });
                }
            }
        }
        _ => {
            // Ignore unknown event types (heartbeat, subscription ack, etc.)
        }
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn parse_levels(levels: &Option<Vec<WsLevel>>) -> Vec<Level> {
    match levels {
        Some(lvls) => lvls
            .iter()
            .filter_map(|l| {
                let price = l.price.parse::<f64>().ok()?;
                let size = l.size.parse::<f64>().ok()?;
                Some(Level { price, size })
            })
            .collect(),
        None => Vec::new(),
    }
}

async fn backoff_sleep(attempt: u32) {
    let delay = Duration::from_millis(RECONNECT_BASE_DELAY_MS * (1u64 << attempt.min(5)));
    info!("ws: backoff sleep {}ms before reconnect", delay.as_millis());
    sleep(delay).await;
}
