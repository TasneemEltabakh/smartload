# Forecasting Engine Benchmark — Results

Generated `candidate-v1` (UTC). Rolling-origin / walk-forward, 1-step horizon, 5min buckets.

Single-step forecasters compared on synthetic RPS series: **naive** (persistence floor, local — not shipped), **moving_average** (window=60, shipped), **arima_serving** (ARIMA(2, 0, 2) production serving path, shipped), and **harmonic_residual** (robust dynamic-harmonic-regression + AR(1) residual with split-conformal bands — the candidate). Every contender is handed the identical history window at each origin.

5 seeds × 4 profiles. ~130 scored origins per series (last 15% of a 3-day span). All metrics are finite-masked; cells show mean ± 95% CI over seeds.

> **Out-of-distribution note.** The ARIMA artifact's parameters were fit on the Alibaba production trace, not on these synthetic series. It is therefore evaluated out-of-distribution here. That is a fair generalization test and deliberately avoids the in-sample leakage that scoring it on a tail of its own training data would introduce.

## Overall roll-up (all profiles × seeds)

| Engine | MAPE% | sMAPE% | RMSE | MAE | CI-coverage | latency_ms | MAPE<20% |
|---|---:|---:|---:|---:|---:|---:|:--:|
| `naive` | 7.5 ± 1.7 | 7.5 ± 1.7 | 10.10 ± 5.70 | 5.02 ± 1.20 | n/a | 0.34 ± 0.00 | PASS |
| `moving_average` | 10.5 ± 3.9 | 10.2 ± 3.6 | 11.52 ± 5.86 | 6.71 ± 2.09 | 0.570 ± 0.090 | 0.01 ± 0.00 | PASS |
| `arima_serving` | 8.9 ± 1.1 | 9.2 ± 1.1 | 13.36 ± 5.61 | 8.67 ± 3.48 | 0.759 ± 0.184 | 57.12 ± 0.17 | PASS |
| `harmonic_residual` | 5.4 ± 1.2 | 5.4 ± 1.2 | 8.29 ± 5.14 | 3.47 ± 0.69 | 0.955 ± 0.005 | 0.70 ± 0.00 | PASS |

## Per-profile breakdown

### Profile: `steady`

| Engine | MAPE% | sMAPE% | RMSE | MAE | CI-coverage | latency_ms | MAPE<20% |
|---|---:|---:|---:|---:|---:|---:|:--:|
| `naive` | 6.8 ± 0.5 | 6.7 ± 0.5 | 4.12 ± 0.32 | 3.34 ± 0.23 | n/a | 0.34 ± 0.00 | PASS |
| `moving_average` | 4.8 ± 0.1 | 4.8 ± 0.1 | 3.01 ± 0.06 | 2.39 ± 0.06 | 0.694 ± 0.030 | 0.01 ± 0.00 | PASS |
| `arima_serving` | 6.2 ± 0.3 | 6.4 ± 0.3 | 3.86 ± 0.15 | 3.16 ± 0.16 | 1.000 ± 0.000 | 56.80 ± 0.47 | PASS |
| `harmonic_residual` | 4.8 ± 0.2 | 4.8 ± 0.2 | 3.00 ± 0.09 | 2.39 ± 0.10 | 0.954 ± 0.012 | 0.70 ± 0.00 | PASS |

### Profile: `diurnal`

| Engine | MAPE% | sMAPE% | RMSE | MAE | CI-coverage | latency_ms | MAPE<20% |
|---|---:|---:|---:|---:|---:|---:|:--:|
| `naive` | 10.0 ± 0.8 | 10.0 ± 0.8 | 5.50 ± 0.43 | 4.48 ± 0.32 | n/a | 0.34 ± 0.00 | PASS |
| `moving_average` | 18.0 ± 0.9 | 16.9 ± 0.9 | 9.55 ± 0.41 | 8.16 ± 0.39 | 0.382 ± 0.043 | 0.01 ± 0.00 | PASS |
| `arima_serving` | 8.9 ± 0.6 | 9.1 ± 0.6 | 5.14 ± 0.27 | 4.13 ± 0.24 | 0.995 ± 0.005 | 57.26 ± 0.52 | PASS |
| `harmonic_residual` | 7.2 ± 0.4 | 7.1 ± 0.3 | 4.00 ± 0.12 | 3.18 ± 0.13 | 0.954 ± 0.012 | 0.71 ± 0.00 | PASS |

### Profile: `spiky`

| Engine | MAPE% | sMAPE% | RMSE | MAE | CI-coverage | latency_ms | MAPE<20% |
|---|---:|---:|---:|---:|---:|---:|:--:|
| `naive` | 10.9 ± 2.6 | 11.1 ± 2.8 | 25.28 ± 22.17 | 7.81 ± 5.16 | n/a | 0.34 ± 0.00 | PASS |
| `moving_average` | 15.7 ± 13.4 | 15.5 ± 12.2 | 25.98 ± 23.82 | 9.71 ± 9.44 | 0.800 ± 0.115 | 0.01 ± 0.00 | PASS |
| `arima_serving` | 9.9 ± 4.1 | 10.2 ± 4.0 | 23.37 ± 20.98 | 6.75 ± 4.93 | 0.945 ± 0.064 | 57.02 ± 0.17 | PASS |
| `harmonic_residual` | 7.8 ± 1.1 | 8.2 ± 1.5 | 22.16 ± 19.67 | 5.14 ± 2.80 | 0.957 ± 0.022 | 0.70 ± 0.00 | PASS |

### Profile: `ramp`

| Engine | MAPE% | sMAPE% | RMSE | MAE | CI-coverage | latency_ms | MAPE<20% |
|---|---:|---:|---:|---:|---:|---:|:--:|
| `naive` | 2.3 ± 0.2 | 2.3 ± 0.2 | 5.49 ± 0.43 | 4.46 ± 0.31 | n/a | 0.34 ± 0.00 | PASS |
| `moving_average` | 3.3 ± 0.1 | 3.4 ± 0.1 | 7.54 ± 0.22 | 6.59 ± 0.23 | 0.405 ± 0.019 | 0.01 ± 0.00 | PASS |
| `arima_serving` | 10.5 ± 0.0 | 11.1 ± 0.0 | 21.08 ± 0.11 | 20.64 ± 0.08 | 0.097 ± 0.032 | 57.38 ± 0.33 | PASS |
| `harmonic_residual` | 1.6 ± 0.1 | 1.6 ± 0.1 | 4.00 ± 0.12 | 3.18 ± 0.13 | 0.954 ± 0.012 | 0.71 ± 0.00 | PASS |

## How to read this

- **MAPE** is the headline (SOT KPI: < 20%). The `MAPE<20%` column marks PASS/FAIL on the seed-mean. **naive** is the floor — an engine that does not beat persistence is not earning its keep.
- **sMAPE** and **CI-coverage** are the honesty checks. MAPE punishes under-prediction and over-prediction asymmetrically and blows up near small actuals; sMAPE is symmetric and bounded. A CI-coverage far from 0.95 means the 95% band is miscalibrated (too narrow if < 0.95, too wide if ≫ 0.95). **naive** emits no band, so its coverage is `n/a`.
- **RMSE/MAE** are in raw RPS units — useful for absolute error size, not comparable across profiles with different load levels.

---

### Reproducibility footer

- statsmodels: `0.14.6`
- ARIMA order: `(2, 0, 2)` (d=0 — no differencing)
- bucket size: `5min`
- horizon: `1-step`
- moving_average window: `60`
- seeds: `[1, 2, 3, 4, 5]`
- profiles: `['steady', 'diurnal', 'spiky', 'ramp']`
- holdout fraction: `0.15`
- MAPE gate: `< 20.0%` (per-row PASS/FAIL above)

Re-run: `python experiments/forecasting-engine-bench/run.py` (deterministic — same args reproduce these numbers, latency aside).