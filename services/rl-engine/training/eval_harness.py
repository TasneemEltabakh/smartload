"""
services/rl-engine/training/eval_harness.py
─────────────────────────────────────────────
Evaluation harness for SmartLoad routing policies.

Runs a set of policies against fixed held-out episodes from eval_seed_bank.json
and produces reproducible benchmark results.

Usage (baseline-only, no PPO artifact needed):
    python training/eval_harness.py \\
        --policies random_shadow round_robin least_connections \\
        --partitions datasets/alibaba/*.csv \\
        --seed-bank services/rl-engine/training/eval_seed_bank.json \\
        --episode-length 200 \\
        --slo-ms 200

Outputs (in current directory):
    eval_results_<8-char-git-sha>.csv
    eval_meta_<8-char-git-sha>.json

CSV columns:
    policy, episode_id, mean_reward, p50_latency, p95_latency, p99_latency,
    slo_violation_rate, utilization_variance

Reproducibility contract:
    Given identical seed bank and dataset partitions, two runs on the same
    codebase produce byte-identical CSVs (excluding timestamps in meta JSON).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

_RL_ENGINE = Path(__file__).resolve().parents[1]
if str(_RL_ENGINE) not in sys.path:
    sys.path.insert(0, str(_RL_ENGINE))

from obs_builder import NormParams                          # noqa: E402
from policy_base import BackendState, select_policy        # noqa: E402
from training.dataset import TraceReplayDataset            # noqa: E402
from training.reward import RewardCalculator               # noqa: E402
from training.simulator import BackendSimulator            # noqa: E402

# Default NormParams — matches env.py defaults; updated when artifact_meta.json
# is available (PPO evaluation path).
_DEFAULT_NORM = NormParams(latency_scale=2000.0, request_count_scale=200.0)
_SLO_DEFAULT_MS = 200.0
_EPSILON = 1.0


# ── core evaluation ───────────────────────────────────────────────────────────

def run_eval(
    policies: list[str],
    dataset: TraceReplayDataset,
    seed_bank: dict,
    episode_length: int = 200,
    slo_ms: float = _SLO_DEFAULT_MS,
    norm: NormParams = _DEFAULT_NORM,
    n_eval_episodes: int = 20,
) -> list[dict[str, Any]]:
    """Run all policies over the first n_eval_episodes seed-bank entries.

    Returns a list of row dicts (one per policy × episode).
    """
    reward_calc = RewardCalculator(norm=norm)
    episodes = seed_bank["episodes"][:n_eval_episodes]
    rows: list[dict[str, Any]] = []

    for policy_name in policies:
        for ep in episodes:
            episode_id   = ep["episode_id"]
            start_ts     = ep["start_ts"]
            seed         = ep["seed"]

            # Fresh policy per episode — stochastic policies (random_shadow)
            # are seeded with the episode seed for reproducibility.
            try:
                policy = select_policy(policy_name, seed=seed)
            except TypeError:
                try:
                    policy = select_policy(policy_name)
                except Exception as exc:
                    print(f"[eval_harness] skipping {policy_name!r}: {exc}", flush=True)
                    break
            except Exception as exc:
                print(f"[eval_harness] skipping {policy_name!r}: {exc}", flush=True)
                break

            sim = BackendSimulator(dataset, episode_length=episode_length)
            # Reset to the fixed episode start
            sim._rng = np.random.default_rng(seed)
            sim._current_ts = start_ts
            sim._step_count = 0
            state = dataset.get_window(start_ts)

            step_latencies:  list[float] = []
            step_utilvar:    list[float] = []
            step_rewards:    list[float] = []

            for _ in range(episode_length):
                if not state:
                    break
                action = policy.act(state)
                # Chosen backend = highest score in rankings
                if action.rankings:
                    chosen_id = max(action.rankings, key=lambda r: r.score).backend_id
                    sorted_state = sorted(state, key=lambda s: s.backend_id)
                    chosen_idx = next(
                        (i for i, s in enumerate(sorted_state) if s.backend_id == chosen_id),
                        0,
                    )
                else:
                    chosen_idx = 0

                next_state, done = sim.step(chosen_idx)
                reward = reward_calc.compute(state, chosen_idx, next_state)

                # Latency of chosen backend in next_state (credit assignment)
                if next_state:
                    sorted_next = sorted(next_state, key=lambda s: s.backend_id)
                    chosen_next = sorted_next[min(chosen_idx, len(sorted_next) - 1)]
                    step_latencies.append(chosen_next.latency_ms)
                    counts = np.array([s.queue_depth for s in sorted_next], dtype=float)
                    util_var = counts.std() / (counts.mean() + _EPSILON)
                    step_utilvar.append(float(util_var))

                step_rewards.append(reward)
                state = next_state
                if done:
                    break

            if not step_latencies:
                continue

            lat = np.array(step_latencies, dtype=float)
            rows.append({
                "policy":              policy_name,
                "episode_id":          episode_id,
                "mean_reward":         float(np.mean(step_rewards)),
                "p50_latency":         float(np.percentile(lat, 50)),
                "p95_latency":         float(np.percentile(lat, 95)),
                "p99_latency":         float(np.percentile(lat, 99)),
                "slo_violation_rate":  float(np.mean(lat > slo_ms)),
                "utilization_variance":float(np.mean(step_utilvar)),
            })

    return rows


# ── output helpers ────────────────────────────────────────────────────────────

def _git_sha(length: int = 8) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", f"HEAD"],
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()[:length]
    except Exception:
        return "unknown"


def _file_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _validate_rows(rows: list[dict]) -> None:
    required = {
        "policy", "episode_id", "mean_reward",
        "p50_latency", "p95_latency", "p99_latency",
        "slo_violation_rate", "utilization_variance",
    }
    for i, row in enumerate(rows):
        missing = required - set(row.keys())
        if missing:
            raise ValueError(f"Row {i} missing columns: {missing}")
        for col, val in row.items():
            if col in {"policy", "episode_id"}:
                continue
            if isinstance(val, float) and (val != val):  # NaN check
                raise ValueError(f"Row {i} column {col!r} is NaN")


def write_results(
    rows: list[dict],
    seed_bank_path: Path,
    partition_paths: list[Path],
    split_idx: int,
    out_dir: Path = Path("."),
) -> tuple[Path, Path]:
    """Write eval_results_<sha>.csv and eval_meta_<sha>.json.

    Returns (csv_path, meta_path).
    """
    import csv as csv_mod

    _validate_rows(rows)

    sha = _git_sha()
    csv_path  = out_dir / f"eval_results_{sha}.csv"
    meta_path = out_dir / f"eval_meta_{sha}.json"

    cols = [
        "policy", "episode_id", "mean_reward",
        "p50_latency", "p95_latency", "p99_latency",
        "slo_violation_rate", "utilization_variance",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv_mod.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        # Sort rows for deterministic output (policy ASC, episode_id ASC)
        for row in sorted(rows, key=lambda r: (r["policy"], r["episode_id"])):
            w.writerow(row)

    meta = {
        "git_sha": sha,
        "eval_date": datetime.now(timezone.utc).isoformat(),
        "seed_bank_sha": _file_md5(seed_bank_path),
        "dataset_partition_hashes": {
            p.name: _file_md5(p) for p in sorted(partition_paths)
        },
        "train_eval_split_idx": split_idx,
        "n_rows": len(rows),
    }
    meta_path.write_text(json.dumps(meta, indent=2))

    return csv_path, meta_path


# ── CLI entry point ───────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SmartLoad policy eval harness")
    p.add_argument("--policies", nargs="+",
                   default=["random_shadow", "round_robin", "least_connections"])
    p.add_argument("--partitions", nargs="+",
                   default=sorted(str(x) for x in
                                  Path("datasets/alibaba").glob("*.csv")))
    p.add_argument("--seed-bank",
                   default="services/rl-engine/training/eval_seed_bank.json")
    p.add_argument("--episode-length", type=int, default=200)
    p.add_argument("--slo-ms", type=float, default=_SLO_DEFAULT_MS)
    p.add_argument("--n-eval-episodes", type=int, default=20)
    p.add_argument("--out-dir", default=".")
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    seed_bank_path = Path(args.seed_bank)
    seed_bank = json.loads(seed_bank_path.read_text())

    partition_paths = [Path(p) for p in args.partitions]
    print(f"[eval_harness] loading dataset ({len(partition_paths)} partitions)...",
          flush=True)
    dataset = TraceReplayDataset(
        partition_paths, n_backends=seed_bank.get("n_backends", 5),
        window_ms=seed_bank.get("window_ms", 30_000),
    )

    print(f"[eval_harness] evaluating policies: {args.policies}", flush=True)
    rows = run_eval(
        policies=args.policies,
        dataset=dataset,
        seed_bank=seed_bank,
        episode_length=args.episode_length,
        slo_ms=args.slo_ms,
        n_eval_episodes=args.n_eval_episodes,
    )

    csv_path, meta_path = write_results(
        rows=rows,
        seed_bank_path=seed_bank_path,
        partition_paths=partition_paths,
        split_idx=seed_bank["train_eval_split_idx"],
        out_dir=Path(args.out_dir),
    )
    print(f"[eval_harness] wrote {csv_path}  ({len(rows)} rows)", flush=True)
    print(f"[eval_harness] wrote {meta_path}", flush=True)


if __name__ == "__main__":
    main()
