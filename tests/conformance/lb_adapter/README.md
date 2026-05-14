# lb_adapter conformance suite

Every `LoadBalancerAdapter` implementation must pass these tests. They exercise the contract defined in `services/shared/lb_adapters/base.py`:

- `set_upstream_weights` is idempotent
- `exclude_backend` is idempotent
- `include_backend` is idempotent
- `current_state` reflects committed mutations
- Method calls return within reasonable latency under normal conditions

## Status

Scaffolded only. The tests themselves are pending until the first real adapter (NGINX) is refactored behind the interface — issue to be filed.

## Why this matters

Without conformance tests, "we have an adapter pattern" is a lie — the second adapter inevitably diverges from the first. The suite is the contract.
