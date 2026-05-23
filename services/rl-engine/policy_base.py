"""
services/rl-engine/policy_base.py
─────────────────────────────────
Abstract base class for RL routing policies + factory.

NOT yet imported by app.py. Named `policy_base.py` to avoid collision
with per-plugin `policies/<plugin>/policy.py`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class BackendState:
    backend_id: str
    latency_ms: float
    queue_depth: int
    health: str  # "healthy" | "degraded" | "unhealthy"


@dataclass
class Ranking:
    backend_id: str
    score: float


@dataclass
class RoutingAction:
    """Engine output. Run loop converts to a RoutingRecommendation envelope."""

    mode: str  # "shadow" | "active"
    rankings: list[Ranking]


class RoutingPolicy(ABC):
    @abstractmethod
    def act(self, state: list[BackendState]) -> RoutingAction:
        """Rank backends for the next routing window."""

    def reload(self) -> None:
        """Optional hook called when operating-mode policy changes."""


def _routing_fallback(state: list[BackendState]) -> RoutingAction:
    """Canonical all-unhealthy fallback for classical (non-PPO) policies.

    Called when no healthy or degraded backend exists.  Emits the canonical
    WARNING via obs_builder.all_masked_fallback(), then returns uniform-scored
    shadow rankings so the run loop still has a valid envelope to publish.

    Defined here (not per-policy) so round_robin and least_connections share
    one implementation rather than duplicating the fallback logic.
    """
    # Late import to keep policy_base lightweight (numpy pulled in transitively).
    import importlib
    _obs = importlib.import_module("obs_builder")
    _obs.all_masked_fallback(state, _obs.N_MAX_BACKENDS)   # emits WARNING log

    score = 1.0 / max(len(state), 1)
    rankings = [Ranking(backend_id=b.backend_id, score=score) for b in state]
    return RoutingAction(mode="shadow", rankings=rankings)


def select_policy(name: str, **kwargs) -> RoutingPolicy:
    if name == "random_shadow":
        from policies.random_shadow.policy import RandomShadowPolicy
        return RandomShadowPolicy(**kwargs)
    if name == "round_robin":
        from policies.round_robin.policy import RoundRobinPolicy
        return RoundRobinPolicy(**kwargs)
    if name == "least_connections":
        from policies.least_connections.policy import LeastConnectionsPolicy
        return LeastConnectionsPolicy(**kwargs)
    if name == "ppo":
        from policies.ppo.policy import PPOPolicy
        return PPOPolicy(**kwargs)
    raise ValueError(f"Unknown RL policy: {name!r}")
