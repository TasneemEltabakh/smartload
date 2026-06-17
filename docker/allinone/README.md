# SmartLoad — all-in-one container

A single image that runs the **entire** SmartLoad stack — TimescaleDB, Redis,
the OpenTelemetry Collector, Prometheus, Grafana, `redis_exporter`, the NGINX
load balancer, the five Node test backends, every SmartLoad Python service, and
the operator + demo UIs — under one `supervisord`.

It is a packaging convenience for demos / single-host deployment, **not** a
replacement for `docker-compose.yml` (which remains the canonical multi-container
topology and what CI tests against).

## Run

```bash
docker run --rm -p 8080:80 -p 8090:8090 -p 3000:3000 \
  ghcr.io/tasneemeltabakh/smartload-allinone:latest
```

Then open:

| URL                        | What                                         |
|----------------------------|----------------------------------------------|
| http://localhost:8080      | NGINX load balancer (container port 80)      |
| http://localhost:8090      | Operator UI                                  |
| http://localhost:8091      | Demo UI                                       |
| http://localhost:3000      | Grafana (admin / `admin`)                    |
| http://localhost:9090      | Prometheus                                    |
| http://localhost:8081-8087 | SmartLoad service APIs (`/health`, `/metrics`)|

Publish whichever ports you need (or `-P` for all `EXPOSE`d). First boot spends
a few extra seconds initialising the database before services go healthy.

### Persisting data

The Postgres cluster lives at `/var/lib/postgresql/data`. Mount a volume there
to keep telemetry/forecasts/policy history across `docker run`s:

```bash
docker run -p 8080:80 -p 8090:8090 -p 3000:3000 \
  -v smartload-pgdata:/var/lib/postgresql/data \
  ghcr.io/tasneemeltabakh/smartload-allinone:latest
```

### Credentials / tuning

`TIMESCALEDB_PASSWORD` (default `changeme`) and `GRAFANA_PASSWORD` (default
`admin`) can be overridden with `-e`. The Postgres password is only applied on
first boot (when the cluster is created).

## How it works

* **Networking** — the entrypoint appends `/etc/hosts` aliases mapping every
  compose service name (`timescaledb`, `redis`, `otel-collector`, `telemetry`,
  …) to `127.0.0.1`, so the unmodified service configs and env resolve to the
  in-container processes.
* **Process management** — `docker/allinone/supervisord.conf` defines one
  program per compose service, mirroring its env, with startup priorities
  (data layer → observability → services → UIs). All logs stream to the
  container stdout (`docker logs`).
* **Backends** — the five `test-backend` replicas run as in-container Node
  processes on `127.0.0.1:9001-9005`; `nginx.conf` round-robins across them.

## Differences from the compose stack

Two compose features fundamentally depend on the Docker daemon and are
neutralised in the single container:

1. **Autoscaler container provisioning** — `AUTOSCALER_PROVISIONING_ENABLED=false`.
   The autoscaler still makes scaling *decisions* and publishes them on
   `smartload.scale`; it just can't spawn/destroy sibling containers.
2. **Dynamic NGINX weighting** — the `lb-sidecar` reloads NGINX via
   `docker exec`, which isn't available here, so routing is **static
   round-robin** over the five backends. The sidecar still runs, consumes the
   Redis control bus, and reports health; its computed upstream is written to a
   throwaway path.
3. **Host-resource collection** — the `resource-collector` ships per-container
   CPU/memory from the Docker stats API. With no daemon and no sibling
   containers it has nothing to collect, so it idles. Bind-mount the host socket
   (`-v /var/run/docker.sock:/var/run/docker.sock:ro`) to activate it.

Everything else — the full telemetry → TimescaleDB → engines → Redis control
bus → UIs/Grafana pipeline — runs exactly as in compose.

## Build locally

```bash
# from the repo root
docker build -f Dockerfile.allinone -t smartload-allinone .
```
