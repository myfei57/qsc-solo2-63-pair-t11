"""Recompute the overflow history by time.

The whole story folds from the journal, so instead of trusting any live
counter this module walks the committed commands in commit order and rebuilds,
minute by minute: queue length, capacity, held bags, the policy decision and
the batch it created. It also classifies each transfer as *needed* or
*misdiverted* after the fact: a transfer was needed if, within
``needed_window`` windows after it, the un-relieved queue would have exceeded
capacity. The counterfactual subtracts every bag moved by not-yet-justified
transfers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bagsort.model.entities import OverflowDecision
from bagsort.model.enums import AT_HOME, HELD, IN_FLIGHT, RELEASED, RETURNED
from bagsort.store.journal import JournalEntry

DIVERT_ACTION = "divert"
RECALL_ACTION = "recall"


@dataclass
class HistoryRow:
    minute: str
    queue: int
    held: int
    in_flight: int
    capacity: int
    decision: str
    amount: int
    batch_id: str
    armed: bool = False
    false_alarm: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "minute": self.minute,
            "queue": self.queue,
            "held": self.held,
            "in_flight": self.in_flight,
            "capacity": self.capacity,
            "decision": self.decision,
            "amount": self.amount,
            "batch_id": self.batch_id,
            "armed": self.armed,
            "false_alarm": self.false_alarm,
        }


@dataclass
class TransferVerdict:
    batch_id: str
    minute: str
    amount: int
    needed: bool
    peak_queue_without: int
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "batch_id": self.batch_id,
            "minute": self.minute,
            "amount": self.amount,
            "needed": self.needed,
            "peak_queue_without": self.peak_queue_without,
            "reason": self.reason,
        }


@dataclass
class OverflowHistory:
    rows: list[HistoryRow]
    transfers: list[TransferVerdict]

    def as_dict(self) -> dict[str, object]:
        return {
            "rows": [row.as_dict() for row in self.rows],
            "transfers": [item.as_dict() for item in self.transfers],
            "misdiverted": sum(item.amount for item in self.transfers if not item.needed),
            "misdiverted_batches": sum(1 for item in self.transfers if not item.needed),
        }


def recompute(entries: list[JournalEntry], needed_window: int = 5) -> OverflowHistory:
    """Rebuild the timeline and mark which transfers were false transfers."""

    home = ""
    home_capacity = 0
    bags: dict[str, str] = {}
    batch_size: dict[str, int] = {}
    decisions: list[OverflowDecision] = []
    divert_events: list[tuple[str, str, int]] = []

    for entry in entries:
        kind, payload, at = entry.kind, entry.payload, entry.at
        minute = _minute_of(at)
        if kind == "overflow.configure":
            home = str(payload["home"])
            home_capacity = int(payload["capacity"][home])
        elif kind == "overflow.capacity":
            if str(payload["terminal"]) == home:
                home_capacity = int(payload["capacity"])
        elif kind == "overflow.admit":
            bag_id = str(payload["bag"]["bag_id"])
            bags[bag_id] = AT_HOME
        elif kind == "overflow.release":
            if str(payload["bag_id"]) in bags:
                bags[str(payload["bag_id"])] = RELEASED
        elif kind == "overflow.depart":
            bags.pop(str(payload["bag_id"]), None)
        elif kind == "overflow.prepare":
            batch = payload["batch"]
            batch_id = str(batch["id"])
            bag_ids = [str(item) for item in batch["bag_ids"]]
            batch_size[batch_id] = len(bag_ids)
            for bag_id in bag_ids:
                if bag_id in bags:
                    bags[bag_id] = IN_FLIGHT
            divert_events.append((minute, batch_id, len(bag_ids)))
        elif kind == "overflow.accept":
            for bag_id in payload.get("accepted_ids", ()):
                if str(bag_id) in bags:
                    bags[str(bag_id)] = HELD
        elif kind == "overflow.rollback":
            for bag_id in payload.get("bag_ids", ()):
                if str(bag_id) in bags:
                    bags[str(bag_id)] = AT_HOME
        elif kind == "overflow.recall":
            for bag_id in payload.get("bag_ids", ()):
                if str(bag_id) in bags:
                    bags[str(bag_id)] = RETURNED
        elif kind == "overflow.decision":
            decisions.append(OverflowDecision.from_dict(payload["decision"]))

    transfers = _classify(divert_events, decisions, batch_size, needed_window, home_capacity)
    false_batches = {item.batch_id for item in transfers if not item.needed}
    rows = _rows(decisions, bags, home_capacity, false_batches)
    return OverflowHistory(rows=rows, transfers=transfers)


def _rows(
    decisions: list[OverflowDecision],
    bags: dict[str, str],
    capacity: int,
    false_batches: set[str],
) -> list[HistoryRow]:
    in_flight = sum(1 for status in bags.values() if status == IN_FLIGHT)
    rows: list[HistoryRow] = []
    for decision in decisions:
        # A pre-armed transfer whose batch the counterfactual analysis later
        # marks unneeded is recorded as a false alarm.
        false_alarm = (
            decision.action == DIVERT_ACTION
            and decision.batch_id in false_batches
        )
        rows.append(
            HistoryRow(
                minute=decision.minute,
                queue=decision.queue,
                held=decision.held,
                in_flight=in_flight,
                capacity=capacity,
                decision=decision.action,
                amount=decision.amount,
                batch_id=decision.batch_id,
                armed=decision.armed,
                false_alarm=false_alarm,
            )
        )
    return rows


def _classify(
    divert_events: list[tuple[str, str, int]],
    decisions: list[OverflowDecision],
    batch_size: dict[str, int],
    window: int,
    capacity: int,
) -> list[TransferVerdict]:
    """Counterfactual: queue curve had this batch's bags never left home.

    The committed decision log already records the observed queue after each
    move; adding this batch's amount back while it was outstanding answers
    "would we have overflowed without it?" entirely from history.
    """

    verdicts: list[TransferVerdict] = []
    outstanding_at: dict[str, set[str]] = {}
    outstanding: set[str] = set()
    decisions_by_minute = sorted(decisions, key=lambda item: (item.minute, item.seq))
    for decision in decisions_by_minute:
        if decision.batch_id and decision.action == DIVERT_ACTION:
            outstanding.add(decision.batch_id)
        if decision.action == RECALL_ACTION or decision.reason.startswith("rollback"):
            outstanding.discard(decision.batch_id)
        outstanding_at[decision.minute] = set(outstanding)

    for minute, batch_id, amount in divert_events:
        start = _minute_index(minute)
        peak_without = 0
        for decision in decisions_by_minute:
            index = _minute_index(decision.minute)
            if index < start or index > start + window:
                continue
            active = batch_id in outstanding_at.get(decision.minute, set())
            counterfactual = decision.queue + (amount if active else 0)
            peak_without = max(peak_without, counterfactual)
        # The decision logged at the divert minute is post-move; include the
        # move itself in the window even if the set update landed in the same row.
        peak_without = max(peak_without, amount)
        needed = peak_without > capacity
        verdicts.append(
            TransferVerdict(
                batch_id=batch_id,
                minute=minute,
                amount=batch_size.get(batch_id, amount),
                needed=needed,
                peak_queue_without=peak_without,
                reason=(
                    f"counterfactual peak {peak_without} > capacity {capacity}"
                    if needed
                    else f"queue would have stayed at {peak_without} <= capacity {capacity}"
                ),
            )
        )
    return verdicts


def _minute_of(at: str) -> str:
    return (at or "")[:16]


def _minute_index(minute: str) -> int:
    """Minutes since epoch for ``YYYY-MM-DDTHH:MM`` strings; 0 if unparsable."""

    if not minute:
        return 0
    try:
        from datetime import datetime

        stamp = datetime.strptime(minute, "%Y-%m-%dT%H:%M")
        epoch = datetime(2024, 1, 1)
        return int((stamp - epoch).total_seconds() // 60)
    except ValueError:
        return 0


def filter_window(
    history: OverflowHistory, start: str = "", end: str = ""
) -> OverflowHistory:
    """Keep rows and transfers inside an inclusive minute range."""

    rows = [
        row
        for row in history.rows
        if (not start or row.minute >= start) and (not end or row.minute <= end)
    ]
    transfers = [
        item
        for item in history.transfers
        if (not start or item.minute >= start) and (not end or item.minute <= end)
    ]
    return OverflowHistory(rows=rows, transfers=transfers)


def summary(history: OverflowHistory) -> dict[str, Any]:
    """Aggregate numbers callers (CLI, console) present directly."""

    transfers = history.transfers
    total = sum(item.amount for item in transfers)
    misdiverted = sum(item.amount for item in transfers if not item.needed)
    armed = sum(1 for row in history.rows if row.decision == DIVERT_ACTION and row.armed)
    false_alarms = sum(1 for row in history.rows if row.false_alarm)
    return {
        "decisions": len(history.rows),
        "transfers": len(transfers),
        "bags_moved": total,
        "misdiverted_bags": misdiverted,
        "misdiverted_rate": round(misdiverted / total, 4) if total else 0.0,
        "armed_transfers": armed,
        "false_alarm_transfers": false_alarms,
        "peak_queue": max((row.queue for row in history.rows), default=0),
    }
