# Adaptive-bench

Three-round benchmark programme that quantitatively answers RQ4 (forecast-driven scale-out vs reactive) on the dynamic test-backend pool the autoscaler manages via the Docker SDK.

| Round | Tracking | Deliverable |
|---|---|---|
| R1 — Dynamic-pool foundation | #155 (shipped v1.0.7v) | Autoscaler `provision()`/`decommission()`; lb-sidecar dynamic discovery; NGINX DNS pre-flight. |
| **R2 — Orchestrator + collectors + 5-phase Locust shape** | **#156 (this directory)** | **`run.py` + three async collectors + 5-phase Locust + phase-D anomaly injector → eight raw artefacts per run.** |
| R3 — Analysis pipeline + plots + doc sync | #157 | Joined `run.parquet` via `pandas.merge_asof`; four plots; `SUMMARY.md`; SOT §§18/22/25.10/33/34 sync; walkthrough §8.16 expansion; `docs/features/adaptive-bench.md` slice manifest. |
| **Multi-run batching + per-metric CIs** | **#160 (§35.3)** | **`--runs N`/`--seed-base S`; per-run folders + `scripts/aggregate_runs.py` → `summary.parquet` + per-phase `mean ± CI` `SUMMARY.md` + error-band plots.** |

R2 ships the raw artefacts; R3 the per-run analysis; #160 batches N independently-seeded runs and reports `mean ± confidence interval` so results survive a reviewer who discounts N=1.

> **Backend model.** The pool is now closed-loop M/G/c queues (`test-backends/app.js`), so phase-C/D latency reflects real queue-wait under load and the phase-D `/_admin/delay` anomaly collapses a backend's throughput rather than adding flat latency (`anomaly_injector.py` documents the dynamics). The Locust shape is *closed-loop* and cannot hold a fixed arrival rate; `fortio/` adds a minimal standalone **open-loop** Fortio probe to chart the backend saturation curve + tail latency directly — see `fortio/README.md`. It does not replace Locust and is not wired into `run.py`.

## What R2 produces per run

Eight artefacts under `results/<TIMESTAMP>/`:

| File | Source | Purpose |
|---|---|---|
| `MANIFEST.json` | orchestrator | Knobs + git SHA + bench version for repro |
| `pre_status.json` | `GET /api/v1/status` | Pre-flight snapshot of every service's health |
| `post_status.json` | `GET /api/v1/status` | Post-flight snapshot |
| `scaling_audit.json` | `GET /api/v1/audit/scaling?limit=200` | All scaling decisions during the run |
| `prom_timeseries.parquet` | `prom_collector` (1 Hz) | Continuous Prometheus snapshots over the run |
| `decision_envelopes.jsonl` | `sse_collector` (`/api/ui/engines/stream`) | Every decision-plane envelope, with `captured_at` wall-clock |
| `upstream_changes.jsonl` | `upstream_watcher` (2 s `docker cp`) | Every detected `upstream.conf` rewrite |
| `locust_stats.csv` + `locust_stats_history.csv` | Locust headless | Per-name and per-second request stats |

## The 5-phase shape

| Phase | Window | User curve | RQ it tests |
|---|---|---|---|
| `A_bootstrap` | 0 → 60 s | ramp 0 → 20 | RQ4 first forecast |
| `B_forecast_burst` | 60 → 90 s | spike to 200 | Autoscaler grows pool 1 → ~4 |
| `C_sustain` | 90 → 240 s | hold 200 | Larger pool sustains the load |
| `D_anomaly_scale_down` | 240 → 300 s | drop to 30 + anomaly | Two adaptive paths concurrent |
| `E_steady` | 300 → 360 s | hold 30 | Stabilisation (no oscillation) |

Phase markers fire Locust events and tag each request name with the phase so the post-run analysis (R3) can slice cleanly per phase.

## How to run

```powershell
# Install host-side bench deps once
python -m pip install -r experiments/adaptive-bench/requirements-bench.txt

# Bring the stack up
docker compose up -d

# Run the default 5-run batch (writes to results/<TIMESTAMP>/run-01 .. run-05/,
# then aggregates to results/<TIMESTAMP>/summary.parquet + SUMMARY.md + plots)
python experiments/adaptive-bench/run.py --output-root experiments/adaptive-bench/results

# Pick the run count + seed base explicitly
python experiments/adaptive-bench/run.py --output-root experiments/adaptive-bench/results --runs 5 --seed-base 1337

# Compressed batch (harness validation / CI; same shape, 1/6th the wall-clock per run)
python experiments/adaptive-bench/run.py --output-root experiments/adaptive-bench/results --runs 2 --short

# Re-aggregate an existing batch without re-running the load
python experiments/adaptive-bench/scripts/aggregate_runs.py experiments/adaptive-bench/results/<TIMESTAMP>
```

> **Running from a git worktree?** The stack hard-codes the Compose project name,
> so prefix the command with `COMPOSE_PROJECT_NAME=smartload` (otherwise the
> autoscaler recreate targets a fresh, empty project).

The orchestrator is asyncio-based. Three collectors (Prometheus, BFF SSE, upstream.conf watcher) run concurrently with the Locust subprocess and the phase-D anomaly injector. Locust completion is the lifecycle signal; the orchestrator does post-flight cleanup (restores the temporary policy + env-file + tears down leftover dynamic backends) regardless of how the run exits.

## Lifecycle

```
pre-flight
├── docker compose ps health check
├── scale test-backend pool down to min_backends=1
├── push temporary policy override: autoscaler_cooldown_seconds=10
└── set AUTOSCALER_PROVISIONING_ENABLED=true via env-file + force-recreate autoscaler

concurrent
├── prom_collector       (1 Hz Prometheus polls → parquet)
├── sse_collector        (BFF SSE → JSONL with captured_at)
├── upstream_watcher     (2 s docker cp + diff → JSONL on change)
├── anomaly_injector     (sleeps until phase-D start, then injects)
└── locust subprocess    (5-phase shape; --csv-full-history)

post-flight (runs even on locust failure)
├── snapshot /api/v1/status → post_status.json
├── snapshot /api/v1/audit/scaling?limit=200 → scaling_audit.json
├── flip env-file back (AUTOSCALER_PROVISIONING_ENABLED=false) + recreate autoscaler
├── restore policy override (cooldown_seconds back to file value)
└── tear down any leftover smartload.dynamic=true containers (defence-in-depth)
```

## Multi-run batching & confidence intervals (#160, SOT §35.3)

A single run is a point estimate; a reviewer rightly discounts N=1. `--runs N`
(default **5**) repeats the whole 5-phase shape N times under independent seeds
and reports per-metric **mean ± 95% confidence interval** (Student's t, df=N−1).

**Layout.** Each batch is one timestamped folder:

```
results/<TIMESTAMP>/
├── run-01/ … run-NN/        # the eight raw artefacts + per-run MANIFEST.json
├── summary.parquet          # tidy/long: phase, metric, mean, std, ci_lower, ci_upper, half_width, n
├── SUMMARY.md               # per-phase mean ± CI table
├── plot_pool_size.png       # mean line + 95% CI band across runs
├── plot_upstream_timeline.png   # per-second p50/p95 mean ± CI band
├── plot_phase_latency_ci.png    # per-phase p50/p95/p99 bars with CI error bars
├── plot_time_to_react.png       # run-01 representative (event-overlay plot)
└── plot_anomaly_recovery.png    # run-01 representative
```

**Metrics** aggregated per phase: `latency_p50_ms`, `latency_p95_ms`,
`latency_p99_ms`, `error_rate_pct` (delta-based `100·Δfail/Δreq`), `rps`, and
`replica_count` (peak active pool — the forecast-driven scale-out signal).

**Seeding.** Run *k* launches Locust with `BENCH_SEED = seed_base + (k−1)`; the
locustfile seeds `random` from it. This fixes the **load-generation jitter** so
each run's request cadence is reproducible. It deliberately does **not** control
cold-cache / JIT / container-warm-up variance — that residual run-to-run spread
is exactly what the CI quantifies. With `--runs 1` every cell reads `(n=1)` and
no interval is defined.

## R2 acceptance — closed when

- `python run.py --output-root results` exits 0 in under 15 minutes on a clean machine
- All eight artefacts land under `results/<TIMESTAMP>/`
- `tests/e2e/adaptive-bench/test_compressed_run.py` (60 s total, 20 users) passes in CI under 5 minutes

R2 ships raw artefacts only. The joined `run.parquet`, plots, and `SUMMARY.md` are R3 (#157).
