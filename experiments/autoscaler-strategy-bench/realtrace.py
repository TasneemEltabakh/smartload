"""
experiments/autoscaler-strategy-bench/realtrace.py
───────────────────────────────────────────────────
Real-world demand realizations for the autoscaler benchmark, drawn from the
shared corpus at /data/smartload-datasets (per-minute request-rate traces).

Three sources, each surfaced as a benchmark "profile" with a deterministic
window per seed so the controlled-comparison property holds (the SAME demand
realization is replayed through every strategy):

  azure     Azure Functions Trace 2019 (PRIMARY). Smooth diurnal serverless
            demand (peak/mean ≈ 2). Windows evenly spaced across the 14-day
            trace so each seed sees a different real diurnal segment.

  worldcup  1998 FIFA World Cup access logs (real flash crowds, peak/mean ≈ 21).
            Windows are centred on the trace's largest peaks so each seed
            replays a genuine flash-crowd event — the real-data analogue of the
            synthetic `spike` profile.

  alibaba   Alibaba Cluster Trace 2018, instances-launched-per-minute as a
            demand-shape PROXY (labelled; not HTTP requests). Bursty
            (peak/mean ≈ 18); windows centred on its largest bursts.

PROCESSING (identical, leakage-free, deterministic):
  1. Load the per-minute series once (cached).
  2. Select the window for this (source, seed) — a fixed 30-minute span, so the
     replay length, warm-up and cooldown ratios match the synthetic profiles.
  3. Upsample minute→second by linear interpolation (load ramps between minute
     buckets; it does not teleport), giving `n` one-second steps.
  4. Normalize so the window peak sits at `peak_rps` (= 8 × per-instance
     capacity), exactly as the synthetic curves are scaled — the pool must span
     most of its [min, max] range. Only the SHAPE is real; the absolute scale is
     normalized so all profiles grade the same pool.

No synthetic noise is injected: variation across seeds comes from different real
windows, not a noise draw. `seed` selects the window deterministically.
"""

from __future__ import annotations

import functools
import pathlib

import numpy as np

DATA_ROOT = pathlib.Path("/data/smartload-datasets")

REAL_SOURCES: tuple[str, ...] = ("azure", "worldcup", "alibaba")

_SRC_DIR = {
    "azure": "azure-functions-2019",
    "worldcup": "worldcup98",
    "alibaba": "alibaba-2018",
}

# How each source's candidate windows are chosen.
#   "spread" — evenly spaced across the active region (typical/diurnal segments).
#   "peaks"  — centred on the largest peaks (flash-crowd / burst events).
_SELECTION = {
    "azure": "spread",
    "worldcup": "peaks",
    "alibaba": "peaks",
}

_WINDOW_MIN = 30          # window length in minutes (→ 30 min like the synthetic run)
_N_CANDIDATES = 16        # candidate windows per source; seed indexes into these
_PEAK_OFFSET_FRAC = 0.40  # for "peaks": place the peak 40% into the window


@functools.lru_cache(maxsize=None)
def _load_series(source: str) -> np.ndarray:
    """Per-minute requests for `source` as a float array (cached)."""
    import pandas as pd
    path = DATA_ROOT / _SRC_DIR[source] / "requests_per_minute.csv"
    df = pd.read_csv(path)
    return df["requests_per_minute"].to_numpy().astype(float)


@functools.lru_cache(maxsize=None)
def _candidate_starts(source: str, window_min: int) -> tuple[int, ...]:
    """Deterministic candidate window start-minutes for `source`.

    "spread": evenly spaced over the trace. "peaks": the largest non-overlapping
    peaks, window placed so the peak lands `_PEAK_OFFSET_FRAC` into it.
    """
    r = _load_series(source)
    n = len(r)
    w = window_min
    last_start = n - (w + 1)
    if last_start <= 0:
        return (0,)

    if _SELECTION[source] == "spread":
        starts = np.linspace(0, last_start, _N_CANDIDATES).astype(int)
        return tuple(int(s) for s in starts)

    # "peaks": greedily pick the highest minutes, each ≥ one window apart.
    order = np.argsort(r)[::-1]
    chosen: list[int] = []
    offset = int(_PEAK_OFFSET_FRAC * w)
    for idx in order:
        start = int(idx) - offset
        start = max(0, min(start, last_start))
        if all(abs(start - c) >= w for c in chosen):
            chosen.append(start)
        if len(chosen) >= _N_CANDIDATES:
            break
    chosen.sort()
    return tuple(chosen)


def realtrace_curve(source: str, n: int, peak_rps: float, seed: int) -> np.ndarray:
    """Absolute-RPS demand realization for (source, seed) over `n` 1-second steps.

    Mirrors `demand.demand_curve`'s signature so the strategy runner is identical
    for synthetic and real profiles. `seed` deterministically selects which real
    window is replayed; the window is upsampled to `n` steps and scaled so its
    peak equals `peak_rps`.
    """
    if source not in REAL_SOURCES:
        raise ValueError(f"unknown real source: {source!r}")
    r = _load_series(source)
    starts = _candidate_starts(source, _WINDOW_MIN)
    start = starts[seed % len(starts)]

    minute_slice = r[start:start + _WINDOW_MIN + 1]
    minute_idx = np.arange(len(minute_slice))
    # Map each 1-s step onto the minute axis and linearly interpolate.
    sec_on_min_axis = np.arange(n) * (len(minute_slice) - 1) / max(1, n - 1)
    series = np.interp(sec_on_min_axis, minute_idx, minute_slice)

    peak = float(np.max(series))
    if peak <= 0:
        return np.zeros(n)
    return (series / peak * peak_rps).clip(min=0.0)
