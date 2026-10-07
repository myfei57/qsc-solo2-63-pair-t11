"""When to divert, how much, and when to recall.

Three policies are provided so the response-speed vs false-transfer trade-off
is a testable choice rather than a hard-coded feeling:

* :class:`ReactivePolicy` triggers purely on the observed queue. It cannot
  false-trigger on a bad forecast (no phantom arrivals exist), but by the time
  the queue is over the line the station is already overflowing.
* :class:`PredictivePolicy` triggers on a forecast upper quantile. It acts
  earlier, but a wrong forecast (or a bogus upstream advisory) diverts bags
  that never needed moving -- the false-transfer risk this policy carries.
* :class:`HybridPolicy` is the recommended default. Prediction may only *arm*
  one small, capped first tranche before the queue overflows; every further
  tranche needs the observed queue to confirm the surge; and anything moved
  purely on prediction is auto-recalled once the prediction fails to
  materialise. The exposure to a wrong forecast is therefore bounded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .forecast import ArrivalForecast

DIVERT = "divert"
RECALL = "recall"
HOLD = "hold"


@dataclass
class PolicyContext:
    """Everything a policy may look at, in one frozen picture."""

    minute: str
    queue: int
    capacity: int
    held: int
    free_home: int
    free_backup: int
    service_rate: int
    forecast: "ForecastView | None"
    home_occupancy: int = 0
    outstanding: int = 0
    armed: bool = False
    projected: int = 0

    @property
    def recall_room(self) -> int:
        """Bags that can come home now without exceeding home capacity.

        Home occupancy includes the queue plus in-flight and already-returned
        bags; held bags are at the backup. After recall the home count becomes
        ``home_occupancy + recalled`` and must not exceed capacity.
        """

        return max(0, self.capacity - self.home_occupancy)


@dataclass
class PolicyDecision:
    """One policy judgement."""

    action: str
    amount: int = 0
    basis: str = ""
    projected: int = 0
    armed: bool = False
    predictive_only: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "amount": self.amount,
            "basis": self.basis,
            "projected": self.projected,
            "armed": self.armed,
            "predictive_only": self.predictive_only,
            "reason": self.reason,
        }


class ForecastView(Protocol):
    expected: float
    upper: float
    observed: float
    advisory: float


class Policy(Protocol):
    name: str

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        ...


def _recall_decision(
    queue: int,
    held: int,
    room: int,
    recall_water: float,
    capacity: int,
    reason: str,
) -> PolicyDecision:
    # Only recall what physically fits at home.
    if held > 0 and queue <= capacity * recall_water:
        amount = min(held, max(0, room))
        if amount > 0:
            return PolicyDecision(action=RECALL, amount=amount, basis="queue", reason=reason)
    return PolicyDecision(action=HOLD, basis="queue")


@dataclass
class ReactivePolicy:
    """Current queue length only. No false alarms, no head start."""

    high_water: float = 1.0
    recall_water: float = 0.8
    name: str = "reactive"

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        trigger = context.capacity * self.high_water
        if context.queue > trigger:
            # Move just enough to get back to the trigger line.
            amount = min(
                context.queue - int(trigger),
                context.free_backup,
                context.queue,
            )
            if amount > 0:
                return PolicyDecision(
                    action=DIVERT,
                    amount=amount,
                    basis="queue",
                    projected=context.queue,
                    reason=f"queue {context.queue} > trigger {int(trigger)}",
                )
        return _recall_decision(
            context.queue,
            context.held,
            context.recall_room,
            self.recall_water,
            context.capacity,
            f"queue {context.queue} back at recall line {int(context.capacity * self.recall_water)}",
        )


@dataclass
class PredictivePolicy:
    """Forecast upper quantile only. Fast, but exposed to forecast error."""

    headroom: float = 1.0
    recall_water: float = 0.8
    name: str = "predictive"

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        if context.forecast is None:
            return PolicyDecision(action=HOLD, basis="forecast", reason="no forecast available")
        projected = _projected(context)
        if projected > context.capacity * self.headroom:
            amount = min(
                projected - int(context.capacity * self.headroom),
                context.free_backup,
                max(context.queue, 0),
            )
            if amount > 0:
                return PolicyDecision(
                    action=DIVERT,
                    amount=amount,
                    basis="forecast",
                    projected=projected,
                    predictive_only=context.queue <= context.capacity,
                    reason=(
                        f"projected {projected} > capacity {context.capacity} "
                        f"(observed queue {context.queue})"
                    ),
                )
        return _recall_decision(
            context.queue,
            context.held,
            context.recall_room,
            self.recall_water,
            context.capacity,
            f"queue {context.queue} back at recall line {int(context.capacity * self.recall_water)}",
        )


@dataclass
class HybridPolicy:
    """Forecast arms a bounded tranche; the observed queue confirms the rest.

    False-transfer protection:

    * prediction alone never moves more than ``first_tranche`` bags;
    * the tranche is only released when the forecast upper band exceeds
      capacity, while a normal-mean forecast only arms (does not move);
    * subsequent tranches require the observed queue to actually cross
      ``confirm_water`` of capacity;
    * a bag moved while the queue was still healthy is tagged
      ``predictive_only`` and is recalled the moment the surge fails to show
      (the *backstop*), even before the usual peak-end recall.
    """

    first_tranche: int = 4
    confirm_water: float = 0.85
    recall_water: float = 0.8
    backstop_water: float = 1.0
    arm_water: float = 1.0
    name: str = "hybrid"

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        capacity = context.capacity
        confirm_line = int(capacity * self.confirm_water)
        recall_line = int(capacity * self.recall_water)
        backstop_line = int(capacity * self.backstop_water)

        # 1. Observed overflow is the hard signal: cover the real excess now.
        if context.queue > capacity:
            amount = min(context.queue - capacity, context.free_backup, context.queue)
            if amount > 0:
                return PolicyDecision(
                    action=DIVERT,
                    amount=amount,
                    basis="queue+forecast",
                    projected=context.projected,
                    armed=context.armed,
                    reason=f"observed queue {context.queue} already over capacity {capacity}",
                )

        # 2. Backstop: prediction moved bags but the surge never arrived. As
        #    long as the home queue is healthy (at or below capacity, i.e. the
        #    prediction has not been confirmed by a real overflow), undo the
        #    pre-emptive tranche so the false-transfer exposure closes fast.
        if context.held > 0 and context.queue <= backstop_line:
            fit = min(context.held, context.recall_room)
            if fit > 0:
                return PolicyDecision(
                    action=RECALL,
                    amount=fit,
                    basis="queue+forecast",
                    projected=context.projected,
                    reason=(
                        f"prediction unconfirmed: queue {context.queue} <= {backstop_line}, "
                        "early backstop recall"
                    ),
                )

        # 3. Queue not overflowing yet: a strong upper-quantile forecast may
        #    release one small capped tranche ahead of time.
        if context.forecast is not None and context.free_backup > 0:
            projected = _projected(context)
            if projected > capacity * self.arm_water and context.queue > confirm_line:
                amount = min(self.first_tranche, context.free_backup, context.queue)
                if amount > 0:
                    return PolicyDecision(
                        action=DIVERT,
                        amount=amount,
                        basis="forecast",
                        projected=projected,
                        armed=True,
                        predictive_only=True,
                        reason=(
                            f"armed tranche: projected {projected} > {capacity} "
                            f"and queue {context.queue} > confirm {confirm_line}"
                        ),
                    )

        # 4. Peak genuinely over: bring everything home.
        return _recall_decision(
            context.queue,
            context.held,
            context.recall_room,
            self.recall_water,
            capacity,
            f"queue {context.queue} back at recall line {recall_line}",
        )


def _projected(context: PolicyContext) -> int:
    """Worst plausible queue one service window out."""

    if context.forecast is None:
        return context.queue
    arrivals = max(context.forecast.upper, context.forecast.advisory)
    projected = context.queue + int(round(arrivals)) - context.service_rate
    return max(0, projected)


def build_policy(name: str, **options: float) -> Policy:
    """Factory used by the CLI and console to choose a trigger by name."""

    kind = (name or "hybrid").strip().lower()
    if kind == "reactive":
        return ReactivePolicy(**_options(ReactivePolicy, options))
    if kind == "predictive":
        return PredictivePolicy(**_options(PredictivePolicy, options))
    if kind == "hybrid":
        return HybridPolicy(**_options(HybridPolicy, options))
    raise ValueError(f"unknown overflow policy {name!r}")


def _options(cls: type, raw: dict[str, object]) -> dict[str, object]:
    """Coerce overrides, ignoring keys the dataclass does not take.

    Integer-typed fields stay ints (counts like ``first_tranche`` must not
    become floats and end up as slice bounds); water-mark fields are floats.
    """

    import dataclasses

    fields = {item.name: item for item in dataclasses.fields(cls)}
    picked: dict[str, object] = {}
    for key, value in raw.items():
        if key in fields and key != "name":
            if fields[key].type is int:
                picked[key] = int(float(value))
            else:
                picked[key] = float(value)
    return picked


def forecast_from(model: ArrivalForecast) -> ForecastView:
    return model.current()
