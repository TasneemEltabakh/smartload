# Autoscaler SLA-vs-cost frontier

Over-provisioning cost (instance-seconds, lower=better) vs SLA% (higher=better) as the controller safety margin is swept. Each point is the mean over all 6 profiles × 8 seeds.

_Params: cap=100 rps, warm-up=20s, cooldown=60s, peak=8×cap, seeds=n8._

### Controller + trend (predictive)

| headroom | SLA% | Over-prov cost | #ScaleActions |
|---|---|---|---|
| 0.00 | 97.7 | 1070 | 17.4 |
| 0.05 | 98.9 | 1752 | 20.2 |
| 0.10 | 98.7 | 2032 | 16.8 |
| 0.15 | 99.2 | 2404 | 24.5 |
| 0.20 | 99.4 | 2773 | 16.3 |
| 0.30 | 99.4 | 3094 | 14.8 |
| 0.50 | 99.5 | 3712 | 21.2 |

### Controller + reactive

| headroom | SLA% | Over-prov cost | #ScaleActions |
|---|---|---|---|
| 0.00 | 87.3 | 855 | 10.4 |
| 0.05 | 93.4 | 1005 | 11.4 |
| 0.10 | 96.9 | 1311 | 11.2 |
| 0.15 | 98.3 | 2188 | 11.1 |
| 0.20 | 98.7 | 2473 | 11.6 |
| 0.30 | 98.9 | 2822 | 11.5 |
| 0.50 | 99.0 | 3531 | 10.1 |

### Controller + MA forecast

| headroom | SLA% | Over-prov cost | #ScaleActions |
|---|---|---|---|
| 0.00 | 87.3 | 855 | 10.4 |
| 0.05 | 93.4 | 1005 | 11.4 |
| 0.10 | 96.9 | 1311 | 11.2 |
| 0.15 | 98.3 | 2188 | 11.1 |
| 0.20 | 98.7 | 2473 | 11.6 |
| 0.30 | 98.9 | 2822 | 11.5 |
| 0.50 | 99.0 | 3531 | 10.1 |

### Sqrt-staffing + trend (swept β)

| β | SLA% | Over-prov cost | #ScaleActions |
|---|---|---|---|
| 0.5 | 99.1 | 2671 | 29.3 |
| 1.0 | 99.4 | 3649 | 38.0 |
| 1.5 | 99.4 | 4538 | 34.0 |
| 2.0 | 99.5 | 5266 | 24.4 |
| 3.0 | 99.8 | 6590 | 25.4 |

### Baseline anchors (fixed)

| Strategy | SLA% | Over-prov cost | #ScaleActions |
|---|---|---|---|
| S1 oracle (old rule ceiling) | 95.5 | 1017 | 14.6 |
| S2 predictive-MA (baseline) | 77.2 | 534 | 14.7 |
| S5 naive-threshold | 96.4 | 4182 | 6.8 |
| S4 static N=max | 100.0 | 7844 | 0.0 |

### Read

- At matched over-prov cost, the predictive (trend) controller averages **+1.09 SLA pts** vs the reactive controller across the swept range (positive = forecasting pays off at equal cost).
