"""
tests/unit/experiments/test_bench_stats.py
────────────────────────────────────────────
Unit tests for the shared multi-run statistics helper
(experiments/_bench_common/bench_stats.py, #160 / SOT §35.3).

Pure-function tests — no docker, no live stack. They pin the confidence-interval
maths both bench harnesses rely on, and the degradation behaviour for the
small-sample edges (N=1, N=0).
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd
import pytest
from scipy import stats

# Make experiments/_bench_common importable (same path trick the harnesses use).
_EXPERIMENTS = Path(__file__).resolve().parents[3] / "experiments"
if str(_EXPERIMENTS) not in sys.path:
    sys.path.insert(0, str(_EXPERIMENTS))

from _bench_common import bench_stats  # noqa: E402


# ── mean_ci ────────────────────────────────────────────────────────────────────

def test_mean_ci_matches_t_distribution_formula():
    vals = [8, 9, 10, 11, 12]
    r = bench_stats.mean_ci(vals, confidence=0.95)
    assert r["n"] == 5
    assert r["mean"] == pytest.approx(10.0)
    # sample std with ddof=1
    assert r["std"] == pytest.approx(1.5811388, rel=1e-5)
    # half = t(0.975, df=4) * s / sqrt(n)
    t_crit = stats.t.ppf(0.975, df=4)
    expected_half = t_crit * r["std"] / math.sqrt(5)
    assert r["half_width"] == pytest.approx(expected_half, rel=1e-9)
    assert r["ci_lower"] == pytest.approx(10.0 - expected_half)
    assert r["ci_upper"] == pytest.approx(10.0 + expected_half)
    assert r["ci_lower"] < r["mean"] < r["ci_upper"]


def test_mean_ci_single_run_has_no_interval():
    r = bench_stats.mean_ci([42.0])
    assert r["n"] == 1
    assert r["mean"] == 42.0
    assert r["std"] == 0.0
    assert r["ci_lower"] == r["ci_upper"] == 42.0
    assert math.isnan(r["half_width"])


def test_mean_ci_empty_is_all_nan():
    r = bench_stats.mean_ci([])
    assert r["n"] == 0
    assert all(math.isnan(r[k]) for k in ("mean", "std", "ci_lower", "ci_upper", "half_width"))


def test_mean_ci_drops_nans():
    r = bench_stats.mean_ci([10.0, float("nan"), 20.0])
    assert r["n"] == 2
    assert r["mean"] == pytest.approx(15.0)


def test_mean_ci_confidence_widens_interval():
    vals = [5, 6, 7, 8, 9, 10]
    narrow = bench_stats.mean_ci(vals, confidence=0.90)
    wide = bench_stats.mean_ci(vals, confidence=0.99)
    assert wide["half_width"] > narrow["half_width"]


# ── summarize_runs ──────────────────────────────────────────────────────────────

def test_summarize_runs_groups_and_orders():
    df = pd.DataFrame([
        {"side": "baseline", "phase": "A", "metric": "p95", "value": 100},
        {"side": "baseline", "phase": "A", "metric": "p95", "value": 110},
        {"side": "baseline", "phase": "A", "metric": "p95", "value": 120},
        {"side": "smartload", "phase": "A", "metric": "p95", "value": 80},
        {"side": "smartload", "phase": "A", "metric": "p95", "value": 84},
    ])
    out = bench_stats.summarize_runs(df, group_keys=["side", "phase", "metric"])
    assert list(out.columns) == ["side", "phase", "metric", *bench_stats.STAT_COLUMNS]
    assert len(out) == 2
    base = out[out["side"] == "baseline"].iloc[0]
    assert base["n"] == 3
    assert base["mean"] == pytest.approx(110.0)
    sl = out[out["side"] == "smartload"].iloc[0]
    assert sl["n"] == 2
    assert sl["mean"] == pytest.approx(82.0)


def test_summarize_runs_empty_returns_typed_empty():
    out = bench_stats.summarize_runs(pd.DataFrame(), group_keys=["phase", "metric"])
    assert out.empty
    assert list(out.columns) == ["phase", "metric", *bench_stats.STAT_COLUMNS]


# ── format_mean_ci ──────────────────────────────────────────────────────────────

def test_format_mean_ci_normal():
    assert bench_stats.format_mean_ci(110.0, 24.84, 3, decimals=0, unit="ms") == "110 ± 25 ms"


def test_format_mean_ci_single_run():
    assert bench_stats.format_mean_ci(42.0, float("nan"), 1, decimals=1, unit="ms") == "42.0 ms (n=1)"


def test_format_mean_ci_no_data():
    assert bench_stats.format_mean_ci(float("nan"), float("nan"), 0) == "—"


def test_format_mean_ci_no_unit():
    assert bench_stats.format_mean_ci(5.0, 1.0, 4, decimals=1) == "5.0 ± 1.0"
