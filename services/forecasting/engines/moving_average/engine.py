"""Moving-average forecaster. Baseline before ARIMA / Prophet ship."""

from __future__ import annotations

import math
import sys
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from engine_base import ForecastEngine, Forecast, HistoryWindow  # noqa: E402


class MovingAverageEngine(ForecastEngine):
    """Predicts next horizon by averaging the last N observations.

    No confidence interval estimation beyond a simple stddev band.
    """

    def __init__(self, horizon_minutes: int = 5, window_samples: int = 60):
        self.horizon_minutes = horizon_minutes
        self.window_samples = window_samples

    def forecast(self, history: HistoryWindow) -> Forecast:
        rates = [r for r in history.request_rates[-self.window_samples :] if math.isfinite(r)]
        if not rates:
            return Forecast(self.horizon_minutes, 0.0, 0.0, 0.0)

        mean = sum(rates) / len(rates)
        if len(rates) >= 2:
            var = sum((r - mean) ** 2 for r in rates) / (len(rates) - 1)
            std = var**0.5
        else:
            std = 0.0

        return Forecast(
            horizon_minutes=self.horizon_minutes,
            predicted_rps=mean,
            confidence_lower=max(0.0, mean - std),
            confidence_upper=mean + std,
        )
