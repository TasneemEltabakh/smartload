"""
tests/e2e/adaptive-bench/test_compressed_run.py
────────────────────────────────────────────────
E2E test for adaptive-bench multi-run batching (#156 R2 + #160 §35.3).

Drives `experiments/adaptive-bench/run.py --runs 2 --short` against the live
docker-compose stack and asserts:
  - the batch lands two per-run folders (run-01, run-02), each carrying the
    raw artefacts from the issue's R2 AC,
  - the batch top level carries the aggregated `summary.parquet` + `SUMMARY.md`
    with per-metric mean ± CI (a `±` token for the n=2 cells).

The --short flag compresses each run's 5-phase shape into 60 s, so two runs +
once-per-batch pre/post-flight + the join/aggregate/plot analysis land well
inside the timeout below on a clean CI runner.

Prerequisite (CI's compose-test job sets this up):
  - docker compose up -d before invoking pytest
  - bench dependencies installed into the test env:
      python -m pip install -r experiments/adaptive-bench/requirements-bench.txt
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
RUN_SCRIPT = REPO_ROOT / "experiments" / "adaptive-bench" / "run.py"

RUNS = 2

# Marker is registered in conftest.py so `pytest -m e2e` and
# `pytest -m "not e2e"` both behave correctly.
pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def batch_run(tmp_path_factory) -> Path:
    """Drive run.py --runs 2 --short once for the whole module. Returns the
    timestamped batch directory."""
    output_root = tmp_path_factory.mktemp("adaptive_bench_results")

    start = time.monotonic()
    proc = subprocess.run(
        [
            sys.executable, str(RUN_SCRIPT),
            "--output-root", str(output_root),
            "--runs", str(RUNS),
            "--short",
        ],
        cwd=REPO_ROOT,
        capture_output=True, text=True,
        timeout=600,  # 2 short runs + once-per-batch pre/post-flight + analysis
    )
    elapsed = time.monotonic() - start
    print(f"\n[test] run.py exit={proc.returncode} elapsed={elapsed:.1f}s")
    print(f"[test] stdout tail:\n{proc.stdout[-2000:]}")
    if proc.returncode != 0:
        print(f"[test] stderr tail:\n{proc.stderr[-2000:]}")

    assert proc.returncode == 0, (
        f"run.py --runs {RUNS} --short failed (exit={proc.returncode}). "
        f"stderr tail:\n{proc.stderr[-1000:]}"
    )

    # There must be exactly one timestamped batch directory under output_root.
    children = [p for p in output_root.iterdir() if p.is_dir()]
    assert len(children) == 1, (
        f"expected exactly one batch directory under {output_root}, got {children!r}"
    )
    return children[0]


@pytest.fixture(scope="module")
def run_dirs(batch_run) -> list[Path]:
    dirs = sorted(p for p in batch_run.glob("run-*") if p.is_dir())
    assert [d.name for d in dirs] == ["run-01", "run-02"], (
        f"expected run-01 + run-02 under {batch_run}, got {[d.name for d in dirs]!r}"
    )
    return dirs


def test_per_run_manifests_valid_and_seeded(run_dirs):
    seeds = []
    for i, rd in enumerate(run_dirs, start=1):
        manifest_path = rd / "MANIFEST.json"
        assert manifest_path.exists(), f"{rd.name}/MANIFEST.json missing"
        data = json.loads(manifest_path.read_text())
        assert data["bench_version"], "manifest missing bench_version"
        assert data["short"] is True
        assert data["phases"]["PHASE_E_END_SECS"] == 60
        assert data["run_index"] == i
        assert data["runs_total"] == RUNS
        assert "git_sha" in data and "git_state" in data
        seeds.append(data["seed"])
    # Each run uses a distinct, independently-seeded RNG path (#160 AC).
    assert len(set(seeds)) == RUNS, f"runs must be independently seeded; got {seeds!r}"


def test_each_run_has_raw_artefacts(run_dirs):
    for rd in run_dirs:
        for name in ("pre_status.json", "post_status.json", "scaling_audit.json",
                     "prom_timeseries.parquet", "decision_envelopes.jsonl",
                     "upstream_changes.jsonl", "locust_stats.csv",
                     "locust_stats_history.csv"):
            path = rd / name
            assert path.exists(), f"{rd.name}/{name} missing"
        # The locust CSVs must be non-empty (the run actually produced load).
        assert (rd / "locust_stats.csv").stat().st_size > 0
        assert (rd / "locust_stats_history.csv").stat().st_size > 0


def test_prom_parquet_non_trivial(run_dirs):
    import pyarrow.parquet as pq
    for rd in run_dirs:
        table = pq.read_table(str(rd / "prom_timeseries.parquet"))
        assert table.num_rows >= 5, (
            f"{rd.name}/prom_timeseries.parquet too small ({table.num_rows} rows) — "
            f"is Prometheus reachable from the bench host?"
        )
        assert {"ts", "metric", "labels_json", "value"}.issubset(set(table.column_names))


def test_batch_summary_parquet_present(batch_run):
    import pyarrow.parquet as pq
    path = batch_run / "summary.parquet"
    assert path.exists(), "batch summary.parquet missing — aggregation did not run"
    table = pq.read_table(str(path))
    cols = set(table.column_names)
    assert {"phase", "metric", "mean", "ci_lower", "ci_upper", "n"}.issubset(cols), (
        f"summary.parquet schema mismatch: {table.column_names!r}"
    )
    assert table.num_rows >= 1, "summary.parquet is empty"


def test_batch_summary_md_reports_confidence_interval(batch_run):
    path = batch_run / "SUMMARY.md"
    assert path.exists(), "batch SUMMARY.md missing"
    text = path.read_text(encoding="utf-8")
    # With two runs at least one (phase, metric) cell carries a ± CI.
    assert "±" in text, "SUMMARY.md should report at least one mean ± CI cell for a 2-run batch"
    assert f"{RUNS} run(s)" in text
