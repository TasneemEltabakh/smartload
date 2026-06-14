"""Unit tests for TrendForestEngine.

The bundle-shape and threshold tests use a tiny model fitted inline (no
dependency on the real trained artifact) so they run without the training
pipeline. The behavioural tests (healthy stream stays healthy, a ramp
eventually trips, reset clears state) use the REAL shipped bundle when present,
since they exercise the temporal extractor end-to-end against the calibrated
thresholds.
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
from features.trend import ENRICHED_FEATURE_ORDER  # noqa: E402
from engines.trend_forest.engine import (  # noqa: E402
    DEFAULT_MODEL_PATH,
    FEATURE_ORDER,
    TrendForestEngine,
)

_N_FEATURES = len(ENRICHED_FEATURE_ORDER)


def _features(latency=20.0, rolling_mean=20.0, error_rate=0.005, samples=300, std=4.0,
              backend_id="b1"):
    return BackendFeatures(
        backend_id=backend_id,
        latency_ms=latency,
        latency_rolling_mean_ms=rolling_mean,
        error_rate=error_rate,
        sample_count=samples,
        latency_rolling_std_ms=std,
    )


def _dump_bundle(path, **thresholds):
    """A tiny self-consistent bundle in the enriched feature space."""
    rng = np.random.RandomState(42)
    # Rough scales: ms latencies, [0,1] error_rate, small dimensionless trends.
    scales = np.array([200.0, 200.0, 1.0, 50.0, 0.5, 0.5, 5.0, 0.1, 5.0, 1.0])
    X = rng.rand(300, _N_FEATURES) * scales
    scaler = StandardScaler().fit(X)
    model = IsolationForest(n_estimators=50, contamination=0.05, random_state=42).fit(
        scaler.transform(X)
    )
    th = {"healthy_above": 0.05, "unhealthy_below": -0.05, "unhealthy_score_scale": 0.5}
    th.update(thresholds)
    bundle = {
        "model": model,
        "smd_scaler": scaler,
        "production_scaler": scaler,
        "feature_order": list(ENRICHED_FEATURE_ORDER),
        "thresholds": th,
        "metadata": {"pipeline": "trend_enriched_quantile_calibrated"},
    }
    joblib.dump(bundle, path)
    return bundle


@pytest.fixture
def tiny_bundle(tmp_path):
    path = tmp_path / "trend_forest.pkl"
    _dump_bundle(path)
    return path


# ── bundle loading / schema ──────────────────────────────────────────────────

def test_feature_order_matches_shared_constant():
    assert FEATURE_ORDER == ENRICHED_FEATURE_ORDER
    assert FEATURE_ORDER == (
        "latency_ms", "latency_rolling_mean_ms", "error_rate", "latency_rolling_std_ms",
        "mean_dev", "max_dev", "cusum_pos", "slope", "max_ratio", "std_ratio",
    )


def test_bundle_loads(tiny_bundle):
    eng = TrendForestEngine(model_path=tiny_bundle)
    assert eng.model is not None
    assert eng.production_scaler is not None
    assert eng.healthy_above > eng.unhealthy_below


def test_real_bundle_loads_if_present():
    if not Path(DEFAULT_MODEL_PATH).exists():
        pytest.skip("real trend_forest.pkl not built")
    eng = TrendForestEngine()
    assert eng.model is not None


def test_rejects_non_dict_bundle(tmp_path):
    path = tmp_path / "bad.pkl"
    joblib.dump(["not", "a", "dict"], path)
    with pytest.raises(ValueError):
        TrendForestEngine(model_path=path)


def test_rejects_wrong_feature_order(tmp_path):
    path = tmp_path / "wrong.pkl"
    bundle = _dump_bundle(path)
    # Re-dump with the point-feature order only -> must be rejected.
    bundle["feature_order"] = ["latency_ms", "latency_rolling_mean_ms", "error_rate", "latency_rolling_std_ms"]
    joblib.dump(bundle, path)
    with pytest.raises(ValueError):
        TrendForestEngine(model_path=path)


def test_select_engine_kwargs_parity(tiny_bundle):
    # Same kwargs select_engine passes to the point-feature engines must work.
    eng = TrendForestEngine(
        model_path=tiny_bundle,
        latency_multiplier=3.0,
        error_rate_threshold=0.05,
        min_sample_count=10,
    )
    assert eng.min_sample_count == 10


# ── gates: low sample / non-finite ───────────────────────────────────────────

def test_low_sample_is_healthy(tiny_bundle):
    eng = TrendForestEngine(model_path=tiny_bundle, min_sample_count=10)
    score = eng.score(_features(samples=3))
    assert score.status == "healthy"
    assert score.score == 0.0
    assert eng.last_anomaly_value() == 0.0


def test_non_finite_is_healthy(tiny_bundle):
    eng = TrendForestEngine(model_path=tiny_bundle)
    score = eng.score(_features(latency=float("nan")))
    assert score.status == "healthy"
    assert score.score == 0.0
    assert eng.last_anomaly_value() == 0.0


def test_state_advances_once_per_cycle(tiny_bundle):
    # A low-sample window must still advance the extractor exactly once (so the
    # baseline tracks wall-clock cycles) — check n_seen grows per call.
    eng = TrendForestEngine(model_path=tiny_bundle, min_sample_count=10)
    eng.score(_features(samples=3))
    eng.score(_features(samples=3))
    st = eng._extractor._states["b1"]
    assert st.n_seen == 2


# ── behaviour against the real calibrated bundle ─────────────────────────────

def _real_engine_or_skip():
    if not Path(DEFAULT_MODEL_PATH).exists():
        pytest.skip("real trend_forest.pkl not built")
    return TrendForestEngine()


def test_steady_healthy_stream_scores_healthy():
    eng = _real_engine_or_skip()
    eng.reset()
    rng = np.random.default_rng(12345)
    statuses = []
    for _ in range(60):
        # Steady healthy traffic with realistic within-window shape: aggregate a
        # window of lognormal per-request draws exactly as the live pipeline
        # does, so max/mean and std/mean match the distribution the model was
        # trained on (a flat max==mean stream is itself out-of-distribution).
        lat = 20.0 * rng.lognormal(0.0, 0.20, 300)
        mx, mean, std = float(lat.max()), float(lat.mean()), float(lat.std())
        s = eng.score(_features(latency=mx, rolling_mean=mean, error_rate=0.002, std=std))
        statuses.append(s.status)
    # After warmup, a steady clean stream is overwhelmingly healthy.
    post_warmup = statuses[15:]
    healthy_frac = sum(s == "healthy" for s in post_warmup) / len(post_warmup)
    assert healthy_frac >= 0.85, f"healthy_frac={healthy_frac} statuses={post_warmup}"


def test_gradual_ramp_eventually_non_healthy():
    eng = _real_engine_or_skip()
    eng.reset()
    rng = np.random.default_rng(7)
    base = 20.0
    statuses = []

    def _window(mean_level):
        lat = mean_level * rng.lognormal(0.0, 0.20, 300)
        return float(lat.max()), float(lat.mean()), float(lat.std())

    # 20 steady windows to establish a baseline, then a long upward ramp.
    for _ in range(20):
        mx, mean, std = _window(base)
        statuses.append(eng.score(_features(latency=mx, rolling_mean=mean,
                                            error_rate=0.002, std=std)).status)
    for i in range(1, 41):
        level = base * (1.0 + 0.10 * i)  # ramps to ~5x baseline
        mx, mean, std = _window(level)
        statuses.append(eng.score(_features(latency=mx, rolling_mean=mean,
                                            error_rate=0.002, std=std)).status)
    ramp_statuses = statuses[20:]
    assert any(s != "healthy" for s in ramp_statuses), f"ramp never tripped: {ramp_statuses}"


def test_reset_clears_state():
    eng = _real_engine_or_skip()
    eng.reset()
    # Drive a ramp to build up CUSUM / baseline state.
    base = 20.0
    for i in range(40):
        mean = base * (1.0 + 0.10 * i)
        eng.score(_features(latency=mean * 1.05, rolling_mean=mean, error_rate=0.002, std=0.2 * mean))
    assert eng._extractor._states  # state exists
    eng.reset()
    assert not eng._extractor._states  # extractor cleared
    assert eng.last_anomaly_value() == 0.0
    # After reset the first windows are warming up again -> healthy.
    s = eng.score(_features(latency=21.0, rolling_mean=20.0, error_rate=0.002, std=4.0))
    assert s.status == "healthy"
