"""
services/rl-engine/training/train_dqn.py
─────────────────────────────────────────
DQN fallback training entry point for SmartLoad routing policy.

Activated when the PPO canary (train_ppo.py run_canary) fails both gates.
DQN is a simpler off-policy algorithm that may converge where PPO diverges
on shorter horizon tasks.

Usage:
    python training/train_dqn.py

Outputs (written to services/rl-engine/models/):
    policy_dqn.zip       — SB3 DQN artifact
    artifact_meta_dqn.json

Action masking:
    SB3's DQN does not natively support action masking.  We use a thin
    wrapper (_MaskedActionEnv) that intercepts invalid actions and redirects
    them to the first valid action before calling env.step().  This ensures
    the agent never routes to an unhealthy backend even without native mask
    support.

Acceptance criterion (N2.4 fallback):
    policy_dqn.zip loads without error and produces a RoutingAction-compatible
    output.  Formal eval (must beat round_robin + least_connections) runs
    after PPOPolicy/DQNPolicy serving plugin is built in N2.5.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

_RL_ENGINE = Path(__file__).resolve().parents[1]
if str(_RL_ENGINE) not in sys.path:
    sys.path.insert(0, str(_RL_ENGINE))

from obs_builder import N_MAX_BACKENDS, NormParams, all_masked_fallback  # noqa: E402
from training.dataset import TraceReplayDataset    # noqa: E402
from training.env import SmartLoadEnv              # noqa: E402
from training.simulator import DEFAULT_EPISODE_LENGTH  # noqa: E402

# ── constants ──────────────────────────────────────────────────────────────────

_MODELS_DIR   = _RL_ENGINE / "models"
_ALIBABA_DIR  = _RL_ENGINE.parents[1] / "datasets" / "alibaba"
_DEFAULT_NORM = NormParams(latency_scale=2000.0, request_count_scale=200.0)
_FULL_STEPS   = 500_000   # DQN is sample-efficient; fewer steps than PPO


# ── action-masking env wrapper ─────────────────────────────────────────────────

def _make_masked_action_env(base_env: SmartLoadEnv):
    """Wrap SmartLoadEnv so invalid DQN actions are silently redirected."""
    import gymnasium as gym

    class _MaskedActionEnv(gym.Wrapper):
        """Redirect masked (unhealthy-backend) actions to the first valid one.

        DQN does not call action_masks(); we intercept at step() instead.
        The agent may still CHOOSE a masked action, but it is rerouted before
        the simulator sees it — meaning the agent observes the penalty-free
        consequence of the valid action.  Over time this shapes the Q-values
        away from masked actions.
        """

        def step(self, action: int):
            mask = self.env.action_masks()
            if not mask.any():
                mask = all_masked_fallback(self.env._state, N_MAX_BACKENDS)
            if not bool(mask[action]):
                valid = np.where(mask)[0]
                action = int(valid[0])
            return self.env.step(action)

        def reset(self, **kwargs):
            return self.env.reset(**kwargs)

    return _MaskedActionEnv(base_env)


# ── norm params (same as train_ppo) ───────────────────────────────────────────

def _compute_norm_params(ds: TraceReplayDataset) -> NormParams:
    all_latencies = [rt for _, _, rt in ds._rows if rt >= 0]
    lat_p99   = float(np.percentile(all_latencies, 99)) if all_latencies else 2000.0
    lat_scale = max(lat_p99, 100.0)

    rng      = np.random.default_rng(42)
    n_sample = min(200, ds.n_windows)
    ts_vals  = rng.integers(ds._min_ts, max(ds._min_ts + 1, ds._max_ts - ds.window_ms), size=n_sample)
    counts: list[float] = []
    for ts in ts_vals:
        for s in ds.get_window(int(ts)):
            counts.append(float(s.queue_depth))
    count_p99   = float(np.percentile(counts, 99)) if counts else 200.0
    count_scale = max(count_p99, 10.0)

    print(
        f"[train_dqn] norm_params: latency_scale={lat_scale:.1f}, "
        f"request_count_scale={count_scale:.1f}",
        flush=True,
    )
    return NormParams(latency_scale=lat_scale, request_count_scale=count_scale)


# ── main ──────────────────────────────────────────────────────────────────────

def main(
    partitions: list[Path] | None = None,
    steps: int = _FULL_STEPS,
    episode_length: int = DEFAULT_EPISODE_LENGTH,
    out_dir: Path = _MODELS_DIR,
) -> None:
    """Train DQN and write policy_dqn.zip + artifact_meta_dqn.json."""
    try:
        import stable_baselines3 as _sb3
        from stable_baselines3 import DQN
    except ImportError as exc:
        raise ImportError(
            "stable-baselines3 not installed.  Run:\n"
            "  pip install -r training/requirements-training.txt"
        ) from exc

    if partitions is None:
        partitions = sorted(_ALIBABA_DIR.glob("*.csv"))
    if not partitions:
        raise FileNotFoundError(f"No Alibaba CSV files found at {_ALIBABA_DIR}")

    print(f"[train_dqn] loading dataset ({len(partitions)} partitions)...", flush=True)
    ds   = TraceReplayDataset(partitions, n_backends=N_MAX_BACKENDS)
    norm = _compute_norm_params(ds)

    base_env = SmartLoadEnv(dataset=ds, norm=norm, episode_length=episode_length)
    env      = _make_masked_action_env(base_env)

    model = DQN(
        "MlpPolicy",
        env,
        verbose=1,
        learning_rate=1e-3,
        buffer_size=50_000,
        learning_starts=1_000,
        batch_size=64,
        gamma=0.99,
        train_freq=4,
        target_update_interval=1_000,
        exploration_fraction=0.2,
        exploration_final_eps=0.05,
        seed=42,
        tensorboard_log=None,
    )

    print(f"[train_dqn] training DQN for {steps:,} steps...", flush=True)
    model.learn(total_timesteps=steps)

    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / "policy_dqn"
    model.save(str(model_path))
    print(f"[train_dqn] saved {model_path}.zip", flush=True)

    mean_eval_reward = (
        float(np.mean([e["r"] for e in model.ep_info_buffer]))
        if model.ep_info_buffer
        else 0.0
    )

    meta: dict[str, Any] = {
        "policy_type":       "dqn",
        "training_date":     datetime.now(timezone.utc).isoformat(),
        "n_max_backends":    N_MAX_BACKENDS,
        "norm_params":       norm.to_dict(),
        "reward_lambda":     0.1,
        "episode_length":    episode_length,
        "training_steps":    steps,
        "mean_eval_reward":  mean_eval_reward,
        "sb3_version":       _sb3.__version__,
        "dataset_partitions": [p.name for p in partitions],
        "note":              "DQN fallback — activated because PPO canary failed",
    }
    meta_path = out_dir / "artifact_meta_dqn.json"
    meta_path.write_text(json.dumps(meta, indent=2))
    print(f"[train_dqn] wrote {meta_path}", flush=True)
    print(f"[train_dqn] mean_eval_reward (training buffer): {mean_eval_reward:.4f}", flush=True)
    print("[train_dqn] done.", flush=True)


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="SmartLoad DQN fallback training")
    p.add_argument("--partitions", nargs="+", default=None)
    p.add_argument("--steps", type=int, default=_FULL_STEPS)
    p.add_argument("--episode-length", type=int, default=DEFAULT_EPISODE_LENGTH)
    p.add_argument("--out-dir", default=str(_MODELS_DIR))
    args = p.parse_args()

    main(
        partitions=[Path(x) for x in args.partitions] if args.partitions else None,
        steps=args.steps,
        episode_length=args.episode_length,
        out_dir=Path(args.out_dir),
    )
