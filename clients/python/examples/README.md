# SDK examples

Runnable scripts that demonstrate the SmartLoad Python client against a live stack.

## Files

- `quickstart.py` — 20-line "hello world." Connect, fetch policy, print. Start here.
- `middleware_integration/` — full external middleware that consumes anomaly events and reacts via `POST /api/v1/isolate`. Tracked by issue #137.

## Running

Start a local stack first:

```bash
docker compose up -d
```

Then:

```bash
python clients/python/examples/quickstart.py
```

## CI

CI runs every example here against a freshly started stack on every PR that touches the SDK or its examples. See `.github/workflows/`.
