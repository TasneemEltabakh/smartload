"""Unit tests for IsolationForestEngine.

Uses a tiny model fitted inline (no dependency on the real trained
artifact) so these tests run without the dataset or training pipeline.
"""

import sys
from pathlib import Path

import joblib
import numpy as np
import pytest
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from engine_base import BackendFeatures  # noqa: E402
from engines.isolation_forest.engine import FEATURE_ORDER, IsolationForestEngine  # noqa: E402


def _features(latency=20.0, rolling_mean=20.0, error_rate=0.01, samples=100, std=5.0):
    return BackendFeatures(
        backend_id="b1",
        latency_ms=latency,
        latency_rolling_mean_ms=rolling_mean,
        error_rate=error_rate,
        sample_count=samples,
        latency_rolling_std_ms=std,
    )


def _dump_bundle(path, **overrides):
    rng = np.random.RandomState(42)
    # Roughly mimic real feature scales: latency/mean/std in ms, error_rate in [0,1).
    X = rng.rand(200, 4) * np.array([200.0, 200.0, 1.0, 50.0])
    scaler = StandardScaler().fit(X)
    Xs = scaler.transform(X)
    model = IsolationForest(n_estimators=100, random_state=42).fit(Xs)
    scores = model.decision_function(Xs)
    bundle = {
        "model": model,
        "smd_scaler": scaler,
        "production_scaler": scaler,
        "feature_order": list(FEATURE_ORDER),
        "thresholds": {
            "healthy_above": float(np.percentile(scores, 60)),
            "unhealthy_below": float(np.percentile(scores, 10)),
            "unhealthy_score_scale": float(np.percentile(scores, 10) - scores.min()) or 0.5,
        },
        "metadata": {"test_fixture": True},
    }
    bundle.update(overrides)
    joblib.dump(bundle, path)
    return str(path)


@pytest.fixture
def tiny_model_path(tmp_path):
    return _dump_bundle(tmp_path / "isolation_forest.pkl")


def test_normal_features_are_not_unhealthy(tiny_model_path):
    engine = IsolationForestEngine(model_path=tiny_model_path)
    # A point near the centre of the training distribution.
    score = engine.score(_features(latency=100.0, rolling_mean=100.0, error_rate=0.5, std=25.0))
    assert score.status in ("healthy", "degraded")


def test_extreme_outlier_is_unhealthy(tiny_model_path):
    engine = IsolationForestEngine(model_path=tiny_model_path)
    # Wildly outside the training range on every dimension.
    score = engine.score(_features(latency=50_000.0, rolling_mean=50_000.0, error_rate=1.0, std=20_000.0))
    assert score.status == "unhealthy"
    assert 0.0 < score.score <= 1.0


def test_sample_count_gate_returns_healthy(tiny_model_path):
    engine = IsolationForestEngine(model_path=tiny_model_path, min_sample_count=10)
    score = engine.score(_features(latency=50_000.0, rolling_mean=50_000.0, error_rate=1.0, samples=2))
    assert score.status == "healthy"
    assert score.score == 0.0


def test_missing_model_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        IsolationForestEngine(model_path=str(tmp_path / "does_not_exist.pkl"))


def test_score_always_in_unit_range(tiny_model_path):
    engine = IsolationForestEngine(model_path=tiny_model_path)
    for latency in (0.0, 50.0, 500.0, 50_000.0):
        score = engine.score(_features(latency=latency, rolling_mean=50.0, std=10.0))
        assert 0.0 <= score.score <= 1.0


def test_status_is_always_a_valid_label(tiny_model_path):
    engine = IsolationForestEngine(model_path=tiny_model_path)
    for latency in (0.0, 50.0, 500.0, 50_000.0):
        score = engine.score(_features(latency=latency, rolling_mean=50.0, std=10.0))
        assert score.status in ("healthy", "degraded", "unhealthy")


def test_engine_accepts_policy_kwargs(tiny_model_path):
    engine = IsolationForestEngine(
        model_path=tiny_model_path,
        latency_multiplier=4.0,
        error_rate_threshold=0.1,
        min_sample_count=20,
    )
    assert engine.latency_multiplier == 4.0
    assert engine.error_rate_threshold == 0.1
    assert engine.min_sample_count == 20


def test_reload_is_a_noop(tiny_model_path):
    engine = IsolationForestEngine(model_path=tiny_model_path)
    engine.reload()  # must not raise


def test_malformed_bundle_raises(tmp_path):
    # Old format: a bare IsolationForest, not a bundle dict.
    rng = np.random.RandomState(42)
    X = rng.rand(200, 4)
    model = IsolationForest(n_estimators=10, random_state=42).fit(X)
    path = tmp_path / "isolation_forest.pkl"
    joblib.dump(model, path)

    with pytest.raises(ValueError):
        IsolationForestEngine(model_path=str(path))


def test_feature_order_mismatch_raises(tmp_path):
    path = _dump_bundle(tmp_path / "isolation_forest.pkl", feature_order=["wrong", "order", "here", "x"])

    with pytest.raises(ValueError):
        IsolationForestEngine(model_path=path)


def test_thresholds_default_when_absent(tmp_path):
    path = _dump_bundle(tmp_path / "isolation_forest.pkl", thresholds={})

    engine = IsolationForestEngine(model_path=path)
    score = engine.score(_features())
    assert score.status in ("healthy", "degraded", "unhealthy")
    assert 0.0 <= score.score <= 1.0
