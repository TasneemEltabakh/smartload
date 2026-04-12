# Anomaly Detection Microservice v1

This service implements the first standalone anomaly detection flow for SmartLoad.
It reads backend telemetry, evaluates node health, and publishes anomaly status updates to Redis.

## What It Does

- reads telemetry records from a JSON-backed temporary store
- evaluates each backend node independently
- classifies nodes as `healthy`, `degraded`, or `unhealthy`
- publishes node status messages to Redis on `smartload.anomaly.status`
- logs anomaly alerts with timestamps and trigger metrics

## Detection Logic

The detector uses a hybrid statistical approach:

- missing or stale telemetry marks a node as `unhealthy`
- high error rate triggers `degraded` or `unhealthy`
- latency anomalies are detected using:
  - EWMA-smoothed baseline
  - z-score deviation
  - minimum latency floors to avoid over-alerting on small fluctuations

This keeps v1 explainable and easy to tune while still being more reliable than fixed thresholds alone.

## Endpoints

- `GET /health`
  - returns service health
- `GET /status`
  - returns the most recent anomaly analysis report
- `POST /analyze`
  - reads telemetry input, runs anomaly detection, stores the report, and publishes results

## Configuration

The service is configured through environment variables.

Important settings:

- `PORT`
- `TELEMETRY_FILE`
- `REDIS_HOST`
- `REDIS_PORT`
- `REDIS_CHANNEL`
- `EWMA_ALPHA`
- `WARNING_Z_SCORE`
- `CRITICAL_Z_SCORE`
- `WARNING_ERROR_RATE`
- `CRITICAL_ERROR_RATE`
- `STALE_AFTER_SECONDS`

See [config.py](E:/GP/repo/smartload/services/anomaly-detector/config.py) for the full set of supported options and defaults.

## Local Run

In PowerShell:

```powershell
$env:PORT="8081"
python services\anomaly-detector\app.py
```

In Command Prompt:

```cmd
set PORT=8081
python services\anomaly-detector\app.py
```

## Run Tests

From the repository root:

```powershell
python -m unittest tests.unit.test_anomaly_detector
```

## Try the API

Run anomaly analysis:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8081/analyze | ConvertTo-Json -Depth 10
```

Read latest status:

```powershell
Invoke-RestMethod -Method Get -Uri http://localhost:8081/status | ConvertTo-Json -Depth 10
```

Health check:

```powershell
Invoke-RestMethod -Method Get -Uri http://localhost:8081/health | ConvertTo-Json -Depth 10
```

## Telemetry Input

The temporary telemetry source is:

- [telemetry_stream.json](E:/GP/repo/smartload/services/anomaly-detector/data/telemetry_stream.json)

This file stands in for the future Metrics DB.
The repository access layer is intentionally isolated so it can later be replaced with a real database-backed repository without changing detector logic.

## Current Scope

This service is the standalone anomaly detection part of issue `#17`.
It does not yet perform:

- routing updates in NGINX
- final Metrics DB integration

Those belong to later integration work.
