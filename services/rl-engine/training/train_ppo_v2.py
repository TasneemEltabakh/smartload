"""
services/rl-engine/training/train_ppo_v2.py
────────────────────────────────────────────
Continuous PPO trainer for the closed-loop routing MDP (env_v2 + closed_loop_sim).

This replaces the open-loop MaskablePPO/Discrete pipeline (train_ppo.py) for the
causal retrain:
  • Algorithm: stable_baselines3.PPO (continuous Box action = weight vector),
    NOT sb3-contrib MaskablePPO (which is for discrete single-pick).
  • Reward/dynamics are causal (see env_v2 / reward_v2 / closed_loop_sim).
  • No Alibaba dataset needed — demand + heterogeneity are synthesised, so this
    runs anywhere without re-fetching the trace.

Outputs to services/rl-engine/models/ (or --out-dir):
  policy_v2.zip         — SB3 PPO artifact
  artifact_meta.json    — with policy_kind="continuous_weights" so the serving
                          plugin knows to load PPO (not MaskablePPO) and emit the
                          softmax weight vector instead of argmax-dominant 0.7.

Promotion gate (printed, not auto-applied): PPO must beat round-robin (uniform)
mean reward on held-out seeds. The full homogeneous/degraded gates live in
eval_gates_v2.py.

Usage:
  python training/train_ppo_v2.py --smoke           # 30k steps, quick wiring check
  python training/train_ppo_v2.py --steps 400000    # real run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

_RL_ENGINE = Path(__file__).resolve().parents[1]
if str(_RL_ENGINE) not in sys.path:
    sys.path.insert(0, str(_RL_ENGINE))

# Quiet the all-unhealthy overload-window warnings during training.
logging.getLogger("obs_builder").setLevel(logging.ERROR)

from obs_builder import N_MAX_BACKENDS                       # noqa: E402
from training.env_v2 import SmartLoadEnvV2, DEFAULT_NORM  # noqa: E402
from training.reward_v2 import RewardConfig  # noqa: E402

_MODELS_DIR = _RL_ENGINE / "models"
_ACTION_BOUND = 10.0


# ── baselines (fixed routing policies, for the gate) ───────────────────────────

def uniform_action(obs):
    """Round-robin / even split."""
    return np.zeros(N_MAX_BACKENDS, dtype=np.float32)


def inv_latency_action(obs):
    """Least-connections / capacity-aware proxy: weight ∝ low observed latency."""
    lat = np.asarray(obs, dtype=float).reshape(N_MAX_BACKENDS, 3)[:, 0]
    return (-lat * 8.0).astype(np.float32)


# ── evaluation ─────────────────────────────────────────────────────────────────

def eval_policy(action_fn, seeds, episode_length=128, reward_cfg=None):
    """Run a callable obs->action over held-out seeds; return mean reward/step,
    mean served latency (ms), and mean shed fraction."""
    reward_cfg = reward_cfg or RewardConfig()
    rewards, lats, sheds = [], [], []
    for s in seeds:
        env = SmartLoadEnvV2(episode_length=episode_length, reward_cfg=reward_cfg)
        obs, _ = env.reset(seed=int(s))
        done = False
        while not done:
            obs, r, done, _, info = env.step(action_fn(obs))
            rewards.append(r)
            lats.append(info["served_lat_ms"])
            sheds.append(info["shed"])
    return {
        "mean_reward": float(np.mean(rewards)),
        "mean_served_latency_ms": float(np.mean(lats)),
        "mean_shed": float(np.mean(sheds)),
    }


def model_action_fn(model):
    """Wrap an SB3 model into a deterministic obs->action callable."""
    def fn(obs):
        a, _ = model.predict(np.asarray(obs, dtype=np.float32), deterministic=True)
        return a
    return fn


# ── training ───────────────────────────────────────────────────────────────────

def main(steps: int, out_dir: Path, episode_length: int = 128, seed: int = 42) -> dict:
    from stable_baselines3 import PPO
    from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize

    norm = DEFAULT_NORM
    reward_cfg = RewardConfig()

    def make_env():
        return SmartLoadEnvV2(episode_length=episode_length, norm=norm, reward_cfg=reward_cfg)

    # Routing here is effectively a CONTEXTUAL BANDIT: the queueing consequence
    # of a window's weights lands in that same window's reward, so there is no
    # long-horizon credit to assign. gamma=0 makes the advantage = reward -
    # V(obs), which stops the value fn from chasing the unobservable random
    # demand and drowning the routing signal in noise (the failure mode of the
    # gamma=0.95 run: the policy collapsed to state-blind near-uniform).
    # VecNormalize(norm_reward) stabilises the spiky shed-penalty scale. obs is
    # NOT normalised here — build_observation already normalises it, and that is
    # the only obs transform serving applies, so train/serve parity is kept.
    vec = VecNormalize(DummyVecEnv([make_env]), norm_obs=False, norm_reward=True, gamma=0.0)
    model = PPO(
        "MlpPolicy", vec, verbose=0, seed=seed,
        learning_rate=3e-4, n_steps=1024, batch_size=128, n_epochs=10,
        gamma=0.0, gae_lambda=1.0, ent_coef=0.01,
    )
    print(f"[train_v2] training PPO (continuous weights) for {steps:,} steps...", flush=True)
    model.learn(total_timesteps=steps, progress_bar=False)

    # Held-out evaluation (seeds disjoint from training's RNG stream).
    eval_seeds = list(range(10_000, 10_060))
    ppo = eval_policy(model_action_fn(model), eval_seeds, episode_length, reward_cfg)
    uni = eval_policy(uniform_action, eval_seeds, episode_length, reward_cfg)
    inv = eval_policy(inv_latency_action, eval_seeds, episode_length, reward_cfg)

    print("\n[train_v2] held-out evaluation (60 episodes):", flush=True)
    for name, r in [("PPO-v2", ppo), ("round-robin", uni), ("least-conn-ish", inv)]:
        print(f"  {name:<14} reward={r['mean_reward']:+.3f}  "
              f"served_latency={r['mean_served_latency_ms']:.0f}ms  shed={r['mean_shed']:.3f}",
              flush=True)
    beats_rr = ppo["mean_reward"] > uni["mean_reward"]
    print(f"\n[train_v2] gate: PPO-v2 beats round-robin? {'PASS' if beats_rr else 'FAIL'}", flush=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / "policy_v2"
    model.save(str(model_path))

    import stable_baselines3 as _sb3
    meta = {
        "policy_type": "ppo",
        "policy_kind": "continuous_weights",   # <- serving branches on this
        "training_date": datetime.now(timezone.utc).isoformat(),
        "n_max_backends": N_MAX_BACKENDS,
        "norm_params": norm.to_dict(),
        "action_bound": _ACTION_BOUND,
        "reward_config": vars(reward_cfg),
        "episode_length": episode_length,
        "training_steps": steps,
        "sb3_version": _sb3.__version__,
        "eval": {"ppo": ppo, "round_robin": uni, "least_conn": inv, "beats_round_robin": beats_rr},
        "sim": "closed_loop_sim (causal M/G/c, synthetic hetero demand)",
    }
    (out_dir / "artifact_meta_v2.json").write_text(json.dumps(meta, indent=2))
    print(f"[train_v2] saved {model_path}.zip + artifact_meta_v2.json", flush=True)
    return meta


def _parse():
    p = argparse.ArgumentParser(description="Closed-loop continuous-PPO trainer")
    p.add_argument("--steps", type=int, default=400_000)
    p.add_argument("--smoke", action="store_true", help="30k-step wiring check")
    p.add_argument("--episode-length", type=int, default=128)
    p.add_argument("--out-dir", default=str(_MODELS_DIR))
    return p.parse_args()


if __name__ == "__main__":
    args = _parse()
    main(
        steps=30_000 if args.smoke else args.steps,
        out_dir=Path(args.out_dir),
        episode_length=args.episode_length,
    )
