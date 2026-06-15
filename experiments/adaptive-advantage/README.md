# adaptive-advantage — a harder SmartLoad vs NGINX-RR benchmark

A well-rounded comparison built to expose the *real* differences between the full
SmartLoad decision plane and plain static NGINX round-robin — where the original
`baseline-vs-smartload` benchmark tied (its 50-user closed-loop load self-throttled
a slow backend, so nothing ever shed and the slowdown hid in the worst-case `max`).

## What it does differently

- **Load is driven past the queue knee** (`STEADY_USERS=70 > QUEUE_MAX=64`) so a
  *severely* degraded backend's queue overflows and **sheds 503**. NGINX runs
  `max_fails=0`, so plain RR can **never eject** it and keeps routing 1/N of traffic
  onto it; SmartLoad's error channel detects it and the sidecar pulls it out.
- **Organic detection** — anomalies are injected with `/_admin/delay` only (no
  manual `/isolate` hint), so SmartLoad's detector must find them itself.
- **A traffic spike** — to exercise autoscaling vs a static pool.
- **Two backends degrade at different times** (severe 503-shedding, then moderate
  slow) — testing the error channel, the latency channel, and continuous adaptation.

### 5-phase load shape (`locust/locustfile.py`)
| Phase | window | event |
|---|---|---|
| A_ramp    | 0–60s    | ramp to STEADY_USERS |
| B_degrade | 60–180s  | backend-1 +SEVERE_MS (organic 503-shed) |
| C_spike   | 180–240s | spike to SPIKE_USERS (backend-1 recovered) |
| D_slow    | 240–360s | backend-2 +MODERATE_MS (slow, not failing) |
| E_tail    | 360–420s | backend-2 recovered; settle |

### Two capacity scenarios
- **Equal-capacity (5v5):** both NGINX and SmartLoad limited to 5 backends
  (`MAX_BACKENDS=5`). SmartLoad can only **reroute**, not add servers — isolates
  pure routing/detection intelligence vs round-robin.
- **Full-system (10v5):** SmartLoad allowed to scale out (`MAX_BACKENDS=10`) vs a
  static 5-backend NGINX — shows total real-world value (adaptation + capacity).

### Run it
```bash
# equal-capacity (routing only)
MAX_BACKENDS=5 STEADY_USERS=70 SPIKE_USERS=110 SEVERE_MS=1800 MODERATE_MS=400 RUNS=2 \
  bash experiments/adaptive-advantage/run.sh
# full-system (scaling allowed)
MAX_BACKENDS=10 ... RUNS=2 bash experiments/adaptive-advantage/run.sh
# SHORT=1 for a ~2-min/side harness validation
python3 experiments/adaptive-advantage/compare.py <results/timestamp-dir>
```

## Results (2 runs each, clean load — per-phase error rate)

| Phase | 5v5 base | 5v5 SL | 10v5 base | 10v5 SL |
|---|---|---|---|---|
| A_ramp    | 0.0%  | 0.0%      | 0.0%  | 0.0% |
| **B_degrade** | **23.6%** | **1.6%** ✅ | **12.4%** | **1.0%** ✅ |
| C_spike   | 29.9% | **60.9%** ❌ | 32.3% | **55.9%** ❌ |
| D_slow    | 0.2%  | 0.8%      | 0.5%  | 1.1% |
| E_tail    | 0.0%  | 0.05%     | 0.0%  | 0.01% |
| **Overall** | 11.1% | 18.8% | 10.7% | 16.4% |

Tail latency in B_degrade (5v5): **baseline p99 = 60,000 ms** (requests stuck behind
the un-ejectable shedding backend) vs **SmartLoad p99 = 985 ms** — ~60× better.

Result batches on disk (kept for reference):
- `results/20260615T0953*` — first full run (over-saturated; superseded)
- the two clean 2-run batches referenced by `compare.py`

## Findings

1. **Decisive, real advantage — pure routing.** In **B_degrade, even at equal 5v5
   capacity**, SmartLoad cut errors **23.6% → 1.6%** and tail latency **60s → ~1s**.
   A backend that is *slow/shedding but never trips `max_fails`* is invisible to
   round-robin (which keeps feeding it 1/N of traffic); SmartLoad detects it
   organically and routes around it. This is the core thesis claim, cleanly proven,
   and it holds with or without scaling.

2. **The slow-backend phase (D_slow) ~ties** at this healthy load — a 400 ms
   slowdown is *latency*, not errors, and 70 users leaves enough slack to absorb it.

3. **The spike is a real SmartLoad weakness (under investigation).** SmartLoad
   *loses* C_spike in both scenarios (~61% vs ~30%). Because the 5v5 run cannot
   scale at all, the penalty there is **not** provisioning churn — under a sudden,
   *uniform* overload, SmartLoad's active routing is worse than dumb round-robin
   (RR's even split is optimal when every backend is equally swamped; the RL
   weighting + reactive churn distributes unevenly, so more queues overflow). In
   10v5, provisioning churn (the sidecar routing to backends before they are
   healthy) adds a second penalty (60s timeouts). This spike regression currently
   drags the aggregate negative despite the large B_degrade win.

## Honest bottom line

SmartLoad **decisively beats static NGINX at the failure mode it is built for** (a
hidden bad backend: ~15× fewer errors, ~60× better tail), but **its adaptation
currently back-fires under a sudden uniform-load spike**. Recovering the spike is
the open optimisation work (see the investigation log appended below as it lands).
