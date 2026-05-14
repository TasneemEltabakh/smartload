# End-to-end tests

One folder per feature. Each folder exercises a vertical slice through every customer surface (HTTP, Redis, SDK, UI as available) against a live compose stack.

## Why this exists separately from tests/integration/

- `tests/unit/` — pure-function tests, no I/O
- `tests/integration/` — service-pair / wire-protocol tests (one service in isolation, real Redis + real TimescaleDB)
- `tests/e2e/` — feature-level tests that span multiple services through their customer-facing surfaces

A passing `tests/e2e/<feature>/` is the strongest claim that "this feature works for real users."

## Convention

For every folder `tests/e2e/<feature>/` there must also exist:

- `docs/features/<feature>.md` — the manifest
- `examples/scenarios/<feature>.py` — the runnable scenario

`scripts/lint-structure.py` enforces this. Half a slice is visible by an empty folder.

## Running

```bash
# Spin up the stack
docker compose up -d

# Run all e2e suites
pytest tests/e2e/

# Run one feature
pytest tests/e2e/policy-management/
```

## Current suites

- `policy-management/` — read, update, audit cycle of the operating policy
