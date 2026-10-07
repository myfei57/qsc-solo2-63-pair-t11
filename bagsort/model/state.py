"""The folded document every subsystem reads and the reducer that builds it.

The store keeps an append-only journal of committed commands. This module turns
that journal into the materialised state and knows how to apply one command,
which is what makes restart-by-replay possible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .entities import (
    ArrivalSample,
    AuditEntry,
    Bag,
    ChuteAssignment,
    EvidenceAnchor,
    ExternalRecord,
    Flight,
    LoadRecord,
    OverflowBatch,
    Sequence,
    SortRecord,
    TransferCommand,
)
from bagsort.errors import ValidationError


@dataclass
class State:
    """Materialised view of every committed command."""

    flights: dict[str, Flight] = field(default_factory=dict)
    bags: dict[str, Bag] = field(default_factory=dict)
    mappings: dict[str, str] = field(default_factory=dict)
    assignments: dict[str, ChuteAssignment] = field(default_factory=dict)
    sort_records: list[SortRecord] = field(default_factory=list)
    transfer_commands: list[TransferCommand] = field(default_factory=list)
    load_records: list[LoadRecord] = field(default_factory=list)
    audit: list[AuditEntry] = field(default_factory=list)
    quota_limits: dict[str, int] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    sequence: Sequence = field(default_factory=Sequence)
    recheck_queue: list[str] = field(default_factory=list)
    belt_carousel: str = ""
    belt_batch: int = 0
    belt_on_carousel: list[str] = field(default_factory=list)
    overflow_batches: list[OverflowBatch] = field(default_factory=list)
    external_records: list[ExternalRecord] = field(default_factory=list)
    overflow_capacity: dict[str, int] = field(default_factory=dict)
    overflow_granted: dict[str, int] = field(default_factory=dict)
    overflow_policy: dict[str, Any] = field(default_factory=dict)
    arrival_samples: list[ArrivalSample] = field(default_factory=list)
    evidence_anchors: list[EvidenceAnchor] = field(default_factory=list)

    def copy(self) -> "State":
        return State(
            flights=dict(self.flights),
            bags=dict(self.bags),
            mappings=dict(self.mappings),
            assignments=dict(self.assignments),
            sort_records=list(self.sort_records),
            transfer_commands=list(self.transfer_commands),
            load_records=list(self.load_records),
            audit=list(self.audit),
            quota_limits=dict(self.quota_limits),
            counters=dict(self.counters),
            sequence=self.sequence,
            recheck_queue=list(self.recheck_queue),
            belt_carousel=self.belt_carousel,
            belt_batch=self.belt_batch,
            belt_on_carousel=list(self.belt_on_carousel),
            overflow_batches=list(self.overflow_batches),
            external_records=list(self.external_records),
            overflow_capacity=dict(self.overflow_capacity),
            overflow_granted=dict(self.overflow_granted),
            overflow_policy=dict(self.overflow_policy),
            arrival_samples=list(self.arrival_samples),
            evidence_anchors=list(self.evidence_anchors),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "flights": {key: value.as_dict() for key, value in sorted(self.flights.items())},
            "bags": {key: value.as_dict() for key, value in sorted(self.bags.items())},
            "mappings": dict(sorted(self.mappings.items())),
            "assignments": {
                key: value.as_dict() for key, value in sorted(self.assignments.items())
            },
            "sort_records": [record.as_dict() for record in self.sort_records],
            "transfer_commands": [command.as_dict() for command in self.transfer_commands],
            "load_records": [record.as_dict() for record in self.load_records],
            "audit": [entry.as_dict() for entry in self.audit],
            "quota_limits": dict(sorted(self.quota_limits.items())),
            "counters": dict(sorted(self.counters.items())),
            "sequence": self.sequence.as_dict(),
            "recheck_queue": list(self.recheck_queue),
            "belt": {
                "carousel": self.belt_carousel,
                "batch": self.belt_batch,
                "on_carousel": list(self.belt_on_carousel),
            },
            "overflow_batches": [batch.as_dict() for batch in self.overflow_batches],
            "external_records": [record.as_dict() for record in self.external_records],
            "overflow_capacity": dict(sorted(self.overflow_capacity.items())),
            "overflow_granted": dict(sorted(self.overflow_granted.items())),
            "overflow_policy": dict(sorted(self.overflow_policy.items())),
            "arrival_samples": [sample.as_dict() for sample in self.arrival_samples],
            "evidence_anchors": [anchor.as_dict() for anchor in self.evidence_anchors],
        }

def apply_entry(state: State, kind: str, payload: dict[str, Any]) -> None:
    """Apply one journal command to ``state`` in place."""

    if kind == "flight.put":
        flight = Flight.from_dict(payload["flight"])
        state.flights[flight.id] = flight
    elif kind == "flight.remove":
        state.flights.pop(str(payload["flight_id"]), None)
        state.mappings = {
            bag_id: flight_id
            for bag_id, flight_id in state.mappings.items()
            if flight_id != str(payload["flight_id"])
        }
    elif kind == "chute.assign":
        assignment = ChuteAssignment.from_dict(payload)
        state.assignments[assignment.flight_id] = assignment
    elif kind == "bag.put":
        bag = Bag.from_dict(payload["bag"])
        state.bags[bag.id] = bag
    elif kind == "bag.remove":
        state.bags.pop(str(payload["bag_id"]), None)
        state.mappings.pop(str(payload["bag_id"]), None)
    elif kind == "mapping.put":
        state.mappings[str(payload["bag_id"])] = str(payload["flight_id"])
    elif kind == "sequence.put":
        state.sequence = Sequence.from_dict(payload)
    elif kind == "sort.record":
        state.sort_records.append(SortRecord.from_dict(payload["record"]))
    elif kind == "transfer.append":
        state.transfer_commands.append(TransferCommand.from_dict(payload["command"]))
    elif kind == "transfer.clear_before":
        floor = int(payload["batch"])
        state.transfer_commands = [
            command for command in state.transfer_commands if command.batch >= floor
        ]
    elif kind == "load.append":
        state.load_records.append(LoadRecord.from_dict(payload["record"]))
    elif kind == "audit.append":
        state.audit.append(AuditEntry.from_dict(payload["entry"]))
    elif kind == "quota.set":
        state.quota_limits[str(payload["flight_id"])] = int(payload["limit"])
    elif kind == "quota.adjust":
        flight_id = str(payload["flight_id"])
        base = state.quota_limits.get(flight_id, 0)
        state.quota_limits[flight_id] = base + int(payload["delta"])
    elif kind == "counter.add":
        name = str(payload["name"])
        state.counters[name] = state.counters.get(name, 0) + int(payload["delta"])
    elif kind == "recheck.enqueue":
        bag_id = str(payload["bag_id"])
        if bag_id not in state.recheck_queue:
            state.recheck_queue.append(bag_id)
    elif kind == "recheck.dequeue":
        bag_id = str(payload["bag_id"])
        state.recheck_queue = [item for item in state.recheck_queue if item != bag_id]
    elif kind == "recheck.clear":
        state.recheck_queue = []
    elif kind == "belt.set":
        state.belt_carousel = str(payload.get("carousel", ""))
        state.belt_batch = int(payload.get("batch", 0))
    elif kind == "belt.place":
        bag_id = str(payload["bag_id"])
        if bag_id not in state.belt_on_carousel:
            state.belt_on_carousel.append(bag_id)
    elif kind == "belt.clear":
        state.belt_on_carousel = []
        state.belt_carousel = str(payload.get("carousel", state.belt_carousel))
        state.belt_batch = int(payload.get("batch", state.belt_batch))
    elif kind == "overflow.batch.put":
        batch = OverflowBatch.from_dict(payload["batch"])
        state.overflow_batches = [
            item for item in state.overflow_batches if item.id != batch.id
        ]
        state.overflow_batches.append(batch)
    elif kind == "overflow.record.put":
        record = ExternalRecord.from_dict(payload["record"])
        state.external_records = [
            item
            for item in state.external_records
            if not (item.bag_id == record.bag_id and item.batch_id == record.batch_id)
        ]
        state.external_records.append(record)
    elif kind == "overflow.capacity.set":
        state.overflow_capacity[str(payload["backup_terminal"])] = int(payload["amount"])
    elif kind == "overflow.granted":
        terminal = str(payload["backup_terminal"])
        base = int(state.overflow_granted.get(terminal, 0))
        state.overflow_granted[terminal] = base + int(payload["delta"])
    elif kind == "overflow.policy.set":
        state.overflow_policy = dict(payload["policy"])
    elif kind == "overflow.arrival":
        state.arrival_samples.append(
            ArrivalSample(at=str(payload["at"]), count=int(payload["count"]))
        )
    elif kind == "overflow.evidence":
        state.evidence_anchors.append(EvidenceAnchor.from_dict(payload["anchor"]))
    else:  # pragma: no cover - guards against a typo in a new command kind
        raise ValidationError(f"unknown journal command {kind!r}")


def fold(entries: list[tuple[str, dict[str, Any]]]) -> State:
    """Rebuild a :class:`State` from a sequence of ``(kind, payload)`` pairs."""

    state = State()
    for kind, payload in entries:
        apply_entry(state, kind, payload)
    return state
