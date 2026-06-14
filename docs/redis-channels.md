# Redis channel registry

Canonical catalog of every Redis pub/sub channel SmartLoad uses. Every channel mentioned in `services/` source must appear here — enforced by `scripts/lint-redis-channels.py` (permissive today, enforcing later).

Pairs with `docs/openapi/smartload-v1.yaml` (HTTP contract) and `docs/asyncapi/smartload-v1.yaml` (AsyncAPI 3.0 — the machine-readable contract for these channels plus the operator-UI SSE stream) to form the complete external contract surface.

## Channels

### `smartload.policy`
- **Publisher**: `policy-manager`
- **Subscribers**: all services that read the operating policy at runtime
- **Envelope**: `PolicyUpdate` — `services/shared/contracts.py`
- **Publish frequency**: on every accepted policy change (event-driven, not periodic)
- **Retention**: pub/sub (no buffer)
- **Webhook-eligible**: yes (planned via #130)

Example payload:
```json
{
  "version": 1,
  "operating_mode": "hybrid",
  "safe_mode": false,
  "min_backends": 1,
  "max_backends": 5,
  "slo_p95_latency_ms": 200,
  "anomaly_latency_multiplier": 3.0,
  "per_instance_capacity_rps": 100,
  "autoscaler_cooldown_seconds": 60,
  "timestamp": "2026-05-14T12:00:00Z"
}
```

### `smartload.anomaly`
- **Publisher**: `anomaly-detector`
- **Subscribers**: `lb-sidecar` (T2.1 — excludes/includes backends on health change), operator-ui (live-engines feed)
- **Envelope**: `AnomalyEvent`
- **Publish frequency**: every `POLL_INTERVAL_SECONDS` per backend (default 5s)
- **Retention**: pub/sub
- **Webhook-eligible**: yes (planned via #130)

Example payload:
```json
{
  "version": 1,
  "backend_id": "test-backend-3",
  "status": "degraded",
  "score": 0.82,
  "timestamp": "2026-05-14T12:00:05Z"
}
```

### `smartload.forecast`
- **Publisher**: `forecasting`
- **Subscribers**: `autoscaler`
- **Envelope**: `ForecastResult`
- **Publish frequency**: every `POLL_INTERVAL_SECONDS` (default 60s)
- **Retention**: pub/sub
- **Webhook-eligible**: yes (planned via #130)

Example payload:
```json
{
  "version": 1,
  "horizon_minutes": 5,
  "predicted_rps": 420.0,
  "confidence_lower": 380.0,
  "confidence_upper": 460.0,
  "timestamp": "2026-05-14T12:01:00Z"
}
```

### `smartload.routing`
- **Publisher**: `rl-engine`
- **Subscribers**: `lb-sidecar` (T2.1 — rewrites NGINX upstream weights when `mode=active`)
- **Envelope**: `RoutingRecommendation`
- **Publish frequency**: every `POLL_INTERVAL_SECONDS` (default 5s)
- **Retention**: pub/sub
- **Webhook-eligible**: yes (planned via #130)

Example payload:
```json
{
  "version": 1,
  "mode": "shadow",
  "server_rankings": [
    {"backend_id": "test-backend-1", "score": 0.9},
    {"backend_id": "test-backend-2", "score": 0.7}
  ],
  "timestamp": "2026-05-14T12:00:05Z"
}
```

### `smartload.scale`
- **Publisher**: `autoscaler`
- **Subscribers**: `lb-sidecar` (v1.0.7z #164 — re-queries Docker pool + rewrites `upstream.conf`), `operator-ui` (live-engines feed + audit), future `webhook-dispatcher`
- **Envelope**: `ScalingEvent`
- **Publish frequency**: on action (event-driven, not periodic)
- **Retention**: pub/sub + bounded ring buffer (planned via #121)
- **Webhook-eligible**: yes (planned via #130)

Example payload:
```json
{
  "version": 1,
  "action": "scale_out",
  "target_count": 4,
  "current_count": 3,
  "reason": "forecast_predicted_rps=420 exceeds 3 backends × 100 capacity",
  "timestamp": "2026-05-14T12:01:00Z"
}
```

## Failure semantics

- No subscriber present at publish time: message is dropped silently (pub/sub default). Consumers that need durability subscribe before the publisher starts, or read the audit hypertable.
- Publisher down: subscribers see no traffic on the channel. Each subscriber documents its degraded behavior in `docs/architecture/failure-modes.md` (#58).

## Tenant namespacing (planned)

After multi-tenancy lands (#129), every channel becomes `t:<tenant_id>:smartload.<topic>`. Single-tenant deployments default to `t:default:`.

## CI guardrail

`scripts/lint-redis-channels.py` greps `services/` for the regex `["']smartload\.[a-z_]+["']` and asserts every match appears in this file.
