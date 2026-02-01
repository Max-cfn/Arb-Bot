from __future__ import annotations

import asyncio
import os
import time
from dataclasses import asdict
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, OpenOrderParams
from py_clob_client.order_builder.constants import BUY

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
        self.order_timeout_s = float(os.getenv("CLOB_ORDER_TIMEOUT_S", "1.5"))
        self.poll_interval_s = float(os.getenv("CLOB_POLL_INTERVAL_S", "0.05"))

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

        def _submit_both_sync():
            # Create signed limit orders. We use FOK to avoid leaving resting exposure.
            yes = OrderArgs(token_id=str(opp.yes_token_id), price=yes_price, size=yes_size, side=BUY)
            no = OrderArgs(token_id=str(opp.no_token_id), price=no_price, size=no_size, side=BUY)

            signed_yes = client.create_order(yes)
            signed_no = client.create_order(no)

            resp_yes = client.post_order(signed_yes, OrderType.FOK)
            resp_no = client.post_order(signed_no, OrderType.FOK)
            return resp_yes, resp_no

        metrics.t_submit_ns = time.monotonic_ns()

        try:
            resp_yes, resp_no = await asyncio.to_thread(_submit_both_sync)
            metrics.t_ack_ns = time.monotonic_ns()

            # Best-effort order ids
            yes_oid = (resp_yes or {}).get("orderID") or (resp_yes or {}).get("id")
            no_oid = (resp_no or {}).get("orderID") or (resp_no or {}).get("id")

            logger.info(
                "EXEC_ACK run=%s yes_oid=%s no_oid=%s submit_to_ack_ms=%.3f resp_yes=%s resp_no=%s",
                run_id,
                yes_oid,
                no_oid,
                (metrics.t_ack_ns - metrics.t_submit_ns) / 1e6,
                str(resp_yes)[:500],
                str(resp_no)[:500],
            )

            # Poll open orders briefly to detect immediate cancellation vs fill.
            # NOTE: For FOK, many outcomes will be immediate fill or immediate cancel.
            async def _poll_open_once():
                return await asyncio.to_thread(lambda: client.get_orders(OpenOrderParams()))

            deadline = time.monotonic() + self.order_timeout_s
            last_open = None
            while time.monotonic() < deadline:
                try:
                    last_open = await _poll_open_once()
                    open_ids = {o.get("id") for o in (last_open or []) if isinstance(o, dict)}
                    # if order ids exist and are still open, keep waiting
                    if (yes_oid and yes_oid in open_ids) or (no_oid and no_oid in open_ids):
                        await asyncio.sleep(self.poll_interval_s)
                        continue
                    # Not open anymore => likely filled or cancelled
                    break
                except Exception:
                    break

            # Without a dedicated fills endpoint here, we classify:
            # - If neither id is open after polling window => treat as DONE (could be filled/cancelled)
            # - If either remains open => CANCELLED via cancel_all (safety)
            if last_open is not None:
                open_ids = {o.get("id") for o in (last_open or []) if isinstance(o, dict)}
                still_open = (yes_oid and yes_oid in open_ids) or (no_oid and no_oid in open_ids)
            else:
                still_open = False

            if still_open:
                # Safety: cancel all outstanding orders
                await asyncio.to_thread(client.cancel_all)
                metrics.t_cancel_ns = time.monotonic_ns()
                return ExecutionResult(
                    status="CANCELLED",
                    run_id=run_id,
                    yes_order_id=str(yes_oid) if yes_oid else None,
                    no_order_id=str(no_oid) if no_oid else None,
                    reason=f"Timeout; cancelled all. submit→cancel={(metrics.t_cancel_ns-metrics.t_submit_ns)/1e6:.3f}ms",
                    metrics=metrics,
                )

            # Assume completed quickly (FOK semantics): mark as FILLED if both ids returned, else FAILED.
            metrics.t_both_filled_ns = time.monotonic_ns()
            status = "FILLED" if (yes_oid and no_oid) else "FAILED"
            return ExecutionResult(
                status=status,  # best-effort
                run_id=run_id,
                yes_order_id=str(yes_oid) if yes_oid else None,
                no_order_id=str(no_oid) if no_oid else None,
                reason=f"submit→ack={(metrics.t_ack_ns-metrics.t_submit_ns)/1e6:.3f}ms submit→done={(metrics.t_both_filled_ns-metrics.t_submit_ns)/1e6:.3f}ms",
                metrics=metrics,
            )

        except Exception as exc:
            logger.error("Real execution error run=%s metrics=%s err=%s", run_id, asdict(metrics), exc)
            return ExecutionResult(status="FAILED", run_id=run_id, reason=str(exc), metrics=metrics)
