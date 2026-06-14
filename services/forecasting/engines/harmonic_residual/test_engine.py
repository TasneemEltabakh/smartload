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


def test_trend_damping_leaves_single_step_unchanged():
    # The damping must be a no-op at one step (offset(1)==1 for any φ), so the
    # single-step fitness-function results are unaffected by the damping factor.
    n = 600
    rates = [50.0 + 0.2 * i for i in range(n)]
    hw = HistoryWindow(_iso(n), rates)
    a = HarmonicResidualEngine(trend_damping=0.9).forecast(hw)
    b = HarmonicResidualEngine(trend_damping=1.0).forecast(hw)
    assert a.predicted_rps == b.predicted_rps
    # forecast_ahead(1) is also identical to forecast() and to the undamped one.
    c = HarmonicResidualEngine(trend_damping=0.5).forecast_ahead(hw, 1)
    assert abs(c.predicted_rps - b.predicted_rps) < 1e-9


def test_significant_trend_projects_full_lead():
    # A clean, strongly-significant ramp keeps shrink ≈ 1, so the multi-step
    # projection leads the curve by ~steps·slope (the full linear projection).
    n = 600
    slope = 0.2
    rates = [50.0 + slope * i for i in range(n)]
    hw = HistoryWindow(_iso(n), rates)
    f = HarmonicResidualEngine().forecast_ahead(hw, 20)
    expected_full = rates[-1] + 20 * slope
    assert abs(f.predicted_rps - expected_full) < 1.0  # full lead, not shrunk


def test_significance_shrink_factor():
    # The shrink helper: a strongly-significant slope → ~1; a slope buried in
    # noise → well below 1 (so it is projected weakly over a multi-step lead).
    eng = HarmonicResidualEngine()
    n = 120
    t = np.arange(n, dtype=float)
    tcol = (t - t.mean()) / max(t.std(), 1.0)
    # Clean steep ramp: huge SNR.
    clean = 0.5 * t
    coef_clean = np.polyfit(tcol, clean, 1)[0]
    assert eng._trend_shrink(tcol, coef_clean, clean - np.polyval([coef_clean, clean.mean()], tcol)) > 0.95
    # Flat + noise: slope is noise, low SNR.
    rng = np.random.default_rng(3)
    noisy = 100.0 + rng.normal(0, 5, n)
    coef_noisy = np.polyfit(tcol, noisy, 1)[0]
    resid = noisy - (coef_noisy * tcol + noisy.mean())
    assert eng._trend_shrink(tcol, coef_noisy, resid) < 0.5


def test_insignificant_trend_reduces_projection_churn():
    # Walk a flat noisy series; the significance-shrunk multi-step projection
    # must vary LESS across consecutive origins than the full-linear (ρ=1) one —
    # i.e. it does not chase the noise slope over the lead, the property that
    # removes downstream scale churn on flat demand.
    rng = np.random.default_rng(11)
    n = 400
    rates = 100.0 + rng.normal(0, 5, n)
    ts = _iso(n, step_s=1)  # per-second → no daily season, pure level+noise
    shrunk_eng = HarmonicResidualEngine(trend_damping=0.8)
    full_eng = HarmonicResidualEngine(trend_damping=1.0)
    shrunk_sig, full_sig = [], []
    for t in range(120, n):
        lo = t - 60
        hw = HistoryWindow(ts[lo:t], rates[lo:t].tolist())
        shrunk_sig.append(shrunk_eng.forecast_ahead(hw, 20).predicted_rps)
        full_sig.append(full_eng.forecast_ahead(hw, 20).predicted_rps)
    assert np.std(shrunk_sig) < np.std(full_sig)


def test_infers_one_minute_cadence_period():
    # At 1-min cadence the inferred daily period is 1440; with <2 cycles of data
    # the engine still produces a sane, finite forecast (seasonal basis dropped).
    engine = HarmonicResidualEngine()
    n = 500
    rates = [40.0 + 0.01 * i for i in range(n)]
    f = engine.forecast(HistoryWindow(_iso(n, step_s=60), rates))
    assert np.isfinite(f.predicted_rps)
    assert f.predicted_rps > 0
