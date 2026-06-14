# Anomaly Detection

> **Slice status — near-complete.** The wiring layer (Phase-1 run loop, threshold-engine baseline, Redis publish, lb-sidecar exclude/include, Live Engines UI surfacing, Grafana Anomaly dashboard) ships today. **The compose default is now `trend_rule`** — an interpretable, stateful trend-aware rule engine over per-backend temporal features (`features/trend.py`) that closes the gradual-degradation gap every stateless engine misses (bench F1 0.000 → 0.845, recall 0.791), keeps clean-control false positives at 0.000, needs no model artifact, and runs at `flip_confirmation_cycles=2` (`experiments/anomaly-detection-bench/REPORT.md` §8). `threshold` (baseline + automatic fallback), `trend_forest` (learned IsolationForest over the enriched temporal vector), and the point-feature `isolation_forest` (trained v1.0.7ab #101, re-calibrated #165 v1.0.7ah — 91.4% bench agreement) remain selectable. **v1.0.7bd** added the auto-recovery cool-down (flip-confirmation stability gate), per-cycle `backend_health` persistence, a live-stack acceptance test, e2e + scenario coverage, and a complementary Stage-B live-injection retrain/validation track. Remaining: a dedicated SDK `subscribe_anomaly` helper and the webhook fan-out (#130). This manifest is the canonical place to track what's done and what's pending.

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
- Default engine: `services/anomaly-detector/engines/trend_rule/engine.py` — interpretable trend-aware rule engine (error / spike / drift channels via CUSUM + a slope-based recovery suppressor) over per-backend temporal features from `services/anomaly-detector/features/trend.py`; no model artifact. Calibration: `tools/anomaly-training/calibrate_trend.py`. Comparison bench + write-up: `experiments/anomaly-detection-bench/` (`REPORT.md`). The Dockerfile ships `features/` so the trend engines import in the container.
- Learned counterpart: `services/anomaly-detector/engines/trend_forest/engine.py` — quantile-calibrated IsolationForest over the same 10-D enriched vector (`tools/anomaly-training/train_trend.py`); selectable, more trigger-happy than the rule engine (see REPORT §5).
- Baseline / fallback engine: `services/anomaly-detector/engines/threshold/engine.py` — wired against `ANOMALY_QUERY`; classifies on latency > 200 ms (DEGRADED) and error_rate > 5% (UNHEALTHY); the never-fails engine the run loop reverts to
- Trained engine: `services/anomaly-detector/engines/isolation_forest/engine.py` + `services/anomaly-detector/models/isolation_forest.pkl` (N2.1, shipped v1.0.7ab via #101 — Nada); training pipeline at `tools/anomaly-training/train_smd.py`. Comparison bench: `experiments/anomaly-engine-bench/`. Live-stack test: `tests/integration/test_isolation_forest_live_stack.py` (`@pytest.mark.slow`, requires `ANOMALY_ENGINE=isolation_forest`). Artifact smoke tests: `tests/integration/test_isolation_forest_artifact.py`.
- Envelope: `services/shared/contracts.py::AnomalyEvent`
- SQL: `services/shared/queries.py::ANOMALY_QUERY` (parameterised on `window`, `service`, `metric_names`)
- Storage: `backend_health` hypertable (TimescaleDB) — written every poll cycle, for every backend, by `app.py::_inference_cycle` (plus the manual `/api/v1/isolate` path), v1.0.7bd; read by lb-sidecar startup hydration (v1.0.7b G2)
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
- [x] Persistence to `backend_health` — anomaly-detector writes every verdict, every poll cycle, for every backend (v1.0.7bd, `app.py::_inference_cycle`), so lb-sidecar startup hydration always has fresh data
- [x] Auto-recovery cool-down — `flip_confirmation_cycles` requires N consecutive confirming cycles before a status change is published/persisted, and a low-sample cycle holds the last non-healthy verdict (`runloop.apply_stability_gate`, v1.0.7bd)
- [ ] SDK method — dedicated `client.subscribe_anomaly(callback)` (the generic `client.engines.subscribe()` already delivers `smartload.anomaly` events today)
- [ ] Webhook fan-out (#130)
- [x] Scenario script `examples/scenarios/anomaly-detection/anomaly_walk.py`
- [x] E2E test suite `tests/e2e/anomaly-detection/` (+ live-stack `tests/integration/test_anomaly_isolation_forest.py`)
- [x] §25.9 slice-catalog row flipped to *Shipped*

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
