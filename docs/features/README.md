# Feature manifests

One file per shippable feature of the SmartLoad product. Each manifest is the single source of truth for that feature's scope, status, and acceptance.

## Why this folder exists

In the systems we surveyed (Istio, Flagger, Argo Rollouts, KEDA, Temporal, OTel Collector), architecture documentation routinely drifts away from the code because nothing enforces the link. SmartLoad treats feature manifests as part of the build: a feature is not "done" until its manifest exists, every box is checked, the runnable scenario exists, and the e2e test passes.

## How a feature relates to the rest of the repo

For feature `<name>`:

| Surface | Location |
|---|---|
| Manifest (this folder) | `docs/features/<name>.md` |
| Runnable end-to-end scenario | `examples/scenarios/<name>.py` |
| End-to-end test | `tests/e2e/<name>/` |
| HTTP contract | section of `docs/openapi/smartload-v1.yaml` |
| Redis contract | rows in `docs/redis-channels.md` |
| Implementation | one or more `services/<role>/` folders |

The structure lint (`scripts/lint-structure.py`) requires all three of the first three to exist together. Half a slice is visible by an empty folder.

## Manifest template

```markdown
# <Feature name>

## What this slice delivers
One paragraph. Customer-facing, not implementation-facing.

## Customer surfaces
- HTTP: routes and methods
- Redis: channels published / consumed
- SDK: methods exposed
- UI: pages affected

## Implementation pointers
- Service: `services/...`
- Envelope: `services/shared/contracts.py::...`
- SDK: `clients/python/smartload_client/...`
- UI: `services/operator-ui/web/src/pages/...`
- Storage: hypertables / config files

## Status
- [ ] Service shipped
- [ ] OpenAPI fragment merged
- [ ] Redis channel registered
- [ ] SDK methods + example
- [ ] UI page
- [ ] Scenario script
- [ ] E2E test passes

## How to verify
Commands a reader can run to see the feature work end-to-end.
```

## Current manifests
- [policy-management](policy-management.md) — read / write / audit the operating policy
- anomaly-routing — *not yet manifested*
- forecast-autoscale — *not yet manifested*
- scaling-actions — *not yet manifested*
- routing-decisions — *not yet manifested*
