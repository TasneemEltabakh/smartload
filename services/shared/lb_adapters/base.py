"""Abstract base class for load-balancer adapters.

Plugins implement this interface; the decision plane never speaks to a
specific load balancer directly. Conformance tests in
tests/conformance/lb_adapter/ exercise every adapter against the same
contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class AdapterState:
    """Snapshot of the load balancer's current routing configuration."""

    upstream_weights: dict[str, int]
    excluded_backends: set[str]


class LoadBalancerAdapter(ABC):
    """Contract every load-balancer adapter must satisfy.

    Adapters are expected to be:
      - **idempotent**: calling the same method twice with the same
        arguments is a no-op the second time.
      - **bounded latency**: each method must return within ~1 second under
        normal conditions; reload latency varies by backend.
      - **fail-safe**: a transient failure raises, but state mutation is
        all-or-nothing — the load balancer is never left in a half-applied
        state.

    Conformance tests assert these properties.
    """

    @abstractmethod
    def set_upstream_weights(self, backend_weights: dict[str, int]) -> None:
        """Rewrite upstream weights. Excluded backends remain excluded."""

    @abstractmethod
    def exclude_backend(self, backend_id: str) -> None:
        """Stop routing to a backend. Idempotent."""

    @abstractmethod
    def include_backend(self, backend_id: str) -> None:
        """Restore routing to a previously-excluded backend. Idempotent."""

    @abstractmethod
    def current_state(self) -> AdapterState:
        """Return the adapter's view of the load balancer's current config."""
