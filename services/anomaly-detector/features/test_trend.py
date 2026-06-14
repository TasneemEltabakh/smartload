"""Tests for the stateful trend-feature extractor (features/trend.py)."""

from __future__ import annotations

import sys
from pathlib import Path

_SVC = Path(__file__).resolve().parents[1]
if str(_SVC) not in sys.path:
    sys.path.insert(0, str(_SVC))

from features.trend import (  # noqa: E402
    ENRICHED_FEATURE_ORDER,
    TREND_FEATURE_ORDER,
    TrendExtractor,
)


def _steady(ext, mean=20.0, n=20):
    last = None
    for _ in range(n):
        last = ext.update("b", mean * 1.8, mean, 0.002, mean * 0.2)
    return last


def test_feature_order_shapes():
    assert TREND_FEATURE_ORDER == ("mean_dev", "max_dev", "cusum_pos", "slope",
                                   "max_ratio", "std_ratio")
    assert ENRICHED_FEATURE_ORDER[:4] == (
        "latency_ms", "latency_rolling_mean_ms", "error_rate", "latency_rolling_std_ms")
    assert len(ENRICHED_FEATURE_ORDER) == 10


def test_warmup_suppresses_trend_signals():
    ext = TrendExtractor()
    tf = ext.update("b", 36.0, 20.0, 0.002, 4.0)
    assert tf.warming_up is True
    assert tf.mean_dev == 0.0 and tf.cusum_pos == 0.0 and tf.slope == 0.0


def test_steady_stream_has_near_zero_drift():
    ext = TrendExtractor()
    tf = _steady(ext, 20.0, 40)
    assert not tf.warming_up
    assert abs(tf.mean_dev) < 0.05
    assert tf.cusum_pos < 1.0


def test_gradual_drift_builds_cusum_and_positive_slope():
    ext = TrendExtractor()
    _steady(ext, 20.0, 20)
    mean = 20.0
    last = None
    for _ in range(30):
        mean *= 1.03
        last = ext.update("b", mean * 1.8, mean, 0.002, mean * 0.2)
    assert last.cusum_pos > 3.0
    assert last.slope > 0.0
    assert last.mean_dev > 0.1


def test_baseline_resists_drift_then_recovers():
    """The guarded baseline does not chase a ramp (so mean_dev grows), and the
    CUSUM drains quickly once the level returns to normal."""
    ext = TrendExtractor()
    _steady(ext, 20.0, 20)
    mean = 20.0
    for _ in range(25):
        mean *= 1.04
        ext.update("b", mean * 1.8, mean, 0.002, mean * 0.2)
    peak = ext.update("b", mean * 1.8, mean, 0.002, mean * 0.2)
    assert peak.cusum_pos > 5.0
    # Return to baseline; CUSUM should drain within a few in-control windows.
    for _ in range(6):
        last = ext.update("b", 36.0, 20.0, 0.002, 4.0)
    assert last.cusum_pos < 3.0


def test_reset_clears_per_backend_state():
    ext = TrendExtractor()
    _steady(ext, 20.0, 25)
    ext.update("b", 400.0, 200.0, 0.002, 60.0)
    ext.reset()
    tf = ext.update("b", 36.0, 20.0, 0.002, 4.0)
    assert tf.warming_up is True  # fresh backend again


def test_per_backend_isolation():
    ext = TrendExtractor()
    _steady(ext, 20.0, 20)
    # A second backend starts cold and is unaffected by the first's history.
    tf = ext.update("other", 36.0, 20.0, 0.002, 4.0)
    assert tf.warming_up is True
