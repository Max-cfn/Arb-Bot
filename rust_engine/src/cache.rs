//! Pre-warm cache for tick_size, neg_risk, fee_rate_bps per token.
//! These are fetched once at startup and refreshed periodically.

use dashmap::DashMap;
use reqwest::Client;
use serde::Deserialize;
use tracing::info;

use crate::types::TokenMeta;

#[derive(Deserialize)]
struct TickSizeResp {
    minimum_tick_size: String,
}

#[derive(Deserialize)]
struct NegRiskResp {
    neg_risk: bool,
}

#[derive(Deserialize)]
struct FeeRateResp {
    #[serde(default)]
    base_fee: Option<u32>,
    #[serde(default)]
    fee_rate_bps: Option<u32>,
}

/// Shared cache accessible from all hot-path threads.
pub struct MetaCache {
    cache: DashMap<String, TokenMeta>,
    http: Client,
    base_url: String,
}

impl MetaCache {
    pub fn new(base_url: &str) -> Self {
        let http = Client::builder()
            .pool_max_idle_per_host(10)
            .tcp_keepalive(Some(std::time::Duration::from_secs(30)))
            .timeout(std::time::Duration::from_secs(5))
            .build()
            .expect("Failed to build HTTP client");

        Self {
            cache: DashMap::with_capacity(2048),
            http,
            base_url: base_url.trim_end_matches('/').to_string(),
        }
    }

    /// Get cached metadata, or None if not warmed.
    pub fn get(&self, token_id: &str) -> Option<TokenMeta> {
        self.cache.get(token_id).map(|v| v.clone())
    }

    /// Pre-warm cache for a list of token IDs (batch, parallel).
    pub async fn warm(&self, token_ids: &[String]) {
        let mut tasks = Vec::new();

        for tid in token_ids {
            if self.cache.contains_key(tid) {
                continue;
            }
            let tid = tid.clone();
            let base = self.base_url.clone();
            let http = self.http.clone();

            tasks.push(tokio::spawn(async move {
                let tick = fetch_tick_size(&http, &base, &tid).await;
                let neg = fetch_neg_risk(&http, &base, &tid).await;
                let fee = fetch_fee_rate(&http, &base, &tid).await;
                (
                    tid.clone(),
                    TokenMeta {
                        token_id: tid,
                        tick_size: tick.unwrap_or_else(|| "0.01".into()),
                        neg_risk: neg.unwrap_or(false),
                        fee_rate_bps: fee.unwrap_or(0),
                    },
                )
            }));
        }

        let mut warmed = 0u32;
        for task in tasks {
            if let Ok((tid, meta)) = task.await {
                self.cache.insert(tid, meta);
                warmed += 1;
            }
        }
        info!("MetaCache: warmed {} tokens", warmed);
    }
}

async fn fetch_tick_size(http: &Client, base: &str, token_id: &str) -> Option<String> {
    let url = format!("{}/tick-size?token_id={}", base, token_id);
    let resp = http.get(&url).send().await.ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let data: TickSizeResp = resp.json().await.ok()?;
    Some(data.minimum_tick_size)
}

async fn fetch_neg_risk(http: &Client, base: &str, token_id: &str) -> Option<bool> {
    let url = format!("{}/neg-risk?token_id={}", base, token_id);
    let resp = http.get(&url).send().await.ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let data: NegRiskResp = resp.json().await.ok()?;
    Some(data.neg_risk)
}

async fn fetch_fee_rate(http: &Client, base: &str, token_id: &str) -> Option<u32> {
    let url = format!("{}/fee-rate?token_id={}", base, token_id);
    let resp = http.get(&url).send().await.ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let data: FeeRateResp = resp.json().await.ok()?;
    data.base_fee.or(data.fee_rate_bps)
}
