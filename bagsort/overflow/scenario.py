"""Deterministic rush-hour scenario comparing the three triggers.

The same arrival curve is replayed once per policy against a fresh store, so
the only difference between runs is the trigger. The curve contains:

* a genuine sustained peak that every policy must handle;
* a short phantom surge *before* the peak that only forecast-based policies
  see (via the upstream advisory), which is what produces false transfers;
* a taper where the correct action is to recall and then release bags.

Nothing here reads randomness or the wall clock: advisory values and arrivals
are fixed lists, so two runs are byte-identical.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bagsort.clock import Clock
from bagsort.store.store import Store

from .archive import SnapshotArchive
from .controller import OverflowController
from .forecast import ArrivalForecast
from .history import recompute, summary
from .policy import build_policy

# 80 deterministic minutes, service rate 10/min, home capacity 60. Each entry
# is ``(arrivals, upstream_advisory)``. There are two forecast events:
#
#   * minutes 26-28: an advisory at 24 while the queue is at the confirm line,
#     but no surge follows (arrivals drop to 4). The predictive policy diverts
#     the full projected excess; the hybrid releases only its capped first
#     tranche (4) and the backstop recalls it once the surge fails to show.
#   * minutes 31-33: a genuine advisory right before the real 22/min peak.
#     Pre-armed bags stay held because the counterfactual queue really would
#     have overflowed -- they count as *needed*, not false transfers.
#
# The reactive policy ignores every advisory and only moves bags once the
# observed queue crosses capacity.
def _curve() -> tuple[tuple[int, float], ...]:
    schedule: list[tuple[int, float]] = []
    # minutes 0-25: baseline 12/min slowly builds the queue to the confirm line
    schedule += [(12, 0.0)] * 26
    # minutes 26-28: bogus advisory, no surge behind it
    schedule += [(12, 24.0)] * 3
    # minutes 29-30: the promised surge fails to arrive; the line drains
    schedule += [(4, 0.0)] * 2
    # minutes 31-33: the queue rebuilds and a genuine advisory arrives
    schedule += [(12, 24.0)] * 3
    # minutes 34-51: the real peak
    schedule += [(22, 0.0)] * 18
    # minutes 52-55: taper
    schedule += [(10, 0.0)] * 4
    # minutes 56-95: a long quiet tail so every held batch is recalled and
    # every returned bag is released before the scenario ends
    schedule += [(4, 0.0)] * 40
    return tuple(schedule)


CURVE = _curve()
SERVICE_RATE = 10
HOME_CAPACITY = 60
BACKUP_CAPACITY = 120


@dataclass
class ScenarioResult:
    policy: str
    minutes: int
    peak_queue: int
    overflow_minutes: int
    bags_moved: int
    recalled: int
    misdiverted_bags: int
    misdiverted_rate: float
    transfers: int
    rolled_back: int
    snapshots: int
    chain_ok: bool
    reconciled: bool
    timeline: list[dict[str, object]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "policy": self.policy,
            "minutes": self.minutes,
            "peak_queue": self.peak_queue,
            "overflow_minutes": self.overflow_minutes,
            "bags_moved": self.bags_moved,
            "recalled": self.recalled,
            "misdiverted_bags": self.misdiverted_bags,
            "misdiverted_rate": self.misdiverted_rate,
            "transfers": self.transfers,
            "rolled_back": self.rolled_back,
            "snapshots": self.snapshots,
            "chain_ok": self.chain_ok,
            "reconciled": self.reconciled,
        }


def run_policy(
    policy_name: str,
    snapshot_dir: str,
    store: Store | None = None,
) -> ScenarioResult:
    """Replay the fixed curve under one policy and measure the outcome."""

    store = store or Store(f"var/overflow-{policy_name}.json")
    clock = Clock()
    forecast = ArrivalForecast(alpha=0.5, quantile_sigma=1.28)
    policy = build_policy(policy_name)
    controller = OverflowController(
        store,
        clock,
        policy,
        SnapshotArchive(snapshot_dir),
        forecast=forecast,
        service_rate=SERVICE_RATE,
    )
    controller.configure("T1", "T2", HOME_CAPACITY, BACKUP_CAPACITY)

    peak_queue = 0
    overflow_minutes = 0
    timeline: list[dict[str, object]] = []
    recalled_total = 0
    bag_seq = 0
    for minute, (arrivals, advisory) in enumerate(CURVE):
        # The reactive policy never consumes upstream advisories at all.
        forecast.set_advisory(advisory if policy_name != "reactive" else 0.0)
        # admit this minute's arrivals, let the station process its normal
        # exits at the service rate, and only then ask the policy to act --
        # overflow transfers only cover what the station could not process.
        for _ in range(arrivals):
            bag_seq += 1
            bag_id = f"BAG{minute:02d}-{bag_seq:04d}"
            try:
                controller.admit(bag_id)
            except Exception:
                pass
        # Normal processing clears the oldest queued bags first (FIFO).
        queued = controller.ledger.home_queue()
        for bag in queued[: min(SERVICE_RATE, len(queued))]:
            controller.depart_home(bag.bag_id)
        before_queue = controller.ledger.queue_length()
        held_before = len(controller.ledger.held_bags())
        result = controller.tick(arrivals=arrivals)
        recalled_total += max(0, held_before - len(controller.ledger.held_bags()))
        peak_queue = max(peak_queue, before_queue)
        if before_queue > HOME_CAPACITY:
            overflow_minutes += 1
        timeline.append(
            {
                "minute": minute,
                "arrivals": arrivals,
                "advisory": advisory,
                "queue_before": before_queue,
                "action": result.decision.action,
                "amount": result.decision.amount,
                "held": controller.ledger.occupancy("T2"),
            }
        )
        clock.advance(60)

    # Peak is over: drain the home queue, release returned bags, and recall
    # held batches in a loop until nothing is away from home. Extra ticks keep
    # the synthetic clock advancing so timestamps stay ordered.
    final_recalled = 0
    for _ in range(200):
        queued = controller.ledger.home_queue()
        for bag in queued[: min(SERVICE_RATE, len(queued))]:
            controller.depart_home(bag.bag_id)
        for bag in list(controller.ledger.returned_bags()):
            controller.depart_home(bag.bag_id)
        moved = 0
        if controller.ledger.held_bags():
            step = controller.recall()
            moved = sum(len(item["bag_ids"]) for item in step["recalled"])
            final_recalled += moved
        clock.advance(60)
        if (
            not controller.ledger.held_bags()
            and not controller.ledger.returned_bags()
            and not controller.ledger.home_queue()
        ):
            break
    recalled_total += final_recalled
    history = recompute(store.entries(), needed_window=6)
    stats = summary(history)
    chain = SnapshotArchive(snapshot_dir).verify()
    kinds = store.kinds()
    return ScenarioResult(
        policy=policy_name,
        minutes=len(CURVE),
        peak_queue=peak_queue,
        overflow_minutes=overflow_minutes,
        bags_moved=int(stats["bags_moved"]),
        recalled=recalled_total,
        misdiverted_bags=int(stats["misdiverted_bags"]),
        misdiverted_rate=float(stats["misdiverted_rate"]),
        transfers=int(stats["transfers"]),
        rolled_back=kinds.get("overflow.rollback", 0),
        snapshots=chain["count"],
        chain_ok=bool(chain["ok"]),
        reconciled=bool(controller.reconcile()["ok"]),
        timeline=timeline,
    )


def compare(snapshot_root: str, policies: tuple[str, ...] = ("reactive", "hybrid", "predictive")) -> list[ScenarioResult]:
    """Run every policy against its own fresh journal and snapshot directory."""

    import os

    results: list[ScenarioResult] = []
    for name in policies:
        directory = os.path.join(snapshot_root, name)
        store = Store(os.path.join(snapshot_root, f"{name}.json"))
        results.append(run_policy(name, directory, store=store))
    return results
