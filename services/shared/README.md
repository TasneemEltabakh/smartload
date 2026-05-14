# shared

Cross-service Python modules. Anything imported by more than one service belongs here.

## Modules
- `contracts.py` — typed envelope dataclasses for every Redis channel + JSON encode / decode helpers. Canonical source for envelope shapes.
- `queries.py` — SQL query constants the AI services run against TimescaleDB.
- `lb_adapters/` — load-balancer adapter interface + plugin-per-folder implementations (NGINX today; Envoy / HAProxy / ALB stubbed).

## Rules
- Code here must not import from any specific service folder (avoid cycles).
- Every public type used across services must be defined here, not duplicated.
- The channel registry in `docs/redis-channels.md` and the OpenAPI spec in `docs/openapi/smartload-v1.yaml` are the canonical contract surfaces; envelopes here mirror them.

## See also
- SOT §11 (Redis envelope catalog)
- `docs/redis-channels.md`
