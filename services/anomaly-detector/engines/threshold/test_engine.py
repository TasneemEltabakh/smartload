"""Unit tests for ThresholdEngine."""

import sys
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from engine_base import BackendFeatures  # noqa: E402
from engines.threshold.engine import ThresholdEngine  # noqa: E402


def _features(latency=10.0, rolling_mean=10.0, error_rate=0.0, samples=100):
    return BackendFeatures(
        backend_id="b1",
        latency_ms=latency,
        latency_rolling_mean_ms=rolling_mean,
        error_rate=error_rate,
        sample_count=samples,
    )


def test_healthy_when_within_thresholds():
    engine = ThresholdEngine()
    score = engine.score(_features(latency=12.0, rolling_mean=10.0))
    assert score.status == "healthy"


def test_degraded_when_latency_spikes():
    engine = ThresholdEngine(latency_multiplier=3.0)
    score = engine.score(_features(latency=100.0, rolling_mean=10.0))
    assert score.status == "degraded"


def test_unhealthy_when_error_rate_exceeds_threshold():
    engine = ThresholdEngine(error_rate_threshold=0.05)
    score = engine.score(_features(error_rate=0.20))
    assert score.status == "unhealthy"


def test_healthy_when_sample_count_too_low():
    engine = ThresholdEngine(min_sample_count=10)
    score = engine.score(_features(latency=1000.0, rolling_mean=10.0, samples=2))
    assert score.status == "healthy"


def test_healthy_when_rolling_mean_zero():
    engine = ThresholdEngine()
    score = engine.score(_features(latency=10.0, rolling_mean=0.0))
    assert score.status == "healthy"
