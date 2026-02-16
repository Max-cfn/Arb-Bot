use std::collections::HashMap;
use std::env;
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use reqwest::Client;
use serde::Deserialize;
use tokio::sync::mpsc;
use tracing::{error, info, warn};

use polymarket_engine::cache::MetaCache;
use polymarket_engine::detector::detect_binary_arb;
use polymarket_engine::executor::FastExecutor;
use polymarket_engine::orderbook::OrderbookManager;
use polymarket_engine::types::{EngineConfig, MarketInfo};
use polymarket_engine::ws_client::{run_ws_stream, WsEvent};

#[derive(Debug, Deserialize)]
struct GammaToken {
    #[serde(rename = "token_id")]
    token_id: Option<String>,
    #[serde(rename = "tokenId")]
    token_id_alt: Option<String>,
}

#[derive(Debug, Deserialize)]
struct GammaMarket {
    id: Option<String>,
    #[serde(rename = "conditionId")]
    condition_id: Option<String>,
    question: Option<String>,
    slug: Option<String>,
    #[serde(rename = "endDate")]
    end_date: Option<String>,
    tokens: Option<Vec<GammaToken>>,
    #[serde(rename = "clobTokenIds")]
    clob_token_ids: Option<serde_json::Value>,
    volume: Option<serde_json::Value>,
    liquidity: Option<serde_json::Value>,
}

fn json_num(v: Option<serde_json::Value>) -> f64 {
    match v {
        Some(serde_json::Value::Number(n)) => n.as_f64().unwrap_or(0.0),
        Some(serde_json::Value::String(s)) => s.parse::<f64>().unwrap_or(0.0),
        _ => 0.0,
    }
}

fn parse_clob_ids(v: Option<serde_json::Value>) -> Vec<String> {
    match v {
        Some(serde_json::Value::Array(arr)) => arr
            .into_iter()
            .filter_map(|x| match x {
                serde_json::Value::String(s) => Some(s),
                serde_json::Value::Number(n) => Some(n.to_string()),
                _ => None,
            })
            .collect(),
        Some(serde_json::Value::String(s)) => {
            let trimmed = s.trim().trim_start_matches('[').trim_end_matches(']');
            trimmed
                .split(',')
                .map(|x| x.trim().trim_matches('"').to_string())
                .filter(|x| !x.is_empty())
                .collect()
        }
        _ => vec![],
    }
}

fn getenv_parse<T: std::str::FromStr>(k: &str, default: T) -> T {
    env::var(k)
        .ok()
        .and_then(|v| v.parse::<T>().ok())
        .unwrap_or(default)
}

fn load_dotenv(dotenv_path: &str) {
    let p = Path::new(dotenv_path);
    if !p.exists() {
        return;
    }
    let Ok(text) = std::fs::read_to_string(p) else {
        return;
    };
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if let Some((k, v)) = line.split_once('=') {
            if env::var_os(k).is_none() {
                unsafe { env::set_var(k.trim(), v.trim()); }
            }
        }
    }
}

fn build_config() -> EngineConfig {
    let mut cfg = EngineConfig::new();
    cfg.clob_base_url = env::var("CLOB_BASE_URL").unwrap_or_else(|_| "https://clob.polymarket.com".into());
    cfg.ws_url = env::var("WS_URL").unwrap_or_else(|_| "wss://ws-subscriptions-clob.polymarket.com/ws/market".into());
    cfg.private_key = env::var("CLOB_PRIVATE_KEY").unwrap_or_default();
    cfg.funder_address = env::var("CLOB_FUNDER_ADDRESS").unwrap_or_default();
    cfg.api_key = env::var("CLOB_API_KEY").unwrap_or_default();
    cfg.api_secret = env::var("CLOB_API_SECRET").unwrap_or_default();
    cfg.api_passphrase = env::var("CLOB_API_PASSPHRASE").unwrap_or_default();
    cfg.chain_id = getenv_parse("CLOB_CHAIN_ID", 137u64);
    cfg.signature_type = getenv_parse("CLOB_SIGNATURE_TYPE", 0u32);
    cfg.min_edge_percent = getenv_parse("MIN_EDGE_PERCENT", 0.5f64);
    cfg.min_liquidity_usd = getenv_parse("MIN_LIQUIDITY_USD", 100.0f64);
    cfg.buffer_liquid_percent = getenv_parse("BUFFER_LIQUID_PERCENT", 0.5f64);
    cfg.buffer_illiquid_percent = getenv_parse("BUFFER_ILLIQUID_PERCENT", 1.0f64);
    cfg.illiquid_threshold_usd = getenv_parse("ILLIQUID_THRESHOLD_USD", 1000.0f64);
    cfg.target_size_usd = getenv_parse("TARGET_SIZE_USD", 50.0f64);
    cfg.cross_bps = getenv_parse("CLOB_AGGRESSIVE_CROSS_BPS", 5.0f64);
    cfg.min_order_usd = getenv_parse("CLOB_MIN_ORDER_USD", 1.0f64);
    cfg.min_order_shares = getenv_parse("CLOB_MIN_ORDER_SHARES", 5.0f64);
    cfg
}

async fn fetch_markets(max_markets_watch: usize, min_liq: f64, min_vol: f64) -> Result<Vec<MarketInfo>> {
    let client = Client::builder().timeout(Duration::from_secs(20)).build()?;
    let mut all = Vec::<MarketInfo>::new();
    let mut offset = 0usize;
    let limit = 100usize;

    loop {
        let url = format!(
            "https://gamma-api.polymarket.com/markets?limit={}&offset={}&active=true&closed=false",
            limit, offset
        );
        let resp = client.get(&url).send().await.context("gamma fetch failed")?;
        if !resp.status().is_success() {
            break;
        }
        let batch: Vec<GammaMarket> = resp.json().await.context("gamma parse failed")?;
        if batch.is_empty() {
            break;
        }

        for m in batch {
            let id = m.id.clone().unwrap_or_default();
            if id.is_empty() {
                continue;
            }
            let tokens = m.tokens.unwrap_or_default();
            let (yes, no) = if tokens.len() >= 2 {
                let yes = tokens[0]
                    .token_id
                    .clone()
                    .or(tokens[0].token_id_alt.clone())
                    .unwrap_or_default();
                let no = tokens[1]
                    .token_id
                    .clone()
                    .or(tokens[1].token_id_alt.clone())
                    .unwrap_or_default();
                (yes, no)
            } else {
                let ids = parse_clob_ids(m.clob_token_ids.clone());
                if ids.len() >= 2 {
                    (ids[0].clone(), ids[1].clone())
                } else {
                    (String::new(), String::new())
                }
            };
            if yes.is_empty() || no.is_empty() {
                continue;
            }
            let liq = json_num(m.liquidity);
            let vol = json_num(m.volume);
            if !(liq >= min_liq || vol >= min_vol) {
                continue;
            }

            all.push(MarketInfo {
                id,
                condition_id: m.condition_id.unwrap_or_default(),
                question: m.question.unwrap_or_default(),
                slug: m.slug.unwrap_or_default(),
                end_date: m.end_date.unwrap_or_default(),
                is_crypto_15min: false,
                yes_token: yes,
                no_token: no,
                volume: vol,
                liquidity: liq,
            });
        }

        offset += limit;
        if offset >= 60000 || all.len() >= max_markets_watch {
            break;
        }
    }

    all.sort_by(|a, b| b.volume.partial_cmp(&a.volume).unwrap_or(std::cmp::Ordering::Equal));
    all.truncate(max_markets_watch);
    Ok(all)
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(std::env::var("RUST_LOG").unwrap_or_else(|_| "polymarket_rust_service=info,polymarket_engine=warn".to_string()))
        .init();

    let dotenv_path = env::var("ENV_FILE").unwrap_or_else(|_| "/opt/polymarket-bot/Arb-Bot/.env".into());
    load_dotenv(&dotenv_path);

    let cfg = build_config();
    let max_markets_watch = getenv_parse("MAX_MARKETS_WATCH", 500usize);
    let min_market_liq = getenv_parse("MARKET_MIN_LIQUIDITY_USD", 500.0f64);
    let min_market_vol = getenv_parse("MARKET_MIN_VOLUME_USD", 500.0f64);
    let execution_mode = env::var("EXECUTION_MODE").unwrap_or_else(|_| "dryrun".into());

    info!("rust-service: loading markets (max={})", max_markets_watch);
    let markets = fetch_markets(max_markets_watch, min_market_liq, min_market_vol).await?;
    if markets.is_empty() {
        return Err(anyhow::anyhow!("no markets loaded"));
    }

    let obm = Arc::new(OrderbookManager::new());
    obm.load_markets(markets);

    let assets = obm.get_all_asset_ids();
    let meta_cache = Arc::new(MetaCache::new(&cfg.clob_base_url));
    meta_cache.warm(&assets).await;

    let executor = if execution_mode == "real" && !cfg.private_key.is_empty() {
        Some(Arc::new(FastExecutor::new(&cfg)?))
    } else {
        None
    };

    let (tx, mut rx) = mpsc::unbounded_channel::<WsEvent>();
    let ws_url = cfg.ws_url.clone();
    let obm_ws = Arc::clone(&obm);
    tokio::spawn(async move {
        run_ws_stream(&ws_url, assets, obm_ws, tx).await;
    });

    let mut last_edge_by_market: HashMap<String, f64> = HashMap::new();
    info!("rust-service: started");

    while let Some(ev) = rx.recv().await {
        match ev {
            WsEvent::BookUpdate { asset_id } => {
                let Some(market) = obm.get_market_for_asset(&asset_id) else { continue; };
                let Some((yes_book, no_book)) = obm.get_market_books(&market) else { continue; };

                let yes_age = yes_book.last_update.elapsed().as_secs_f64();
                let no_age = no_book.last_update.elapsed().as_secs_f64();

                if let Some(opp) = detect_binary_arb(
                    &market,
                    &yes_book.asks,
                    &no_book.asks,
                    &yes_book.bids,
                    &no_book.bids,
                    yes_age,
                    no_age,
                    cfg.target_size_usd,
                    cfg.min_edge_percent,
                    cfg.min_liquidity_usd,
                    cfg.buffer_liquid_percent,
                    cfg.buffer_illiquid_percent,
                    cfg.illiquid_threshold_usd,
                ) {
                    // hot-path dedup + minimal logs (avoid noise)
                    let prev = last_edge_by_market.get(&opp.market_id).copied().unwrap_or(-999.0);
                    if (opp.net_edge_percent - prev).abs() < 0.001 {
                        continue;
                    }
                    last_edge_by_market.insert(opp.market_id.clone(), opp.net_edge_percent);

                    if opp.verdict == "ACTIONABLE" {
                        if let Some(exec) = &executor {
                            let exec = Arc::clone(exec);
                            let cfg2 = cfg.clone();
                            let mc = Arc::clone(&meta_cache);
                            tokio::spawn(async move {
                                let res = exec.execute(&opp, &cfg2, &mc).await;
                                if res.status != "SUBMITTED" {
                                    warn!("exec status={} reason={}", res.status, res.reason);
                                }
                            });
                        }
                    }
                }
            }
            WsEvent::Error(e) => error!("ws error: {}", e),
            WsEvent::Disconnected => {},
            WsEvent::Connected => {},
        }
    }

    Ok(())
}
