# Scenarios

One Python script per feature. Each script:

1. Sets up baseline state (snapshot policy, baseline scaling, etc.)
2. Triggers the feature (publish an envelope, POST to an endpoint, etc.)
3. Watches for the expected response (subscribe to the right channel, poll the right endpoint)
4. Reports the outcome to stdout in plain English
5. Restores baseline state if applicable
6. Exits 0 on observed expected behavior, 1 on timeout or mismatch

## Shape of each script

- Standalone Python file, runnable as `python examples/scenarios/<name>.py [--args]`
- Reads connection info from env vars (`REDIS_URL`, `TIMESCALEDB_URL`, service URLs) with the same defaults as `tests/integration/conftest.py`
- Imports envelopes from `services.shared.contracts` — never redefines them inline
- Prints a one-line summary at the top describing what the script proves

## CI

Every script here is invoked against a freshly started compose stack in CI. A broken script fails the build (see `scripts/lint-structure.py`).

## Current scenarios

- `policy_walk.py` — read current policy, propose a sequence of valid changes, show audit rows after. Proves the policy-management slice end-to-end.

## Planned scenarios (one per feature, file as work progresses)

- `safe_mode_toggle.py` — toggle safe_mode and observe propagation
- `forecast_burst.py` — publish a high-RPS forecast and observe autoscaler reaction
- `anomaly_inject.py` — publish a synthetic anomaly and observe routing exclusion (once T2.1 lands)
- `scale_to_n.py` — call `POST /api/v1/scale` and watch backends settle (once #123 lands)
