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


def select_policy(name: str, **kwargs) -> RoutingPolicy:
    if name == "random_shadow":
        from policies.random_shadow.policy import RandomShadowPolicy
        return RandomShadowPolicy(**kwargs)
    if name == "ppo":
        from policies.ppo.policy import PPOPolicy
        return PPOPolicy(**kwargs)
    raise ValueError(f"Unknown RL policy: {name!r}")
