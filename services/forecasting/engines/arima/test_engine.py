"""
services/forecasting/engines/arima/test_engine.py
──────────────────────────────────────────────────
Unit tests for the ArimaEngine.

No Docker, no Redis, no DB. The tests cover:

  1. Engine constructs cleanly even when the artifact is missing
     (artifact-absent → fallback path is the safety net).
  2. forecast() with an empty HistoryWindow returns zero-valued Forecast.
  3. Fallback path: missing artifact → mean-of-history with stddev band.
  4. Fallback path: forecast() never raises on a malformed pickle.
  5. Loaded path (skipped if statsmodels not installed locally + artifact
     present): forecast returns a non-negative predicted_rps + confidence
     interval that brackets the prediction.

The expensive "actually call statsmodels + the real 37 MB artifact"
path is conditional on the artifact existing AND statsmodels importing
cleanly. Both are true inside the runtime forecasting container; the
local dev box may not have statsmodels.
"""

from __future__ import annotations

import pickle
import sys
from pathlib import Path

import pytest

_SERVICE = Path(__file__).resolve().parents[3]
if str(_SERVICE) not in sys.path:
    sys.path.insert(0, str(_SERVICE))

from engine_base import Forecast, HistoryWindow              # noqa: E402
from engines.arima.engine import ArimaEngine, _DEFAULT_MODEL_PATH  # noqa: E402


def _history(rates: list[float]) -> HistoryWindow:
    """Build a HistoryWindow from a list of rates. Timestamps are dummy
    ISO strings — the engine only consumes request_rates."""
    timestamps = [f"2026-01-01T00:{i:02d}:00+00:00" for i in range(len(rates))]
    return HistoryWindow(timestamps=timestamps, request_rates=rates)


# ── construction ─────────────────────────────────────────────────────────────


def test_engine_constructs_without_artifact(tmp_path):
    engine = ArimaEngine(model_path=str(tmp_path / "no-such.pkl"))
    assert engine.model_loaded is False


def test_engine_constructs_with_malformed_artifact(tmp_path):
    bad = tmp_path / "bad.pkl"
    bad.write_bytes(b"not a pickle")
    engine = ArimaEngine(model_path=str(bad))
    assert engine.model_loaded is False


def test_engine_constructs_with_wrong_bundle_shape(tmp_path):
    """A valid pickle that's missing the required keys must degrade
    gracefully rather than raising at construction time."""
    bad = tmp_path / "wrong-shape.pkl"
    bad.write_bytes(pickle.dumps({"unexpected": "shape"}))
    engine = ArimaEngine(model_path=str(bad))
    assert engine.model_loaded is False


# ── forecast() — fallback path ───────────────────────────────────────────────


def test_forecast_empty_history_returns_zero(tmp_path):
    engine = ArimaEngine(model_path=str(tmp_path / "absent.pkl"))
    f = engine.forecast(_history([]))
    assert isinstance(f, Forecast)
    assert f.predicted_rps == 0.0
    assert f.confidence_lower == 0.0
    assert f.confidence_upper == 0.0


def test_forecast_fallback_returns_mean_of_history(tmp_path):
    engine = ArimaEngine(model_path=str(tmp_path / "absent.pkl"))
    f = engine.forecast(_history([10.0, 20.0, 30.0, 40.0, 50.0]))
    assert f.predicted_rps == pytest.approx(30.0)
    # stddev of [10..50 step 10] = sqrt(250) ≈ 15.81
    assert f.confidence_upper > f.predicted_rps
    assert f.confidence_lower < f.predicted_rps
    assert f.confidence_lower >= 0.0


def test_forecast_fallback_single_sample(tmp_path):
    """Single-sample history: std should be 0 → CI collapses to predicted."""
    engine = ArimaEngine(model_path=str(tmp_path / "absent.pkl"))
    f = engine.forecast(_history([42.0]))
    assert f.predicted_rps == pytest.approx(42.0)
    assert f.confidence_lower == pytest.approx(42.0)
    assert f.confidence_upper == pytest.approx(42.0)


def test_forecast_fallback_never_raises(tmp_path):
    """Even a horrible history should produce a Forecast, never propagate."""
    engine = ArimaEngine(model_path=str(tmp_path / "absent.pkl"))
    f = engine.forecast(_history([float("inf")] * 3))
    assert isinstance(f, Forecast)


# ── loaded path (runtime / CI only) ──────────────────────────────────────────


@pytest.mark.skipif(
    not _DEFAULT_MODEL_PATH.exists(),
    reason="trained arima_model.pkl not present; runtime path only.",
)
def test_loaded_engine_returns_nonnegative_with_ci():
    pytest.importorskip("statsmodels")
    engine = ArimaEngine()
    assert engine.model_loaded is True
    # Use a small, well-behaved history so the model has something to
    # consume without dominating the prediction.
    rates = [10.0, 11.0, 12.0, 13.0, 12.5, 11.5, 12.0, 13.5, 14.0, 13.0]
    f = engine.forecast(_history(rates))
    assert f.predicted_rps >= 0.0
    assert f.confidence_lower >= 0.0
    assert f.confidence_upper >= f.predicted_rps
    assert f.horizon_minutes == 5
