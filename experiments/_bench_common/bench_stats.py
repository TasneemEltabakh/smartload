"""
experiments/_bench_common/bench_stats.py
──────────────────────────────────────────
Shared statistics for the multi-run bench harnesses (#160, SOT §35.3).

Both harnesses (baseline-vs-smartload #148, adaptive-bench #156/#157) batch N
independent runs and report per-metric ``mean ± confidence interval`` instead
of single-run point estimates. This module is the one place the
confidence-interval maths lives so the two harnesses agree to the digit.

CI method: Student's t-distribution. For a small sample (default N=5),

    half_width = t(1-α/2, df=N-1) · s / √N

where ``s`` is the sample standard deviation (``ddof=1``). This is the textbook
small-sample CI for the mean and needs no resampling — deterministic given the
run set.

Degradation:
  N == 1  → std=0, ci_lower=ci_upper=mean, half_width=NaN (interval undefined).
  N == 0  → every field NaN.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

import numpy as np
import pandas as pd
from scipy import stats


# The columns every per-group summary row carries, in stable order. Callers
# prepend their group keys (e.g. ``side, phase, metric``) to this.
STAT_COLUMNS: tuple[str, ...] = (
    "mean", "std", "ci_lower", "ci_upper", "half_width", "n",
)


def mean_ci(values: Iterable[float], confidence: float = 0.95) -> dict:
    """Mean and a two-sided t-distribution confidence interval for a 1-D sample.

    Returns a dict with ``mean``, ``std`` (ddof=1), ``ci_lower``, ``ci_upper``,
    ``half_width`` and ``n``. NaNs in the input are dropped before computing.
    """
    arr = np.asarray(list(values), dtype="float64")
    arr = arr[~np.isnan(arr)]
    n = int(arr.size)
    if n == 0:
        return {"mean": math.nan, "std": math.nan, "ci_lower": math.nan,
                "ci_upper": math.nan, "half_width": math.nan, "n": 0}

    mean = float(arr.mean())
    if n == 1:
        # A single run has no spread to estimate — report the point value and
        # flag the interval as undefined rather than inventing a zero-width one.
        return {"mean": mean, "std": 0.0, "ci_lower": mean,
                "ci_upper": mean, "half_width": math.nan, "n": 1}

    std = float(arr.std(ddof=1))
    # Two-sided critical value: ppf(0.5 + confidence/2) == ppf(1 - α/2).
    t_crit = float(stats.t.ppf(0.5 + confidence / 2.0, df=n - 1))
    half = t_crit * std / math.sqrt(n)
    return {"mean": mean, "std": std, "ci_lower": mean - half,
            "ci_upper": mean + half, "half_width": half, "n": n}


def summarize_runs(long_df: pd.DataFrame, group_keys: Sequence[str], *,
                   value_col: str = "value", confidence: float = 0.95) -> pd.DataFrame:
    """Aggregate a tidy/long per-run frame into per-group ``mean ± CI``.

    ``long_df`` must carry the columns named in ``group_keys`` plus
    ``value_col``; each row is one run's observation of one (group) cell —
    e.g. one row per ``(side, phase, metric)`` per run. Returns one row per
    distinct group with the :data:`STAT_COLUMNS` appended, sorted by
    ``group_keys``.
    """
    group_keys = list(group_keys)
    empty_cols = [*group_keys, *STAT_COLUMNS]
    if long_df.empty:
        return pd.DataFrame(columns=empty_cols)

    rows: list[dict] = []
    for keys, sub in long_df.groupby(group_keys, dropna=False, sort=True):
        if not isinstance(keys, tuple):
            keys = (keys,)
        stat = mean_ci(sub[value_col].tolist(), confidence=confidence)
        rows.append({**dict(zip(group_keys, keys)), **stat})

    return pd.DataFrame(rows)[empty_cols]


def format_mean_ci(mean: float, half_width: float, n: int, *,
                   decimals: int = 1, unit: str = "") -> str:
    """Render a ``mean ± CI`` cell for a Markdown table.

    Falls back to ``"… (n=1)"`` when the interval is undefined (single run)
    and ``"—"`` when there is no data at all.
    """
    if n == 0 or (isinstance(mean, float) and math.isnan(mean)):
        return "—"
    suffix = f" {unit}" if unit else ""
    if n == 1 or (isinstance(half_width, float) and math.isnan(half_width)):
        return f"{mean:.{decimals}f}{suffix} (n=1)"
    return f"{mean:.{decimals}f} ± {half_width:.{decimals}f}{suffix}"
