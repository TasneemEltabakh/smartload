# Examples

Runnable end-to-end demonstrations and reference deployments.

This folder is the universal "show me how it works" surface for SmartLoad. Everything here is designed to be executed, not just read.

## Subfolders

- `scenarios/` — one Python script per feature. Each script triggers the feature against a running stack and reports observable behavior to stdout. Used during development, demos, and CI.
- `deployments/` — reference deployment shapes (single-tenant compose, multi-tenant Helm). Each subfolder is self-contained.

## Anti-patterns we explicitly reject

- **YAML-only examples** (Argo Rollouts' mistake). Scripts here run and produce observable output. If a feature can only be demonstrated via `kubectl apply`, that goes under `deployments/`, not `scenarios/`.
- **Out-of-tree examples** (Temporal's mistake). All examples live in this repo, version-locked with the code.
- **Stale examples** — every script under `scenarios/` is invoked in CI. Broken scripts fail the build.
