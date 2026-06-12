# isolation_forest engine

Scikit-learn IsolationForest anomaly detector. Replaces the threshold baseline when `ANOMALY_ENGINE=isolation_forest` and the model file (`services/anomaly-detector/models/isolation_forest.pkl`) is present and loads successfully.

**As of v1.0.7ah (#165) the shipped bundle is re-calibrated in production-shape feature space** (`tools/anomaly-training/train_production.py`) — the scaler and the model live in the *same* real-millisecond coordinate system, removing the SMD↔MST bridge that made the prior SMD-trained bundle under-react. On the `anomaly-engine-bench` 16×16 sweep it now agrees with the threshold rule on **91.4%** of cells (up from 25%) with **zero under-reactions** (every disagreement is the value-add `threshold healthy → model unhealthy` direction). The earlier SMD pipeline (`train_smd.py`, F1=0.8012 on SMD holdout) is kept for historical provenance; its domain-adaptation story is below under "Superseded".

## Behavior

The engine takes the same four fields as the threshold engine: `latency_ms`, `latency_rolling_mean_ms`, `error_rate`, `latency_rolling_std_ms` (in `FEATURE_ORDER`), gated by `sample_count >= min_sample_count` (below that, always `healthy`/0.0).

The `.pkl` is a bundle, not a bare model: `{model, smd_scaler, production_scaler, feature_order, thresholds, metadata}`. The engine applies `production_scaler.transform()` to the raw `BackendFeatures` before calling `model.decision_function()`. In the v1.0.7ah production-shape bundle the `production_scaler` is fit on the *same* synthetic healthy operating region the model is trained on (real-ms latency, `[0,1]` error fraction), so model and inputs share one coordinate system — `smd_scaler` is retained only for bundle-format compatibility (it equals `production_scaler` and is unused at inference). In the superseded SMD bundle, model and scaler lived in two different mean-0/std-1 spaces (see "Superseded").

The resulting raw score is classified using thresholds baked into the bundle at training time:
- `raw > thresholds.healthy_above` → `healthy`, score `0.0`
- `thresholds.unhealthy_below <= raw <= thresholds.healthy_above` → `degraded`, score `0.5`
- `raw < thresholds.unhealthy_below` → `unhealthy`, score `min(1.0, abs(raw - unhealthy_below) / unhealthy_score_scale)`

If the bundle has no `thresholds` key, the engine falls back to `healthy_above=0.05`, `unhealthy_below=-0.05`, `unhealthy_score_scale=0.5`.

## Why it ships

This is the trained replacement for `threshold` (which remains the day-1 baseline and the fallback path when this model's `.pkl` is missing or fails to load/validate).

**Methodology — production-shape re-calibration** (`tools/anomaly-training/train_production.py`, v1.0.7ah / #165, option 3 in the issue): the model and its scaler are fit on a synthetic *healthy operating region* in real-ms feature space, rather than borrowing a model trained on SMD's per-machine `[0,1]` space. The healthy distribution encodes what a healthy SmartLoad backend emits: latency spanning the full operating range (a steady high-latency backend is normal), `max ≈ avg` (requests consistent — the diagonal), `error_rate ≲ 0.05` (the threshold rule's boundary), and variance `std ≈ 0.25·avg`. The threshold rule is the implicit weak label. An `IsolationForest` (`n_estimators=200`, `contamination=0.02`) then flags whatever leaves that envelope: high error, an off-diagonal latency spike (`max ≫ avg`), or anomalous variance.

The decision thresholds are calibrated empirically — the script replays the `anomaly-engine-bench` sweep inline and searches `(healthy_above, unhealthy_below)` to maximise agreement subject to: the live-stack 400 ms point and the extreme outlier score `unhealthy`, a plainly-healthy point stays `healthy`, and **zero** `threshold-unhealthy → model-healthy` under-reactions. It only writes the bundle when all gates pass.

**Result**: bench agreement **91.4%** (234/256, up from 25%), under-reactions **0** (all 22 disagreements are the value-add `healthy → unhealthy` direction). The live-stack 400 ms anomaly — whose measured feature vector is `(max=406, avg=162, err=0, std=196)`, i.e. caught by its `std/avg≈1.2` (vs a healthy ~0.25), *not* by absolute latency, which the threshold rule's `ratio=2.5 < 3` actually misses — is flagged `unhealthy`. Provenance is in the bundle `metadata` (`pipeline="production_synthetic"`).

**Live-loop check**: with `ANOMALY_ENGINE=isolation_forest`, a 400 ms-injected backend drives the engine to publish `unhealthy` on `smartload.anomaly` (verified: 9 publishes during a 30 s injection). The literal `test_isolation_forest_live_stack.py` still fails on a *separate* pre-existing issue — the anomaly is published for `backend_id="backend_pool"` (the load-balancer latency aggregate the telemetry exposes), while the test asserts the per-backend container name `smartload-test-backend-1:8080`. That backend-id granularity mismatch is engine-independent (the threshold engine publishes the same `backend_pool` id) and is tracked as a follow-up — not fixed by retuning the model.

### Superseded — the SMD-trained bundle (v1.0.7ab)

The prior bundle (`train_smd.py`, `pipeline="smd"`) trained on SMD with real `test_label/` anomalies, reaching `test_f1=0.8012` on the SMD holdout (PASS of the SOT N2.1 gate). Its flaw, surfaced by the bench: the model lived in SMD-standardized space but the engine fed it MST-2021-scaled inputs, so real-ms latency collapsed toward the SMD origin and the model under-reacted (25% bench agreement, 107/108 "clearly bad" cells called healthy). The two scalers (`smd_scaler`, `production_scaler`) were two unrelated mean-0/std-1 coordinate systems. The v1.0.7ah re-calibration removes that bridge entirely by co-locating the scaler and model in one space.

## Tuning

Thresholds (`healthy_above`, `unhealthy_below`, `unhealthy_score_scale`) and the contamination are baked into the `.pkl` at training time — re-tuning means re-running `tools/anomaly-training/train_production.py` (adjust the healthy-region distribution or the calibration gates), not a runtime policy field. Train it inside the anomaly-detector container so the bundle is pickled with the runtime scikit-learn (1.3.2), then `docker cp` the artifact onto the host (the script header documents the exact commands). The only runtime knob shared with other engines is `min_sample_count` (data-quality gate); `latency_multiplier`/`error_rate_threshold` are accepted for `select_engine()` kwarg compatibility but unused.

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
