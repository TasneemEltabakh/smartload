"""
services/forecasting/engine_base.py
───────────────────────────────────
Abstract base class for forecasting engines + factory.

NOT yet imported by app.py. Named `engine_base.py` to avoid collision
with per-plugin `engines/<plugin>/engine.py`.
"""

from __future__ import annotations

import inspect
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


def _accepted_kwargs(cls, kwargs: dict) -> dict:
    """Filter `kwargs` down to the ones `cls.__init__` can accept.

    The run loop hands every engine a single uniform kwargs set (horizon +
    window-samples for the smoother, plus the scaler-facing fit_window /
    robust_mode for the harmonic forecaster). Engines whose __init__ declares
    ``**kwargs`` absorb the extras themselves, but the ones that don't
    (moving_average) would raise TypeError on a param they never declared —
    which, for the moving_average baseline, would defeat the fallback. So drop
    any param the constructor neither names nor catches via **kwargs.
    """
    sig = inspect.signature(cls.__init__)
    params = sig.parameters.values()
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return dict(kwargs)   # **kwargs present → engine absorbs anything
    named = {
        p.name for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                      inspect.Parameter.KEYWORD_ONLY)
    }
    return {k: v for k, v in kwargs.items() if k in named}


def select_engine(name: str, **kwargs) -> ForecastEngine:
    if name == "moving_average":
        from engines.moving_average.engine import MovingAverageEngine
        return MovingAverageEngine(**_accepted_kwargs(MovingAverageEngine, kwargs))
    if name == "arima":
        from engines.arima.engine import ArimaEngine
        return ArimaEngine(**_accepted_kwargs(ArimaEngine, kwargs))
    if name == "harmonic_residual":
        from engines.harmonic_residual.engine import HarmonicResidualEngine
        return HarmonicResidualEngine(**_accepted_kwargs(HarmonicResidualEngine, kwargs))
    raise ValueError(f"Unknown forecast engine: {name!r}")
