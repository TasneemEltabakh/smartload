# smartload-client (Python)

The official Python client for the SmartLoad HTTP API and Redis event stream.

## Why this lives in the main repo

The SDK is version-locked to the API and the envelopes. Splitting it into a sibling repo (Temporal's pattern) creates drift the moment one side ships a breaking change. We keep both here until traffic forces a split.

## Status

Scaffolded — module shapes exist, methods are stubs. Implementation lands with issue #127.

## Install (after first release)

```bash
pip install smartload-client
```

For development, install from the repo:

```bash
pip install -e clients/python
```

## Quickstart

See `examples/quickstart.py`. The 20-line version:

```python
from smartload_client import SmartLoadClient

client = SmartLoadClient(base_url="http://localhost:8086")
policy = client.get_policy()
print(policy.operating_mode, policy.safe_mode)
```

## Middleware integration

A full runnable example of an external middleware consuming SmartLoad events and reacting to anomalies lives at `examples/middleware_integration/`. Tracked by issue #137.

## Layout

- `smartload_client/` — the importable package
  - `client.py` — top-level client; aggregates the sub-clients below
  - `policy.py` — policy endpoints
  - `metrics.py` — telemetry endpoints
  - `webhooks.py` — webhook management endpoints (planned #130)
  - `events.py` — Redis pub/sub helpers
  - `exceptions.py` — typed exceptions
- `examples/` — runnable demonstrations
- `tests/` — unit + smoke tests
