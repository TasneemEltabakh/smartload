"""
experiments/forecasting-downstream-bench/demand.py
────────────────────────────────────────────────
Demand-curve generation for the forecasting downstream (autoscaler) benchmark.

Vendored verbatim (shapes + noise model) from
experiments/autoscaler-strategy-bench/demand.py so this track grades its
forecaster against the SAME demand realizations the autoscaler strategy
benchmark uses — no shape drift between the two harnesses.

The benchmark needs ONE demand realization per (profile, seed) that is replayed
identically through all five scaling strategies — a controlled comparison where
the only thing that varies is the signal fed to the scale-decision rule. So the
curve shape must be deterministic for a given (profile, seed), with seed driving
only the noise realization, not the shape.

The four shipped shapes reuse the exact mathematical forms from
``services/rl-engine/training/closed_loop_sim.py::_demand_curve`` (steady ones,
ramp linspace(0.3, 1.0), burst 0.4-base with a 1.0 segment, diurnal
0.6 + 0.4·sin), so the workload geometry the routing trainer sees is the same
geometry the scaler is graded on. Two profiles are added here:

  spike     a flash-crowd step: a calm base that jumps to a high plateau for a
            short span and drops back. This is the regime where acting *before*
            warm-up (predictive) should beat acting *after* the load is already
            here (reactive) — the spike is the settling-time stressor.

  sawtooth  repeated linear ramp-and-reset. A periodic non-stationary shape that
            keeps the scaler chasing a moving target, exposing cooldown lag and
            scale-action churn.

Curves are expressed in absolute RPS, scaled so the peak sits at a chosen
multiple of single-instance capacity (``peak_rps``). Seed controls only the
multiplicative noise band (same band as the source: N(1, 0.05) clipped to
[0.7, 1.3]); the underlying shape is fixed per profile so every seed replays the
same geometry with a different noise draw.
"""

from __future__ import annotations

import numpy as np

PROFILES: tuple[str, ...] = ("steady", "diurnal", "ramp", "spike", "sawtooth", "burst")

# Shapes are normalized to roughly [floor, 1.0]; the caller multiplies by
# `peak_rps` to place the peak at a known multiple of instance capacity.


def _shape(profile: str, n: int) -> np.ndarray:
    """Return the deterministic [0, 1]-ish demand shape for `profile` over n steps.

    steady/ramp/burst/diurnal reproduce the forms in closed_loop_sim._demand_curve;
    spike and sawtooth are added for this benchmark.
    """
    t = np.arange(n)
    if profile == "steady":
        return np.ones(n)
    if profile == "ramp":
        return np.linspace(0.3, 1.0, n)
    if profile == "burst":
        # 0.4 base with a single 1.0 plateau over the second quarter — the
        # closed_loop_sim form, but with a fixed (deterministic) burst window so
        # the shape is identical across seeds.
        curve = np.full(n, 0.4)
        s = n // 4
        curve[s:s + n // 4] = 1.0
        return curve
    if profile == "diurnal":
        return 0.6 + 0.4 * np.sin(2 * np.pi * t / n)
    if profile == "spike":
        # Flash crowd: calm 0.3 base, single sharp step to 1.0 for ~12% of the
        # horizon near the 40% mark, then back to base. The step is what makes
        # warm-up matter: a scaler that reacts only after the step is already
        # observed cannot have capacity in place when it lands.
        curve = np.full(n, 0.3)
        s = int(0.40 * n)
        span = max(1, int(0.12 * n))
        curve[s:s + span] = 1.0
        return curve
    if profile == "sawtooth":
        # Three rising teeth from 0.3 to 1.0 with an instant reset — a periodic
        # non-stationary chase that keeps the scaler perpetually behind.
        teeth = 3
        period = max(1, n // teeth)
        frac = (t % period) / max(1, period - 1)
        return 0.3 + 0.7 * np.clip(frac, 0.0, 1.0)
    raise ValueError(f"unknown profile: {profile!r}")


def demand_curve(profile: str, n: int, peak_rps: float, seed: int) -> np.ndarray:
    """Absolute-RPS demand realization for (profile, seed) over n one-second steps.

    Shape is deterministic per profile; `seed` draws only the multiplicative
    noise (N(1, 0.05) clipped [0.7, 1.3]) — same noise model as the source curve.
    Peak of the noise-free shape is placed at `peak_rps`.
    """
    shape = _shape(profile, n)
    rng = np.random.default_rng(seed)
    noise = rng.normal(1.0, 0.05, size=n).clip(0.7, 1.3)
    return (shape * peak_rps * noise).clip(min=0.0)
