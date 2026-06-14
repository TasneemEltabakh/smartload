"""
services/forecasting/engine_base.py
───────────────────────────────────
Abstract base class for forecasting engines + factory.

NOT yet imported by app.py. Named `engine_base.py` to avoid collision
with per-plugin `engines/<plugin>/engine.py`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class HistoryWindow:
    """Recent request-rate history feeding the forecaster."""

    timestamps: list[str]   # ISO 8601
    request_rates: list[float]


@dataclass
class Forecast:
    """Engine output. Run loop converts to a ForecastResult envelope."""

    horizon_minutes: int
    predicted_rps: float
    confidence_lower: float
    confidence_upper: float


class ForecastEngine(ABC):
    @abstractmethod
    def forecast(self, history: HistoryWindow) -> Forecast:
        """Predict the next-horizon RPS from a window of recent observations."""

    def reload(self) -> None:
        """Optional hook called when policy changes."""


def select_engine(name: str, **kwargs) -> ForecastEngine:
    if name == "moving_average":
        from engines.moving_average.engine import MovingAverageEngine
        return MovingAverageEngine(**kwargs)
    if name == "arima":
        from engines.arima.engine import ArimaEngine
        return ArimaEngine(**kwargs)
    if name == "harmonic_residual":
        from engines.harmonic_residual.engine import HarmonicResidualEngine
        return HarmonicResidualEngine(**kwargs)
    raise ValueError(f"Unknown forecast engine: {name!r}")
