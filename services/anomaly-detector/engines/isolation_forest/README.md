# isolation_forest engine

Scikit-learn IsolationForest trained on the Server Machine Dataset (SMD / OmniAnomaly), with real per-timestep anomaly labels from `test_label/`. Replaces the threshold baseline when `ANOMALY_ENGINE=isolation_forest` and the model file (`services/anomaly-detector/models/isolation_forest.pkl`) is present and loads successfully.

## Behavior

The engine takes the same four fields as the threshold engine: `latency_ms`, `latency_rolling_mean_ms`, `error_rate`, `latency_rolling_std_ms` (in `FEATURE_ORDER`), gated by `sample_count >= min_sample_count` (below that, always `healthy`/0.0).

The `.pkl` is a bundle, not a bare model: `{model, smd_scaler, production_scaler, feature_order, thresholds, metadata}`. The model was fit on SMD data, which is normalized to `[0, 1]` per machine — a different scale to production's real-millisecond latencies and `[0,1]` error fraction. To bridge this, the engine applies `production_scaler.transform()` (a `StandardScaler` fit on MST-2021-derived features — the closest available proxy for live `ANOMALY_QUERY` data) to the raw `BackendFeatures` before calling `model.decision_function()`.

The resulting raw score is classified using thresholds baked into the bundle at training time:
- `raw > thresholds.healthy_above` → `healthy`, score `0.0`
- `thresholds.unhealthy_below <= raw <= thresholds.healthy_above` → `degraded`, score `0.5`
- `raw < thresholds.unhealthy_below` → `unhealthy`, score `min(1.0, abs(raw - unhealthy_below) / unhealthy_score_scale)`

If the bundle has no `thresholds` key, the engine falls back to `healthy_above=0.05`, `unhealthy_below=-0.05`, `unhealthy_score_scale=0.5`.

## Why it ships

This is the trained replacement for `threshold` (which remains the day-1 baseline and the fallback path when this model's `.pkl` is missing or fails to load/validate).

**Methodology** (`tools/anomaly-training/train_smd.py`): the SOT originally specified training on NAB + Yahoo SMD. NAB's data is univariate (timestamp, value) and doesn't map onto the 4-feature `BackendFeatures` schema, so this pipeline trains on **SMD only**. SMD's 38 columns are searched (latency candidates: dim1, the dimension most frequently implicated across all 28 machines' `interpretation_label/` files; error-rate candidates: the next-most-implicated dims, all bounded `[0,1]`) together with a rolling window size and an `IsolationForest` `contamination` value, picking the combination with the best F1 on a held-out half of SMD's labeled test split. The winning configuration trains on **machine-1-1 + machine-1-6** (pooled — a single machine's anomalies were too sparse to clear the F1 gate), maps **SMD dim1 → latency family** (rolling window 5) and **SMD dim15 → error_rate**, with `contamination=0.005`.

**Result**: `test_f1=0.8012`, `precision=0.8681`, `recall=0.7439` against real SMD `test_label` — **PASS** of the SOT N2.1 KPI gate (F1 > 0.80). Full provenance (machines, dims, window, contamination, sklearn version) is in `tools/anomaly-training/training_log.json` (the entry with `"pipeline": "smd"`); the earlier `"pipeline": "mst"` entry (test_f1=0.10, invented labels) is retained for historical comparison only and is superseded.

**Domain-adaptation caveat**: the `production_scaler` mapping is a documented approximation, not a validated one — no labeled production telemetry exists to check it against. Both `smd_scaler` (the model's training space) and `production_scaler` map their respective domains to mean-0/std-1 space; the qualitative relationships between the 4 features (latency ≥ rolling mean; high error_rate co-occurring with instability) are preserved by construction in both domains, since both are derived the same way (raw value / rolling mean / rolling std / bounded error fraction). Two alternatives were rejected: a single scaler fit on MST only (would crush SMD's already-narrow `[0,1]` features near zero, destroying the separation the model needs to learn) and a single scaler fit on the union of both datasets (MST's much larger raw scale would dominate, making SMD's contribution numerically negligible).

## Tuning

Thresholds (`healthy_above`, `unhealthy_below`, `unhealthy_score_scale`) and the feature mapping/contamination are baked into the `.pkl` at training time — re-tuning means re-running `tools/anomaly-training/train_smd.py` (optionally widening its search grids), not a runtime policy field. The only runtime knob shared with other engines is `min_sample_count` (data-quality gate); `latency_multiplier`/`error_rate_threshold` are accepted for `select_engine()` kwarg compatibility but unused.

## Tests

- `test_normal_features_are_not_unhealthy` — a feature vector near the centre of the training distribution is classified `healthy` or `degraded`, never `unhealthy`.
- `test_extreme_outlier_is_unhealthy` — a feature vector far outside the training range is classified `unhealthy` with a score in `(0, 1]`.
- `test_sample_count_gate_returns_healthy` — below `min_sample_count`, the engine returns `healthy`/0.0 regardless of how anomalous the features look.
- `test_missing_model_file_raises` — a nonexistent model path raises `FileNotFoundError` (so `select_engine()` can fall back to `threshold`).
- `test_score_always_in_unit_range` — across a range of latency values, `score` always lies in `[0, 1]`.
- `test_status_is_always_a_valid_label` — `status` is always one of `healthy`/`degraded`/`unhealthy`.
- `test_engine_accepts_policy_kwargs` — constructor accepts and stores `latency_multiplier`/`error_rate_threshold`/`min_sample_count`.
- `test_reload_is_a_noop` — `reload()` does not raise (the model is immutable for the process lifetime).
- `test_malformed_bundle_raises` — a `.pkl` containing a bare `IsolationForest` (old format, not a bundle dict) raises `ValueError`.
- `test_feature_order_mismatch_raises` — a bundle whose `feature_order` doesn't match the engine's `FEATURE_ORDER` raises `ValueError`.
- `test_thresholds_default_when_absent` — a bundle with no `thresholds` key still constructs and scores using the hardcoded fallback thresholds.
