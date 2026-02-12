//! In-memory orderbook state management using DashMap for lock-free access.

use dashmap::DashMap;
use std::time::Instant;

use crate::types::{Level, MarketInfo};

const MAX_LEVELS: usize = 10;

/// Per-asset orderbook state.
#[derive(Debug, Clone)]
pub struct BookState {
    pub bids: Vec<Level>,
    pub asks: Vec<Level>,
    pub last_update: Instant,
}

impl Default for BookState {
    fn default() -> Self {
        Self {
            bids: Vec::new(),
            asks: Vec::new(),
            last_update: Instant::now(),
        }
    }
}

/// Thread-safe orderbook manager.
pub struct OrderbookManager {
    books: DashMap<String, BookState>,
    /// asset_id -> market_id
    asset_to_market: DashMap<String, String>,
    /// market_id -> MarketInfo
    markets: DashMap<String, MarketInfo>,
}

impl OrderbookManager {
    pub fn new() -> Self {
        Self {
            books: DashMap::with_capacity(2048),
            asset_to_market: DashMap::with_capacity(4096),
            markets: DashMap::with_capacity(2048),
        }
    }

    /// Load (or reload) market definitions.
    pub fn load_markets(&self, markets: Vec<MarketInfo>) {
        self.markets.clear();
        self.asset_to_market.clear();
        for m in markets {
            self.asset_to_market
                .insert(m.yes_token.clone(), m.id.clone());
            self.asset_to_market
                .insert(m.no_token.clone(), m.id.clone());
            self.markets.insert(m.id.clone(), m);
        }
    }

    /// Update orderbook for an asset.
    /// Handles both full snapshots and top-of-book deltas.
    pub fn update(&self, asset_id: &str, bids: Vec<Level>, asks: Vec<Level>) {
        let is_top_only = bids.len() <= 1
            && asks.len() <= 1
            && bids.iter().all(|l| l.size == 0.0)
            && asks.iter().all(|l| l.size == 0.0);

        if is_top_only {
            // Merge into existing book
            if let Some(mut entry) = self.books.get_mut(asset_id) {
                if let Some(new_bid) = bids.first() {
                    if new_bid.price > 0.0 {
                        // Replace or insert at top
                        let old_size = entry
                            .bids
                            .first()
                            .filter(|b| (b.price - new_bid.price).abs() < 1e-9)
                            .map(|b| b.size)
                            .unwrap_or(0.0);
                        entry.bids.retain(|b| (b.price - new_bid.price).abs() >= 1e-9);
                        entry.bids.insert(
                            0,
                            Level {
                                price: new_bid.price,
                                size: old_size,
                            },
                        );
                        entry.bids.sort_by(|a, b| b.price.partial_cmp(&a.price).unwrap());
                        entry.bids.truncate(MAX_LEVELS);
                    }
                }
                if let Some(new_ask) = asks.first() {
                    if new_ask.price > 0.0 {
                        let old_size = entry
                            .asks
                            .first()
                            .filter(|a| (a.price - new_ask.price).abs() < 1e-9)
                            .map(|a| a.size)
                            .unwrap_or(0.0);
                        entry.asks.retain(|a| (a.price - new_ask.price).abs() >= 1e-9);
                        entry.asks.insert(
                            0,
                            Level {
                                price: new_ask.price,
                                size: old_size,
                            },
                        );
                        entry.asks.sort_by(|a, b| a.price.partial_cmp(&b.price).unwrap());
                        entry.asks.truncate(MAX_LEVELS);
                    }
                }
                entry.last_update = Instant::now();
                return;
            }
        }

        // Full replacement
        let mut sorted_bids = bids;
        let mut sorted_asks = asks;
        sorted_bids.sort_by(|a, b| b.price.partial_cmp(&a.price).unwrap());
        sorted_asks.sort_by(|a, b| a.price.partial_cmp(&b.price).unwrap());
        sorted_bids.truncate(MAX_LEVELS);
        sorted_asks.truncate(MAX_LEVELS);

        self.books.insert(
            asset_id.to_string(),
            BookState {
                bids: sorted_bids,
                asks: sorted_asks,
                last_update: Instant::now(),
            },
        );
    }

    /// Get the market associated with an asset ID.
    pub fn get_market_for_asset(&self, asset_id: &str) -> Option<MarketInfo> {
        let mid = self.asset_to_market.get(asset_id)?;
        self.markets.get(mid.value()).map(|m| m.value().clone())
    }

    /// Get both books for a market. Returns (yes_book, no_book).
    pub fn get_market_books(&self, market: &MarketInfo) -> Option<(BookState, BookState)> {
        let yes = self.books.get(&market.yes_token)?.value().clone();
        let no = self.books.get(&market.no_token)?.value().clone();
        Some((yes, no))
    }

    /// Get all tracked asset IDs.
    pub fn get_all_asset_ids(&self) -> Vec<String> {
        self.asset_to_market.iter().map(|e| e.key().clone()).collect()
    }

    pub fn tracked_assets(&self) -> usize {
        self.books.len()
    }

    pub fn total_markets(&self) -> usize {
        self.markets.len()
    }
}

    /// Alias: full book replacement (used by ws_client).
    pub fn update_full(&self, asset_id: &str, bids: Vec<Level>, asks: Vec<Level>) {
        // Force full replacement by providing levels with non-zero sizes
        let mut sorted_bids = bids;
        let mut sorted_asks = asks;
        sorted_bids.sort_by(|a, b| b.price.partial_cmp(&a.price).unwrap());
        sorted_asks.sort_by(|a, b| a.price.partial_cmp(&b.price).unwrap());
        sorted_bids.truncate(MAX_LEVELS);
        sorted_asks.truncate(MAX_LEVELS);

        self.books.insert(
            asset_id.to_string(),
            BookState {
                bids: sorted_bids,
                asks: sorted_asks,
                last_update: Instant::now(),
            },
        );
    }
