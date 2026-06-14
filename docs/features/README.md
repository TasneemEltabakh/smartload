# Feature manifests

One file per shippable feature of the SmartLoad product. Each manifest is the single source of truth for that feature's scope, status, and acceptance.

## Why this folder exists

In the systems we surveyed (Istio, Flagger, Argo Rollouts, KEDA, Temporal, OTel Collector), architecture documentation routinely drifts away from the code because nothing enforces the link. SmartLoad treats feature manifests as part of the build: a feature is not "done" until its manifest exists, every box is checked, the runnable scenario exists, and the e2e test passes.

## How a feature relates to the rest of the repo

For feature `<name>`:

| Surface | Location |
|---|---|
| Manifest (this folder) | `docs/features/<name>.md` |
| Runnable end-to-end scenario | `examples/scenarios/<name>/` (or `examples/scenarios/<name>.py`) |
| End-to-end test | `tests/e2e/<name>/` |
| HTTP contract | section of `docs/openapi/smartload-v1.yaml` |
| Redis contract | rows in `docs/redis-channels.md` |
| Implementation | one or more `services/<role>/` folders |

## Structure contract (enforced in CI, #139)

As of #139 all three anti-drift lints run `--strict` in the `structure-lint` CI job — a violation **fails the build**, so the structure is a contract, not a suggestion:

- **`scripts/lint-structure.py --strict`** — every `tests/e2e/<feature>/` must have a sibling `docs/features/<feature>.md` **and** `examples/scenarios/<feature>/` (or `<feature>.py`); every `services/<svc>/` and every `engines/<x>/` · `policies/<x>/` · `lb_adapters/<x>/` plugin folder must carry a `README.md`, and each engine/policy plugin must carry a `test_*.py`. Half a slice is visible by an empty folder.
- **`scripts/lint-openapi.py --strict`** — every `@app.route("/api/v1/...")` in `services/` must appear in `docs/openapi/smartload-v1.yaml`.
- **`scripts/lint-redis-channels.py --strict`** — every `smartload.<channel>` literal in `services/` must be registered in `docs/redis-channels.md` (genuine non-channel `smartload.*` Docker labels are allowlisted in the lint).

Run all three locally before pushing: `python scripts/lint-structure.py --strict && python scripts/lint-openapi.py --strict && python scripts/lint-redis-channels.py --strict`.

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
- [adaptive-bench](adaptive-bench.md) — 5-phase Locust shape + 3 async collectors + join + 4 plots + SUMMARY for RQ4 evidence (#155 R1 + #156 R2 + #157 R3, shipped 2026-06-10)

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
