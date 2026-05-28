# forecasting-training

Offline training pipeline for the SmartLoad forecasting service's ARIMA engine. This directory ships **separately from the runtime Docker image** — Tasneem's review of the original PR (#144) flagged that statsmodels + prophet + scikit-learn shouldn't bloat the production forecasting container.

## Files

| File | Purpose |
|---|---|
| `preprocess.py` | Loads the raw Alibaba industrial trace, resamples to 5-minute buckets, computes exogenous features (avg_cpu_util, system_load), writes the prepared `alibaba_industrial_data.csv`. |
| `train.py` | Trains ARIMA, ARIMAX, and Prophet models on the prepared data; holds out 15% for test; emits `training_log.json` and a `.pkl` per model. The ARIMA artifact wins on test MAPE (25.0%, +22.77% over the moving-average baseline) and ships in `services/forecasting/models/arima_model.pkl`. |
| `training_log.json` | Reference log from Nada's 2026-05-13/16 training runs. The active artifact's row is the `model=arima` entry. |
| `requirements.txt` | Training-only deps. NOT in the runtime image. |

## Activation

```bash
cd tools/forecasting-training
python -m venv .venv && source .venv/bin/activate   # or use your existing env
pip install -r requirements.txt

# Step 1 — preprocess (requires the raw Alibaba trace)
python preprocess.py --input <path-to-alibaba-raw> --output alibaba_industrial_data.csv

# Step 2 — train
python train.py --data alibaba_industrial_data.csv --out-dir out/

# Step 3 — promote the winning artifact to the runtime
cp out/arima_model.pkl ../../services/forecasting/models/arima_model.pkl
cp out/training_log.json training_log.json   # update the in-repo reference log
```

Then activate the new artifact at runtime per `services/forecasting/engines/arima/README.md`.

## Datasets

Per SOT §18, datasets are not committed — `scripts/download-datasets.sh` is the canonical acquisition path. The Alibaba trace specifically is not fetched by that script today (#97); for now, point `--input` at a local copy.

## Acceptance criteria

Per SOT §17.4 the ARIMA engine targets test MAPE < 20% on a 5-minute horizon. The current artifact (25.0%) falls short — the moving-average baseline stays the default `FORECAST_ENGINE` value until a tuned model hits the bar. When that lands:

1. Rerun `train.py`, confirm `training_log.json` shows the new `passed: true` row.
2. Promote the new `arima_model.pkl`.
3. Update `services/forecasting/engine_base.py` if the new model needs an updated `select_engine` signature (it probably doesn't).
4. Flip the docker-compose `FORECAST_ENGINE` default from `moving_average` to `arima` (it's currently soft-set so an operator just sets `FORECAST_ENGINE=arima` in `.env`).

## Author attribution

Model architecture, feature engineering, and training pipeline by **Nada Nabil** ([@nadasoudi](https://github.com/nadasoudi)), originally in PR #144. Engine handoff to the #138 plugin layout + relocation here by **Tasneem Muhammed**.
