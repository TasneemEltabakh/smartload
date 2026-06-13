# Anomaly Detection — Known Weaknesses & Open Issues

> Working checklist of weaknesses in the anomaly-detector slice (threshold +
> isolation_forest engines), compiled for thesis/defense prep. Combines an
> in-depth code/methodology review with the PR #158 code-review comments
> (Tasneem Eltabakh, 2026-06-11, merged as 57f95c0 / ff055f3).
>
> **2026-06-14 reconciliation note — read this first.** The "Phase 2 / Stage B"
> resolutions claimed against the A-section items below were written before the
> #165 work was reconciled onto `main`. On `main` the train/inference **domain
> mismatch was actually resolved by #165 "Option 3" (v1.0.7ah)**: a single
> scaler + IsolationForest fit in a synthetic production-shape space, which is
> the **shipped** bundle (`tools/anomaly-training/train_production.py`,
> `pipeline="production_synthetic"`, 25% → 91.4% bench agreement). The Stage-B
> live-injection pipeline described below was kept as a **complementary
> real-data validation / alternative-retrain track**, renamed
> `tools/anomaly-training/train_stage_b.py` (`pipeline="production_live"`); it
> does **not** auto-promote and is not the shipped resolution. Treat the A-item
> "Resolved by Phase 2" notes as "addressed by the #165 recalibration on the
> latency axis, with Stage-B as additional live-data corroboration." The
> engineering items (B1/B2 stability gate, `backend_health` persistence) shipped
> v1.0.7bd. See `docs/features/anomaly-detection.md` and SOT §22 v1.0.7bd.

## A. ML methodology — core weaknesses (highest priority to be able to defend)

- [x] **Train/inference domain mismatch (3 unrelated datasets).** **Resolved by
      Phase 2 (latency axis).** `tools/anomaly-training/collect_production_data.py`
      + `train_production.py` ("Stage B") collect labeled features directly
      from the live stack via latency injection (`/_admin/delay`), using the
      exact `ANOMALY_QUERY` + `build_features_from_rows` pivot the run loop
      uses, and fit a single domain-consistent scaler + thresholds. Train,
      calibrate, and infer now all happen in the production domain on the
      latency axis. The **error-rate axis** remains Stage-A (SMD)-sourced —
      see the "accepted scope tradeoff" note at the end of this section.
- [x] **Feature relabeling, not feature mapping.** **Resolved by Phase 2
      (latency axis).** Stage B's `latency_ms`/`latency_rolling_mean_ms`/
      `latency_rolling_std_ms` are the *actual* production latency features,
      not relabeled SMD dims — no mapping step exists for this axis anymore.
      `error_rate` remains a relabeled SMD dim (Stage A), documented below.
- [x] **Threshold calibration is internally inconsistent.** **Resolved by
      Phase 2.** Stage B derives `healthy_above`/`unhealthy_below`/
      `unhealthy_score_scale` from `decision_function` on the **same** fitted
      scaler used at inference (`smd_scaler` and `production_scaler` alias one
      `StandardScaler`) — no cross-domain percentile mismatch for a Stage-B
      bundle.
- [x] **Multi-axis search-until-pass methodology.** **Resolved by Phase 2 +
      reframed in Phase 6 docs.** Stage B does not search until a gate is
      cleared: `contamination` is derived directly from the observed injection
      rate (pre-registered by the collection schedule, not searched), and the
      temporal train/holdout split is fixed by construction. Stage A's
      original search (kept for cold-start/no-stack scenarios) is now framed
      honestly in `engines/isolation_forest/README.md` as the cold-start
      bootstrap stage of a two-stage pipeline, not the final word — see A6.
- [x] **Interleaved tune/holdout split leaks information.** **Resolved by
      Phase 2 (latency axis).** `collect_production_data.py`'s schedule is
      contiguous per backend (alternating normal/anomalous *windows*, not
      interleaved rows), so `train_production.py`'s temporal train/holdout
      split has positives in both halves by construction — no adjacent-row
      leakage. Stage A's interleaved split (`tune_df = test_df[::2]`) is
      unchanged but is now explicitly the cold-start stage, superseded for
      live deployments by Stage B.
- [x] **The repo's own evidence contradicts transfer.** **Resolved by Phase 6
      docs (reframing).** See A6 below — the F1=0.10 → 0.8012 → Stage-B
      progression is now told as a coherent before/after story rather than a
      contradiction.
- [x] **`contamination=0.005` is an SMD-search artifact.** **Resolved by Phase
      2 (latency axis).** Stage B's `contamination` is derived from the
      observed injection rate in `collect_production_data.py`'s schedule, not
      a cross-dataset search value. Stage A's `contamination=0.005` remains
      for the cold-start bundle and (via aliasing) the error-rate axis of a
      Stage-B bundle — see the accepted-tradeoff note below.
- [~] **No drift detection / no retraining loop.** **Partially addressed by
      Phase 5 (manual).** `tools/anomaly-training/evaluate_live.py` is an
      on-demand runbook step: it scores the *currently shipped* bundle against
      a fresh injection-labeled live collection and flags `DRIFT WARNING` if
      F1 drops more than `DRIFT_F1_TOLERANCE` (0.15) vs the bundle's recorded
      `metadata.test_f1`, appending a `"pipeline": "drift-check"` entry to
      `training_log.json`. `IsolationForestEngine.reload()` is still a no-op
      and there is still no automatic/CI-scheduled retraining loop — full
      automation remains future work.

### A6 — reframing: the F1=0.10 → 0.8012 → Stage-B progression (resolved by Phase 6 docs)

The original concern was that the superseded MST-only pipeline (`train.py`,
now `tools/anomaly-training/superseded/train_mst_superseded.py`) used
production-shaped features and scored F1=0.10, while the SMD pipeline changed
*both* the dataset *and* the label definition to reach F1=0.8012 — looking
like the underlying problem (production-domain anomaly detection) was
side-stepped rather than solved.

With Phase 2 in place, `training_log.json` now tells a complete three-stage
story: `mst` (F1=0.10, invented labels, production-shaped features — proves
the naive approach doesn't work and why), `smd` (F1=0.8012, real labels,
SMD-shaped features — proves the *modeling approach* (IsolationForest +
this feature family) can achieve F1>0.80 given real ground truth, and
provides a usable cold-start artifact), `production` (Stage B — real labels
*and* production-shaped features, closing the loop the `mst` run attempted).
The `mst` result is no longer a contradiction to explain away; it's the
motivating baseline that Stage A and Stage B together resolve.

### Accepted scope tradeoff: error-rate axis remains Stage-A-sourced

Per the approved remediation plan, anomaly injection stayed **latency-only**
(`/_admin/delay`) — no `/_admin/error_rate` endpoint was added to
`test-backends/app.js`. Consequently Stage B validates the **latency axis**
end-to-end, but the **error-rate axis** of a Stage-B bundle is still
Stage-A-sourced (SMD `dim15` relabeled as `error_rate`, calibrated via the
MST-derived `production_scaler`) and therefore still carries the A1–A3/A5/A7
caveats *for that axis only*. This is documented in
`engines/isolation_forest/README.md` ("Scope of Stage B" /
"Domain-adaptation caveat"). Closing it is future work: add an
`/_admin/error_rate` endpoint and extend `live_collection.py`'s schedule to
cover it.

## B. Engineering / operational gaps

- [x] **`sample_count < min_sample_count` → always `"healthy"`.** **Resolved by
      Phase 1.** `runloop.py::apply_stability_gate` now preserves the backend's
      last non-healthy status on a low-sample cycle instead of reverting to
      `healthy` — a fast-failing backend (few samples) no longer reports
      healthy by construction.
- [x] **No hysteresis / cooldown.** **Resolved by Phase 1.** New
      `EnginePolicy.flip_confirmation_cycles` (default 2,
      `DEFAULT_FLIP_CONFIRMATION_CYCLES`); `apply_stability_gate` requires N
      consecutive confirming cycles before a status change is
      published/persisted. `docs/features/anomaly-detection.md` checklist item
      ticked.
- [x] **`backend_health` persistence incomplete.** **Resolved by Phase 1.**
      `app.py::_inference_cycle` writes a `backend_health` row via
      `BACKEND_HEALTH_INSERT` every poll cycle, for every backend (previously
      only on manual `/api/v1/isolate`). lb-sidecar startup hydration now
      always has fresh data. `docs/features/anomaly-detection.md` checklist
      item ticked.
- [ ] **IF engine's output is still just a 2-cutpoint classification of a 1D
      score** — not qualitatively more expressive than the threshold engine's
      output space, despite the additional ML machinery. **Accepted scope
      tradeoff / future work**: changing this would mean changing the
      `AnomalyScore`/`AnomalyEvent` contract (consumed by lb-sidecar, operator
      UI, Grafana, and the SDK) — out of scope for this remediation pass.
      A richer output (e.g. per-feature attribution, confidence interval) is a
      candidate for a future contract-versioned slice.

## C. PR #158 review action items (Tasneem Eltabakh, 2026-06-11)

Blocking items:

- [ ] **#1 — Remove/`.gitignore` `CLAUDE.md`.** Reviewer flags this file as
      carrying AI-tool-workflow framing that conflicts with repo conventions
      for generated artefacts. **Not yet done** — `CLAUDE.md` is still present
      at repo root. *(Intentionally untouched: excluded from the remediation
      plan as a separate repo-policy decision, not something to action without
      confirming with the team; deleting it would remove the guidance Claude
      Code currently relies on for this repo.)*
- [x] **#2 — Pin scikit-learn exactly.** **Resolved by Phase 0.**
      `tools/anomaly-training/requirements.txt` pins `scikit-learn==1.3.2`
      (matches `training_log.json` `sklearn_version`), and
      `services/anomaly-detector/requirements.txt` now also pins
      `scikit-learn==1.3.2` (previously `>=1.3.2`) — verified in this working
      tree. The reviewer's `InconsistentVersionWarning` / load-failure concern
      is closed for both the training tooling and the runtime image.
- [ ] **#3 — Rebase onto current main** (`docs/SOURCE_OF_TRUTH.html` §N2.1,
      `docs/features/anomaly-detection.md` status checkboxes). *(Intentionally
      untouched: this is an ongoing git-housekeeping item tracked at
      integration/PR time, not a discrete code change for this remediation
      pass.)*

Should-fix items:

- [x] **#4 — Live-stack verification.** **Resolved by Phase 3.**
      `tests/integration/test_anomaly_isolation_forest.py` (skipped unless
      `ANOMALY_ENGINE=isolation_forest` is loaded and ready) injects real
      latency via `POST /_admin/delay` on a running container and asserts the
      `backend_health` table flips to `degraded`/`unhealthy` and back to
      `healthy` — exercises the real shipped `.pkl`, real `joblib.load`, and
      the Phase-1 stability gate end-to-end. This also empirically catches any
      sklearn-version mismatch (#2) at load time.
- [x] **#5 — Document unused kwargs.** **Resolved by Phase 0.**
      `IsolationForestEngine.__init__` now has an inline comment next to
      `latency_multiplier`/`error_rate_threshold` noting they're accepted for
      `select_engine()` kwarg-compatibility only and unused by `score()`
      (thresholds are baked into the bundle at training time), in addition to
      the existing README documentation.

Nice-to-have:

- [x] **#6 — Isolate the superseded MST pipeline.** **Resolved by Phase 0.**
      `tools/anomaly-training/train.py` moved to
      `tools/anomaly-training/superseded/train_mst_superseded.py`
      (F1=0.10, `pipeline="mst"` in `training_log.json`); `train_smd.py`'s
      module docstring and `CLAUDE.md`'s "Offline ML training" section
      reference the new path.
- [x] **#7 — Trust-boundary comment in `engine.py`.** **Resolved by Phase 0.**
      `engine.py` now has an inline comment near `joblib.load(path)` stating
      it only ever loads from the in-image `models/` directory (relevant for
      future Helm/K8s work where the artifact source might change).
- [x] **#8 — `LATENCY_DIM = 0` hardcoded.** **Resolved by Phase 0.**
      `train_smd.py` now asserts that the chosen machine set's `LATENCY_DIM`
      interpretation-label frequency rank exceeds a documented threshold
      before training, so a future re-train on a different SMD machine set
      that would silently reuse dim 0 fails loudly instead.

## Strengths (for balance — don't lead with only weaknesses in defense)

- Engine-wrapper pattern (ABC + factory + automatic fallback-to-baseline) is
  clean and the fallback path is unit-tested end-to-end.
- `training_log.json` keeping both the failed MST run (F1=0.10) and the
  successful SMD run (F1=0.8012) is exactly the right shape for SOT/thesis
  defensibility — reviewers specifically called this out positively.
- README's "domain-adaptation caveat" section names `production_scaler` as a
  documented approximation rather than a validated one, and explains why two
  alternative scaler strategies were rejected.
- 11 unit tests using a synthetic inline bundle keep the test suite
  dataset-free while covering bundle validation, feature-order mismatch,
  sample-count gating, and score/status range invariants.
