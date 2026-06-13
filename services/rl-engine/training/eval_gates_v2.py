"""
services/rl-engine/training/eval_gates_v2.py
─────────────────────────────────────────────
Promotion gates for the closed-loop PPO artifact. Runs controlled scenarios on
the causal simulator and compares the trained model's served latency against
round-robin and least-connections.

Gate A (homogeneous):  PPO served-latency <= round-robin x (1+tol).
    On a homogeneous pool even spread is optimal, so PPO must at least MATCH
    round-robin — the bar the old artifact failed by ~2x.
Gate B (degrading):    PPO served-latency <= least-connections x (1+tol).
    On the one-degraded-backend case PPO must match/beat the classical
    load-aware router.

Both gates must pass before leaving shadow. This script reports pass/fail +
numbers; it does NOT flip RL_MODE (that decision stays with the operator/Rghda).

Usage:
  python training/eval_gates_v2.py --model training/_staging/policy_v2
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np

_RL_ENGINE = Path(__file__).resolve().parents[1]
if str(_RL_ENGINE) not in sys.path:
    sys.path.insert(0, str(_RL_ENGINE))

logging.getLogger("obs_builder").setLevel(logging.ERROR)

from obs_builder import N_MAX_BACKENDS, NormParams, build_observation, build_action_mask  # noqa: E402
from policy_base import is_eligible                                       # noqa: E402
from training.closed_loop_sim import ClosedLoopSimulator                  # noqa: E402
from training.env_v2 import action_to_weights, DEFAULT_NORM               # noqa: E402

_TOL = 0.05   # 5% slack — PPO must MATCH (not necessarily beat) the baseline


# ── weight functions (obs, state) -> weight vector over N_MAX_BACKENDS ─────────

def w_round_robin(obs, state):
    mask = build_action_mask(state, N_MAX_BACKENDS).astype(float)
    return mask / mask.sum() if mask.sum() else np.ones(N_MAX_BACKENDS) / N_MAX_BACKENDS


def w_least_conn(obs, state):
    """Capacity/least-connections proxy: weight ∝ 1 / observed latency, eligible only."""
    sorted_state = sorted(state, key=lambda s: s.backend_id)
    w = np.zeros(N_MAX_BACKENDS)
    for i, s in enumerate(sorted_state[:N_MAX_BACKENDS]):
        if is_eligible(s.health):
            w[i] = 1.0 / max(s.latency_ms, 1.0)
    return w / w.sum() if w.sum() else w_round_robin(obs, state)


def w_concentrate(obs, state):
    """The old PPO pathology — argmax-dominant 0.7 on the first eligible."""
    mask = build_action_mask(state, N_MAX_BACKENDS)
    w = np.full(N_MAX_BACKENDS, 0.0)
    elig = np.where(mask)[0]
    if len(elig) == 0:
        return w_round_robin(obs, state)
    w[:] = 0.3 / max(len(elig), 1)
    w[elig[0]] = 0.7
    return w / w.sum()


def make_w_ppo(model_path: Path, norm: NormParams):
    from stable_baselines3 import PPO
    model = PPO.load(str(model_path))

    def fn(obs, state):
        raw, _ = model.predict(np.asarray(obs, dtype=np.float32), deterministic=True)
        mask = build_action_mask(state, N_MAX_BACKENDS)
        return action_to_weights(np.asarray(raw).flatten()[:N_MAX_BACKENDS], mask)
    return fn


# ── scenario evaluation ────────────────────────────────────────────────────────

def eval_kind(weight_fn, norm, kind, seeds, episode_length=128):
    """Mean served latency (ms) over `seeds` episodes of the given scenario kind."""
    lats = []
    for s in seeds:
        sim = ClosedLoopSimulator(N_MAX_BACKENDS, episode_length=episode_length)
        state = sim.reset(seed=int(s), force_kind=kind)
        done = False
        while not done:
            obs = build_observation(state, N_MAX_BACKENDS, norm)
            w = weight_fn(obs, state)
            state, metrics, done = sim.step(w)
            lats.append(metrics.served_mean_latency_ms)
    return float(np.mean(lats))


def main(model_path: Path, meta_path: Path | None) -> int:
    norm = DEFAULT_NORM
    if meta_path and meta_path.exists():
        norm = NormParams.from_dict(json.loads(meta_path.read_text())["norm_params"])

    w_ppo = make_w_ppo(model_path, norm)
    seeds = list(range(20_000, 20_040))
    policies = {
        "PPO-v2": w_ppo,
        "round-robin": w_round_robin,
        "least-conn": w_least_conn,
        "concentrate(old)": w_concentrate,
    }
    kinds = ["homogeneous", "heterogeneous", "degrading"]

    print(f"\nServed latency (ms, lower better) over {len(seeds)} episodes/kind:\n")
    print(f"{'policy':<18}" + "".join(f"{k:>16}" for k in kinds))
    results = {}
    for name, fn in policies.items():
        row = {k: eval_kind(fn, norm, k, seeds) for k in kinds}
        results[name] = row
        print(f"{name:<18}" + "".join(f"{row[k]:>16.1f}" for k in kinds))

    ppo, rr, lc = results["PPO-v2"], results["round-robin"], results["least-conn"]
    gate_a = ppo["homogeneous"] <= rr["homogeneous"] * (1 + _TOL)
    gate_b = ppo["degrading"] <= lc["degrading"] * (1 + _TOL)
    print("\nGates:")
    print(f"  A homogeneous  PPO {ppo['homogeneous']:.1f} <= round-robin {rr['homogeneous']:.1f} x1.05 "
          f"=> {'PASS' if gate_a else 'FAIL'}")
    print(f"  B degrading    PPO {ppo['degrading']:.1f} <= least-conn  {lc['degrading']:.1f} x1.05 "
          f"=> {'PASS' if gate_b else 'FAIL'}")
    verdict = gate_a and gate_b
    print(f"\nPROMOTION GATE: {'PASS — eligible to leave shadow' if verdict else 'FAIL — keep in shadow'}")
    return 0 if verdict else 1


def _parse():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="training/_staging/policy_v2",
                   help="path to policy_v2 (without .zip)")
    p.add_argument("--meta", default="training/_staging/artifact_meta_v2.json")
    return p.parse_args()


if __name__ == "__main__":
    a = _parse()
    raise SystemExit(main(Path(a.model), Path(a.meta) if a.meta else None))
