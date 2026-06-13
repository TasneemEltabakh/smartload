"""
services/anomaly-detector/engine_base.py
─────────────────────────────────────────
Abstract base class for anomaly engines + the factory that selects which
engine to load at startup.

Named `engine_base.py` (not `engine.py`) to avoid a name collision with
the per-plugin implementation files at `engines/<plugin>/engine.py`.

The service's run loop (when wired) will:
  1. read ANOMALY_ENGINE env var
  2. call select_engine(name) to obtain an instance
  3. on every poll cycle, call engine.score(features) and publish AnomalyEvent

Plugin folders live in engines/. Each plugin folder is self-contained
(README, implementation, tests) — never a flat dump.

This module is intentionally NOT imported by app.py yet. The wrapping
refactor lands in a separate sprint when dependencies are finalized.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class BackendFeatures:
    """Inputs an engine needs to classify one backend's health.

    Built by the run loop from a TimescaleDB query (services/shared/queries.py).
    """

    backend_id: str
    latency_ms: float
    latency_rolling_mean_ms: float
    error_rate: float
    sample_count: int
    latency_rolling_std_ms: float = 0.0


@dataclass
class AnomalyScore:
    """Engine output, one per backend per poll cycle.

    Run loop converts this to an AnomalyEvent envelope and publishes to
    smartload.anomaly.

    The optional metric / observed_value / threshold triple lets a non-healthy
    verdict carry the evidence that produced it (e.g. latency_ms=312 crossed a
    250ms gate) so the operator UI can show "why" without re-deriving it. They
    stay None for healthy verdicts and for engines that don't populate them.
    """

    backend_id: str
    status: str  # one of: "healthy" | "degraded" | "unhealthy"
    score: float
    # ── optional evidence behind the verdict (UI alert detail) ───────────────
    metric: str | None = None          # which signal tripped, e.g. "latency_ms"
    observed_value: float | None = None  # the measured value
    threshold: float | None = None     # the boundary it crossed


class AnomalyEngine(ABC):
    """An anomaly-classification strategy.

    Implementations:
      - engines/threshold/engine.py — baseline; rule-based comparisons
      - engines/isolation_forest/engine.py — trained model (planned)
    """

    @abstractmethod
    def score(self, features: BackendFeatures) -> AnomalyScore:
        """Classify one backend's current health."""

    def reload(self) -> None:
        """Optional hook called when policy changes.

        Override if the engine needs to re-read configuration without restart.
        Default no-op.
        """


def select_engine(name: str, **kwargs) -> AnomalyEngine:
    """Factory. Returns the engine matching name."""
    if name == "threshold":
        from engines.threshold.engine import ThresholdEngine
        return ThresholdEngine(**kwargs)
    if name == "isolation_forest":
        from engines.isolation_forest.engine import IsolationForestEngine
        return IsolationForestEngine(**kwargs)
    raise ValueError(f"Unknown anomaly engine: {name!r}")
