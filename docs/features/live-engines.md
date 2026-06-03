# Live Engines

> **Slice status — fully shipped 2026-06-03 (#121, v1.0.7n).** Backend + UI (session 1, 2026-05-24), SDK + scenario + manifest (session 2 SDK leg, v1.0.7j), per-engine deep-dive page (v1.0.7k–m), embedded Grafana panels (v1.0.7l, #131 Phase 3), and the e2e suite (v1.0.7n) are all in `main`.

## What this slice delivers

Operators get a single live view of the three AI engines (anomaly-detector, forecasting, rl-engine) — what they're doing right now, what they decided last cycle, what policy they're reasoning under, and a colour-coded activity stream that updates in real time. Replaces "ssh in and grep container logs" with one always-on page.

## Customer surfaces

| Surface | Detail | Status |
|---|---|---|
| HTTP | `GET /api/v1/engine/state` on each of the three AI services (`8082`, `8083`, `8084`) — returns `{ engine, policy_snapshot, stats, last_output, rl_mode_env (rl only) }` | ✓ |
| BFF | `GET /api/ui/engines/snapshot` — parallel fan-out across the three AI services + ring snapshot + merged recent view | ✓ |
| BFF | `GET /api/ui/engines/stream` — SSE: replay-then-live, 15 s heartbeat comments | ✓ |
| Redis | Subscriber on `smartload.{anomaly,forecast,routing,scale}` from inside the BFF, into a per-channel `deque(maxlen=100)` | ✓ |
| UI | `/engines` — engine tiles with the headline ("what just happened") on the left, colour-coded activity feed on the right with channel-filter chips | ✓ |
| UI | `/engines/<service>` — per-engine deep-dive page (anomaly / forecasting / rl); engine block + stats + policy snapshot + full last_output + channel-filtered activity feed + Grafana/raw-state links (v1.0.7k); two embedded `/d-solo/` panels per engine (v1.0.7l) | ✓ |
| SDK | `client.engines.{snapshot, state, subscribe}` + top-level aliases (v1.0.7j) | ✓ |
| Webhook | not in scope — webhooks (#130) target external integrators; Live Engines is an operator-UI surface | n/a |

## Implementation pointers

- AI service endpoint: `services/<svc>/app.py::get_engine_state()` → `services/<svc>/runloop.py::serialize_engine_state()` (pure-Python, unit-testable)
- BFF aggregator: `services/operator-ui/bff/engines.py` — `EngineEventBus` thread-safe per-SSE-client fan-out; `collections.deque(maxlen=100)` per channel
- BFF routes: `services/operator-ui/bff/app.py::ui_engines_snapshot()` + `ui_engines_stream()`
- UI page: `services/operator-ui/web/src/pages/LiveEngines.tsx` — two-pane layout, right-slide Details drawer for per-engine raw last_output
- Envelope parsing: `services/shared/contracts.py::parse_envelope` (TTL drops surfaced as `dropped` counter, not user-visible noise)

## Status

- [x] Three AI services expose `/api/v1/engine/state` (anomaly, forecasting, rl-engine)
- [x] Pure-Python `serialize_engine_state` in each `runloop.py` with unit-test coverage
- [x] BFF `/api/ui/engines/snapshot` (parallel fan-out) + `/api/ui/engines/stream` (SSE) shipped
- [x] BFF Redis subscriber thread + `EngineEventBus` + bounded queues
- [x] Gunicorn `gthread` worker config so long-lived SSE doesn't pin a sync worker
- [x] UI `/engines` page with headline-led tiles + colour-coded activity feed + channel filters
- [x] BFF SPA fallback fix for direct `/engines` URLs (`static_url_path="/assets"`)
- [x] BFF Docker build context widened to `./services` so `shared/` is pulled in
- [x] OpenAPI fragments — `/api/v1/engine/state` per service + `/api/ui/engines/snapshot` + `/api/ui/engines/stream`
- [x] Unit tests — 100 module-level tests including 19 new BFF tests (#121 session 1)
- [x] Scenario script `examples/scenarios/live-engines/live_engines_walk.py` (this batch — closes the structural-lint orphan: `tests/e2e/live-engines/` existed without a sibling scenario)
- [x] Manifest `docs/features/live-engines.md` (this batch — closes the structural-lint orphan: `tests/e2e/live-engines/` existed without a sibling manifest)
- [x] SDK methods — `client.engines.snapshot()`, `client.engines.state(service)`, `client.engines.subscribe(callback, channels=...)` + convenience top-level aliases (`engines_snapshot`, `engines_state`, `subscribe_engines`, plus per-channel `subscribe_anomaly/forecast/routing/scale`) — landed v1.0.7j; 15 unit tests at `clients/python/tests/test_engines.py`; live-smoke verified against the running BFF.
- [x] Per-engine deep-dive page — `/engines/<service>` for `anomaly-detector` / `forecasting` / `rl-engine`. Header (name + status badge + loaded engine/policy + Grafana link + raw-state link) + four cards (run-loop stats, policy snapshot, last cycle output, channel-filtered activity feed). Tile names on `/engines` link to the new page. Landed v1.0.7k.
- [x] Embedded Grafana panels on the deep-dive page (#131 Phase 3) — `GF_SECURITY_ALLOW_EMBEDDING=true` + anonymous Viewer auth in `docker-compose.yml`; two `/d-solo/` iframes per engine (last 30 min, 10 s refresh, dark theme) on the "Live charts" card. Landed v1.0.7l. Follow-up: production same-origin `/grafana/*` proxy via BFF.
- [x] E2E test suite — `tests/e2e/live-engines/test_live_engines.py` (v1.0.7n, 2026-06-03; 17 tests across 6 classes covering state, snapshot, SSE delivery per channel, client-side channel filter, snapshot↔ring parity, subscription lifecycle; 17 passed in 56.27 s against the live compose stack)
- [x] §25.9 slice-catalog row flipped to *Shipped* (v1.0.7n, 2026-06-03)

## Non-goals

- Authentication on the SSE stream — the operator UI is single-tenant + assumed-trusted in Phase 1; Auth lands with #125 (Phase 2)
- Multi-tab fan-out optimisation beyond `bounded queue per SSE client` — current 100-element deque per channel is enough for the operator-UI cardinality (~5 channels × ~3 concurrent operators)
- Time-travel / scrubback — Live Engines is "now"; historical replay lives in the Audit page and Grafana

## How to verify (what ships today)

```bash
# Stack up with the default v1.0.7g flags
docker compose up -d

# UI
open http://localhost:8090/engines

# BFF endpoints direct
curl http://localhost:8090/api/ui/engines/snapshot | jq
curl -N http://localhost:8090/api/ui/engines/stream    # SSE — Ctrl+C to stop

# Per-engine raw state
curl http://localhost:8082/api/v1/engine/state | jq    # anomaly
curl http://localhost:8083/api/v1/engine/state | jq    # forecast
curl http://localhost:8084/api/v1/engine/state | jq    # rl

# Scenario walk
python examples/scenarios/live-engines/live_engines_walk.py
```
