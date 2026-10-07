"""When and how much overflow moves: the trigger policy.

Two trigger signals are supported because they fail in different ways:

``queue``
    Fire on the queue length that is already physically present. It cannot be
    fooled by a forecast, but it only reacts after bags have piled up, so the
    line has to absorb a spike before help arrives.

``forecast``
    Fire on arrivals expected over the next ticks. It moves bags out *ahead* of
    a predicted surge, but a wrong prediction ships bags for no reason - a false
    transfer.

The policy keeps the two comparable and records the false-transfer backstops
next to every decision: hysteresis (separate fire/recover levels), a minimum
sample count for forecast confidence, a transfer cap so one decision can never
empty the hall, and reversible outbound batches so a bad call can be undone by
:meth:`OverflowControl.rollback_batch` instead of being lived with.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bagsort.errors import ValidationError
from bagsort.overflow.forecast import MIN_SAMPLES, Forecast

MODE_QUEUE = "queue"
MODE_FORECAST = "forecast"
MODES = (MODE_QUEUE, MODE_FORECAST)


@dataclass(frozen=True)
class Policy:
    """Tunable thresholds for one home terminal."""

    mode: str = MODE_QUEUE
    high_water: int = 20
    recover_water: int = 8
    forecast_horizon: int = 3
    forecast_fire_rate: float = 6.0
    min_confidence_samples: int = MIN_SAMPLES
    transfer_cap: int = 10
    cooldown_ticks: int = 2

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "high_water": self.high_water,
            "recover_water": self.recover_water,
            "forecast_horizon": self.forecast_horizon,
            "forecast_fire_rate": self.forecast_fire_rate,
            "min_confidence_samples": self.min_confidence_samples,
            "transfer_cap": self.transfer_cap,
            "cooldown_ticks": self.cooldown_ticks,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Policy":
        return cls(
            mode=str(raw.get("mode", MODE_QUEUE)),
            high_water=int(raw.get("high_water", 20)),
            recover_water=int(raw.get("recover_water", 8)),
            forecast_horizon=int(raw.get("forecast_horizon", 3)),
            forecast_fire_rate=float(raw.get("forecast_fire_rate", 6.0)),
            min_confidence_samples=int(raw.get("min_confidence_samples", MIN_SAMPLES)),
            transfer_cap=int(raw.get("transfer_cap", 10)),
            cooldown_ticks=int(raw.get("cooldown_ticks", 2)),
        )

    def validated(self) -> "Policy":
        if self.mode not in MODES:
            raise ValidationError(f"overflow mode must be one of {MODES}")
        if self.high_water <= 0 or self.recover_water < 0:
            raise ValidationError("water marks must be positive / non-negative")
        if self.recover_water >= self.high_water:
            raise ValidationError(
                "recover_water must sit below high_water so transfers cannot flap"
            )
        if self.forecast_horizon < 1:
            raise ValidationError("forecast horizon must be at least one tick")
        if self.forecast_fire_rate <= 0:
            raise ValidationError("forecast fire rate must be positive")
        if self.min_confidence_samples < 1:
            raise ValidationError("min confidence samples must be at least one")
        if self.transfer_cap < 1:
            raise ValidationError("transfer cap must be at least one")
        if self.cooldown_ticks < 0:
            raise ValidationError("cooldown ticks must not be negative")
        return self


@dataclass(frozen=True)
class Decision:
    """The outcome of evaluating the policy; nothing here mutates the plant."""

    action: str
    mode: str
    queue_length: int
    overflow_out: int
    available: int
    suggested: int
    reason: str
    forecast: dict[str, Any] = field(default_factory=dict)
    backstops: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "mode": self.mode,
            "queue_length": self.queue_length,
            "overflow_out": self.overflow_out,
            "available": self.available,
            "suggested": self.suggested,
            "reason": self.reason,
            "forecast": dict(self.forecast),
            "backstops": list(self.backstops),
        }


def evaluate(
    policy: Policy,
    queue_length: int,
    overflow_out: int,
    available: int,
    forecast: Forecast | None,
    cooled_down: bool,
) -> Decision:
    """Decide ``hold`` or ``transfer`` and how many bags, without acting."""

    backstops = [
        "hysteresis: recover_water < high_water",
        "transfer_cap bounds one decision",
        "forecast needs min_confidence_samples before it fires",
        "outbound batches are reversible via rollback",
    ]
    forecast_view = forecast.as_dict() if forecast is not None else {}

    if not cooled_down:
        return Decision(
            action="hold",
            mode=policy.mode,
            queue_length=queue_length,
            overflow_out=overflow_out,
            available=available,
            suggested=0,
            reason="cooldown after the previous movement",
            forecast=forecast_view,
            backstops=backstops,
        )
    if overflow_out and queue_length < policy.recover_water:
        return Decision(
            action="recover",
            mode=policy.mode,
            queue_length=queue_length,
            overflow_out=overflow_out,
            available=available,
            suggested=overflow_out,
            reason="queue back below recover_water",
            forecast=forecast_view,
            backstops=backstops,
        )

    fired = False
    reason = f"queue {queue_length} below high_water {policy.high_water}"
    if policy.mode == MODE_QUEUE:
        fired = queue_length >= policy.high_water
        if fired:
            reason = f"queue {queue_length} reached high_water {policy.high_water}"
    else:
        if forecast is None:
            reason = "no forecast available"
        elif forecast.samples < policy.min_confidence_samples:
            reason = (
                f"forecast confidence insufficient: {forecast.samples} samples "
                f"< {policy.min_confidence_samples} required"
            )
        elif forecast.expected < policy.forecast_fire_rate:
            reason = (
                f"forecast {forecast.expected:.2f} arrivals below fire rate "
                f"{policy.forecast_fire_rate}"
            )
        elif queue_length < policy.high_water:
            fired = True
            reason = (
                f"forecast {forecast.expected:.2f} arrivals over "
                f"{policy.forecast_horizon} ticks exceeds {policy.forecast_fire_rate}"
            )
        else:
            fired = True
            reason = "forecast surge and queue already at high_water"

    if not fired:
        return Decision(
            action="hold",
            mode=policy.mode,
            queue_length=queue_length,
            overflow_out=overflow_out,
            available=available,
            suggested=0,
            reason=reason,
            forecast=forecast_view,
            backstops=backstops,
        )

    suggested = min(
        policy.transfer_cap,
        max(queue_length - policy.recover_water, 0),
        available,
    )
    if available <= 0:
        return Decision(
            action="hold",
            mode=policy.mode,
            queue_length=queue_length,
            overflow_out=overflow_out,
            available=available,
            suggested=0,
            reason="backup terminal has no available allowance",
            forecast=forecast_view,
            backstops=backstops,
        )
    if suggested <= 0:
        return Decision(
            action="hold",
            mode=policy.mode,
            queue_length=queue_length,
            overflow_out=overflow_out,
            available=available,
            suggested=0,
            reason="movement would not bring the queue below high_water",
            forecast=forecast_view,
            backstops=backstops,
        )
    return Decision(
        action="transfer",
        mode=policy.mode,
        queue_length=queue_length,
        overflow_out=overflow_out,
        available=available,
        suggested=suggested,
        reason=reason,
        forecast=forecast_view,
        backstops=backstops,
    )
