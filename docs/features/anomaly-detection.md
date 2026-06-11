# Anomaly Detection

> **Slice status — partial.** The wiring layer (Phase-1 run loop, threshold-engine baseline, Redis publish, lb-sidecar exclude/include, Live Engines UI surfacing, Grafana Anomaly dashboard) ships today. The **trained Isolation Forest plugin landed v1.0.7ab (#101)** — F1=0.8012 on SMD holdout (PASS of the >0.80 KPI gate) — but is **currently under-reacting at production scales** (25% bench agreement with threshold; production_scaler domain-adaptation gap, tracked as **#165**). Compose default remains `ANOMALY_ENGINE=threshold` until #165's re-calibration lands. Remaining work for full slice closure: #165, plus the SDK / scenario / webhook layers below. This manifest is the canonical place to track what's done and what's pending.

## What this slice delivers

Customers running SmartLoad in front of a backend pool stop having to write their own per-instance health-check plumbing. The decision plane watches NGINX's per-request latency and error-rate signal continuously, classifies each backend as healthy / degraded / unhealthy, and the load balancer automatically takes unhealthy backends out of the upstream pool — with a published audit trail. Operators see the verdicts live; integrators (once the SDK + webhook layers land) can subscribe to the events.

## Customer surfaces

| Surface | Detail | Status |
|---|---|---|
| HTTP | `POST /api/v1/isolate` (anomaly-detector) for operator-driven manual exclusion + include — shipped under slice #3 (manual-actions) | ✓ |
| Redis | `smartload.anomaly` (anomaly-detector → lb-sidecar, operator-ui BFF) — payload `AnomalyEvent {backend_id, status, score, ...}` | ✓ |
| UI | Live Engines page (`/engines`) surfaces the latest verdict per backend + the activity feed (#121 session 1) | ✓ |
| Grafana | Anomaly dashboard `/d/smartload-anomaly/` — state timeline + thresholds + backend_health verdicts table (v1.0.7d, 2026-05-29) | ✓ |
| SDK | `client.subscribe_anomaly(callback)` Redis subscriber pattern (mirrors `subscribe_policy`) | pending |
| Webhook | HMAC-signed outbound POST when a backend flips to UNHEALTHY (#130) | pending |

## Implementation pointers

- Service: `services/anomaly-detector/{app,runloop,engine_base}.py` + plugin folders under `engines/`
- Baseline engine: `services/anomaly-detector/engines/threshold/engine.py` — wired against `ANOMALY_QUERY`; classifies on latency > 200 ms (DEGRADED) and error_rate > 5% (UNHEALTHY)
- Trained engine: `services/anomaly-detector/engines/isolation_forest/engine.py` + `services/anomaly-detector/models/isolation_forest.pkl` (N2.1, shipped v1.0.7ab via #101 — Nada); training pipeline at `tools/anomaly-training/train_smd.py`. Comparison bench: `experiments/anomaly-engine-bench/`. Live-stack test: `tests/integration/test_isolation_forest_live_stack.py` (`@pytest.mark.slow`, requires `ANOMALY_ENGINE=isolation_forest`). Artifact smoke tests: `tests/integration/test_isolation_forest_artifact.py`.
- Envelope: `services/shared/contracts.py::AnomalyEvent`
- SQL: `services/shared/queries.py::ANOMALY_QUERY` (parameterised on `window`, `service`, `metric_names`)
- Storage: `backend_health` hypertable (TimescaleDB) — written by the anomaly-detector when persistence lands; read by lb-sidecar startup hydration (v1.0.7b G2)
- LB consume: `services/lb-sidecar/runloop.py::handle_anomaly` — translates IP backend_id → container name via `BackendRegistry`, calls `adapter.exclude_backend()` / `include_backend()`
- UI: `services/operator-ui/web/src/pages/LiveEngines.tsx` (anomaly tile + activity feed)

## Status

- [x] Service wired (Phase-1 run loop via `engine_base` ABC, #138 round 1)
- [x] Run loop enabled by default in `docker-compose.yml` (v1.0.7g)
- [x] Threshold baseline engine ships
- [x] Envelope contract (`AnomalyEvent`) defined in `shared/contracts.py`
- [x] Redis channel `smartload.anomaly` registered in `docs/redis-channels.md`
- [x] LB sidecar consumes — exclude/include + startup hydration from `backend_health` (T2.1 + v1.0.7b)
- [x] UI surface — Live Engines anomaly tile + activity feed (#121 session 1)
- [x] Grafana Anomaly dashboard (v1.0.7d)
- [x] Manual operator override — `POST /api/v1/isolate` (slice #3, #123)
- [x] Isolation Forest model artifact (`isolation_forest.pkl`) — N2.1. Trained on SMD (Server Machine Dataset, machine-1-1 + machine-1-6), test F1=0.8012 > 0.80. See `engines/isolation_forest/README.md` and `tools/anomaly-training/training_log.json`.
- [ ] Persistence to `backend_health` — anomaly-detector should write every verdict so lb-sidecar startup hydration has data; currently the table only fills under specific test paths
- [ ] Auto-recovery cool-down — engine should not flicker between HEALTHY and DEGRADED on a single noisy sample
- [ ] SDK method — `client.subscribe_anomaly(callback)`
- [ ] Webhook fan-out (#130)
- [ ] Scenario script `examples/scenarios/anomaly-detection/anomaly_walk.py`
- [ ] E2E test suite `tests/e2e/anomaly-detection/`
- [ ] §25.9 slice-catalog row flipped to *Shipped*

## Non-goals

- Multi-tenant anomaly streams — Phase 2 SaaS (#129)
- Anomaly explanation / feature attribution surfaces — out of scope for v1
- Auto-scaling on anomaly alone — that decision belongs to the autoscaler driven by forecast signals, not the anomaly stream

## How to verify (what does ship today)

```bash
# Stack up with the default v1.0.7g flags
docker compose up -d

# Watch live anomaly events via the operator UI activity feed
open http://localhost:8090/engines

# Direct Redis tap (verify the channel is alive)
docker exec smartload-redis-1 redis-cli SUBSCRIBE smartload.anomaly

# Grafana state timeline + backend_health verdicts
open http://localhost:3000/d/smartload-anomaly/

# Manually isolate a backend (operator override)
curl -X POST http://localhost:8082/api/v1/isolate \
     -H 'Content-Type: application/json' \
     -d '{"backend_id":"smartload-test-backend-3:8080","action":"exclude","actor":"manual-test"}'
```
