# Adaptive-bench

Three-round benchmark programme that quantitatively answers RQ4 (forecast-driven scale-out vs reactive) on the dynamic test-backend pool the autoscaler manages via the Docker SDK.

| Round | Tracking | Deliverable |
|---|---|---|
| R1 — Dynamic-pool foundation | #155 (shipped v1.0.7v) | Autoscaler `provision()`/`decommission()`; lb-sidecar dynamic discovery; NGINX DNS pre-flight. |
| **R2 — Orchestrator + collectors + 5-phase Locust shape** | **#156 (this directory)** | **`run.py` + three async collectors + 5-phase Locust + phase-D anomaly injector → eight raw artefacts per run.** |
| R3 — Analysis pipeline + plots + doc sync | #157 | Joined `run.parquet` via `pandas.merge_asof`; four plots; `SUMMARY.md`; SOT §§18/22/25.10/33/34 sync; walkthrough §8.16 expansion; `docs/features/adaptive-bench.md` slice manifest. |

This README covers R2 only. R3 ships the analysis on top of these raw artefacts.

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

# Run the full 6-minute bench (writes to results/<TIMESTAMP>/)
python experiments/adaptive-bench/run.py --output-root experiments/adaptive-bench/results

# Compressed 60-second run (for harness validation; same shape, 1/6th the wall-clock)
python experiments/adaptive-bench/run.py --output-root experiments/adaptive-bench/results --short
```

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

## R2 acceptance — closed when

- `python run.py --output-root results` exits 0 in under 15 minutes on a clean machine
- All eight artefacts land under `results/<TIMESTAMP>/`
- `tests/e2e/adaptive-bench/test_compressed_run.py` (60 s total, 20 users) passes in CI under 5 minutes

R2 ships raw artefacts only. The joined `run.parquet`, plots, and `SUMMARY.md` are R3 (#157).
