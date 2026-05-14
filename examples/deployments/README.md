# Reference deployments

Self-contained example deployment shapes. Pick the one that matches your environment.

## Subfolders (planned)

- `single-tenant-compose/` — minimal docker-compose for self-host trial
- `multi-tenant-helm/` — Kubernetes deployment via the Helm chart in `infrastructure/helm/smartload/`

Each subfolder is a complete, runnable example. README in each explains the 5-minute quickstart.

## When to add a deployment shape

- A real customer has asked for it
- The shape exercises configuration that is not obvious from the chart values

Do not pre-create deployment shapes for hypothetical scenarios.
