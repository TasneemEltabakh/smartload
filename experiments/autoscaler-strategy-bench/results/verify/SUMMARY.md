# Autoscaler strategy benchmark — results

Five provisioning strategies on the same demand realization per (profile, seed), replayed identically. All dynamic strategies call the shipped `decide()` rule; only the input signal varies. S1 is the oracle ceiling (true future demand); S2 is the headline predictive number (real moving-average forecaster, which lags); S3 is the production reactive fallback; S4 anchors the SLA-vs-cost extremes; S5 is a util-threshold baseline.

_Params: per-instance capacity = 100 rps, min_backends = 1, max_backends = 10, run length = 1800 s (30 min), forecast horizon = 300 s, warm-up w = 20 s, cooldown = 60 s, peak demand = 8×capacity = 800 rps, seeds = [1000, 1001, 1002, 1003, 1004, 1005, 1006, 1007] (n=8). Cells: mean ± 95% t-CI. SLA% = fraction of steps with capacity ≥ demand; Unmet-RPS = Σ max(0, demand − capacity); Over-prov cost = Σ max(0, instances − ceil(demand/capacity)) instance-seconds; #ScaleActions = non-NOOP decisions; Settling-s = mean steps from a demand step-change until capacity ≥ demand._

### Profile: steady

| Strategy | SLA% | Unmet-RPS | Over-prov cost | #ScaleActions | Settling-s |
|---|---|---|---|---|---|
| S1 Predictive-oracle (upper bound) | 99.5 ± 0.3 % | 246 ± 159 | 1180 ± 76 | 13.6 ± 3.6 | 0.0 ± 0.0 s |
| S2 Predictive-realistic (MA forecast) | 69.3 ± 1.8 % | 17490 ± 1013 | 358 ± 39 | 22.9 ± 2.3 | 0.9 ± 0.1 s |
| S3 Reactive (trailing mean) | 69.3 ± 1.8 % | 17490 ± 1013 | 358 ± 39 | 22.9 ± 2.3 | 0.9 ± 0.1 s |
| S4 Static N=max (SLA-optimal) | 100.0 ± 0.0 % | 0 ± 0 | 2715 ± 23 | 0.0 ± 0.0 | 0.0 ± 0.0 s |
| S4 Static N=cost-matched | 69.7 ± 20.6 % | 17160 ± 11794 | 341 ± 385 | 0.0 ± 0.0 | 0.8 ± 0.6 s |
| S5 Naive-threshold | 99.6 ± 0.3 % | 213 ± 158 | 2651 ± 15 | 1.6 ± 0.4 | 0.0 ± 0.0 s |

_Profile **steady**, n=8 seeds._

### Profile: diurnal

| Strategy | SLA% | Unmet-RPS | Over-prov cost | #ScaleActions | Settling-s |
|---|---|---|---|---|---|
| S1 Predictive-oracle (upper bound) | 99.4 ± 0.2 % | 150 ± 38 | 648 ± 25 | 15.4 ± 0.6 | 0.0 ± 0.0 s |
| S2 Predictive-realistic (MA forecast) | 78.4 ± 0.9 % | 10780 ± 500 | 234 ± 13 | 13.8 ± 0.6 | 0.9 ± 0.0 s |
| S3 Reactive (trailing mean) | 78.4 ± 0.9 % | 10780 ± 500 | 234 ± 13 | 13.8 ± 0.6 | 0.9 ± 0.0 s |
| S4 Static N=max (SLA-optimal) | 100.0 ± 0.0 % | 0 ± 0 | 8503 ± 12 | 0.0 ± 0.0 | 0.0 ± 0.0 s |
| S4 Static N=cost-matched | 52.1 ± 0.2 % | 165825 ± 738 | 1602 ± 8 | 0.0 ± 0.0 | 2.4 ± 0.1 s |
| S5 Naive-threshold | 99.5 ± 0.1 % | 136 ± 36 | 4430 ± 36 | 14.0 ± 0.0 | 0.0 ± 0.0 s |

_Profile **diurnal**, n=8 seeds._

### Profile: ramp

| Strategy | SLA% | Unmet-RPS | Over-prov cost | #ScaleActions | Settling-s |
|---|---|---|---|---|---|
| S1 Predictive-oracle (upper bound) | 98.8 ± 0.7 % | 369 ± 213 | 941 ± 55 | 12.8 ± 2.1 | 0.0 ± 0.0 s |
| S2 Predictive-realistic (MA forecast) | 81.0 ± 0.7 % | 7070 ± 350 | 263 ± 8 | 8.0 ± 0.0 | 0.6 ± 0.0 s |
| S3 Reactive (trailing mean) | 81.0 ± 0.7 % | 7070 ± 350 | 263 ± 8 | 8.0 ± 0.0 | 0.6 ± 0.0 s |
| S4 Static N=max (SLA-optimal) | 100.0 ± 0.0 % | 0 ± 0 | 7759 ± 22 | 0.0 ± 0.0 | 0.0 ± 0.0 s |
| S4 Static N=cost-matched | 64.4 ± 0.3 % | 65364 ± 787 | 1557 ± 8 | 0.0 ± 0.0 | 1.5 ± 0.0 s |
| S5 Naive-threshold | 100.0 ± 0.0 % | 0 ± 0 | 3738 ± 58 | 4.0 ± 0.0 | 0.0 ± 0.0 s |

_Profile **ramp**, n=8 seeds._

### Profile: spike

| Strategy | SLA% | Unmet-RPS | Over-prov cost | #ScaleActions | Settling-s |
|---|---|---|---|---|---|
| S1 Predictive-oracle (upper bound) | 88.0 ± 0.0 % | 57447 ± 376 | 381 ± 0 | 9.0 ± 0.0 | 1.2 ± 0.1 s |
| S2 Predictive-realistic (MA forecast) | 88.0 ± 0.0 % | 67847 ± 376 | 485 ± 0 | 9.0 ± 0.0 | 1.2 ± 0.1 s |
| S3 Reactive (trailing mean) | 88.0 ± 0.0 % | 67847 ± 376 | 485 ± 0 | 9.0 ± 0.0 | 1.2 ± 0.1 s |
| S4 Static N=max (SLA-optimal) | 100.0 ± 0.0 % | 0 ± 0 | 11415 ± 7 | 0.0 ± 0.0 | 0.0 ± 0.0 s |
| S4 Static N=cost-matched | 88.0 ± 0.0 % | 86247 ± 376 | 1584 ± 0 | 0.0 ± 0.0 | 1.2 ± 0.1 s |
| S5 Naive-threshold | 88.5 ± 0.1 % | 44135 ± 386 | 3802 ± 194 | 5.9 ± 0.3 | 1.2 ± 0.1 s |

_Profile **spike**, n=8 seeds._

### Profile: sawtooth

| Strategy | SLA% | Unmet-RPS | Over-prov cost | #ScaleActions | Settling-s |
|---|---|---|---|---|---|
| S1 Predictive-oracle (upper bound) | 99.0 ± 1.4 % | 427 ± 782 | 1897 ± 34 | 23.8 ± 1.2 | 0.0 ± 0.1 s |
| S2 Predictive-realistic (MA forecast) | 62.2 ± 0.3 % | 25323 ± 459 | 1023 ± 7 | 22.0 ± 0.0 | 1.4 ± 0.0 s |
| S3 Reactive (trailing mean) | 62.2 ± 0.3 % | 25323 ± 459 | 1023 ± 7 | 22.0 ± 0.0 | 1.4 ± 0.0 s |
| S4 Static N=max (SLA-optimal) | 100.0 ± 0.0 % | 0 ± 0 | 7747 ± 18 | 0.0 ± 0.0 | 0.0 ± 0.0 s |
| S4 Static N=cost-matched | 64.5 ± 0.3 % | 65587 ± 813 | 1548 ± 10 | 0.0 ± 0.0 | 1.5 ± 0.0 s |
| S5 Naive-threshold | 100.0 ± 0.0 % | 0 ± 0 | 5166 ± 202 | 9.0 ± 1.3 | 0.0 ± 0.0 s |

_Profile **sawtooth**, n=8 seeds._

### Aggregate (all profiles)

| Strategy | SLA% | Unmet-RPS | Over-prov cost | #ScaleActions | Settling-s |
|---|---|---|---|---|---|
| S1 Predictive-oracle (upper bound) | 95.5 ± 1.5 % | 15912 ± 6712 | 1017 ± 141 | 14.6 ± 1.4 | 0.3 ± 0.1 s |
| S2 Predictive-realistic (MA forecast) | 77.2 ± 2.6 % | 29790 ± 6471 | 534 ± 88 | 14.7 ± 1.7 | 1.0 ± 0.1 s |
| S3 Reactive (trailing mean) | 77.2 ± 2.6 % | 29790 ± 6471 | 534 ± 88 | 14.7 ± 1.7 | 1.0 ± 0.1 s |
| S4 Static N=max (SLA-optimal) | 100.0 ± 0.0 % | 0 ± 0 | 7844 ± 764 | 0.0 ± 0.0 | 0.0 ± 0.0 s |
| S4 Static N=cost-matched | 68.9 ± 4.2 % | 89169 ± 14403 | 1355 ± 143 | 0.0 ± 0.0 | 1.5 ± 0.2 s |
| S5 Naive-threshold | 96.4 ± 1.4 % | 11559 ± 5033 | 4182 ± 271 | 6.8 ± 1.2 | 0.3 ± 0.1 s |

_Aggregate over all 6 profiles × 8 seeds._

### Read (aggregate)

- **S1 oracle vs S2 realistic (the cost of forecast error):** SLA 95.5% → 77.2% (18.3 pts lost); Unmet-RPS 15912 → 29790. The oracle keeps capacity ahead of a moving curve; the realistic forecaster cannot, and the gap is the entire unrealized value of forecasting on this rule.
- **S2 ≡ S3 (the forecaster carries no predictive lead):** the shipped moving-average engine sets `predicted_rps = mean(trailing window)` with no forward projection, so the value the autoscaler receives is identical to the reactive trailing-mean signal. S2 and S3 therefore coincide to the digit in every profile. The realistic predictive strategy is, on the current engine, reactive scaling wearing a forecast label — this is the central forecasting↔scaling finding: closing the S1→S2 gap needs a forecaster that actually extrapolates, not just averages.
- **SLA-vs-cost vs static-N:** Static N=max buys SLA 100.0% at over-prov cost 7844 instance-seconds (the cost-worst, SLA-optimal extreme); the dynamic predictive pool runs at over-prov cost 534 — roughly 15× cheaper — for 77.2% SLA. That is the core trade-off: SLA vs cost vs churn. See per-profile settling-s (spike/sawtooth) for where warm-up lead-time decides response speed.
