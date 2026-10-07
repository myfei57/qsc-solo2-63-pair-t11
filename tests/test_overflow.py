"""Overflow diversion ledger, policy and reconciliation."""

from __future__ import annotations

import json
import os

import pytest

from bagsort.clock import Clock
from bagsort.errors import ConflictError, QuotaError, ValidationError
from bagsort.model.enums import HELD, RELEASED, RETURNED
from bagsort.overflow import (
    ArrivalForecast,
    HybridPolicy,
    OverflowController,
    PredictivePolicy,
    ReactivePolicy,
    SnapshotArchive,
    build_policy,
    reconcile,
    recompute,
)
from bagsort.overflow.history import filter_window, summary
from bagsort.overflow.policy import DIVERT, HOLD, RECALL, PolicyContext
from bagsort.store.store import Store


def _controller(
    tmp_path,
    policy="reactive",
    home_capacity: int = 10,
    backup_capacity: int = 20,
    service_rate: int = 10,
    home: str = "T1",
    backup: str = "T2",
) -> OverflowController:
    store = Store(str(tmp_path / "state.json"))
    controller = OverflowController(
        store,
        Clock(),
        build_policy(policy),
        SnapshotArchive(str(tmp_path / "snapshots")),
        forecast=ArrivalForecast(),
        service_rate=service_rate,
    )
    controller.configure(home, backup, home_capacity, backup_capacity)
    return controller


def _admit(controller: OverflowController, count: int, prefix: str = "B") -> list[str]:
    ids = [f"{prefix}{index:02d}" for index in range(count)]
    for bag_id in ids:
        controller.admit(bag_id)
    return ids


# ---------------------------------------------------------------------------
# quota account, ownership and outbound records agree
# ---------------------------------------------------------------------------


def test_quota_account_tracks_physical_location(tmp_path):
    controller = _controller(tmp_path)
    ids = _admit(controller, 8)
    assert controller.ledger.queue_length() == 8
    assert controller.ledger.occupancy("T1") == 8
    assert controller.ledger.free("T1") == 2
    result = controller.transfer(3)
    assert result["status"] == "accepted"
    assert controller.ledger.occupancy("T2") == 3
    assert controller.ledger.occupancy("T1") == 5
    # ownership never moves: the bags are still owned by T1
    for bag_id in ids[-3:]:
        assert controller.ledger.bag(bag_id).home == "T1"
        assert controller.ledger.bag(bag_id).status == HELD


def test_backup_capacity_is_enforced(tmp_path):
    controller = _controller(tmp_path, home_capacity=10, backup_capacity=2)
    _admit(controller, 14)
    with pytest.raises(QuotaError):
        controller.transfer(5)
    result = controller.transfer(2)
    assert result["status"] == "accepted"


def test_transfer_accepts_only_landed_bags(tmp_path):
    controller = _controller(tmp_path)
    _admit(controller, 12)
    result = controller.transfer(4, accepted_ids=["B00", "B01"])
    # the unaccepted tail is rolled back so nothing is stranded in flight
    assert result["rolled_back"] is True
    assert controller.ledger.queue_length() == 12
    assert controller.ledger.occupancy("T2") == 0


# ---------------------------------------------------------------------------
# interruption rollback keeps both terminals consistent
# ---------------------------------------------------------------------------


def test_interrupt_rolls_back_everything_and_reconciles(tmp_path):
    controller = _controller(tmp_path)
    _admit(controller, 14)
    prepared = controller.transfer(4)
    batch_id = prepared["batch_id"]
    result = controller.interrupt(batch_id)
    assert result["status"] == "rolled_back"
    assert result["reconciliation"]["ok"] is True
    assert controller.ledger.queue_length() == 14
    assert controller.ledger.occupancy("T2") == 0
    assert controller.ledger.outstanding() == 0
    batch = controller.ledger.batch(batch_id)
    assert batch.status == "rolled_back"
    assert all(controller.ledger.bag(bag_id).status == "at_home" for bag_id in batch.bag_ids)


def test_partial_delivery_then_interrupt_still_reconciles(tmp_path):
    controller = _controller(tmp_path)
    _admit(controller, 14)
    first = controller.transfer(4, accepted_ids=["B12", "B13"], fail_remaining=True)
    assert first["status"] == "rolled_back"
    second = controller.transfer(3)
    result = controller.interrupt(second["batch_id"])
    assert result["reconciliation"]["ok"] is True
    assert controller.ledger.queue_length() == 14
    assert controller.ledger.occupancy("T2") == 0


def test_rollback_is_idempotent_on_a_closed_batch(tmp_path):
    controller = _controller(tmp_path)
    _admit(controller, 12)
    result = controller.transfer(3)
    controller.interrupt(result["batch_id"])
    with pytest.raises(ConflictError):
        controller.interrupt(result["batch_id"])


# ---------------------------------------------------------------------------
# recalled bags cannot be re-booked until released; recall reconciles
# ---------------------------------------------------------------------------


def test_recalled_bag_cannot_be_readmitted_until_released(tmp_path):
    controller = _controller(tmp_path)
    _admit(controller, 13)
    transferred = controller.transfer(3)
    # free three slots at home so the recalled bags fit
    home = controller.ledger.home_queue()
    for bag in home[:3]:
        controller.depart_home(bag)
    controller.recall(transferred["batch_id"])
    returned = controller.ledger.returned_bags()
    assert len(returned) == 3
    for bag in returned:
        with pytest.raises(ConflictError):
            controller.admit(bag.bag_id)
        controller.depart_home(bag.bag_id)
    # after release the retained row does not block re-admission
    controller.admit("B12")
    assert controller.ledger.bag("B12").status == "at_home"


def test_recall_refuses_when_home_is_full(tmp_path):
    controller = _controller(tmp_path, home_capacity=10)
    _admit(controller, 13)
    transferred = controller.transfer(3)
    # home is full (10 at home): even though 3 are at the backup, there is no
    # room to bring them back
    assert controller.ledger.occupancy("T1") == 10
    with pytest.raises(QuotaError):
        controller.ledger.recall(transferred["batch_id"])


def test_post_peak_recall_closes_whole_batches_lifo(tmp_path):
    controller = _controller(tmp_path, home_capacity=10)
    _admit(controller, 12)
    first = controller.transfer(2)
    _admit(controller, 2, prefix="C")
    second = controller.transfer(2)
    # drain the home queue, then recall: newest batch closes first
    for bag_id in list(controller.ledger.home_queue()):
        controller.depart_home(bag_id)
    result = controller.recall()
    recalled_batches = [item["batch_id"] for item in result["recalled"]]
    assert recalled_batches[0] == second["batch_id"]
    assert recalled_batches[-1] == first["batch_id"]
    assert result["reconciliation"]["ok"] is True


# ---------------------------------------------------------------------------
# snapshots before and after every move, chained on disk
# ---------------------------------------------------------------------------


def test_transfer_and_recall_write_linked_snapshots(tmp_path):
    controller = _controller(tmp_path)
    _admit(controller, 12)
    controller.transfer(2)
    archive = SnapshotArchive(str(tmp_path / "snapshots"))
    files = archive.list_snapshots()
    names = [item.name.split("-", 1)[1].rsplit(".", 1)[0] for item in files]
    assert "transfer-before" in names
    assert "transfer-after" in names
    verification = archive.verify()
    assert verification["ok"] is True
    assert verification["count"] >= 2
    # every snapshot records the watermark of a real committed state
    for item in files:
        assert "watermark" in item.body
        assert item.body["state"]["terminal"] == "T1"


def test_tampering_with_a_snapshot_breaks_the_chain(tmp_path):
    controller = _controller(tmp_path)
    _admit(controller, 12)
    controller.transfer(2)
    archive_dir = tmp_path / "snapshots"
    target = sorted(archive_dir.glob("*.json"))[0]
    raw = json.loads(target.read_text())
    raw["state"]["terminal"] = "T9"
    target.write_text(json.dumps(raw))
    assert SnapshotArchive(str(archive_dir)).verify()["ok"] is False


# ---------------------------------------------------------------------------
# policy choice: reactive vs predictive vs hybrid
# ---------------------------------------------------------------------------


def _context(queue, held=0, capacity=60, forecast=None, service_rate=10, occupancy=None):
    return PolicyContext(
        minute="2024-01-01T00:30",
        queue=queue,
        capacity=capacity,
        held=held,
        free_home=capacity - (queue if occupancy is None else occupancy),
        free_backup=120 - held,
        service_rate=service_rate,
        forecast=forecast,
        home_occupancy=queue if occupancy is None else occupancy,
    )


def test_reactive_only_moves_on_observed_queue():
    policy = ReactivePolicy()
    forecast = ArrivalForecast()
    forecast.set_advisory(40)
    decision = policy.evaluate(_context(5, forecast=forecast.current()))
    assert decision.action == HOLD
    decision = policy.evaluate(_context(65, forecast=forecast.current()))
    assert decision.action == DIVERT
    assert decision.basis == "queue"


def test_predictive_moves_before_overflow_and_is_exposed_to_false_alarms():
    policy = PredictivePolicy()
    forecast = ArrivalForecast()
    forecast.set_advisory(40)
    forecast.observe(6)
    decision = policy.evaluate(_context(40, forecast=forecast.current()))
    assert decision.action == DIVERT
    assert decision.predictive_only is True


def test_hybrid_caps_prediction_and_backstops_false_alarms():
    policy = HybridPolicy(first_tranche=4, confirm_water=0.85)
    forecast = ArrivalForecast()
    forecast.set_advisory(30)
    forecast.observe(12)
    # near the confirm line and a strong forecast: only 4 leave
    decision = policy.evaluate(_context(54, forecast=forecast.current()))
    assert decision.action == DIVERT
    assert decision.amount == 4
    assert decision.predictive_only is True
    # the surge never arrives, queue healthy: the backstop recalls them
    forecast.set_advisory(0)
    forecast.observe(4)
    decision = policy.evaluate(
        _context(50, held=4, forecast=forecast.current(), occupancy=50)
    )
    assert decision.action == RECALL
    assert decision.amount == 4


def test_hybrid_observed_overflow_is_not_capped(tmp_path):
    store = Store(str(tmp_path / "state.json"))
    controller = OverflowController(
        store,
        Clock(),
        HybridPolicy(first_tranche=4, confirm_water=0.85),
        SnapshotArchive(str(tmp_path / "snap")),
        forecast=ArrivalForecast(),
        service_rate=10,
    )
    controller.configure("T1", "T2", 60, 120)
    _admit(controller, 72)
    result = controller.tick(arrivals=22)
    # real excess above capacity (12) is fully covered, not capped at 4
    assert result.decision.action == DIVERT
    assert result.decision.amount == 12


# ---------------------------------------------------------------------------
# history recomputation and false-transfer classification
# ---------------------------------------------------------------------------


def test_history_recomputes_by_time_and_flags_false_transfers(tmp_path):
    controller = _controller(tmp_path, home_capacity=10, backup_capacity=40)
    _admit(controller, 13)
    controller.transfer(3)  # genuinely needed: queue is over capacity
    controller.clock.advance(180)
    for bag_id in controller.ledger.home_queue():
        controller.depart_home(bag_id)
    controller.recall()
    history = recompute(controller._store.entries(), needed_window=6)
    stats = summary(history)
    assert stats["transfers"] == 1
    assert stats["misdiverted_bags"] == 0
    rows = filter_window(history, end="2024-01-01T00:03")
    assert all(row.minute <= "2024-01-01T00:03" for row in rows.rows)


def test_history_marks_a_transfer_no_overflow_followed_as_misdiverted(tmp_path):
    controller = _controller(tmp_path, home_capacity=10, backup_capacity=40)
    _admit(controller, 9)
    # the queue is still below capacity; one bag is moved on a forecast that
    # never materialises, so the counterfactual queue never breaches capacity
    controller.transfer(1)
    controller.clock.advance(120)
    for bag_id in controller.ledger.home_queue():
        controller.depart_home(bag_id)
    controller.clock.advance(420)  # beyond the needed window
    history = recompute(controller._store.entries(), needed_window=3)
    verdict = history.transfers[0]
    assert verdict.needed is False
    assert summary(history)["misdiverted_bags"] == 1


# ---------------------------------------------------------------------------
# replay persistence
# ---------------------------------------------------------------------------


def test_overflow_state_survives_persist_and_reopen(tmp_path):
    controller = _controller(tmp_path)
    _admit(controller, 12)
    controller.transfer(2)
    controller._store.persist()
    reopened = Store.open(str(tmp_path / "state.json"))
    report = reconcile(reopened.state())
    assert report.ok is True
    assert report.occupancy == {"T1": 10, "T2": 2}
    assert len(reopened.state().overflow_batches) == 1


def test_configuration_validation(tmp_path):
    controller = _controller(tmp_path)
    with pytest.raises(ValidationError):
        controller.configure("T1", "T1", 10, 10)
    with pytest.raises(ValidationError):
        controller.configure("T1", "T2", 0, 10)
