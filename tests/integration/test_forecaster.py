"""
tests/integration/test_forecaster.py
──────────────────────────────────────
Pure-Python unit tests for services/forecasting/forecaster.py.
No Docker stack required — runs in the unit-tests CI job.

Coverage:
  1. Model loading      — ARIMA pkl loads, active model is set correctly
  2. Predict output     — ForecastResult fields are valid (non-negative, ordered CI)
  3. model_id           — reflects the loaded model name
  4. Fallback chain     — missing exog → ARIMA; no model → moving average
  5. Edge cases         — empty DataFrame, single-row DataFrame
  6. Moving average     — correct mean/std regardless of model state
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import numpy as np
import pandas as pd
import pytest

# ── path setup ────────────────────────────────────────────────────────────────
# Add services/forecasting/ (for forecaster.py) and services/ (for shared/)
_REPO        = pathlib.Path(__file__).resolve().parents[2]
_FORECASTING = _REPO / "services" / "forecasting"
_SERVICES    = _REPO / "services"

for _p in (_FORECASTING, _SERVICES):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from forecaster import ProphetForecaster  # noqa: E402
from shared.contracts import ForecastResult  # noqa: E402

# ── helpers ───────────────────────────────────────────────────────────────────

def _make_df(n: int = 60, rps_mean: float = 50.0, seed: int = 42) -> pd.DataFrame:
    """Synthetic (ds, y) DataFrame at 5-minute intervals."""
    rng = np.random.default_rng(seed)
    times = pd.date_range("2024-01-01", periods=n, freq="5min")
    values = rps_mean + rng.normal(0, 5, size=n)
    values = np.clip(values, 0, None)
    return pd.DataFrame({"ds": times, "y": values})


def _loaded_forecaster() -> ProphetForecaster:
    """Return a forecaster with the pre-trained ARIMA model loaded."""
    fc = ProphetForecaster()
    fc.load_pretrained()
    return fc


# ── 1. Model loading ──────────────────────────────────────────────────────────

class TestModelLoading:

    def test_load_pretrained_returns_true(self):
        fc = ProphetForecaster()
        result = fc.load_pretrained()
        assert result is True, "Expected at least one model artifact to be found"

    def test_active_model_is_arima_or_arimax(self):
        fc = _loaded_forecaster()
        assert fc._active in ("arima", "arimax"), (
            f"Expected active model to be arima or arimax, got '{fc._active}'"
        )

    def test_arima_result_is_loaded(self):
        fc = _loaded_forecaster()
        # ARIMA result must be present either as primary or silent backup
        assert fc._arima_result is not None, "ARIMA result object not loaded"

    def test_arima_order_is_tuple_of_three(self):
        fc = _loaded_forecaster()
        order = fc._arima_order
        assert isinstance(order, tuple) and len(order) == 3, (
            f"Expected ARIMA order to be a 3-tuple, got {order}"
        )

    def test_freq_is_set(self):
        fc = _loaded_forecaster()
        assert fc._freq in ("1min", "5min", "10min", "15min", "30min", "60min"), (
            f"Unexpected freq value: {fc._freq}"
        )


# ── 2. Predict output shape and validity ─────────────────────────────────────

class TestPredictOutput:

    def setup_method(self):
        self.fc = _loaded_forecaster()
        self.df = _make_df(n=60)

    def test_predict_returns_forecast_result(self):
        result = self.fc.predict(self.df)
        assert isinstance(result, ForecastResult)

    def test_predicted_rps_is_non_negative(self):
        result = self.fc.predict(self.df)
        assert result.predicted_rps >= 0.0, (
            f"predicted_rps must be >= 0, got {result.predicted_rps}"
        )

    def test_confidence_interval_is_ordered(self):
        result = self.fc.predict(self.df)
        assert result.confidence_lower <= result.predicted_rps <= result.confidence_upper, (
            f"CI not ordered: [{result.confidence_lower}, {result.predicted_rps}, "
            f"{result.confidence_upper}]"
        )

    def test_confidence_bounds_are_non_negative(self):
        result = self.fc.predict(self.df)
        assert result.confidence_lower >= 0.0
        assert result.confidence_upper >= 0.0

    def test_horizon_minutes_is_set(self):
        result = self.fc.predict(self.df)
        assert result.horizon_minutes == 5

    def test_model_id_contains_arima(self):
        result = self.fc.predict(self.df)
        assert "arima" in result.model_id.lower(), (
            f"Expected model_id to contain 'arima', got '{result.model_id}'"
        )

    def test_predict_is_in_plausible_range(self):
        result = self.fc.predict(self.df)
        # Prediction should be within 10x of the input mean — catches wild extrapolations
        mean_y = float(self.df["y"].mean())
        assert result.predicted_rps < mean_y * 10, (
            f"Prediction {result.predicted_rps:.2f} seems unreasonably large "
            f"(input mean={mean_y:.2f})"
        )


# ── 3. Fallback chain ─────────────────────────────────────────────────────────

class TestFallbackChain:

    def test_arimax_falls_back_to_arima_when_exog_missing(self):
        fc = _loaded_forecaster()
        if fc._active != "arimax":
            pytest.skip("ARIMAX model not present — skipping fallback test")
        df = _make_df(n=60)
        # Pass None exog — should silently fall through to ARIMA
        result = fc.predict(df, df_exog=None)
        assert isinstance(result, ForecastResult)
        assert result.predicted_rps >= 0.0

    def test_no_model_falls_back_to_moving_average(self):
        fc = ProphetForecaster()
        # Deliberately do NOT load any model
        df = _make_df(n=60)
        result = fc.predict(df)
        assert result.model_id == "moving_average"
        assert result.predicted_rps >= 0.0

    def test_empty_dataframe_returns_moving_average(self):
        fc = _loaded_forecaster()
        empty = pd.DataFrame(columns=["ds", "y"])
        result = fc.predict(empty)
        # ARIMA/ARIMAX cannot forecast from empty data — must fall to MA
        assert isinstance(result, ForecastResult)
        assert result.predicted_rps >= 0.0


# ── 4. Moving average baseline ────────────────────────────────────────────────

class TestMovingAverage:

    def setup_method(self):
        self.fc = ProphetForecaster()

    def test_moving_average_empty_df_returns_zero(self):
        empty = pd.DataFrame(columns=["ds", "y"])
        result = self.fc.moving_average_predict(empty)
        assert result.predicted_rps == 0.0
        assert result.model_id == "moving_average"

    def test_moving_average_matches_window_mean(self):
        df = _make_df(n=20, rps_mean=100.0, seed=0)
        result = self.fc.moving_average_predict(df)
        expected = float(df["y"].tail(12).mean())
        assert abs(result.predicted_rps - expected) < 1e-6

    def test_moving_average_single_row(self):
        df = pd.DataFrame({"ds": [pd.Timestamp("2024-01-01")], "y": [42.0]})
        result = self.fc.moving_average_predict(df)
        assert result.predicted_rps == pytest.approx(42.0)
        assert result.confidence_lower == pytest.approx(42.0)
        assert result.confidence_upper == pytest.approx(42.0)

    def test_moving_average_ci_is_ordered(self):
        df = _make_df(n=30)
        result = self.fc.moving_average_predict(df)
        assert result.confidence_lower <= result.predicted_rps <= result.confidence_upper


# ── 5. Edge cases ─────────────────────────────────────────────────────────────

class TestEdgeCases:

    def setup_method(self):
        self.fc = _loaded_forecaster()

    def test_single_row_dataframe_does_not_crash(self):
        df = pd.DataFrame({
            "ds": [pd.Timestamp("2024-01-01 00:00:00")],
            "y":  [25.0],
        })
        result = self.fc.predict(df)
        assert isinstance(result, ForecastResult)

    def test_constant_series_does_not_crash(self):
        df = _make_df(n=60)
        df["y"] = 50.0  # perfectly flat signal
        result = self.fc.predict(df)
        assert isinstance(result, ForecastResult)
        assert result.predicted_rps >= 0.0

    def test_high_variance_series_stays_non_negative(self):
        rng = np.random.default_rng(99)
        times = pd.date_range("2024-01-01", periods=60, freq="5min")
        values = np.abs(rng.normal(0, 500, size=60))  # very noisy
        df = pd.DataFrame({"ds": times, "y": values})
        result = self.fc.predict(df)
        assert result.predicted_rps >= 0.0
        assert result.confidence_lower >= 0.0
