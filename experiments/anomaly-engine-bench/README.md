# anomaly-engine-bench

Comparison benchmark for the SmartLoad anomaly engines. Runs both engines (`threshold` and `isolation_forest`) against the same synthetic feature grid and reports where they agree, where they disagree, and what shape the model's decision surface takes.

This is an **investigation tool**, not a CI gate. The output answers two questions the unit-test suite can't:

1. **Is the trained model functionally useful on production-shape features?** The engine's unit tests use a synthetic bundle (`test_engine.py`) or test specific points (`test_isolation_forest_artifact.py`). Neither shows the full decision surface across the latency × error_rate plane that real telemetry walks.
2. **Does the domain-adaptation caveat in `engines/isolation_forest/README.md` matter in practice?** The model was trained on SMD's `[0, 1]` normalised features; the production_scaler reconciles that with real-millisecond inputs as a documented approximation. The grid sweep shows empirically how much that approximation costs.

## Run it

```bash
python experiments/anomaly-engine-bench/run.py
```

No docker stack required — both engines import directly and the `isolation_forest` engine loads the real shipped `.pkl`.

Custom sweep:

```bash
python experiments/anomaly-engine-bench/run.py --latency-max 1200 --error-max 0.30 --steps 32
```

## What you get

Each run drops a timestamped directory under `results/<UTC>/`:

| File | What's in it |
|---|---|
| `grid.csv` | One row per swept cell: `latency_ms`, `error_rate`, `std_ms`, both engines' `status` + `score`, and an `agree` boolean. Grep for `False` to find disagreement rows. |
| `SUMMARY.md` | Headline agreement rate, per-engine verdict distribution, disagreement-pair counts, and an interpretation guide. |
| `heatmap.png` | Two side-by-side heatmaps of verdict over `(error_rate, latency_ms)`. Generated only if matplotlib is installed; the SUMMARY.md is the load-bearing artifact. |

## How to read disagreements

- **Threshold says `unhealthy`, model says `healthy`** — the model is under-reacting to the simple rule. Most likely cause: the production_scaler maps the input latency into a region of the SMD-normalised space where it doesn't look like an outlier. This is the calibration concern the model's README documents; the grid quantifies it.
- **Threshold says `healthy`, model says `unhealthy`** — the model is finding signal the rule misses (latency-std interactions, error-rate compounding effects from the contamination=0.005 boundary). This is the value-add case for shipping the trained model at all.
- **Pure agreement everywhere** is suspicious — either the sweep range is too narrow or the model has degenerated to a constant. Re-run with `--latency-max 1200 --error-max 0.40 --steps 32` to widen the search.

## Where the model came from

The `.pkl` consumed here was trained by `tools/anomaly-training/train_smd.py`. The training search picked `machine-1-1 + machine-1-6` from SMD (real labels), `dim1` → latency, `dim15` → error_rate, rolling window 5, `contamination=0.005` — landing test F1 = 0.8012 against `test_label/`. Full provenance: `tools/anomaly-training/training_log.json`.

## Not in scope

- **Real-traffic replay** — the live-stack test at `tests/integration/test_isolation_forest_live_stack.py` covers the closed-loop case (injected latency on a backend → engine publishes UNHEALTHY).
- **Multi-run statistics / confidence intervals** — issue #160 covers this for the baseline-vs-smartload bench; once that lands, the pattern can extend here.
- **Comparison against ground truth** — `train_smd.py` already reports F1 against SMD's `test_label/` at training time. This bench is about decision-surface shape, not metric validation.
