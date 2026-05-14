# SmartLoad Helm chart

## Status

Scaffolded only. Templates are pending — see issue #133.

## Quickstart (once implemented)

```bash
# Local development (kind / minikube)
helm install smartload ./infrastructure/helm/smartload

# Production-shaped install with external TimescaleDB + Redis
helm install smartload ./infrastructure/helm/smartload \
  --set timescaledb.external=true \
  --set timescaledb.url=postgresql://user:pass@db.internal:5432/smartload \
  --set redis.external=true \
  --set redis.url=redis://redis.internal:6379 \
  --set ingress.enabled=true \
  --set ingress.host=smartload.example.com
```

## What this chart deploys

- One Deployment + Service per SmartLoad service
- StatefulSet for TimescaleDB + Redis (unless `external: true`)
- ConfigMap holding `policy.yaml`
- Secret holding DB password + (future) API-key signing secret
- HPA example for `test-backend`
- Ingress + TLS (when enabled)
- ServiceMonitor (when Prometheus Operator detected)

## Anti-pattern explicitly rejected

Operator-pattern (CRD-based) deployment. Helm is the only supported mode in v1. CRDs add operational surface area without adoption gain at this stage.
