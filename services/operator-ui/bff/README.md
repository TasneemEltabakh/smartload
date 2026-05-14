# operator-ui / bff

Flask BFF (backend-for-frontend) for the operator UI.

## Responsibilities

- Aggregate `/health` from every SmartLoad service for the Home page
- Proxy REST calls to policy-manager, autoscaler, telemetry (with the operator's session credentials)
- Serve Swagger UI as a static asset (`/api/docs`) from `docs/openapi/smartload-v1.yaml`
- Hold session state for the human operator's login (#125)
- Stream Redis events to the web frontend via SSE (#121)
- Provide BFF-private endpoints under `/api/ui/` for chart data, log tails, etc.

## Status

Scaffolded only. Implementation lands with #119.

## Planned files

- `app.py`
- `Dockerfile`
- `requirements.txt`
- `routes/` — split by page (health, policy, events, audit, actions, dashboards, logs)
