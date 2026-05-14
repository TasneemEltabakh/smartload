# Helm chart for SmartLoad

Kubernetes deployment for self-host and SaaS control plane.

## Status

Scaffolded only. Implementation lands with issue #133.

## Layout

- `smartload/` — the chart itself
  - `Chart.yaml`
  - `values.yaml` — defaults + documented overrides
  - `templates/` — Deployments, Services, ConfigMap, Secret, HPA example, Ingress example, ServiceMonitor (if Prometheus Operator installed)
  - `README.md` — minikube + kind quickstart, production-overrides example

## Quickstart (planned)

```bash
helm install smartload ./infrastructure/helm/smartload
```

All services reach Ready within ~2 minutes in a clean kind cluster.

## See also

- Issue: #133
- Raw manifests (cluster-wide CRDs, namespaces, RBAC) live in `infrastructure/k8s/`
