"""
services/anomaly-detector/engines/trend_rule/engine.py
───────────────────────────────────────────────────────
Interpretable, *stateful* trend-aware anomaly engine.

This is the classical-mode counterpart to the trained trend_forest engine: no
model artifact, just transparent rules over the temporal features produced by
``features/trend.py``. It is the engine the cheap operating mode can run when a
trained bundle is unavailable, and it is what closes the gradual-degradation
gap that every stateless engine misses.

Three channels, evaluated per backend per cycle:

  error channel   error_rate > error_rate_threshold              -> unhealthy
                  (catches the error-burst profile, same as the baselines).

  spike channel   the window MAX jumps far above the backend's own established
                  baseline MAX (max_dev), or the within-window mean lifts
                  sharply (mean_dev). A latency spike trips this on its first
                  window — no accumulation needed.

  drift channel   a one-sided CUSUM of the standardised mean deviation. CUSUM
                  is the textbook detector for a small, persistent upward shift
                  in the mean: it accumulates sub-threshold deviations so a slow
                  ramp (gradual degradation) trips it long before any single
                  window looks abnormal to a stateless rule.

Each channel has a degraded and an unhealthy level; the worst channel wins. A
continuous ``anomaly_value`` in [0, 1] is exposed for PR-AUC.

State: one ``features.trend.TrendExtractor`` shared across backends (it keys
state internally by backend_id). ``reset()`` drops it. During warmup (before
the extractor has enough history) only the error channel is live, so a cold
start cannot raise a latency alert.

The thresholds are calibrated on production-shaped streams at seeds DISJOINT
from the evaluation seeds — see tools/anomaly-training/calibrate_trend.py. The
defaults below are that calibration's output.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from engine_base import AnomalyEngine, AnomalyScore, BackendFeatures  # noqa: E402
from features.trend import TrendConfig, TrendExtractor  # noqa: E402


class TrendRuleEngine(AnomalyEngine):
    """Stateful CUSUM + baseline-deviation rule engine."""

    def __init__(
        self,
        latency_multiplier: float = 3.0,        # accepted for select_engine parity; unused
        error_rate_threshold: float = 0.05,
        min_sample_count: int = 10,
        # Defaults below are the output of tools/anomaly-training/calibrate_trend.py
        # (calibration seeds 300..331, disjoint from the benchmark eval seeds and
        # the trend_forest fit seeds) — see trend_rule_calibration.json. The
        # degraded-entry gates + recovery_slope set the primary `status!=healthy`
        # boundary; the unhealthy gates set only the tiering / severity.
        # ── drift channel (CUSUM of standardised mean deviation) ─────────────
        cusum_degraded: float = 2.0,
        cusum_unhealthy: float = 25.0,
        # ── spike channel (deviation of window MAX / mean from own baseline) ─
        max_dev_degraded: float = 0.50,
        max_dev_unhealthy: float = 0.863,
        mean_dev_degraded: float = 0.12,
        mean_dev_unhealthy: float = 0.72,
        # ── recovery suppressor ──────────────────────────────────────────────
        # A backend whose latency is steeply *falling* is recovering, not
        # degrading: don't raise (or sustain) a latency alarm on it. This is
        # what clears the post-injection tail — where a wide window still
        # straddles the just-ended anomaly — quickly, instead of paging on a
        # backend that is visibly getting better. slope is the OLS trend as a
        # fraction of baseline per step; below -recovery_slope means falling.
        recovery_slope: float = 0.02,
        trend_config: TrendConfig | None = None,
    ):
        self.latency_multiplier = latency_multiplier
        self.error_rate_threshold = error_rate_threshold
        self.min_sample_count = min_sample_count
        self.cusum_degraded = cusum_degraded
        self.cusum_unhealthy = cusum_unhealthy
        self.max_dev_degraded = max_dev_degraded
        self.max_dev_unhealthy = max_dev_unhealthy
        self.mean_dev_degraded = mean_dev_degraded
        self.mean_dev_unhealthy = mean_dev_unhealthy
        self.recovery_slope = recovery_slope
        self._extractor = TrendExtractor(trend_config)
        self._last_anomaly_value = 0.0

    def reset(self) -> None:
        """Drop all per-backend temporal state."""
        self._extractor.reset()
        self._last_anomaly_value = 0.0

    def last_anomaly_value(self) -> float:
        """Continuous anomaly severity (in [0, 1]) for the window most recently
        passed to score(). Used by the benchmark for PR-AUC. score() is the
        single state-advancing call per cycle, so this is read straight after
        it rather than recomputed (which would double-advance state)."""
        return self._last_anomaly_value

    # ── continuous severity, used both for the value and to break ties ───────
    def _severity(self, error_rate: float, mean_dev: float, max_dev: float,
                  cusum_pos: float) -> float:
        """A bounded [0, 1] severity that rises with the strongest channel.

        Each channel is normalised by its unhealthy threshold so a value of 1.0
        means 'at the unhealthy boundary on at least one channel'."""
        err_c = error_rate / self.error_rate_threshold if self.error_rate_threshold > 0 else 0.0
        cusum_c = cusum_pos / self.cusum_unhealthy if self.cusum_unhealthy > 0 else 0.0
        maxd_c = max_dev / self.max_dev_unhealthy if self.max_dev_unhealthy > 0 else 0.0
        meand_c = mean_dev / self.mean_dev_unhealthy if self.mean_dev_unhealthy > 0 else 0.0
        return max(0.0, err_c, cusum_c, maxd_c, meand_c)

    def score(self, features: BackendFeatures) -> AnomalyScore:
        # Always advance temporal state, even when the window is low-sample, so
        # the baseline/CUSUM stay aligned with wall-clock cycles. But a
        # low-sample window carries no trustworthy aggregate, so we gate the
        # verdict to healthy (the stability gate's low-sample hold then
        # preserves any prior non-healthy status across the quiet patch).
        tf = self._extractor.update(
            features.backend_id, features.latency_ms, features.latency_rolling_mean_ms,
            features.error_rate, features.latency_rolling_std_ms,
        )
        if features.sample_count < self.min_sample_count:
            self._last_anomaly_value = 0.0
            return AnomalyScore(features.backend_id, "healthy", 0.0)

        err = features.error_rate
        sev = min(1.0, self._severity(err, tf.mean_dev, tf.max_dev, tf.cusum_pos))
        # PR-AUC reads the continuous severity for EVERY window (incl. healthy),
        # so the curve sweeps a real operating range; cache it before the
        # tier-thresholding below collapses healthy verdicts to score 0.0.
        self._last_anomaly_value = sev

        # error channel — always live (no history needed).
        if err > self.error_rate_threshold:
            return AnomalyScore(
                features.backend_id, "unhealthy", sev,
                metric="error_rate", observed_value=err, threshold=self.error_rate_threshold,
            )

        # latency channels need a trusted baseline; suppress during warmup, and
        # suppress while the backend is clearly recovering (latency falling
        # steeply) so the tail of a just-ended anomaly doesn't keep paging.
        recovering = tf.slope <= -self.recovery_slope
        if not tf.warming_up and not recovering:
            # unhealthy: strong spike, large sustained lift, or saturated drift.
            if tf.max_dev >= self.max_dev_unhealthy:
                return AnomalyScore(
                    features.backend_id, "unhealthy", sev,
                    metric="latency_max_dev", observed_value=tf.max_dev, threshold=self.max_dev_unhealthy)
            if tf.mean_dev >= self.mean_dev_unhealthy:
                return AnomalyScore(
                    features.backend_id, "unhealthy", sev,
                    metric="latency_mean_dev", observed_value=tf.mean_dev, threshold=self.mean_dev_unhealthy)
            if tf.cusum_pos >= self.cusum_unhealthy:
                return AnomalyScore(
                    features.backend_id, "unhealthy", sev,
                    metric="latency_cusum", observed_value=tf.cusum_pos, threshold=self.cusum_unhealthy)
            # degraded: emerging spike or accumulating drift.
            if tf.cusum_pos >= self.cusum_degraded:
                return AnomalyScore(
                    features.backend_id, "degraded", sev,
                    metric="latency_cusum", observed_value=tf.cusum_pos, threshold=self.cusum_degraded)
            if tf.max_dev >= self.max_dev_degraded:
                return AnomalyScore(
                    features.backend_id, "degraded", sev,
                    metric="latency_max_dev", observed_value=tf.max_dev, threshold=self.max_dev_degraded)
            if tf.mean_dev >= self.mean_dev_degraded:
                return AnomalyScore(
                    features.backend_id, "degraded", sev,
                    metric="latency_mean_dev", observed_value=tf.mean_dev, threshold=self.mean_dev_degraded)

        return AnomalyScore(features.backend_id, "healthy", 0.0)
