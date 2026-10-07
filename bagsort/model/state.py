"""The folded document every subsystem reads and the reducer that builds it.

The store keeps an append-only journal of committed commands. This module turns
that journal into the materialised state and knows how to apply one command,
which is what makes restart-by-replay possible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .entities import (
    AuditEntry,
    Bag,
    ChuteAssignment,
    Flight,
    LoadRecord,
    OverflowBag,
    OverflowBatch,
    OverflowDecision,
    Sequence,
    SortRecord,
    TransferCommand,
)
from bagsort.errors import ValidationError

from .enums import (
    ACCEPTED,
    AT_HOME,
    HELD,
    IN_FLIGHT,
    PARTIAL_RECALL,
    RELEASED,
    RECALLED,
    RETURNED,
    ROLLED_BACK,
)


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
    # Overflow diversion ledger: capacities, ownership, outbound records,
    # admissions (counter.add style counters live in ``counters``) and policy
    # decisions. All four views fold from the same commands so they can never
    # disagree after replay.
    overflow_terminal: str = ""
    overflow_backup: str = ""
    overflow_capacity: dict[str, int] = field(default_factory=dict)
    overflow_bags: dict[str, OverflowBag] = field(default_factory=dict)
    overflow_batches: dict[str, OverflowBatch] = field(default_factory=dict)
    overflow_admitted: dict[str, str] = field(default_factory=dict)
    overflow_decisions: list[OverflowDecision] = field(default_factory=list)

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
            overflow_terminal=self.overflow_terminal,
            overflow_backup=self.overflow_backup,
            overflow_capacity=dict(self.overflow_capacity),
            overflow_bags=dict(self.overflow_bags),
            overflow_batches=dict(self.overflow_batches),
            overflow_admitted=dict(self.overflow_admitted),
            overflow_decisions=list(self.overflow_decisions),
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
            "overflow": {
                "terminal": self.overflow_terminal,
                "backup": self.overflow_backup,
                "capacity": dict(sorted(self.overflow_capacity.items())),
                "bags": {
                    key: value.as_dict() for key, value in sorted(self.overflow_bags.items())
                },
                "batches": {
                    key: value.as_dict() for key, value in sorted(self.overflow_batches.items())
                },
                "admitted": dict(sorted(self.overflow_admitted.items())),
                "decisions": [item.as_dict() for item in self.overflow_decisions],
            },
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
    elif kind == "overflow.configure":
        state.overflow_terminal = str(payload["home"])
        state.overflow_backup = str(payload["backup"])
        state.overflow_capacity = {
            str(key): int(value) for key, value in payload.get("capacity", {}).items()
        }
    elif kind == "overflow.capacity":
        state.overflow_capacity[str(payload["terminal"])] = int(payload["capacity"])
    elif kind == "overflow.admit":
        bag = OverflowBag.from_dict(payload["bag"])
        state.overflow_bags[bag.bag_id] = bag
        state.overflow_admitted[bag.bag_id] = str(payload.get("minute", ""))
    elif kind == "overflow.release":
        bag_id = str(payload["bag_id"])
        current = state.overflow_bags.get(bag_id)
        if current is not None:
            state.overflow_bags[bag_id] = current.evolved(status=RELEASED)
        state.overflow_admitted.pop(bag_id, None)
    elif kind == "overflow.depart":
        bag_id = str(payload["bag_id"])
        state.overflow_bags.pop(bag_id, None)
        state.overflow_admitted.pop(bag_id, None)
    elif kind == "overflow.prepare":
        batch = OverflowBatch.from_dict(payload["batch"])
        state.overflow_batches[batch.id] = batch
        for bag_id in batch.bag_ids:
            current = state.overflow_bags.get(bag_id)
            if current is not None:
                state.overflow_bags[bag_id] = current.evolved(
                    status=IN_FLIGHT, batch_id=batch.id
                )
    elif kind == "overflow.accept":
        batch_id = str(payload["batch_id"])
        batch = state.overflow_batches[batch_id]
        accepted = tuple(str(item) for item in payload.get("accepted_ids", ()))
        batch = batch.evolved(
            accepted_ids=tuple(dict.fromkeys((*batch.accepted_ids, *accepted))),
            status=str(payload.get("status", ACCEPTED)),
            updated_at=str(payload.get("at", batch.updated_at)),
        )
        state.overflow_batches[batch_id] = batch
        for bag_id in accepted:
            current = state.overflow_bags.get(bag_id)
            if current is not None:
                state.overflow_bags[bag_id] = current.evolved(status=HELD, batch_id=batch_id)
    elif kind == "overflow.rollback":
        batch_id = str(payload["batch_id"])
        batch = state.overflow_batches[batch_id]
        returned = tuple(str(item) for item in payload.get("bag_ids", ()))
        batch = batch.evolved(
            status=ROLLED_BACK,
            updated_at=str(payload.get("at", batch.updated_at)),
        )
        state.overflow_batches[batch_id] = batch
        for bag_id in batch.bag_ids:
            current = state.overflow_bags.get(bag_id)
            if current is None:
                continue
            if bag_id in returned or bag_id not in batch.accepted_ids:
                state.overflow_bags[bag_id] = current.evolved(
                    status=AT_HOME, batch_id=batch_id
                )
            else:
                state.overflow_bags[bag_id] = current.evolved(status=HELD, batch_id=batch_id)
    elif kind == "overflow.recall":
        batch_id = str(payload["batch_id"])
        batch = state.overflow_batches[batch_id]
        recalled = tuple(str(item) for item in payload.get("bag_ids", ()))
        remaining = tuple(bag_id for bag_id in batch.accepted_ids if bag_id not in recalled)
        batch = batch.evolved(
            status=RECALLED if not remaining else PARTIAL_RECALL,
            updated_at=str(payload.get("at", batch.updated_at)),
        )
        state.overflow_batches[batch_id] = batch
        for bag_id in recalled:
            current = state.overflow_bags.get(bag_id)
            if current is not None:
                state.overflow_bags[bag_id] = current.evolved(
                    status=RETURNED, batch_id=batch_id
                )
    elif kind == "overflow.decision":
        state.overflow_decisions.append(OverflowDecision.from_dict(payload["decision"]))
    else:  # pragma: no cover - guards against a typo in a new command kind
        raise ValidationError(f"unknown journal command {kind!r}")


def fold(entries: list[tuple[str, dict[str, Any]]]) -> State:
    """Rebuild a :class:`State` from a sequence of ``(kind, payload)`` pairs."""

    state = State()
    for kind, payload in entries:
        apply_entry(state, kind, payload)
    return state
