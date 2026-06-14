"""
experiments/forecasting-engine-bench/generators.py
───────────────────────────────────────────────────
Synthetic request-rate series for the forecasting-engine benchmark.

The real Alibaba production trace is not vendored in this repository, so the
benchmark drives every contender with reproducible synthetic series instead.
Each series is a 5-minute-bucketed request-per-second (RPS) signal, matching
the bucket size the shipped ARIMA artifact was trained at (``freq='5min'``).

Four profiles span the load shapes an autoscaler actually sees:

  steady   constant level + small Gaussian noise. The easy case — any sane
           forecaster should nail it; it exists to expose a contender that
           cannot even hold a flat line.

  diurnal  a sinusoidal daily cycle (period = 288 buckets = 24 h) riding on a
           base level, plus noise. This is the realistic autoscaling workload:
           a smooth, predictable day/night swing.

  spiky    a steady base with Poisson-timed multiplicative bursts injected on
           top. Models flash-crowd / retry-storm behaviour: mostly calm with
           occasional sharp, short-lived spikes.

  ramp     a monotone upward trend + noise. Deliberately non-stationary — it
           stresses the ARIMA(2,0,2) ``d=0`` assumption (no differencing), so a
           drifting mean is exactly the regime the differencing order does not
           cover.

Every profile takes an explicit integer ``seed`` so a (profile, seed) pair is
fully reproducible. All levels are clamped at >= 0 (negative RPS is
meaningless) and returned as a float64 numpy array of length ``n_buckets``.

These series are out-of-distribution for the ARIMA artifact (its parameters
were fit on the Alibaba trace, not on anything generated here). That is
deliberate: scoring a model on a tail of its own training data leaks the fit
and flatters the result. A fresh distribution is the fair generalization test.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

# One day at a 5-minute bucket size: 24 * 60 / 5 = 288 buckets. Used as the
# diurnal period and as the default span unit so "days" maps cleanly to buckets.
BUCKETS_PER_DAY = 288


@dataclass(frozen=True)
class GenParams:
    """Knobs shared across profiles, plus the per-profile ones.

    Defaults are chosen so the resulting RPS lives in a realistic
    tens-to-low-hundreds band and the noise is visible but not dominant.
    """

    n_buckets: int = 3 * BUCKETS_PER_DAY  # 3 days → multiple diurnal cycles

    # steady
    steady_level: float = 50.0
    steady_noise_sd: float = 3.0

    # diurnal
    diurnal_base: float = 60.0
    diurnal_amplitude: float = 40.0   # peak-to-base swing
    diurnal_period: int = BUCKETS_PER_DAY
    diurnal_noise_sd: float = 4.0

    # spiky
    spiky_base: float = 40.0
    spiky_noise_sd: float = 3.0
    spiky_burst_rate: float = 0.02    # P(a burst starts in any one bucket)
    spiky_burst_mult: float = 4.0     # mean multiplicative burst height
    spiky_burst_decay: float = 0.55   # per-bucket geometric decay of a burst

    # ramp
    ramp_start: float = 30.0
    ramp_slope_per_day: float = 60.0  # added per day of elapsed time
    ramp_noise_sd: float = 4.0

    def as_dict(self) -> dict:
        return asdict(self)


def _rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(seed)


def gen_steady(p: GenParams, seed: int) -> np.ndarray:
    rng = _rng(seed)
    noise = rng.normal(0.0, p.steady_noise_sd, p.n_buckets)
    return np.clip(p.steady_level + noise, 0.0, None).astype("float64")


def gen_diurnal(p: GenParams, seed: int) -> np.ndarray:
    rng = _rng(seed)
    t = np.arange(p.n_buckets)
    # sin starts at the base level and swings ± half the amplitude around base.
    cycle = (p.diurnal_amplitude / 2.0) * np.sin(2.0 * np.pi * t / p.diurnal_period)
    noise = rng.normal(0.0, p.diurnal_noise_sd, p.n_buckets)
    return np.clip(p.diurnal_base + cycle + noise, 0.0, None).astype("float64")


def gen_spiky(p: GenParams, seed: int) -> np.ndarray:
    rng = _rng(seed)
    base = p.spiky_base + rng.normal(0.0, p.spiky_noise_sd, p.n_buckets)
    series = np.clip(base, 0.0, None)

    # Burst starts are a Bernoulli(burst_rate) process per bucket (a discrete
    # stand-in for a Poisson arrival of bursts). Each start injects an additive
    # spike that decays geometrically over the following buckets — a short,
    # sharp flash rather than a step change.
    starts = rng.random(p.n_buckets) < p.spiky_burst_rate
    for idx in np.flatnonzero(starts):
        height = rng.exponential(p.spiky_burst_mult) * p.spiky_base
        k = 0
        while True:
            pos = idx + k
            if pos >= p.n_buckets:
                break
            contrib = height * (p.spiky_burst_decay ** k)
            if contrib < 0.5:  # spike has decayed into the noise floor
                break
            series[pos] += contrib
            k += 1
    return np.clip(series, 0.0, None).astype("float64")


def gen_ramp(p: GenParams, seed: int) -> np.ndarray:
    rng = _rng(seed)
    t = np.arange(p.n_buckets)
    slope_per_bucket = p.ramp_slope_per_day / p.diurnal_period
    trend = p.ramp_start + slope_per_bucket * t
    noise = rng.normal(0.0, p.ramp_noise_sd, p.n_buckets)
    return np.clip(trend + noise, 0.0, None).astype("float64")


# Registry mapping profile name → generator. Iteration order is the table order.
PROFILES: dict[str, callable] = {
    "steady": gen_steady,
    "diurnal": gen_diurnal,
    "spiky": gen_spiky,
    "ramp": gen_ramp,
}


def generate(profile: str, seed: int, params: GenParams) -> np.ndarray:
    """Build one synthetic series for ``profile`` at ``seed``."""
    try:
        fn = PROFILES[profile]
    except KeyError as exc:
        raise ValueError(f"unknown profile: {profile!r}") from exc
    return fn(params, seed)
