"""Controller: turns policy judgements into ledger moves with audit evidence.

One :meth:`OverflowController.tick` is one evaluation instant. The controller:

1. snapshots the ledger to disk *before* acting;
2. asks the policy what to do;
3. commits the ledger move in one journal session;
4. snapshots the ledger again *after* acting, linked to the before-file;
5. appends the policy decision to the journal so the history is replayable.

Outbound failures are explicit: :meth:`transfer` accepts a failure map so the
"外发中断、回滚已转出归属" path runs against the same ledger a real link
failure would leave behind, and the rollback snapshots both sides too.
"""

from __future__ import annotations

from dataclasses import dataclass

from bagsort.clock import Clock
from bagsort.model.entities import OverflowDecision
from bagsort.store.store import Store

from .archive import SnapshotArchive
from .forecast import ArrivalForecast
from .ledger import OverflowLedger
from .policy import (
    DIVERT,
    RECALL,
    Policy,
    PolicyContext,
    PolicyDecision,
    _projected,
)
from .reconcile import reconcile


@dataclass
class TickResult:
    decision: PolicyDecision
    batch_id: str
    snapshot_before: str
    snapshot_after: str
    report: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision.as_dict(),
            "batch_id": self.batch_id,
            "snapshot_before": self.snapshot_before,
            "snapshot_after": self.snapshot_after,
            "reconciliation": self.report,
        }


class OverflowController:
    """Drives one overflow ledger from a policy and a forecast."""

    def __init__(
        self,
        store: Store,
        clock: Clock,
        policy: Policy,
        archive: SnapshotArchive,
        forecast: ArrivalForecast | None = None,
        service_rate: int = 10,
        bus: object | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._policy = policy
        self._archive = archive
        self._forecast = forecast
        self._service_rate = service_rate
        self._bus = bus
        self.ledger = OverflowLedger(store, clock)

    @property
    def policy(self) -> Policy:
        return self._policy

    @property
    def clock(self) -> Clock:
        return self._clock

    def configure(
        self, home: str, backup: str, home_capacity: int, backup_capacity: int
    ) -> dict[str, object]:
        self.ledger.configure(home, backup, home_capacity, backup_capacity)
        return self.status()

    def status(self) -> dict[str, object]:
        state = self._store.state()
        report = reconcile(state)
        return {
            "home": state.overflow_terminal,
            "backup": state.overflow_backup,
            "policy": getattr(self._policy, "name", "unknown"),
            "queue": self.ledger.queue_length(),
            "held": len(self.ledger.held_bags()),
            "outstanding": self.ledger.outstanding(),
            "open_batches": len(self.ledger.open_batches()),
            "capacities": self.ledger.capacities(),
            "terminals": self.ledger.terminal_rows(),
            "reconciliation": report.as_dict(),
        }

    def reconcile(self) -> dict[str, object]:
        return reconcile(self._store.state()).as_dict()

    def set_advisory(self, arrivals: float) -> None:
        """Feed an upstream forecast advisory for the next tick."""

        if self._forecast is not None:
            self._forecast.set_advisory(arrivals)

    # -- admission / release used by the plant -----------------------------

    def admit(self, bag_id: str) -> str:
        self.ledger.admit(bag_id)
        return bag_id

    def can_admit(self, bag_id: str) -> bool:
        return self.ledger.can_admit(bag_id)

    def depart_home(self, bag_id: str) -> None:
        if hasattr(bag_id, "bag_id"):
            bag_id = bag_id.bag_id
        self.ledger.depart_home(bag_id)

    # -- explicit transfer / recall / rollback -----------------------------

    def transfer(
        self,
        amount: int,
        reason: str = "manual",
        accepted_ids: list[str] | tuple[str, ...] | None = None,
        fail_remaining: bool = False,
    ) -> dict[str, object]:
        """Prepare, then accept; optionally simulate an outbound interruption.

        ``fail_remaining`` means anything not listed in ``accepted_ids`` never
        arrives AND the link is considered broken, so the accepted subset is
        rolled back as well -- the interrupted transfer leaves no bag split
        across terminals.
        """

        before = self._snapshot("transfer", "before")
        batch = self.ledger.prepare(amount, reason=reason)
        if accepted_ids is None:
            batch = self.ledger.accept(batch.id)
            accepted = list(batch.accepted_ids)
            failed: list[str] = []
        else:
            accepted = [bag_id for bag_id in accepted_ids if bag_id in batch.bag_ids]
            failed = [bag_id for bag_id in batch.bag_ids if bag_id not in accepted]
            if accepted:
                batch = self.ledger.accept(batch.id, accepted)
        rolled_back = False
        if fail_remaining and failed:
            batch = self.ledger.rollback(batch.id, reason="outbound_interrupt")
            rolled_back = True
        elif failed:
            # Partial drop with no full interrupt: settle the unaccepted tail
            # so no bag is stranded in flight.
            batch = self.ledger.rollback(batch.id, reason="partial_delivery")
            rolled_back = True
        self._record_decision(
            action=DIVERT,
            amount=len(batch.bag_ids),
            basis="manual",
            projected=self.ledger.queue_length(),
            batch_id=batch.id,
            reason=reason + (" rolled_back" if rolled_back else ""),
            predictive_only=False,
        )
        self._publish(
            "overflow.rolled_back" if rolled_back else "overflow.transferred",
            batch_id=batch.id,
            bags=list(batch.bag_ids),
            reason=reason,
        )
        after = self._snapshot("transfer", "after")
        return {
            "batch_id": batch.id,
            "status": batch.status,
            "offered": list(batch.bag_ids),
            "accepted": accepted,
            "failed": failed,
            "rolled_back": rolled_back,
            "snapshot_before": before,
            "snapshot_after": after,
            "reconciliation": self.reconcile(),
        }

    def interrupt(self, batch_id: str) -> dict[str, object]:
        """The outbound link dies after a (possibly partial) delivery: roll it back."""

        before = self._snapshot("rollback", "before")
        batch = self.ledger.rollback(batch_id, reason="outbound_interrupt")
        self._record_decision(
            action="rollback",
            amount=len(batch.bag_ids),
            basis="incident",
            projected=self.ledger.queue_length(),
            batch_id=batch_id,
            reason="outbound_interrupt",
            predictive_only=False,
        )
        self._publish(
            "overflow.rolled_back", batch_id=batch_id, bags=list(batch.bag_ids)
        )
        after = self._snapshot("rollback", "after")
        return {
            "batch_id": batch_id,
            "status": batch.status,
            "bag_ids": list(batch.bag_ids),
            "snapshot_before": before,
            "snapshot_after": after,
            "reconciliation": self.reconcile(),
        }

    def recall(self, batch_id: str | None = None) -> dict[str, object]:
        """Recall held bags after the peak, refusing an unreconciled ledger.

        Without a batch id it keeps bringing whole batches home (newest first)
        until the home terminal cannot fit the next one, so a later tick with
        more room can finish the closeout.
        """

        report = self.reconcile()
        if not report["ok"]:
            raise ValueError(f"refusing recall, ledger does not reconcile: {report['errors']}")
        before = self._snapshot("recall", "before")
        recalled: list[dict[str, object]] = []
        if batch_id is not None:
            recalled = self._recall_batches(batch_id, reason="post_peak_recall")
        else:
            while True:
                step = self._recall_batches(None, reason="post_peak_recall")
                if not step:
                    break
                recalled.extend(step)
        after = self._snapshot("recall", "after")
        return {
            "recalled": recalled,
            "snapshot_before": before,
            "snapshot_after": after,
            "reconciliation": self.reconcile(),
        }

    def _recall_batches(
        self, batch_id: str | None, reason: str, limit: int | None = None
    ) -> list[dict[str, object]]:
        """The shared recall execution; records one decision per batch.

        ``limit`` caps how many held bags come home in total (the capacity-fit
        amount the policy computed); ``None`` recalls every held bag.
        """

        if batch_id is not None:
            held_bags = [
                bag for bag in self.ledger.held_bags() if bag.batch_id == batch_id
            ]
        else:
            held_bags = self.ledger.held_bags()
        # Recall newest batches first (LIFO), always as whole batches. A batch
        # that does not fit waits for a later tick; splitting would leave an
        # accepted batch half-open for a long time, which is what partial
        # recall is reserved for (explicit per-bag API calls).
        held_bags = sorted(held_bags, key=lambda bag: bag.batch_id, reverse=True)
        grouped: dict[str, list[str]] = {}
        for bag in held_bags:
            grouped.setdefault(bag.batch_id, []).append(bag.bag_id)
        budget = self.ledger.capacity(self.ledger.home()) - self.ledger.occupancy(
            self.ledger.home()
        )
        if limit is not None:
            budget = min(budget, limit)
        recalled: list[dict[str, object]] = []
        for current_batch_id, bag_ids in grouped.items():
            batch = self.ledger.batch(current_batch_id)
            if batch.status not in ("accepted", "partial", "partial_recall"):
                continue
            if len(bag_ids) > budget:
                continue
            updated = self.ledger.recall(current_batch_id, bag_ids)
            recalled.append(
                {"batch_id": current_batch_id, "bag_ids": bag_ids, "status": updated.status}
            )
            budget -= len(bag_ids)
            self._record_decision(
                action=RECALL,
                amount=len(bag_ids),
                basis="queue",
                projected=self.ledger.queue_length(),
                batch_id=current_batch_id,
                reason=reason,
                predictive_only=False,
            )
            self._publish(
                "overflow.recalled", batch_id=current_batch_id, bags=list(bag_ids)
            )
        return recalled

    # -- policy driven tick ------------------------------------------------

    def tick(self, arrivals: float = 0.0) -> TickResult:
        """Evaluate one instant and act. ``arrivals`` feeds the forecast first."""

        if self._forecast is not None:
            self._forecast.observe(arrivals)
        ledger = self.ledger
        state = self._store.state()
        context = PolicyContext(
            minute=self._clock.now_iso()[:16],
            queue=ledger.queue_length(),
            capacity=ledger.capacity(state.overflow_terminal),
            held=len(ledger.held_bags()),
            free_home=ledger.free(state.overflow_terminal),
            free_backup=ledger.free(state.overflow_backup),
            service_rate=self._service_rate,
            forecast=self._forecast.current() if self._forecast is not None else None,
            home_occupancy=ledger.occupancy(state.overflow_terminal),
            outstanding=ledger.outstanding(),
        )
        context.projected = _projected(context)
        decision = self._policy.evaluate(context)
        before_name = self._snapshot(decision.action, "before")
        batch_id = ""
        recalled: list[dict[str, object]] = []
        if decision.action == DIVERT and decision.amount > 0:
            batch = ledger.prepare(
                int(decision.amount), reason=decision.reason or self._policy.name
            )
            ledger.accept(batch.id)
            batch_id = batch.id
            self._publish(
                "overflow.transferred",
                batch_id=batch.id,
                bags=list(batch.bag_ids),
                reason=decision.reason,
                predictive_only=decision.predictive_only,
            )
        elif decision.action == RECALL and decision.amount > 0:
            recalled = self._recall_batches(None, reason=decision.reason, limit=int(decision.amount))
            if recalled:
                batch_id = str(recalled[0]["batch_id"])
        if not recalled or decision.action != RECALL:
            self._record_decision(
                action=decision.action,
                amount=decision.amount,
                basis=decision.basis,
                projected=decision.projected or context.projected,
                batch_id=batch_id,
                reason=decision.reason,
                predictive_only=decision.predictive_only,
                armed=decision.armed,
            )
        after_name = self._snapshot(decision.action, "after")
        return TickResult(
            decision=decision,
            batch_id=batch_id,
            snapshot_before=before_name,
            snapshot_after=after_name,
            report=self.reconcile(),
        )

    def history(self, needed_window: int = 5):
        from .history import recompute

        return recompute(self._store.entries(), needed_window=needed_window)

    # -- internals ---------------------------------------------------------

    def _publish(self, topic: str, **payload: object) -> None:
        if self._bus is not None:
            self._bus.publish(topic, **payload)

    def _snapshot(self, operation: str, phase: str) -> str:
        picture = self._store.state().as_dict()["overflow"]
        snapshot = self._archive.write(
            operation=operation,
            phase=phase,
            picture=picture,
            watermark=self._store.watermark,
            at=self._clock.now_iso(),
            prev_digest=self._archive.last_digest(),
        )
        return snapshot.name

    def _record_decision(
        self,
        action: str,
        amount: int,
        basis: str,
        projected: int,
        batch_id: str,
        reason: str,
        predictive_only: bool,
        armed: bool = False,
    ) -> None:
        state = self._store.state()
        decision = OverflowDecision(
            seq=len(state.overflow_decisions) + 1,
            at=self._clock.now_iso(),
            minute=self._clock.now_iso()[:16],
            policy=getattr(self._policy, "name", "manual"),
            action=action,
            amount=amount,
            basis=basis,
            queue=self.ledger.queue_length(),
            projected=int(projected),
            held=len(self.ledger.held_bags()),
            armed=armed,
            false_alarm=False,
            batch_id=batch_id,
            reason=reason,
        )
        at = self._clock.now_iso()
        with self._store.session() as session:
            session.state.overflow_decisions.append(decision)
            session.record("overflow.decision", {"decision": decision.as_dict()}, at)
