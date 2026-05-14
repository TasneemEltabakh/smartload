# nginx adapter

The default and only fully-implemented adapter in v1.

## How it works

- Rewrites the upstream block in the NGINX config (mounted via volume)
- Sends `nginx -s reload` to apply changes without dropping live connections
- Tracks excluded backends in-memory so weight rewrites preserve exclusions

## Status

Adapter shape is scaffolded here. The current NGINX-reload logic still lives in `services/lb-otel-shipper` and the planned T2.1 sidecar. The refactor that moves it behind this adapter is a deferred issue — when it lands, no decision-plane code changes.

## Tests

- `tests/conformance/lb_adapter/` — generic contract tests this adapter must pass.
- (Future) `test_adapter.py` — adapter-specific tests against a real or fixture NGINX.
