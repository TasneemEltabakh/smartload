# Architecture

Design documents — one per cross-cutting concern. Each document describes a *system-level* decision, distinct from the feature manifests in `docs/features/` (which describe *customer-level* slices).

## Documents

- `control-plane.md` — what runs in the decision plane and how the components talk
- `data-plane.md` — what the request path looks like end-to-end
- `lb-adapter.md` — adapter interface that decouples decision plane from NGINX (#136)
- `multi-tenancy.md` — `tenant_id` propagation across DB, Redis, policy storage, API (#129)
- `versioning-policy.md` — HTTP API + policy schema + Redis envelope + SDK semver discipline (#134)
- `failure-modes.md` — per-service degraded behavior, when each upstream dies (#58)

## Distinction from SOT

The canonical Source of Truth (`docs/SOURCE_OF_TRUTH.html`) remains the locked specification. Documents here are *expansions* of SOT sections that benefit from longer prose, diagrams, or rationale that doesn't fit the SOT format. When this folder's content conflicts with the SOT, the SOT wins; update both in the same PR.

## CI enforcement

`scripts/lint-structure.py` checks that referenced architecture docs exist when filed in issues or manifests. Missing docs surface as warnings in permissive mode.
