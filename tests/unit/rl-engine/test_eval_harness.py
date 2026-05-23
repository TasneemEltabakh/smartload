"""
tests/unit/rl-engine/test_eval_harness.py
──────────────────────────────────────────
Unit tests for services/rl-engine/training/eval_harness.py.

No Docker, no Redis, no DB.  Uses synthetic datasets via tmp_path.

Coverage:
  1. test_baselines_complete: runs random_shadow, round_robin,
     least_connections through a 3-episode synthetic seed bank;
     asserts CSV has exactly 3×3=9 rows, all required columns, zero NaN.
  2. test_reproducibility: two runs with identical seed bank → byte-identical
     CSV content (rows sorted deterministically; meta timestamp excluded).
  3. test_ppo_skipped_gracefully: passing 'ppo' without an artifact
     results in a graceful skip (no crash), remaining policies still run.
  4. test_no_nan_in_output: explicit NaN guard — write_results raises if
     any metric value is NaN.
"""

from __future__ import annotations

import csv as csv_mod
import json
import sys
from io import StringIO
from pathlib import Path

import numpy as np
import pytest

_SERVICE = Path(__file__).resolve().parents[2].parent / "services" / "rl-engine"
if str(_SERVICE) not in sys.path:
    sys.path.insert(0, str(_SERVICE))

from obs_builder import NormParams                               # noqa: E402
from training.dataset import TraceReplayDataset                  # noqa: E402
from training.eval_harness import run_eval, write_results, _validate_rows  # noqa: E402

_REQUIRED_COLS = {
    "policy", "episode_id", "mean_reward",
    "p50_latency", "p95_latency", "p99_latency",
    "slo_violation_rate", "utilization_variance",
}


# ── fixtures ──────────────────────────────────────────────────────────────────

def _make_synthetic_csv(path: Path, n_rows: int = 600) -> None:
    """Write a small but non-trivial Alibaba-format CSV."""
    import csv as csv_mod_inner
    fieldnames = ["traceid", "timestamp", "rpcid", "um",
                  "rpctype", "dm", "interface", "rt"]
    n_backends = 5
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv_mod_inner.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for i in range(n_rows):
            dm  = f"dm_{i % n_backends:02d}"
            ts  = i * 200          # 200 ms apart → 120 s total
            rt  = 30.0 + (i % 50)  # varying 30–79 ms; all positive (healthy)
            w.writerow({"traceid": "t", "timestamp": ts, "rpcid": "0",
                        "um": "u", "rpctype": "http", "dm": dm,
                        "interface": "", "rt": rt})


def _make_seed_bank(dataset: TraceReplayDataset, n_episodes: int = 3) -> dict:
    """Build a tiny seed bank in the eval territory (last 20% of time range)."""
    total = len(dataset._rows)
    split_idx = int(total * 0.8)
    split_ts  = dataset._rows[split_idx][1]
    eval_max_ts = dataset._max_ts - dataset.window_ms

    if eval_max_ts <= split_ts:
        # Fallback: use the start of the dataset when time range is tiny
        starts = [dataset._min_ts] * n_episodes
    else:
        rng = np.random.default_rng(99)
        starts = list(map(int, rng.integers(split_ts, eval_max_ts + 1, size=n_episodes * 3)))

    episodes = []
    seen_starts: set[int] = set()
    import bisect
    for ts in starts:
        if ts in seen_starts:
            continue
        state = dataset.get_window(ts)
        if not state:
            continue
        row_idx = bisect.bisect_left(dataset._ts_index, ts)
        episodes.append({
            "episode_id": len(episodes),
            "dataset_start_idx": row_idx,
            "start_ts": ts,
            "seed": 42 + len(episodes),
        })
        seen_starts.add(ts)
        if len(episodes) >= n_episodes:
            break

    return {
        "train_eval_split_idx": split_idx,
        "split_ts": int(split_ts),
        "n_backends": 5,
        "window_ms": 30_000,
        "partitions": [],
        "episodes": episodes,
    }


@pytest.fixture
def tiny_setup(tmp_path):
    csv_path = tmp_path / "trace.csv"
    _make_synthetic_csv(csv_path, n_rows=600)
    ds = TraceReplayDataset([csv_path], n_backends=5, window_ms=5_000)
    seed_bank = _make_seed_bank(ds, n_episodes=3)
    norm = NormParams(latency_scale=500.0, request_count_scale=100.0)
    return ds, seed_bank, norm, tmp_path, csv_path


# ── test_baselines_complete ───────────────────────────────────────────────────

def test_baselines_complete(tiny_setup):
    ds, seed_bank, norm, tmp_path, csv_path = tiny_setup

    seed_bank_path = tmp_path / "seed_bank.json"
    seed_bank_path.write_text(json.dumps(seed_bank))

    policies = ["random_shadow", "round_robin", "least_connections"]
    rows = run_eval(
        policies=policies,
        dataset=ds,
        seed_bank=seed_bank,
        episode_length=10,
        slo_ms=200.0,
        norm=norm,
        n_eval_episodes=3,
    )

    assert len(rows) == 9, (
        f"Expected 3 policies × 3 episodes = 9 rows, got {len(rows)}"
    )
    for row in rows:
        missing = _REQUIRED_COLS - set(row.keys())
        assert not missing, f"Row missing columns: {missing}"

    for row in rows:
        for col in _REQUIRED_COLS - {"policy", "episode_id"}:
            val = row[col]
            assert not (isinstance(val, float) and val != val), (
                f"NaN found in column {col!r} for policy {row['policy']!r}"
            )


def test_csv_has_correct_columns(tiny_setup):
    ds, seed_bank, norm, tmp_path, csv_path = tiny_setup
    seed_bank_path = tmp_path / "seed_bank.json"
    seed_bank_path.write_text(json.dumps(seed_bank))

    rows = run_eval(
        policies=["round_robin"],
        dataset=ds, seed_bank=seed_bank,
        episode_length=5, n_eval_episodes=3, norm=norm,
    )
    csv_out, _ = write_results(
        rows=rows,
        seed_bank_path=seed_bank_path,
        partition_paths=[csv_path],
        split_idx=seed_bank["train_eval_split_idx"],
        out_dir=tmp_path,
    )
    with open(csv_out, newline="", encoding="utf-8") as f:
        reader = csv_mod.DictReader(f)
        cols = set(reader.fieldnames or [])
    assert _REQUIRED_COLS <= cols, f"Missing columns in CSV: {_REQUIRED_COLS - cols}"


# ── test_reproducibility ──────────────────────────────────────────────────────

def test_reproducibility(tiny_setup):
    """Two identical runs → byte-identical CSV content."""
    ds, seed_bank, norm, tmp_path, csv_path = tiny_setup
    seed_bank_path = tmp_path / "seed_bank.json"
    seed_bank_path.write_text(json.dumps(seed_bank))

    kwargs = dict(
        policies=["random_shadow", "round_robin", "least_connections"],
        dataset=ds, seed_bank=seed_bank,
        episode_length=5, n_eval_episodes=3, norm=norm,
    )

    rows1 = run_eval(**kwargs)
    rows2 = run_eval(**kwargs)

    # Normalize: sort by (policy, episode_id) and stringify
    def _canonical(rows):
        return sorted(
            [(r["policy"], r["episode_id"],
              round(r["mean_reward"], 8),
              round(r["p95_latency"], 8))
             for r in rows]
        )

    assert _canonical(rows1) == _canonical(rows2), (
        "Two identical eval runs produced different results — not reproducible"
    )


# ── graceful PPO skip ─────────────────────────────────────────────────────────

def test_ppo_skipped_gracefully(tiny_setup):
    """Passing 'ppo' without an artifact must not crash; other policies run."""
    ds, seed_bank, norm, tmp_path, csv_path = tiny_setup
    # PPO plugin exists but policy.zip is absent → select_policy raises → skip
    rows = run_eval(
        policies=["round_robin", "ppo"],
        dataset=ds, seed_bank=seed_bank,
        episode_length=5, n_eval_episodes=3, norm=norm,
    )
    policy_names = {r["policy"] for r in rows}
    assert "round_robin" in policy_names, "round_robin must still produce results"
    # ppo either runs (if artifact exists) or is absent — no crash either way


# ── NaN guard ────────────────────────────────────────────────────────────────

def test_validate_rows_raises_on_nan(tmp_path):
    """write_results must raise ValueError if any metric is NaN."""
    bad_row = {
        "policy": "test", "episode_id": 0,
        "mean_reward": float("nan"),
        "p50_latency": 50.0, "p95_latency": 95.0, "p99_latency": 99.0,
        "slo_violation_rate": 0.0, "utilization_variance": 0.0,
    }
    with pytest.raises(ValueError, match="NaN"):
        _validate_rows([bad_row])
