"""
services/anomaly-detector/features/trend.py
────────────────────────────────────────────
Per-backend *temporal* feature extraction for anomaly detection.

Why this exists
---------------
The four point features the run loop emits per window
(``runloop.build_features_from_rows``) —

    latency_ms              = MAX(request_latency_ms) over the window
    latency_rolling_mean_ms = AVG(request_latency_ms) over the window
    latency_rolling_std_ms  = STDDEV(request_latency_ms) over the window
    error_rate              = AVG(error_rate) over the window

— carry no *history*. They describe a single window in isolation, so a backend
whose latency drifts slowly upward (a memory leak, a saturating connection
pool, a degrading disk) looks identical, window-by-window, to a backend that is
simply, steadily slow: the within-window *shape* (max/mean, std/mean) is
unchanged, only the absolute level rises relative to the backend's OWN normal.
With no memory of that normal, every stateless engine (threshold, z-score, the
Isolation Forests) scores gradual degradation at ~0 recall.

This module supplies the missing axis. It holds a small amount of per-backend
state across cycles — exactly as ``app.py``'s run loop already holds a
``BackendState`` per backend for the stability gate — and from the stream of
point features derives *backend-relative* signals:

  mean_dev   relative deviation of the current window mean from a slow,
             contamination-guarded EWMA baseline of the mean. This is the
             headline signal for gradual degradation: 0 for a steady backend,
             growing as the level drifts away from its own established normal.
  max_dev    same, for the window MAX — a latency *spike* lifts this sharply
             even when the mean barely moves.
  cusum_pos  one-sided CUSUM of the standardised mean deviation. CUSUM is the
             classical detector for a small, persistent shift in the mean: it
             accumulates sub-threshold positive deviations so a slow ramp trips
             it well before any single window looks abnormal.
  slope      ordinary-least-squares slope of the recent window means, expressed
             as a fraction of the baseline per step — the instantaneous trend.
  max_ratio  MAX / mean — within-window shape (high for a spiky window).
  std_ratio  STD / mean — within-window dispersion (high for a spiky window).

The baseline EWMA is *contamination-guarded*: when the current deviation is
large the baseline update is damped toward zero, so an active anomaly does not
poison the very normal it is being measured against (and recovery is fast — the
baseline still reflects pre-anomaly normal the moment the level returns).

State lifecycle
---------------
One ``BackendTrendState`` per ``backend_id``. A ``TrendExtractor`` owns a dict
of them and exposes ``update(features) -> TrendFeatures`` (advances state and
returns the derived features) plus ``reset()`` (drop all state — used at the
start of an independent evaluation trace, and equivalent to a backend coming
online fresh). During the warmup period (before ``warmup_steps`` windows have
been seen) the derived trend signals are reported as 0.0 / non-anomalous, so a
cold start never manufactures an alert.

This module is dependency-light (numpy only) and deterministic: identical input
streams produce identical features. It is shared by the rule-based
``trend_rule`` engine and the trained ``trend_forest`` engine.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

# Order of the derived temporal block. The trained trend_forest engine appends
# this to the four point features; keep it stable or retrain.
TREND_FEATURE_ORDER = (
    "mean_dev",
    "max_dev",
    "cusum_pos",
    "slope",
    "max_ratio",
    "std_ratio",
)

# Full enriched vector = point features (engine FEATURE_ORDER) + trend block.
POINT_FEATURE_ORDER = (
    "latency_ms",
    "latency_rolling_mean_ms",
    "error_rate",
    "latency_rolling_std_ms",
)
ENRICHED_FEATURE_ORDER = POINT_FEATURE_ORDER + TREND_FEATURE_ORDER


@dataclass
class TrendConfig:
    """Tunables for the temporal extractor. Defaults are calibrated on
    production-shaped streams at seeds disjoint from any evaluation set (see
    tools/anomaly-training/calibrate_trend.py)."""

    warmup_steps: int = 12          # windows to observe before trusting trend signals
    baseline_alpha: float = 0.08    # EWMA weight for the slow baseline (~12-step memory)
    guard_dev: float = 0.12         # |mean_dev| above this starts damping the baseline update
    freeze_cusum: float = 3.0       # cusum_pos above this freezes the baseline (drift suspected)
    slope_window: int = 10          # windows used for the OLS slope fit
    cusum_k: float = 0.5            # CUSUM slack (in scale units) — drift must exceed this to accumulate
    cusum_cap: float = 25.0         # ceiling on cusum_pos so a spike can't make it unbounded
    recovery_dev: float = 0.08      # |mean_dev| below this = back in control -> hard-drain CUSUM
    recovery_decay: float = 0.40    # multiplicative CUSUM decay per in-control window (fast recovery)
    scale_alpha: float = 0.08       # EWMA weight for the robust deviation scale
    scale_floor_frac: float = 0.05  # scale floor as a fraction of the baseline (avoids /0 on flat streams)


@dataclass
class TrendFeatures:
    """Derived temporal features for one window plus a warmup flag.

    ``warming_up`` is True until the extractor has seen ``warmup_steps`` windows
    for this backend; while True the trend signals are 0.0 and callers should
    treat the backend as healthy-by-default (insufficient history)."""

    mean_dev: float
    max_dev: float
    cusum_pos: float
    slope: float
    max_ratio: float
    std_ratio: float
    warming_up: bool
    baseline_mean: float
    baseline_max: float

    def trend_vector(self) -> list[float]:
        """The TREND_FEATURE_ORDER block as a plain list (for the model)."""
        return [self.mean_dev, self.max_dev, self.cusum_pos,
                self.slope, self.max_ratio, self.std_ratio]


@dataclass
class BackendTrendState:
    """Per-backend memory advanced once per cycle by TrendExtractor.update()."""

    n_seen: int = 0
    baseline_mean: float = 0.0      # slow guarded EWMA of latency_rolling_mean_ms
    baseline_max: float = 0.0       # slow guarded EWMA of latency_ms (window MAX)
    dev_scale: float = 0.0          # EWMA of |mean - baseline_mean| — robust deviation scale
    cusum_pos: float = 0.0          # one-sided positive CUSUM accumulator
    recent_means: deque = field(default_factory=lambda: deque(maxlen=64))


class TrendExtractor:
    """Owns per-backend trend state and derives temporal features per cycle.

    Stateful by design: ``update`` must be called once per backend per cycle in
    time order. Call ``reset`` (or ``reset_backend``) to drop history when an
    independent stream begins."""

    def __init__(self, config: TrendConfig | None = None):
        self.config = config or TrendConfig()
        self._states: dict[str, BackendTrendState] = {}

    def reset(self) -> None:
        """Drop all per-backend state (fresh start for a new trace/episode)."""
        self._states.clear()

    def reset_backend(self, backend_id: str) -> None:
        self._states.pop(backend_id, None)

    def _slope(self, state: BackendTrendState) -> float:
        """OLS slope of the recent window means, normalised by the baseline so
        it is a dimensionless 'fraction-of-normal per step'. 0 with <2 points."""
        n = min(len(state.recent_means), self.config.slope_window)
        if n < 2:
            return 0.0
        y = np.fromiter(list(state.recent_means)[-n:], dtype="float64", count=n)
        x = np.arange(n, dtype="float64")
        x -= x.mean()
        denom = float((x * x).sum())
        if denom <= 0.0:
            return 0.0
        slope = float((x * (y - y.mean())).sum() / denom)
        base = state.baseline_mean if state.baseline_mean > 1e-9 else (y.mean() or 1.0)
        return slope / base

    def update(self, backend_id: str, latency_ms: float, latency_rolling_mean_ms: float,
               error_rate: float, latency_rolling_std_ms: float) -> TrendFeatures:
        """Advance this backend's state with one window and return its derived
        temporal features."""
        c = self.config
        st = self._states.get(backend_id)
        if st is None:
            st = BackendTrendState()
            self._states[backend_id] = st

        mean = float(latency_rolling_mean_ms)
        mx = float(latency_ms)
        st.recent_means.append(mean)

        # Cold start: seed the baselines from the first observed window and
        # report no trend signal until warmup completes.
        if st.n_seen == 0:
            st.baseline_mean = mean
            st.baseline_max = mx
            st.dev_scale = max(c.scale_floor_frac * max(mean, 1e-9), 1e-9)

        base_mean = st.baseline_mean if st.baseline_mean > 1e-9 else max(mean, 1e-9)
        base_max = st.baseline_max if st.baseline_max > 1e-9 else max(mx, 1e-9)

        mean_dev = (mean - base_mean) / base_mean
        max_dev = (mx - base_max) / base_max

        # Robust deviation scale: EWMA of |mean - baseline|, floored relative to
        # the baseline so a perfectly flat clean stream has a finite scale.
        scale_floor = c.scale_floor_frac * base_mean
        scale = max(st.dev_scale, scale_floor, 1e-9)
        standardised = (mean - base_mean) / scale

        # One-sided CUSUM of the standardised deviation with slack k. Only
        # positive (upward-latency) drift accumulates; it bleeds off via the
        # slack, is floored at 0 and capped so a single spike can't run it away.
        st.cusum_pos = min(c.cusum_cap, max(0.0, st.cusum_pos + standardised - c.cusum_k))
        # Reset-on-return-to-control: once the window is clearly back at the
        # baseline, classical CUSUM would still bleed off only at the slack rate
        # (~1/step), leaving a long false-positive overhang after the anomaly
        # clears. When the deviation is back inside `recovery_dev`, hard-drain
        # the accumulator so recovery is fast — without touching its slow
        # accumulation during an actual drift (where the deviation stays large).
        if abs(mean_dev) < c.recovery_dev:
            st.cusum_pos *= c.recovery_decay

        warming_up = st.n_seen < c.warmup_steps

        # ── advance the contamination-guarded baselines ──────────────────────
        # Damp the baseline update when an anomaly is in progress so it cannot
        # drag the baseline up to meet itself (which would erase the very
        # deviation we measure, and is exactly why a plain EWMA chases a slow
        # ramp to ~0 signal). Two guards multiply: an instantaneous one on the
        # current deviation, and a CUSUM one that freezes the baseline once
        # persistent upward drift has been detected. Both are 1 in the normal
        # regime and fall smoothly toward 0 as the anomaly asserts itself.
        if c.guard_dev > 0:
            guard_inst = max(0.0, 1.0 - max(0.0, abs(mean_dev) - c.guard_dev) / c.guard_dev)
        else:
            guard_inst = 1.0
        if c.freeze_cusum > 0:
            guard_cusum = max(0.0, 1.0 - max(0.0, st.cusum_pos - c.freeze_cusum) / c.freeze_cusum)
        else:
            guard_cusum = 1.0
        guard = min(guard_inst, guard_cusum)
        a_mean = c.baseline_alpha * guard
        st.baseline_mean = (1.0 - a_mean) * st.baseline_mean + a_mean * mean
        a_max = c.baseline_alpha * guard
        st.baseline_max = (1.0 - a_max) * st.baseline_max + a_max * mx
        # The deviation scale tracks normal dispersion; freeze it under anomaly
        # too so a spike does not inflate the scale and mask the next event.
        a_scale = c.scale_alpha * guard
        st.dev_scale = (1.0 - a_scale) * st.dev_scale + a_scale * abs(mean - base_mean)

        st.n_seen += 1

        slope = self._slope(st)
        max_ratio = mx / mean if mean > 1e-9 else 0.0
        std_ratio = float(latency_rolling_std_ms) / mean if mean > 1e-9 else 0.0

        if warming_up:
            # Suppress trend signals during warmup, but still report shape.
            return TrendFeatures(
                mean_dev=0.0, max_dev=0.0, cusum_pos=0.0, slope=0.0,
                max_ratio=max_ratio, std_ratio=std_ratio, warming_up=True,
                baseline_mean=st.baseline_mean, baseline_max=st.baseline_max,
            )

        return TrendFeatures(
            mean_dev=mean_dev, max_dev=max_dev, cusum_pos=st.cusum_pos, slope=slope,
            max_ratio=max_ratio, std_ratio=std_ratio, warming_up=False,
            baseline_mean=st.baseline_mean, baseline_max=st.baseline_max,
        )
