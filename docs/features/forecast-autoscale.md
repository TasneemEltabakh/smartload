# Forecast + Autoscale

> **Slice status — partial.** Both services compose end-to-end: forecasting publishes `ForecastResult` envelopes; the autoscaler subscribes, makes scale_in / scale_out decisions, actuates the test-backend pool via the Docker SDK. Forecast + Scaling Grafana dashboards ship (v1.0.7e + v1.0.7f). **Trained ARIMA artifact landed v1.0.7i** (25.0% test MAPE; ships behind `FORECAST_ENGINE=arima` until tuned below the <20% SOT KPI). Remaining work: tighten ARIMA MAPE, `forecasts` hypertable for continuous predicted-RPS history (Forecast dashboard follow-up), SDK `subscribe_forecast`, e2e + scenario, webhook fan-out (#130).

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
| SDK | `client.subscribe_forecast(callback)` Redis subscriber + `client.scale(target_count, actor)` operator override | partial (scale shipped; subscribe pending) |
| Webhook | HMAC-signed outbound POST on every scale event (#130) | pending |

## Implementation pointers

- Forecasting service: `services/forecasting/{app,runloop,engine_base}.py` + plugin folders under `engines/`
- Baseline engine: `services/forecasting/engines/moving_average/engine.py` — wired against `FORECAST_QUERY` (1-minute buckets, last 60 minutes by default)
- ARIMA engine: `services/forecasting/engines/arima/engine.py` + `services/forecasting/models/arima_model.pkl` (ARIMA(3,0,1), 36.9 MB, 25.0% test MAPE — landed v1.0.7i, closes #102, supersedes stale PR #144). Training pipeline at `tools/forecasting-training/`.
- Autoscaler: `services/autoscaler/{app,decisions,cluster_client}.py` — Forecast subscriber + Docker SDK + cooldown + reactive fallback when forecast stream goes stale
- Envelopes: `services/shared/contracts.py::ForecastResult`, `::ScalingEvent`
- SQL: `services/shared/queries.py::FORECAST_QUERY` (forecaster) + `::SCALING_AUDIT_QUERY` + `::OBSERVED_RPS_QUERY` (autoscaler reactive fallback)
- Storage: `scaling_events` hypertable (TimescaleDB)
- UI: `services/operator-ui/web/src/pages/LiveEngines.tsx` (forecast tile) + `services/operator-ui/web/src/pages/Audit.tsx` (scaling decisions)
- Grafana: `infrastructure/grafana/dashboards/{smartload-forecast,smartload-scaling}.json`

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
- [ ] Continuous `forecasts` hypertable — forecasting service should persist every publish so the Forecast Grafana dashboard's predicted line is continuous instead of sparse-at-decision-moments. Documented as the v1.0.7f follow-up.
- [ ] SDK method — `client.subscribe_forecast(callback)`
- [ ] Webhook fan-out for scaling events (#130)
- [ ] Scenario script `examples/scenarios/forecast-autoscale/forecast_walk.py`
- [ ] E2E test suite `tests/e2e/forecast-autoscale/`
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
