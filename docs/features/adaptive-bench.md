# Adaptive-bench

> **Slice status — shipped (R1 + R2 + R3), with both gating bugs closed in v1.0.7y + v1.0.7z.** Three-round programme: R1 (#155, v1.0.7v) delivered the dynamic-pool autoscaler foundation; R2 (#156) delivered the asyncio orchestrator + three async collectors + 5-phase Locust shape + phase-D anomaly injector; R3 (#157, v1.0.7x) delivered `join_run.py` + `plot_results.py` + four PNGs + `SUMMARY.md`. The first end-to-end run on 2026-06-10 produced real data and surfaced two architectural gaps; **both now closed**: **#163** decision-plane silent thread death (catch-all + /health liveness, v1.0.7y) + **#164** lb-sidecar `smartload.scale` subscription (handle_scale handler, v1.0.7z). The next adaptive-bench re-run produces the affirmative "pool grew N→M during B" + "pool shrank N→M during D" gate strings without manual restarts. **v1.0.7am (#160)** added multi-run batching: `--runs N` (default 5) + `--seed-base S` land per-run folders under one batch dir, and `scripts/aggregate_runs.py` rolls them up into per-phase per-metric `mean ± 95% CI` (`summary.parquet` + `SUMMARY.md`) with CI-band plots — closing SOT §35.3.

## What this slice delivers

A single repeatable command that drives the SmartLoad stack through a 5-phase load shape designed to exercise both forecast-driven scale-out (RQ4) and anomaly-driven reroute, captures eight raw artefacts per run, joins them into a per-second timeline, and emits four plots + a `SUMMARY.md` honestly reporting what fired and what didn't. The output is the canonical evidence surface for thesis-quality RQ4 claims and for ongoing regression detection on the decision plane.

## Customer surfaces

| Surface | Detail | Status |
|---|---|---|
| CLI | `python experiments/adaptive-bench/run.py --output-root <dir>` — default 5-run batch (auto-joins + aggregates) | ✓ |
| CLI | `python experiments/adaptive-bench/run.py --output-root <dir> --runs 2 --short` — compressed multi-run (CI) | ✓ |
| CLI | `python experiments/adaptive-bench/scripts/join_run.py <run-dir>` — emits `run.parquet` + per-channel parquets | ✓ |
| CLI | `python experiments/adaptive-bench/scripts/aggregate_runs.py <batch-dir>` — `summary.parquet` + mean ± CI `SUMMARY.md` + plots (#160) | ✓ |
| CLI | `python experiments/adaptive-bench/scripts/plot_results.py <batch-or-run-dir>` — CI-band plots | ✓ |
| Test | `tests/e2e/adaptive-bench/test_compressed_run.py` — drives `--short`, asserts all 8 artefacts land with the right shape | ✓ |
| Artefacts | `MANIFEST.json`, `pre_status.json`, `post_status.json`, `scaling_audit.json`, `prom_timeseries.parquet`, `decision_envelopes.jsonl`, `upstream_changes.jsonl`, `locust_stats*.csv` | ✓ |
| Outputs (per run) | `run.parquet`, `forecasts.parquet`, `anomalies.parquet`, `scalings.parquet`, `routings.parquet`, `upstream_changes.parquet`, `scaling_audit.parquet` | ✓ |
| Outputs (batch, #160) | `summary.parquet` (per-phase per-metric mean ± CI) + `SUMMARY.md` + CI-band plots at the batch top level | ✓ |

## Implementation pointers

- Orchestrator: `experiments/adaptive-bench/run.py` — asyncio main with strict pre-flight → concurrent → post-flight lifecycle
- Locust shape: `experiments/adaptive-bench/locust/locustfile.py` — `FivePhaseShape` + per-request phase tags
- Async collectors: `experiments/adaptive-bench/collectors/{prom_collector,sse_collector,upstream_watcher}.py`
- Phase-D injector: `experiments/adaptive-bench/anomaly_injector.py` — picks a dynamic backend (preferring `smartload.dynamic=true` label), drives `/_admin/delay` + `POST /api/v1/isolate`
- R3 analysis: `experiments/adaptive-bench/scripts/{join_run,plot_results}.py`
- R3 inputs depended on: `pandas`, `pyarrow`, `matplotlib`, plus all of R2's deps
- E2E test: `tests/e2e/adaptive-bench/test_compressed_run.py`
- Closes-the-gap dependencies (for the RQ4 narrative): **#163** (silent thread death) and **#164** (lb-sidecar `smartload.scale` subscription)

## Status

- [x] R1 dynamic-pool foundation (#155 v1.0.7v) — `provision()` / `decommission()` lifecycle on the autoscaler; lb-sidecar dynamic backend discovery; NGINX DNS pre-flight
- [x] R2 orchestrator + collectors + 5-phase shape (#156) — see `experiments/adaptive-bench/README.md`
- [x] R3 analysis pipeline + 4 plots + `SUMMARY.md` (#157, this release)
- [x] First real bench run on 2026-06-10 — artefacts at `experiments/adaptive-bench/results/20260610T135509Z/`
- [x] E2E test under `tests/e2e/adaptive-bench/` (5-min CI budget)
- [x] SOT §22 / §18 / §25.10 / §33 / §34 sync (this release)
- [x] PROJECT_WALKTHROUGH §8.16 expansion (this release)
- [x] Multi-run batching + per-metric confidence intervals (#160, v1.0.7am) — `--runs`/`--seed-base` + shared `_bench_common/bench_stats.py` + `aggregate_runs.py` → `summary.parquet` + mean ± CI `SUMMARY.md` + CI-band plots; closes SOT §35.3 (capability)
- [x] **#163 + #164 both landed (v1.0.7y + v1.0.7z, 2026-06-10)** — the gates "pool grew during B" and "pool shrank during D" will produce affirmative strings on the next bench run; the harness itself needs no further changes
- [ ] **Next bench run on the unblocked stack** — drives the freshly-cleared chain end-to-end, captures the affirmative gate strings, updates §34.6 with the new numbers
- [ ] Multi-run batching with per-metric CIs (#160 — separate workstream)

## Non-goals (R1-R3 phase)

- K8s HPA-vs-SmartLoad comparison (deferred to SOT §35.5 once Helm chart lands per #133)
- Cross-host network behaviour (single Docker host only)
- Production traffic shadow-mode runs (synthetic Locust load only; SOT §35.7)
- Multi-tenancy in the bench (single-tenant; Phase 2 per SOT §35.1)

## How to verify

```powershell
# Install host-side bench deps once
python -m pip install -r experiments/adaptive-bench/requirements-bench.txt

# Bring the stack up
docker compose up -d

# Full 6-minute bench → artefacts under results/<TIMESTAMP>/
python experiments/adaptive-bench/run.py --output-root experiments/adaptive-bench/results

# Join into run.parquet + per-channel parquets
python experiments/adaptive-bench/scripts/join_run.py experiments/adaptive-bench/results/<TIMESTAMP>

# 4 plots + SUMMARY.md
python experiments/adaptive-bench/scripts/plot_results.py experiments/adaptive-bench/results/<TIMESTAMP>

# Read the SUMMARY
cat experiments/adaptive-bench/results/<TIMESTAMP>/SUMMARY.md

# CI compressed run (60 s total)
pytest tests/e2e/adaptive-bench/test_compressed_run.py -v -m e2e
```

## Honest finding from the first real run (2026-06-10)

The pipeline works end-to-end: 6 forecasts captured, 197 decision-plane envelopes, 2 autoscaler decisions (one `scale_in` 5.7 s after the first forecast; one `scale_out` 1.2 s after a 317 RPS forecast crossed capacity). The harness's reported "time-to-react" range was 1.2 s — 121.5 s depending on which forecast happened to cross a decision band.

But `upstream.conf` saw **zero rewrites** during the bench. The lb-sidecar received routing + anomaly + policy envelopes correctly (we have 71 routing + 107 anomaly events in `decision_envelopes.jsonl`), but it doesn't subscribe to `smartload.scale` — so when the autoscaler grew the Docker pool from 3 to 4 backends, NGINX's `upstream.conf` still listed the boot-time seed list of 5 backends and relied on its own passive `max_fails` to handle the missing one. That's **#164**.

A separate complication: the four decision-plane services (`forecasting`, `anomaly-detector`, `rl-engine`, `autoscaler`) silently stopped doing work some time before the first bench run. After restart they recovered immediately and produced the data above. That's **#163**.

Once #163 + #164 close, a re-run will produce SUMMARY.md acceptance-gate strings of "yes — pool grew N→M during B" and "yes — pool shrank N→M during D" rather than the current "no — pool stayed at 5 (see #164)". The harness needs no further work for that re-run to succeed.

## Related issues

- #155 R1 (shipped v1.0.7v) — dynamic-pool foundation
- #156 R2 (shipped this release path) — orchestrator + collectors + 5-phase shape
- #157 R3 (this release) — analysis pipeline + plots + SUMMARY + doc sync
- #160 — multi-run batching + CIs (S6)
- #163 — decision-plane silent thread death (blocks complete RQ4 narrative)
- #164 — lb-sidecar `smartload.scale` subscription gap (blocks pool-size acceptance gates)
