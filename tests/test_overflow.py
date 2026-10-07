"""Automatic overflow transfer to backup terminals."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bagsort.errors import ConflictError, OrderingError
from bagsort.overflow.forecast import ewma, predict
from bagsort.overflow.history import replay_check
from bagsort.overflow.policy import MODE_FORECAST, MODE_QUEUE, Policy, evaluate
from bagsort.overflow.forecast import ArrivalSample
from bagsort.store.store import Store
from tests.support import check_in, register_flight


def _configure(plant, **overrides):
    settings = {
        "backup_terminal": "T2",
        "grant": 10,
        "mode": MODE_QUEUE,
        "high_water": 5,
        "recover_water": 2,
        "transfer_cap": 3,
        "cooldown_ticks": 0,
    }
    settings.update(overrides)
    return plant.configure_overflow(**settings)


def _surge(plant, flight, count):
    return check_in(plant, flight.id, count, prefix="BAG")


def test_queue_trigger_fires_on_length_not_before(plant, flight):
    _configure(plant)
    _surge(plant, flight, 4)
    assert plant.overflow.evaluate().action == "hold"
    plant.check_in("BAG0004", flight.id)
    decision = plant.overflow.evaluate()
    assert decision.action == "transfer"
    assert decision.suggested == 3  # cap, not the whole surplus
    assert decision.mode == MODE_QUEUE


def test_transfer_moves_ownership_and_the_account_together(plant, flight):
    _configure(plant, grant=5)
    bags = _surge(plant, flight, 5)
    decision = plant.overflow.evaluate()
    batch = plant.send_overflow(decision.suggested)

    assert batch.status == "outbound"
    assert set(batch.bag_ids) == {bag.id for bag in bags[:3]}
    for bag_id in batch.bag_ids:
        bag = plant.bag(bag_id)
        assert bag.terminal == "T2"
        assert bag.home_terminal == "T1"
        assert bag.state.value == "outbound"
    account = plant.overflow.account.accounts()["T2"]
    assert account == {"granted": 5, "held": 3, "available": 2, "movement_balance": -3}
    assert plant.overflow_reconcile()["ok"] is True


def test_send_without_capacity_is_refused(plant, flight):
    _configure(plant, grant=0)
    _surge(plant, flight, 6)
    with pytest.raises(ConflictError):
        plant.send_overflow(1)


def test_outbound_bag_cannot_be_checked_in_again_at_home(plant, flight):
    _configure(plant)
    bags = _surge(plant, flight, 5)
    batch = plant.send_overflow(plant.overflow.evaluate().suggested)
    barcode = plant.bag(batch.bag_ids[0]).barcode
    with pytest.raises(ConflictError):
        plant.check_in(barcode, flight.id)


def test_rollback_restores_both_terminals_to_a_consistent_state(plant, flight):
    _configure(plant, grant=5)
    _surge(plant, flight, 5)
    batch = plant.send_overflow(3)

    rolled = plant.rollback_overflow(batch.id, reason="link down")
    assert rolled.status == "rolled_back"
    for bag_id in rolled.bag_ids:
        bag = plant.bag(bag_id)
        assert bag.terminal == "T1"
        assert bag.state.value == "injected"
    account = plant.overflow.account.accounts()["T2"]
    assert account["held"] == 0
    assert account["available"] == 5
    assert account["movement_balance"] == 0
    assert plant.overflow_reconcile()["ok"] is True


def test_rollback_only_applies_to_outbound_batches(plant, flight):
    _configure(plant)
    _surge(plant, flight, 5)
    batch = plant.send_overflow(3)
    plant.rollback_overflow(batch.id)
    with pytest.raises(OrderingError):
        plant.rollback_overflow(batch.id)


def test_recovery_is_blocked_until_the_home_queue_subsides(plant, flight):
    _configure(plant, high_water=10, recover_water=1)
    _surge(plant, flight, 5)
    batch = plant.send_overflow(3)
    # Two bags remain at home; accepting even one would breach recover_water.
    with pytest.raises(OrderingError):
        plant.plan_return(batch_id=batch.id)
    for bag in plant.overflow.home_queue():
        plant.divert_bag(bag.id)
    # the hall is empty; a generous recovery can bring all three home at once
    plant.overflow.configure(
        backup_terminal="T2",
        mode=MODE_QUEUE,
        high_water=10,
        recover_water=5,
        transfer_cap=3,
        cooldown_ticks=0,
    )
    plan = plant.plan_return(batch_id=batch.id)
    assert plan["planned"] == 3
    result = plant.return_overflow(plan["bag_ids"])
    assert sorted(result["returned"]) == sorted(batch.bag_ids)
    assert plant.overflow.history.batch(batch.id).status == "returned"
    account = plant.overflow.account.accounts()["T2"]
    assert account["available"] == 10
    assert account["movement_balance"] == 0
    assert plant.overflow_reconcile()["ok"] is True


def test_partial_return_keeps_the_batch_open_and_the_books_balanced(plant, flight):
    _configure(plant, high_water=6, recover_water=1)
    _surge(plant, flight, 6)
    batch = plant.send_overflow(3)
    plant.divert_bag(plant.overflow.home_queue()[0].id)
    plant.divert_bag(plant.overflow.home_queue()[0].id)
    plant.divert_bag(plant.overflow.home_queue()[0].id)
    # home queue is empty -> room is 6; limit the recovery to one
    result = plant.recover_overflow(batch_id=batch.id, limit=1)
    assert len(result["returned"]) == 1
    assert plant.overflow.history.batch(batch.id).status == "outbound"
    account = plant.overflow.account.accounts()["T2"]
    assert account["held"] == 2
    assert account["movement_balance"] == -2
    assert plant.overflow_reconcile()["ok"] is True


def test_snapshots_are_written_and_tampering_is_detected(plant, flight, tmp_path):
    _configure(plant)
    _surge(plant, flight, 5)
    plant.send_overflow(3)
    assert plant.overflow.verify_evidence() >= 2
    store_path = Path(plant.store.path)
    evidence_dir = store_path.parent / "overflow-evidence"
    files = list(evidence_dir.glob("*.json"))
    assert any("transfer-pre" in f.name for f in files)
    assert any("transfer-post" in f.name for f in files)

    anchor = plant.store.state().evidence_anchors[0]
    target = evidence_dir / f"{anchor.ref}.json"
    document = target.read_text(encoding="utf-8")
    target.write_text(document.replace('"count": 3', '"count": 9', 1), encoding="utf-8")
    result = plant.overflow_reconcile()
    assert result["ok"] is False
    assert any("evidence" in problem for problem in result["problems"])


def test_history_recomputes_from_the_journal_and_survives_restart(plant, flight):
    _configure(plant, grant=5)
    _surge(plant, flight, 5)
    batch = plant.send_overflow(3)
    plant.rollback_overflow(batch.id)
    history = plant.overflow_history()
    assert history["matches_live"] is True
    assert len(history["batches"]) == 1
    assert replay_check(plant.store) is True

    plant.store.persist()
    reopened = Plant_reopen(plant.store.path)
    assert reopened.overflow.history.batch(batch.id).status == "rolled_back"
    assert reopened.overflow_reconcile()["ok"] is True


def Plant_reopen(path):
    from bagsort.app import Plant
    from bagsort.clock import Clock

    return Plant.build(Store.open(path), terminal="T1", clock=Clock())


def test_forecast_mode_holds_without_enough_samples(plant, flight):
    _configure(
        plant,
        mode=MODE_FORECAST,
        high_water=50,
        forecast_horizon=2,
        forecast_fire_rate=4.0,
    )
    _surge(plant, flight, 10)
    plant.record_arrival(8)
    decision = plant.overflow.evaluate()
    assert decision.action == "hold"
    assert "confidence" in decision.reason
    assert len(decision.backstops) == 4


def test_forecast_mode_fires_ahead_of_the_queue(plant, flight):
    _configure(
        plant,
        mode=MODE_FORECAST,
        high_water=50,
        forecast_horizon=2,
        forecast_fire_rate=4.0,
        transfer_cap=4,
    )
    _surge(plant, flight, 20)
    for count in (3, 5, 6):
        plant.record_arrival(count)
    decision = plant.overflow.evaluate()
    assert decision.action == "transfer"
    assert decision.queue_length < 50  # the physical queue never hit high water
    assert decision.forecast["samples"] == 3
    batch = plant.send_overflow(decision.suggested, reason=decision.reason)
    assert batch.trigger == MODE_FORECAST


def test_forecast_math_is_recomputable():
    samples = [ArrivalSample(at="t", count=c) for c in (2, 4, 6)]
    forecast = predict(samples, horizon=2)
    assert forecast.rate == ewma(samples)
    assert forecast.samples == 3
    assert forecast.expected == forecast.rate * 2
    assert predict([]).rate == 0.0
    assert predict([]).confidence == 0.0


def test_policy_rejects_a_flappy_configuration():
    with pytest.raises(Exception):
        Policy(mode=MODE_QUEUE, high_water=3, recover_water=3).validated()
    with pytest.raises(Exception):
        Policy(mode="bogus").validated()


def test_cooldown_prevents_back_to_back_movements(plant, flight):
    _configure(plant, cooldown_ticks=2)
    _surge(plant, flight, 5)
    plant.send_overflow(3)
    # rolled-back bags came home, queue is long again, but cooldown must hold
    plant.rollback_overflow(plant.overflow.history.batches()[0].id)
    decision = plant.overflow.evaluate()
    assert decision.action == "hold"
    assert "cooldown" in decision.reason


def test_a_bag_can_leave_twice_after_coming_home(plant, flight):
    _configure(plant, high_water=10, recover_water=5)
    _surge(plant, flight, 5)
    first = plant.send_overflow(3, reason="surge one")
    for bag in plant.overflow.home_queue():
        plant.divert_bag(bag.id)
    plant.recover_overflow(batch_id=first.id)
    # the same bags are injected at home again; a second surge reuses them
    plant.check_in("NEW0001", flight.id)
    second = plant.send_overflow(3, reason="surge two")
    assert set(second.bag_ids) <= set(first.bag_ids) | {"T1-NEW0001"}
    records = plant.overflow.history.records()
    assert len([r for r in records if r.batch_id == first.id]) == 3
    assert len([r for r in records if r.batch_id == second.id]) == 3
    assert plant.overflow_reconcile()["ok"] is True


def test_outbound_bag_is_refused_by_every_home_stage(plant, flight):
    _configure(plant)
    _surge(plant, flight, 5)
    batch = plant.send_overflow(3)
    bag_id = batch.bag_ids[0]
    bag = plant.bag(bag_id)
    with pytest.raises(ConflictError):
        plant.push_bag(bag_id, "T1-CHA")
    with pytest.raises(OrderingError):
        plant.enqueue_recheck(bag_id)
    with pytest.raises(OrderingError):
        plant.belt.place(bag)
    # divert operates on the home record only; an outbound bag is not divertible
    with pytest.raises(ConflictError):
        plant.divert_bag(bag_id)


def test_history_at_a_time_only_counts_what_had_happened(plant, flight):
    _configure(plant, grant=5, high_water=10, recover_water=5)
    _surge(plant, flight, 5)
    first = plant.send_overflow(3)
    plant.clock.advance(60)
    for bag in plant.overflow.home_queue():
        plant.divert_bag(bag.id)
    plant.recover_overflow(batch_id=first.id)
    at_return = plant.clock.now_iso()
    plant.clock.advance(60)
    before = plant.overflow_history(first.created_at)
    assert before["outbound_count"] == 3
    after = plant.overflow_history(at_return)
    assert after["outbound_count"] == 0
