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
- [policy-management](policy-management.md) — read / write / audit the operating policy (slice #1, shipped 2026-05-14)
- [audit-log](audit-log.md) — browse `policy_changes` + `scaling_events` history (slice #2, shipped 2026-05-21, #122)
- [manual-actions](manual-actions.md) — operator overrides for scale + isolate, surfaced with `manual:<actor>:` audit prefix (slice #3, shipped 2026-05-22, #123)

## Planned slices

The full slice catalog (with foundation dependencies and status) lives in SOT §25.9. Slices on deck:

- **anomaly-detection** — real-time anomaly events on UI + SDK + webhooks (depends on #138 + #101)
- **forecasting** — workload forecast drives the autoscaler; UI chart + CI band (depends on #138 + revised model PR)
- **webhook-delivery** — HMAC-signed outbound HTTP for integrators who can't talk to Redis (#130; depends on #141 + #134)
- **live-engines** — real-time engine state stream in operator UI (#121; depends on #138)
- **rl-routing** — RL shadow → active routing recommendations (depends on #138 + #27 PPO training)
- **embedded-metrics** — Grafana panels embedded in operator UI (#131)
- **named-strategies** — `POST /api/v1/policy/strategy` alias endpoint over `operating_mode` primitives (#150; extends `policy-management.md`)
- **simulate-actions** — `POST /api/v1/actions/simulate` dry-run endpoints (#146; extends `manual-actions.md`)
- **status-aggregator** — consolidated `GET /api/v1/status` on the BFF (#149)

Adjacent foundation passes and delivery artefacts also on deck — see SOT §25.9 *Integration adoptions* table:
- `smartload.yml` consolidation (#145) — foundation pass, single client-config file
- HAProxy adapter (#147) — foundation pass, implements the existing `LoadBalancerAdapter` ABC
- Baseline-LB vs SmartLoad benchmark (#148) — delivery artefact under `experiments/`, blocked on #82

## Slice acceptance contract

What it means for a slice to be "done" is defined in [SLICE_CHECKLIST.md](SLICE_CHECKLIST.md). Every manifest references it and must satisfy every layer listed there.
