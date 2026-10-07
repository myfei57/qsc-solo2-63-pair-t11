"""Overflow diversion ledger: quota account, ownership and outbound records.

This is the single writer every overflow move goes through. Three views of the
same journal must always agree:

* **quota account** -- how many bags physically occupy each terminal against
  its capacity (``occupancy`` / ``free``);
* **ownership** -- every bag keeps ``home`` as its owner for its whole life,
  even while physically held at the backup;
* **outbound records** -- :class:`OverflowBatch` rows listing exactly which
  bags were offered, which were accepted, and whether the move was rolled back
  or recalled.

Every mutation is one committed journal session, so the three views fold from
identical commands and a replay rebuilds them byte for byte.
"""

from __future__ import annotations

from bagsort.clock import Clock
from bagsort.errors import ConflictError, NotFoundError, QuotaError, ValidationError
from bagsort.model.entities import OverflowBag, OverflowBatch
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
from bagsort.model.state import State
from bagsort.store.store import Store

CLOSED_BATCH_STATUSES = (ROLLED_BACK, RECALLED, PARTIAL_RECALL)
LIVE_BATCH_STATUSES = ("prepared", ACCEPTED, PARTIAL_ACCEPT)


class OverflowLedger:
    """Owns the overflow accounts and validates every state transition."""

    def __init__(self, store: Store, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    # -- configuration -----------------------------------------------------

    def configure(self, home: str, backup: str, home_capacity: int, backup_capacity: int) -> None:
        home = (home or "").strip().upper()
        backup = (backup or "").strip().upper()
        if not home or not backup or home == backup:
            raise ValidationError("home and backup must be two different terminals")
        if home_capacity <= 0 or backup_capacity <= 0:
            raise ValidationError("terminal capacities must be positive")
        at = self._clock.now_iso()
        with self._store.session() as session:
            session.state.overflow_terminal = home
            session.state.overflow_backup = backup
            session.state.overflow_capacity = {home: home_capacity, backup: backup_capacity}
            session.record(
                "overflow.configure",
                {
                    "home": home,
                    "backup": backup,
                    "capacity": {home: home_capacity, backup: backup_capacity},
                },
                at,
            )

    def set_capacity(self, terminal: str, capacity: int) -> int:
        terminal = (terminal or "").strip().upper()
        if capacity < 0:
            raise ValidationError("capacity must not be negative")
        if not self._state().overflow_capacity and terminal not in self._terminals():
            raise ConflictError("overflow ledger is not configured")
        at = self._clock.now_iso()
        with self._store.session() as session:
            session.state.overflow_capacity[terminal] = capacity
            session.record(
                "overflow.capacity",
                {"terminal": terminal, "capacity": capacity},
                at,
            )
        return capacity

    # -- admission / release ----------------------------------------------

    def admit(self, bag_id: str) -> OverflowBag:
        """Register a bag arriving at the home terminal queue.

        Admission past capacity is deliberately allowed: a queue longer than
        capacity *is* the overflow condition the policies trigger on, so
        refusing entry would hide the very signal we must react to. The
        capacity account is enforced on the *physical* side -- transfers only
        land at a backup that has free slots (see :meth:`accept`).
        """

        bag_id = (bag_id or "").strip()
        if not bag_id:
            raise ValidationError("bag id must not be empty")
        home, backup = self._require_terminals()
        existing = self._state().overflow_bags.get(bag_id)
        if existing is not None and existing.status not in (AT_HOME, RELEASED):
            # A bag that is in flight, held at the backup or back but not yet
            # released must not be booked into the home queue again.
            raise ConflictError(
                f"bag {bag_id!r} is already on the overflow ledger "
                f"(status={existing.status}); it must be recalled and released first"
            )
        bag = OverflowBag(bag_id=bag_id, home=home, backup=backup, status=AT_HOME)
        at = self._clock.now_iso()
        with self._store.session() as session:
            session.state.overflow_bags[bag_id] = bag
            session.state.overflow_admitted[bag_id] = at
            session.record("overflow.admit", {"bag": bag.as_dict(), "minute": at}, at)
            session.record(
                "counter.add", {"name": "overflow.admitted", "delta": 1}, at
            )
        return bag

    def can_admit(self, bag_id: str) -> bool:
        """Guard used by check-in: outbound/recalled bags stay blocked.

        ``at_home`` rows (e.g. a re-checked-in barcode) and the retained
        ``released`` historical marker do not block admission.
        """

        existing = self._state().overflow_bags.get(bag_id)
        return existing is None or existing.status in (AT_HOME, RELEASED)

    def depart_home(self, bag_id: str) -> None:
        """A home-side bag leaves the system (normal processing).

        Both ``at_home`` bags and bags that came back via recall
        (``returned``) are released here; the latter is the final step of the
        post-peak closeout. The ownership row is kept in the terminal
        ``released`` state rather than deleted so every outbound record keeps
        reconciling after the bag is gone.
        """

        bag_id = (bag_id or "").strip()
        bag = self._require_bag(bag_id)
        if bag.status not in (AT_HOME, RETURNED):
            raise ConflictError(f"bag {bag_id!r} is not at home (status={bag.status})")
        at = self._clock.now_iso()
        with self._store.session() as session:
            session.state.overflow_bags[bag_id] = bag.evolved(status=RELEASED)
            session.state.overflow_admitted.pop(bag_id, None)
            session.record("overflow.release", {"bag_id": bag_id}, at)
            session.record(
                "counter.add", {"name": "overflow.departed", "delta": 1}, at
            )

    # -- transfer ----------------------------------------------------------

    def prepare(self, amount: int, reason: str = "") -> OverflowBatch:
        """Reserve up to ``amount`` queued bags for one outbound transfer.

        The newest queued bags are picked first (LIFO): bags that have waited
        longest are protected from being bounced to the backup.
        """

        if amount <= 0:
            raise ValidationError("transfer amount must be positive")
        home, backup = self._require_terminals()
        queued = self.home_queue()
        if not queued:
            raise ConflictError("home queue is empty; nothing to transfer")
        if self.free(backup) <= 0:
            raise QuotaError(f"backup terminal {backup!r} has no free capacity")
        chosen = sorted(queued, key=lambda bag: bag.bag_id, reverse=True)
        bag_ids = tuple(bag.bag_id for bag in chosen[:amount])
        batch_id = self._next_batch_id()
        at = self._clock.now_iso()
        batch = OverflowBatch(
            id=batch_id,
            home=home,
            backup=backup,
            bag_ids=bag_ids,
            accepted_ids=(),
            status="prepared",
            reason=reason,
            created_at=at,
            updated_at=at,
        )
        with self._store.session() as session:
            session.state.overflow_batches[batch_id] = batch
            for bag_id in bag_ids:
                current = session.state.overflow_bags[bag_id]
                session.state.overflow_bags[bag_id] = current.evolved(
                    status=IN_FLIGHT, batch_id=batch_id
                )
            session.record(
                "overflow.prepare",
                {"batch": batch.as_dict(), "status": IN_FLIGHT},
                at,
            )
            session.record(
                "counter.add", {"name": "overflow.prepared", "delta": len(bag_ids)}, at
            )
        return batch

    def accept(
        self, batch_id: str, bag_ids: list[str] | tuple[str, ...] | None = None
    ) -> OverflowBatch:
        """Confirm bags physically reached the backup.

        ``bag_ids=None`` means the whole batch landed. A subset models the
        outbound link dropping part of a batch; the rest stay in flight until a
        rollback settles them.
        """

        batch = self._require_batch(batch_id)
        self._require_live(batch)
        if batch.status not in ("prepared", PARTIAL_ACCEPT):
            raise ConflictError(f"batch {batch_id!r} already fully accepted")
        offered = set(bag.bag_id for bag in self._bags_of(batch))
        if bag_ids is None:
            accepted = tuple(bag_id for bag_id in batch.bag_ids if bag_id in offered)
        else:
            wanted = tuple(dict.fromkeys(str(item) for item in bag_ids))
            unknown = [bag_id for bag_id in wanted if bag_id not in batch.bag_ids]
            if unknown:
                raise ValidationError(f"bags {unknown} are not in batch {batch_id!r}")
            already = [bag_id for bag_id in wanted if bag_id in batch.accepted_ids]
            if already:
                raise ConflictError(f"bags {already} already accepted")
            in_flight = [
                bag_id
                for bag_id in wanted
                if (record := self._state().overflow_bags.get(bag_id)) is not None
                and record.status == IN_FLIGHT
            ]
            accepted = tuple(in_flight)
        if not accepted:
            raise ConflictError(f"no in-flight bags left to accept in {batch_id!r}")
        free = self.free(batch.backup)
        if len(accepted) > free:
            raise QuotaError(
                f"backup {batch.backup!r} can take {free}, asked to accept {len(accepted)}"
            )
        merged = tuple(dict.fromkeys((*batch.accepted_ids, *accepted)))
        status = ACCEPTED if len(merged) == len(batch.bag_ids) else PARTIAL_ACCEPT
        at = self._clock.now_iso()
        updated = batch.evolved(accepted_ids=merged, status=status, updated_at=at)
        with self._store.session() as session:
            session.state.overflow_batches[batch_id] = updated
            for bag_id in accepted:
                current = session.state.overflow_bags[bag_id]
                session.state.overflow_bags[bag_id] = current.evolved(
                    status=HELD, batch_id=batch_id
                )
            session.record(
                "overflow.accept",
                {"batch_id": batch_id, "accepted_ids": list(accepted), "status": status, "at": at},
                at,
            )
            session.record(
                "counter.add", {"name": "overflow.accepted", "delta": len(accepted)}, at
            )
        return updated

    def rollback(self, batch_id: str, reason: str = "outbound_interrupt") -> OverflowBatch:
        """Undo a transfer after an outbound interruption.

        Bags that never reached the backup (still in flight) come straight home;
        bags that did reach it but have not been recalled are returned from the
        backup side too, because an interrupted transfer cannot leave ownership
        custody split without an outbound record describing it -- and here that
        record is closed as ``rolled_back`` with every bag accounted for.
        """

        batch = self._require_batch(batch_id)
        self._require_live(batch)
        held = [
            bag.bag_id
            for bag in self._bags_of(batch)
            if bag.status == HELD
        ]
        at = self._clock.now_iso()
        updated = batch.evolved(status=ROLLED_BACK, updated_at=at)
        with self._store.session() as session:
            session.state.overflow_batches[batch_id] = updated
            for bag_id in batch.bag_ids:
                current = session.state.overflow_bags[bag_id]
                session.state.overflow_bags[bag_id] = current.evolved(
                    status=AT_HOME, batch_id=batch_id
                )
            session.record(
                "overflow.rollback",
                {
                    "batch_id": batch_id,
                    "bag_ids": list(batch.bag_ids),
                    "returned_from_backup": held,
                    "reason": reason,
                    "at": at,
                },
                at,
            )
            session.record(
                "counter.add",
                {"name": "overflow.rolled_back", "delta": len(batch.bag_ids)},
                at,
            )
        return updated

    def recall(self, batch_id: str, bag_ids: list[str] | tuple[str, ...] | None = None) -> OverflowBatch:
        """Bring transferred bags home after the peak. Reconciles before moving.

        Only bags physically held at the backup can be recalled, and the recall
        is refused unless quota, ownership and the outbound record already
        agree, so a recall can never book against a broken ledger.
        """

        batch = self._require_batch(batch_id)
        if batch.status not in (ACCEPTED, PARTIAL_ACCEPT, PARTIAL_RECALL):
            raise ConflictError(f"batch {batch_id!r} is not recallable (status={batch.status})")
        held_ids = [
            bag.bag_id
            for bag in self._bags_of(batch)
            if bag.status == HELD
        ]
        wanted = list(held_ids) if bag_ids is None else [str(item) for item in bag_ids]
        unknown = [bag_id for bag_id in wanted if bag_id not in batch.accepted_ids]
        if unknown:
            raise ValidationError(f"bags {unknown} were not accepted in {batch_id!r}")
        not_held = [
            bag_id for bag_id in wanted
            if (record := self._state().overflow_bags.get(bag_id)) is None
            or record.status != HELD
        ]
        if not_held:
            raise ConflictError(f"bags {not_held} are not held at the backup")
        # Home occupancy includes returned bags already back; recalled bags
        # would add to that count, so test against occupancy, not free() (which
        # already excludes only the held bags at the backup).
        room = self.capacity(batch.home) - self.occupancy(batch.home)
        if room < len(wanted):
            raise QuotaError(
                f"home {batch.home!r} has {room} free slots, recall needs {len(wanted)}"
            )
        remaining = [bag_id for bag_id in batch.accepted_ids if bag_id not in wanted]
        status = RECALLED if not remaining else PARTIAL_RECALL
        at = self._clock.now_iso()
        updated = batch.evolved(status=status, updated_at=at)
        with self._store.session() as session:
            session.state.overflow_batches[batch_id] = updated
            for bag_id in wanted:
                current = session.state.overflow_bags[bag_id]
                session.state.overflow_bags[bag_id] = current.evolved(
                    status=RETURNED, batch_id=batch_id
                )
            session.record(
                "overflow.recall",
                {"batch_id": batch_id, "bag_ids": wanted, "status": status, "at": at},
                at,
            )
            session.record(
                "counter.add", {"name": "overflow.recalled", "delta": len(wanted)}, at
            )
        return updated

    # -- read side ---------------------------------------------------------

    def is_configured(self) -> bool:
        return bool(self._state().overflow_terminal and self._state().overflow_backup)

    def home(self) -> str:
        return self._state().overflow_terminal

    def backup(self) -> str:
        return self._state().overflow_backup

    def capacity(self, terminal: str) -> int:
        return self._state().overflow_capacity.get(str(terminal).upper(), 0)

    def capacities(self) -> dict[str, int]:
        return dict(sorted(self._state().overflow_capacity.items()))

    def occupancy(self, terminal: str) -> int:
        """Bags physically occupying the terminal right now.

        A bag in flight still counts against the home (it has left the queue
        but has not been accepted anywhere). Held bags count against the
        backup. Returned bags count against home again.
        """

        terminal = str(terminal).upper()
        count = 0
        for bag in self._state().overflow_bags.values():
            if bag.status == HELD:
                count += 1 if bag.backup == terminal else 0
            elif bag.status in (AT_HOME, IN_FLIGHT, RETURNED):
                count += 1 if bag.home == terminal else 0
            # RELEASED occupies nobody.
        return count

    def free(self, terminal: str) -> int:
        terminal = str(terminal).upper()
        if terminal not in self._state().overflow_capacity:
            return 0
        return self.capacity(terminal) - self.occupancy(terminal)

    def home_queue(self) -> list[OverflowBag]:
        """Bags still queued at home (at_home), oldest admission first."""

        state = self._state()
        bags = [bag for bag in state.overflow_bags.values() if bag.status == AT_HOME]
        return sorted(bags, key=lambda bag: (state.overflow_admitted.get(bag.bag_id, ""), bag.bag_id))

    def queue_length(self) -> int:
        return len(self.home_queue())

    def held_bags(self) -> list[OverflowBag]:
        state = self._state()
        return [bag for bag in state.overflow_bags.values() if bag.status == HELD]

    def returned_bags(self) -> list[OverflowBag]:
        state = self._state()
        return [bag for bag in state.overflow_bags.values() if bag.status == RETURNED]

    def bag(self, bag_id: str) -> OverflowBag:
        return self._require_bag(bag_id)

    def batches(self) -> list[OverflowBatch]:
        return [
            self._state().overflow_batches[key]
            for key in sorted(self._state().overflow_batches)
        ]

    def batch(self, batch_id: str) -> OverflowBatch:
        return self._require_batch(batch_id)

    def open_batches(self) -> list[OverflowBatch]:
        return [batch for batch in self.batches() if batch.status in LIVE_BATCH_STATUSES]

    def outstanding(self) -> int:
        """Bags currently away from home (in flight or held at backup)."""

        return sum(
            1
            for bag in self._state().overflow_bags.values()
            if bag.status in (IN_FLIGHT, HELD)
        )

    def terminal_rows(self) -> list[dict[str, object]]:
        return [
            {
                "terminal": terminal,
                "capacity": self.capacity(terminal),
                "occupied": self.occupancy(terminal),
                "free": self.free(terminal),
            }
            for terminal in sorted(self._state().overflow_capacity)
        ]

    # -- internals ---------------------------------------------------------

    def _state(self) -> State:
        return self._store.state()

    def _terminals(self) -> tuple[str, str]:
        state = self._state()
        return state.overflow_terminal, state.overflow_backup

    def _require_terminals(self) -> tuple[str, str]:
        home, backup = self._terminals()
        if not home or not backup:
            raise ConflictError("overflow ledger is not configured")
        return home, backup

    def _require_bag(self, bag_id: str) -> OverflowBag:
        bag = self._state().overflow_bags.get(bag_id)
        if bag is None:
            raise NotFoundError(f"overflow bag {bag_id!r} is not on the ledger")
        return bag

    def _require_batch(self, batch_id: str) -> OverflowBatch:
        batch = self._state().overflow_batches.get(batch_id)
        if batch is None:
            raise NotFoundError(f"overflow batch {batch_id!r} does not exist")
        return batch

    def _require_live(self, batch: OverflowBatch) -> None:
        if batch.status in CLOSED_BATCH_STATUSES:
            raise ConflictError(f"batch {batch.id!r} is already {batch.status}")

    def _bags_of(self, batch: OverflowBatch) -> list[OverflowBag]:
        state = self._state()
        return [
            state.overflow_bags[bag_id]
            for bag_id in batch.bag_ids
            if bag_id in state.overflow_bags
        ]

    def _next_batch_id(self) -> str:
        # Derived from committed state rather than a wall clock or random
        # source, so replaying the same command stream mints the same id.
        return f"OVF-{len(self._state().overflow_batches) + 1:06d}"
