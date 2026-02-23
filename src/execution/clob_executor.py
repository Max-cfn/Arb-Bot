from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType, BalanceAllowanceParams
from py_clob_client.order_builder.constants import BUY, SELL

from src.utils.logger import logger
from .types import ExecutionMetrics, ExecutionResult


def _safe_float(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return float(default)


def _tick_price(p: float) -> float:
    """Round price to Polymarket tick size (0.001).

    Prices outside [0.001, 0.999] are rejected by the CLOB with HTTP 400.
    """
    p = round(float(p), 4)          # avoid float noise before rounding
    p = round(round(p * 1000) / 1000, 6)
    return float(min(0.999, max(0.001, p)))


def _tick_size(s: float) -> float:
    """Round size down to the nearest integer.

    Polymarket CLOB requires integer share quantities; non-integer sizes return 400.
    """
    return float(int(float(s)))


def _redact(s: str, keep: int = 6) -> str:
    if not s:
        return ""
    s = str(s)
    if len(s) <= keep * 2:
        return s
    return s[:keep] + "…" + s[-keep:]


class PolymarketClobExecutor:
    """Real Polymarket CLOB executor (via official `py-clob-client`).

    Design goals:
    - Fast: create + submit both legs ASAP
    - Safe: "complete-or-abort" policy with bounded recovery
    - Observable: monotonic_ns latency metrics (detect→sign→submit→ack)

    IMPORTANT: This will refuse to execute if credentials are missing or set to placeholders.
    """

    def __init__(self) -> None:
        # Defaults per official docs
        self.host = os.getenv("CLOB_BASE_URL", "https://clob.polymarket.com").strip()
        self.chain_id = int(os.getenv("CLOB_CHAIN_ID", "137"))
        self.signature_type = int(os.getenv("CLOB_SIGNATURE_TYPE", "0"))  # 0=EOA, 1=Magic/email

        # Secrets / account
        self.private_key = os.getenv("CLOB_PRIVATE_KEY", "XXXXXX").strip()  # replace
        self.funder_address = os.getenv("CLOB_FUNDER_ADDRESS", "XXXXXX").strip()  # replace

        # Tunables (Complete-or-Abort policy)
        self.wait_for_both_s = float(os.getenv("WAIT_FOR_BOTH_MS", "500")) / 1000.0
        self.poll_interval_s = float(os.getenv("POLL_INTERVAL_MS", "50")) / 1000.0
        self.retry_duration_s = float(os.getenv("RETRY_DURATION_MS", "200")) / 1000.0
        self.retry_slippage_percent = float(os.getenv("RETRY_SLIPPAGE_PERCENT", "1.5"))

        self.unwind_max_loss_percent = float(os.getenv("UNWIND_MAX_LOSS_PERCENT", "3.0"))
        self.unwind_min_loss_usd = float(os.getenv("UNWIND_MAX_LOSS_USD", "2.0"))

        self.killswitch_partials_count = int(os.getenv("KILLSWITCH_PARTIALS_COUNT", "2"))
        self.killswitch_partials_window_s = float(os.getenv("KILLSWITCH_PARTIALS_WINDOW_MIN", "10")) * 60.0
        self.killswitch_consecutive_failures = int(os.getenv("KILLSWITCH_CONSECUTIVE_FAILURES", "3"))

        self.min_shares = float(os.getenv("MIN_SHARES", "1"))

        # Aggressive limit: cross a few bps above best ask to improve initial fill probability.
        self.cross_bps = float(os.getenv("CLOB_AGGRESSIVE_CROSS_BPS", "5"))  # 5 bps

        # Internal state (process-local)
        self._partial_ts: list[float] = []
        self._consecutive_failures: int = 0

        self._client: Optional[ClobClient] = None

    def _control_path(self) -> str:
        # Keep backward-compat with CONTROL_FILE, but prefer POLY_CONTROL_FILE (/killswitch path).
        return os.getenv("POLY_CONTROL_FILE", os.getenv("CONTROL_FILE", "data/control.json"))

    def _log_exec(self, event: str, **fields) -> None:
        """Structured execution logs (JSON) for debugging early live tests.

        Never include secrets.
        """
        base = {
            "event": event,
            "ts": time.time(),
            "component": "clob_executor",
        }
        base.update(fields)
        try:
            logger.info("EXEC_JSON %s", json.dumps(base, separators=(",", ":"), ensure_ascii=False)[:8000])
        except Exception:
            # Fall back to plain logs
            logger.info("EXEC event=%s fields=%s", event, str(fields)[:2000])

    def _set_trading_enabled(self, enabled: bool) -> None:
        """Best-effort kill-switch toggle by writing control.json.

        Main loop reads `data/control.json` (see `is_trading_enabled()` in main).
        """
        path = self._control_path()
        try:
            # Preserve any other keys, only toggle trading_enabled.
            data: dict = {}
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            except FileNotFoundError:
                data = {}
            except Exception:
                data = {}

            data["trading_enabled"] = bool(enabled)

            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except Exception as exc:
            logger.error("Failed to write control file %s: %s", path, exc)

    def _note_outcome(self, result_status: str) -> None:
        """Update kill-switch counters.

        - 'FILLED' resets consecutive failures
        - any non-FILLED counts as a failure for the '3 consecutive without BOTH filled' rule
        - 'PARTIAL' also increments partial window
        """
        now = time.time()

        # prune old partials
        if self._partial_ts:
            cutoff = now - self.killswitch_partials_window_s
            self._partial_ts = [t for t in self._partial_ts if t >= cutoff]

        if result_status == "FILLED":
            self._consecutive_failures = 0
            return

        # any non-FILLED outcome is a failure per Option B
        self._consecutive_failures += 1

        if result_status in {"PARTIAL", "PARTIAL_OPEN"}:
            self._partial_ts.append(now)

        # Trigger kill-switch if thresholds exceeded
        if len(self._partial_ts) >= self.killswitch_partials_count:
            logger.warning(
                "KILLSWITCH: %d partials within %.0fs => trading OFF",
                len(self._partial_ts),
                self.killswitch_partials_window_s,
            )
            self._set_trading_enabled(False)

        if self._consecutive_failures >= self.killswitch_consecutive_failures:
            logger.warning(
                "KILLSWITCH: %d consecutive failures (no BOTH filled) => trading OFF",
                self._consecutive_failures,
            )
            self._set_trading_enabled(False)

    def _is_placeholder(self, v: Optional[str]) -> bool:
        if not v:
            return True
        return ("XXXXXX" in v) or (v.strip() in {"", "changeme", "TODO"})

    def missing_creds(self) -> list[str]:
        missing: list[str] = []
        if not self.host:
            missing.append("CLOB_BASE_URL")
        if self._is_placeholder(self.private_key):
            missing.append("CLOB_PRIVATE_KEY")
        if self._is_placeholder(self.funder_address):
            missing.append("CLOB_FUNDER_ADDRESS")
        return missing

    def _get_client(self) -> ClobClient:
        if self._client is None:
            # NOTE: `py-clob-client` is synchronous. We call it in threads from async context.
            client = ClobClient(
                self.host,
                key=self.private_key,
                chain_id=self.chain_id,
                signature_type=self.signature_type,
                funder=self.funder_address,
            )
            # API creds: prefer explicit env vars (stable for proxy accounts), else derive via signing.
            api_key = os.getenv("CLOB_API_KEY", "").strip()
            api_secret = os.getenv("CLOB_API_SECRET", "").strip()
            api_passphrase = os.getenv("CLOB_API_PASSPHRASE", "").strip()

            if api_key and api_secret and api_passphrase:
                client.set_api_creds(ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase))
            else:
                client.set_api_creds(client.create_or_derive_api_creds())

            self._client = client
        return self._client

    async def execute_two_leg(
        self,
        opp,
        run_id: str,
        metrics: ExecutionMetrics,
        *,
        discord_alert_fn=None,
    ) -> ExecutionResult:
        """Execute a two-leg arbitrage.

        discord_alert_fn: optional async callable(opp, *, note, run_id, status)
            Called immediately (fire-and-forget) for critical mid-execution events
            (PARTIAL_OPEN_AFTER_CANCEL, UNKNOWN_CANCEL_STATE) that must surface
            on Discord without waiting for the final result.
        """
        missing = self.missing_creds()
        if missing:
            self._log_exec(
                "creds_missing",
                run_id=run_id,
                missing=missing,
            )
            return ExecutionResult(
                status="FAILED",
                run_id=run_id,
                reason=f"Missing/placeholder CLOB creds: {', '.join(missing)}",
                metrics=metrics,
            )

        # Compute share sizes.
        # Goal: Polymarket enforces a ~$1 minimum order notional.
        # We buy enough shares on the cheaper leg to reach >= $1 notional,
        # and buy the exact same number of shares on the other leg.
        yes_price = float(opp.yes_best_ask or opp.yes_ask_vwap)
        no_price = float(opp.no_best_ask or opp.no_ask_vwap)
        if yes_price <= 0 or no_price <= 0:
            self._log_exec(
                "invalid_prices",
                run_id=run_id,
                market_id=getattr(opp, "market_id", ""),
                yes_price=yes_price,
                no_price=no_price,
            )
            return ExecutionResult(status="FAILED", run_id=run_id, reason="Invalid prices", metrics=metrics)

        min_order_usd = float(os.getenv("CLOB_MIN_ORDER_USD", "1.0"))
        # Polymarket also enforces a minimum *share size* on some markets (often 5).
        # Keep this configurable so we can tune without code changes.
        min_order_shares = float(os.getenv("CLOB_MIN_ORDER_SHARES", "5"))
        min_shares = float(self.min_shares)

        import math

        cheaper_price = min(yes_price, no_price)
        shares_for_min_notional = int(math.ceil(min_order_usd / cheaper_price)) if cheaper_price > 0 else 0
        shares_floor = int(math.ceil(max(min_shares, min_order_shares)))
        shares = max(shares_floor, shares_for_min_notional)

        # Keep legs symmetric in shares (binary arb).
        yes_size = float(shares)
        no_size = float(shares)

        client = self._get_client()

        # Balance/allowance snapshot — fired asynchronously (does NOT block the hot path).
        # The result is captured for the start-log only; we do not gate execution on it.
        bal_info_holder: list = [None]

        async def _fetch_balance() -> None:
            try:
                from py_clob_client.clob_types import AssetType
                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=int(self.signature_type))
                try:
                    await asyncio.to_thread(lambda: client.update_balance_allowance(params))
                except Exception:
                    pass
                bal_info_holder[0] = await asyncio.to_thread(lambda: client.get_balance_allowance(params))
            except Exception as exc:
                bal_info_holder[0] = {"error": str(exc)[:200]}

        # Start balance fetch in background; we proceed with order prep immediately.
        _bal_task = asyncio.create_task(_fetch_balance(), name=f"bal-{run_id}")

        # Aggressive limit prices: cross a few bps above best ask to improve fill probability.
        # Apply tick rounding AFTER the bump — avoids HTTP 400 "invalid price precision".
        def _aggressive_buy_limit(p: float) -> float:
            p = float(p)
            if p <= 0:
                return p
            bumped = p * (1.0 + (self.cross_bps / 10_000.0))
            return _tick_price(bumped)

        yes_limit = _aggressive_buy_limit(yes_price)
        no_limit = _aggressive_buy_limit(no_price)
        # Sizes must be integers — CLOB returns 400 for fractional shares.
        yes_size = _tick_size(yes_size)
        no_size = _tick_size(no_size)

        if yes_size <= 0 or no_size <= 0:
            return ExecutionResult(
                status="FAILED",
                run_id=run_id,
                reason=f"Size rounded to 0 (shares={shares}); cannot submit zero-size order.",
                metrics=metrics,
            )

        yes_args = OrderArgs(token_id=str(opp.yes_token_id), price=yes_limit, size=yes_size, side=BUY)
        no_args = OrderArgs(token_id=str(opp.no_token_id), price=no_limit, size=no_size, side=BUY)

        # loss cap (scales after test phase)
        est_total_cost = float(yes_limit * yes_size + no_limit * no_size)
        max_loss_usd = max(self.unwind_min_loss_usd, est_total_cost * 0.05)

        # Initial execution context log — balance may still be fetching; log whatever we have.
        bal_info = bal_info_holder[0]  # None if still in-flight (that's fine)
        self._log_exec(
            "start",
            run_id=run_id,
            market_id=getattr(opp, "market_id", ""),
            condition_id=_redact(getattr(opp, "condition_id", "")),
            slug=getattr(opp, "slug", "")[:120],
            is_crypto_15min=bool(getattr(opp, "is_crypto_15min", False)),
            end_date=getattr(opp, "end_date", ""),
            yes_token=_redact(getattr(opp, "yes_token_id", "")),
            no_token=_redact(getattr(opp, "no_token_id", "")),
            yes_price_best=_safe_float(getattr(opp, "yes_best_ask", 0.0)),
            no_price_best=_safe_float(getattr(opp, "no_best_ask", 0.0)),
            yes_vwap=_safe_float(getattr(opp, "yes_ask_vwap", 0.0)),
            no_vwap=_safe_float(getattr(opp, "no_ask_vwap", 0.0)),
            combined_cost=_safe_float(getattr(opp, "combined_cost", 0.0)),
            gross_edge_pct=_safe_float(getattr(opp, "gross_edge_percent", 0.0)),
            net_edge_pct=_safe_float(getattr(opp, "net_edge_percent", 0.0)),
            one_share_net_edge_pct=_safe_float(getattr(opp, "one_share_net_edge_percent", 0.0)),
            yes_age_s=_safe_float(getattr(opp, "yes_book_age_s", 0.0)),
            no_age_s=_safe_float(getattr(opp, "no_book_age_s", 0.0)),
            fee_bps_yes=int(getattr(opp, "fee_rate_bps_yes", 0) or 0),
            fee_bps_no=int(getattr(opp, "fee_rate_bps_no", 0) or 0),
            taker_fee_yes_pct=_safe_float(getattr(opp, "taker_fee_rate_percent_yes", 0.0)),
            taker_fee_no_pct=_safe_float(getattr(opp, "taker_fee_rate_percent_no", 0.0)),
            auth={
                "signature_type": int(self.signature_type),
                "funder": _redact(str(self.funder_address)),
            },
            balance_allowance=(
                (lambda b: (
                    {
                        "collateral_balance_usd": round(int(str(b.get("balance") or "0")) / 1_000_000.0, 6),
                        "allowances_n": len(b.get("allowances") or {}) if isinstance(b.get("allowances"), dict) else None,
                    }
                    if isinstance(b, dict) and "balance" in b
                    else (str(b)[:600] if b is not None else None)
                ))(bal_info)
            ),
            policy={
                "wait_for_both_ms": int(self.wait_for_both_s * 1000),
                "poll_interval_ms": int(self.poll_interval_s * 1000),
                "retry_duration_ms": int(self.retry_duration_s * 1000),
                "retry_slippage_percent": self.retry_slippage_percent,
                "unwind_max_loss_percent": self.unwind_max_loss_percent,
                "max_loss_usd": round(max_loss_usd, 4),
                "min_shares": self.min_shares,
                "cross_bps": self.cross_bps,
            },
            sizing={
                "yes_size": round(float(yes_size), 6),
                "no_size": round(float(no_size), 6),
                "yes_limit": round(float(yes_limit), 6),
                "no_limit": round(float(no_limit), 6),
                "est_total_cost": round(float(est_total_cost), 6),
            },
        )

        metrics.t_submit_ns = time.monotonic_ns()

        try:
            # Phase 1: pre-sign then submit in parallel (py-clob-client is sync => use threads)
            t0 = time.monotonic_ns()
            signed_yes, signed_no = await asyncio.gather(
                asyncio.to_thread(lambda: client.create_order(yes_args)),
                asyncio.to_thread(lambda: client.create_order(no_args)),
            )
            t_sign_ns = time.monotonic_ns()

            resp_yes, resp_no = await asyncio.gather(
                asyncio.to_thread(lambda: client.post_order(signed_yes, OrderType.FOK)),
                asyncio.to_thread(lambda: client.post_order(signed_no, OrderType.FOK)),
            )
            metrics.t_ack_ns = time.monotonic_ns()

            self._log_exec(
                "submitted",
                run_id=run_id,
                t_submit_ns=metrics.t_submit_ns,
                t_ack_ns=metrics.t_ack_ns,
                submit_to_ack_ms=round((metrics.t_ack_ns - metrics.t_submit_ns) / 1e6, 3),
                sign_ms=round((t_sign_ns - t0) / 1e6, 3),
            )

            # Best-effort order ids
            yes_oid = (resp_yes or {}).get("orderID") or (resp_yes or {}).get("id")
            no_oid = (resp_no or {}).get("orderID") or (resp_no or {}).get("id")

            submit_to_ack_ms = (metrics.t_ack_ns - metrics.t_submit_ns) / 1e6
            detect_to_sign_ms = None
            if isinstance(metrics.t_detect_ns, int):
                detect_to_sign_ms = (t_sign_ns - metrics.t_detect_ns) / 1e6
            sign_to_submit_ms = (metrics.t_submit_ns - t_sign_ns) / 1e6
            sign_and_post_ms = (metrics.t_ack_ns - t0) / 1e6

            logger.info(
                "EXEC_ACK run=%s yes_oid=%s no_oid=%s submit_to_ack_ms=%.3f sign_to_submit_ms=%.3f sign+post_ms=%.3f detect_to_sign_ms=%s resp_yes=%s resp_no=%s",
                run_id,
                yes_oid,
                no_oid,
                submit_to_ack_ms,
                sign_to_submit_ms,
                sign_and_post_ms,
                f"{detect_to_sign_ms:.3f}" if detect_to_sign_ms is not None else "n/a",
                str(resp_yes)[:500],
                str(resp_no)[:500],
            )

            self._log_exec(
                "ack",
                run_id=run_id,
                yes_oid=_redact(str(yes_oid) if yes_oid else ""),
                no_oid=_redact(str(no_oid) if no_oid else ""),
                resp_yes=str(resp_yes)[:800],
                resp_no=str(resp_no)[:800],
                submit_to_ack_ms=round(submit_to_ack_ms, 3),
                sign_to_submit_ms=round(sign_to_submit_ms, 3),
                sign_and_post_ms=round(sign_and_post_ms, 3),
                detect_to_sign_ms=(round(detect_to_sign_ms, 3) if detect_to_sign_ms is not None else None),
            )

            # ── PARTIAL SUBMIT GUARD ──────────────────────────────────────────────────
            # If one submit returned no orderID (400/auth error on that leg), the other
            # leg may already be live on the exchange.  Cancel it immediately and abort.
            if yes_oid is None or no_oid is None:
                abort_reason = (
                    f"Partial submit: yes_oid={'ok' if yes_oid else 'NONE'} "
                    f"no_oid={'ok' if no_oid else 'NONE'}. "
                    f"resp_yes={str(resp_yes)[:200]} resp_no={str(resp_no)[:200]}"
                )
                logger.error("EXEC_PARTIAL_SUBMIT run=%s %s", run_id, abort_reason)
                # Best-effort cancel of whichever leg DID get an ID
                for _oid in filter(None, [yes_oid, no_oid]):
                    try:
                        await asyncio.to_thread(lambda: client.cancel_order(str(_oid)))
                    except Exception as _ce:
                        logger.error("EXEC_PARTIAL_SUBMIT cancel run=%s oid=%s err=%s", run_id, _oid, _ce)
                self._note_outcome("FAILED")
                return ExecutionResult(
                    status="FAILED",
                    run_id=run_id,
                    reason=abort_reason,
                    reason_code="PARTIAL_SUBMIT",
                    metrics=metrics,
                )

            # --- Complete-or-Abort execution policy ---
            # Phase 2: WAIT FOR BOTH (up to WAIT_FOR_BOTH_MS)
            async def _get_status(order_id: str | None) -> tuple[str, dict] | None:
                if not order_id:
                    return None
                try:
                    od = await asyncio.to_thread(lambda: client.get_order(order_id))
                    if not isinstance(od, dict):
                        return None
                    st = str(od.get("status") or od.get("state") or "").upper()
                    return st, od
                except Exception:
                    return None

            def _is_done(st: str) -> bool:
                return st in {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "FAILED", "EXPIRED"}

            def _filled_size(od: dict) -> float:
                for k in ("sizeMatched", "filledSize", "matchedSize", "filled", "filled_size", "size_matched"):
                    try:
                        v = od.get(k)
                        if v is not None:
                            return float(v)
                    except Exception:
                        pass
                return 0.0

            async def _cancel_open() -> list[str]:
                to_cancel: list[str] = []
                if yes_oid:
                    ys = await _get_status(str(yes_oid))
                    # Belt-and-suspenders: cancel if confirmed non-terminal OR if status
                    # is unreachable (ys is None) — a cancel on an already-closed order
                    # is a safe no-op on the CLOB.
                    if ys is None or not _is_done(ys[0]):
                        to_cancel.append(str(yes_oid))
                if no_oid:
                    ns = await _get_status(str(no_oid))
                    if ns is None or not _is_done(ns[0]):
                        to_cancel.append(str(no_oid))
                if to_cancel:
                    await asyncio.to_thread(lambda: client.cancel_orders(to_cancel))
                    metrics.t_cancel_ns = time.monotonic_ns()
                return to_cancel

            async def _buy_other_leg(token_id: str, shares: float, ref_price: float) -> str | None:
                p = float(ref_price) * (1.0 + self.retry_slippage_percent / 100.0)
                p = _tick_price(p)
                sz = _tick_size(shares)
                if sz <= 0:
                    return None
                args = OrderArgs(token_id=str(token_id), price=p, size=sz, side=BUY)
                signed = await asyncio.to_thread(lambda: client.create_order(args))
                resp = await asyncio.to_thread(lambda: client.post_order(signed, OrderType.FAK))
                oid = (resp or {}).get("orderID") or (resp or {}).get("id")
                return str(oid) if oid else None

            async def _unwind_sell(
                token_id: str,
                shares: float,
                buy_price: float,
                *,
                emergency: bool = False,
            ) -> tuple[bool, dict]:
                """Attempt to unwind by SELLing filled shares.

                emergency=True  → FOK one-sided fill: sell at any price (0.0001 floor),
                                  take the hit, get out immediately.
                emergency=False → bounded loss unwind (normal recovery path).

                Returns (ok, info).
                """
                if shares <= 0:
                    return False, {"reason": "shares<=0"}

                if emergency:
                    # FOK one-sided fill: we must exit the position immediately.
                    #
                    # In Polymarket, a SELL limit order means "fill at any price >= this".
                    # Setting price=0.0001 on a FAK SELL is equivalent to a MARKET SELL:
                    # the order fills at whatever the best bid is (e.g. 0.43 if we bought
                    # at 0.45). We will NOT be filled at 0.0001 — that is just the floor.
                    # If there are zero bids the FAK cancels, but binary markets always
                    # have bids close to fair value.
                    #
                    # We do NOT fetch the orderbook here: that HTTP round-trip costs
                    # ~100 ms and serves no purpose since we accept whatever bid is there.
                    sell_price = 0.001  # Polymarket tick floor — FAK SELL fills at best bid
                    sz = _tick_size(shares)
                    if sz <= 0:
                        return False, {"reason": "emergency unwind: rounded size=0"}
                    try:
                        sell_args = OrderArgs(token_id=str(token_id), price=float(sell_price), size=sz, side=SELL)
                        signed = await asyncio.to_thread(lambda: client.create_order(sell_args))
                        resp = await asyncio.to_thread(lambda: client.post_order(signed, OrderType.FAK))
                        logger.warning(
                            "EXEC_EMERGENCY_UNWIND run=%s token=%s shares=%.4f price_floor=%.4f (market sell)",
                            run_id, token_id, float(shares), float(sell_price),
                        )
                        return True, {
                            "token": token_id,
                            "shares": float(shares),
                            "sell_price_floor": float(sell_price),
                            "emergency": True,
                            "resp": str(resp)[:500],
                        }
                    except Exception as exc:
                        logger.error("EXEC_EMERGENCY_UNWIND_FAILED run=%s err=%s", run_id, exc)
                        return False, {"token": token_id, "shares": float(shares), "err": str(exc)[:200], "emergency": True}
                else:
                    # Percent bound
                    min_price_pct = float(buy_price) * (1.0 - self.unwind_max_loss_percent / 100.0)
                    # USD bound
                    min_price_usd = float(buy_price) - (max_loss_usd / float(shares))
                    min_sell_price = _tick_price(max(0.001, min_price_pct, min_price_usd))

                    # Use best bid as a reference if available
                    best_bid = None
                    try:
                        ob = await asyncio.to_thread(lambda: client.get_order_book(str(token_id)))
                        if isinstance(ob, dict):
                            bids = ob.get("bids") or []
                            if bids and isinstance(bids, list) and isinstance(bids[0], dict):
                                best_bid = float(bids[0].get("price") or 0.0)
                    except Exception:
                        best_bid = None

                    # For SELL: lower price is more aggressive, but never below bounds.
                    sell_price = min_sell_price
                    if best_bid and best_bid > 0:
                        sell_price = max(min_sell_price, min(best_bid, buy_price))
                    sell_price = _tick_price(sell_price)
                    sz = _tick_size(shares)
                    if sz <= 0:
                        return False, {"reason": "bounded unwind: rounded size=0"}
                    try:
                        sell_args = OrderArgs(token_id=str(token_id), price=float(sell_price), size=sz, side=SELL)
                        signed = await asyncio.to_thread(lambda: client.create_order(sell_args))
                        resp = await asyncio.to_thread(lambda: client.post_order(signed, OrderType.FAK))

                        logger.warning(
                            "EXEC_UNWIND run=%s token=%s shares=%.4f sell_price=%.4f min_sell_price=%.4f max_loss_usd=%.2f",
                            run_id,
                            token_id,
                            float(shares),
                            float(sell_price),
                            float(min_sell_price),
                            float(max_loss_usd),
                        )
                        return True, {
                            "token": token_id,
                            "shares": float(shares),
                            "sell_price": float(sell_price),
                            "min_sell_price": float(min_sell_price),
                            "max_loss_usd": float(max_loss_usd),
                            "resp": str(resp)[:500],
                        }
                    except Exception as exc:
                        logger.error("EXEC_UNWIND_FAILED run=%s err=%s", run_id, exc)
                        return False, {"token": token_id, "shares": float(shares), "err": str(exc)[:200]}

            # Phase 2: FOK orders resolve instantly (filled or auto-cancelled by exchange).
            # Wait one poll interval for the server to process, then read final statuses.
            # If both filled in first check → done. Otherwise go straight to recovery.
            await asyncio.sleep(self.poll_interval_s)

            last_yes: dict = {}
            last_no: dict = {}
            yes_filled = False
            no_filled = False
            yes_size_filled = 0.0
            no_size_filled = 0.0

            # Up to wait_for_both_s in case server-side status propagation is slow.
            wait_deadline = time.monotonic() + self.wait_for_both_s
            while time.monotonic() < wait_deadline:
                ys = await _get_status(str(yes_oid) if yes_oid else None)
                ns = await _get_status(str(no_oid) if no_oid else None)

                if ys is not None:
                    yst, last_yes = ys
                    yes_size_filled = max(yes_size_filled, _filled_size(last_yes))
                    if yst == "FILLED":
                        yes_filled = True

                if ns is not None:
                    nst, last_no = ns
                    no_size_filled = max(no_size_filled, _filled_size(last_no))
                    if nst == "FILLED":
                        no_filled = True

                if yes_filled and no_filled:
                    metrics.t_both_filled_ns = time.monotonic_ns()
                    res = ExecutionResult(
                        status="FILLED",
                        run_id=run_id,
                        yes_order_id=str(yes_oid) if yes_oid else None,
                        no_order_id=str(no_oid) if no_oid else None,
                        reason=(
                            f"FOK complete. "
                            f"submit→ack={(metrics.t_ack_ns-metrics.t_submit_ns)/1e6:.3f}ms submit→filled={(metrics.t_both_filled_ns-metrics.t_submit_ns)/1e6:.3f}ms"
                        ),
                        reason_code="BOTH_FILLED",
                        yes_filled_size=float(yes_size_filled),
                        no_filled_size=float(no_size_filled),
                        metrics=metrics,
                    )
                    self._log_exec(
                        "result",
                        run_id=run_id,
                        status=res.status,
                        reason=res.reason[:800],
                        phase="fok_both_filled",
                        yes_size_filled=round(float(yes_size_filled), 6),
                        no_size_filled=round(float(no_size_filled), 6),
                    )
                    self._note_outcome(res.status)
                    return res

                # FOK should resolve nearly immediately; stop polling early if both are terminal
                ys_done = ys is not None and _is_done(ys[0])
                ns_done = ns is not None and _is_done(ns[0])
                if ys_done and ns_done:
                    break  # Both terminal, no point continuing

                await asyncio.sleep(self.poll_interval_s)

            # Phase 3: RECOVERY
            # ── CANCEL OPEN ORDERS ────────────────────────────────────────────────────
            # Cancel any orders still OPEN (or status-unreachable as belt-and-suspenders).
            to_cancel = await _cancel_open()

            # ── EXPLICIT CANCEL VERIFICATION ─────────────────────────────────────────
            # After cancel_orders(), confirm each order is actually terminal via GET
            # with retries.  FOK orders auto-cancel on the CLOB but status propagation
            # can lag by hundreds of ms.  We NEVER assume CANCELLED until we see it.
            async def _verify_cancel_confirmed(oid: str | None, max_retries: int = 4) -> tuple[str, dict]:
                """GET order with retries until terminal. Returns (status, od) or ('UNKNOWN', {})."""
                if not oid:
                    return "UNKNOWN", {}
                for _ in range(max_retries):
                    st = await _get_status(str(oid))
                    if st is not None:
                        status_str, od = st
                        if _is_done(status_str):
                            return status_str, od
                    await asyncio.sleep(0.05)  # 50 ms retry
                # Final attempt after all retries
                final = await _get_status(str(oid))
                if final is not None:
                    return final[0], final[1]
                return "UNKNOWN", {}

            yes_cancel_status, yes_cancel_od = await _verify_cancel_confirmed(str(yes_oid) if yes_oid else None)
            no_cancel_status, no_cancel_od = await _verify_cancel_confirmed(str(no_oid) if no_oid else None)

            self._log_exec(
                "cancel_verify",
                run_id=run_id,
                yes_oid=_redact(str(yes_oid) if yes_oid else ""),
                no_oid=_redact(str(no_oid) if no_oid else ""),
                yes_cancel_status=yes_cancel_status,
                no_cancel_status=no_cancel_status,
                cancelled_open=len(to_cancel),
            )

            # ── TERMINAL-STATE GUARANTEE ─────────────────────────────────────────────
            # Seed terminal flags from cancel verification results, then continue polling.
            # CRITICAL: when API is unreachable (None response) we do NOT set
            # yes_terminal/no_terminal = True — that was the previous bug.
            # Instead we track yes_unreachable/no_unreachable and handle below.
            terminal_deadline = time.monotonic() + 0.3  # 300 ms hard cap
            yes_terminal = yes_filled  # already confirmed above if True
            no_terminal = no_filled
            yes_unreachable = False
            no_unreachable = False

            # Incorporate cancel-verify results
            if not yes_terminal:
                if yes_cancel_status != "UNKNOWN":
                    yes_size_filled = max(yes_size_filled, _filled_size(yes_cancel_od))
                    yes_filled = yes_filled or (yes_cancel_status == "FILLED")
                    yes_terminal = _is_done(yes_cancel_status)
                else:
                    yes_unreachable = True

            if not no_terminal:
                if no_cancel_status != "UNKNOWN":
                    no_size_filled = max(no_size_filled, _filled_size(no_cancel_od))
                    no_filled = no_filled or (no_cancel_status == "FILLED")
                    no_terminal = _is_done(no_cancel_status)
                else:
                    no_unreachable = True

            while time.monotonic() < terminal_deadline and not (yes_terminal and no_terminal):
                yt, nt = await asyncio.gather(
                    _get_status(str(yes_oid)),
                    _get_status(str(no_oid)),
                )
                if yt is not None:
                    yst2, last_yes = yt
                    yes_size_filled = max(yes_size_filled, _filled_size(last_yes))
                    yes_filled = yes_filled or (yst2 == "FILLED")
                    yes_terminal = _is_done(yst2)
                    yes_unreachable = False
                else:
                    # API unreachable — do NOT assume terminal
                    yes_unreachable = True

                if nt is not None:
                    nst2, last_no = nt
                    no_size_filled = max(no_size_filled, _filled_size(last_no))
                    no_filled = no_filled or (nst2 == "FILLED")
                    no_terminal = _is_done(nst2)
                    no_unreachable = False
                else:
                    no_unreachable = True

                if yes_terminal and no_terminal:
                    break
                await asyncio.sleep(0.025)

            # Log final confirmed state of both orders
            self._log_exec(
                "terminal_state",
                run_id=run_id,
                yes_oid=_redact(str(yes_oid)),
                no_oid=_redact(str(no_oid)),
                yes_filled=yes_filled,
                no_filled=no_filled,
                yes_size_filled=round(float(yes_size_filled), 6),
                no_size_filled=round(float(no_size_filled), 6),
                yes_terminal=yes_terminal,
                no_terminal=no_terminal,
                yes_unreachable=yes_unreachable,
                no_unreachable=no_unreachable,
                yes_cancel_status=yes_cancel_status,
                no_cancel_status=no_cancel_status,
                cancelled_open=len(to_cancel),
            )

            # ── UNKNOWN_CANCEL_STATE ──────────────────────────────────────────────────
            # CLOB API consistently unreachable: cannot confirm whether orders are
            # cancelled.  We may have an open position we can't see.
            # Action: emergency-sell any known filled leg, then raise CRITICAL alert.
            if (yes_unreachable or no_unreachable) and not (yes_filled and no_filled):
                unwind_detail = ""
                if yes_size_filled > 0 and not no_filled:
                    uw_ok, uw_info = await _unwind_sell(str(opp.yes_token_id), yes_size_filled, yes_limit, emergency=True)
                    unwind_detail = f" EMERGENCY_UNWIND_YES ok={uw_ok}"
                    self._log_exec("emergency_unwind", run_id=run_id, ok=uw_ok, info=uw_info, trigger="unknown_cancel_state")
                elif no_size_filled > 0 and not yes_filled:
                    uw_ok, uw_info = await _unwind_sell(str(opp.no_token_id), no_size_filled, no_limit, emergency=True)
                    unwind_detail = f" EMERGENCY_UNWIND_NO ok={uw_ok}"
                    self._log_exec("emergency_unwind", run_id=run_id, ok=uw_ok, info=uw_info, trigger="unknown_cancel_state")

                unk_reason = (
                    f"UNKNOWN_CANCEL_STATE: CLOB unreachable during cancel verification — "
                    f"cannot confirm order cancellation. "
                    f"yes_unreachable={yes_unreachable} no_unreachable={no_unreachable} "
                    f"yes_cancel_status={yes_cancel_status} no_cancel_status={no_cancel_status} "
                    f"yes_size={yes_size_filled:.4f} no_size={no_size_filled:.4f}"
                    f"{unwind_detail}"
                )
                logger.error("EXEC_UNKNOWN_CANCEL run=%s %s", run_id, unk_reason)

                if discord_alert_fn is not None:
                    asyncio.create_task(discord_alert_fn(
                        opp,
                        note=f"CRITICAL: {unk_reason}",
                        run_id=run_id,
                        status="FAILED",
                    ))

                res = ExecutionResult(
                    status="FAILED",
                    run_id=run_id,
                    yes_order_id=str(yes_oid) if yes_oid else None,
                    no_order_id=str(no_oid) if no_oid else None,
                    reason=unk_reason,
                    reason_code="UNKNOWN_CANCEL_STATE",
                    yes_filled_size=float(yes_size_filled),
                    no_filled_size=float(no_size_filled),
                    metrics=metrics,
                )
                self._log_exec("result", run_id=run_id, status=res.status, reason=res.reason[:800], phase="unknown_cancel_state")
                self._note_outcome(res.status)
                return res

            # ── PARTIAL_OPEN_AFTER_CANCEL ─────────────────────────────────────────────
            # Cancel was sent but CLOB still reports a non-terminal status (confirmed
            # reachable, not UNKNOWN).  The order is still live on the book.
            # Action: emergency-sell any filled shares, then raise CRITICAL alert.
            po_yes = not yes_terminal and not yes_unreachable
            po_no = not no_terminal and not no_unreachable
            if po_yes or po_no:
                po_notes: list[str] = []
                if po_yes:
                    if yes_size_filled > 0:
                        uw_ok, uw_info = await _unwind_sell(str(opp.yes_token_id), yes_size_filled, yes_limit, emergency=True)
                        po_notes.append(f"YES PARTIAL_OPEN filled={yes_size_filled:.4f} unwind_ok={uw_ok}")
                        self._log_exec("emergency_unwind", run_id=run_id, ok=uw_ok, info=uw_info, trigger="partial_open_after_cancel_yes")
                    else:
                        po_notes.append("YES PARTIAL_OPEN no fills — order still live on CLOB, manual check needed")
                if po_no:
                    if no_size_filled > 0:
                        uw_ok, uw_info = await _unwind_sell(str(opp.no_token_id), no_size_filled, no_limit, emergency=True)
                        po_notes.append(f"NO PARTIAL_OPEN filled={no_size_filled:.4f} unwind_ok={uw_ok}")
                        self._log_exec("emergency_unwind", run_id=run_id, ok=uw_ok, info=uw_info, trigger="partial_open_after_cancel_no")
                    else:
                        po_notes.append("NO PARTIAL_OPEN no fills — order still live on CLOB, manual check needed")

                po_reason = "PARTIAL_OPEN_AFTER_CANCEL: " + "; ".join(po_notes)
                logger.error("EXEC_PARTIAL_OPEN run=%s %s", run_id, po_reason)

                if discord_alert_fn is not None:
                    asyncio.create_task(discord_alert_fn(
                        opp,
                        note=f"CRITICAL: {po_reason}",
                        run_id=run_id,
                        status="PARTIAL_OPEN",
                    ))

                res = ExecutionResult(
                    status="PARTIAL_OPEN",
                    run_id=run_id,
                    yes_order_id=str(yes_oid) if yes_oid else None,
                    no_order_id=str(no_oid) if no_oid else None,
                    reason=po_reason,
                    reason_code="PARTIAL_OPEN_AFTER_CANCEL",
                    yes_filled_size=float(yes_size_filled),
                    no_filled_size=float(no_size_filled),
                    metrics=metrics,
                )
                self._log_exec("result", run_id=run_id, status=res.status, reason=res.reason[:800], phase="partial_open_after_cancel")
                self._note_outcome(res.status)
                return res

            # Case A: both legs filled >0 (hedged, possibly smaller)
            if yes_size_filled > 0 and no_size_filled > 0:
                # If mismatch, unwind the excess (best-effort bounded-loss sell)
                excess_unwind_note = ""
                if abs(yes_size_filled - no_size_filled) > 1e-6:
                    excess = abs(yes_size_filled - no_size_filled)
                    if yes_size_filled > no_size_filled:
                        uw_ok, uw_info = await _unwind_sell(str(opp.yes_token_id), excess, yes_limit)
                        excess_leg = "YES"
                    else:
                        uw_ok, uw_info = await _unwind_sell(str(opp.no_token_id), excess, no_limit)
                        excess_leg = "NO"
                    self._log_exec(
                        "excess_unwind",
                        run_id=run_id,
                        ok=uw_ok,
                        info=uw_info,
                        excess_leg=excess_leg,
                        excess_shares=round(excess, 6),
                    )
                    excess_unwind_note = (
                        f" | excess_unwind leg={excess_leg} shares={excess:.4f} ok={uw_ok}"
                    )
                    # If the bounded unwind failed, alert immediately so the operator can
                    # check whether there is an uncovered position.
                    if not uw_ok and discord_alert_fn is not None:
                        asyncio.create_task(discord_alert_fn(
                            opp,
                            note=(
                                f"WARN: RECOVERY_BOTH_FILLED size mismatch — excess unwind FAILED. "
                                f"leg={excess_leg} excess={excess:.4f} shares. "
                                f"Both legs ARE filled; position is hedged but sizes differ. "
                                f"Manual review recommended. err={uw_info.get('err', '')}"
                            ),
                            run_id=run_id,
                            status="PARTIAL_OPEN",
                        ))

                metrics.t_both_filled_ns = time.monotonic_ns()
                res = ExecutionResult(
                    status="FILLED",
                    run_id=run_id,
                    yes_order_id=str(yes_oid) if yes_oid else None,
                    no_order_id=str(no_oid) if no_oid else None,
                    reason=(
                        f"Recovery success (both legs filled>0). yes={yes_size_filled:.4f} no={no_size_filled:.4f} cancelled_open={len(to_cancel)}"
                        f"{excess_unwind_note}"
                    ),
                    reason_code="RECOVERY_BOTH_FILLED",
                    yes_filled_size=float(yes_size_filled),
                    no_filled_size=float(no_size_filled),
                    metrics=metrics,
                )
                self._log_exec(
                    "result",
                    run_id=run_id,
                    status=res.status,
                    reason=res.reason[:800],
                    phase="recovery_both_filled",
                    yes_size_filled=round(float(yes_size_filled), 6),
                    no_size_filled=round(float(no_size_filled), 6),
                    cancelled_open=len(to_cancel),
                )
                self._note_outcome(res.status)
                return res

            # Case B: one-sided fill (FOK — other leg was auto-cancelled by exchange)
            # → immediately try to buy the other leg one more time with FAK + slippage.
            # → if that also fails, emergency-unwind (sell at ANY price) the filled leg NOW.
            if (yes_size_filled > 0) != (no_size_filled > 0):
                metrics.t_first_fill_ns = time.monotonic_ns()

                if yes_size_filled > 0:
                    # YES filled, NO auto-cancelled — attempt one fast FAK retry for NO
                    retry_oid = await _buy_other_leg(str(opp.no_token_id), yes_size_filled, float(opp.no_best_ask or no_price))
                    retry_deadline = time.monotonic() + self.retry_duration_s
                    no_retry_filled = False
                    while time.monotonic() < retry_deadline:
                        st = await _get_status(retry_oid)
                        if st is not None and st[0] == "FILLED":
                            no_retry_filled = True
                            break
                        await asyncio.sleep(self.poll_interval_s)

                    if no_retry_filled:
                        metrics.t_both_filled_ns = time.monotonic_ns()
                        res = ExecutionResult(
                            status="FILLED",
                            run_id=run_id,
                            yes_order_id=str(yes_oid) if yes_oid else None,
                            no_order_id=str(retry_oid) if retry_oid else (str(no_oid) if no_oid else None),
                            reason=f"FOK YES filled + FAK retry NO succeeded within {self.retry_duration_s*1000:.0f}ms (+{self.retry_slippage_percent:.1f}% slippage).",
                            reason_code="PARTIAL_RETRY_SUCCESS",
                            yes_filled_size=float(yes_size_filled),
                            no_filled_size=float(yes_size_filled),
                            metrics=metrics,
                        )
                        self._log_exec(
                            "result",
                            run_id=run_id,
                            status=res.status,
                            reason=res.reason[:800],
                            phase="fok_yes_filled_retry_no_success",
                            retry_leg="NO",
                            retry_oid=_redact(str(retry_oid) if retry_oid else ""),
                            yes_size_filled=round(float(yes_size_filled), 6),
                        )
                        self._note_outcome(res.status)
                        return res

                    # NO retry failed → EMERGENCY market-sell YES immediately
                    unwind_ok, unwind_info = await _unwind_sell(str(opp.yes_token_id), yes_size_filled, yes_limit, emergency=True)
                    res = ExecutionResult(
                        status="CANCELLED",
                        run_id=run_id,
                        yes_order_id=str(yes_oid) if yes_oid else None,
                        no_order_id=str(no_oid) if no_oid else None,
                        reason=f"FOK: YES={yes_size_filled:.4f} filled, NO auto-cancelled; FAK retry failed; EMERGENCY_UNWIND ok={unwind_ok}.",
                        reason_code=("FOK_EMERGENCY_UNWIND_OK" if unwind_ok else "FOK_EMERGENCY_UNWIND_FAILED"),
                        yes_filled_size=float(yes_size_filled),
                        no_filled_size=0.0,
                        metrics=metrics,
                    )
                    self._log_exec("emergency_unwind", run_id=run_id, ok=unwind_ok, info=unwind_info)
                    self._log_exec(
                        "result",
                        run_id=run_id,
                        status=res.status,
                        reason=res.reason[:800],
                        phase="fok_yes_filled_emergency_unwind",
                        filled_leg="YES",
                        filled_shares=round(float(yes_size_filled), 6),
                    )
                    self._note_outcome(res.status)
                    return res

                else:
                    # NO filled, YES auto-cancelled — attempt one fast FAK retry for YES
                    retry_oid = await _buy_other_leg(str(opp.yes_token_id), no_size_filled, float(opp.yes_best_ask or yes_price))
                    retry_deadline = time.monotonic() + self.retry_duration_s
                    yes_retry_filled = False
                    while time.monotonic() < retry_deadline:
                        st = await _get_status(retry_oid)
                        if st is not None and st[0] == "FILLED":
                            yes_retry_filled = True
                            break
                        await asyncio.sleep(self.poll_interval_s)

                    if yes_retry_filled:
                        metrics.t_both_filled_ns = time.monotonic_ns()
                        res = ExecutionResult(
                            status="FILLED",
                            run_id=run_id,
                            yes_order_id=str(retry_oid) if retry_oid else (str(yes_oid) if yes_oid else None),
                            no_order_id=str(no_oid) if no_oid else None,
                            reason=f"FOK NO filled + FAK retry YES succeeded within {self.retry_duration_s*1000:.0f}ms (+{self.retry_slippage_percent:.1f}% slippage).",
                            reason_code="PARTIAL_RETRY_SUCCESS",
                            yes_filled_size=float(no_size_filled),
                            no_filled_size=float(no_size_filled),
                            metrics=metrics,
                        )
                        self._log_exec(
                            "result",
                            run_id=run_id,
                            status=res.status,
                            reason=res.reason[:800],
                            phase="fok_no_filled_retry_yes_success",
                            retry_leg="YES",
                            retry_oid=_redact(str(retry_oid) if retry_oid else ""),
                            no_size_filled=round(float(no_size_filled), 6),
                        )
                        self._note_outcome(res.status)
                        return res

                    # YES retry failed → EMERGENCY market-sell NO immediately
                    unwind_ok, unwind_info = await _unwind_sell(str(opp.no_token_id), no_size_filled, no_limit, emergency=True)
                    res = ExecutionResult(
                        status="CANCELLED",
                        run_id=run_id,
                        yes_order_id=str(yes_oid) if yes_oid else None,
                        no_order_id=str(no_oid) if no_oid else None,
                        reason=f"FOK: NO={no_size_filled:.4f} filled, YES auto-cancelled; FAK retry failed; EMERGENCY_UNWIND ok={unwind_ok}.",
                        reason_code=("FOK_EMERGENCY_UNWIND_OK" if unwind_ok else "FOK_EMERGENCY_UNWIND_FAILED"),
                        yes_filled_size=0.0,
                        no_filled_size=float(no_size_filled),
                        metrics=metrics,
                    )
                    self._log_exec("emergency_unwind", run_id=run_id, ok=unwind_ok, info=unwind_info)
                    self._log_exec(
                        "result",
                        run_id=run_id,
                        status=res.status,
                        reason=res.reason[:800],
                        phase="fok_no_filled_emergency_unwind",
                        filled_leg="NO",
                        filled_shares=round(float(no_size_filled), 6),
                    )
                    self._note_outcome(res.status)
                    return res

            # Case C: nothing filled -> ABORT
            res = ExecutionResult(
                status="CANCELLED" if to_cancel else "FAILED",
                run_id=run_id,
                yes_order_id=str(yes_oid) if yes_oid else None,
                no_order_id=str(no_oid) if no_oid else None,
                reason=f"ABORT: no BOTH filled; cancelled_open={len(to_cancel)} max_loss_usd={max_loss_usd:.2f}",
                reason_code="TIMEOUT_NO_FILLS",
                yes_filled_size=float(yes_size_filled),
                no_filled_size=float(no_size_filled),
                metrics=metrics,
            )
            self._log_exec(
                "result",
                run_id=run_id,
                status=res.status,
                reason=res.reason[:800],
                phase="abort_no_fill",
                cancelled_open=len(to_cancel),
                max_loss_usd=round(float(max_loss_usd), 4),
            )
            self._note_outcome(res.status)
            return res

        except Exception as exc:
            import traceback

            err_s = str(exc)
            err_repr = repr(exc)
            err_type = type(exc).__name__
            cause = getattr(exc, "__cause__", None)
            context = getattr(exc, "__context__", None)

            # ── Detailed error classification ─────────────────────────────────────
            # py-clob-client wraps HTTP errors as PolyApiException with status_code.
            # We inspect both the exception type and the message to classify.
            http_status: int | None = getattr(exc, "status_code", None)
            err_lower = err_s.lower()

            if http_status == 400 or "status_code=400" in err_repr or "bad request" in err_lower:
                # Invalid price precision, invalid size, or malformed payload.
                # These should NEVER be retried — log params for debugging.
                reason_code = "HTTP_400_BAD_REQUEST"
                logger.error(
                    "EXEC_HTTP_400 run=%s — likely price/size precision or token error. "
                    "yes_limit=%s no_limit=%s yes_size=%s no_size=%s err=%s",
                    run_id, yes_limit, no_limit, yes_size, no_size, err_s[:400],
                )
            elif http_status == 401 or "status_code=401" in err_repr or "unauthorized" in err_lower or "invalid signature" in err_lower:
                # Auth / signature failure: wrong key, clock skew, bad passphrase.
                reason_code = "HTTP_401_AUTH"
                logger.error(
                    "EXEC_HTTP_401 run=%s — check CLOB_API_KEY / CLOB_API_SECRET / CLOB_API_PASSPHRASE / clock skew. err=%s",
                    run_id, err_s[:400],
                )
            elif http_status == 403 or "status_code=403" in err_repr or "forbidden" in err_lower:
                reason_code = "HTTP_403_FORBIDDEN"
            elif http_status is not None and http_status >= 500:
                reason_code = "HTTP_5XX_SERVER"
            elif "request exception" in err_s or "request exception" in err_repr:
                reason_code = "REQUEST_EXCEPTION"
            elif "not enough balance" in err_lower or "allowance" in err_lower:
                reason_code = "NOT_ENOUGH_BALANCE_OR_ALLOWANCE"
            else:
                reason_code = "EXEC_EXCEPTION"

            tb = traceback.format_exc(limit=6)

            self._log_exec(
                "exception",
                run_id=run_id,
                reason_code=reason_code,
                err=err_s[:800],
                err_type=err_type,
                err_repr=err_repr[:800],
                cause=(repr(cause)[:400] if cause else None),
                context=(repr(context)[:400] if context else None),
                traceback=tb[:2000],
                metrics=asdict(metrics),
            )

            logger.error("Real execution error run=%s metrics=%s err=%s", run_id, asdict(metrics), exc)
            res = ExecutionResult(
                status="FAILED",
                run_id=run_id,
                reason=err_s,
                reason_code=reason_code,
                yes_filled_size=0.0,
                no_filled_size=0.0,
                metrics=metrics,
            )
            # Count this failure towards kill-switch thresholds
            try:
                # Failure should count toward consecutive failures kill-switch
                self._note_outcome(res.status)
            except Exception:
                pass
            return res
