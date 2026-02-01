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
    status: Literal["SUBMITTED", "ACK", "WAITING", "PARTIAL", "FILLED", "CANCELLED", "FAILED"]
    run_id: str
    yes_order_id: Optional[str] = None
    no_order_id: Optional[str] = None
    reason: str = ""
    metrics: Optional[ExecutionMetrics] = None
