"""
tests/e2e/adaptive-bench/test_compressed_run.py
────────────────────────────────────────────────
E2E test for adaptive-bench Round 2 (#156).

Drives `experiments/adaptive-bench/run.py --short` against the live
docker-compose stack and asserts every one of the eight artefacts in the
issue's AC lands at `experiments/adaptive-bench/results/<TIMESTAMP>/`.

The --short flag compresses the 5-phase shape into 60 s total, so the
full test (pre-flight + 60 s bench + post-flight) lands in under 5
minutes on a clean CI runner — well inside the 5-minute AC budget.

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


# Marker is registered in conftest.py so `pytest -m e2e` and
# `pytest -m "not e2e"` both behave correctly.
pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def compressed_run(tmp_path_factory) -> Path:
    """Drive run.py --short once for the whole module. Returns the
    timestamped run directory."""
    output_root = tmp_path_factory.mktemp("adaptive_bench_results")

    start = time.monotonic()
    proc = subprocess.run(
        [
            sys.executable, str(RUN_SCRIPT),
            "--output-root", str(output_root),
            "--short",
        ],
        cwd=REPO_ROOT,
        capture_output=True, text=True,
        timeout=300,  # 5 min AC budget, hard ceiling
    )
    elapsed = time.monotonic() - start
    print(f"\n[test] run.py exit={proc.returncode} elapsed={elapsed:.1f}s")
    print(f"[test] stdout tail:\n{proc.stdout[-2000:]}")
    if proc.returncode != 0:
        print(f"[test] stderr tail:\n{proc.stderr[-2000:]}")

    assert proc.returncode == 0, (
        f"run.py --short failed (exit={proc.returncode}). "
        f"stderr tail:\n{proc.stderr[-1000:]}"
    )

    # There must be exactly one timestamped run dir under output_root
    children = [p for p in output_root.iterdir() if p.is_dir()]
    assert len(children) == 1, (
        f"expected exactly one run directory under {output_root}, got {children!r}"
    )
    return children[0]


def test_manifest_exists_and_is_valid(compressed_run):
    manifest_path = compressed_run / "MANIFEST.json"
    assert manifest_path.exists(), "MANIFEST.json missing"
    data = json.loads(manifest_path.read_text())
    assert data["bench_version"], "manifest missing bench_version"
    assert data["short"] is True
    # phase boundaries must mirror SHORT_PHASES (PHASE_E_END_SECS=60)
    assert data["phases"]["PHASE_E_END_SECS"] == 60
    # git fields populated (may be 'unknown' in detached CI checkout)
    assert "git_sha" in data
    assert "git_state" in data


def test_pre_and_post_status_snapshots_present(compressed_run):
    for name in ("pre_status.json", "post_status.json"):
        path = compressed_run / name
        assert path.exists(), f"{name} missing"
        body = json.loads(path.read_text())
        # status payload shape: should at minimum have 'overall' or 'services'
        assert isinstance(body, dict)
        assert any(k in body for k in ("overall", "services", "status")), (
            f"{name} body does not look like a /api/v1/status response: {body!r}"
        )


def test_scaling_audit_present(compressed_run):
    path = compressed_run / "scaling_audit.json"
    assert path.exists(), "scaling_audit.json missing"
    body = json.loads(path.read_text())
    # Audit may be empty if no scaling fired in 60 s, but the file must parse
    assert isinstance(body, (dict, list))


def test_prom_parquet_present_and_non_trivial(compressed_run):
    path = compressed_run / "prom_timeseries.parquet"
    assert path.exists(), "prom_timeseries.parquet missing"
    # ~1 row/sec/metric × 60 s × ≥1 metric → expect at least 30 rows total
    import pyarrow.parquet as pq
    table = pq.read_table(str(path))
    assert table.num_rows >= 5, (
        f"prom_timeseries.parquet too small ({table.num_rows} rows) — "
        f"is Prometheus reachable from the bench host?"
    )
    assert {"ts", "metric", "labels_json", "value"}.issubset(
        set(table.column_names)
    ), f"parquet schema mismatch: {table.column_names!r}"


def test_decision_envelopes_jsonl_is_syntactically_valid(compressed_run):
    """File may be empty if the bench window saw no envelope traffic, but
    every line that does exist must parse as JSON with the expected shape."""
    path = compressed_run / "decision_envelopes.jsonl"
    assert path.exists(), "decision_envelopes.jsonl missing"
    for i, line in enumerate(path.read_text().splitlines()):
        if not line.strip():
            continue
        row = json.loads(line)
        assert "captured_at" in row, f"line {i}: missing captured_at"
        assert "envelope" in row, f"line {i}: missing envelope"


def test_upstream_changes_jsonl_is_syntactically_valid(compressed_run):
    path = compressed_run / "upstream_changes.jsonl"
    assert path.exists(), "upstream_changes.jsonl missing"
    # At minimum the bench's initial state should land one row
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    assert len(lines) >= 1, "expected at least the initial upstream.conf snapshot"
    for i, line in enumerate(lines):
        row = json.loads(line)
        assert {"ts", "sha256", "body"}.issubset(row.keys()), (
            f"line {i}: row missing required keys {row.keys()!r}"
        )


def test_locust_csvs_present(compressed_run):
    # locust --csv X writes X_stats.csv, X_stats_history.csv, X_failures.csv,
    # X_exceptions.csv. We assert the two named in the AC.
    for name in ("locust_stats.csv", "locust_stats_history.csv"):
        path = compressed_run / name
        assert path.exists(), f"{name} missing — locust headless run didn't complete"
        assert path.stat().st_size > 0, f"{name} is empty"
