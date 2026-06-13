"""
tests/unit/anomaly-detector/test_isolation_forest_engine.py
────────────────────────────────────────────────────────────
Unit tests for the Isolation Forest engine's non-finite feature guard
(services/anomaly-detector/engines/isolation_forest/engine.py).

The engine is constructed via __new__ with a stubbed scaler/model so the
tests are hermetic — no .pkl, no trained sklearn model, no DB. joblib is
required only because engine.py imports it at module load; the suite
skips cleanly where joblib is absent (the bare CI unit-tests runner
until it installs the anomaly-detector deps).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("joblib")  # engine.py imports joblib at module load

_SERVICE = Path(__file__).resolve().parents[2].parent / "services" / "anomaly-detector"
if str(_SERVICE) not in sys.path:
    sys.path.insert(0, str(_SERVICE))

from engine_base import AnomalyScore, BackendFeatures        # noqa: E402
from engines.isolation_forest.engine import IsolationForestEngine  # noqa: E402


def _engine(decision_value: float = 0.0):
    """Build an engine without loading a .pkl. The scaler is identity and the
    model returns a fixed decision_function value so each branch is drivable."""
    eng = IsolationForestEngine.__new__(IsolationForestEngine)
    eng.min_sample_count = 10
    eng.healthy_above = 0.05
    eng.unhealthy_below = -0.05
    eng.unhealthy_score_scale = 0.5

    class _Scaler:
        def transform(self, x):
            return x

    class _Model:
        def decision_function(self, x):
            return [decision_value]

    eng.production_scaler = _Scaler()
    eng.model = _Model()
    return eng


def _features(**overrides):
    base = dict(
        backend_id="b1:8080",
        latency_ms=50.0,
        latency_rolling_mean_ms=48.0,
        error_rate=0.0,
        sample_count=50,
        latency_rolling_std_ms=5.0,
    )
    base.update(overrides)
    return BackendFeatures(**base)


# ── non-finite guard ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_feature_scores_healthy(bad):
    """NaN/±inf in any feature → 'healthy' (insufficient data), model never
    consulted. Without the guard, decision_function(NaN) returns NaN, which
    falls through to 'unhealthy' score 1.0 — a spurious exclusion."""
    eng = _engine(decision_value=-10.0)  # model would say 'unhealthy' if reached
    score = eng.score(_features(latency_ms=bad))
    assert isinstance(score, AnomalyScore)
    assert score.status == "healthy"
    assert score.score == 0.0


def test_none_feature_scores_healthy():
    """A NULL aggregate surfacing as None must not reach the scaler either."""
    eng = _engine(decision_value=-10.0)
    score = eng.score(_features(error_rate=None))
    assert score.status == "healthy"


def test_finite_features_still_reach_the_model_unhealthy():
    """Guard must not disturb the normal path: a finite vector whose model
    score is below unhealthy_below is still classified unhealthy."""
    eng = _engine(decision_value=-1.0)  # < unhealthy_below
    score = eng.score(_features())
    assert score.status == "unhealthy"


def test_finite_features_still_reach_the_model_healthy():
    eng = _engine(decision_value=1.0)   # > healthy_above
    score = eng.score(_features())
    assert score.status == "healthy"


def test_low_sample_count_short_circuits_before_guard():
    """The existing sample-count gate still fires first, even on a NaN vector."""
    eng = _engine(decision_value=-10.0)
    score = eng.score(_features(sample_count=1, latency_ms=float("nan")))
    assert score.status == "healthy"
