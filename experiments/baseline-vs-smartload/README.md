# Baseline NGINX RR vs SmartLoad benchmark (#148)

Quantitative comparison of the same compose stack running with and without SmartLoad's decision plane. Closes the "operator can see the audit log but not the win" gap noted in #148.

## Why this exists

Every slice shipped through v1.0.7q is qualitative — operators see the policy diff, see the audit log, see the live engines feed. None of those answer the question a reviewer (or a potential customer) asks first: **does it actually do better than plain round-robin?** This experiment is the chart that answers that. Same hardware, same workload, same backend pool — only the decision plane changes.

## What gets measured

| Metric | Where it comes from |
|---|---|
| Sustained RPS | Locust `current_rps` over time |
| p50 / p95 / p99 latency | Locust `*_response_time` over time |
| Failure rate (4xx + 5xx) | Locust `total_failure_count` deltas |
| Cumulative request count | Locust `total_request_count` |
| Per-phase p95 | Locust per-name stats (each phase tagged in the request name) |
| Recovery curve near anomaly | Failures around the injection window |

## Load profile

Three phases (all knobs in `experiments/baseline-vs-smartload/locust/locustfile.py`):

| Phase | Time (default) | What it exercises |
|---|---|---|
| **A — Steady ramp** | 0 → 60 s, ramp to 50 concurrent users | Forecast-driven scale-out *before* backends saturate. In SmartLoad mode the forecasting service should publish a `ForecastResult` ahead of saturation and the autoscaler should add backends pre-emptively. In baseline mode there's no such anticipation. |
| **B — Anomaly injection** | 120 s → 180 s, 50 users held; backend-1 marked unhealthy via `POST /api/v1/isolate` at t=120 s, recovered at t=180 s | Anomaly-detector + lb-sidecar pipeline should pull backend-1 out of rotation. In baseline mode NGINX keeps sending 1/5 of traffic to the bad backend for the full 60 s of the anomaly window. |
| **C — Sustained tail** | 180 → 360 s, 50 users held | Tail-latency under steady load. Exercises the SLO defense + RL routing path. |

Total wall clock per side: ~6 minutes. Both sides → ~12 minutes + a few minutes of recreate / teardown. Set `SHORT=1` for a 2-minute-per-side harness validation.

## How to run

```bash
# Full experiment (≈15 min total)
bash experiments/baseline-vs-smartload/scripts/run_experiment.sh

# Shorter harness validation (≈4 min total) — proves the pipeline, low statistical power
SHORT=1 bash experiments/baseline-vs-smartload/scripts/run_experiment.sh

# Just one side (re-run smartload only after a model change)
SIDES=smartload bash experiments/baseline-vs-smartload/scripts/run_experiment.sh

# Generate plots from a completed run
python experiments/baseline-vs-smartload/scripts/plot_results.py \
    experiments/baseline-vs-smartload/results/<timestamp>
```

The script:

1. Stops the standing `traffic-simulator` container so it doesn't pollute the load profile.
2. For each side (baseline → smartload):
   a. Applies the side's env-file (`env/baseline.env` or `env/smartload.env`) by `docker compose up -d --force-recreate` on the decision-plane services + `lb-sidecar`. The load-balancer NGINX container is left alone — in baseline mode NGINX just uses its static round-robin upstream config; in SmartLoad mode the sidecar rewrites that upstream.conf based on Redis signals.
   b. Waits for `/api/v1/status overall` to come back as `ok` or `degraded` (not `down`) before starting load.
   c. Launches `locust` in a one-shot container on the `smartload-net` network, headless, with `--csv` + `--csv-full-history` so the post-run plotter gets time-series data.
   d. In the background, after `ANOMALY_AT_SECS`, posts a synthetic anomaly via `POST /api/v1/isolate` against backend-1; recovers it after `ANOMALY_HOLD_SECS`.
3. Captures pre/post snapshots from `/api/v1/status`, `/api/v1/audit/scaling`, and Prometheus's `up` query.
4. Restarts the standing `traffic-simulator` so the dev stack returns to normal.

## Outputs

Each run drops everything under `results/<UTC-timestamp>/`:

```
results/<timestamp>/
├── MANIFEST.json              # knobs + git SHA for reproducibility
├── baseline/
│   ├── locust_stats.csv       # final per-name stats
│   ├── locust_stats_history.csv   # 2-second-interval time series
│   ├── locust_failures.csv
│   ├── locust_report.html     # locust's built-in report
│   ├── pre_status.json        # /api/v1/status before load
│   ├── post_status.json       # after load
│   ├── pre_prom.json
│   ├── post_prom.json
│   ├── scaling_audit.json     # autoscaler's view of what it did
│   └── run.log
├── smartload/                 # same shape
└── (after plot script:)
    ├── plot_rps.png
    ├── plot_p50_p95_p99.png
    ├── plot_error_rate.png
    ├── plot_total_requests.png
    ├── plot_per_phase_p95.png
    ├── plot_recovery_curve.png
    └── SUMMARY.md
```

## Methodology notes

- **Same backend pool, same compose, same hardware.** The two sides differ only in the four `*_RUNLOOP_ENABLED` flags + `RL_MODE`. The load-balancer container is identical; in baseline mode the upstream block is the static round-robin from `services/load-balancer/nginx/nginx.conf`, in SmartLoad mode the `lb-sidecar` rewrites `upstream.conf` in place based on Redis signals from the decision plane.
- **Load injected from outside the LB.** Locust runs in a one-shot container on `smartload-net`. It hits the LB at its internal hostname (`http://load-balancer:80`), so the LB itself does the dispatch. This matches the operator's view of latency — what an external caller measures.
- **Anomaly injection uses the existing `/api/v1/isolate` endpoint** (slice #3). The bad backend's NGINX upstream entry has its weight set to 0 in SmartLoad mode (via the `anomaly_response: auto-isolate` policy + the lb-sidecar). In baseline mode that path is silent and NGINX RR keeps including the bad backend in its rotation.
- **No autoscaler container shutdowns during the run.** The autoscaler can still scale within `min_backends..max_backends`, but the test-backend pool is fixed at 5 in compose. Scale-out in SmartLoad mode is bounded by what's already provisioned; this is a known limitation — measuring actuation latency rather than capacity expansion.
- **Each side runs serially, not concurrently.** Stack state is reset between sides via `docker compose up -d --force-recreate` on the relevant services. This sacrifices same-second comparability for clean isolation; the same workload is replayed identically on each side.

## Knobs

Override at the command line:

| Var | Default | Meaning |
|---|---|---|
| `SIDES` | `baseline smartload` | Subset of sides to run |
| `RAMP_USERS` | 50 | Concurrent users at the top of phase A |
| `RAMP_SECS` | 60 | Phase A duration |
| `ANOMALY_AT_SECS` | 120 | Wall-clock seconds when phase B begins |
| `ANOMALY_HOLD_SECS` | 60 | Phase B duration |
| `SUSTAIN_END_SECS` | 360 | Total runtime per side |
| `SHORT` | unset | If `1`, overrides the four duration knobs to a 2-minute total run for harness validation |

## Acceptance gates (per #148)

- [ ] `bash run_experiment.sh` runs end-to-end on a clean machine in under 30 minutes
- [ ] README publishable — a reviewer with no project context can read it and understand what was measured, how, and what the result was
- [ ] Plots show a clear delta on at least three of the five metrics (p95, error rate during anomaly, time-to-recover)

## Out of scope

- Cross-cluster federation benchmarks
- Multi-tenant load (Phase 2 SaaS, #129)
- Cost-per-request economic analysis
- PPO retraining on the workload — uses whatever `policy.zip` ships with the rl-engine

## Current status (when read fresh)

| Layer | Status |
|---|---|
| `env/{baseline,smartload}.env` | scaffolded |
| `locust/locustfile.py` 3-phase shape | scaffolded |
| `scripts/run_experiment.sh` orchestration | scaffolded |
| `scripts/plot_results.py` six plots + SUMMARY.md | scaffolded |
| First full run | **pending** (operator to invoke) |
| Plots committed to repo | pending |
| SOT §18 Build Status row | pending |
| §25 evidence-for-value-prop paragraph | pending |
