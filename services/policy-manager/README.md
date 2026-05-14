# policy-manager

Owns the canonical operating policy for the SmartLoad stack and the audit trail of every change.

## Role
- Reads / writes `config/policy.yaml`
- Validates incoming policy changes against the canonical SOT schema
- Persists every change as a row in the `policy_changes` hypertable (audit)
- Publishes `PolicyUpdate` to the `smartload.policy` Redis channel on every accepted change
- Supports live reload (no service restart needed by subscribers)

## HTTP endpoints
| Method | Path | Purpose |
|---|---|---|
| GET  | `/health` | uniform health (Redis + TimescaleDB) |
| GET  | `/api/v1/policy` | current policy as JSON |
| POST | `/api/v1/policy` | propose + commit a policy change |
| GET  | `/api/v1/audit/policy` | recent audit rows |

Full spec: `docs/openapi/smartload-v1.yaml`

## Redis channels published
- `smartload.policy` — payload: `PolicyUpdate` (see `services/shared/contracts.py`)

Full registry: `docs/redis-channels.md`

## Env vars
- `TIMESCALEDB_URL`
- `REDIS_URL`
- `POLICY_FILE` (default `/app/config/policy.yaml`)
- `PORT` (default `8086`)

## Status
Shipped — T1.4 (commit `3577e76`). Validation + audit + live reload all live.

## See also
- Feature manifest: `docs/features/policy-management.md`
- Tests: `tests/integration/test_policy_manager.py`, `tests/integration/test_policy_validation.py`
