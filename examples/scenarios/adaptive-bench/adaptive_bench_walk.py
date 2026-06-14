"""
examples/scenarios/adaptive-bench/adaptive_bench_walk.py
─────────────────────────────────────────────────────────
Operator-runnable walk for the adaptive-bench delivery artefact (#156 / #157 /
#160). The adaptive-bench is the dynamic load harness that drives the live
stack through a multi-phase RPS shape and records what SmartLoad did (pool
size over time, decision envelopes, upstream changes) so a run can be scored
against a baseline.

This scenario does NOT re-implement the bench — it demonstrates it:

  - default: locate the most recent batch under
    experiments/adaptive-bench/results/, print SUMMARY.md, and confirm the
    expected artefacts are present (per-run folders + aggregated
    summary.parquet + SUMMARY.md).
  - --run: trigger a fresh short batch via experiments/adaptive-bench/run.py
    --short (needs the live docker-compose stack + bench deps), then inspect it.

Exit code:
  0 — a batch was found/produced and carries the expected artefacts
  1 — no results to inspect (and --run not given or the run failed), or a
      batch is missing expected artefacts

Usage:
  python examples/scenarios/adaptive-bench/adaptive_bench_walk.py
  python examples/scenarios/adaptive-bench/adaptive_bench_walk.py --run --runs 2
  python examples/scenarios/adaptive-bench/adaptive_bench_walk.py \\
      --results-dir experiments/adaptive-bench/results/20260612T162342Z

This scenario satisfies the per-feature triad for tests/e2e/adaptive-bench/.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BENCH_ROOT = _REPO_ROOT / "experiments" / "adaptive-bench"
_DEFAULT_RESULTS = _BENCH_ROOT / "results"


def _latest_batch(results_root: Path) -> Path | None:
    """Most recent batch folder under results_root (newest mtime), or None."""
    if not results_root.is_dir():
        return None
    batches = [p for p in results_root.iterdir() if p.is_dir()]
    if not batches:
        return None
    return max(batches, key=lambda p: p.stat().st_mtime)


def _trigger_run(runs: int) -> int:
    """Run a fresh short batch. Returns the subprocess exit code."""
    cmd = [
        sys.executable, str(_BENCH_ROOT / "run.py"),
        "--output-root", str(_DEFAULT_RESULTS),
        "--runs", str(runs), "--short",
    ]
    print(f"  $ {' '.join(cmd)}")
    print("  (needs the live docker-compose stack + bench deps; this is the long leg)")
    return subprocess.call(cmd)


def _inspect(batch: Path) -> bool:
    """Print the batch summary + confirm expected artefacts. True iff complete."""
    print(f"\nInspecting batch: {batch.relative_to(_REPO_ROOT)}")

    run_dirs = sorted(p for p in batch.iterdir() if p.is_dir() and p.name.startswith("run-"))
    summary_md = batch / "SUMMARY.md"
    summary_parquet = batch / "summary.parquet"

    print(f"  per-run folders : {len(run_dirs)} ({', '.join(p.name for p in run_dirs) or 'none'})")
    print(f"  summary.parquet : {'present' if summary_parquet.is_file() else 'MISSING'}")
    print(f"  SUMMARY.md      : {'present' if summary_md.is_file() else 'MISSING'}")

    if summary_md.is_file():
        print("\n--- SUMMARY.md ---")
        text = summary_md.read_text(encoding="utf-8").splitlines()
        for line in text[:40]:
            print(f"  {line}")
        if len(text) > 40:
            print(f"  … ({len(text) - 40} more lines)")

    complete = bool(run_dirs) and summary_md.is_file() and summary_parquet.is_file()
    if not complete:
        print("\nBatch is missing expected artefacts (per-run folders + summary.parquet + SUMMARY.md).")
    return complete


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Adaptive-bench walk")
    parser.add_argument("--run", action="store_true", help="trigger a fresh short batch first")
    parser.add_argument("--runs", type=int, default=1, help="runs in the fresh batch (with --run)")
    parser.add_argument("--results-dir", default=None, help="inspect this batch instead of the latest")
    args = parser.parse_args(argv)

    print("=== adaptive-bench walk ===")

    if args.results_dir:
        batch = Path(args.results_dir)
        if not batch.is_absolute():
            batch = _REPO_ROOT / batch
        if not batch.is_dir():
            print(f"no such batch: {batch}")
            return 1
        return 0 if _inspect(batch) else 1

    if args.run:
        rc = _trigger_run(args.runs)
        if rc != 0:
            print(f"\nbench run failed (exit {rc})")
            return 1

    batch = _latest_batch(_DEFAULT_RESULTS)
    if batch is None:
        print(f"\nno batches under {_DEFAULT_RESULTS.relative_to(_REPO_ROOT)} — "
              "re-run with --run to produce one (requires the live stack), or pass "
              "--results-dir.")
        return 1

    return 0 if _inspect(batch) else 1


if __name__ == "__main__":
    raise SystemExit(main())
