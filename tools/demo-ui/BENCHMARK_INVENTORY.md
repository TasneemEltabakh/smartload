# Benchmark & Audit Inventory

The open set of benchmark suites the UI renders, each grounded in its harness:
the **systems** axis, the **parameter/configuration** axis, the **metrics**
(with direction-of-better), and whether the artifact provides numbers today. The
suite list is data, not code — this is a snapshot, not a fixed list.

Direction: ↓ lower better · ↑ higher better · ◎ closer to target · — neutral.
Numbers below are the verified **stale / pre-VPS sample** values; the VPS re-run
replaces them. Four suites have committed numbers; four are results-pending.

---

## Group: System comparison

### baseline-vs-smartload — *SmartLoad vs plain NGINX* (RQ1, #148) — RESULTS PENDING
- **Source:** `experiments/baseline-vs-smartload/` (harness; `results/` empty — `.gitignore`).
- **Systems:** `smartload` (subject, full decision plane) vs `baseline` (floor, plain NGINX round-robin). Same pool/load; only the plane toggles; sides run serially.
- **Parameters (load phases):** `A_ramp`, `A_hold`, `B_anomaly`, `C_sustain` + aggregate.
- **Metrics:** p50↓, p95↓, p99↓ (ms); error_rate↓ (%); rps↑ (RPS). Primary p95.
- **Provenance:** `MANIFEST.json` (timestamp_utc, git_sha, git_state, sides, knobs).

### adaptive-bench — *Adaptive scaling* (RQ4) — RESULTS PENDING
- **Source:** `experiments/adaptive-bench/` (harness; `results/` empty).
- **Systems:** `smartload` only (single-stack phased demonstration — not system-vs-system).
- **Parameters (5 load phases):** `A_bootstrap`, `B_forecast_burst`, `C_sustain`, `D_anomaly_scale_down`, `E_steady` + aggregate.
- **Metrics:** p50↓, p95↓, p99↓ (ms); error_rate↓ (%); rps↑ (RPS); replica_count — (pool size, diagnostic). Primary p95.
- **Note:** the headline is pool growth ahead of saturation (`replica_count`) plus latency/error staying flat.

---

## Group: Routing

### rl-routing — *RL routing (honest null result)* — POPULATED
- **Source:** `experiments/rl-routing-bench/results/20260614T045152Z/SUMMARY.md`.
- **Systems:** `policy_shipped` (subject, PPO/shadow), `round_robin`, `least_connections`, `least_response_time` (baselines), `random_shadow` (floor).
- **Parameters (scenarios):** `homogeneous` (default), `heterogeneous`, `degrading`, `near-idle`, `held_out_dual_degrade`. (No aggregate — scenarios are not averaged.)
- **Metrics:** p95↓, slaviol↓ (% over 200 ms), p50↓, shed↓, hhi↓ (concentration). Primary p95.
- **Verdict (honest):** PPO does **not** beat classical baselines; the winner varies by scenario and is usually `least_response_time`. Per-scenario p95 (PPO / RR / LRT): homogeneous 453 / 512 / **248**; heterogeneous **736** / 824 / 1061; degrading **1037** / 1058 / 1081; near-idle 57 / 85 / **31**; held-out 2940 / 3023 / **2666**. Monotonicity probe PASS. ✅ all in SUMMARY.

---

## Group: Autoscaling

### autoscaler-strategy — *Autoscaler strategy* — POPULATED
- **Source:** `experiments/autoscaler-strategy-bench/results/improved/SUMMARY.md`.
- **Systems:** `C2` (subject, target controller + MA), `S2` (baseline, old ±1 rule + MA), `S5` (baseline, naive threshold), `C4` (candidate, controller + trend), `C1` (ceiling, controller + oracle), `S4max` (reference, static N=max).
- **Parameters (profiles):** aggregate (default), `steady`, `diurnal`, `ramp`, `spike`, `sawtooth`. (Harness also sweeps cooldown 0/30/60/120 — not surfaced.)
- **Metrics:** sla↑ (%), unmet↓ (RPS), overprov↓ (inst·s), actions↓, settling↓ (s). Primary sla.
- **Headline:** C2 98.3% vs S2 77.2% on the same MA signal (aggregate); SLA fully populated per profile. ✅ aggregate + per-profile tables.

---

## Group: Forecasting

*(Paired suites — same forecasters, different question and metrics.)*

### forecasting-engine — *Forecasting engine* (accuracy) — POPULATED
- **Source:** `experiments/forecasting-engine-bench/results/candidate-v1/SUMMARY.md`.
- **Systems:** `harmonic_residual` (subject), `moving_average` (baseline, shipped), `arima_serving` (baseline, OOD), `naive` (floor).
- **Parameters (profiles):** aggregate (default), `steady`, `diurnal`, `spiky`, `ramp`.
- **Metrics:** mape↓, smape↓, rmse↓ (RPS), mae↓ (RPS), coverage ◎0.95, latency↓ (ms). Primary mape.
- **Distinction:** measures *forecast accuracy* (MAPE etc.) on the forecaster itself.

### forecasting-downstream — *Forecasting downstream* (SLA value) — POPULATED
- **Source:** `experiments/forecasting-downstream-bench/results/downstream-8seed/SUMMARY.md`.
- **Systems:** `hr` (subject, HR-predictive), `ma` (baseline, shipped), `reactive` (baseline), `oracle` (ceiling).
- **Parameters (profiles):** aggregate (default), `steady`, `diurnal`, `ramp`, `spike`, `sawtooth`, `burst`.
- **Metrics:** sla↑ (%), unmet↓ (RPS), overprov↓ (inst·s), actions —. Primary sla.
- **Distinction:** measures the *end-to-end SLA impact* of the same forecaster driving the autoscaler (different metric, extra profiles spike/sawtooth/burst). HR +6.3 pp SLA vs reactive.

---

## Group: Anomaly detection

*(Paired suites — overlapping engines, different evaluation regime.)*

### anomaly-detection — *Anomaly detection quality* — RESULTS PENDING
- **Source:** `experiments/anomaly-detection-bench/` (harness; `results/` empty).
- **Systems (6 detectors):** `trend_rule` (subject, shipped default), `threshold`, `zscore` (baselines), `isolation_forest_shipped` (floor), `isolation_forest_retrained`, `trend_forest` (candidates).
- **Parameters (injection profiles):** aggregate, `latency-spike`, `error-burst`, `gradual-degradation`, `clean-control`, plus held-out `partial-failure`, `flappy-clean`. (Harness also sweeps a stability-gate variant {raw, gate-2, gate-3} — not surfaced.)
- **Metrics:** precision↑, recall↑, f1↑, fp_rate↓, detect_latency_s↓, recover_latency_s↓, pr_auc↑. Primary f1.
- **Distinction:** detection quality on **labelled time-series traces** (P/R/F1).

### anomaly-engine — *Engine agreement (decision surface)* — RESULTS PENDING
- **Source:** `experiments/anomaly-engine-bench/` (harness; `results/` empty).
- **Systems (2 engines):** `isolation_forest` (subject, shipped model), `threshold` (baseline).
- **Parameters:** a latency × error-rate **feature grid** (modelled as a single aggregate config — the bench reports decision-surface agreement, not a parameter breakdown).
- **Metrics:** unhealthy_pct —, healthy_pct —; suite KPI: engine-agreement ↑. Primary unhealthy_pct.
- **Distinction:** measures **decision-surface agreement** between the model and the rule across the feature space (a production-scaler sanity check) — **not** detection quality. Complements anomaly-detection.

---

## _bench_common
Shared harness utilities only (`bench_stats.py`: `mean_ci`, `summarize_runs`, `format_mean_ci` — the common `mean ± 95% CI` math). **Not a comparison surface**; no suite emitted.

---

## Audit — Control-loop audit & hardening (POPULATED)
- **Source:** `audit/REPORT.md` (before/after batches `20260615T040536Z` → `20260615T043317Z`).
- **KPIs:** error rate 0.39% (from 54.5%, ~140×); 45 findings confirmed (10 rejected); 5 fixes.
- **Arc:** 54.5 → 60.6 (D1) → 44.1 (D1+D2+D3 @max=5) → 12.3 (+headroom) → 0.39 (+spike hardening).
- **Findings:** D1 (e290b46), D2 (07f861d), D3 (018061c), spike hardening (671b9f2), prime-suspect refuted, k8s deferred.

## Grafana
- 6 dashboards (`smartload-overview/anomaly/forecast/scaling/rl-routing/redis`), port 3000, embedding + anonymous Viewer enabled. Live; pending until the stack is up.

---

## Gaps (what the artifacts do NOT provide directly)
- Artifacts are `SUMMARY.md` + `meta.json` (+ `grid.csv`), not the schema JSON — injection needs a schema-shaped `results.json` (per-parameter `{value, ci95}`); see `RESULTS_INJECTION_GUIDE.md` §4.
- Four suites (baseline-vs-smartload, adaptive-bench, anomaly-detection, anomaly-engine) have **no committed results** — they render fully pending until the VPS run produces them.
- `meta.json` has no `host`; CI is `±` text to split into `ci95`; RL/anomaly emit CSV to reduce.
