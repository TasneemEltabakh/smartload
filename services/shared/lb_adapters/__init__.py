"""Load-balancer adapter interface + plugin implementations.

The decision plane (anomaly-detector, rl-engine, autoscaler) speaks to
this interface — not to NGINX directly. New backends (Envoy, HAProxy,
ALB) drop in as new plugin folders without touching the decision plane.
"""

from .base import LoadBalancerAdapter, AdapterState

__all__ = ["LoadBalancerAdapter", "AdapterState"]
