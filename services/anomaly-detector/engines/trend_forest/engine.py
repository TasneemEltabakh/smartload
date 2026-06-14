"""
services/anomaly-detector/engines/trend_forest/engine.py
─────────────────────────────────────────────────────────
Trained, *stateful* temporal anomaly engine.

This is the trained counterpart to the interpretable ``trend_rule`` engine: a
scikit-learn IsolationForest scored over the ENRICHED feature vector — the four
point features the run loop emits plus the six backend-relative temporal signals
derived by ``features/trend.py`` (mean_dev, max_dev, cusum_pos, slope, max_ratio,
std_ratio). Those extra signals carry the per-backend history a point-feature
model lacks, which is what lets the model see a slow latency ramp that every
stateless engine (threshold, z-score, the point-feature Isolation Forests) scores
at ~0 recall.

Statefulness, exactly like trend_rule:
  * The engine owns a single ``TrendExtractor`` (keyed internally by backend_id).
  * ``score`` calls ``extractor.update(...)`` exactly once per cycle — the single
    state-advancing call — then reads the derived features.
  * ``reset`` drops all per-backend state for an independent trace / a backend
    coming online fresh.

Verdict tiering mirrors IsolationForestEngine: decision_function -> raw; raw above
``healthy_above`` is healthy, in the (unhealthy_below, healthy_above] band is
degraded (score 0.5), below ``unhealthy_below`` is unhealthy with a saturating
score. The bundle is produced by tools/anomaly-training/train_trend.py and shares
the schema of the point-feature bundles.

Loads the bundle eagerly on init; raises ValueError if the file is missing,
unpickles wrong, or its feature_order does not match ENRICHED_FEATURE_ORDER — so
bootstrap can fall back to the rule engine.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import joblib

# Make the service root importable so we can find engine_base / features.
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from engine_base import AnomalyEngine, AnomalyScore, BackendFeatures  # noqa: E402
from features.trend import ENRICHED_FEATURE_ORDER, TrendConfig, TrendExtractor  # noqa: E402

DEFAULT_MODEL_PATH = _SERVICE_ROOT / "models" / "trend_forest.pkl"

# Order MUST match the columns the model was trained on
# (tools/anomaly-training/train_trend.py). Re-export the shared constant so the
# engine and the trainer share one source of truth.
FEATURE_ORDER = ENRICHED_FEATURE_ORDER

# Continuous severity reported for windows where the model is not consulted
# (low-sample, non-finite). Kept at the low (most-healthy) end so the PR-AUC
# curve treats these as non-anomalous, matching the trend_rule convention.
_SUPPRESSED_VALUE = 0.0


class TrendForestEngine(AnomalyEngine):
    """Stateful trained IsolationForest over enriched temporal features."""

    def __init__(
        self,
        model_path: str | Path | None = None,
        latency_multiplier: float = 3.0,
        error_rate_threshold: float = 0.05,
        min_sample_count: int = 10,
        trend_config: TrendConfig | None = None,
    ):
        # latency_multiplier / error_rate_threshold are accepted for
        # select_engine(**policy.engine_kwargs()) parity with the threshold and
        # isolation_forest engines but are unused at inference — the decision
        # boundaries are the bundle's calibrated thresholds, baked in at training
        # time. min_sample_count remains a runtime data-quality gate.
        self.latency_multiplier = latency_multiplier
        self.error_rate_threshold = error_rate_threshold
        self.min_sample_count = min_sample_count

        path = Path(model_path) if model_path is not None else DEFAULT_MODEL_PATH
        bundle = joblib.load(path)

        if not isinstance(bundle, dict) or "model" not in bundle or "production_scaler" not in bundle:
            raise ValueError(
                f"trend_forest.pkl at {path}: expected a bundle dict with "
                f"'model'/'production_scaler' keys, got {type(bundle)}"
            )
        feature_order = tuple(bundle.get("feature_order", ()))
        if feature_order != FEATURE_ORDER:
            raise ValueError(
                f"trend_forest.pkl feature_order {feature_order} != engine FEATURE_ORDER {FEATURE_ORDER}"
            )

        self.model = bundle["model"]
        self.production_scaler = bundle["production_scaler"]
        thresholds = bundle.get("thresholds", {})
        self.healthy_above = float(thresholds.get("healthy_above", 0.05))
        self.unhealthy_below = float(thresholds.get("unhealthy_below", -0.05))
        self.unhealthy_score_scale = float(thresholds.get("unhealthy_score_scale", 0.5))

        self._extractor = TrendExtractor(trend_config)
        self._last_anomaly_value = _SUPPRESSED_VALUE

    def reset(self) -> None:
        """Drop all per-backend temporal state (fresh start for a new trace)."""
        self._extractor.reset()
        self._last_anomaly_value = _SUPPRESSED_VALUE

    def last_anomaly_value(self) -> float:
        """Continuous anomaly severity for the window most recently passed to
        score(). Higher = more anomalous (negated decision_function). Used by the
        benchmark for PR-AUC. score() is the single state-advancing call per
        cycle, so this is read straight after it rather than recomputed (which
        would double-advance the temporal state)."""
        return self._last_anomaly_value

    def score(self, features: BackendFeatures) -> AnomalyScore:
        # Always advance temporal state exactly once, even on a low-sample
        # window, so the baseline / CUSUM stay aligned with wall-clock cycles.
        tf = self._extractor.update(
            features.backend_id, features.latency_ms, features.latency_rolling_mean_ms,
            features.error_rate, features.latency_rolling_std_ms,
        )

        # Low-sample window: no trustworthy aggregate -> healthy-by-default, same
        # gate as the point-feature engines. (During warmup the four
        # history-dependent trend signals are already 0.0 from the extractor, so
        # the model simply sees a shape-only vector — no special-casing needed.)
        if features.sample_count < self.min_sample_count:
            self._last_anomaly_value = _SUPPRESSED_VALUE
            return AnomalyScore(features.backend_id, "healthy", 0.0)

        vector = [
            features.latency_ms,
            features.latency_rolling_mean_ms,
            features.error_rate,
            features.latency_rolling_std_ms,
            tf.mean_dev, tf.max_dev, tf.cusum_pos, tf.slope,
            tf.max_ratio, tf.std_ratio,
        ]

        # A DB hiccup (NULL/NaN aggregates, an empty rolling window) can surface
        # non-finite features; StandardScaler propagates NaN/inf and
        # decision_function then returns NaN, which would fall through to a
        # spurious "unhealthy". Treat a non-finite vector as insufficient data,
        # exactly like IsolationForestEngine.
        try:
            if not all(math.isfinite(v) for v in vector):
                self._last_anomaly_value = _SUPPRESSED_VALUE
                return AnomalyScore(features.backend_id, "healthy", 0.0)
        except TypeError:
            self._last_anomaly_value = _SUPPRESSED_VALUE
            return AnomalyScore(features.backend_id, "healthy", 0.0)

        x = self.production_scaler.transform([vector])
        raw = float(self.model.decision_function(x)[0])
        # Continuous severity for PR-AUC: negate so higher = more anomalous.
        self._last_anomaly_value = -raw

        if raw > self.healthy_above:
            return AnomalyScore(features.backend_id, "healthy", 0.0)
        if raw >= self.unhealthy_below:
            return AnomalyScore(
                features.backend_id,
                "degraded",
                0.5,
                metric="anomaly_score",
                observed_value=raw,
                threshold=self.healthy_above,
            )
        return AnomalyScore(
            features.backend_id,
            "unhealthy",
            min(1.0, abs(raw - self.unhealthy_below) / self.unhealthy_score_scale),
            metric="anomaly_score",
            observed_value=raw,
            threshold=self.unhealthy_below,
        )

    def reload(self) -> None:
        """No-op: the model is immutable for the lifetime of the process."""
