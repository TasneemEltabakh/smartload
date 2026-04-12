# RL Engine v1

This service implements the first standalone RL-engine-compatible routing score producer for SmartLoad.
It reads raw backend telemetry, computes per-backend routing scores, and publishes ranked routing decisions to Redis.

## What It Does

- reads raw backend telemetry from a JSON-backed temporary store
- computes routing scores for each backend independently
- ranks backends from best to worst routing target
- publishes the decision payload to Redis on `smartload.routing.scores`
- exposes local endpoints for health, scoring, and latest status

## Current Policy

The v1 policy is a heuristic baseline, not a trained RL policy.
It uses raw telemetry only:

- latency
- error rate
- request count
- CPU usage
- memory usage

This gives the service the correct interface now, while keeping room for a true RL policy later.

## Endpoints

- `GET /health`
- `GET /status`
- `POST /score`

## Local Run

In PowerShell:

```powershell
$env:PORT="8083"
python services\rl-engine\app.py
```

## Run Tests

```powershell
python -m unittest tests.unit.test_rl_engine
```
