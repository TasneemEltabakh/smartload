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
# Default 5-run batch per side (≈15 min × 5) — statistically defensible
bash experiments/baseline-vs-smartload/scripts/run_experiment.sh

# Pick the run count + seed base explicitly
RUNS=5 SEED_BASE=1337 bash experiments/baseline-vs-smartload/scripts/run_experiment.sh

# Single fast pass for harness validation (low statistical power)
RUNS=1 SHORT=1 bash experiments/baseline-vs-smartload/scripts/run_experiment.sh

# Just one side (re-run smartload only after a model change)
SIDES=smartload bash experiments/baseline-vs-smartload/scripts/run_experiment.sh

# Aggregate an existing batch to summary.parquet + SUMMARY.md + error-band plots
python experiments/baseline-vs-smartload/scripts/aggregate_runs.py \
    experiments/baseline-vs-smartload/results/<timestamp>

# (the runner calls aggregate_runs.py automatically at the end of a batch)
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

Each batch drops everything under `results/<UTC-timestamp>/`, with one folder
per run:

```
results/<timestamp>/
├── MANIFEST.json              # knobs (incl. RUNS, SEED_BASE) + git SHA
├── run-01/ … run-NN/
│   ├── baseline/
│   │   ├── locust_stats.csv          # final per-name stats
│   │   ├── locust_stats_history.csv  # interval time series
│   │   ├── locust_failures.csv
│   │   ├── locust_report.html
│   │   ├── pre_status.json / post_status.json
│   │   ├── pre_prom.json / post_prom.json
│   │   ├── scaling_audit.json
│   │   └── run.log
│   └── smartload/                    # same shape
└── (after aggregate_runs.py:)
    ├── summary.parquet        # tidy/long: side, phase, metric, mean, std, ci_lower, ci_upper, half_width, n
    ├── SUMMARY.md             # per-side per-phase mean ± CI + smartload−baseline delta
    ├── plot_rps.png           # mean line + 95% CI band across runs, both sides
    ├── plot_p50_p95_p99.png
    ├── plot_error_rate.png
    ├── plot_total_requests.png
    ├── plot_per_phase_p95.png # per-phase p95 bars with CI error bars
    └── plot_recovery_curve.png
```

## Methodology notes

- **Same backend pool, same compose, same hardware.** The two sides differ only in the four `*_RUNLOOP_ENABLED` flags + `RL_MODE`. The load-balancer container is identical; in baseline mode the upstream block is the static round-robin from `services/load-balancer/nginx/nginx.conf`, in SmartLoad mode the `lb-sidecar` rewrites `upstream.conf` in place based on Redis signals from the decision plane.
- **Load injected from outside the LB.** Locust runs in a one-shot container on `smartload-net`. It hits the LB at its internal hostname (`http://load-balancer:80`), so the LB itself does the dispatch. This matches the operator's view of latency — what an external caller measures.
- **Anomaly injection uses the existing `/api/v1/isolate` endpoint** (slice #3). The bad backend's NGINX upstream entry has its weight set to 0 in SmartLoad mode (via the `anomaly_response: auto-isolate` policy + the lb-sidecar). In baseline mode that path is silent and NGINX RR keeps including the bad backend in its rotation.
- **No autoscaler container shutdowns during the run.** The autoscaler can still scale within `min_backends..max_backends`, but the test-backend pool is fixed at 5 in compose. Scale-out in SmartLoad mode is bounded by what's already provisioned; this is a known limitation — measuring actuation latency rather than capacity expansion.
- **Each side runs serially, not concurrently.** Stack state is reset between sides via `docker compose up -d --force-recreate` on the relevant services. This sacrifices same-second comparability for clean isolation; the same workload is replayed identically on each side.
- **Multi-run batching with confidence intervals (#160, §35.3).** `RUNS=N` (default 5) repeats the whole 3-phase shape per side under independent seeds (`BENCH_SEED = SEED_BASE + run−1`, seeding the locustfile's `random`). `aggregate_runs.py` then computes per-side per-phase **mean ± 95% CI** (Student's t) into `summary.parquet` + `SUMMARY.md`, and the plots render CI bands/error bars. Treat a smartload−baseline delta smaller than the two sides' CI half-widths as not yet significant at that run count. The seed fixes only the load-generation jitter — cold-cache / JIT / warm-up variance is the residual spread the CI is there to capture.

## Knobs

Override at the command line:

| Var | Default | Meaning |
|---|---|---|
| `SIDES` | `baseline smartload` | Subset of sides to run |
| `RUNS` | 5 | Independently-seeded repeats per side; aggregated to per-metric mean ± CI (§35.3) |
| `SEED_BASE` | 1337 | Base RNG seed; run *k* uses `SEED_BASE + (k−1)` (load-gen jitter only) |
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
- [x] Multi-run batching with per-metric confidence intervals (#160, §35.3) — `RUNS=N` + `aggregate_runs.py` report `mean ± CI`, so reported deltas are statistically defensible rather than single-run point estimates

## Out of scope

- Cross-cluster federation benchmarks
- Multi-tenant load (Phase 2 SaaS, #129)
- Cost-per-request economic analysis
- PPO retraining on the workload — uses whatever `policy.zip` ships with the rl-engine

## Current status (when read fresh)

| Layer | Status |
|---|---|
| `env/{baseline,smartload}.env` | scaffolded |
| `locust/locustfile.py` 3-phase shape + `BENCH_SEED` seeding | shipped |
| `scripts/run_experiment.sh` orchestration + `RUNS`/`SEED_BASE` batching | shipped |
| `scripts/plot_results.py` six plots (Locust column fixes + CI bands) | shipped |
| `scripts/aggregate_runs.py` multi-run mean ± CI → `summary.parquet` + `SUMMARY.md` | shipped (#160) |
| First full run | **pending** (operator to invoke) |
| Plots committed to repo | pending |
| SOT §18 Build Status row | pending |
| §25 evidence-for-value-prop paragraph | pending |

> **Note (#160):** the plotter previously read Locust columns that don't exist
> (`current_rps`, `p50/p95/p99_response_time`), so the RPS / latency / error-rate
> plots came out empty on any real run. Those column lookups are fixed alongside
> the multi-run CI work, so the plots now populate correctly.
