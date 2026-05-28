# webhook-dispatcher

Outbound HTTP event delivery for SmartLoad. Subscribes to control-plane Redis channels and fans events out to customer-registered URLs with HMAC signing + retries.

## Status

Scaffolded only. Implementation lands with issue #130. Not yet in `docker-compose.yml`, does not run.

## Responsibilities (once implemented)

- Subscribe to `smartload.anomaly`, `smartload.forecast`, `smartload.scale`, `smartload.policy`
- Load registered webhooks from the `webhooks` table (per tenant)
- POST each event to each subscribed URL with `X-SmartLoad-Signature` (HMAC-SHA256 of the body)
- Retry on failure with exponential backoff (5 attempts, ~10 min)
- Persist final-failure rows for the operator UI dead-letter view

## Management API (mounted on this service or proxied through policy-manager — TBD)

- `POST /api/v1/webhooks`
- `GET  /api/v1/webhooks`
- `DELETE /api/v1/webhooks/{id}`

Full spec lands in `docs/openapi/smartload-v1.yaml` when #130 lands.

## See also
- Issue: #130
- Depends on: #129 (multi-tenancy), #132 (API keys), #60 (OpenAPI)
