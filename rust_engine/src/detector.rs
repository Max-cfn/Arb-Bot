//! VWAP calculation, fee computation, and binary arbitrage detection.
//! Pure compute — no I/O, no async. Must be as fast as possible.

use std::time::{SystemTime, UNIX_EPOCH};

use crate::types::{Level, MarketInfo, RustArbOpportunity};

/// Calculate VWAP to fill `size_usd` on one side of the book.
/// Returns None if insufficient liquidity.
#[inline]
pub fn calculate_vwap(levels: &[Level], size_usd: f64) -> Option<f64> {
    if levels.is_empty() || size_usd <= 0.0 {
        return None;
    }

    let mut remaining = size_usd;
    let mut total_cost = 0.0_f64;
    let mut total_shares = 0.0_f64;

    for lvl in levels {
        if lvl.price <= 0.0 {
            continue;
        }
        let available_usd = lvl.size * lvl.price;
        let take_usd = remaining.min(available_usd);
        let shares = take_usd / lvl.price;

        total_cost += take_usd;
        total_shares += shares;
        remaining -= take_usd;

        if remaining <= 1e-9 {
            break;
        }
    }

    if remaining > 1e-9 {
        return None; // Insufficient liquidity
    }

    if total_shares > 0.0 {
        Some(total_cost / total_shares)
    } else {
        None
    }
}

/// Total USD liquidity on one side.
#[inline]
pub fn available_liquidity_usd(levels: &[Level]) -> f64 {
    levels
        .iter()
        .filter(|l| l.price > 0.0)
        .map(|l| l.price * l.size)
        .sum()
}

/// Calculate fees for a standard binary arb position.
#[inline]
fn calc_fees_standard(yes_vwap: f64, no_vwap: f64, size_usd: f64) -> (f64, f64, f64) {
    let gross_cost = (yes_vwap + no_vwap) * size_usd;
    let resolution_fee = 0.02 * size_usd;
    let net_cost = gross_cost;
    let payout = size_usd - resolution_fee;
    let net_profit = payout - net_cost;
    (net_profit, resolution_fee, 0.0)
}

/// Calculate fees for crypto 15-min markets (with trading fee).
#[inline]
fn calc_fees_crypto(yes_vwap: f64, no_vwap: f64, size_usd: f64) -> (f64, f64, f64) {
    let gross_cost = (yes_vwap + no_vwap) * size_usd;
    let yes_fee = 0.02 * yes_vwap.min(1.0 - yes_vwap) * size_usd;
    let no_fee = 0.02 * no_vwap.min(1.0 - no_vwap) * size_usd;
    let trading_fee = yes_fee + no_fee;
    let resolution_fee = 0.02 * size_usd;
    let net_cost = gross_cost + trading_fee;
    let payout = size_usd - resolution_fee;
    let net_profit = payout - net_cost;
    (net_profit, resolution_fee, trading_fee)
}

/// Calculate per-share fees (for "buy 1 YES + 1 NO" display).
#[inline]
fn calc_one_share_fees(yes_price: f64, no_price: f64, is_crypto: bool) -> (f64, f64) {
    let gross_cost = yes_price + no_price;
    let resolution_fee = 0.02;
    let trading_fee = if is_crypto {
        0.02 * yes_price.min(1.0 - yes_price) * yes_price
            + 0.02 * no_price.min(1.0 - no_price) * no_price
    } else {
        0.0
    };
    let payout = 1.0 - resolution_fee;
    let net_profit = payout - (gross_cost + trading_fee);
    let net_edge_pct = if gross_cost > 0.0 {
        (net_profit / gross_cost) * 100.0
    } else {
        0.0
    };
    (net_profit, net_edge_pct)
}

/// Detect binary arbitrage: buy YES + NO when combined ask < 1 after fees.
///
/// Returns None if no opportunity or insufficient quality.
#[allow(clippy::too_many_arguments)]
pub fn detect_binary_arb(
    market: &MarketInfo,
    yes_asks: &[Level],
    no_asks: &[Level],
    _yes_bids: &[Level],
    _no_bids: &[Level],
    yes_age_s: f64,
    no_age_s: f64,
    target_size_usd: f64,
    min_edge_percent: f64,
    min_liquidity_usd: f64,
    buffer_liquid: f64,
    buffer_illiquid: f64,
    illiquid_threshold: f64,
) -> Option<RustArbOpportunity> {
    if yes_asks.is_empty() || no_asks.is_empty() {
        return None;
    }

    // Freshness gating
    let max_age = if market.is_crypto_15min { 2.0 } else { 8.0 };
    if yes_age_s > max_age || no_age_s > max_age {
        return None;
    }

    // Check liquidity
    let yes_liq = available_liquidity_usd(yes_asks);
    let no_liq = available_liquidity_usd(no_asks);
    if yes_liq < min_liquidity_usd || no_liq < min_liquidity_usd {
        return None;
    }

    // VWAP
    let yes_vwap = calculate_vwap(yes_asks, target_size_usd)?;
    let no_vwap = calculate_vwap(no_asks, target_size_usd)?;
    let combined_cost = yes_vwap + no_vwap;

    if combined_cost >= 1.0 {
        return None;
    }

    let gross_edge = 1.0 - combined_cost;
    let gross_edge_percent = (gross_edge / combined_cost) * 100.0;

    // Fees
    let (net_profit, _res_fee, _trading_fee) = if market.is_crypto_15min {
        calc_fees_crypto(yes_vwap, no_vwap, target_size_usd)
    } else {
        calc_fees_standard(yes_vwap, no_vwap, target_size_usd)
    };

    let net_edge = if target_size_usd > 0.0 {
        net_profit / target_size_usd
    } else {
        0.0
    };
    let net_edge_percent = if combined_cost > 0.0 {
        (net_edge / combined_cost) * 100.0
    } else {
        0.0
    };

    // Buffer
    let buffer = if yes_liq.min(no_liq) < illiquid_threshold {
        buffer_illiquid
    } else {
        buffer_liquid
    };
    let adjusted_edge = net_edge_percent - buffer;

    // Verdict
    let verdict = if adjusted_edge >= 2.0 {
        "ACTIONABLE"
    } else if adjusted_edge >= 1.0 {
        "MARGINAL"
    } else if adjusted_edge >= min_edge_percent {
        "SKIP"
    } else {
        return None; // Below minimum, don't even report
    };

    // Best asks
    let best_yes = yes_asks[0].price;
    let best_no = no_asks[0].price;

    // One-share calc
    let (one_share_profit, one_share_edge_pct) =
        calc_one_share_fees(best_yes, best_no, market.is_crypto_15min);

    let t_detect_ns = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos() as u64;

    let max_safe_size = yes_liq.min(no_liq) * 0.5;

    Some(RustArbOpportunity {
        market_id: market.id.clone(),
        market_question: market.question.clone(),
        condition_id: market.condition_id.clone(),
        slug: market.slug.clone(),
        end_date: market.end_date.clone(),
        yes_token_id: market.yes_token.clone(),
        no_token_id: market.no_token.clone(),
        is_crypto_15min: market.is_crypto_15min,
        yes_best_ask: best_yes,
        no_best_ask: best_no,
        combined_best_asks: best_yes + best_no,
        yes_ask_vwap: yes_vwap,
        no_ask_vwap: no_vwap,
        combined_cost,
        gross_edge,
        gross_edge_percent,
        net_edge,
        net_edge_percent,
        one_share_net_profit: one_share_profit,
        one_share_net_edge_percent: one_share_edge_pct,
        size_usd: target_size_usd,
        yes_liquidity: yes_liq,
        no_liquidity: no_liq,
        max_safe_size,
        yes_book_age_s: yes_age_s,
        no_book_age_s: no_age_s,
        verdict: verdict.to_string(),
        t_detect_ns,
    })
}
