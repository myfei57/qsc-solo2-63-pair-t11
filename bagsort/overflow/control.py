"""Automatic overflow control: decide, ship out, roll back, bring home.

Every movement is one committed transaction wrapped by two evidence snapshots:

* :meth:`evaluate` decides whether the queue length or the arrival forecast
  warrants a transfer and how many bags should go. It never mutates anything,
  which is what lets an operator preview a decision (and lets the policy be
  unit tested without moving luggage).
* :meth:`run_once` evaluates and executes in one tick; :meth:`send_out` executes
  a decision or an explicit request.
* :meth:`rollback_batch` undoes an interrupted outbound transfer. The bags come
  straight home, the backup allowance is reversed and the batch is marked
  rolled back, so neither terminal ends the movement holding phantom luggage.
* :meth:`return_bags` is the two-phase recall: :meth:`request_return` first
  checks the books and the home headroom, then the return settles against that
  plan.

The account, the external records and bag ownership are all written in the same
journal transaction, so a crash mid-movement replays to either the before or the
after picture, never to a mixture.
"""

from __future__ import annotations

from typing import Any, Callable

from bagsort.clock import Clock
from bagsort.errors import ConflictError, OrderingError, ValidationError
from bagsort.ids import IdFactory
from bagsort.model.entities import (
    ArrivalSample,
    EvidenceAnchor,
    ExternalRecord,
    OverflowBatch,
)
from bagsort.model.enums import BagState
from bagsort.store.store import Store
from .account import OverflowAccount
from .evidence import EvidenceStore
from .forecast import predict
from .history import OverflowHistory, replay_check
from .policy import MODE_FORECAST, MODE_QUEUE, Decision, Policy, evaluate

DEFAULT_BACKUP = "T2"

QueueLength = Callable[[], int]


class OverflowControl:
    """State machine that owns outbound overflow for one home terminal."""

    def __init__(
        self,
        store: Store,
        clock: Clock,
        home_terminal: str,
        evidence: EvidenceStore | None = None,
        ids: IdFactory | None = None,
        queue_length: QueueLength | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._home = home_terminal
        self._evidence = evidence or EvidenceStore(store.path)
        self._ids = ids or IdFactory("of")
        self._queue_length = queue_length or self._home_queue_length
        self.account = OverflowAccount(store, clock)
        self.history = OverflowHistory(store)

    # -- configuration -----------------------------------------------------

    def configure(
        self,
        backup_terminal: str = DEFAULT_BACKUP,
        mode: str = MODE_QUEUE,
        high_water: int = 20,
        recover_water: int = 8,
        forecast_horizon: int = 3,
        forecast_fire_rate: float = 6.0,
        transfer_cap: int = 10,
        cooldown_ticks: int = 2,
        grant: int = -1,
    ) -> dict[str, Any]:
        policy = Policy(
            mode=mode,
            high_water=high_water,
            recover_water=recover_water,
            forecast_horizon=forecast_horizon,
            forecast_fire_rate=forecast_fire_rate,
            transfer_cap=transfer_cap,
            cooldown_ticks=cooldown_ticks,
        ).validated()
        backup = backup_terminal.strip().upper()
        if not backup or backup == self._home:
            raise ValidationError("backup terminal must differ from the home terminal")
        settings = {"backup_terminal": backup, "last_movement_tick": self.tick, **policy.as_dict()}
        with self._store.session() as session:
            session.state.overflow_policy = settings
            session.record(
                "overflow.policy.set",
                {"policy": settings},
                self._clock.now_iso(),
            )
        if grant >= 0:
            self.account.grant(backup, grant)
        return self.settings()

    @property
    def tick(self) -> int:
        return int(self._store.state().overflow_policy.get("last_movement_tick", -10**6))

    def policy(self) -> Policy:
        return Policy.from_dict(self._store.state().overflow_policy)

    def settings(self) -> dict[str, Any]:
        raw = dict(self._store.state().overflow_policy)
        raw["home_terminal"] = self._home
        raw.setdefault("backup_terminal", DEFAULT_BACKUP)
        return raw

    # -- arrivals and the queue -------------------------------------------

    def record_arrival(self, count: int = 1) -> ArrivalSample:
        if count < 0:
            raise ValidationError("arrival count must not be negative")
        sample = ArrivalSample(at=self._clock.now_iso(), count=count)
        with self._store.session() as session:
            session.state.arrival_samples.append(sample)
            session.record(
                "overflow.arrival",
                {"at": sample.at, "count": sample.count},
                sample.at,
            )
        return sample

    def arrivals(self) -> list[ArrivalSample]:
        return list(self._store.state().arrival_samples)

    def home_queue(self) -> list:
        state = self._store.state()
        bags = [
            bag
            for bag in state.bags.values()
            if bag.terminal == self._home and bag.state is BagState.INJECTED
        ]
        return sorted(bags, key=lambda bag: (bag.epoch, bag.sequence, bag.id))

    def _home_queue_length(self) -> int:
        return len(self.home_queue())

    def outbound_count(self) -> int:
        return len(self.history.outbound_bag_ids())

    # -- decision ----------------------------------------------------------

    def evaluate(self) -> Decision:
        state = self._store.state()
        settings = state.overflow_policy
        if not settings:
            raise ConflictError("overflow policy has not been configured")
        policy = Policy.from_dict(settings)
        backup = str(settings.get("backup_terminal", DEFAULT_BACKUP))

        forecast = None
        if policy.mode == MODE_FORECAST:
            forecast = predict(
                state.arrival_samples,
                horizon=policy.forecast_horizon,
            )

        current_tick = len(state.arrival_samples)
        cooled = current_tick - self.tick >= policy.cooldown_ticks
        decision = evaluate(
            policy,
            queue_length=self._queue_length(),
            overflow_out=self.outbound_count(),
            available=self.account.available(backup),
            forecast=forecast,
            cooled_down=cooled,
        )
        return decision

    def run_once(self) -> dict[str, Any]:
        """Evaluate and, when the policy says transfer, ship in one tick.

        A ``recover`` verdict only produces a plan: the headroom may accept just
        part of the batch, so actually pulling bags home stays an explicit step
        (:meth:`recover_when_ready`). This keeps an evaluation tick from
        surprising the hall with a partial recovery nobody approved.
        """

        decision = self.evaluate()
        result: dict[str, Any] = {"decision": decision.as_dict()}
        if decision.action == "transfer" and decision.suggested > 0:
            batch = self.send_out(decision.suggested, reason=decision.reason)
            result["batch"] = batch.as_dict()
        elif decision.action == "recover":
            try:
                result["return_plan"] = self.request_return()
            except (ConflictError, OrderingError) as exc:
                result["return_plan"] = {"blocked": str(exc)}
        return result

    # -- shipping out ------------------------------------------------------

    def send_out(self, count: int, reason: str = "manual", bag_ids: list[str] | None = None):
        if count <= 0:
            raise ValidationError("transfer count must be positive")
        settings = self._store.state().overflow_policy
        if not settings:
            raise ConflictError("overflow policy has not been configured")
        backup = str(settings.get("backup_terminal", DEFAULT_BACKUP))
        self.account.require_capacity(backup, count)

        candidates = self.home_queue()
        if bag_ids is not None:
            chosen_ids = list(bag_ids)
            chosen = [bag for bag in candidates if bag.id in chosen_ids]
            if len(chosen) != len(set(chosen_ids)):
                raise ConflictError("requested bags are not all waiting at the home terminal")
            chosen.sort(key=lambda bag: (bag.epoch, bag.sequence, bag.id))
        else:
            chosen = candidates[:count]
        if len(chosen) < count:
            raise ConflictError(
                f"only {len(chosen)} bags are eligible at the home terminal, {count} requested"
            )
        self.account.require_capacity(backup, len(chosen))

        at = self._clock.now_iso()
        pre_ref, pre_digest = self._evidence.write(
            "transfer-pre", self._store.watermark, at, self._store.state(),
            detail={"reason": reason, "count": len(chosen), "backup_terminal": backup},
        )
        batch = OverflowBatch(
            id=self._ids.next(),
            backup_terminal=backup,
            home_terminal=self._home,
            bag_ids=tuple(bag.id for bag in chosen),
            status="outbound",
            reason=reason,
            trigger=str(settings.get("mode", "")),
            created_at=at,
            pre_evidence=pre_ref,
        )
        with self._store.session() as session:
            session.state.evidence_anchors.append(
                self._anchor_record("transfer-pre", pre_ref, pre_digest, at)
            )
            session.record(
                "overflow.evidence",
                {"anchor": session.state.evidence_anchors[-1].as_dict()},
                at,
            )
            for bag in chosen:
                updated = bag.evolved(state=BagState.OUTBOUND, terminal=backup)
                session.state.bags[bag.id] = updated
                session.record("bag.put", {"bag": updated.as_dict()}, at)
                record = ExternalRecord(
                    bag_id=bag.id,
                    batch_id=batch.id,
                    backup_terminal=backup,
                    home_terminal=self._home,
                    state="outbound",
                    out_at=at,
                    out_evidence=pre_ref,
                )
                session.state.external_records.append(record)
                session.record("overflow.record.put", {"record": record.as_dict()}, at)
            session.state.overflow_batches.append(batch)
            session.record("overflow.batch.put", {"batch": batch.as_dict()}, at)
            session.record(
                "overflow.granted",
                {"backup_terminal": backup, "delta": -len(chosen)},
                at,
            )
            session.state.overflow_policy = {
                **session.state.overflow_policy,
                "last_movement_tick": len(session.state.arrival_samples),
            }
            session.record(
                "overflow.policy.set",
                {"policy": session.state.overflow_policy},
                at,
            )
            session.record(
                "counter.add", {"name": "overflow.sent", "delta": len(chosen)}, at
            )
        post_ref, post_digest = self._evidence.write(
            "transfer-post", self._store.watermark, self._clock.now_iso(),
            self._store.state(),
            detail={"batch_id": batch.id, "count": len(chosen)},
        )
        self._anchor("transfer-post", post_ref, post_digest)
        settled = self.history.batch(batch.id).evolved(post_evidence=post_ref)
        self._update_batch(settled)
        return self.history.batch(batch.id)

    # -- rollback after an interrupted outbound ----------------------------

    def rollback_batch(self, batch_id: str, reason: str = "outbound_interrupted"):
        batch = self.history.batch(batch_id)
        if batch is None:
            raise ConflictError(f"unknown overflow batch {batch_id!r}")
        if batch.status != "outbound":
            raise OrderingError(
                f"batch {batch_id!r} is {batch.status}; only outbound batches roll back"
            )
        state = self._store.state()
        at = self._clock.now_iso()
        pre_ref, pre_digest = self._evidence.write(
            "rollback-pre", self._store.watermark, at, state,
            detail={"batch_id": batch_id, "reason": reason},
        )
        with self._store.session() as session:
            session.state.evidence_anchors.append(
                self._anchor_record("rollback-pre", pre_ref, pre_digest, at)
            )
            session.record(
                "overflow.evidence",
                {"anchor": session.state.evidence_anchors[-1].as_dict()},
                at,
            )
            for bag_id in batch.bag_ids:
                bag = session.state.bags.get(bag_id)
                if bag is not None:
                    updated = bag.evolved(
                        state=BagState.INJECTED, terminal=batch.home_terminal
                    )
                    session.state.bags[bag_id] = updated
                    session.record("bag.put", {"bag": updated.as_dict()}, at)
                old = next(
                    (
                        r
                        for r in session.state.external_records
                        if r.bag_id == bag_id and r.batch_id == batch_id
                    ),
                    None,
                )
                if old is not None:
                    record = old.evolved(state="rolled_back", back_at=at)
                    session.state.external_records = [
                        record
                        if item.bag_id == bag_id and item.batch_id == batch_id
                        else item
                        for item in session.state.external_records
                    ]
                    session.record("overflow.record.put", {"record": record.as_dict()}, at)
            updated_batch = batch.evolved(
                status="rolled_back", finished_at=at
            )
            session.state.overflow_batches = [
                updated_batch if item.id == batch_id else item
                for item in session.state.overflow_batches
            ]
            session.record("overflow.batch.put", {"batch": updated_batch.as_dict()}, at)
            # The bags never entered backup custody: hand the allowance back.
            session.record(
                "overflow.granted",
                {
                    "backup_terminal": batch.backup_terminal,
                    "delta": len(batch.bag_ids),
                },
                at,
            )
            session.record(
                "counter.add",
                {"name": "overflow.rolled_back", "delta": len(batch.bag_ids)},
                at,
            )
        post_ref, post_digest = self._evidence.write(
            "rollback-post", self._store.watermark, self._clock.now_iso(),
            self._store.state(), detail={"batch_id": batch_id},
        )
        self._anchor("rollback-post", post_ref, post_digest)
        self._update_batch(self.history.batch(batch_id).evolved(post_evidence=post_ref))
        return self.history.batch(batch_id)

    # -- recovery ----------------------------------------------------------

    def request_return(self, batch_id: str = "", limit: int = 0) -> dict[str, Any]:
        """Phase one: verify the books and the headroom without moving anything.

        Recovery is only offered once the home queue has subsided to
        ``recover_water``; the planned count is capped so accepting the bags
        cannot push the queue back above it. That gap between ``high_water`` and
        ``recover_water`` is the hysteresis that keeps luggage from oscillating
        between the two terminals.
        """

        problems = self.account.reconcile()
        if problems:
            raise ConflictError("overflow books do not reconcile: " + "; ".join(problems))
        candidates = self._returnable(batch_id)
        if not candidates:
            raise ConflictError("no outbound bags eligible for return")
        policy = self.policy()
        current = self._queue_length()
        room = policy.recover_water - current
        if room <= 0:
            raise OrderingError(
                "home queue has not subsided to recover_water; hold recovery"
            )
        count = min(len(candidates), room) if not limit else min(len(candidates), room, limit)
        plan = candidates[:count]
        return {
            "batch_id": batch_id,
            "queue_length": current,
            "recover_water": policy.recover_water,
            "high_water": policy.high_water,
            "room": room,
            "planned": len(plan),
            "bag_ids": [bag.id for bag in plan],
            "remaining_outbound": self.outbound_count() - len(plan),
        }

    def return_bags(self, bag_ids: list[str] | None = None, batch_id: str = ""):
        """Phase two: bring the planned bags home and settle both ledgers."""

        problems = self.account.reconcile()
        if problems:
            raise ConflictError("overflow books do not reconcile: " + "; ".join(problems))
        state = self._store.state()
        outbound = {
            record.bag_id: record
            for record in state.external_records
            if record.state == "outbound"
        }
        if bag_ids is None:
            candidates = self._returnable(batch_id)
            chosen = [bag for bag in candidates if bag.id in outbound]
        else:
            missing = [bag_id for bag_id in bag_ids if bag_id not in outbound]
            if missing:
                raise ConflictError(f"bags not outbound at a backup: {missing}")
            chosen = [state.bags[bag_id] for bag_id in bag_ids]
        if not chosen:
            raise ConflictError("nothing to return")
        touched_batches = {outbound[bag.id].batch_id for bag in chosen}
        at = self._clock.now_iso()
        pre_ref, pre_digest = self._evidence.write(
            "return-pre", self._store.watermark, at, state,
            detail={"bag_ids": [bag.id for bag in chosen]},
        )
        with self._store.session() as session:
            session.state.evidence_anchors.append(
                self._anchor_record("return-pre", pre_ref, pre_digest, at)
            )
            session.record(
                "overflow.evidence",
                {"anchor": session.state.evidence_anchors[-1].as_dict()},
                at,
            )
            for bag in chosen:
                record = outbound[bag.id]
                updated = bag.evolved(
                    state=BagState.INJECTED, terminal=batch_home(session, record)
                )
                session.state.bags[bag.id] = updated
                session.record("bag.put", {"bag": updated.as_dict()}, at)
                settled_record = record.evolved(state="returned", back_at=at)
                session.state.external_records = [
                    settled_record
                    if item.bag_id == bag.id and item.batch_id == record.batch_id
                    else item
                    for item in session.state.external_records
                ]
                session.record(
                    "overflow.record.put", {"record": settled_record.as_dict()}, at
                )
            for touched in touched_batches:
                current = next(b for b in session.state.overflow_batches if b.id == touched)
                still_out = [
                    record
                    for record in session.state.external_records
                    if record.batch_id == touched and record.state == "outbound"
                ]
                if not still_out:
                    current = current.evolved(status="returned", finished_at=at)
                session.state.overflow_batches = [
                    current if item.id == touched else item
                    for item in session.state.overflow_batches
                ]
                session.record(
                    "overflow.batch.put", {"batch": current.as_dict()}, at
                )
            # Settle the movement balance per backup terminal (+n back home).
            per_terminal: dict[str, int] = {}
            for bag in chosen:
                per_terminal[outbound[bag.id].backup_terminal] = (
                    per_terminal.get(outbound[bag.id].backup_terminal, 0) + 1
                )
            for terminal, amount in per_terminal.items():
                session.record(
                    "overflow.granted",
                    {"backup_terminal": terminal, "delta": amount},
                    at,
                )
            session.record(
                "counter.add", {"name": "overflow.returned", "delta": len(chosen)}, at
            )
        post_ref, post_digest = self._evidence.write(
            "return-post", self._store.watermark, self._clock.now_iso(),
            self._store.state(), detail={"bag_ids": [bag.id for bag in chosen]},
        )
        self._anchor("return-post", post_ref, post_digest)
        for touched in touched_batches:
            self._update_batch(self.history.batch(touched).evolved(post_evidence=post_ref))
        return {
            "returned": [bag.id for bag in chosen],
            "batches": sorted(touched_batches),
            "post_evidence": post_ref,
        }

    def recover_when_ready(self, batch_id: str = "", limit: int = 0) -> dict[str, Any]:
        """Plan and execute a return when the queue has genuinely subsided."""

        plan = self.request_return(batch_id=batch_id, limit=limit)
        result = self.return_bags(plan["bag_ids"])
        result["plan"] = plan
        return result

    # -- verification ------------------------------------------------------

    def reconcile(self) -> dict[str, Any]:
        problems = self.account.reconcile()
        for anchor in self._store.state().evidence_anchors:
            if not self._evidence.verify_ref(anchor.ref, anchor.digest):
                problems.append(
                    f"evidence {anchor.ref!r} is missing or its digest changed"
                )
        replay_ok = replay_check(self._store)
        if not replay_ok:
            problems.append("journal replay does not match the materialised state")
        return {"ok": not problems, "problems": problems, "replay_ok": replay_ok}

    def verify_evidence(self) -> int:
        return self._evidence.verify_all(self._store.state().evidence_anchors)

    # -- internals ---------------------------------------------------------

    def _returnable(self, batch_id: str = "") -> list:
        outbound = self.history.outbound_bag_ids()
        if batch_id:
            batch = self.history.batch(batch_id)
            if batch is None:
                raise ConflictError(f"unknown overflow batch {batch_id!r}")
            outbound = outbound & set(batch.bag_ids)
        state = self._store.state()
        bags = [state.bags[bag_id] for bag_id in outbound if bag_id in state.bags]
        return sorted(bags, key=lambda bag: (bag.epoch, bag.sequence, bag.id))

    def _anchor(self, phase: str, ref: str, digest: str) -> None:
        anchor = EvidenceAnchor(
            phase=phase,
            ref=ref,
            digest=digest,
            watermark=self._store.watermark,
            at=self._clock.now_iso(),
        )
        with self._store.session() as session:
            session.state.evidence_anchors.append(anchor)
            session.record("overflow.evidence", {"anchor": anchor.as_dict()}, anchor.at)

    def _anchor_record(self, phase: str, ref: str, digest: str, at: str) -> EvidenceAnchor:
        """Build an anchor stamped with the pre-movement watermark."""

        return EvidenceAnchor(
            phase=phase,
            ref=ref,
            digest=digest,
            watermark=self._store.watermark,
            at=at,
        )

    def _update_batch(self, batch: OverflowBatch) -> None:
        with self._store.session() as session:
            session.state.overflow_batches = [
                batch if item.id == batch.id else item
                for item in session.state.overflow_batches
            ]
            session.record("overflow.batch.put", {"batch": batch.as_dict()}, self._clock.now_iso())


def batch_home(session, record: ExternalRecord) -> str:
    batch = next(
        (b for b in session.state.overflow_batches if b.id == record.batch_id),
        None,
    )
    return batch.home_terminal if batch is not None else record.home_terminal
