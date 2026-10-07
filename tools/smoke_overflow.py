"""End to end exercise of the overflow subsystem without pytest.

Run: python3 tools/smoke_overflow.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bagsort.app import Plant
from bagsort.clock import Clock
from bagsort.errors import ConflictError, OrderingError
from bagsort.overflow.forecast import predict
from bagsort.overflow.policy import MODE_FORECAST, MODE_QUEUE
from bagsort.store.store import Store


def check(label, condition):
    if not condition:
        raise AssertionError(label)
    print(f"  ok - {label}")


def build(tmp):
    store = Store(str(os.path.join(tmp, "state.json")))
    return Plant.build(store, terminal="T1", clock=Clock())


def main():
    with tempfile.TemporaryDirectory() as tmp:
        plant = build(tmp)
        flight = plant.register_flight("CA1234", "CR1", "")

        print("queue trigger configuration and evaluation")
        plant.configure_overflow(
            backup_terminal="T2",
            grant=5,
            mode=MODE_QUEUE,
            high_water=10,
            recover_water=5,
            transfer_cap=3,
            cooldown_ticks=0,
        )
        bags = [plant.check_in(f"BAG{i:04d}", flight.id) for i in range(10)]
        decision = plant.overflow.evaluate()
        check("queue at high water fires transfer", decision.action == "transfer")
        check("suggested respects cap and headroom", decision.suggested == 3)

        print("sending out wraps one transaction and two snapshots")
        batch = plant.send_overflow(decision.suggested, reason=decision.reason)
        check("batch outbound", batch.status == "outbound")
        check("three bags moved", len(batch.bag_ids) == 3)
        sent = [plant.bag(bag_id) for bag_id in batch.bag_ids]
        check("bags now belong to T2", all(b.terminal == "T2" for b in sent))
        check("bags are outbound state", all(b.state.value == "outbound" for b in sent))
        check("account held = 3", plant.overflow.account.held("T2") == 3)
        check("account available = 2", plant.overflow.account.available("T2") == 2)
        check("evidence files on disk", plant.overflow.verify_evidence() >= 1)

        print("outbound bag cannot be checked in again at the home terminal")
        try:
            plant.check_in(sent[0].barcode, flight.id)
            raise AssertionError("expected rejection")
        except ConflictError:
            check("re-checkin of an outbound barcode refused", True)

        print("rollback restores ownership and both accounts")
        rolled = plant.rollback_overflow(batch.id, reason="link down")
        check("batch rolled back", rolled.status == "rolled_back")
        again = [plant.bag(bag_id) for bag_id in batch.bag_ids]
        check("bags home at T1", all(b.terminal == "T1" for b in again))
        check("bags injected again", all(b.state.value == "injected" for b in again))
        check("account held back to zero", plant.overflow.account.held("T2") == 0)
        check("allowance fully returned", plant.overflow.account.available("T2") == 5)
        result = plant.overflow_reconcile()
        check("reconciliation clean after rollback", result["ok"] is True)

        print("send again, then recover after the peak subsides")
        batch2 = plant.send_overflow(3, reason="second surge")
        check("available drops to 2", plant.overflow.account.available("T2") == 2)
        # fill the hall back up so recovery has no headroom
        extra = [plant.check_in(f"FILL{i}", flight.id) for i in range(5)]
        try:
            plant.plan_return()
            raise AssertionError("expected recovery refusal")
        except OrderingError:
            check("recovery refused while queue still at high water", True)
        # drain the home bags (load them away) so the home queue subsides
        for bag in plant.overflow.home_queue():
            plant.divert_bag(bag.id)
        check("home queue drained", plant.overflow._home_queue_length() == 0)
        outcome = plant.recover_overflow(batch_id=batch2.id)
        check("three bags returned", len(outcome["returned"]) == 3)
        returned = [plant.bag(bag_id) for bag_id in batch2.bag_ids]
        check("returned bags home and injected",
              all(b.terminal == "T1" and b.state.value == "injected" for b in returned))
        check("batch marked returned",
              plant.overflow.history.batch(batch2.id).status == "returned")
        check("account restored", plant.overflow.account.available("T2") == 5)

        print("reconciliation, audit and journal replay agree")
        result = plant.overflow_reconcile()
        check("reconciliation clean", result["ok"] is True)
        check("audit chain intact", plant.audit.verify() is True)
        history = plant.overflow_history()
        check("history replay matches live", history["matches_live"] is True)

        print("forecast trigger behaves differently and explains its risk")
        # clear the home hall first so the physical queue cannot be the trigger
        for bag in plant.overflow.home_queue():
            plant.divert_bag(bag.id)
        plant.configure_overflow(
            backup_terminal="T2",
            grant=10,
            mode=MODE_FORECAST,
            high_water=50,
            recover_water=10,
            forecast_horizon=2,
            forecast_fire_rate=4.0,
            transfer_cap=5,
            cooldown_ticks=0,
        )
        # a physical queue large enough to act on, yet below high_water
        forecast_bags = [plant.check_in(f"FC{i}", flight.id) for i in range(20)]
        decision = plant.overflow.evaluate()
        check("forecast with no samples holds", decision.action == "hold")
        check("decision names its backstops", len(decision.backstops) == 4)
        for count in (3, 5, 6):
            plant.record_arrival(count)
        decision = plant.overflow.evaluate()
        check("forecast fires below physical high water", decision.action == "transfer")
        check("forecast view attached", decision.forecast["samples"] == 3)
        batch3 = plant.send_overflow(decision.suggested, reason=decision.reason)
        check("forecast-driven shipment happened", batch3.status == "outbound")
        # cleanup so restart checks see a clean movement balance
        for bag in plant.overflow.home_queue():
            plant.divert_bag(bag.id)
        plant.recover_overflow(batch_id=batch3.id)

        print("insufficient samples guard against a false forecast transfer")
        with tempfile.TemporaryDirectory() as tmp2:
            fresh = build(tmp2)
            fresh.register_flight("CA1", "", "")
            fresh.configure_overflow(
                backup_terminal="T2", grant=10, mode=MODE_FORECAST,
                high_water=50, recover_water=10, forecast_horizon=2,
                forecast_fire_rate=1.0, transfer_cap=5, cooldown_ticks=0,
            )
            [fresh.check_in(f"X{i}", fresh.book.list()[0].id) for i in range(3)]
            fresh.record_arrival(1)
            d = fresh.overflow.evaluate()
            check("low confidence forecast holds", d.action == "hold")

        print("snapshot restart round trip")
        digest = plant.store.persist()
        check("snapshot persisted", bool(digest))
        reopened = Store.open(plant.store.path)
        rebuilt = Plant.build(reopened, terminal="T1", clock=Clock())
        check("batches survive restart",
              len(rebuilt.overflow.history.batches()) == 3)
        check("accounts survive restart",
              rebuilt.overflow.account.available("T2") == 10)
        check("evidence anchors survive restart",
              rebuilt.overflow.verify_evidence() >= 3)
        check("reconciliation clean after restart",
              rebuilt.overflow_reconcile()["ok"] is True)

        print("tampered evidence is detected")
        anchor = plant.store.state().evidence_anchors[0]
        path = os.path.join(plant.overflow._evidence.directory, f"{anchor.ref}.json")
        with open(path, "r", encoding="utf-8") as handle:
            document = handle.read()
        document = document.replace('"count": 3', '"count": 99', 1)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(document)
        check("altered snapshot fails reconciliation",
              plant.overflow_reconcile()["ok"] is False)

    print("\nALL OVERFLOW SCENARIOS PASSED")


if __name__ == "__main__":
    main()
