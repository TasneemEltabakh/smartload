# RL Routing Benchmark — SmartLoad

Run tag: `20260614T004702Z`  |  Total runtime: 754.4s

Closed-loop causal M/G/c queue sim. 5 disjoint seed-bands x 40 episodes x 128 windows. Cells are **mean ± 95% CI** across seed-bands (normal approx, `mean ± 1.96·std/√5`). SLA threshold: served latency > **200 ms**. Primary ranking key: **p95 served latency** and **SLA-violation %** (lower is better). Reward is diagnostic only. A result inside another's 95% CI is a tie, not a win.

Latencies in ms. `SLA%` = % of windows with served latency > threshold. `shed%` = mean shed fraction. `HHI` = mean routing-weight Herfindahl (1/N≈0.2 = perfectly even, 1.0 = all on one backend). **Bold** p95 cell marks the per-scenario leader and any statistical tie with it.

## Per-scenario results

### Homogeneous

| contender | type | p95 | SLA% | p50 | p99 | mean | shed% | reward* | HHI |
|---|---|---|---|---|---|---|---|---|---|
| policy.zip (shipped MaskablePPO) | rl | **453.1 ± 3.7** | 67.5 ± 5.3 | 318.0 ± 15.5 | 466.7 ± 0.6 | 262.0 ± 18.5 | 20.6 ± 2.1 | -3.639 ± 0.271 | 0.517 ± 0.000 |
| candidate_v2 (PPO) | rl | **417.3 ± 23.1** | 14.2 ± 2.3 | 24.0 ± 0.6 | 578.5 ± 40.5 | 77.3 ± 9.3 | 4.7 ± 0.9 | -0.986 ± 0.130 | 0.231 ± 0.005 |
| candidate_a2c (A2C) | rl | 652.3 ± 15.1 | 19.4 ± 4.0 | 24.0 ± 0.6 | 660.0 ± 0.0 | 121.9 ± 19.7 | 6.8 ± 1.3 | -1.396 ± 0.227 | 0.243 ± 0.008 |
| candidate_sac (SAC) | rl | 659.9 ± 0.1 | 13.4 ± 2.6 | 25.0 ± 0.5 | 660.0 ± 0.0 | 102.4 ± 16.1 | 5.5 ± 1.2 | -1.168 ± 0.186 | 0.307 ± 0.020 |
| candidate_dqn (DQN) | rl | **375.6 ± 241.4** | 5.4 ± 1.4 | 23.8 ± 0.6 | 660.0 ± 0.0 | 62.6 ± 8.2 | 2.1 ± 0.6 | -0.641 ± 0.092 | 0.218 ± 0.005 |
| round_robin | cls | **511.6 ± 6.0** | 32.5 ± 6.3 | 28.4 ± 1.7 | 523.7 ± 1.8 | 165.4 ± 26.5 | 8.6 ± 1.5 | -1.941 ± 0.308 | 0.282 ± 0.007 |
| least_connections | cls | **511.6 ± 6.0** | 32.5 ± 6.3 | 28.4 ± 1.7 | 523.7 ± 1.8 | 165.4 ± 26.5 | 8.6 ± 1.5 | -1.941 ± 0.308 | 0.282 ± 0.007 |
| random_shadow | cls | 648.8 ± 16.4 | 35.3 ± 5.8 | 30.5 ± 2.8 | 660.0 ± 0.0 | 186.1 ± 26.6 | 9.9 ± 1.7 | -2.161 ± 0.309 | 0.310 ± 0.008 |

Leader on p95: **candidate_dqn (DQN)** (tie set: candidate_dqn (DQN), candidate_v2 (PPO), least_connections, policy.zip (shipped MaskablePPO), round_robin).

### Heterogeneous

| contender | type | p95 | SLA% | p50 | p99 | mean | shed% | reward* | HHI |
|---|---|---|---|---|---|---|---|---|---|
| policy.zip (shipped MaskablePPO) | rl | 736.0 ± 17.1 | 60.7 ± 6.4 | 350.0 ± 45.4 | 882.9 ± 35.5 | 328.0 ± 29.6 | 21.2 ± 2.4 | -4.431 ± 0.411 | 0.517 ± 0.001 |
| candidate_v2 (PPO) | rl | **627.1 ± 57.6** | 18.5 ± 1.5 | 30.4 ± 0.7 | 934.0 ± 117.2 | 124.1 ± 12.6 | 6.3 ± 1.0 | -1.577 ± 0.161 | 0.273 ± 0.010 |
| candidate_a2c (A2C) | rl | 771.3 ± 68.8 | 22.5 ± 2.6 | 31.5 ± 0.4 | 1020.4 ± 98.2 | 164.1 ± 22.5 | 7.7 ± 1.2 | -1.981 ± 0.250 | 0.276 ± 0.009 |
| candidate_sac (SAC) | rl | **672.4 ± 65.9** | 23.3 ± 3.0 | 30.0 ± 1.0 | 916.4 ± 110.0 | 151.3 ± 20.5 | 10.0 ± 1.5 | -1.939 ± 0.238 | 0.565 ± 0.028 |
| candidate_dqn (DQN) | rl | 909.5 ± 105.4 | 23.0 ± 2.3 | 32.9 ± 0.5 | 1246.0 ± 62.7 | 183.3 ± 16.2 | 7.2 ± 1.2 | -2.059 ± 0.197 | 0.262 ± 0.011 |
| round_robin | cls | 823.9 ± 57.7 | 40.4 ± 4.8 | 57.3 ± 17.9 | 1025.3 ± 71.9 | 254.3 ± 29.2 | 8.9 ± 1.4 | -3.063 ± 0.351 | 0.289 ± 0.006 |
| least_connections | cls | 821.0 ± 53.7 | 42.3 ± 5.3 | 68.0 ± 26.4 | 967.2 ± 61.4 | 251.2 ± 34.2 | 10.5 ± 1.7 | -3.179 ± 0.415 | 0.284 ± 0.006 |
| random_shadow | cls | 860.3 ± 52.6 | 41.8 ± 5.1 | 65.3 ± 24.9 | 1105.2 ± 54.0 | 263.5 ± 29.8 | 10.6 ± 1.6 | -3.221 ± 0.378 | 0.313 ± 0.007 |

Leader on p95: **candidate_v2 (PPO)** (tie set: candidate_sac (SAC), candidate_v2 (PPO)).

### Degrading (1 backend)

| contender | type | p95 | SLA% | p50 | p99 | mean | shed% | reward* | HHI |
|---|---|---|---|---|---|---|---|---|---|
| policy.zip (shipped MaskablePPO) | rl | 1037.3 ± 51.3 | 61.3 ± 3.9 | 368.8 ± 25.0 | 2069.4 ± 651.5 | 409.5 ± 12.0 | 21.4 ± 2.2 | -7.098 ± 0.350 | 0.518 ± 0.000 |
| candidate_v2 (PPO) | rl | **811.9 ± 50.6** | 25.6 ± 5.8 | 32.7 ± 2.0 | 1240.9 ± 49.8 | 181.5 ± 31.5 | 8.9 ± 1.7 | -3.082 ± 0.500 | 0.295 ± 0.010 |
| candidate_a2c (A2C) | rl | 972.0 ± 72.4 | 29.0 ± 6.3 | 34.5 ± 2.2 | 1505.8 ± 212.8 | 233.4 ± 43.5 | 10.1 ± 2.1 | -3.937 ± 0.677 | 0.294 ± 0.011 |
| candidate_sac (SAC) | rl | **746.9 ± 60.5** | 28.6 ± 6.1 | 33.2 ± 3.0 | 1048.8 ± 37.7 | 193.0 ± 35.0 | 13.4 ± 2.5 | -2.624 ± 0.409 | 0.589 ± 0.020 |
| candidate_dqn (DQN) | rl | 986.5 ± 63.5 | 29.8 ± 5.2 | 35.5 ± 1.7 | 1254.5 ± 65.7 | 246.4 ± 43.3 | 10.9 ± 2.0 | -3.053 ± 0.467 | 0.310 ± 0.017 |
| round_robin | cls | 1057.7 ± 47.2 | 44.7 ± 4.8 | 118.2 ± 89.5 | 1833.2 ± 161.9 | 341.0 ± 33.5 | 11.3 ± 1.8 | -6.174 ± 0.530 | 0.297 ± 0.009 |
| least_connections | cls | 1045.7 ± 65.8 | 46.6 ± 4.6 | 152.5 ± 107.4 | 2009.6 ± 224.1 | 349.8 ± 28.6 | 13.4 ± 1.9 | -6.407 ± 0.515 | 0.292 ± 0.006 |
| random_shadow | cls | 1077.5 ± 59.3 | 46.1 ± 4.3 | 125.8 ± 94.7 | 2007.2 ± 240.9 | 347.1 ± 30.9 | 12.9 ± 1.8 | -6.234 ± 0.502 | 0.320 ± 0.007 |

Leader on p95: **candidate_sac (SAC)** (tie set: candidate_sac (SAC), candidate_v2 (PPO)).

### Near-idle

| contender | type | p95 | SLA% | p50 | p99 | mean | shed% | reward* | HHI |
|---|---|---|---|---|---|---|---|---|---|
| policy.zip (shipped MaskablePPO) | rl | 57.2 ± 4.0 | 0.8 ± 0.3 | 25.6 ± 2.8 | 146.0 ± 107.3 | 59.6 ± 9.9 | 0.3 ± 0.1 | -0.779 ± 0.092 | 0.513 ± 0.000 |
| candidate_v2 (PPO) | rl | 32.5 ± 1.6 | 0.1 ± 0.0 | 23.8 ± 0.9 | 38.6 ± 3.6 | 26.6 ± 0.6 | 0.0 ± 0.0 | -0.368 ± 0.024 | 0.216 ± 0.002 |
| candidate_a2c (A2C) | rl | 35.8 ± 1.5 | 0.1 ± 0.0 | 24.9 ± 0.9 | 43.4 ± 1.5 | 27.8 ± 0.9 | 0.0 ± 0.0 | -0.376 ± 0.028 | 0.211 ± 0.001 |
| candidate_sac (SAC) | rl | **28.4 ± 2.1** | 0.1 ± 0.0 | 19.8 ± 0.9 | 34.2 ± 4.4 | 25.9 ± 1.5 | 0.1 ± 0.0 | -0.505 ± 0.021 | 0.699 ± 0.014 |
| candidate_dqn (DQN) | rl | 34.6 ± 1.3 | 0.1 ± 0.0 | 25.9 ± 1.2 | 51.2 ± 14.9 | 28.1 ± 0.8 | 0.0 ± 0.0 | -0.374 ± 0.025 | 0.207 ± 0.002 |
| round_robin | cls | 85.4 ± 17.3 | 3.0 ± 0.4 | 26.1 ± 1.1 | 1972.7 ± 116.0 | 76.3 ± 7.4 | 0.1 ± 0.0 | -1.187 ± 0.172 | 0.245 ± 0.000 |
| least_connections | cls | 75.0 ± 18.5 | 3.2 ± 0.3 | 25.9 ± 1.2 | 2161.5 ± 149.0 | 86.6 ± 5.1 | 0.3 ± 0.1 | -1.322 ± 0.134 | 0.246 ± 0.000 |
| random_shadow | cls | 65.7 ± 14.7 | 2.7 ± 0.4 | 26.0 ± 1.1 | 2054.0 ± 122.6 | 76.1 ± 7.3 | 0.2 ± 0.0 | -1.164 ± 0.161 | 0.265 ± 0.001 |

Leader on p95: **candidate_sac (SAC)** (tie set: candidate_sac (SAC)).

### Held-out (dual-degrade, disjoint means)

| contender | type | p95 | SLA% | p50 | p99 | mean | shed% | reward* | HHI |
|---|---|---|---|---|---|---|---|---|---|
| policy.zip (shipped MaskablePPO) | rl | 2940.0 ± 139.0 | 71.2 ± 5.6 | 1110.4 ± 77.9 | 4558.4 ± 352.9 | 1138.1 ± 99.7 | 23.8 ± 2.0 | -14.500 ± 1.132 | 0.519 ± 0.001 |
| candidate_v2 (PPO) | rl | 2743.6 ± 96.6 | 42.9 ± 2.7 | 94.2 ± 9.9 | 3835.9 ± 202.7 | 828.4 ± 61.5 | 14.6 ± 1.5 | -10.030 ± 0.723 | 0.328 ± 0.007 |
| candidate_a2c (A2C) | rl | 2830.4 ± 84.1 | 45.0 ± 2.8 | 118.3 ± 37.7 | 4403.6 ± 539.0 | 901.5 ± 60.1 | 14.9 ± 1.6 | -11.442 ± 0.704 | 0.319 ± 0.007 |
| candidate_sac (SAC) | rl | **2420.6 ± 67.2** | 44.3 ± 2.1 | 106.0 ± 7.0 | 3086.4 ± 195.2 | 863.9 ± 48.1 | 18.9 ± 1.3 | -8.585 ± 0.458 | 0.439 ± 0.004 |
| candidate_dqn (DQN) | rl | 2737.7 ± 91.0 | 39.6 ± 2.2 | 96.0 ± 4.6 | 4669.6 ± 1565.5 | 915.7 ± 72.7 | 16.1 ± 1.7 | -9.499 ± 0.664 | 0.385 ± 0.014 |
| round_robin | cls | 3023.3 ± 107.0 | 51.0 ± 2.7 | 333.1 ± 226.1 | 4417.3 ± 375.3 | 972.7 ± 61.0 | 13.8 ± 1.4 | -12.612 ± 0.772 | 0.304 ± 0.005 |
| least_connections | cls | 2697.3 ± 57.6 | 50.2 ± 2.2 | 320.1 ± 254.7 | 4399.9 ± 127.4 | 929.8 ± 58.2 | 14.8 ± 1.3 | -12.469 ± 0.841 | 0.296 ± 0.004 |
| random_shadow | cls | 2990.4 ± 116.2 | 52.4 ± 3.1 | 421.4 ± 278.2 | 4399.1 ± 104.0 | 1008.9 ± 76.5 | 15.4 ± 1.5 | -12.784 ± 0.887 | 0.330 ± 0.006 |

Leader on p95: **candidate_sac (SAC)** (tie set: candidate_sac (SAC)).

## Overall (mean of per-scenario means)

Coarse roll-up: for each contender, the per-scenario mean of each metric is averaged across the 5 scenarios; CI is across those 5 scenario means. Per-scenario blocks above are authoritative — scenarios are not pooled at the window level.

| contender | type | p95 | SLA% | p50 | p99 | mean | shed% | reward* | HHI |
|---|---|---|---|---|---|---|---|---|---|
| policy.zip (shipped MaskablePPO) | rl | 1044.7 ± 981.2 | 52.3 ± 25.5 | 434.6 ± 353.1 | 1624.7 ± 1573.0 | 439.4 ± 360.6 | 17.5 ± 8.5 | -6.090 ± 4.570 | 0.517 ± 0.002 |
| candidate_v2 (PPO) | rl | 926.5 ± 925.9 | 20.3 ± 13.7 | 41.1 ± 26.3 | 1325.6 ± 1291.0 | 247.6 ± 289.0 | 6.9 ± 4.7 | -3.209 ± 3.457 | 0.269 ± 0.040 |
| candidate_a2c (A2C) | rl | 1052.4 ± 923.5 | 23.2 ± 14.2 | 46.6 ± 35.3 | 1526.6 ± 1485.2 | 289.7 ± 306.8 | 7.9 ± 4.8 | -3.826 ± 3.901 | 0.269 ± 0.037 |
| candidate_sac (SAC) | rl | 905.7 ± 784.6 | 21.9 ± 14.5 | 42.8 ± 31.3 | 1149.2 ± 1008.9 | 267.3 ± 297.4 | 9.6 ± 6.3 | -2.964 ± 2.842 | 0.520 ± 0.132 |
| candidate_dqn (DQN) | rl | 1008.8 ± 914.0 | 19.6 ± 14.5 | 42.8 ± 26.4 | 1576.3 ± 1576.9 | 287.2 ± 317.6 | 7.3 ± 5.7 | -3.125 ± 3.265 | 0.276 ± 0.064 |
| round_robin | cls | 1100.4 ± 994.9 | 34.3 ± 16.4 | 112.6 ± 112.8 | 1954.4 ± 1314.1 | 361.9 ± 311.5 | 8.5 ± 4.5 | -4.995 ± 4.087 | 0.283 ± 0.020 |
| least_connections | cls | 1030.1 ± 877.1 | 35.0 ± 16.6 | 119.0 ± 108.3 | 2012.4 ± 1317.1 | 356.6 ± 293.7 | 9.5 ± 5.0 | -5.064 ± 4.016 | 0.280 ± 0.017 |
| random_shadow | cls | 1128.5 ± 970.2 | 35.7 ± 17.1 | 133.8 ± 145.2 | 2045.1 ± 1266.1 | 376.3 ± 322.1 | 9.8 ± 5.1 | -5.113 ± 4.111 | 0.308 ± 0.022 |

*reward is diagnostic only (embeds the trainer's weightings); never used to rank.
