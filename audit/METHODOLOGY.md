# Benchmark methodology: the adaptive-advantage comparison

This document is the standalone methodology write-up for the canonical SmartLoad
benchmark, the `adaptive-advantage` comparison against plain NGINX round-robin. It
explains *why* the benchmark measures what it claims to measure: the load model, the
queue mathematics that makes a degraded backend observable, the five-phase shape and
the claim each phase isolates, the two capacity scenarios and the test-hygiene controls
that keep them honest, the organic anomaly-injection discipline, and a reproducibility
checklist. Facts here are sourced from
`experiments/adaptive-advantage/{README.md, locust/locustfile.py, run.sh, ABLATION.md}`,
`test-backends/{app.js, lib/config.js}`, `config/policy.yaml`, and `audit/REPORT.md`.

The benchmark answers one question: against a static round-robin load balancer of equal
capacity, what measurable value does SmartLoad's decision plane add, and where does it
not? The honest answer it produces is that SmartLoad's value is anomaly-driven exclusion
and capacity holding rather than fine-grained routing on a homogeneous pool. It
decisively beats round-robin at the failure modes it is built for and roughly ties under
a uniform spike where an even split is already optimal.

---

## 1. Closed-loop versus open-loop load, and why it decided the design

A load generator can drive a system in one of two regimes, and the choice silently
determines what failures the benchmark can ever observe.

**Closed-loop** load is the regime of a fixed population of virtual users, each of which
issues one request, waits for the response, thinks for a short interval, and only then
issues its next request. The number of requests in flight is therefore bounded by the
user count. This is exactly the model Locust implements: each `SmartLoadUser` holds at
most one outstanding request and waits `between(0.10, 0.20)` seconds between requests
(`locust/locustfile.py`).

**Open-loop** load is the regime of an arrival process that issues requests at a target
rate independent of how fast the system responds. Requests in flight are unbounded; if
the system slows, the backlog grows without limit.

The distinction matters because of a self-throttle that closed-loop load creates. When a
backend slows down, every user routed to it blocks on its slow response. Those users
stop generating new load while they wait, so the offered request rate falls on its own.
The slowdown converts into queue-wait latency, but the in-flight count is capped by the
user population, so nothing is ever shed. A slow backend stays hidden inside the
worst-case tail of the latency distribution and never produces a single error.

This is precisely the artifact that compromised the earlier `baseline-vs-smartload`
benchmark. That benchmark ran a 50-user closed-loop load. With 50 users the maximum
in-flight count was 50, which sat below the per-backend queue capacity (see Section 2),
so a degraded backend could only add latency and could never overflow its queue or shed.
SmartLoad and round-robin tied at 50 users not because they are equivalent, but because
the load profile had hidden the one failure mode where they differ. **That benchmark is
superseded.** Its tie is a closed-loop self-throttle artifact, not a result about the
systems under test.

The `adaptive-advantage` benchmark keeps the practical convenience of a closed-loop
generator but defeats the self-throttle by raising the user population past the queue
knee, so a degraded backend's queue genuinely overflows and sheds. The next section gives
the arithmetic.

---

## 2. The queue-knee mathematics

Each test backend is not a constant-delay echo server. It is a small bounded
finite-server queue, an M/G/c model in queueing terms (`test-backends/app.js`). Two
parameters define it (`test-backends/lib/config.js`):

- `WORKERS = 2`: the number of concurrent service slots. At most two requests are in
  service at once; the rest wait.
- `QUEUE_MAX = 64`: the depth of the bounded FIFO admission queue. Arrivals that find the
  queue full are shed immediately with HTTP 503.

The per-backend **admission ceiling** is therefore `WORKERS + QUEUE_MAX = 66`
simultaneous requests. The 67th concurrent request at a single backend is shed. A healthy
backend services roughly `per_instance_capacity_rps = 100` requests per second
(`config/policy.yaml`), consistent with two workers clearing requests at a few tens of
milliseconds each. Observed latency is queue-wait plus service-time end to end, so it
rises with utilisation, which is what makes a slowed backend visible to a latency-aware
detector before it ever sheds.

The knee is the load level at which a degraded backend crosses from "merely slow" to
"shedding 503". Under closed-loop load the standing population on any one backend is its
share of the total user count, but a severely degraded backend holds each request far
longer, so by Little's law its in-flight count climbs as residence time grows. The
controlling design choice is to keep the total concurrency above the queue depth:

```
STEADY_USERS = 70  >  QUEUE_MAX = 64
```

With the user population above 64, a backend that is driven into a severe slowdown
accumulates enough standing in-flight requests for its bounded queue to overflow, at
which point it sheds 503 organically. Below 64 (the old 50-user regime) this can never
happen: the population is smaller than the queue, so the queue cannot fill no matter how
slow the backend gets, and the slowdown stays buried in latency. Driving the load past
the knee is the single change that makes the decisive failure mode observable at all.

### Why round-robin can never eject the shedding backend

The NGINX baseline runs every upstream member with `max_fails=0`
(`services/load-balancer/nginx/conf.d/upstream.conf`). This is deliberate and is *not* a
handicap planted to make round-robin look bad. `max_fails=0` disables NGINX's passive
health-checking, which means NGINX never marks a backend as failed and never takes it out
of rotation. The audit confirmed this is the correct choice for the experiment
(`audit/REPORT.md`, section 3): it keeps a degraded backend producing honest 503
backpressure rather than letting passive ejection cascade into 502s, and it isolates the
variable under study. A backend that is *slow and shedding but never trips a failure
count* is exactly the adversary the benchmark is built around. Static round-robin keeps
routing one in N of all traffic onto it for the entire degradation window, because
round-robin has no signal that would tell it to stop. SmartLoad's error channel detects
the 503 shedding and its sidecar rewrites the upstream to route around the backend, so
the comparison measures detection-and-exclusion intelligence against a policy that
structurally cannot exclude.

---

## 3. The five-phase load shape and the claim each phase isolates

The load profile is a single `LoadTestShape` with five wall-clock phases
(`locust/locustfile.py`). Each phase is engineered to isolate one claim, so a per-phase
result table reads as a per-claim verdict. Backend anomalies are injected on a schedule
by the orchestrator (`run.sh`, `_schedule_anomalies`) timed to the phase boundaries.

| Phase | Window | Load | Injected event | Claim isolated |
|---|---|---|---|---|
| **A_ramp** | 0 to 60 s | ramp to `STEADY_USERS` | none | Baseline parity: with no anomaly both systems should be healthy and indistinguishable. Establishes the no-fault control. |
| **B_degrade** | 60 to 180 s | hold `STEADY_USERS` | backend-1 `+SEVERE_MS` | The core claim. A hidden 503-shedding backend, invisible to round-robin under `max_fails=0`, must be detected and excluded organically. |
| **C_spike** | 180 to 240 s | spike to `SPIKE_USERS` | backend-1 recovered | Behaviour under uniform overload. Tests scale-out in the full-system scenario and honest behaviour where an even split is already optimal. |
| **D_slow** | 240 to 360 s | back to `STEADY_USERS` | backend-2 `+MODERATE_MS` | The latency channel. A slow-but-not-failing backend must be re-routed around without an over-exclusion cascade. |
| **E_tail** | 360 to 420 s | hold `STEADY_USERS` | backend-2 recovered | Recovery and settling. After both faults clear, the pool must return to a healthy steady state with no lingering exclusions. |

Two backends degrade at different times and in different ways on purpose. B_degrade
exercises the **error channel** with a severe slowdown (`SEVERE_MS`, large enough to push
the backend past the knee and into 503 shedding). D_slow exercises the **latency channel**
with a moderate slowdown (`MODERATE_MS`, large enough to hurt tail latency but not large
enough to shed), with the recovered B-phase backend back in the pool. Separating them in
time lets each detection channel be scored independently, and the spike sits between them
so the system is tested on continuous adaptation rather than a single static fault.

A note on reading C_spike: a near-tie there, or round-robin slightly ahead, is the
expected and honest result, not a regression. On a homogeneous pool under uniform
overload an even split is provably optimal, so learned routing cannot beat round-robin
and SmartLoad's bounded routing skew costs a little. The earlier large C_spike collapse
seen in some batches was a coupled-failure and benchmark-contamination artifact (the
autoscaler flap stripping capacity mid-spike), not an inherent property; the
test-hygiene controls in Section 4 remove it (`audit/REPORT.md`; `README.md`).

---

## 4. Two capacity scenarios, the floor pin, and the per-side reset

The benchmark is run in two scenarios that answer two different questions, and both rely
on two test-hygiene controls. The controls are deliberately separated from the variable
under study so they cannot be read as result-gaming.

### 4.1 Equal-capacity (5v5) versus full-system (10v5)

**Equal-capacity (5v5).** Both sides are limited to five backends (`MAX_BACKENDS=5`).
SmartLoad cannot add servers; it can only re-route within the same budget the baseline
has. This scenario isolates **pure routing and detection intelligence** against
round-robin at matched capacity. It is the scenario the headline thesis claim rests on,
because any win cannot be attributed to extra hardware.

**Full-system (10v5).** SmartLoad may scale out to ten backends (`MAX_BACKENDS=10`) while
the NGINX baseline stays at a static five. This scenario shows **total real-world value**,
adaptation plus elastic capacity, and exercises the autoscaling path that the 5v5
scenario never triggers.

### 4.2 The `min_backends` pin (equal-capacity hygiene)

In the 5v5 scenario the pool floor is pinned with `MIN_BACKENDS=5`, so the pool is a fixed
five backends throughout the run (`run.sh`). This matches the baseline's static five and
the "SmartLoad can only re-route, not add servers" definition of the scenario. The pin is
necessary because, without it, the autoscaler is free to flap the pool between four and
five on a decoupled forecast signal, and that flap removes real capacity mid-load. That
flap is a separately documented defect (`audit/REPORT.md`, D1) and has nothing to do with
routing quality; leaving it in the 5v5 run would confound the pure-routing measurement and
would contaminate the **baseline** as well, since the flap was destroying its capacity too.
The pin is test hygiene that holds the capacity variable constant, not a tuning knob that
favours SmartLoad. The 10v5 scenario leaves `MIN_BACKENDS` at its default of 1 precisely
because scaling is the thing under study there.

### 4.3 The per-side routing reset (A/B hygiene)

Between the baseline side and the SmartLoad side of each run, the orchestrator rewrites
the NGINX upstream to a clean, all-up five-backend pool before the decision plane is
recreated (`run.sh`, `_reset_upstream`, gated by `RESET_UPSTREAM=1`). The harness already
resets backend *delays* between sides; the reset also clears the *routing* state. Without
it, a backend left `down;` by a prior side (an exclusion the decision plane never
re-included before the side ended, and which a sidecar restart re-imports from the stale
on-disk conf) would carry into the next side as lost capacity and bias the comparison.
Writing an identical clean pool before each side guarantees both sides start from the same
five-backend state. This is A/B hygiene that removes carryover between conditions, not a
manipulation of either condition's result.

Both controls have a measured cost in the ablation (Section 5): the pin and the reset each
account for a small, quantified contribution. They are reported as fixes with measured
effects, not hidden adjustments.

---

## 5. Organic anomaly injection: detection must be earned

Anomalies are injected through one channel only: a POST to each backend's `/_admin/delay`
endpoint, which adds milliseconds to that replica's service time
(`test-backends/app.js`; `run.sh`, `_delay`). There is deliberately **no** call to the
operator `/isolate` hint and no out-of-band signal telling the decision plane which
backend is sick or when.

This is the central fairness discipline of the benchmark. Injecting a slowdown is a
*physical* fault: the backend simply gets slower, exactly as a real overloaded or
degraded instance would. Whether and when SmartLoad notices is left entirely to its own
detectors reading live metrics. If the benchmark instead used `/isolate` to tell the
system which backend to exclude, it would be measuring the actuation plumbing, not the
detection intelligence, and the result would not transfer to production where no oracle
exists. By injecting organically, the benchmark forces SmartLoad to *earn* every
exclusion through detection, and it forces the detection latency (the time between the
fault appearing and the backend being excluded) into the measured error and tail-latency
numbers where it belongs. The anomaly timeline is scheduled relative to the phase
boundaries (`run.sh`, `_schedule_anomalies`): backend-1 is slowed shortly after the ramp
and recovered at the end of B_degrade; backend-2 is slowed at the start of D_slow and
recovered at its end.

---

## 6. Reproducibility checklist

The benchmark is scripted end to end. The following reproduces the canonical results.

### 6.1 Preconditions

- The full stack must be up under Compose project name `smartload` (hardcoded backend
  hostnames depend on it). From a worktree, force `COMPOSE_PROJECT_NAME=smartload`.
- A one-time Locust image build happens automatically on first run
  (`smartload-locust:latest`), with a fallback to `python:3.11-slim` plus `pip`.
- For the ablation only, bake the env-reading knobs into the service images first:
  `docker compose build lb-sidecar anomaly-detector` (`ABLATION.md`).

### 6.2 Commands

```bash
# Equal-capacity (5v5): pure routing/detection intelligence at matched capacity.
MIN_BACKENDS=5 MAX_BACKENDS=5 \
  STEADY_USERS=70 SPIKE_USERS=110 SEVERE_MS=1800 MODERATE_MS=400 \
  RUNS=3 bash experiments/adaptive-advantage/run.sh

# Full-system (10v5): adaptation plus elastic capacity (MIN_BACKENDS left at default 1).
MAX_BACKENDS=10 \
  STEADY_USERS=70 SPIKE_USERS=110 SEVERE_MS=1800 MODERATE_MS=400 \
  RUNS=3 bash experiments/adaptive-advantage/run.sh

# Fast harness validation (~2 min/side), not for reported numbers.
SHORT=1 bash experiments/adaptive-advantage/run.sh

# Per-fix contribution (leave-one-out ablation), 5v5.
RUNS=3 bash experiments/adaptive-advantage/ablation.sh

# Re-render the comparison for an existing batch.
python3 experiments/adaptive-advantage/compare.py experiments/adaptive-advantage/results/<TS>
```

### 6.3 Seeds and repetition

- The load is seeded. Each side receives `BENCH_SEED` and the locustfile calls
  `random.seed(BENCH_SEED)` (`locust/locustfile.py`), so the request stream is
  deterministic for a given seed.
- Run `k` uses seed `SEED_BASE + k - 1`, with `SEED_BASE = 1337` by default (`run.sh`).
  A three-run batch therefore exercises seeds 1337, 1338, 1339, and both sides of a given
  run share the same seed so the A/B comparison faces the identical request stream.
- Use **RUNS >= 3**. A single run is not reportable: the early run-1-good / run-2-collapsed
  episode showed one run can mislead entirely. The clean reference batch was three runs
  (`results/20260615T124519Z`).

### 6.4 Statistics and continuous integration

- Per-phase results should be reported as **mean with a 95% confidence interval** across
  the runs, with a significance flag where SmartLoad's interval is disjoint from the
  baseline's. This aggregation across `run-NN` directories is tracked in **issue #181**
  (`compare.py`, stdlib `statistics` only, single-run input degrades to `n/a` rather than
  crashing). A thesis-grade table requires RUNS >= 3 plus these intervals; the ablation's
  RUNS=2 deltas on the noisier phases are explicitly within run-to-run noise until re-run
  at RUNS >= 3 with intervals (`ABLATION.md`; `README.md`).

### 6.5 Artifacts

Each run writes per-side Locust CSV (`--csv`, `--csv-full-history`), an HTML report, a log,
pre/post status snapshots, and the scaling audit, under
`experiments/adaptive-advantage/results/<TS>/run-NN/<side>/` (`run.sh`). Result batches are
kept on disk for the before/after record; superseded batches (contaminated by the
autoscaler flap or stale `down;` carryover, before the pin and reset landed) are retained
and labelled as superseded rather than deleted, so the methodology's own correction is
auditable (`README.md`).
