"""Overflow diversion subsystem.

Decides when to move excess bags to a backup terminal, how many, and when to
bring them back, with the quota account, outbound records and terminal
ownership always reconciling. See ``overflow/README.md`` for the design.
"""

from .archive import SnapshotFile, SnapshotArchive, canonical_digest, parse_name
from .controller import OverflowController, TickResult
from .forecast import ArrivalForecast, Forecast
from .history import (
    HistoryRow,
    OverflowHistory,
    TransferVerdict,
    filter_window,
    recompute,
    summary,
)
from .ledger import OverflowLedger
from .policy import (
    DIVERT,
    HOLD,
    RECALL,
    HybridPolicy,
    PolicyContext,
    PolicyDecision,
    PredictivePolicy,
    ReactivePolicy,
    build_policy,
    forecast_from,
)
from .reconcile import ReconciliationReport, reconcile
from .scenario import ScenarioResult, compare, run_policy

__all__ = [
    "SnapshotFile",
    "SnapshotArchive",
    "canonical_digest",
    "parse_name",
    "OverflowController",
    "TickResult",
    "ArrivalForecast",
    "Forecast",
    "HistoryRow",
    "OverflowHistory",
    "TransferVerdict",
    "filter_window",
    "recompute",
    "summary",
    "OverflowLedger",
    "DIVERT",
    "RECALL",
    "HOLD",
    "HybridPolicy",
    "PolicyContext",
    "PolicyDecision",
    "PredictivePolicy",
    "ReactivePolicy",
    "build_policy",
    "forecast_from",
    "ReconciliationReport",
    "reconcile",
    "ScenarioResult",
    "compare",
    "run_policy",
]
