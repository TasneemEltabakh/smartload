# Policy Management

> **Vertical Slice #1 — shipped 2026-05-14.** First feature manifested end-to-end across every product surface (HTTP, Redis, SDK, runnable scenario, e2e test, operator UI). Every later feature follows this template.

## What this slice delivers

An operator (UI) or external integrator (SDK / HTTP) can read the current operating policy, propose a change, see a diff, commit it, and observe propagation to every dependent service via Redis pub/sub. Every change is persisted as an audit row queryable via REST.

## Customer surfaces

| Surface | Detail |
|---|---|
| HTTP | `GET /api/v1/policy`, `POST /api/v1/policy`, `GET /api/v1/audit/policy` |
| Redis | publishes `smartload.policy` (envelope: `PolicyUpdate`) |
| SDK | `client.get_policy()`, `client.set_policy(patch, actor=...)`, `client.audit_policy(limit)`, `client.subscribe_policy(callback)` |
| UI | Operator UI > Policy page — read, edit, diff preview, commit, audit table |

## Implementation pointers

- Service: `services/policy-manager/app.py`
- Validation: `services/policy-manager/validation.py` (authoritative for the `operating_mode` enum + cross-field invariants)
- Envelope: `services/shared/contracts.py::PolicyUpdate`
- SDK: `clients/python/smartload_client/policy.py`, `events.py` (PolicySubscription)
- UI: `services/operator-ui/web/src/pages/Policy.tsx`, BFF proxy at `services/operator-ui/bff/app.py`
- Audit storage: `policy_changes` hypertable (TimescaleDB)
- Canonical contract: `docs/openapi/smartload-v1.yaml` (`Policy`, `PolicyPatch`, `PolicyUpdateResponse`, `PolicyAuditRow`)
- Redis registry row: `docs/redis-channels.md` → `smartload.policy`

## Status

- [x] Service shipped (T1.4, commit `3577e76`)
- [x] Validation + audit + live reload on the service side
- [x] `GET /api/v1/audit/policy` route added (slice #1)
- [x] OpenAPI fragment merged into canonical spec (#60)
- [x] Redis channel registered in `docs/redis-channels.md` (#128)
- [x] SDK methods + quickstart example (#127, #137)
- [x] Scenario script at `examples/scenarios/policy-management/policy_walk.py` (#126)
- [x] E2E test suite at `tests/e2e/policy-management/test_policy_walk.py`
- [x] UI editor with diff preview, audit table, commit toast (#119, #120)

Open follow-ups: webhook subscription for policy changes (#130); multi-tenant policy storage (#129); API key + RBAC enforcement (#132).

## How to verify

```bash
# 1. Start the stack
docker compose up -d

# 2. Read current policy via the SDK
pip install -e clients/python
python clients/python/examples/quickstart.py

# 3. Walk the slice end-to-end (read → subscribe → toggle → restore → audit)
python examples/scenarios/policy-management/policy_walk.py

# 4. Run the e2e suite (read, write, subscribe, audit)
pytest tests/e2e/policy-management/ -v

# 5. Browser smoke test
# open http://localhost:8090/policy
#   - current policy renders
#   - edit a field in the JSON textarea
#   - diff preview updates
#   - click Commit → toast confirms; audit row appears
#   - reload → state persists

# 6. Lint gates (all clean for this slice)
python scripts/lint-structure.py
python scripts/lint-openapi.py
python scripts/lint-redis-channels.py
```

## Non-goals

- Multi-tenant policy storage (separate slice: see #129)
- Policy schema versioning beyond v1 (separate slice: see #134)
- Programmatic policy templates / inheritance (out of scope for v1)
- Webhook delivery for policy events (separate slice: see #130)
