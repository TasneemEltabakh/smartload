# arima engine

ARIMA(p,d,q) forecaster trained on preprocessed Alibaba industrial traces. Replaces the moving-average baseline when `FORECAST_ENGINE=arima` and `services/forecasting/models/arima_model.pkl` is present.

## Status

**Implementation shipped.** Engine + artifact + training pipeline landed via the v1.0.7i forecasting model handoff (closes #102, supersedes the stale PR #144).

| Layer | Where |
|---|---|
| Inference engine | `services/forecasting/engines/arima/engine.py` |
| Trained artifact | `services/forecasting/models/arima_model.pkl` (ARIMA(2,0,2), 36.9 MB) |
| Unit tests | `services/forecasting/engines/arima/test_engine.py` |
| Training pipeline | `tools/forecasting-training/` (excluded from the runtime image) |
| Training log | `tools/forecasting-training/training_log.json` |

## Model

- **Order**: ARIMA(2,0,2)
- **Trained on**: Alibaba industrial trace, 5-minute resampling, 70/15/15 train/val/test split
- **Best test MAPE**: **25.0%** (the moving-average baseline scores 34.3% — **+22.77% relative improvement**)
- **SOT KPI**: < 20% MAPE — **not yet met**. The engine ships but is **not** the default; an operator activates it via `FORECAST_ENGINE=arima` in `.env`. The moving-average baseline stays the canonical Phase-1 forecaster until a tuned model crosses the SLO.

## Activation

```bash
# .env
FORECAST_ENGINE=arima

# Then recreate the container
docker compose up -d --force-recreate forecasting

# Confirm
curl http://localhost:8083/health | jq
#   engine_ready: true
#   engine: arima
```

## Fallback graph

1. `FORECAST_ENGINE=arima` requested
2. `select_engine("arima")` constructs `ArimaEngine`
3. `__init__` tries to load `models/arima_model.pkl`. If missing or malformed, `model_loaded=False` and `forecast()` will return a mean-of-history Forecast on every call — the service stays alive, the dashboards show real numbers, the autoscaler keeps making decisions.
4. If construction itself raises (e.g. statsmodels not installed), the service's run-loop fallback to `moving_average` engages and the baseline takes over.

So at no point does an ARIMA failure stall the forecasting pipeline.

## Inference shape

`forecast(history: HistoryWindow) -> Forecast` — the canonical `engine_base.ForecastEngine` contract. The engine appends the most recent `_MAX_APPEND_SAMPLES` (default 60) rates to the loaded model state via `statsmodels` `append(refit=False)` then calls `get_forecast(steps=1)`. The 95% CI from `conf_int(alpha=0.05)` populates `Forecast.confidence_{lower,upper}`. All values are clamped to ≥ 0.

## Known follow-ups

- **MAPE tightening.** Trained model is at 25.0% on Alibaba; the SOT KPI is < 20%. Either tune ARIMA order or add exogenous features (note the next item).
- **ARIMAX path.** The trained bundle's `exog_cols` and `exog_stats` keys are populated but currently ignored — the `ForecastEngine.forecast(history: HistoryWindow)` ABC doesn't pass live exog. Filed as ABC extension follow-up; see `tools/forecasting-training/README.md` acceptance section.
- **Artifact size.** The 36.9 MB statsmodels result object is large. Git LFS would be cleaner than committing it directly; deferred.

## Author attribution

Model architecture, feature engineering, training pipeline, and 22.77% baseline improvement by **Nada Nabil** ([@nadasoudi](https://github.com/nadasoudi)) — original work in PR #144 (2026-05-16). Engine handoff to the #138 plugin layout, fallback wiring, and unit test scaffolding by **Tasneem Muhammed** in v1.0.7i (2026-05-29).
