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

## Aggregation endpoints (real data, graceful degradation)

Every `/api/ui/*` endpoint composes real upstream data and degrades to a clean
typed shape (empty series / null fields / empty list) on upstream failure — the
UI shows a calm placeholder rather than an error. The endpoints below back the
KPI tiles, the forecast hero chart, the Helmsman RL control, the Ledger
isolation rows, and the System view.

- `GET /api/ui/metrics/trends` — per-KPI recent series + window-over-window
  delta + human label (throughput rpm, p95 latency, SLO %, error rate %, active
  backends). Source: telemetry `rpm` / `latency` / `slo` / `backends` + the
  autoscaler scaling audit (active backends). Empty series + null delta on
  failure.

- `GET /api/ui/metrics/forecast-summary?window=N` — aligned actual-vs-forecast
  series + confidence band + the scale-ahead decision marker. Source:
  forecasting `/api/v1/forecasts`, telemetry `rpm` (actual, normalised to rps),
  and the autoscaler scaling audit (latest forecast-driven actuation). Each part
  degrades to empty/null independently.

- `GET /api/ui/engines/rl/mode` — current RL routing mode + recommended mode +
  whether promotion is operator-actionable. **CASE B: there is no safe runtime
  write path.** RL mode is pinned at deploy time by the rl-engine `RL_MODE`
  environment variable; `rl_mode` is deliberately not a policy field (the
  policy-manager rejects it). The published mode is composed from three gates
  (rl-engine `runloop.effective_mode`): `RL_MODE` env, policy `safe_mode`, and
  policy `operating_mode`. This endpoint is therefore **read-only** —
  `actionable` is always `false` and there is no `POST` counterpart — so the
  Helmsman "Promote to active" control is presented honestly as a deploy-time
  recommendation. The two operator-writable gates (`safe_mode`,
  `operating_mode`) are surfaced so the UI can explain what would still need to
  change. Reads rl-engine `/api/v1/engine/state` (`rl_mode_env`, with a `/health`
  `rl_mode` fallback) + policy-manager `/api/v1/policy`. Current mode null on
  rl-engine failure.

- `GET /api/ui/audit/isolation?window=N&limit=M` — real isolation / exclusion
  events for the Ledger (one row per `status != "healthy"` verdict): time,
  backend_id, status, score, severity, actor, reason. Source: anomaly-detector
  `/api/v1/anomaly/history` (backend_health verdicts) enriched from the live
  `smartload.anomaly` ring buffer (severity + metric evidence + publishing
  source as actor; the backend_health table has no actor/reason column, so the
  engine source is the default actor and the reason is derived from the
  evidence). Empty list on failure.

- `GET /api/ui/system/topology` — whole-system live topology for the System
  view: every service as a node (id, display name, role, health status,
  last-activity, one key live metric) + the data-flow edges between them. The
  two headless OTLP shippers (`resource-collector`, `lb-otel-shipper`) have no
  HTTP surface, so they appear with status `headless` and no probe — never
  omitted. Source: the health fan-out + `SERVICE_URLS` + the engines ring buffer
  (channel last-activity). Unreachable services show status `unreachable` rather
  than dropping out of the graph.
