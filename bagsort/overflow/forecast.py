"""Arrival rate prediction for forecast-triggered overflow.

The queue trigger reacts only after bags are already piling up; the forecast
trigger reacts to how fast bags are *expected* to arrive. Predictions here are
intentionally simple and fully derivable from committed arrival samples, so a
forecast that drove a transfer can be recomputed later from the history rather
than trusted as a remembered number.

The estimator is an exponentially weighted moving average of observed arrivals
per tick. Because an EWMA never forgets, its uncertainty only shrinks while
samples accumulate; ``confidence`` is reported alongside the rate so the
controller can refuse to act on a forecast that does not yet have evidence
behind it. That refusal is the first guard against false transfers.
"""

from __future__ import annotations

from dataclasses import dataclass

from bagsort.model.entities import ArrivalSample

DEFAULT_ALPHA = 0.4
MIN_SAMPLES = 3


@dataclass(frozen=True)
class Forecast:
    """Expected arrivals per tick and how much evidence backs the estimate."""

    rate: float
    samples: int
    confidence: float
    horizon: int
    expected: float

    def as_dict(self) -> dict[str, object]:
        return {
            "rate": round(self.rate, 6),
            "samples": self.samples,
            "confidence": round(self.confidence, 6),
            "horizon": self.horizon,
            "expected": round(self.expected, 6),
        }


def ewma(samples: list[ArrivalSample], alpha: float = DEFAULT_ALPHA) -> float:
    """Exponentially weighted mean of the observed counts, oldest first."""

    if not samples:
        return 0.0
    rate = float(samples[0].count)
    for sample in samples[1:]:
        rate += alpha * (sample.count - rate)
    return rate


def predict(
    samples: list[ArrivalSample],
    horizon: int = 1,
    alpha: float = DEFAULT_ALPHA,
) -> Forecast:
    """Forecast arrivals over the next ``horizon`` ticks.

    Confidence approaches one with the number of samples; with fewer than
    :data:`MIN_SAMPLES` observations it stays below one so the controller can
    decline forecast-driven transfers entirely.
    """

    count = len(samples)
    rate = ewma(samples, alpha)
    confidence = count / (count + MIN_SAMPLES) if count else 0.0
    return Forecast(
        rate=rate,
        samples=count,
        confidence=confidence,
        horizon=horizon,
        expected=rate * horizon,
    )
