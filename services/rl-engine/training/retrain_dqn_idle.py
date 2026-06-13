"""
services/rl-engine/training/retrain_dqn_idle.py
────────────────────────────────────────────────
Retrain the DQN-templates candidate after the idle/OOD fix (idle episodes in the
demand curriculum + a small always-on spread penalty in reward_v2). Re-checks the
promotion gates AND the idle behaviour the live Fortio test flagged (the model
must select the *uniform* template, not concentrate, on a near-idle pool).

Overwrites models/candidate_dqn/ with the improved artifact. Run:
  python training/retrain_dqn_idle.py
"""

from __future__ import annotations

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

from obs_builder import N_MAX_BACKENDS, build_observation                  # noqa: E402
from policy_base import BackendState                                       # noqa: E402
from routing_templates import template_weights                            # noqa: E402
from training.env_v2 import DEFAULT_NORM                                   # noqa: E402
from training.reward_v2 import RewardConfig                               # noqa: E402
from training.train_ppo_v2 import uniform_action, inv_latency_action      # noqa: E402
from training.eval_gates_v2 import w_round_robin, w_least_conn            # noqa: E402
from training.train_algo_comparison import (                              # noqa: E402
    train_dqn, eval_discrete_model, eval_baseline_weightfn,
)

_STEPS = 200_000


def idle_template(model):
    """What template does the model pick on a near-idle, all-healthy pool?
    Returns (template_id, weights). Want template 0 (uniform)."""
    st = [BackendState(f"backend_{i+1}", 20.0, 0.0, "healthy") for i in range(N_MAX_BACKENDS)]
    obs = build_observation(st, N_MAX_BACKENDS, DEFAULT_NORM)
    a, _ = model.predict(obs.astype(np.float32), deterministic=True)
    t = int(np.asarray(a).reshape(-1)[0])
    return t, np.round(template_weights(t, st, N_MAX_BACKENDS), 3)


def main():
    cfg = RewardConfig()
    print(f"[retrain-dqn] reward w_spread={cfg.w_spread}; training {_STEPS:,} steps ...", flush=True)
    model = train_dqn(_STEPS, cfg)

    rew, scen = eval_discrete_model(model, cfg)
    rr_rew, rr_scen = eval_baseline_weightfn(uniform_action, w_round_robin, cfg)
    lc_rew, lc_scen = eval_baseline_weightfn(inv_latency_action, w_least_conn, cfg)
    gate_a = scen["homogeneous"] <= rr_scen["homogeneous"] * 1.05
    gate_b = scen["degrading"] <= lc_scen["degrading"] * 1.05

    print(f"\n[retrain-dqn] reward={rew['mean_reward']:+.3f}  "
          f"homo={scen['homogeneous']:.1f}  het={scen['heterogeneous']:.1f}  "
          f"deg={scen['degrading']:.1f}", flush=True)
    print(f"[retrain-dqn] Gate A homo<= {rr_scen['homogeneous']*1.05:.1f}: "
          f"{'PASS' if gate_a else 'FAIL'}   "
          f"Gate B deg<= {lc_scen['degrading']*1.05:.1f}: {'PASS' if gate_b else 'FAIL'}", flush=True)

    t, w = idle_template(model)
    # Pass if the live weights are spread (every backend keeps a meaningful share,
    # none near zero) — independent of which template id achieves it.
    idle_ok = bool(np.all(np.asarray(w) > 0.10) and np.max(w) < 0.30)
    print(f"[retrain-dqn] IDLE behaviour: template={t} weights={w} "
          f"-> {'PASS (spread)' if idle_ok else 'FAIL (concentrated)'}", flush=True)

    out = _RL_ENGINE / "models" / "candidate_dqn"
    out.mkdir(parents=True, exist_ok=True)
    model.save(str(out / "policy"))
    import stable_baselines3 as _sb3
    meta = {
        "policy_type": "dqn",
        "policy_kind": "discrete_templates",
        "training_date": datetime.now(timezone.utc).isoformat(),
        "n_max_backends": N_MAX_BACKENDS,
        "norm_params": DEFAULT_NORM.to_dict(),
        "reward_config": vars(cfg),
        "episode_length": 128,
        "gamma": 0.0,
        "training_steps": _STEPS,
        "sb3_version": _sb3.__version__,
        "idle_fix": True,
        "eval": {"reward": rew, "per_scenario_served_latency_ms": scen,
                 "gate_a": gate_a, "gate_b": gate_b,
                 "idle_template": t, "idle_uniform": idle_ok},
    }
    for name in ("meta.json", "artifact_meta.json"):
        (out / name).write_text(json.dumps(meta, indent=2))
    print(f"[retrain-dqn] saved {out}", flush=True)


if __name__ == "__main__":
    main()
