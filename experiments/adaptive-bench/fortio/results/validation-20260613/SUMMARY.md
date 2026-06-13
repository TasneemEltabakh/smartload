# Closed-loop backend — end-to-end validation (2026-06-13)

Branch `feat/closed-loop-backend-sim` @ v1.0.7ba. Stack: full SmartLoad on one
Docker Desktop VM (Windows). Load via the new Fortio open-loop probe.

## Questions
1. Is the benchmark now **realistic** (does backend latency depend on load)?
2. Can it **distinguish routing policies**, and has **RL (PPO) earned promotion
   from shadow mode**?

## Method
- Rebuilt + deployed the closed-loop `test-backend` image (5 replicas, healthy
  on the new unpooled `/health`). Confirmed new code live via `/_admin/stats`.
- Froze the autoscaler to hold the pool at 5.
- Drove fixed-QPS open-loop load at the LB; measured LB-observed latency +
  success rate (Fortio) and per-backend request distribution (`/_admin/stats`).
- Routing policy varied by NGINX upstream weights: **PPO-shape** = the exact
  70/8/8/8/8 weighting the live `RL_POLICY=ppo, RL_MODE=active` engine emits;
  **RR** = even 20/20/20/20/20.

## Key facts established
- **Running stack had `RL_POLICY=ppo, RL_MODE=active`.** PPO's live output is a
  **stable, degenerate concentration**: weight **70** on one arbitrary backend
  (backend-1), 8 on the rest. Never changed across 0→900 QPS.
- **Measurement confound (host):** a *direct* single-backend probe at 50 QPS
  showed **84 ms avg** vs the 20 ms modelled mean → single-host CPU contention
  inflates latency ~4× and caps real pool throughput at ~120–150 rps (not the
  nominal 500). Absolute latency/throughput here are **not** publication-grade;
  distribution + success-rate + relative latency are robust.

## Evidence

### A. Saturation sweep (10 s/point, -c 200) — success rate is robust
| QPS | PPO 2xx% | PPO errs | RR 2xx% | RR errs |
|----:|---------:|---------:|--------:|--------:|
| 100 | 100 | 0 | 100 | 0 |
| 300 | 85 | 445 | 100 | 0 |
| 500 | 21 | 3943 | 100 | 0 |
| 700 | 502-abort | — | 49 | 3575 |

### B. Clean-regime paired test @ 60 QPS (within host capacity → both 100% success, latency comparable)
| Arm | distribution | p50 ms | p99 ms | 2xx% |
|-----|--------------|-------:|-------:|-----:|
| PPO-shape rep1 | **b1=69%**, rest 8% | 158 | 677 | 100 |
| PPO-shape rep2 | **b1=69%**, rest 8% | 159 | 652 | 100 |
| RR-even rep1 | 20% each | 88 | 244 | 100 |
| RR-even rep2 | 20% each | 85 | 238 | 100 |

PPO concentration costs **~1.8× p50, ~2.7× p99** vs trivial round-robin,
reproducibly. With the *old flat* backend this gap would be **0** (concentration
is free when requests don't share a queue).

### C. Fairness test — does PPO react to a degraded backend?
Injected +250 ms on backend-1 (PPO's favourite). Within ~20 s PPO's weight on
backend-1 dropped **70 → 1** via the routing path (`routing applied,
confidence=0.700`; no anomaly *exclude* fired). So PPO **is** latency-reactive —
it steers away from a backend once it degrades. But its reaction just moves the
70% concentration to a *different* backend; it never learns to spread.

## Verdict
- **Q1 realistic?** Yes, qualitatively — backend latency now rises with load,
  queues form, and overload sheds/fails. Bad routing now has a real cost. The
  single-host measurement environment is too CPU-contended for precise numbers;
  a dedicated/quiet host (or CPU-pinned, fewer co-located services) is needed
  for publication-grade deltas.
- **Q2 distinguishes policies?** Yes. The realistic backend turned PPO from
  *looks RR-equivalent* (the flat/sim world the prior audit used) into
  *measurably ~2× worse than RR*. The instrument now separates good from bad
  routing; it did not before.
- **Has RL earned promotion?** **No.** In the common homogeneous case PPO is
  ~2× worse than trivial round-robin (it concentrates instead of spreading). Its
  one redeeming behaviour — backing off a degraded backend — duplicates what the
  anomaly-detector, NGINX `max_fails`, and a classical least-connections router
  already do. Net: keep RL in **shadow**; default routing to a classical policy.

## Caveats / not cherry-picked
- One RR point at 500 QPS wobbled to 39% (rep2) — reported, not hidden: it shows
  500 QPS is past this host's real capacity. The clean conclusion uses the 60 QPS
  in-capacity regime.
- PPO's reaction to degradation (test C) is real and reported even though it
  does not change the promotion verdict.
- Single sweep + 2 paired reps per arm; effect sizes are large but n is small.

## Artifacts in this directory
`raw-*/fortio_qps*.json` (raw Fortio reports), `ppo60-rep*.json` / `rr60-rep*.json`
(per-arm distribution + LB metrics), `run_arm.py` / `snap_backends.py` (helpers).
