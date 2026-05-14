# Middleware integration example

A complete, runnable external middleware that integrates against a running SmartLoad stack.

## Status

Scaffolded. Real implementation lands with issue #137 (depends on #127 + #130 + #132).

## What this will demonstrate

1. Authenticate with an API key against SmartLoad
2. Read current policy
3. Subscribe to anomaly events (Redis + webhook code paths shown side by side)
4. React: when an anomaly fires for backend X, call `POST /api/v1/isolate` to drain it
5. Read metrics and write a one-line dashboard to stdout every 10 seconds

## Why this exists

The SDK without a working integration example is documentation nobody reads. This example is the actual adoption funnel.

## Planned files

- `main.py` — the middleware itself
- `docker-compose.yml` — brings SmartLoad and this middleware up together
- `requirements.txt`
- `.env.example`
