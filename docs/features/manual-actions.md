# Manual Actions

> **Vertical Slice #3 — shipped 2026-05-22.** Operator-driven override surface: scale-to-target and synthetic-anomaly. Reuses the audit pattern from slice #2 so every manual action lands in the same `/audit` page. Backend + UI + scenario + e2e all green.

## What this slice delivers

When automation isn't doing what you need — 3am incident, demo run, capacity-planning rehearsal — operators can intervene from one place:

- **Scale the backend pool to a specific count**, bypassing the forecast subscription and the cooldown timer. Audit row is prefixed `manual:<actor>:` so the override is grep-able in the scaling-events stream.
- **Mark a backend healthy / degraded / unhealthy**, publishing a synthetic `AnomalyEvent` envelope so downstream consumers react as if the engine had emitted it. Useful for demoing anomaly-driven routing without inducing real failure.

Both actions write into the existing audit streams; the Audit page (slice #2) shows them with `manual:<actor>:` reasons so investigators can distinguish operator intent from automated behaviour.

## Customer surfaces

| Surface | Detail |
|---|---|
| HTTP | `POST /api/v1/scale` on autoscaler (port 8085) + `POST /api/v1/isolate` on anomaly-detector (port 8082). Both accept `actor` + `reason` in the body or via `X-Actor` header. |
| HTTP (dry-run) | `POST /api/v1/actions/simulate` on BOTH services — same body as the real action, same validation, zero side effects. The autoscaler returns the scale plan + live policy bounds; the anomaly-detector returns the synthetic `AnomalyEvent` envelope that would publish. |
| SDK | `client.actions.scale(target_count, actor, reason)`, `client.actions.isolate(backend_id, status, actor, reason)`, plus `simulate_scale(...)` / `simulate_isolate(...)` and the top-level `client.scale(...)` / `client.isolate(...)` / `client.simulate_scale(...)` / `client.simulate_isolate(...)` convenience. |
| BFF (operator UI) | `POST /api/ui/scale` + `POST /api/ui/isolate` proxy to the respective upstreams; `POST /api/ui/actions/simulate/scale` + `POST /api/ui/actions/simulate/isolate` proxy the dry-run path. UI hits one origin. |
| UI | Manual Actions page with two forms (scale + isolate) plus a confirmation modal per action (pending UI sub-pass). Simulate powers a preview-before-apply step in the modal. |

## Implementation pointers

- New module: `services/autoscaler/manual.py` — pure-Python plan logic (`plan_manual_scale`, `ManualScaleError`) tested without docker
- New endpoint: `services/autoscaler/app.py::post_manual_scale()` — validates against live policy, runs cluster step-by-step, writes one `scaling_events` row + publishes one `ScalingEvent` envelope, bumps cooldown clock
- New endpoint: `services/anomaly-detector/app.py::post_manual_isolate()` — publishes synthetic `AnomalyEvent` + writes `backend_health` row
- BFF proxies: `services/operator-ui/bff/app.py::ui_manual_scale()` + `ui_manual_isolate()`
- New SDK module: `clients/python/smartload_client/actions.py` — `ActionsClient` with `scale()` / `isolate()` methods
- `SmartLoadClient` gains an `anomaly_detector_url` constructor parameter (env: `SMARTLOAD_ANOMALY_DETECTOR_URL`, default `http://localhost:8082`) for isolate-call routing
- OpenAPI: `/api/v1/scale` + `/api/v1/isolate` paths and four new schemas (`ManualScaleRequest`, `ManualScaleResponse`, `ManualIsolateRequest`, `ManualIsolateResponse`)
- Audit storage: existing `scaling_events` + `backend_health` hypertables (no schema change)
- UI: `services/operator-ui/web/src/pages/Actions.tsx` — two forms (scale + isolate) plus a disabled-placeholder "Force route weights" form (depends on T2.1); confirmation modal per action with state-change preview; results feed of the last 10 actions; live policy bounds shown in the header

## Dry-run / simulate (#146)

Before committing a manual override during incident response, operators (and
integration tests) can preview it. `POST /api/v1/actions/simulate` lives on
**both** the autoscaler and the anomaly-detector, accepts the **same request
body** as its real counterpart, and runs the **same validation path** —
guaranteeing that a failed simulate implies a failed real action with the same
`400` + `field`. It actuates nothing: no cluster change, no `scaling_events` /
`backend_health` row, no envelope publish, and the autoscaler cooldown clock is
left untouched.

- **Autoscaler** reuses `manual.plan_manual_scale` and returns
  `{would_execute, current_count, target_count, action, cooldown_remaining_s,
  would_audit_reason, policy_bounds:{min_backends, max_backends}}`.
- **Anomaly-detector** reuses `manual.plan_manual_isolate` and returns the full
  synthetic `AnomalyEvent` envelope that would publish
  (`{would_publish, channel, envelope:{event_id, source, version, timestamp,
  payload}, backend_id, status, severity, reason}`) without publishing it.

The "apply this simulation" handle is intentionally out of scope — callers
simply POST the same body to `/scale` or `/isolate` to commit.

## Status

- [x] `POST /api/v1/scale` on autoscaler with validation against min/max
- [x] `POST /api/v1/isolate` on anomaly-detector with status validation
- [x] `POST /api/v1/actions/simulate` (dry-run) on autoscaler + anomaly-detector — same body, same validation, zero side effects (#146)
- [x] OpenAPI fragments + schemas (`ManualScale*`, `ManualIsolate*`, `SimulateScaleResponse`, `SimulateIsolateResponse`)
- [x] BFF proxies (`/api/ui/scale`, `/api/ui/isolate`, `/api/ui/actions/simulate/scale`, `/api/ui/actions/simulate/isolate`)
- [x] SDK methods + unit tests (`scale`, `isolate`, `simulate_scale`, `simulate_isolate`)
- [x] `SmartLoadClient.anomaly_detector_url` parameter + env-var override
- [x] Unit tests for `plan_manual_scale` + `plan_manual_isolate` (validation, direction, reason composition)
- [x] Unit tests for the autoscaler + anomaly-detector simulate routes (dry-run shape, side-effect freedom, validation parity)
- [x] Unit tests for the SDK actions surface (scale / isolate / simulate)
- [x] UI page `services/operator-ui/web/src/pages/Actions.tsx` — scale + isolate forms with confirmation modals, results feed, live policy bounds; "Force route weights" placeholder form disabled with T2.1 tooltip
- [x] Scenario script `examples/scenarios/manual-actions/manual_actions_walk.py` — read policy bounds → simulate scale (assert no audit row) → reject out-of-band simulate → scale → confirm audit row → reject out-of-band → simulate isolate → isolate → reject bad status → restore baseline
- [x] E2E test suite `tests/e2e/manual-actions/test_manual_actions.py` — scale bounds + noop + audit round-trip, isolate happy/bad-status/empty-backend-id, simulate dry-run shapes + validation parity + no-write assertions, BFF proxy parity for all four endpoints
- [x] §25.9 slice-catalog row flipped to *Shipped*

Open follow-ups (out of scope for this slice):
- "Force route weights" form — depends on T2.1 sidecar (#82)
- Bulk manual actions / scripted operator macros
- Manual-override cooldown window so the auto-loop doesn't immediately undo
- "Apply this simulation" handle (re-validates + materialises the previewed action)

## How to verify

Backend-only (this pass):

```bash
# 1. Start the stack
docker compose up -d

# 2. Scale via the SDK
python - <<'PY'
from smartload_client import SmartLoadClient
with SmartLoadClient() as c:
    r = c.scale(4, actor="demo", reason="ops drill")
    print(r["status"], r["action"], "final=", r["final_count"])
    r = c.isolate("test-backend-3", "unhealthy", actor="demo")
    print(r["status"], r["backend_id"])
PY

# 3. Hit the endpoints directly
curl -X POST 'http://localhost:8085/api/v1/scale' \
  -H 'Content-Type: application/json' -H 'X-Actor: ops' \
  -d '{"target_count": 5, "reason": "back to baseline"}'

curl -X POST 'http://localhost:8082/api/v1/isolate' \
  -H 'Content-Type: application/json' -H 'X-Actor: ops' \
  -d '{"backend_id": "test-backend-2", "status": "degraded"}'

# 3b. Dry-run first (#146) — preview without actuating. Same body, same
#     validation, zero side effects.
curl -X POST 'http://localhost:8085/api/v1/actions/simulate' \
  -H 'Content-Type: application/json' -H 'X-Actor: ops' \
  -d '{"target_count": 5, "reason": "capacity drill"}'

curl -X POST 'http://localhost:8082/api/v1/actions/simulate' \
  -H 'Content-Type: application/json' -H 'X-Actor: ops' \
  -d '{"backend_id": "test-backend-2", "status": "degraded"}'

# 4. Watch the manual action appear in the audit log UI
# Open http://localhost:8090/audit — the "manual:ops:" prefix is the
# operator-intent marker.

# 5. Lint gates (clean for this pass)
python scripts/lint-openapi.py
```

UI smoke test pending the UI sub-pass.

## Non-goals

- Mutation of past audit rows (audit is immutable)
- Authn / authz (OUI.7 — Phase 2 SaaS or its own slice)
- LB sidecar consumption of the synthetic AnomalyEvent (T2.1 / #82)
- Per-tenant manual-action scoping (Phase 2 SaaS, #129)
