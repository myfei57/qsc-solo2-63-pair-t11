"""Reconciliation: quota account, outbound records and ownership must agree.

The check is deliberately derived independently from the folded state instead
of trusting the subsystem's own counters. For every terminal, every batch and
the global bag population it recomputes what the truth must be and reports the
first disagreement it finds. A non-empty report blocks recalls and is surfaced
on the console, so "回收时能对上账" is an enforced gate, not a promise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bagsort.model.enums import (
    ACCEPTED,
    AT_HOME,
    HELD,
    IN_FLIGHT,
    PARTIAL_ACCEPT,
    PARTIAL_RECALL,
    RELEASED,
    RECALLED,
    RETURNED,
    ROLLED_BACK,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from bagsort.model.state import State

# Statuses in which a batch still represents bags physically away from home.
# ``recalled`` (fully) and ``rolled_back`` are closed states checked directly.
LIVE = ("prepared", ACCEPTED, PARTIAL_ACCEPT, PARTIAL_RECALL)
TERMINAL_STATUSES = (AT_HOME, IN_FLIGHT, RETURNED, HELD, RELEASED)

@dataclass
class ReconciliationReport:
    """The result of comparing the three views of the overflow ledger."""

    ok: bool
    errors: list[str] = field(default_factory=list)
    occupancy: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "errors": list(self.errors), "occupancy": dict(self.occupancy)}


def reconcile(state: "State") -> ReconciliationReport:
    """Cross-check capacities, ownership, outbound records and conservation."""

    errors: list[str] = []
    home = state.overflow_terminal
    backup = state.overflow_backup
    bags = state.overflow_bags
    batches = state.overflow_batches

    if not home or not backup:
        return ReconciliationReport(ok=True)

    # 1. quota account: occupancy recomputed from physical bag statuses.
    #    The home terminal may legitimately exceed capacity between the moment
    #    the queue overflows and the next diversion -- that is the trigger
    #    condition, not corruption. The backup is a hard physical bound, since
    #    transfers are only allowed to land where there are free slots.
    occupancy = {home: 0, backup: 0}
    for bag_id, bag in bags.items():
        if bag.home != home:
            errors.append(f"bag {bag_id!r} owner {bag.home!r} is not the home terminal {home!r}")
        if bag.backup != backup:
            errors.append(f"bag {bag_id!r} backup {bag.backup!r} is not {backup!r}")
        if bag.status not in TERMINAL_STATUSES:
            errors.append(f"bag {bag_id!r} has unknown status {bag.status!r}")
            continue
        if bag.status == HELD:
            occupancy[bag.backup] += 1
        elif bag.status != RELEASED:
            occupancy[bag.home] += 1
    if occupancy[backup] > state.overflow_capacity.get(backup, 0):
        errors.append(
            f"backup terminal {backup!r} occupancy {occupancy[backup]} exceeds capacity "
            f"{state.overflow_capacity.get(backup, 0)}"
        )

    # 2. outbound records vs ownership: each batch id set must match the bag
    #    rows that point back at it, and the accepted subset must be exactly
    #    the bags that landed (held) or later came home via recall/rollback.
    referenced: dict[str, list[str]] = {}
    for bag_id, bag in bags.items():
        if bag.batch_id:
            referenced.setdefault(bag.batch_id, []).append(bag_id)
    for batch_id, batch in batches.items():
        if batch.home != home or batch.backup != backup:
            errors.append(f"batch {batch_id!r} terminals do not match ledger configuration")
        members = referenced.get(batch_id, [])
        accepted_set = set(batch.accepted_ids)
        if not accepted_set.issubset(set(batch.bag_ids)):
            errors.append(f"batch {batch_id!r} accepted ids are not a subset of its bag ids")
        # A bag may only point at a batch it belongs to (a bag re-transferred
        # after a rollback legitimately points at its newer batch, so for
        # closed batches membership is a subset check, not equality).
        rogue = [bag_id for bag_id in members if bag_id not in batch.bag_ids]
        if rogue:
            errors.append(
                f"batch {batch_id!r} is referenced by bags that are not its members: {sorted(rogue)}"
            )
        if batch.status in LIVE:
            # A live batch still owns every member: all its bag ids must point
            # back at it and have a status consistent with the accepted set.
            if sorted(members) != sorted(batch.bag_ids):
                errors.append(
                    f"live batch {batch_id!r} bag list {list(batch.bag_ids)} "
                    f"!= ownership rows {sorted(members)}"
                )
            for bag_id in batch.bag_ids:
                bag = bags.get(bag_id)
                if bag is None:
                    errors.append(f"batch {batch_id!r} references missing bag {bag_id!r}")
                    continue
                if bag_id in accepted_set and bag.status == IN_FLIGHT:
                    errors.append(
                        f"batch {batch_id!r} marks {bag_id!r} accepted but it is still in flight"
                    )
                if bag_id not in accepted_set and bag.status == HELD:
                    errors.append(
                        f"batch {batch_id!r} holds {bag_id!r} at backup without an accepted mark"
                    )
                if bag_id in accepted_set and bag.status in (AT_HOME, RETURNED, RELEASED):
                    if not (bag.status == RETURNED and batch.status == PARTIAL_RECALL):
                        errors.append(
                            f"batch {batch_id!r} still live but accepted bag {bag_id!r} "
                            f"is back at home ({bag.status})"
                        )
            # A batch with no bag still in flight either is fully accepted or
            # must carry the partial-accept marker.
            in_flight = {
                bag_id for bag_id in batch.bag_ids
                if (row := bags.get(bag_id)) is not None and row.status == IN_FLIGHT
            }
            if not in_flight and accepted_set and batch.status not in (
                ACCEPTED,
                PARTIAL_RECALL,
                RECALLED,
            ):
                errors.append(
                    f"batch {batch_id!r} has no in-flight bags but status is {batch.status!r}"
                )
        elif batch.status == ROLLED_BACK:
            for bag_id in members:
                bag = bags.get(bag_id)
                if bag is not None and bag.status not in (AT_HOME, RELEASED):
                    errors.append(
                        f"rolled back batch {batch_id!r} left bag {bag_id!r} in status {bag.status}"
                    )
        elif batch.status == RECALLED:
            for bag_id in members:
                bag = bags.get(bag_id)
                if bag is not None and bag.status not in (RETURNED, RELEASED):
                    errors.append(
                        f"recalled batch {batch_id!r} left bag {bag_id!r} in status {bag.status}"
                    )

    # 3. conservation: bags never appear or disappear. Every id in a live or
    #    closed outbound record must still have exactly one ownership row.
    batch_ids = {bag_id for batch in batches.values() for bag_id in batch.bag_ids}
    missing_ownership = batch_ids - set(bags)
    if missing_ownership:
        errors.append(f"outbound record bags missing an ownership row: {sorted(missing_ownership)}")

    return ReconciliationReport(ok=not errors, errors=errors, occupancy=occupancy)
