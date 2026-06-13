# Anomaly-Detector Remediation — Completed (reconciled to main as v1.0.7bd, 2026-06-14)

> Summary of the remediation pass that closed out
> `docs/planned/anomaly-detection-known-weaknesses.md`. This file is a one-stop
> "what changed and where" index; see the linked docs for full detail.
>
> **Reconciliation note.** This was authored against an earlier branch targeting
> a never-shipped "v1.0.7w"; reconciled onto `main` (which had independently
> shipped #165 plus the v1.0.7ap/aq guards) it landed as **v1.0.7bd**. Two
> corrections to the phase summaries below: **(1) Phase 2** — the train/inference
> domain mismatch is resolved on `main` by the **#165 "Option 3" production-shape
> recalibration** (the shipped bundle, `train_production.py`,
> `pipeline="production_synthetic"`); the Stage-B live-injection pipeline here
> was kept as a **complementary** validation / alternative-retrain track, renamed
> `train_stage_b.py` (`pipeline="production_live"`), which does not auto-promote.
> **(2) Phase 0** — `requirements.txt` already pins `scikit-learn==1.3.2` on
> `main` (and retains `joblib`/`prometheus-client`/`waitress`), and `CLAUDE.md`
> was dropped rather than referenced. The Phase-1 stability gate + persistence
> and the Phase-3/4 live/e2e tests landed as described.

## Phase 0 — Hygiene fixes (PR #158 leftovers)

- `services/anomaly-detector/requirements.txt`: pinned `scikit-learn==1.3.2`
  (was `>=1.3.2`), matching the training tooling and bundle metadata.
- `services/anomaly-detector/engines/isolation_forest/engine.py`: inline
  comments — unused `latency_multiplier`/`error_rate_threshold` kwargs
  (kept for `select_engine()` compatibility only), and a trust-boundary note
  on `joblib.load()` (in-image `models/` only).
- `tools/anomaly-training/train.py` moved to
  `tools/anomaly-training/superseded/train_mst_superseded.py`; references
  updated in `train_smd.py` and `CLAUDE.md`.
- `tools/anomaly-training/train_smd.py`: new `_assert_latency_dim_rank()` —
  fails loudly if `LATENCY_DIM` drops out of the top-ranked
  `interpretation_label` dims for a future machine set.

## Phase 1 — Run-loop stability gate + `backend_health` persistence

- `services/anomaly-detector/runloop.py`: new `BackendState` dataclass +
  `apply_stability_gate(raw, low_sample, state, confirmation_cycles)`:
  - Low-sample cycles preserve the last non-healthy status (fixes the
    "fast-failing backend reports healthy" blind spot).
  - A status flip needs `flip_confirmation_cycles` (new `EnginePolicy` field,
    default 2) consecutive confirming cycles before publish/persist.
- `services/anomaly-detector/app.py`: `_inference_cycle` now writes a
  `backend_health` row via `BACKEND_HEALTH_INSERT` every poll cycle, for
  every backend (previously only on manual `/api/v1/isolate`).

## Phase 2 — Stage B production-domain training

- New `tools/anomaly-training/collect_production_data.py`: drives the live
  compose stack through alternating normal/latency-injected windows
  (`POST /_admin/delay`), records labeled `BackendFeatures` via the real
  `ANOMALY_QUERY` + `build_features_from_rows` pivot → writes
  `datasets/smartload-live/<timestamp>/production_features.csv` +
  `manifest.json`.
- New `tools/anomaly-training/train_production.py`: single domain-consistent
  `StandardScaler` + `IsolationForest`, temporal train/holdout split,
  injection-derived `contamination`. Appends `pipeline="production"` to
  `training_log.json`. Output bundle matches the existing schema
  (`smd_scaler`/`production_scaler` alias one scaler) — no engine changes
  needed. Promotion to the shipped `.pkl` is a manual `cp` after reviewing
  the printed comparison.
- Closes the train/inference domain mismatch on the **latency axis**; the
  **error-rate axis** remains Stage-A (SMD)-sourced — documented as an
  accepted scope tradeoff / future work (would need an `/_admin/error_rate`
  endpoint).

## Phase 3 — Live-stack acceptance test

- New `tools/anomaly-training/live_collection.py`: shared chaos-injection +
  feature-collection helpers, re-exported by `tests/integration/_chaos.py`.
- New `tests/integration/test_anomaly_isolation_forest.py` (skipped unless
  `ANOMALY_ENGINE=isolation_forest` is loaded and ready): injects real
  latency via `/_admin/delay`, asserts `backend_health` flips
  `degraded`/`unhealthy` → `healthy` through the real `.pkl` and the Phase-1
  stability gate.

## Phase 4 — Scenario + e2e triangulation

- New `examples/scenarios/anomaly-detection/anomaly_walk.py`.
- New `tests/e2e/anomaly-detection/` (`conftest.py` + `test_anomaly_detection.py`):
  `/health` engine fields, `client.engines.state()` policy snapshot,
  `client.isolate()` round trip, `smartload.anomaly` envelope delivery via
  `client.engines.subscribe()`, lb-sidecar exclusion/recovery via
  `/api/v1/lb/state`.
- `docs/features/anomaly-detection.md`: status checklist updated (persistence,
  cool-down, SDK, scenario, e2e all ticked); Isolation Forest bullet rewritten
  for the two-stage pipeline.

## Phase 5 — Drift-check tooling

- New `tools/anomaly-training/evaluate_live.py`: on-demand runbook step —
  scores the **currently shipped** `isolation_forest.pkl` (no training)
  against a fresh injection-labeled live collection, compares F1 against the
  bundle's recorded `metadata.test_f1` (`DRIFT_F1_TOLERANCE = 0.15`), appends
  `pipeline="drift-check"` to `training_log.json`.

## Phase 6 — Docs closure

- `services/anomaly-detector/engines/isolation_forest/README.md`: rewritten
  for the two-stage pipeline, Stage A/B results, rescoped domain-adaptation
  caveat, new "Drift checks" section.
- `docs/SOURCE_OF_TRUTH.html`: bumped to **v1.0.7w** (2026-06-12); rewrote the
  §8.5 N2.1 row; added a resolved risk-register row for the train/inference
  domain mismatch; flipped the §25.9 "Anomaly detection" slice row to
  *Shipped*; added a v1.0.7w changelog entry at the top of §22.
- `docs/planned/anomaly-detection-known-weaknesses.md`: checked off all
  resolved items (A1-A3, A5, A7, B1-B3, C#2, C#4-C#8) with phase notes;
  reframed A6 (mst→smd→production progression); marked A8 partial; added
  accepted-tradeoff notes for B4 and the error-rate axis. C#1 (`CLAUDE.md`
  removal) and C#3 (rebase) intentionally left open — repo-policy / ongoing
  housekeeping items, out of scope here.
- `CLAUDE.md`: "Offline ML training" section now documents Stage A, Stage B,
  and the drift check; "anomaly-detector specifics" section documents the
  stability gate and the full pipeline progression.

## Final verification

- `python scripts/lint-structure.py` — no warnings for `anomaly-detection`.
- `python -m ruff check services test-backends tools/anomaly-training tests/e2e/anomaly-detection tests/integration/test_anomaly_isolation_forest.py` — all checks passed.

## What's intentionally still open (by design)

- **Error-rate axis** of the Isolation Forest remains Stage-A (SMD)-sourced —
  needs an `/_admin/error_rate` injection endpoint to close (future work).
- **Full automated drift/retrain loop** — Phase 5 is manual/on-demand only.
- **C#1** (`CLAUDE.md` removal) and **C#3** (rebase) from PR #158 — repo-policy
  / housekeeping, not part of this remediation.
- **Webhook fan-out (#130)** — tracked as its own separate slice, does not
  block the anomaly-detection slice's *Shipped* status.
