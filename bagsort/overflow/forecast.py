"""Arrival forecasting for the predictive trigger.

A forecast is deliberately simple and explainable: an exponentially weighted
moving average of recent arrivals plus a quantile bump derived from the recent
spread. The same object also records the upstream *advisory* (what the belt is
about to deliver, e.g. a dumped container), which is the source of phantom
surges used to compare false-transfer rates.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Forecast:
    """Expected arrivals one look-ahead window out."""

    expected: float
    upper: float
    observed: float
    advisory: float

    def as_dict(self) -> dict[str, float]:
        return {
            "expected": round(self.expected, 3),
            "upper": round(self.upper, 3),
            "observed": self.observed,
            "advisory": self.advisory,
        }


class ArrivalForecast:
    """EWMA mean with a variance-derived quantile band."""

    def __init__(
        self,
        alpha: float = 0.4,
        quantile_sigma: float = 1.28,
        advisory: float = 0.0,
    ) -> None:
        if not 0.0 < alpha <= 1.0:
            raise ValueError("alpha must be in (0, 1]")
        self._alpha = alpha
        self._sigma = quantile_sigma
        self._mean: float | None = None
        self._var: float = 0.0
        self._last = 0.0
        self._advisory = float(advisory)

    def observe(self, arrivals: float) -> Forecast:
        """Feed one window's observed arrivals and return the forecast for next."""

        value = float(arrivals)
        self._last = value
        if self._mean is None:
            self._mean = value
            self._var = 0.0
        else:
            delta = value - self._mean
            self._mean += self._alpha * delta
            self._var = (1 - self._alpha) * (self._var + self._alpha * delta * delta)
        return self.current()

    def set_advisory(self, arrivals: float) -> None:
        self._advisory = float(max(0.0, arrivals))

    def current(self) -> Forecast:
        mean = self._mean if self._mean is not None else 0.0
        upper = mean + self._sigma * self._var ** 0.5
        return Forecast(
            expected=mean,
            upper=upper,
            observed=self._last,
            advisory=self._advisory,
        )
