# Autoscaler strategy benchmark — REAL traces

The same strategies and controlled comparison as the synthetic benchmark (SUMMARY.md), replayed on real per-minute request traces. Sources:

- **azure** — Azure Functions 2019 (PRIMARY, CC-BY)
- **worldcup** — FIFA World Cup 1998 (flash crowds, CC-BY-4.0)
- **alibaba** — Alibaba Cluster 2018 (PROXY: instances/min, academic terms)

_Real-trace demand, normalized so each window's peak = 8×capacity = 800 rps. Window = 30 min upsampled minute→second; only the shape is real, the scale is normalized so every profile grades the same pool. per-instance cap = 100 rps, warm-up w = 20 s, cooldown = 60 s, seeds = [1000, 1001, 1002, 1003, 1004, 1005, 1006, 1007] (n=8; each seed = a different real window). Cells: mean ± 95% t-CI._

### Source: azure

| Strategy | SLA% | Unmet-RPS | Over-prov cost | #ScaleActions | Settling-s |
|---|---|---|---|---|---|
| S1 Predictive-oracle (upper bound) | 99.2 ± 0.6 % | 141 ± 200 | 24 ± 19 | 6.9 ± 2.9 | n/a |
| S2 Predictive-realistic (MA forecast) | 92.1 ± 3.4 % | 2977 ± 1802 | 94 ± 41 | 5.8 ± 2.6 | n/a |
| S3 Reactive (trailing mean) | 92.1 ± 3.4 % | 2977 ± 1802 | 94 ± 41 | 5.8 ± 2.6 | n/a |
| S4 Static N=max (SLA-optimal) | 100.0 ± 0.0 % | 0 ± 0 | 4588 ± 375 | 0.0 ± 0.0 | n/a |
| S4 Static N=cost-matched | 90.2 ± 11.5 % | 7201 ± 9173 | 491 ± 447 | 0.0 ± 0.0 | n/a |
| S5 Naive-threshold | 99.9 ± 0.3 % | 89 ± 209 | 4264 ± 373 | 2.4 ± 0.4 | n/a |
| C1 Controller + oracle (new upper bound) | 99.9 ± 0.3 % | 89 ± 209 | 2923 ± 252 | 3.4 ± 1.0 | n/a |
| C2 Controller + MA forecast | 99.9 ± 0.3 % | 89 ± 209 | 2667 ± 434 | 2.5 ± 0.9 | n/a |
| C3 Controller + reactive (trailing mean) | 99.9 ± 0.3 % | 89 ± 209 | 2667 ± 434 | 2.5 ± 0.9 | n/a |
| C4 Controller + trend forecast | 99.9 ± 0.3 % | 89 ± 209 | 2890 ± 262 | 3.4 ± 1.0 | n/a |
| C5 Controller + calibrated-noise forecast | 99.7 ± 0.3 % | 306 ± 296 | 3158 ± 207 | 54.9 ± 2.1 | n/a |
| C6 Sqrt-staffing + trend forecast | 99.9 ± 0.3 % | 89 ± 209 | 4497 ± 359 | 2.1 ± 1.5 | n/a |

_Real source **azure** (Azure Functions 2019 (PRIMARY, CC-BY)), n=8 windows._

### Source: worldcup

| Strategy | SLA% | Unmet-RPS | Over-prov cost | #ScaleActions | Settling-s |
|---|---|---|---|---|---|
| S1 Predictive-oracle (upper bound) | 99.0 ± 0.6 % | 477 ± 387 | 3 ± 1 | 4.4 ± 2.0 | n/a |
| S2 Predictive-realistic (MA forecast) | 96.4 ± 2.6 % | 824 ± 393 | 80 ± 46 | 4.4 ± 2.0 | n/a |
| S3 Reactive (trailing mean) | 96.4 ± 2.6 % | 824 ± 393 | 80 ± 46 | 4.4 ± 2.0 | n/a |
| S4 Static N=max (SLA-optimal) | 100.0 ± 0.0 % | 0 ± 0 | 5165 ± 1053 | 0.0 ± 0.0 | n/a |
| S4 Static N=cost-matched | 65.8 ± 24.8 % | 49224 ± 41500 | 740 ± 390 | 0.0 ± 0.0 | n/a |
| S5 Naive-threshold | 99.4 ± 0.5 % | 435 ± 415 | 4541 ± 805 | 3.4 ± 1.7 | n/a |
| C1 Controller + oracle (new upper bound) | 99.4 ± 0.5 % | 435 ± 415 | 2566 ± 384 | 5.4 ± 2.4 | n/a |
| C2 Controller + MA forecast | 99.4 ± 0.5 % | 435 ± 415 | 2648 ± 416 | 5.2 ± 2.5 | n/a |
| C3 Controller + reactive (trailing mean) | 99.4 ± 0.5 % | 435 ± 415 | 2648 ± 416 | 5.2 ± 2.5 | n/a |
| C4 Controller + trend forecast | 99.4 ± 0.5 % | 435 ± 415 | 2535 ± 390 | 5.4 ± 2.4 | n/a |
| C5 Controller + calibrated-noise forecast | 99.4 ± 0.5 % | 748 ± 683 | 2742 ± 193 | 50.2 ± 3.8 | n/a |
| C6 Sqrt-staffing + trend forecast | 99.4 ± 0.5 % | 435 ± 415 | 3886 ± 184 | 3.9 ± 2.3 | n/a |

_Real source **worldcup** (FIFA World Cup 1998 (flash crowds, CC-BY-4.0)), n=8 windows._

### Source: alibaba

| Strategy | SLA% | Unmet-RPS | Over-prov cost | #ScaleActions | Settling-s |
|---|---|---|---|---|---|
| S1 Predictive-oracle (upper bound) | 90.7 ± 2.8 % | 31503 ± 6377 | 238 ± 104 | 13.0 ± 4.0 | n/a |
| S2 Predictive-realistic (MA forecast) | 84.2 ± 5.2 % | 45481 ± 11436 | 387 ± 97 | 12.5 ± 3.5 | n/a |
| S3 Reactive (trailing mean) | 84.2 ± 5.2 % | 45481 ± 11436 | 387 ± 97 | 12.5 ± 3.5 | n/a |
| S4 Static N=max (SLA-optimal) | 100.0 ± 0.0 % | 0 ± 0 | 15088 ± 517 | 0.0 ± 0.0 | n/a |
| S4 Static N=cost-matched | 84.8 ± 4.5 % | 52988 ± 12034 | 476 ± 499 | 0.0 ± 0.0 | n/a |
| S5 Naive-threshold | 90.4 ± 3.2 % | 31943 ± 8297 | 766 ± 394 | 14.4 ± 4.0 | n/a |
| C1 Controller + oracle (new upper bound) | 99.7 ± 0.4 % | 671 ± 1326 | 2443 ± 468 | 28.0 ± 5.9 | n/a |
| C2 Controller + MA forecast | 89.7 ± 2.7 % | 27733 ± 7639 | 1642 ± 262 | 22.2 ± 4.6 | n/a |
| C3 Controller + reactive (trailing mean) | 89.7 ± 2.7 % | 27733 ± 7639 | 1642 ± 262 | 22.2 ± 4.6 | n/a |
| C4 Controller + trend forecast | 94.3 ± 1.6 % | 7727 ± 2473 | 2321 ± 445 | 29.2 ± 6.8 | n/a |
| C5 Controller + calibrated-noise forecast | 99.1 ± 0.9 % | 1176 ± 2007 | 2702 ± 686 | 35.5 ± 8.7 | n/a |
| C6 Sqrt-staffing + trend forecast | 98.2 ± 0.6 % | 1895 ± 1183 | 3262 ± 521 | 33.6 ± 5.1 | n/a |

_Real source **alibaba** (Alibaba Cluster 2018 (PROXY: instances/min, academic terms)), n=8 windows._

### Aggregate (all real sources)

| Strategy | SLA% | Unmet-RPS | Over-prov cost | #ScaleActions | Settling-s |
|---|---|---|---|---|---|
| S1 Predictive-oracle (upper bound) | 96.3 ± 1.9 % | 10707 ± 6588 | 88 ± 55 | 8.1 ± 2.2 | n/a |
| S2 Predictive-realistic (MA forecast) | 90.9 ± 2.9 % | 16427 ± 9439 | 187 ± 69 | 7.5 ± 2.0 | n/a |
| S3 Reactive (trailing mean) | 90.9 ± 2.9 % | 16427 ± 9439 | 187 ± 69 | 7.5 ± 2.0 | n/a |
| S4 Static N=max (SLA-optimal) | 100.0 ± 0.0 % | 0 ± 0 | 8281 ± 2107 | 0.0 ± 0.0 | n/a |
| S4 Static N=cost-matched | 80.3 ± 8.9 % | 36471 ± 15220 | 569 ± 222 | 0.0 ± 0.0 | n/a |
| S5 Naive-threshold | 96.6 ± 2.1 % | 10822 ± 6846 | 3190 ± 789 | 6.7 ± 2.6 | n/a |
| C1 Controller + oracle (new upper bound) | 99.7 ± 0.2 % | 398 ± 405 | 2644 ± 203 | 12.2 ± 5.1 | n/a |
| C2 Controller + MA forecast | 96.3 ± 2.2 % | 9419 ± 5979 | 2319 ± 276 | 10.0 ± 4.0 | n/a |
| C3 Controller + reactive (trailing mean) | 96.3 ± 2.2 % | 9419 ± 5979 | 2319 ± 276 | 10.0 ± 4.0 | n/a |
| C4 Controller + trend forecast | 97.9 ± 1.2 % | 2750 ± 1673 | 2582 ± 207 | 12.7 ± 5.5 | n/a |
| C5 Controller + calibrated-noise forecast | 99.4 ± 0.3 % | 743 ± 616 | 2867 ± 225 | 46.9 ± 4.5 | n/a |
| C6 Sqrt-staffing + trend forecast | 99.2 ± 0.4 % | 806 ± 489 | 3881 ± 285 | 13.2 ± 6.4 | n/a |

_Aggregate over all 3 real sources × 8 windows._
