# Audit Log Viewer

> **Vertical Slice #2 — backend pass shipped 2026-05-21.** Builds on the policy-management slice to give operators a unified investigation surface across both audit streams. UI page lands in the follow-up.

## What this slice delivers

An operator can browse, in one place, every policy change *and* every scaling action SmartLoad has taken — with timestamps, actors, before/after values, and the reasons the autoscaler gave for each scale event. Investigations like "who flipped safe_mode at 03:17?" or "which forecast triggered the last scale-out?" become single-query rather than DB-trawl operations.

The two audit streams continue to live in separate hypertables on separate services (policy_changes on policy-manager, scaling_events on autoscaler) — this slice does **not** consolidate them server-side. It unifies them at the SDK and UI layers so consumers see one logical surface.

## Customer surfaces

| Surface | Detail |
|---|---|
| HTTP | `GET /api/v1/audit/policy` (policy-manager, already shipped slice #1) + **new** `GET /api/v1/audit/scaling` (autoscaler). Both accept `?limit=N`, default 50, max 1000. |
| SDK | `client.audit.policy(limit)`, `client.audit.scaling(limit)`, `client.audit.list(kind, limit)`, and top-level `client.list_audit(kind, limit)` convenience |
| BFF (operator UI) | `/api/ui/audit/policy` (existed) + new `/api/ui/audit/scaling` — both proxy `?limit` through; UI hits one origin |
| UI | Audit page with filterable tables (kind, action, actor, time range), cross-linked from the Policy page's audit toast |

## Implementation pointers

- New endpoint: `services/autoscaler/app.py::get_audit_scaling()` — mirrors policy-manager's audit pattern exactly (limit parse + cap, 400 on bad limit, 503 on DB unreachable)
- New SQL constant: `services/shared/queries.py::SCALING_AUDIT_QUERY`
- New SDK module: `clients/python/smartload_client/audit.py` exposing `AuditClient` with `policy()` / `scaling()` / `list(kind)` methods
- SDK constructor: `SmartLoadClient(base_url=..., autoscaler_url=...)` — the new `autoscaler_url` parameter routes scaling-audit traffic to the autoscaler service (default `http://localhost:8085`, env var `SMARTLOAD_AUTOSCALER_URL`)
- BFF proxy: `services/operator-ui/bff/app.py::ui_scaling_audit()` — points at `SERVICE_URLS["autoscaler"]`
- OpenAPI: `/api/v1/audit/scaling` path + `ScalingAuditRow` schema in `docs/openapi/smartload-v1.yaml`
- Audit storage: `scaling_events` hypertable (TimescaleDB) — schema unchanged; this slice only adds a read endpoint
- UI: `services/operator-ui/web/src/pages/Audit.tsx` (pending — UI sub-pass)

## Status

- [x] Service endpoint shipped (autoscaler `GET /api/v1/audit/scaling`)
- [x] OpenAPI fragment merged into canonical spec (`/api/v1/audit/scaling` + `ScalingAuditRow`)
- [x] BFF proxy (`/api/ui/audit/scaling`)
- [x] SDK methods + unit tests (`client.audit.{policy,scaling,list}`, `client.list_audit`)
- [x] SDK `autoscaler_url` parameter + env-var override
- [ ] UI page with filterable tables (sub-pass)
- [ ] Scenario script `examples/scenarios/audit-log/audit_walk.py`
- [ ] E2E test suite `tests/e2e/audit-log/`
- [ ] §25.9 slice-catalog row flipped to *Shipped*

Open follow-ups (out of scope for this slice):
- Time-range filter (`?since=`, `?until=`) on both endpoints
- Action-type filter (`?action=scale_out`) on the scaling endpoint
- Pagination beyond `limit` (cursor-based)
- Webhook for new audit rows (#130)
- Multi-tenant scoping (#129 — Phase 2 SaaS)

## How to verify

Backend-only (this pass):

```bash
# 1. Start the stack
docker compose up -d

# 2. Read scaling audit via the SDK
python - <<'PY'
from smartload_client import SmartLoadClient
with SmartLoadClient() as c:
    print("policy audit rows:", len(c.list_audit("policy", limit=10)))
    print("scaling audit rows:", len(c.list_audit("scaling", limit=10)))
PY

# 3. Hit the endpoint directly
curl 'http://localhost:8085/api/v1/audit/scaling?limit=10'

# 4. Hit via the BFF (what the UI uses)
curl 'http://localhost:8090/api/ui/audit/scaling?limit=10'

# 5. Lint gates (all clean for this pass)
python scripts/lint-structure.py
python scripts/lint-openapi.py
python scripts/lint-redis-channels.py
```

UI smoke test pending the UI sub-pass.

## Non-goals

- Server-side consolidation of the two audit streams into a single table (deliberately kept separate — owner of write is owner of read)
- Tenant-scoped audit views (Phase 2 SaaS, #129)
- Webhook-style audit subscription (lives with #130 webhook delivery)
- Mutation surface on audit rows (audit is immutable observability)
- Time-bucket aggregations / counts-by-action (a metrics concern, not audit)
