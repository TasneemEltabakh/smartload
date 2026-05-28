# Traffic Simulator

Locust-driven synthetic HTTP traffic against the SmartLoad load balancer. Used to
verify the telemetry pipeline records load end-to-end and to exercise routing
under sustained and bursty workloads.

## Layout

| File | Purpose |
|---|---|
| `locustfile.py` | One `SmartLoadUser` class — GET `/` with 0.1–1s think time |
| `Dockerfile` | Containerized Locust, defaults `--host=http://load-balancer` |

The `traffic-simulator` service in `docker-compose.yml` builds this directory and
exposes the Locust UI on port 8089.

## Quick start

Bring up the full stack from the repo root:

```bash
docker compose up --build -d
```

Open the Locust UI:

```
http://localhost:8089
```

`Host` is pre-filled with `http://load-balancer` (the compose service name).
Enter target users and spawn rate, then **Start**.

## Load profiles

The single user class is configurable in the UI. Drive sustained or bursty
load by tuning **Number of users** and **Spawn rate**.

| Profile | Users | Spawn rate | Approx. RPS | Notes |
|---|---|---|---|---|
| Smoke | 5 | 1 | 5–50 | Confirms the pipeline is wired |
| Sustained | 50 | 10 | 50–500 | Default for short demos |
| Heavy | 200 | 50 | 200–2000 | Saturates a single backend; scale with `--scale test-backend=3` |
| Burst | 500 | 500 | 500–5000 | High spawn rate = burst at t=0; latency p95 climbs visibly |

Endpoint hit: `GET /` on the load balancer, which round-robins to `test-backend`
containers. Add new tasks to `locustfile.py` to target other paths.

## Validating that telemetry reflects the load

While a swarm runs, watch the pipeline in three places:

1. **Telemetry stats** — `curl http://localhost:8081/api/v1/stats`
   `rows_written` should climb at roughly `3 × RPS` (three metrics per request:
   `request_count`, `request_latency_ms`, `error_rate`).
2. **Grafana overview** — `http://localhost:3000/d/smartload-overview` — request
   rate, p50/p95/max latency, and error-rate panels update every 5s.
3. **Shipper sidecar logs** — `docker compose logs -f lb-otel-shipper` — confirms
   the NGINX access log is being tailed and emitted as OTLP batches.

## Running Locust outside the compose stack

For ad-hoc runs against a host without the rest of the stack:

```bash
docker compose run --rm traffic-simulator
```

Or run Locust locally (requires `pip install locust`) against the published port:

```bash
locust -f locustfile.py --host=http://localhost:8080
```
