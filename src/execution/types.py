from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Literal


@dataclass
class ExecutionMetrics:
    # monotonic_ns timestamps
    t_detect_ns: Optional[int] = None
    t_submit_ns: Optional[int] = None
    t_ack_ns: Optional[int] = None
    t_first_fill_ns: Optional[int] = None
    t_both_filled_ns: Optional[int] = None
    t_cancel_ns: Optional[int] = None


@dataclass
class ExecutionResult:
    status: Literal["SUBMITTED", "ACK", "WAITING", "PARTIAL", "FILLED", "CANCELLED", "FAILED", "PARTIAL_OPEN"]
    run_id: str
    yes_order_id: Optional[str] = None
    no_order_id: Optional[str] = None

    # High-level reason (human-readable)
    reason: str = ""

    # Machine-readable reason code (for dashboards / alert formatting)
    reason_code: Optional[str] = None

    # Filled sizes (shares) per leg at the point we made the decision.
    # Always set when available (0.0 is valid).
    yes_filled_size: Optional[float] = None
    no_filled_size: Optional[float] = None

    # Planned order details (for Discord visibility / debugging)
    planned_yes_size: Optional[float] = None
    planned_no_size: Optional[float] = None
    planned_yes_limit: Optional[float] = None
    planned_no_limit: Optional[float] = None
    planned_est_total_cost: Optional[float] = None

    metrics: Optional[ExecutionMetrics] = None
