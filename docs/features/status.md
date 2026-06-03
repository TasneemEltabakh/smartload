# Consolidated Status

> **Vertical Slice #5 — shipped 2026-06-03 (v1.0.7q).** OUI.9 / #149. One programmatic read across every service + active policy + most recent audit rows, exposed under the public `/api/v1` namespace so it lives in the SDK + OpenAPI + scenario + e2e triangulation alongside every other shipped slice.

## What this slice delivers

A single `GET /api/v1/status` on the operator-UI BFF returns the full operational picture in one document: every service's `/health` collapsed into a `services` map, the active policy's headline fields, and the most recent rows from both audit streams. Programmatic operators no longer need a 7+ call polling burst (six `/health` plus `GET /api/v1/policy` plus two audit reads) to know "is everything okay right now?" — they hit one endpoint and read one JSON tree.

The fan-out is parallel with a per-service timeout, so a single hung service can only delay the whole response by that bound (default 2 s) rather than serialising into an unbounded wait. Failed services surface as `{"status": "down"}` in the per-service map; the `overall` field rolls them up into one pill ("ok" | "degraded" | "down") for at-a-glance triage.

## Customer surfaces

| Surface | Detail |
|---|---|
| HTTP | `GET /api/v1/status` on operator-UI BFF (port 8090). Always 200; never blocks past the per-service timeout. |
| SDK | `client.get_status()` top-level convenience + `client.status.get()` sub-client method. Returns typed `StatusResponse` dataclass (`generated_at`, `overall`, `services: dict[str, ServiceStatus]`, `active_policy: ActivePolicySnapshot \| None`, `recent: RecentEvents`). |
| OpenAPI | `/api/v1/status` path + four new schemas (`StatusResponse`, `ServiceStatus`, `ActivePolicySnapshot`, `RecentEvents`). New `status` tag. |
| BFF (operator UI) | Same endpoint serves both programmatic operators and the operator UI's Home page when wired through; React Home consumes `/api/v1/status` going forward (the existing `/api/ui/health` is preserved as a smaller-shape sibling for the basic green/red rendering). |

## Response shape

```json
{
  "generated_at": "2026-06-03T18:58:05Z",
  "overall": "ok",
  "services": {
    "policy-manager":   {"status": "ok", "redis": true, "timescaledb": true, "policy_version": 31},
    "autoscaler":       {"status": "ok", "active_target_count": 3, "redis": true, ...},
    "telemetry":        {"status": "ok", "redis": true, "timescaledb": true, "rows_written_1m": 1240},
    "anomaly-detector": {"status": "ok", "runloop_enabled": true, "engine": "threshold", ...},
    "forecasting":      {"status": "ok", "runloop_enabled": true, "engine": "moving_average"},
    "rl-engine":        {"status": "ok", "policy_requested": "ppo", "policy_type": "ppo", "policy_ready": true, "rl_mode": "active", "last_inference_age_seconds": 3.19, "redis": true, "timescaledb": true},
    "lb-sidecar":       {"status": "ok", "redis": true},
    "load-balancer":    {"status": "ok", "upstream_count": 3}
  },
  "active_policy": {
    "operating_mode": "hybrid",
    "safe_mode": false,
    "slo_p95_latency_ms": 200,
    "policy_version": 31
  },
  "recent": {
    "last_policy_change": {"actor": "ops", "field": "safe_mode", "from": false, "to": true, "at": "2026-06-03T18:46:11Z"},
    "last_scaling_event": {"action": "scale_in", "instance_count": 4, "reason": "forecast predicted 0 rps < shed-capacity 400", "at": "2026-06-03T18:57:57Z"}
  }
}
```

## Implementation pointers

- New pure-Python module: `services/operator-ui/bff/aggregator.py` — fan-out, per-service fetch, overall rollup, and response composition. Every IO is injected (`http_get`, `fetch_active_policy`, `fetch_last_*`) so the whole thing is unit-testable with stubs.
- New BFF route: `services/operator-ui/bff/app.py::get_status_v1()` — wires the aggregator to the shared `_http` client + closures that hit policy-manager and autoscaler.
- New SDK module: `clients/python/smartload_client/status.py` — `StatusClient` sub-client + `StatusResponse` / `ServiceStatus` / `ActivePolicySnapshot` / `RecentEvents` dataclasses + `from_dict()` / `to_dict()` round-trip.
- New SDK convenience: `SmartLoadClient.get_status()` top-level method delegating to `self.status.get()`.
- OpenAPI: `/api/v1/status` path + four schemas (`StatusResponse`, `ServiceStatus`, `ActivePolicySnapshot`, `RecentEvents`) + new `status` tag. Schemas use `additionalProperties: true` on `ServiceStatus` so service-specific `/health` fields pass through without spec churn.
- Per-service /health forward-compat: the aggregator copies every key except `service` (redundant) from each /health body, so new fields show up in `/api/v1/status` automatically without code changes.

## Behaviour contract

- **Always 200.** The BFF returns a well-formed status document even when every downstream service is unreachable. Callers route on `overall` and per-service `status`, not HTTP code.
- **`overall` rollup:**
  - `"ok"` — every service reports `status: "ok"`.
  - `"degraded"` — no service is `"down"` but at least one returned a non-`"ok"` status from a reachable /health (e.g. `"degraded"` for a DB-slow service that still serves /health).
  - `"down"` — at least one service is unreachable. The fetcher collapses every reachability failure (timeout, connection refused, non-2xx, malformed JSON, non-object body) to `status: "down"`.
- **Bounded latency.** Fan-out is parallel with a 2 s per-service HTTP timeout. Worst-case wall clock is bounded by the single slowest service's read timeout, plus the (sequential) policy + audit fetches at 2 s each. Operators hitting `/api/v1/status` from a CI loop or dashboard get sub-3-s responses against a healthy stack.
- **Best-effort secondary fetches.** `active_policy` and `recent` are fetched after the service fan-out completes; failures to reach policy-manager or autoscaler for those reads collapse to `null`, but never break the overall response.

## Status

- [x] Aggregator module + unit tests (`services/operator-ui/bff/aggregator.py`, `tests/unit/operator-ui/test_aggregator.py` — 22 tests)
- [x] BFF route `GET /api/v1/status` (`services/operator-ui/bff/app.py::get_status_v1`)
- [x] SDK sub-client + dataclasses + top-level convenience + unit tests (`clients/python/smartload_client/status.py`, additions to `tests/unit/test_smartload_client.py` — 9 tests)
- [x] OpenAPI fragment merged into canonical spec (`/api/v1/status` + four schemas + `status` tag in `docs/openapi/smartload-v1.yaml`)
- [x] Live-validated against the running stack (overall="down" correctly reflects the load-balancer container in restart loop; healthy services pass through with their full /health bodies)
- [x] Scenario script `examples/scenarios/status/status_walk.py`
- [x] E2E test suite `tests/e2e/status/test_status.py`
- [x] §25.9 slice-catalog row added as *Shipped*
- [x] §22 changelog row in SOT (v1.0.7q)
- [x] §11 endpoint table updated with `/api/v1/status`

Open follow-ups (out of scope for this slice):
- Operator UI Home page consumes `/api/v1/status` instead of `/api/ui/health` (richer rendering) — UI sub-pass, separate commit.
- Per-tenant scoping once #129 (multi-tenancy) lands.
- WebSocket / SSE push variant for dashboards that want streaming status rather than polling.
- Make `overall: "degraded"` versus `overall: "down"` configurable — some operators may want unreachable-service to count as degraded (matches the rough acceptance text in #149's body, which contradicts the bullet rules in the same issue). Current implementation follows the bullet rules.
