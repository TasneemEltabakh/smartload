# Control plane

Services that *decide* what the data plane should do. They do not see live request traffic; they read aggregated telemetry from TimescaleDB and react via the Redis control bus.

## Members

| Service | Role | Reads | Publishes |
|---|---|---|---|
| anomaly-detector | flag unhealthy backends | TimescaleDB latency/errors | `smartload.anomaly` |
| forecasting | predict short-horizon RPS | TimescaleDB request rate | `smartload.forecast` |
| rl-engine | rank backends for routing | TimescaleDB state per backend | `smartload.routing` |
| autoscaler | adjust backend pool size | `smartload.forecast`, `smartload.policy` | `smartload.scale` |
| policy-manager | own the operating policy | `config/policy.yaml` | `smartload.policy` |

## Control bus

Redis pub/sub. Channels listed in `docs/redis-channels.md`. Pub/sub is fire-and-forget by design; the only durable surface is the audit + scaling-events hypertables and (for #121) the bounded ring buffer.

## Singleton vs scalable

Control-plane services that own decisions are singletons in v1: `policy-manager`, `autoscaler`. Replicating them creates split-brain risk. The pure-decision services (`anomaly-detector`, `forecasting`, `rl-engine`) can run multiple replicas with stable hash-partitioned input, but v1 ships one of each.

## Why this split

The control plane never sits in the critical request path. A failure in `anomaly-detector` does not stop traffic; it stops *adaptation*. This is the core property that lets the AI engines fail safe.
