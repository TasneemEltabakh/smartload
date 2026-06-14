"""
experiments/anomaly-detection-bench/generators.py
───────────────────────────────────────────────────
Deterministic synthetic anomaly-injection generator with ground truth.

Each generated trace is a per-timestep stream of windowed backend features
that mirrors runloop.build_features_from_rows semantics: for one backend over
a sliding window the run loop emits

    latency_ms              = MAX(request_latency_ms) over the window
    latency_rolling_mean_ms = AVG(request_latency_ms) over the window
    latency_rolling_std_ms  = STDDEV(request_latency_ms) over the window
    error_rate              = AVG(error_rate) over the window
    sample_count            = COUNT(*) over the window

So this generator draws raw per-request latencies and per-request error
indicators inside each window, then aggregates them with exactly those
operators. That keeps the benchmark features in the same shape the live
engine scores, and makes the window MAX (not the mean) the thing a latency
spike moves — which is what the production feature pipeline actually sees.

Every timestep carries a ground-truth label: 1 while the injected anomaly is
active, 0 otherwise. clean-control traces are label-0 throughout and measure
specificity / false-positive rate.

Profiles (parameterised by magnitude x duration x baseline x seed):

  latency-spike        during the injection window the per-request latency MAX
                       jumps to 1.5-5x baseline; per-request std tracks the
                       spike (max * {0.1..1.2}).
  error-burst          error_rate steps 0 -> {0.08, 0.15, 0.30} during the
                       injection; latency stays at baseline.
  gradual-degradation  latency drifts linearly upward over 60-120 s, peaking at
                       the magnitude multiple of baseline, then holds.
  clean-control        pure healthy traffic, no injection — the specificity
                       control.

Each trace runs for `total_seconds` at one feature emission per `window_s`
(default 1 s emission cadence over a `window_s`-wide window). The injection
starts at `inject_start_s` and lasts `duration_s`. Sample counts per window are
kept comfortably above the engines' min_sample_count gate so the gate does not
suppress everything.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

# Per-request arrival rate inside a window. With window_s=10 and 30 req/s the
# window holds ~300 raw samples — far above min_sample_count=10, so the
# data-quality gate never fires on a populated window.
REQUESTS_PER_SECOND = 30


@dataclass(frozen=True)
class GenParams:
    """Knobs shared across profiles plus the per-profile ones.

    total_seconds / window_s / step_s set the trace length and emission
    cadence; inject_start_s / duration_s place the anomaly. Defaults give a
    180 s trace, a 10 s feature window emitted every 1 s, with a 40 s anomaly
    starting at t=60 s.
    """

    total_seconds: int = 180
    window_s: int = 10
    step_s: int = 1
    inject_start_s: int = 60
    duration_s: int = 40
    requests_per_second: int = REQUESTS_PER_SECOND

    # healthy baseline (real-ms)
    base_latency_ms: float = 20.0
    base_latency_jitter: float = 0.20      # per-request lognormal sigma
    base_error_rate: float = 0.002          # healthy error floor

    # latency-spike
    spike_mult: float = 3.0                 # MAX during spike = mult * baseline
    spike_std_frac: float = 0.6             # per-request std = frac * spike peak

    # error-burst
    burst_error_rate: float = 0.15          # error_rate during the burst

    # gradual-degradation
    gradual_peak_mult: float = 4.0          # final latency = mult * baseline
    gradual_ramp_s: int = 90                # linear ramp length (seconds)

    # partial-failure (HELD-OUT generalization profile — see _emit). A ramping
    # fraction of requests turn slow (a long latency tail) and some of those
    # error: a correlated/partial backend failure. Its bimodal within-window
    # latency is a shape NONE of the training profiles produce.
    partial_frac_start: float = 0.10        # fraction of requests failing at onset
    partial_frac_end: float = 0.60          # fraction failing at the end of the ramp
    partial_slow_mult_lo: float = 5.0       # slow-tail latency multiple (low)
    partial_slow_mult_hi: float = 12.0      # slow-tail latency multiple (high)
    partial_fail_error_prob: float = 0.30   # P(a failed request also errors)

    # flappy-clean (gate-quantification profile — see _emit). Healthy traffic
    # with a high per-request jitter, so the window MAX bounces across a
    # detector's threshold on isolated windows: realistic noisy telemetry whose
    # transient flips the stability gate is meant to absorb. label 0 throughout.
    flappy_jitter: float = 0.55             # per-request lognormal sigma (vs 0.20 healthy)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FeatureStep:
    """One emitted feature vector plus its ground-truth label and the time
    (seconds from trace start) it was emitted at."""

    t_s: int
    latency_ms: float
    latency_rolling_mean_ms: float
    error_rate: float
    latency_rolling_std_ms: float
    sample_count: int
    label: int  # 1 while the injected anomaly is active, else 0


def _rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(seed)


def _healthy_latencies(rng: np.random.Generator, n: int, base: float, sigma: float) -> np.ndarray:
    """n healthy per-request latencies: lognormal around `base`."""
    return base * rng.lognormal(mean=0.0, sigma=sigma, size=n)


def _window_features(
    latencies: np.ndarray, errors: np.ndarray
) -> tuple[float, float, float, float, int]:
    """Aggregate raw per-request latencies + per-request error indicators into
    the (max, mean, std, error_rate, count) the run loop emits."""
    n = int(latencies.size)
    if n == 0:
        return 0.0, 0.0, 0.0, 0.0, 0
    return (
        float(latencies.max()),
        float(latencies.mean()),
        float(errors.mean()),
        float(latencies.std()),
        n,
    )


# Profiles that inject NO anomaly: their windows are healthy throughout and
# must carry label 0 for the whole trace (a pure specificity control). Listing
# them here keeps the labeller profile-aware.
NON_INJECTING_PROFILES = ("clean-control", "flappy-clean")


def _active(t_s: int, p: GenParams) -> bool:
    return p.inject_start_s <= t_s < p.inject_start_s + p.duration_s


def _anomalous(t_s: int, profile: str, p: GenParams) -> bool:
    """Ground truth: is the window whose trailing edge is second `t_s` actually
    anomalous? A window is anomalous iff the injection is active at its trailing
    edge AND this profile injects something. clean-control injects nothing, so
    it is healthy throughout — fixing a prior labeller that stamped its
    injection-time window anomalous (contradicting the docstring's 'label-0
    throughout' and letting a detector that fired on clean traffic bank those
    as true positives instead of false positives)."""
    if profile in NON_INJECTING_PROFILES:
        return False
    return _active(t_s, p)


def _emit(profile: str, seed: int, p: GenParams) -> list[FeatureStep]:
    """Drive one trace and emit one FeatureStep per step.

    Raw per-request latencies/errors are drawn for the whole trace once, then
    each emission aggregates the trailing `window_s` worth of raw samples — so
    a feature window straddling the injection boundary mixes both regimes,
    exactly as the live window does."""
    rng = _rng(seed)
    rps = p.requests_per_second
    n_total = p.total_seconds * rps

    # Per-second healthy baseline, plus per-profile injection applied to the
    # raw stream over the active interval.
    latencies = np.empty(n_total, dtype="float64")
    errors = np.zeros(n_total, dtype="float64")

    for sec in range(p.total_seconds):
        lo, hi = sec * rps, (sec + 1) * rps
        # flappy-clean draws its healthy traffic with a wider per-request jitter
        # so the window MAX is noisy; every other profile uses the calm jitter.
        jitter = p.flappy_jitter if profile == "flappy-clean" else p.base_latency_jitter
        base = _healthy_latencies(rng, rps, p.base_latency_ms, jitter)
        err = (rng.random(rps) < p.base_error_rate).astype("float64")

        if profile == "latency-spike" and _active(sec, p):
            # Lift this second's latencies so the window MAX reaches
            # spike_mult * baseline, with spike-proportional variance.
            peak = p.spike_mult * p.base_latency_ms
            base = base + rng.uniform(0.5, 1.0, rps) * (peak - p.base_latency_ms)
            base = base + rng.normal(0.0, p.spike_std_frac * peak, rps)
            base = np.clip(base, p.base_latency_ms, None)
        elif profile == "error-burst" and _active(sec, p):
            err = (rng.random(rps) < p.burst_error_rate).astype("float64")
        elif profile == "gradual-degradation" and _active(sec, p):
            # Linear drift from baseline to gradual_peak_mult*baseline over
            # gradual_ramp_s, then hold at the peak.
            elapsed = sec - p.inject_start_s
            frac = min(1.0, elapsed / max(1, p.gradual_ramp_s))
            mult = 1.0 + frac * (p.gradual_peak_mult - 1.0)
            base = base * mult
        elif profile == "partial-failure" and _active(sec, p):
            # HELD-OUT generalization shape: a ramping fraction of this second's
            # requests turn into a slow tail (5-12x baseline) and some of those
            # also error — a correlated/partial backend failure. The window then
            # holds a bimodal latency distribution (most fast, a growing minority
            # very slow), which lifts MAX and STD hard but the mean only
            # partially — a shape none of the training profiles produce.
            elapsed = sec - p.inject_start_s
            ramp = min(1.0, elapsed / max(1, p.duration_s))
            frac = p.partial_frac_start + ramp * (p.partial_frac_end - p.partial_frac_start)
            fail = rng.random(rps) < frac
            n_fail = int(fail.sum())
            if n_fail:
                base[fail] = base[fail] * rng.uniform(
                    p.partial_slow_mult_lo, p.partial_slow_mult_hi, n_fail)
                err[fail] = (rng.random(n_fail) < p.partial_fail_error_prob).astype("float64")
        # clean-control / flappy-clean: no injection branch — stay healthy
        # (flappy-clean's only difference is the wider jitter drawn above).

        latencies[lo:hi] = base
        errors[lo:hi] = err

    steps: list[FeatureStep] = []
    window_n = p.window_s * rps
    for t_s in range(p.window_s, p.total_seconds + 1, p.step_s):
        end = t_s * rps
        start = max(0, end - window_n)
        mx, mean, erate, std, count = _window_features(latencies[start:end], errors[start:end])
        # Label the emission anomalous iff the injection is active at the
        # window's trailing edge (the most recent second the window covers) AND
        # this profile actually injects an anomaly (clean-control never does).
        label = int(_anomalous(t_s - 1, profile, p))
        steps.append(FeatureStep(
            t_s=t_s, latency_ms=mx, latency_rolling_mean_ms=mean, error_rate=erate,
            latency_rolling_std_ms=std, sample_count=count, label=label,
        ))
    return steps


# The four original profiles a detector may be calibrated / trained on.
TRAIN_PROFILES = ("latency-spike", "error-burst", "gradual-degradation", "clean-control")

# Held-out profiles: NO detector trains or calibrates on these. They are the
# generalization (`partial-failure`, a novel anomaly shape) and gate-behaviour
# (`flappy-clean`, noisy healthy traffic) controls. Keeping them out of
# TRAIN_PROFILES is what makes the benchmark's generalization claim honest.
HELDOUT_PROFILES = ("partial-failure", "flappy-clean")

# Everything the benchmark scores.
PROFILES = TRAIN_PROFILES + HELDOUT_PROFILES


def generate(profile: str, seed: int, params: GenParams) -> list[FeatureStep]:
    """Build one synthetic feature trace for `profile` at `seed`."""
    if profile not in PROFILES:
        raise ValueError(f"unknown profile: {profile!r}")
    return _emit(profile, seed, params)


def injection_window(params: GenParams) -> tuple[int, int]:
    """(start_s, end_s) of the active injection — for latency metrics."""
    return params.inject_start_s, params.inject_start_s + params.duration_s
