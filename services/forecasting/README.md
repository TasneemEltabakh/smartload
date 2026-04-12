# Workload Forecasting Microservice v1

This service implements the first standalone forecasting flow for SmartLoad.
It reads recent request history, predicts the next 5-minute load interval, publishes the forecast to Redis, and logs forecast quality for evaluation.

## What It Does

- reads request-rate history from a JSON-backed temporary store
- forecasts total system request load for the next 5 minutes
- uses lightweight baseline model selection for v1:
  - single exponential smoothing
  - persistence baseline
- publishes forecast results to Redis on `smartload.forecast.load`
- logs forecast outputs and backtest accuracy

## Endpoints

- `GET /health`
- `GET /status`
- `POST /forecast`

## Local Run

In PowerShell:

```powershell
$env:PORT="8082"
python services\forecasting\app.py
```

## Run Tests

```powershell
python -m unittest tests.unit.test_forecasting_service
```
