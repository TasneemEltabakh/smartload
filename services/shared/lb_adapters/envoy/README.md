# envoy adapter (stub)

Placeholder for an Envoy implementation of `LoadBalancerAdapter`. Raises `NotImplementedError` on construction in v1.

## When to implement

When a customer or integrator requests Envoy support, open a feature issue and replace this stub with the real implementation. The adapter must pass `tests/conformance/lb_adapter/` to land.

## Why the stub exists today

To make the plugin contract visible. The import path `from services.shared.lb_adapters.envoy import EnvoyAdapter` is already real — when the implementation lands, no decision-plane code changes.
