"""Tests for the rule-based trend_rule anomaly engine."""

from __future__ import annotations

import sys
from pathlib import Path

_SVC = Path(__file__).resolve().parents[2]
if str(_SVC) not in sys.path:
    sys.path.insert(0, str(_SVC))

from engine_base import BackendFeatures, select_engine  # noqa: E402
from engines.trend_rule.engine import TrendRuleEngine  # noqa: E402


def _feat(mean, mx=None, err=0.002, std=None, samples=300):
    mx = mx if mx is not None else mean * 1.8
    std = std if std is not None else mean * 0.2
    return BackendFeatures("b", mx, mean, err, samples, std)


def _warm(engine, mean=20.0, n=20):
    """Feed n steady healthy windows so the baseline + warmup settle."""
    for _ in range(n):
        engine.score(_feat(mean))


def test_selectable_via_factory():
    eng = select_engine("trend_rule", error_rate_threshold=0.05, min_sample_count=10)
    assert isinstance(eng, TrendRuleEngine)


def test_low_sample_is_healthy():
    eng = TrendRuleEngine()
    assert eng.score(_feat(20.0, samples=5)).status == "healthy"
    assert eng.last_anomaly_value() == 0.0


def test_steady_healthy_stream_stays_healthy():
    eng = TrendRuleEngine()
    statuses = [eng.score(_feat(20.0)).status for _ in range(60)]
    assert set(statuses) == {"healthy"}


def test_error_burst_is_unhealthy_without_warmup():
    """The error channel needs no history: a high error_rate trips immediately."""
    eng = TrendRuleEngine()
    sc = eng.score(_feat(20.0, err=0.20))
    assert sc.status == "unhealthy"
    assert sc.metric == "error_rate"


def test_gradual_drift_eventually_trips_via_cusum():
    eng = TrendRuleEngine()
    _warm(eng, 20.0, 20)
    tripped = False
    mean = 20.0
    for _ in range(40):
        mean *= 1.03  # ~3% per step upward drift
        if eng.score(_feat(mean)).status != "healthy":
            tripped = True
            break
    assert tripped, "a sustained upward latency drift must eventually trip the drift channel"


def test_latency_spike_trips_immediately_after_warmup():
    eng = TrendRuleEngine()
    _warm(eng, 20.0, 20)
    # A window whose MAX jumps far above the established baseline MAX.
    sc = eng.score(_feat(20.0, mx=200.0, std=60.0))
    assert sc.status != "healthy"
    assert sc.metric in ("latency_max_dev", "latency_mean_dev", "latency_cusum")


def test_recovery_returns_to_healthy_after_anomaly_clears():
    """Once the latency falls back to baseline, the engine clears quickly: the
    falling-latency suppressor plus the CUSUM fast-drain bring it back to
    healthy within a few in-control windows rather than holding a long tail."""
    eng = TrendRuleEngine()
    _warm(eng, 20.0, 20)
    # ramp up until it trips
    mean = 20.0
    for _ in range(20):
        mean *= 1.06
        eng.score(_feat(mean))
    assert eng.score(_feat(mean)).status != "healthy"
    # latency returns to baseline and holds — must recover to healthy
    statuses = [eng.score(_feat(20.0)).status for _ in range(12)]
    assert statuses[-1] == "healthy", f"did not recover: tail={statuses}"


def test_reset_clears_state():
    eng = TrendRuleEngine()
    _warm(eng, 20.0, 20)
    for mean in (40, 80, 160):
        eng.score(_feat(float(mean)))
    eng.reset()
    # After reset the first window re-seeds the baseline -> healthy. The
    # continuous value reflects only the (tiny) baseline error ratio, not any
    # latency signal, since all temporal state was dropped.
    assert eng.score(_feat(20.0)).status == "healthy"
    assert eng.last_anomaly_value() < 0.1


def test_last_anomaly_value_in_unit_range():
    eng = TrendRuleEngine()
    _warm(eng, 20.0, 20)
    eng.score(_feat(20.0, mx=300.0, std=90.0))
    v = eng.last_anomaly_value()
    assert 0.0 <= v <= 1.0
