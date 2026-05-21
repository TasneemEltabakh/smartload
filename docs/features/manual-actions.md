# Manual Actions

> **Vertical Slice #3 — backend pass shipped 2026-05-21.** Operator-driven override surface: scale-to-target and synthetic-anomaly. Reuses the audit pattern from slice #2 so every manual action lands in the same `/audit` page. UI page lands in the follow-up.

## What this slice delivers

When automation isn't doing what you need — 3am incident, demo run, capacity-planning rehearsal — operators can intervene from one place:

- **Scale the backend pool to a specific count**, bypassing the forecast subscription and the cooldown timer. Audit row is prefixed `manual:<actor>:` so the override is grep-able in the scaling-events stream.
- **Mark a backend healthy / degraded / unhealthy**, publishing a synthetic `AnomalyEvent` envelope so downstream consumers react as if the engine had emitted it. Useful for demoing anomaly-driven routing without inducing real failure.

Both actions write into the existing audit streams; the Audit page (slice #2) shows them with `manual:<actor>:` reasons so investigators can distinguish operator intent from automated behaviour.

## Customer surfaces

| Surface | Detail |
|---|---|
| HTTP | `POST /api/v1/scale` on autoscaler (port 8085) + `POST /api/v1/isolate` on anomaly-detector (port 8082). Both accept `actor` + `reason` in the body or via `X-Actor` header. |
| SDK | `client.actions.scale(target_count, actor, reason)`, `client.actions.isolate(backend_id, status, actor, reason)`, plus top-level `client.scale(...)` / `client.isolate(...)` convenience. |
| BFF (operator UI) | `POST /api/ui/scale` + `POST /api/ui/isolate` proxy to the respective upstreams. UI hits one origin. |
| UI | Manual Actions page with two forms (scale + isolate) plus a confirmation modal per action (pending UI sub-pass). |

## Implementation pointers

- New module: `services/autoscaler/manual.py` — pure-Python plan logic (`plan_manual_scale`, `ManualScaleError`) tested without docker
- New endpoint: `services/autoscaler/app.py::post_manual_scale()` — validates against live policy, runs cluster step-by-step, writes one `scaling_events` row + publishes one `ScalingEvent` envelope, bumps cooldown clock
- New endpoint: `services/anomaly-detector/app.py::post_manual_isolate()` — publishes synthetic `AnomalyEvent` + writes `backend_health` row
- BFF proxies: `services/operator-ui/bff/app.py::ui_manual_scale()` + `ui_manual_isolate()`
- New SDK module: `clients/python/smartload_client/actions.py` — `ActionsClient` with `scale()` / `isolate()` methods
- `SmartLoadClient` gains an `anomaly_detector_url` constructor parameter (env: `SMARTLOAD_ANOMALY_DETECTOR_URL`, default `http://localhost:8082`) for isolate-call routing
- OpenAPI: `/api/v1/scale` + `/api/v1/isolate` paths and four new schemas (`ManualScaleRequest`, `ManualScaleResponse`, `ManualIsolateRequest`, `ManualIsolateResponse`)
- Audit storage: existing `scaling_events` + `backend_health` hypertables (no schema change)
- UI: `services/operator-ui/web/src/pages/Actions.tsx` (pending — UI sub-pass)

## Status

- [x] `POST /api/v1/scale` on autoscaler with validation against min/max
- [x] `POST /api/v1/isolate` on anomaly-detector with status validation
- [x] OpenAPI fragments + 4 new schemas
- [x] BFF proxies (`/api/ui/scale`, `/api/ui/isolate`)
- [x] SDK methods + unit tests
- [x] `SmartLoadClient.anomaly_detector_url` parameter + env-var override
- [x] 15 unit tests for `plan_manual_scale` (validation, direction, reason composition)
- [x] 11 unit tests for the SDK actions surface
- [ ] UI page `services/operator-ui/web/src/pages/Actions.tsx` with confirmation modals (sub-pass)
- [ ] Scenario script `examples/scenarios/manual-actions/manual_actions_walk.py`
- [ ] E2E test suite `tests/e2e/manual-actions/`
- [ ] §25.9 slice-catalog row flipped to *Shipped*

Open follow-ups (out of scope for this slice):
- "Force route weights" form — depends on T2.1 sidecar (#82)
- Bulk manual actions / scripted operator macros
- Manual-override cooldown window so the auto-loop doesn't immediately undo

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
