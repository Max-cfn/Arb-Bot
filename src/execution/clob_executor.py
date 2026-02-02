from __future__ import annotations

import asyncio
import os
import time
from dataclasses import asdict
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from src.utils.logger import logger
from .types import ExecutionMetrics, ExecutionResult


class PolymarketClobExecutor:
    """Real Polymarket CLOB executor (via official `py-clob-client`).

    Design goals:
    - Fast: create + submit both legs ASAP
    - Safe: one-shot compatible, cancel fast
    - Observable: monotonic_ns latency metrics (submit→ack, submit→(filled/cancelled))

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

        # Tunables
        # Aggressive limit + short cancel window (user preference)
        self.order_timeout_s = float(os.getenv("CLOB_ORDER_TIMEOUT_S", "0.4"))
        self.poll_interval_s = float(os.getenv("CLOB_POLL_INTERVAL_S", "0.05"))
        self.cross_bps = float(os.getenv("CLOB_AGGRESSIVE_CROSS_BPS", "5"))  # 5 bps

        self._client: Optional[ClobClient] = None

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
            # API creds are derived from wallet signing
            client.set_api_creds(client.create_or_derive_api_creds())
            self._client = client
        return self._client

    async def execute_two_leg(self, opp, run_id: str, metrics: ExecutionMetrics) -> ExecutionResult:
        missing = self.missing_creds()
        if missing:
            return ExecutionResult(
                status="FAILED",
                run_id=run_id,
                reason=f"Missing/placeholder CLOB creds: {', '.join(missing)}",
                metrics=metrics,
            )

        # Compute share sizes from USD budget per leg.
        # opp.size_usd is the target USD budget used to compute VWAP; we reuse it.
        yes_price = float(opp.yes_best_ask or opp.yes_ask_vwap)
        no_price = float(opp.no_best_ask or opp.no_ask_vwap)
        if yes_price <= 0 or no_price <= 0:
            return ExecutionResult(status="FAILED", run_id=run_id, reason="Invalid prices", metrics=metrics)

        yes_size = float(opp.size_usd) / yes_price
        no_size = float(opp.size_usd) / no_price

        client = self._get_client()

        # Aggressive limit prices: cross a few bps above best ask to improve fill probability.
        def _aggressive_buy_limit(p: float) -> float:
            p = float(p)
            if p <= 0:
                return p
            bumped = p * (1.0 + (self.cross_bps / 10_000.0))
            return float(min(0.9999, max(0.0001, bumped)))

        yes_limit = _aggressive_buy_limit(yes_price)
        no_limit = _aggressive_buy_limit(no_price)

        yes_args = OrderArgs(token_id=str(opp.yes_token_id), price=yes_limit, size=yes_size, side=BUY)
        no_args = OrderArgs(token_id=str(opp.no_token_id), price=no_limit, size=no_size, side=BUY)

        metrics.t_submit_ns = time.monotonic_ns()

        try:
            # Pre-sign then submit in parallel (py-clob-client is sync => use threads)
            t0 = time.monotonic_ns()
            signed_yes, signed_no = await asyncio.gather(
                asyncio.to_thread(lambda: client.create_order(yes_args)),
                asyncio.to_thread(lambda: client.create_order(no_args)),
            )
            t_sign_ns = time.monotonic_ns()

            resp_yes, resp_no = await asyncio.gather(
                asyncio.to_thread(lambda: client.post_order(signed_yes, OrderType.GTC)),
                asyncio.to_thread(lambda: client.post_order(signed_no, OrderType.GTC)),
            )
            metrics.t_ack_ns = time.monotonic_ns()

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

            # Poll per-order status for a short window, then cancel anything still open.
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
                # Be permissive with status vocabulary.
                return st in {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "FAILED", "EXPIRED"}

            deadline = time.monotonic() + self.order_timeout_s
            yes_done = False
            no_done = False
            yes_filled = False
            no_filled = False
            last_yes: dict = {}
            last_no: dict = {}

            while time.monotonic() < deadline and not (yes_done and no_done):
                ys = await _get_status(str(yes_oid) if yes_oid else None)
                ns = await _get_status(str(no_oid) if no_oid else None)

                if ys is not None:
                    yst, last_yes = ys
                    if _is_done(yst):
                        yes_done = True
                    if yst == "FILLED":
                        yes_filled = True

                if ns is not None:
                    nst, last_no = ns
                    if _is_done(nst):
                        no_done = True
                    if nst == "FILLED":
                        no_filled = True

                if not (yes_done and no_done):
                    await asyncio.sleep(self.poll_interval_s)

            # Cancel whatever is still not done/open after timeout.
            to_cancel: list[str] = []
            if yes_oid and not yes_done:
                to_cancel.append(str(yes_oid))
            if no_oid and not no_done:
                to_cancel.append(str(no_oid))

            if to_cancel:
                await asyncio.to_thread(lambda: client.cancel_orders(to_cancel))
                metrics.t_cancel_ns = time.monotonic_ns()

            # Classification:
            if yes_filled and no_filled:
                metrics.t_both_filled_ns = time.monotonic_ns()
                return ExecutionResult(
                    status="FILLED",
                    run_id=run_id,
                    yes_order_id=str(yes_oid) if yes_oid else None,
                    no_order_id=str(no_oid) if no_oid else None,
                    reason=(
                        f"Aggressive limit+cancel window {self.order_timeout_s:.3f}s. "
                        f"submit→ack={(metrics.t_ack_ns-metrics.t_submit_ns)/1e6:.3f}ms submit→filled={(metrics.t_both_filled_ns-metrics.t_submit_ns)/1e6:.3f}ms"
                    ),
                    metrics=metrics,
                )

            if yes_filled != no_filled:
                # PARTIAL fill risk: attempt emergency unwind (best-effort).
                metrics.t_first_fill_ns = time.monotonic_ns()
                filled_token = str(opp.yes_token_id) if yes_filled else str(opp.no_token_id)
                filled_leg = "YES" if yes_filled else "NO"
                filled_info = last_yes if yes_filled else last_no

                filled_size = 0.0
                for k in ("sizeMatched", "filledSize", "matchedSize", "filled"):  # best-effort
                    try:
                        v = filled_info.get(k)
                        if v is not None:
                            filled_size = float(v)
                            break
                    except Exception:
                        pass

                async def _emergency_unwind() -> None:
                    if filled_size <= 0:
                        return
                    try:
                        from py_clob_client.clob_types import MarketOrderArgs

                        mo = MarketOrderArgs(token_id=filled_token, amount=float(filled_size), side=SELL)
                        signed = await asyncio.to_thread(lambda: client.create_market_order(mo))
                        await asyncio.to_thread(lambda: client.post_order(signed, OrderType.FOK))
                        logger.warning(
                            "EXEC_UNWIND run=%s leg=%s token=%s size=%s status=SUBMITTED",
                            run_id,
                            filled_leg,
                            filled_token,
                            filled_size,
                        )
                    except Exception as exc:
                        logger.error("EXEC_UNWIND_FAILED run=%s err=%s", run_id, exc)

                await _emergency_unwind()

                return ExecutionResult(
                    status="PARTIAL",
                    run_id=run_id,
                    yes_order_id=str(yes_oid) if yes_oid else None,
                    no_order_id=str(no_oid) if no_oid else None,
                    reason=(
                        f"Partial fill detected (filled {filled_leg}, other not). "
                        f"Cancelled open orders after {self.order_timeout_s:.3f}s; attempted emergency unwind."
                    ),
                    metrics=metrics,
                )

            # Neither filled (or unknown) within window.
            return ExecutionResult(
                status="CANCELLED" if to_cancel else "FAILED",
                run_id=run_id,
                yes_order_id=str(yes_oid) if yes_oid else None,
                no_order_id=str(no_oid) if no_oid else None,
                reason=f"No dual fill within {self.order_timeout_s:.3f}s; cancelled_open={len(to_cancel)}",
                metrics=metrics,
            )

        except Exception as exc:
            logger.error("Real execution error run=%s metrics=%s err=%s", run_id, asdict(metrics), exc)
            return ExecutionResult(status="FAILED", run_id=run_id, reason=str(exc), metrics=metrics)
