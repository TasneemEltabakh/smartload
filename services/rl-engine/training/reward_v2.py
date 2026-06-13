"""
services/rl-engine/training/reward_v2.py
─────────────────────────────────────────
Causal, balance-aware reward for the closed-loop MDP (env_v2 + closed_loop_sim).

Unlike reward.py — whose latency term is the *recorded* latency of the chosen
backend (observational) and whose imbalance term reads the *trace's* spread —
every term here is a consequence of the agent's own weight vector this window,
read from the simulator's StepMetrics:

    reward = -(served_mean_latency / latency_scale)      # pool latency it caused
             - w_tail * (max_backend_latency / latency_scale)  # worst-backend tail
             - w_shed * shed_fraction                    # overload it caused

Concentrating load raises all three (the hot backend queues, its tail explodes,
and past capacity it sheds), so the policy is pushed toward spreading on a
homogeneous pool and capacity-aware routing on a heterogeneous one. No explicit
"imbalance = std(counts)" term is needed — imbalance shows up *through its
latency/shed consequence*, which is the whole point of closing the loop.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RewardConfig:
    latency_scale: float = 200.0   # ~SLO; latencies normalised against this
    w_tail: float = 0.5            # weight on the worst-backend latency
    w_shed: float = 5.0            # weight on shed fraction (overload is costly)


def compute_reward(metrics, cfg: RewardConfig) -> float:
    """metrics: closed_loop_sim.StepMetrics for the window just served."""
    served = metrics.served_mean_latency_ms / cfg.latency_scale
    tail = metrics.max_backend_latency_ms / cfg.latency_scale
    return float(-served - cfg.w_tail * tail - cfg.w_shed * metrics.shed_fraction)
