"""Unit tests for HarmonicResidualEngine."""

import sys
from pathlib import Path

import numpy as np

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from engine_base import HistoryWindow  # noqa: E402
from engines.harmonic_residual.engine import HarmonicResidualEngine  # noqa: E402


def _ts(n, step_s=300):
    # n ISO-8601 stamps at `step_s` seconds apart (default 5-min cadence).
    return [f"2024-01-01T00:00:00+00:00" if i == 0 else
            np.datetime64("2024-01-01T00:00:00") + np.timedelta64(i * step_s, "s")
            for i in range(n)]


def _iso(n, step_s=300):
    base = np.datetime64("2024-01-01T00:00:00")
    return [str(base + np.timedelta64(i * step_s, "s")) for i in range(n)]


def test_constant_history_predicts_constant():
    engine = HarmonicResidualEngine()
    n = 100
    f = engine.forecast(HistoryWindow(_iso(n), [100.0] * n))
    assert abs(f.predicted_rps - 100.0) < 1e-6
    assert f.confidence_lower <= f.predicted_rps <= f.confidence_upper


def test_empty_history_returns_zero():
    f = HarmonicResidualEngine().forecast(HistoryWindow([], []))
    assert f.predicted_rps == 0.0


def test_short_history_falls_back_to_mean():
    engine = HarmonicResidualEngine(min_history=12)
    rates = [10.0, 20.0, 30.0]
    f = engine.forecast(HistoryWindow(_iso(3), rates))
    assert abs(f.predicted_rps - 20.0) < 1e-6  # mean of history


def test_tracks_linear_ramp_better_than_persistence():
    # On a clean ramp the model should project the trend, landing closer to the
    # next value than persistence (which always lags by one slope step).
    engine = HarmonicResidualEngine()
    n = 600
    slope = 0.2
    rates = [50.0 + slope * i for i in range(n)]
    f = engine.forecast(HistoryWindow(_iso(n), rates))
    truth_next = 50.0 + slope * n
    persistence = rates[-1]
    assert abs(f.predicted_rps - truth_next) < abs(persistence - truth_next)


def test_band_is_finite_and_ordered():
    rng = np.random.default_rng(0)
    n = 600
    rates = (60.0 + 20.0 * np.sin(2 * np.pi * np.arange(n) / 288)
             + rng.normal(0, 4, n)).tolist()
    f = HarmonicResidualEngine().forecast(HistoryWindow(_iso(n), rates))
    assert np.isfinite(f.predicted_rps)
    assert f.confidence_lower <= f.predicted_rps <= f.confidence_upper
    assert f.confidence_lower >= 0.0


def test_deterministic():
    n = 400
    rates = (50.0 + 10.0 * np.sin(2 * np.pi * np.arange(n) / 288)).tolist()
    hw = HistoryWindow(_iso(n), rates)
    a = HarmonicResidualEngine().forecast(hw)
    b = HarmonicResidualEngine().forecast(hw)
    assert a.predicted_rps == b.predicted_rps
    assert a.confidence_lower == b.confidence_lower
    assert a.confidence_upper == b.confidence_upper


def test_infers_one_minute_cadence_period():
    # At 1-min cadence the inferred daily period is 1440; with <2 cycles of data
    # the engine still produces a sane, finite forecast (seasonal basis dropped).
    engine = HarmonicResidualEngine()
    n = 500
    rates = [40.0 + 0.01 * i for i in range(n)]
    f = engine.forecast(HistoryWindow(_iso(n, step_s=60), rates))
    assert np.isfinite(f.predicted_rps)
    assert f.predicted_rps > 0
