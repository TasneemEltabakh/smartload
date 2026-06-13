"""
services/rl-engine/training/train_algo_comparison.py
─────────────────────────────────────────────────────
Train ALTERNATIVE RL algorithms on the SAME closed-loop routing MDP as PPO-v2 and
compare them through the SAME per-scenario gates.

Algorithms
──────────
  SAC  — off-policy, continuous Box action (env_v2). Apples-to-apples vs PPO-v2.
  A2C  — on-policy, continuous Box action (env_v2). Apples-to-apples vs PPO-v2.
  DQN  — value-based, DISCRETE. Trained on env_discrete_templates (Discrete(K)
         over routing templates) since DQN cannot emit a continuous weight vector.

Fairness invariants (held identical across all algorithms AND the PPO-v2 / classical
baselines):
  • gamma = 0       (this is a contextual bandit; long-horizon credit is noise)
  • episode_length = 128
  • reward config   = RewardConfig() defaults (latency_scale 200, w_tail .5, w_shed 5)
  • norm            = env_v2.DEFAULT_NORM
  • held-out reward eval seeds      = range(10_000, 10_060)   (same as train_ppo_v2)
  • per-scenario gate seeds         = range(20_000, 20_040)   (same as eval_gates_v2)

Artifacts (does NOT touch models/policy.zip or models/candidate_v2/):
  models/candidate_<algo>/policy.zip   — SB3 artifact
  models/candidate_<algo>/meta.json    — norm params (= DEFAULT_NORM) + eval

Usage:
  python training/train_algo_comparison.py            # full budgets
  python training/train_algo_comparison.py --smoke    # tiny budgets, wiring check
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

_RL_ENGINE = Path(__file__).resolve().parents[1]
if str(_RL_ENGINE) not in sys.path:
    sys.path.insert(0, str(_RL_ENGINE))

logging.getLogger("obs_builder").setLevel(logging.ERROR)

from obs_builder import N_MAX_BACKENDS, build_action_mask  # noqa: E402
from training.env_v2 import SmartLoadEnvV2, DEFAULT_NORM, action_to_weights        # noqa: E402
from training.env_discrete_templates import SmartLoadDiscreteTemplatesEnv, template_weights  # noqa: E402
from training.reward_v2 import RewardConfig                                        # noqa: E402
from training.train_ppo_v2 import (                                               # noqa: E402
    eval_policy, model_action_fn, uniform_action, inv_latency_action,
)
from training.eval_gates_v2 import (                                              # noqa: E402
    eval_kind, w_round_robin, w_least_conn,
)

_MODELS_DIR = _RL_ENGINE / "models"
_EPISODE_LENGTH = 128
_REWARD_EVAL_SEEDS = list(range(10_000, 10_060))   # held-out mean-reward seeds
_GATE_SEEDS = list(range(20_000, 20_040))          # per-scenario gate seeds
_KINDS = ["homogeneous", "heterogeneous", "degrading"]
_TOL = 0.05


# ── weight-fn adapters for the per-scenario gate table ─────────────────────────

def make_w_continuous(model):
    """Wrap a continuous-action SB3 model (SAC/A2C/PPO) into eval_gates' (obs,state)
    -> weight-vector signature, mirroring eval_gates_v2.make_w_ppo."""
    def fn(obs, state):
        raw, _ = model.predict(np.asarray(obs, dtype=np.float32), deterministic=True)
        mask = build_action_mask(state, N_MAX_BACKENDS)
        return action_to_weights(np.asarray(raw).flatten()[:N_MAX_BACKENDS], mask)
    return fn


def make_w_discrete(model, n_backends=N_MAX_BACKENDS):
    """Wrap a discrete-action SB3 model (DQN over templates) into eval_gates'
    (obs,state) -> weight-vector signature. The model picks a template id; we
    expand it to weights via the SAME template_weights used in training."""
    def fn(obs, state):
        a, _ = model.predict(np.asarray(obs, dtype=np.float32), deterministic=True)
        template = int(np.asarray(a).reshape(-1)[0])
        w_live = template_weights(template, state, n_backends)
        w = np.zeros(N_MAX_BACKENDS, dtype=float)
        w[:len(w_live)] = w_live
        return w
    return fn


# ── per-algorithm trainers (all gamma=0, same episode_length / reward) ─────────

def _make_env_cont(reward_cfg):
    return SmartLoadEnvV2(episode_length=_EPISODE_LENGTH, norm=DEFAULT_NORM, reward_cfg=reward_cfg)


def _make_env_disc(reward_cfg):
    return SmartLoadDiscreteTemplatesEnv(
        episode_length=_EPISODE_LENGTH, norm=DEFAULT_NORM, reward_cfg=reward_cfg)


def train_sac(steps, reward_cfg, seed=42):
    from stable_baselines3 import SAC
    from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize
    vec = VecNormalize(DummyVecEnv([lambda: _make_env_cont(reward_cfg)]),
                       norm_obs=False, norm_reward=True, gamma=0.0)
    model = SAC(
        "MlpPolicy", vec, verbose=0, seed=seed,
        learning_rate=3e-4, buffer_size=100_000, batch_size=256,
        gamma=0.0, tau=0.005, train_freq=1, gradient_steps=1,
        learning_starts=1_000,
    )
    model.learn(total_timesteps=steps, progress_bar=False)
    return model


def train_a2c(steps, reward_cfg, seed=42):
    from stable_baselines3 import A2C
    from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize
    vec = VecNormalize(DummyVecEnv([lambda: _make_env_cont(reward_cfg)]),
                       norm_obs=False, norm_reward=True, gamma=0.0)
    model = A2C(
        "MlpPolicy", vec, verbose=0, seed=seed,
        learning_rate=7e-4, n_steps=32, gamma=0.0, gae_lambda=1.0,
        ent_coef=0.01, vf_coef=0.5,
    )
    model.learn(total_timesteps=steps, progress_bar=False)
    return model


def train_dqn(steps, reward_cfg, seed=42):
    from stable_baselines3 import DQN
    from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize
    vec = VecNormalize(DummyVecEnv([lambda: _make_env_disc(reward_cfg)]),
                       norm_obs=False, norm_reward=True, gamma=0.0)
    model = DQN(
        "MlpPolicy", vec, verbose=0, seed=seed,
        learning_rate=1e-3, buffer_size=100_000, batch_size=128,
        gamma=0.0, train_freq=4, target_update_interval=1_000,
        learning_starts=1_000, exploration_fraction=0.3,
        exploration_final_eps=0.05,
    )
    model.learn(total_timesteps=steps, progress_bar=False)
    return model


# ── evaluation harness ─────────────────────────────────────────────────────────

def eval_continuous_model(model, reward_cfg):
    """Mean-reward (held-out seeds) + per-scenario served-latency for a continuous
    SB3 model. Uses eval_policy / eval_kind so it is identical to PPO-v2's eval."""
    rew = eval_policy(model_action_fn(model), _REWARD_EVAL_SEEDS, _EPISODE_LENGTH, reward_cfg)
    wfn = make_w_continuous(model)
    scen = {k: eval_kind(wfn, DEFAULT_NORM, k, _GATE_SEEDS, _EPISODE_LENGTH) for k in _KINDS}
    return rew, scen


def eval_discrete_model(model, reward_cfg):
    """Mean-reward + per-scenario served-latency for the DQN-templates model.
    Mean-reward is computed on the SAME held-out seeds via the discrete env so the
    reward scale matches; per-scenario latency uses the shared eval_kind harness."""
    rewards, lats, sheds = [], [], []
    for s in _REWARD_EVAL_SEEDS:
        env = SmartLoadDiscreteTemplatesEnv(episode_length=_EPISODE_LENGTH, reward_cfg=reward_cfg)
        obs, _ = env.reset(seed=int(s))
        done = False
        while not done:
            a, _ = model.predict(np.asarray(obs, dtype=np.float32), deterministic=True)
            obs, r, done, _, info = env.step(a)
            rewards.append(r)
            lats.append(info["served_lat_ms"])
            sheds.append(info["shed"])
    rew = {"mean_reward": float(np.mean(rewards)),
           "mean_served_latency_ms": float(np.mean(lats)),
           "mean_shed": float(np.mean(sheds))}
    wfn = make_w_discrete(model)
    scen = {k: eval_kind(wfn, DEFAULT_NORM, k, _GATE_SEEDS, _EPISODE_LENGTH) for k in _KINDS}
    return rew, scen


def eval_baseline_weightfn(action_fn_reward, weight_fn_scen, reward_cfg):
    """Mean-reward via an obs->action callable + per-scenario via an (obs,state)
    weight fn. Used for round-robin / least-conn and PPO-v2."""
    rew = eval_policy(action_fn_reward, _REWARD_EVAL_SEEDS, _EPISODE_LENGTH, reward_cfg)
    scen = {k: eval_kind(weight_fn_scen, DEFAULT_NORM, k, _GATE_SEEDS, _EPISODE_LENGTH) for k in _KINDS}
    return rew, scen


def save_artifact(model, algo, reward_cfg, rew, scen, steps):
    out = _MODELS_DIR / f"candidate_{algo}"
    out.mkdir(parents=True, exist_ok=True)
    model.save(str(out / "policy"))
    import stable_baselines3 as _sb3
    meta = {
        "policy_type": algo,
        "policy_kind": "discrete_templates" if algo == "dqn" else "continuous_weights",
        "training_date": datetime.now(timezone.utc).isoformat(),
        "n_max_backends": N_MAX_BACKENDS,
        "norm_params": DEFAULT_NORM.to_dict(),
        "reward_config": vars(reward_cfg),
        "episode_length": _EPISODE_LENGTH,
        "gamma": 0.0,
        "training_steps": steps,
        "sb3_version": _sb3.__version__,
        "eval": {"reward": rew, "per_scenario_served_latency_ms": scen},
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    return out


def main(smoke: bool) -> dict:
    reward_cfg = RewardConfig()
    budgets = (
        {"sac": 4_000, "a2c": 4_000, "dqn": 4_000}
        if smoke else
        {"sac": 150_000, "a2c": 400_000, "dqn": 150_000}
    )

    rows: dict[str, dict] = {}

    # ── classical baselines + PPO-v2 (reference rows) ──────────────────────────
    print("[algo-cmp] evaluating round-robin / least-conn / PPO-v2 ...", flush=True)
    rr_rew, rr_scen = eval_baseline_weightfn(uniform_action, w_round_robin, reward_cfg)
    rows["round-robin"] = {"reward": rr_rew, "scen": rr_scen}
    lc_rew, lc_scen = eval_baseline_weightfn(inv_latency_action, w_least_conn, reward_cfg)
    rows["least-conn"] = {"reward": lc_rew, "scen": lc_scen}

    from stable_baselines3 import PPO
    ppo_model = PPO.load(str(_MODELS_DIR / "candidate_v2" / "policy"))
    ppo_rew, ppo_scen = eval_continuous_model(ppo_model, reward_cfg)
    rows["PPO-v2"] = {"reward": ppo_rew, "scen": ppo_scen}

    # ── candidate algorithms ───────────────────────────────────────────────────
    trainers = [
        ("sac", "SAC", train_sac, eval_continuous_model),
        ("a2c", "A2C", train_a2c, eval_continuous_model),
        ("dqn", "DQN-templates", train_dqn, eval_discrete_model),
    ]
    for key, label, trainer, evaluator in trainers:
        t0 = time.time()
        print(f"[algo-cmp] training {label} for {budgets[key]:,} steps ...", flush=True)
        model = trainer(budgets[key], reward_cfg)
        dt = time.time() - t0
        rew, scen = evaluator(model, reward_cfg)
        out = save_artifact(model, key, reward_cfg, rew, scen, budgets[key])
        rows[label] = {"reward": rew, "scen": scen}
        print(f"[algo-cmp]   {label}: reward={rew['mean_reward']:+.3f} "
              f"homo={scen['homogeneous']:.1f} het={scen['heterogeneous']:.1f} "
              f"deg={scen['degrading']:.1f}  ({dt:.0f}s) -> {out.name}/", flush=True)

    # ── gates (relative to round-robin / least-conn, same as eval_gates_v2) ────
    rr_homo = rows["round-robin"]["scen"]["homogeneous"]
    lc_deg = rows["least-conn"]["scen"]["degrading"]
    for label, row in rows.items():
        scen = row["scen"]
        row["gate_a"] = scen["homogeneous"] <= rr_homo * (1 + _TOL)
        row["gate_b"] = scen["degrading"] <= lc_deg * (1 + _TOL)

    # ── print + persist results table ──────────────────────────────────────────
    order = ["PPO-v2", "SAC", "A2C", "DQN-templates", "round-robin", "least-conn"]
    print("\n" + "=" * 96)
    print(f"{'policy':<16}{'reward':>10}{'homo ms':>10}{'het ms':>10}{'deg ms':>10}{'GateA':>8}{'GateB':>8}")
    print("-" * 96)
    for label in order:
        if label not in rows:
            continue
        r = rows[label]
        print(f"{label:<16}{r['reward']['mean_reward']:>+10.3f}"
              f"{r['scen']['homogeneous']:>10.1f}{r['scen']['heterogeneous']:>10.1f}"
              f"{r['scen']['degrading']:>10.1f}"
              f"{('PASS' if r['gate_a'] else 'FAIL'):>8}{('PASS' if r['gate_b'] else 'FAIL'):>8}")
    print("=" * 96)
    print(f"Gate A bar (round-robin homogeneous x1.05): {rr_homo * 1.05:.1f} ms", flush=True)
    print(f"Gate B bar (least-conn degrading x1.05):    {lc_deg * 1.05:.1f} ms", flush=True)

    # dump machine-readable results next to the script for the .md writer
    results = {
        "smoke": smoke,
        "budgets": budgets,
        "episode_length": _EPISODE_LENGTH,
        "gamma": 0.0,
        "reward_eval_seeds": [_REWARD_EVAL_SEEDS[0], _REWARD_EVAL_SEEDS[-1]],
        "gate_seeds": [_GATE_SEEDS[0], _GATE_SEEDS[-1]],
        "gate_a_bar_ms": rr_homo * 1.05,
        "gate_b_bar_ms": lc_deg * 1.05,
        "rows": {
            label: {
                "mean_reward": r["reward"]["mean_reward"],
                "mean_served_latency_ms": r["reward"]["mean_served_latency_ms"],
                "mean_shed": r["reward"]["mean_shed"],
                "homogeneous_ms": r["scen"]["homogeneous"],
                "heterogeneous_ms": r["scen"]["heterogeneous"],
                "degrading_ms": r["scen"]["degrading"],
                "gate_a": r["gate_a"],
                "gate_b": r["gate_b"],
            }
            for label, r in rows.items()
        },
    }
    (Path(__file__).parent / "_algo_comparison_results.json").write_text(json.dumps(results, indent=2))
    print(f"\n[algo-cmp] wrote {Path(__file__).parent / '_algo_comparison_results.json'}", flush=True)
    return results


def _parse():
    p = argparse.ArgumentParser(description="SAC/A2C/DQN-templates vs PPO-v2 comparison")
    p.add_argument("--smoke", action="store_true", help="tiny budgets, wiring check")
    return p.parse_args()


if __name__ == "__main__":
    a = _parse()
    main(smoke=a.smoke)
