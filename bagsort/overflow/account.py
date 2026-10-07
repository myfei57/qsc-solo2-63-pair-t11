"""Backup terminal capacity accounts.

Each backup terminal is *granted* a capacity allowance by the home terminal and
*holds* one unit per bag currently in its custody. The account is the first of
the three books that must agree on every overflow movement:

1. the granted/used account (this ledger),
2. the external records (one per bag sent out),
3. bag ownership (each bag's current terminal and its batch status).

Every change is a committed command, so the account survives a restart and the
whole history can be folded again to recompute the balance.
"""

from __future__ import annotations

from bagsort.clock import Clock
from bagsort.errors import ConflictError, ValidationError
from bagsort.model.entities import ExternalRecord, OverflowBatch
from bagsort.store.store import Store


class OverflowAccount:
    """Granted allowance versus bags currently held, per backup terminal."""

    def __init__(self, store: Store, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    def grant(self, backup_terminal: str, amount: int) -> int:
        terminal = (backup_terminal or "").strip().upper()
        if not terminal:
            raise ValidationError("backup terminal must not be empty")
        if amount < 0:
            raise ValidationError("granted amount must not be negative")
        with self._store.session() as session:
            session.state.overflow_capacity[terminal] = amount
            session.record(
                "overflow.capacity.set",
                {"backup_terminal": terminal, "amount": amount},
                self._clock.now_iso(),
            )
        return amount

    def granted(self, backup_terminal: str) -> int:
        return int(self._store.state().overflow_capacity.get(backup_terminal, 0))

    def held(self, backup_terminal: str) -> int:
        """Bags the backup currently has custody of."""

        return sum(
            1
            for record in self._store.state().external_records
            if record.backup_terminal == backup_terminal and record.state == "outbound"
        )

    def available(self, backup_terminal: str) -> int:
        return self.granted(backup_terminal) - self.held(backup_terminal)

    def accounts(self) -> dict[str, dict[str, int]]:
        state = self._store.state()
        terminals = set(state.overflow_capacity) | {
            record.backup_terminal for record in state.external_records
        }
        return {
            terminal: {
                "granted": self.granted(terminal),
                "held": self.held(terminal),
                "available": self.available(terminal),
                "movement_balance": int(
                    state.overflow_granted.get(terminal, 0)
                ),
            }
            for terminal in sorted(terminals)
        }

    def require_capacity(self, backup_terminal: str, count: int) -> None:
        """Reject a shipment the backup account cannot cover."""

        granted = self.granted(backup_terminal)
        if granted <= 0:
            raise ConflictError(
                f"backup terminal {backup_terminal!r} has no granted overflow allowance"
            )
        if self.available(backup_terminal) < count:
            raise ConflictError(
                f"backup terminal {backup_terminal!r} can take "
                f"{self.available(backup_terminal)} more, {count} requested"
            )

    def reconcile(self) -> list[str]:
        """Cross-check accounts against external records and batch ownership.

        Returns the list of discrepancies; an empty list means the three books
        agree.
        """

        problems: list[str] = []
        state = self._store.state()
        for terminal, balance in self.accounts().items():
            if balance["available"] < 0:
                problems.append(
                    f"terminal {terminal!r} holds {balance['held']} "
                    f"against {balance['granted']} granted"
                )
            # The movement balance nets sends (-n) against returns and rollbacks
            # (+n); any residue means a bag was booked on only one side.
            expected_balance = -balance["held"]
            if balance["movement_balance"] != expected_balance:
                problems.append(
                    f"terminal {terminal!r} movement balance "
                    f"{balance['movement_balance']} does not net against "
                    f"{balance['held']} held bags"
                )
        outbound = [r for r in state.external_records if r.state == "outbound"]
        for record in outbound:
            bag = state.bags.get(record.bag_id)
            if bag is None:
                problems.append(f"external record {record.bag_id!r} has no bag")
                continue
            if bag.terminal != record.backup_terminal:
                problems.append(
                    f"bag {record.bag_id!r} is at {bag.terminal!r} but the external "
                    f"record says {record.backup_terminal!r}"
                )
        batches = {batch.id: batch for batch in state.overflow_batches}
        for record in state.external_records:
            batch = batches.get(record.batch_id)
            if batch is None:
                problems.append(f"external record {record.bag_id!r} names missing batch")
                continue
            if record.bag_id not in batch.bag_ids:
                problems.append(
                    f"external record {record.bag_id!r} is not part of its batch {batch.id!r}"
                )
        for batch in state.overflow_batches:
            records = [
                r
                for r in state.external_records
                if r.batch_id == batch.id
                and r.state in {"outbound", "returned", "rolled_back"}
            ]
            if len(records) != len(batch.bag_ids):
                problems.append(
                    f"batch {batch.id!r} names {len(batch.bag_ids)} bags but has "
                    f"{len(records)} external records"
                )
            if batch.status == "outbound":
                still_out = [
                    record
                    for record in records
                    if record.state == "outbound"
                ]
                for record in still_out:
                    bag = state.bags.get(record.bag_id)
                    if bag is not None and bag.terminal != batch.backup_terminal:
                        problems.append(
                            f"outbound batch {batch.id!r}: bag {record.bag_id!r} is "
                            f"back at {bag.terminal!r}"
                        )
            if batch.status in {"returned", "rolled_back"}:
                for record in records:
                    bag = state.bags.get(record.bag_id)
                    if bag is None:
                        problems.append(
                            f"{batch.status} batch {batch.id!r}: bag {record.bag_id!r} missing"
                        )
                        continue
                    current = next(
                        (
                            r
                            for r in state.external_records
                            if r.bag_id == record.bag_id and r.state == "outbound"
                        ),
                        None,
                    )
                    # A later, still outbound shipment for the same bag is fine.
                    # Otherwise it must be back at the home terminal.
                    if current is None and bag.terminal != batch.home_terminal:
                        problems.append(
                            f"{batch.status} batch {batch.id!r}: bag {record.bag_id!r} "
                            f"never came home to {batch.home_terminal!r}"
                        )
        return problems
