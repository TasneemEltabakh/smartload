# Slice checklist

A slice ships when every layer below is present. Half-shipped slices break the contract that a feature in the SOT is a feature in the running stack.

## Layers a slice must touch

| # | Layer | Concrete artefact |
|---|---|---|
| 1 | Service code | One or more `services/<role>/` folders implementing the behaviour. New strategies live in `services/<svc>/engines/<plugin>/` or `policies/<plugin>/`. |
| 2 | Envelope contract | If new Redis events are emitted, the envelope type is defined in `services/shared/contracts.py` and the channel is registered in `docs/redis-channels.md`. |
| 3 | HTTP contract | If new endpoints exist, they are added to `docs/openapi/smartload-v1.yaml` with request/response schemas, error responses, and examples. The path follows `/api/v1/...`. |
| 4 | Unit tests | `tests/unit/<svc>/` for pure-Python logic — parsers, validators, decision functions. Runs in the `unit-tests` CI job without docker. |
| 5 | E2E test | `tests/e2e/<feature>/` exercises the slice through Redis + DB + services running in compose. Runs in the `compose-test` CI job. |
| 6 | Runnable scenario | `examples/scenarios/<feature>/<feature>_walk.py` — a single script a reader can run after `docker compose up` to see the slice work end to end. Prints expected output. |
| 7 | SDK | If the slice has a customer surface, `clients/python/smartload_client/<area>.py` adds the methods + tests. The quickstart in `clients/python/examples/quickstart.py` references the new methods when relevant. |
| 8 | UI | If the slice has an operator surface, `services/operator-ui/web/src/pages/<Page>.tsx` adds the view; the BFF (`services/operator-ui/bff/app.py`) proxies any new HTTP routes. |
| 9 | Feature manifest | `docs/features/<feature>.md` exists, follows the template in `docs/features/README.md`, every checkbox is ticked, non-goals are listed. |
| 10 | SOT alignment | Relevant SOT updates: §6.3 module card status, §8.x deep-dive (Internal logic, Acceptance criteria, Current state, Gap to close), §11 REST table if endpoints changed, §18 Build Status pill, §22 changelog row, §25.9 Slice catalog row flipped to *Shipped*. |

## Gating CI

| Script | Invariant |
|---|---|
| `scripts/lint-structure.py` | Every `tests/e2e/<feature>/` has a sibling `docs/features/<feature>.md` and `examples/scenarios/<feature>/`. No orphan halves. |
| `scripts/lint-openapi.py` | Every `/api/v1/*` route in `services/` is in `docs/openapi/smartload-v1.yaml`. |
| `scripts/lint-redis-channels.py` | Every `smartload.<topic>` string in `services/` is in `docs/redis-channels.md`. |

All three scripts ship in permissive mode until #139 flips them strict. Even in permissive mode, the slice author runs them locally before declaring done.

The slice's own e2e test must pass on the live compose stack in CI's `compose-test` job.

## Commit discipline

- Service code + tests in one commit.
- SOT alignment in a **separate** commit on the same branch / PR so it's reviewable on its own.
- Do not push the code commit without the SOT commit. The two commits land together or neither lands.

## What is **not** a slice

These look like slices but aren't, because they don't deliver a customer-visible capability on their own:

- **Foundation passes** — engine-wrapper integration (#138 ✓ complete), DB migrations folder (#141), API versioning policy (#134), correlation IDs (#143), test reorg (#140), the structural-lint flip (#139), per-task acceptance harness (#117). Each ships as a horizontal commit + SOT update, but does not get a manifest in `docs/features/`. See SOT §25.9 for the current foundation roster.

  When a foundation pass touches multiple services (the #138 cutover is the canonical example), ship **one service at a time behind a `<SVC>_RUNLOOP_ENABLED=false` feature flag**, validate on the live compose-test stack before replicating to siblings, and only flip the default to `true` once the dev stack has smoked clean. This is round-based, not big-bang. #138 demonstrated the pattern in three rounds on 2026-05-21: round 1 anomaly-detector → round 2 forecasting → round 3 rl-engine. Each round used the same two-commit shape (code + tests; SOT alignment), the same `app.py` + `runloop.py` split, fast-forward merge to `main`, and live-stack CI validation before the next round started.
- **Bug fixes** — patches to an already-shipped slice update the existing manifest's status notes; no new manifest.
- **Pure refactors** — visible only in the diff; SOT update only if behaviour or contract shifted.

## Phase scope

SmartLoad's current phase is **single-tenant middleware**. Slices in current scope must not require `tenant_id` plumbing, API-key authentication, RBAC, rate limiting, or per-tenant Redis namespacing — those belong to the Phase 2 SaaS adaptation track (SOT §25 *Phase scope* callout). If a candidate slice's contract appears to need any of them, check whether the slice belongs in Phase 2 instead.

## Why this rigid

The lint scripts and the manifest folder exist because every architecture doc surveyed (Istio, Flagger, KEDA, Argo Rollouts, Temporal, OTel Collector) drifted from its code within a year. The slice checklist is the structural reason SmartLoad shouldn't drift. Half a slice is visible to a future reviewer as a checkbox unchecked and a lint warning — not as a hidden gap.
