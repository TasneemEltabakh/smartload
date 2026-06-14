# Forecasting Downstream Benchmark — predictive vs reactive autoscaling

Generated `downstream-8seed` (UTC). Per-second demand, 1800-s (30-min) runs, warm-up w = 20s, cooldown = 60s, capacity = 100 rps/instance, peak = 8× capacity. 8 seeds × 6 profiles. Cells: mean ± 95% CI.

Every dynamic strategy drives the **same shipped** `services/autoscaler/decisions.py::decide` rule inside the **same** warm-up-aware provisioning loop. The only difference between rows is the scalar signal fed to decide() each second. So any SLA gap is attributable to the forecast signal, nothing else.

## Headline

- **Reactive (trailing mean):** SLA 77.09%.
- **MA-predictive (shipped):** SLA 77.09% — within noise of reactive: a trailing average carries no forward projection, so it produces essentially the reactive signal.
- **HR-predictive (harmonic_residual, projects 20s ahead):** SLA 83.49% — **+6.40 pp vs reactive**, closing 34% of the reactive→oracle gap (oracle ceiling 95.67%).

_Predictive scaling only beats reactive when the forecast actually leads the curve. With the moving-average signal the two are statistically indistinguishable; the harmonic_residual projection is what makes predictive > reactive on SLA._

## Aggregate (all profiles × seeds)

| Strategy | SLA% | Unmet-RPS | Over-prov | #Actions | Δ SLA vs reactive |
|---|---:|---:|---:|---:|---:|
| Oracle (perfect foresight — upper bound) | 95.67 ± 1.55 | 15808 ± 6718 | 1016 ± 143 | 14.5 ± 1.4 | +18.58 |
| Reactive (trailing mean — backward-looking floor) | 77.09 ± 2.58 | 29766 ± 6479 | 529 ± 89 | 14.4 ± 1.6 | +0.00 |
| MA-predictive (shipped moving-average forecast) | 77.09 ± 2.58 | 29766 ± 6479 | 529 ± 89 | 14.4 ± 1.6 | +0.00 |
| HR-predictive (harmonic_residual, projects w ahead) | 83.49 ± 2.13 | 24847 ± 7204 | 637 ± 154 | 17.1 ± 1.8 | +6.40 |

## Per-profile breakdown

### Profile: `steady`

| Strategy | SLA% | Unmet-RPS | Over-prov | #Actions | Δ SLA vs reactive |
|---|---:|---:|---:|---:|---:|
| Oracle (perfect foresight — upper bound) | 99.61 ± 0.27 | 124 ± 93 | 1194 ± 81 | 14.0 ± 3.3 | +30.60 |
| Reactive (trailing mean — backward-looking floor) | 69.01 ± 2.22 | 17484 ± 1144 | 340 ± 43 | 20.9 ± 1.3 | +0.00 |
| MA-predictive (shipped moving-average forecast) | 69.01 ± 2.22 | 17484 ± 1144 | 340 ± 43 | 20.9 ± 1.3 | +0.00 |
| HR-predictive (harmonic_residual, projects w ahead) | 68.70 ± 1.00 | 17730 ± 464 | 338 ± 10 | 26.5 ± 1.2 | -0.31 |

### Profile: `diurnal`

| Strategy | SLA% | Unmet-RPS | Over-prov | #Actions | Δ SLA vs reactive |
|---|---:|---:|---:|---:|---:|
| Oracle (perfect foresight — upper bound) | 99.40 ± 0.20 | 166 ± 65 | 638 ± 16 | 16.4 ± 1.2 | +20.47 |
| Reactive (trailing mean — backward-looking floor) | 78.93 ± 0.67 | 10584 ± 311 | 236 ± 14 | 13.8 ± 1.1 | +0.00 |
| MA-predictive (shipped moving-average forecast) | 78.93 ± 0.67 | 10584 ± 311 | 236 ± 14 | 13.8 ± 1.1 | +0.00 |
| HR-predictive (harmonic_residual, projects w ahead) | 83.78 ± 1.27 | 6144 ± 573 | 144 ± 15 | 16.5 ± 0.8 | +4.85 |

### Profile: `ramp`

| Strategy | SLA% | Unmet-RPS | Over-prov | #Actions | Δ SLA vs reactive |
|---|---:|---:|---:|---:|---:|
| Oracle (perfect foresight — upper bound) | 99.07 ± 0.59 | 290 ± 222 | 978 ± 53 | 12.1 ± 1.5 | +18.32 |
| Reactive (trailing mean — backward-looking floor) | 80.75 ± 0.53 | 7069 ± 387 | 262 ± 9 | 8.1 ± 0.3 | +0.00 |
| MA-predictive (shipped moving-average forecast) | 80.75 ± 0.53 | 7069 ± 387 | 262 ± 9 | 8.1 ± 0.3 | +0.00 |
| HR-predictive (harmonic_residual, projects w ahead) | 85.24 ± 1.16 | 6252 ± 950 | 391 ± 17 | 14.4 ± 1.1 | +4.49 |

### Profile: `spike`

| Strategy | SLA% | Unmet-RPS | Over-prov | #Actions | Δ SLA vs reactive |
|---|---:|---:|---:|---:|---:|
| Oracle (perfect foresight — upper bound) | 88.01 ± 0.02 | 57346 ± 372 | 382 ± 1 | 9.0 ± 0.0 | +0.01 |
| Reactive (trailing mean — backward-looking floor) | 88.01 ± 0.02 | 67743 ± 375 | 486 ± 1 | 9.0 ± 0.0 | +0.00 |
| MA-predictive (shipped moving-average forecast) | 88.01 ± 0.02 | 67743 ± 375 | 486 ± 1 | 9.0 ± 0.0 | +0.00 |
| HR-predictive (harmonic_residual, projects w ahead) | 88.01 ± 0.02 | 65946 ± 356 | 468 ± 3 | 9.0 ± 0.0 | +0.01 |

### Profile: `sawtooth`

| Strategy | SLA% | Unmet-RPS | Over-prov | #Actions | Δ SLA vs reactive |
|---|---:|---:|---:|---:|---:|
| Oracle (perfect foresight — upper bound) | 99.50 ± 0.24 | 122 ± 110 | 1914 ± 48 | 23.5 ± 0.4 | +37.06 |
| Reactive (trailing mean — backward-looking floor) | 62.44 ± 0.57 | 25317 ± 440 | 1027 ± 9 | 22.0 ± 0.0 | +0.00 |
| MA-predictive (shipped moving-average forecast) | 62.44 ± 0.57 | 25317 ± 440 | 1027 ± 9 | 22.0 ± 0.0 | +0.00 |
| HR-predictive (harmonic_residual, projects w ahead) | 91.56 ± 0.73 | 3055 ± 392 | 1736 ± 32 | 23.0 ± 0.0 | +29.12 |

### Profile: `burst`

| Strategy | SLA% | Unmet-RPS | Over-prov | #Actions | Δ SLA vs reactive |
|---|---:|---:|---:|---:|---:|
| Oracle (perfect foresight — upper bound) | 88.41 ± 0.15 | 36802 ± 589 | 989 ± 127 | 12.2 ± 0.9 | +5.03 |
| Reactive (trailing mean — backward-looking floor) | 83.38 ± 0.44 | 50400 ± 593 | 823 ± 128 | 12.8 ± 0.6 | +0.00 |
| MA-predictive (shipped moving-average forecast) | 83.38 ± 0.44 | 50400 ± 593 | 823 ± 128 | 12.8 ± 0.6 | +0.00 |
| HR-predictive (harmonic_residual, projects w ahead) | 83.63 ± 1.70 | 49952 ± 7674 | 746 ± 96 | 13.0 ± 0.0 | +0.25 |

---

### Reproducibility footer

- python: `3.11.15`, numpy: `1.26.4`
- decision rule: `services/autoscaler/decisions.py::decide` (shipped)
- forecaster: `services/forecasting/engines/harmonic_residual` (forecast_ahead, steps=w=20)
- seeds: `[0, 1, 2, 3, 4, 5, 6, 7]`; profiles: `['steady', 'diurnal', 'ramp', 'spike', 'sawtooth', 'burst']`
- per-second demand from `demand.py` (vendored from the autoscaler strategy bench — identical shapes/noise).
- runtime: `25.8s`

Re-run: `python experiments/forecasting-downstream-bench/run.py` (deterministic).