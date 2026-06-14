# Demo scenarios

Operator-runnable, dev-time **demo** scripts. One per shipped feature. Each one
snapshots baseline state, triggers the feature (publishes an envelope / POSTs a
policy or action / injects an event), watches for the expected response on the
right Redis channel or HTTP endpoint, narrates progress to the console in plain
English, and exits `0` on success or non-zero on timeout / mismatch.

These are **distinct** from the lint-triad scenarios under
`examples/scenarios/`. Those are part of the per-feature TRIAD that
`scripts/lint-structure.py` enforces. The scripts here are NOT tests and NOT
part of that contract; they exist so a developer or a demo presenter can prove
one feature works without standing up the whole pytest suite, and so the thesis
reproducibility appendix has exact one-line commands.

| What you get | `examples/scenarios/` | `scripts/scenarios/` (here) |
|---|---|---|
| Purpose | lint-triad scenario per `tests/e2e/<feature>/` | dev-time / demo narration |
| Output | passes/fails via SDK assertions | human-readable progress to stdout |
| Enforced by | `scripts/lint-structure.py` | not enforced |
| Run when | CI / triad verification | development, demos, thesis appendix |

## Scripts

| Script | Feature | What it proves | Example |
|---|---|---|---|
| `forecast_burst.py` | forecast scale-out | Publishes a high-RPS `ForecastResult`; watches `smartload.scale` for `scale_out`. | `python scripts/scenarios/forecast_burst.py --predicted-rps 9999` |
| `anomaly_inject.py` | anomaly reroute | Injects an `AnomalyEvent` (unhealthy) for a backend; watches `smartload.anomaly`; best-effort lb-sidecar exclusion; recovers. | `python scripts/scenarios/anomaly_inject.py --backend smartload-test-backend-3:8080` |
| `safe_mode_toggle.py` | safe mode | Flips `safe_mode`; watches `smartload.policy`; confirms the autoscaler picks up the new `policy_version`; restores. | `python scripts/scenarios/safe_mode_toggle.py` |
| `policy_walk.py` | policy change + audit | Applies a sequence of valid policy changes; watches each on `smartload.policy`; restores; prints the audit trail. | `python scripts/scenarios/policy_walk.py` |
| `scale_to_n.py` | manual scale | POSTs `/api/v1/scale`; watches `smartload.scale`; restores the starting count. | `python scripts/scenarios/scale_to_n.py --target 4` |
| `consolidated_status.py` | consolidated status | Reads `GET /api/v1/status`; prints the rolled-up health pill, per-service detail, active policy, and recent audit rows. | `python scripts/scenarios/consolidated_status.py` |

Every script supports `--help` for its full flag list.

## Running them against the live stack

1. Start the stack from the repo root (the Compose project name must be
   `smartload` — backend hostnames are hardcoded):

   ```bash
   docker compose up -d
   ```

2. Run any scenario. They are plain Python files; run them by path so the
   bundled path-bootstrap (`_common.py`) finds `services.shared` and the SDK:

   ```bash
   python scripts/scenarios/forecast_burst.py
   python scripts/scenarios/safe_mode_toggle.py
   python scripts/scenarios/anomaly_inject.py --backend smartload-test-backend-3:8080
   ```

3. Read the console. A run ends in `PASS - <summary>` (exit 0) or a
   `FAIL: <reason>` line on stderr (exit 1), so the scripts compose into shell
   pipelines and `&&` chains.

## Connection configuration

Each script reads connection info from environment variables with the same
defaults as `tests/integration/conftest.py` and the SmartLoad SDK constructor.
Override per-run with flags (`--redis-url`, `--policy-url`, ...) or with env
vars:

| Variable | Default | Used by |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379` | all (channel watch) |
| `POLICY_URL` | `http://localhost:8086` | `safe_mode_toggle`, `policy_walk`, `scale_to_n` |
| `SMARTLOAD_AUTOSCALER_URL` | `http://localhost:8085` | `forecast_burst`, `safe_mode_toggle`, `scale_to_n` |
| `SMARTLOAD_ANOMALY_DETECTOR_URL` | `http://localhost:8082` | `anomaly_inject` |
| `SMARTLOAD_FORECASTING_URL` | `http://localhost:8083` | (reserved) |
| `SMARTLOAD_OPERATOR_UI_URL` | `http://localhost:8090` | `consolidated_status` |
| `LB_SIDECAR_URL` | `http://localhost:8087` | `anomaly_inject` (exclusion check) |

## Requirements

- `redis` (redis-py), `httpx`, and the SmartLoad Python SDK on `clients/python`
  (the path-bootstrap in `_common.py` adds it to `sys.path` automatically).
- A running stack; without one the scripts exit non-zero with a connection
  error rather than hanging.

## Shared helpers

`_common.py` carries the repeated plumbing: repo-path bootstrap, the env-var
connection defaults above, the console-narration helpers, and a Redis pub/sub
poll loop built on `services.shared.contracts.parse_envelope` so every script
decodes the canonical Envelope (with channel-TTL drop) exactly like every other
subscriber in the system. It is a helper module, not a runnable scenario.
