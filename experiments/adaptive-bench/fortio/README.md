# Fortio constant-QPS probe

A small open-loop load probe that lives **alongside** the Locust harness. It
does **not** replace Locust and is **not** wired into `run.py`. Its only job is
to validate the backend **saturation curve** and **tail latency** of the
closed-loop test backends (`test-backends/app.js`).

## Why this exists next to Locust

| | Locust (`../locust/locustfile.py`) | Fortio probe (this dir) |
|---|---|---|
| Loop model | **Closed-loop** — each user waits for its reply before the next request | **Open-loop** — fires at a fixed requested QPS regardless of latency |
| Answers | "How does the system behave for *N concurrent users*?" | "At a *fixed offered load*, what tail latency / shed rate results?" |
| Under overload | Effective rate drops as latency rises (users back off) | Rate held constant → queue builds → 503 once `QUEUE_MAX` is exceeded |
| Role here | The benchmark (5-phase shape, RQ4 / anomaly paths) | A smoke/diagnostic to read the saturation knee |

The closed-loop backend is an M/G/c queue: `WORKERS` service slots, a bounded
FIFO `QUEUE_MAX`, and a 503 shed past that. Only an open-loop generator can pin
the pool at a chosen arrival rate and let you read off where queue-wait inflates
the tail and where the LB starts shedding. That curve is what this probe prints.

## Prerequisites

The SmartLoad stack must be up (the probe hits the LB at `/`, the same path
Locust uses):

```bash
COMPOSE_PROJECT_NAME=smartload docker compose up -d
```

Fortio runs from its official Docker image by default — **nothing to install**.
Use `--local` only if you already have a `fortio` binary on PATH.

## Usage

```bash
# Saturation curve out of the box (five 10 s points, ~50 s total):
python fortio_probe.py

# One quick smoke at 200 QPS for 15 s:
python fortio_probe.py --qps 200 --duration 15s

# Custom sweep + keep fortio's raw JSON per point:
python fortio_probe.py --qps 100,300,600 --out ./results

# Use a local fortio binary against the published port (localhost:8080):
python fortio_probe.py --local
```

### Output

One row per offered-QPS point:

```
 offered   actual      p50      p90      p99    p99.9    2xx%    503%   errs
      50     49.8     21.3     28.1     41.0     55.2   100.0     0.0      0
     200    198.7     34.6     61.0    120.4    180.2   100.0     0.0      0
     400    372.1     88.0    210.5    480.7    690.0    96.4     3.6      0
     800    503.9    160.2    430.1    910.3   1320.0    71.2    28.8      0
```

* **offered** — requested QPS. **actual** — QPS fortio actually achieved (it
  falls below *offered* once the backends can't keep up — that gap is the
  ceiling).
* **p50…p99.9** — end-to-end latency in ms (queue-wait + service-time).
* **2xx% / 503%** — the edge split. Rising **503%** is the LB shedding once the
  per-replica `QUEUE_MAX` is exceeded — the saturation signal.
* **errs** — any non-200 that isn't a 503 (connection resets, timeouts).

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `--qps` | `50,100,200,400,800` | Comma-separated offered-QPS points; a single value is one smoke. |
| `--duration` | `10s` | Per-point window (fortio time string). |
| `--connections` | `64` | Connections fortio paces QPS across; must exceed `QPS × latency`. |
| `--target` | `http://load-balancer` (Docker) / `http://localhost:8080` (`--local`) | Override the LB URL. |
| `--local` | off | Use a `fortio` binary on PATH instead of the Docker image. |
| `--network` | `smartload_smartload-net` | Docker network to attach to (ignored with `--local`). |
| `--image` | `fortio/fortio:latest` | Fortio image (ignored with `--local`). |
| `--out` | — | Directory to dump fortio's raw JSON per point. |

A high `503%` is a **finding**, not a tool failure — the probe still exits `0`.
It exits non-zero only if a point produced no parseable fortio report at all.
