"""Unit tests for MovingAverageEngine."""

import sys
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from engine_base import HistoryWindow  # noqa: E402
from engines.moving_average.engine import MovingAverageEngine  # noqa: E402


def test_constant_history_predicts_constant():
    engine = MovingAverageEngine(horizon_minutes=5, window_samples=60)
    history = HistoryWindow(
        timestamps=[f"2026-05-14T12:{i:02d}:00Z" for i in range(10)],
        request_rates=[100.0] * 10,
    )
    f = engine.forecast(history)
    assert f.predicted_rps == 100.0
    assert f.confidence_lower == 100.0
    assert f.confidence_upper == 100.0


def test_empty_history_returns_zero():
    engine = MovingAverageEngine()
    history = HistoryWindow(timestamps=[], request_rates=[])
    f = engine.forecast(history)
    assert f.predicted_rps == 0.0


def test_variance_widens_confidence_band():
    engine = MovingAverageEngine()
    history = HistoryWindow(
        timestamps=[f"2026-05-14T12:{i:02d}:00Z" for i in range(4)],
        request_rates=[50.0, 100.0, 150.0, 200.0],
    )
    f = engine.forecast(history)
    assert f.confidence_upper > f.predicted_rps > f.confidence_lower
