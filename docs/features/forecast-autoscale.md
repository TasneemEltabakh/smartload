# Forecast + Autoscale

> **Slice status — partial.** Both services compose end-to-end: forecasting publishes `ForecastResult` envelopes; the autoscaler subscribes, makes scale_in / scale_out decisions, actuates the test-backend pool via the Docker SDK. Forecast + Scaling Grafana dashboards ship (v1.0.7e + v1.0.7f). **Trained ARIMA artifact landed v1.0.7i** (25.0% test MAPE; ships behind `FORECAST_ENGINE=arima` until tuned below the <20% SOT KPI). **Forecasts hypertable landed v1.0.7w** — the Forecast dashboard's predicted line is now dense across the bucket interval (#159, closes §35.8). **Closed-loop autoscaler ↔ lb-sidecar coordination landed v1.0.7z** — the lb-sidecar now subscribes to `smartload.scale` and rewrites `upstream.conf` to match the live Docker pool on every scaling event (#164). **E2E suite + runnable scenario landed v1.0.7bh** — `tests/e2e/forecast-autoscale/` (migrated from `tests/integration/test_autoscaler.py`, history preserved) and `examples/scenarios/forecast-autoscale/forecast_walk.py` complete the feature triad (#140). Remaining work: tighten ARIMA MAPE, webhook fan-out (#130).

## What this slice delivers

Backends scale ahead of demand instead of in response to it. The forecasting service watches the recent request-rate trend and projects forward by `horizon_minutes` (5 by default). The autoscaler turns that prediction into a backend count decision against the policy's capacity bands — scale_out when predicted RPS exceeds the headroom, scale_in when predicted RPS is below the shed threshold. Every decision goes to `scaling_events` so the operator audit page (slice #2) and the Scaling Grafana dashboard (v1.0.7e) both show what was decided and why.

## Customer surfaces

| Surface | Detail | Status |
|---|---|---|
| Redis | `smartload.forecast` (forecasting → autoscaler) — payload `ForecastResult {horizon_minutes, predicted_rps, confidence_lower, confidence_upper, ...}` | ✓ |
| Redis | `smartload.scale` (autoscaler → operator-ui, webhook-dispatcher when #130 lands) — payload `ScalingEvent {action, instance_count, reason, ...}` | ✓ |
| HTTP | `POST /api/v1/scale` (autoscaler) — operator-driven manual override (shipped slice #3) | ✓ |
| HTTP | `GET /api/v1/audit/scaling` (autoscaler) — recent scaling decisions (shipped slice #2) | ✓ |
| UI | Live Engines forecast tile + Audit page scaling events + Actions page "Scale to N" form | ✓ |
| Grafana | Forecast dashboard `/d/smartload-forecast/` (v1.0.7f) + Scaling dashboard `/d/smartload-scaling/` (v1.0.7e) | ✓ |
| SDK | `client.subscribe_forecast(callback)` + `client.subscribe_scale(callback)` (BFF SSE filters) + `client.scale(target_count, actor)` operator override | ✓ |
| Webhook | HMAC-signed outbound POST on every scale event (#130) | pending |
| E2E test | `tests/e2e/forecast-autoscale/test_forecast_autoscale.py` — forecast→scale slice + cooldown + operator override, via the SDK | ✓ |
| Scenario | `examples/scenarios/forecast-autoscale/forecast_walk.py` — runnable narration of the slice | ✓ |

## Implementation pointers

- Forecasting service: `services/forecasting/{app,runloop,engine_base}.py` + plugin folders under `engines/`
- Baseline engine: `services/forecasting/engines/moving_average/engine.py` — wired against `FORECAST_QUERY` (1-minute buckets, last 60 minutes by default)
- ARIMA engine: `services/forecasting/engines/arima/engine.py` + `services/forecasting/models/arima_model.pkl` (ARIMA(3,0,1), 36.9 MB, 25.0% test MAPE — landed v1.0.7i, closes #102, supersedes stale PR #144). Training pipeline at `tools/forecasting-training/`.
- Autoscaler: `services/autoscaler/{app,decisions,cluster_client}.py` — Forecast subscriber + Docker SDK + cooldown + reactive fallback when forecast stream goes stale. `cluster_client.py` exposes two lifecycle pairs: `start()`/`stop()` toggle compose-provisioned containers (the default, used by the #148 routing bench), and `provision()`/`decommission()` create/destroy dynamic containers via Docker SDK (gated by `AUTOSCALER_PROVISIONING_ENABLED=true`, used by the #155 adaptive bench). `scale_out()` and `scale_in()` return `(name, mechanism)` so the published `ScalingEvent.mechanism` field records which path actuated.
- Envelopes: `services/shared/contracts.py::ForecastResult`, `::ScalingEvent`
- SQL: `services/shared/queries.py::FORECAST_QUERY` (forecaster reads) + `::FORECASTS_INSERT` (forecaster writes per cycle, v1.0.7w) + `::SCALING_AUDIT_QUERY` + `::OBSERVED_RPS_QUERY` (autoscaler reactive fallback)
- Storage: `scaling_events` hypertable + `forecasts` hypertable (TimescaleDB; both 30-day retention)
- UI: `services/operator-ui/web/src/pages/LiveEngines.tsx` (forecast tile) + `services/operator-ui/web/src/pages/Audit.tsx` (scaling decisions)
- Grafana: `infrastructure/grafana/dashboards/{smartload-forecast,smartload-scaling}.json`
- E2E + scenario: `tests/e2e/forecast-autoscale/test_forecast_autoscale.py` + `examples/scenarios/forecast-autoscale/forecast_walk.py` — both inject a `ForecastResult` on `smartload.forecast` (no operator publish surface exists) and observe the autoscaler's `ScalingEvent` via the SDK; pure-Python decision-matrix unit coverage stays in `tests/integration/test_autoscaler_decisions.py`

## Status

- [x] Forecasting service wired (Phase-1 run loop, #138 round 2)
- [x] Forecasting run loop enabled by default in `docker-compose.yml` (v1.0.7g)
- [x] Moving-average baseline engine ships
- [x] Autoscaler T1.3 + T1.4 wired (forecast subscriber, Docker SDK actuation, cooldown, policy live reload)
- [x] Envelope contracts (`ForecastResult`, `ScalingEvent`) defined in `shared/contracts.py`
- [x] Redis channels `smartload.forecast` + `smartload.scale` registered in `docs/redis-channels.md`
- [x] Audit endpoint `GET /api/v1/audit/scaling` (slice #2)
- [x] Manual operator override `POST /api/v1/scale` (slice #3, #123)
- [x] UI Live Engines forecast tile + Audit page scaling events + Actions "Scale to N" form
- [x] Grafana Scaling dashboard (v1.0.7e)
- [x] Grafana Forecast dashboard (v1.0.7f)
- [x] SDK `client.scale(target_count, actor)` operator method
- [x] ARIMA model artifact (`arima_model.pkl`) — N2.2 shipped v1.0.7i (extracted from PR #144 kernel; 25.0% test MAPE — ships behind `FORECAST_ENGINE=arima` until tuned below the <20% SOT KPI per §17.4)
- [x] Continuous `forecasts` hypertable — forecasting service persists every publish; the Forecast Grafana dashboard's predicted line is dense across the bucket interval (v1.0.7w, #159, closes SOT §35.8). 6 new unit tests + 4 integration tests.
- [x] SDK method — `client.subscribe_forecast(callback)` (single-channel filter over the BFF SSE stream)
- [ ] Webhook fan-out for scaling events (#130)
- [x] Scenario script `examples/scenarios/forecast-autoscale/forecast_walk.py` (v1.0.7bh, #140) — injects a high forecast, watches `smartload.scale` for the matching scale_out, confirms the scaling audit, then exercises the operator override
- [x] E2E test suite `tests/e2e/forecast-autoscale/` (v1.0.7bh, #140) — 4 tests: forecast-driven scale_out (+ scaling-audit visibility), cooldown suppression, operator-override noop, operator-override audit round-trip. Migrated from `tests/integration/test_autoscaler.py` (history preserved via `git mv`); observation now goes through the SDK
- [ ] §25.9 slice-catalog row flipped to *Shipped*

## Non-goals

- Auto-scale on anomaly signal — that channel feeds the LB sidecar for routing, not the autoscaler. Capacity decisions stay forecast-driven.
- Multi-cluster scaling — single docker-compose target only; Kubernetes scaling via HPA is the Phase-2 deployment shape per §20.
- Per-tenant capacity bands — Phase 2 SaaS (#129).

## How to verify (what ships today)

```bash
# Stack up with the default v1.0.7g flags
docker compose up -d

# Drive traffic so the moving-average baseline sees something to project
open http://localhost:8089   # Locust traffic-simulator UI

# Watch the live forecast tile + activity feed
open http://localhost:8090/engines

# Grafana — predicted vs actual + scaling events overlay
open http://localhost:3000/d/smartload-forecast/
open http://localhost:3000/d/smartload-scaling/

# Direct Redis tap (forecast channel)
docker exec smartload-redis-1 redis-cli SUBSCRIBE smartload.forecast

# Manually scale (operator override)
curl -X POST http://localhost:8085/api/v1/scale \
     -H 'Content-Type: application/json' \
     -d '{"target_count":3,"actor":"manual-test"}'
```
