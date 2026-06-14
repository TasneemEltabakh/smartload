"""
services/rl-engine/training/train_maxxer.py
────────────────────────────────────────────
candidate_maxxer — a NON-monotone, SLA-targeted continuous PPO router whose only
job is to maximise the benchmark (beat candidate_v2 on p95 + SLA%), accepting
that it will FAIL the latency-monotonicity probe. It is the "benchmark-maxxer"
half of the ship-both deliverable; candidate_mono is the production rec.

Difference from train_ppo_v2 (candidate_v2):
  • Reward targets the eval metric directly: an explicit SLA-violation indicator
    penalty (served_lat > 200 ms) on top of the served-latency term, and a LOWER
    shed weight + NO spread penalty — so the policy is free to do the
    sacrificial-concentration routing that lowers served-p95 in overload (the
    very thing monotonicity forbids).
  • Continuous Box(weights) action -> masked softmax (env_v2.action_to_weights),
    loadable by the unchanged serving path + benchmark adapter.

Trains N seeds; each -> models/candidate_maxxer_seed{s}/policy.zip + artifact_meta.json
(policy_kind="continuous_weights").

Usage:
  python training/train_maxxer.py --seeds 5 --steps 300000
  python training/train_maxxer.py --smoke
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
logging.getLogger("obs_builder").setLevel(logging.ERROR)

from obs_builder import N_MAX_BACKENDS, build_action_mask, all_masked_fallback  # noqa: E402
from training.env_v2 import SmartLoadEnvV2, action_to_weights, DEFAULT_NORM      # noqa: E402

_MODELS_DIR = _RL_ENGINE / "models"
SLA_MS = 200.0


class MaxxerEnv(SmartLoadEnvV2):
    """env_v2 with an SLA-targeted reward (explicit >200ms indicator, low shed
    weight, no spread penalty) to push toward benchmark-optimal routing."""

    def __init__(self, w_tail=0.5, w_shed=1.5, w_sla=3.0, **kw):
        super().__init__(**kw)
        self._wt, self._ws, self._wsla = w_tail, w_shed, w_sla

    def step(self, action):
        mask = build_action_mask(self._state, N_MAX_BACKENDS)
        if not mask.any():
            mask = all_masked_fallback(self._state, N_MAX_BACKENDS)
        w = action_to_weights(np.asarray(action)[: self._n], mask[: self._n])
        next_state, m, done = self._sim.step(w)
        served = m.served_mean_latency_ms / 200.0
        tail = m.max_backend_latency_ms / 200.0
        sla = 1.0 if m.served_mean_latency_ms > SLA_MS else 0.0
        reward = float(-served - self._wt * tail - self._ws * m.shed_fraction - self._wsla * sla)
        self._state = next_state
        info = {"served_lat_ms": m.served_mean_latency_ms, "shed": m.shed_fraction}
        return self._build_obs(next_state), reward, done, False, info


def train_seed(seed, steps, out_dir: Path, episode_length=128):
    from stable_baselines3 import PPO
    from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize

    norm = DEFAULT_NORM

    def make_env():
        return MaxxerEnv(norm=norm, episode_length=episode_length)

    vec = VecNormalize(DummyVecEnv([make_env]), norm_obs=False, norm_reward=True, gamma=0.0)
    model = PPO("MlpPolicy", vec, verbose=0, seed=seed, device="cpu",
                learning_rate=3e-4, n_steps=1024, batch_size=128, n_epochs=10,
                gamma=0.0, gae_lambda=1.0, ent_coef=0.01)
    print(f"[maxxer] seed {seed}: training {steps:,} steps (CPU)...", flush=True)
    model.learn(total_timesteps=steps, progress_bar=False)

    d = out_dir / f"candidate_maxxer_seed{seed}"
    d.mkdir(parents=True, exist_ok=True)
    model.save(str(d / "policy"))
    import stable_baselines3 as _sb3
    meta = {
        "policy_type": "ppo", "policy_kind": "continuous_weights",
        "training_date": datetime.now(timezone.utc).isoformat(),
        "n_max_backends": N_MAX_BACKENDS, "norm_params": norm.to_dict(), "action_bound": 10.0,
        "reward_config": {"w_tail": 0.5, "w_shed": 1.5, "w_sla": 3.0, "sla_ms": SLA_MS,
                          "note": "SLA-targeted, concentration-permitting; NON-monotone by design"},
        "episode_length": episode_length, "training_steps": steps, "training_seed": seed,
        "sb3_version": _sb3.__version__,
        "monotone_by_construction": False,
        "sim": "closed_loop_sim (causal M/G/c, synthetic hetero demand)",
    }
    (d / "artifact_meta.json").write_text(json.dumps(meta, indent=2))
    print(f"[maxxer] saved {d}/policy.zip", flush=True)


def main(n_seeds, steps, out_dir):
    for s in range(n_seeds):
        train_seed(s, steps, out_dir)


def _parse():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--steps", type=int, default=300_000)
    ap.add_argument("--out-dir", default=str(_MODELS_DIR))
    ap.add_argument("--smoke", action="store_true")
    return ap.parse_args()


if __name__ == "__main__":
    a = _parse()
    main(a.seeds, 20_000 if a.smoke else a.steps, Path(a.out_dir))
