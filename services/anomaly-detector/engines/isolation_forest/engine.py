"""Isolation Forest anomaly engine. Trained model, see ../../tools/anomaly-training/."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import joblib

# Make the service root importable so we can find engine_base.
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from engine_base import AnomalyEngine, AnomalyScore, BackendFeatures  # noqa: E402

DEFAULT_MODEL_PATH = _SERVICE_ROOT / "models" / "isolation_forest.pkl"

# Order MUST match the feature columns the model was trained on
# (tools/anomaly-training/train_smd.py).
FEATURE_ORDER = (
    "latency_ms",
    "latency_rolling_mean_ms",
    "error_rate",
    "latency_rolling_std_ms",
)


class IsolationForestEngine(AnomalyEngine):
    """Scores backends with a trained sklearn IsolationForest.

    The .pkl is a bundle dict produced by tools/anomaly-training/train_smd.py:
    {"model", "smd_scaler", "production_scaler", "feature_order",
    "thresholds", "metadata"}. The model is trained on the Server Machine
    Dataset (SMD), which is normalized to [0, 1] per machine -- a different
    scale to production's real-millisecond latency / [0,1] error_rate.
    "production_scaler" (a StandardScaler fit on MST-2021-derived features,
    the closest available proxy for live ANOMALY_QUERY data) is applied to
    BackendFeatures before decision_function() so the model sees inputs in
    roughly the distribution it was calibrated against.

    Loads the bundle eagerly on init; raises if the file is missing, fails
    to unpickle, or doesn't match the expected shape, so bootstrap_engine()
    can fall back to ThresholdEngine.
    """

    def __init__(
        self,
        model_path: str | Path | None = None,
        latency_multiplier: float = 3.0,
        error_rate_threshold: float = 0.05,
        min_sample_count: int = 10,
    ):
        # latency_multiplier and error_rate_threshold are accepted for
        # select_engine(**policy.engine_kwargs()) compatibility with the
        # threshold engine but are unused at inference here — the model's
        # decision boundaries are the bundle's `thresholds.healthy_above`
        # and `unhealthy_below`, baked in at training time. A policy.yaml
        # change to either field will be silently ignored by this engine;
        # retuning means re-running tools/anomaly-training/train_smd.py.
        # min_sample_count remains a runtime knob (data-quality gate).
        self.latency_multiplier = latency_multiplier
        self.error_rate_threshold = error_rate_threshold
        self.min_sample_count = min_sample_count
        path = Path(model_path) if model_path is not None else DEFAULT_MODEL_PATH
        bundle = joblib.load(path)

        if not isinstance(bundle, dict) or "model" not in bundle or "production_scaler" not in bundle:
            raise ValueError(
                f"isolation_forest.pkl at {path}: expected a bundle dict with "
                f"'model'/'production_scaler' keys, got {type(bundle)}"
            )
        feature_order = tuple(bundle.get("feature_order", ()))
        if feature_order != FEATURE_ORDER:
            raise ValueError(
                f"isolation_forest.pkl feature_order {feature_order} != engine FEATURE_ORDER {FEATURE_ORDER}"
            )

        self.model = bundle["model"]
        self.production_scaler = bundle["production_scaler"]
        thresholds = bundle.get("thresholds", {})
        self.healthy_above = float(thresholds.get("healthy_above", 0.05))
        self.unhealthy_below = float(thresholds.get("unhealthy_below", -0.05))
        self.unhealthy_score_scale = float(thresholds.get("unhealthy_score_scale", 0.5))

    def score(self, features: BackendFeatures) -> AnomalyScore:
        if features.sample_count < self.min_sample_count:
            return AnomalyScore(features.backend_id, "healthy", 0.0)

        vector = [getattr(features, name) for name in FEATURE_ORDER]
        # A DB hiccup (NULL/NaN aggregates, an empty rolling window) can surface
        # non-finite or non-numeric features. StandardScaler propagates NaN/inf
        # and decision_function() then returns NaN, which fails BOTH comparisons
        # below and falls through to "unhealthy" with score 1.0 — a spurious
        # exclusion driven by bad data, not a sick backend (one the lb-sidecar
        # quorum guard would then have to absorb). Treat a non-finite vector as
        # insufficient data, exactly like the sample-count gate above.
        try:
            if not all(math.isfinite(v) for v in vector):
                return AnomalyScore(features.backend_id, "healthy", 0.0)
        except TypeError:
            return AnomalyScore(features.backend_id, "healthy", 0.0)

        x = self.production_scaler.transform([vector])
        raw = float(self.model.decision_function(x)[0])

        if raw > self.healthy_above:
            return AnomalyScore(features.backend_id, "healthy", 0.0)
        if raw >= self.unhealthy_below:
            return AnomalyScore(features.backend_id, "degraded", 0.5)
        return AnomalyScore(
            features.backend_id,
            "unhealthy",
            min(1.0, abs(raw - self.unhealthy_below) / self.unhealthy_score_scale),
        )

    def reload(self) -> None:
        """No-op: the model is immutable for the lifetime of the process."""
