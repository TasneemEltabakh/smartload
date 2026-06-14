"""
experiments/anomaly-detection-bench/zscore_engine.py
─────────────────────────────────────────────────────
A standard-deviation baseline anomaly engine for the benchmark.

Where the threshold engine compares the window MAX against a fixed multiple of
the window MEAN, this engine works in z-score units: how many rolling standard
deviations the window MAX sits above the rolling mean. That makes it sensitive
to the *shape* of a latency window (a spike with high variance) rather than to
an absolute ratio, while staying a simple, transparent, untrained rule — a fair
classical counterpart to the trained Isolation Forest.

Rule:
    z = (latency_ms - latency_rolling_mean_ms) / latency_rolling_std_ms
    error_rate > error_rate_threshold         -> unhealthy
    z > unhealthy_z  OR  error breach          -> unhealthy
    z > degraded_z                             -> degraded
    otherwise                                  -> healthy

A note on the gates. The classic single-sample 3-sigma rule (degraded z>3,
unhealthy z>5) is calibrated for an individual observation against a
distribution. Here the input is a *window MAX*, and the maximum of a few
hundred lognormal per-request latencies already sits ~3.5 sigma above the
window mean even for perfectly healthy traffic (extreme-value statistics). So
the gates are lifted to degraded z>5 / unhealthy z>6, which is the 3-sigma
spirit applied to the windowed-MAX baseline. This is itself an honest finding:
a naive latency z-score is a weak detector for windowed-MAX features, since the
clean baseline already carries a high z. It catches error bursts well (via the
error breach) but separates latency spikes poorly.

It also exposes a continuous anomaly score (a smooth, increasing function of z
and the error ratio) so PR-AUC can be computed for it, the same as for the
Isolation Forest. The score is bounded to [0, 1].

This file lives under the experiment, not under services/, because it is a
benchmark contender rather than a shipped engine.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

_SVC = Path(__file__).resolve().parents[2] / "services" / "anomaly-detector"
for _p in (str(_SVC), str(_SVC / "engines" / "threshold")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from engine_base import AnomalyEngine, AnomalyScore, BackendFeatures  # noqa: E402


class ZScoreEngine(AnomalyEngine):
    """3-sigma latency z-score baseline with an error-rate breach."""

    def __init__(
        self,
        latency_multiplier: float = 3.0,   # accepted for select_engine kwarg parity; unused
        error_rate_threshold: float = 0.05,
        min_sample_count: int = 10,
        degraded_z: float = 5.0,
        unhealthy_z: float = 6.0,
    ):
        self.error_rate_threshold = error_rate_threshold
        self.min_sample_count = min_sample_count
        self.degraded_z = degraded_z
        self.unhealthy_z = unhealthy_z

    def _zscore(self, features: BackendFeatures) -> float:
        std = features.latency_rolling_std_ms
        if std <= 0.0:
            # No variance in the window: fall back to a ratio-vs-mean signal so a
            # flat-but-elevated window still registers, scaled to be comparable
            # to a z (treat a doubling of the mean as ~degraded_z).
            mean = features.latency_rolling_mean_ms
            if mean <= 0.0:
                return 0.0
            ratio = features.latency_ms / mean
            return max(0.0, (ratio - 1.0) * self.degraded_z)
        return (features.latency_ms - features.latency_rolling_mean_ms) / std

    def score(self, features: BackendFeatures) -> AnomalyScore:
        if features.sample_count < self.min_sample_count:
            return AnomalyScore(features.backend_id, "healthy", 0.0)

        z = self._zscore(features)
        err = features.error_rate
        err_ratio = err / self.error_rate_threshold if self.error_rate_threshold > 0 else 0.0

        # Continuous score in [0, 1]: a logistic-style squash of the dominant
        # signal (z normalised by the unhealthy gate, or the error ratio).
        z_component = z / self.unhealthy_z if self.unhealthy_z > 0 else 0.0
        raw = max(z_component, err_ratio)
        cont_score = 1.0 - math.exp(-max(0.0, raw))  # 0 at raw=0, ->1 as raw grows

        if err > self.error_rate_threshold:
            return AnomalyScore(
                features.backend_id, "unhealthy", min(1.0, cont_score),
                metric="error_rate", observed_value=err, threshold=self.error_rate_threshold,
            )
        if z > self.unhealthy_z:
            return AnomalyScore(
                features.backend_id, "unhealthy", min(1.0, cont_score),
                metric="latency_zscore", observed_value=z, threshold=self.unhealthy_z,
            )
        if z > self.degraded_z:
            return AnomalyScore(
                features.backend_id, "degraded", min(1.0, cont_score),
                metric="latency_zscore", observed_value=z, threshold=self.degraded_z,
            )
        return AnomalyScore(features.backend_id, "healthy", min(1.0, cont_score))

    def anomaly_value(self, features: BackendFeatures) -> float:
        """Continuous anomaly value for PR-AUC: higher = more anomalous.

        Returns the bounded score the verdict is derived from, independent of
        the tier thresholds, so the PR curve sweeps a real operating range."""
        if features.sample_count < self.min_sample_count:
            return 0.0
        z = self._zscore(features)
        err_ratio = (
            features.error_rate / self.error_rate_threshold
            if self.error_rate_threshold > 0 else 0.0
        )
        z_component = z / self.unhealthy_z if self.unhealthy_z > 0 else 0.0
        raw = max(z_component, err_ratio)
        return 1.0 - math.exp(-max(0.0, raw))
