"""
tests/integration/test_isolation_forest_artifact.py
────────────────────────────────────────────────────
Smoke tests against the real shipped `isolation_forest.pkl` artifact
(closes #103 acceptance gap for #101 / N2.1).

Why "integration" rather than "unit": the engine's own test_engine.py
suite uses a synthetic inline bundle (no dataset dependency), which is
the right shape for fast unit feedback. But that pattern silently masks
a critical risk: the deployed artifact at
`services/anomaly-detector/models/isolation_forest.pkl` was trained on
a specific scikit-learn version (1.3.2 per
tools/anomaly-training/requirements.txt). Joblib / pickle
deserialization is sensitive to sklearn's internal tree representation,
which has changed across recent versions — loading a 1.3.2-trained
IsolationForest under a newer sklearn can throw
InconsistentVersionWarning or, in some cases, fail outright.

These tests load the REAL artifact and assert it behaves sanely. If the
runtime requirements drift away from the training pin, this is the
test that catches it before the engine silently falls back to threshold
in production.

Runs without docker — no live stack needed. Just imports the engine
class and points it at the on-disk .pkl.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SVC = _REPO / "services" / "anomaly-detector"
_MODEL_PATH = _SVC / "models" / "isolation_forest.pkl"

# The engine module uses a sys.path hack to import engine_base from the
# service root; mirror it here so we can import IsolationForestEngine
# from outside the service.
for p in (str(_SVC), str(_SVC / "engines" / "isolation_forest")):
    if p not in sys.path:
        sys.path.insert(0, p)


pytestmark = pytest.mark.skipif(
    not _MODEL_PATH.exists(),
    reason=f"shipped model artifact not present at {_MODEL_PATH}",
)


def _engine():
    from engines.isolation_forest.engine import IsolationForestEngine  # noqa: PLC0415
    return IsolationForestEngine(model_path=str(_MODEL_PATH))


def _features(latency, rolling_mean, error_rate, std, samples=100):
    from engine_base import BackendFeatures  # noqa: PLC0415
    return BackendFeatures(
        backend_id="real-pkl-test",
        latency_ms=latency,
        latency_rolling_mean_ms=rolling_mean,
        error_rate=error_rate,
        sample_count=samples,
        latency_rolling_std_ms=std,
    )


def test_real_pkl_loads_without_version_mismatch():
    """The shipped artifact loads with the runtime sklearn version.

    This is the test that fires when scikit-learn in
    services/anomaly-detector/requirements.txt drifts away from the
    pin in tools/anomaly-training/requirements.txt. Joblib raises
    (or sklearn emits a load-time exception) when the tree structure
    can't be reconstructed."""
    engine = _engine()
    # Bundle keys the runtime engine actually reads.
    assert engine.model is not None, "model attribute not populated from bundle"
    assert engine.production_scaler is not None, "production_scaler missing"
    assert isinstance(engine.healthy_above, float)
    assert isinstance(engine.unhealthy_below, float)
    assert engine.unhealthy_score_scale > 0, (
        "unhealthy_score_scale must be positive (score normalisation denominator)"
    )


def test_real_pkl_score_returns_unit_range_for_typical_input():
    """A modest-traffic backend at typical SmartLoad-bench latencies
    must produce a score in [0, 1] and a valid status label."""
    engine = _engine()
    s = engine.score(_features(latency=120.0, rolling_mean=80.0, error_rate=0.01, std=20.0))
    assert s.status in ("healthy", "degraded", "unhealthy")
    assert 0.0 <= s.score <= 1.0, f"score out of range: {s.score}"


def test_real_pkl_classifies_extreme_outlier_as_unhealthy():
    """A wildly anomalous feature vector — 50 s latency, 100% error
    rate, huge variance — must be flagged unhealthy. If it isn't, the
    domain-adaptation between SMD training and the production_scaler
    has decalibrated past the point where the engine is useful."""
    engine = _engine()
    s = engine.score(_features(latency=50_000.0, rolling_mean=50_000.0, error_rate=1.0, std=20_000.0))
    assert s.status == "unhealthy", (
        f"extreme outlier should be unhealthy but got {s.status} (score={s.score}). "
        "Check the production_scaler calibration in train_smd.py."
    )
    assert 0.0 < s.score <= 1.0


def test_real_pkl_respects_sample_count_gate():
    """Below the data-quality gate, the engine returns healthy
    regardless of how anomalous the features look. This is the only
    runtime knob the operator can actually tune for this engine."""
    engine = _engine()
    s = engine.score(_features(
        latency=50_000.0, rolling_mean=50_000.0, error_rate=1.0, std=20_000.0,
        samples=2,
    ))
    assert s.status == "healthy"
    assert s.score == 0.0
