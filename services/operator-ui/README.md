# operator-ui

Operator-facing control surface. Lets a human see what the decision plane is doing, propose policy changes with diff preview, see audit history, and trigger manual actions (scale, isolate).

Per the SOT lock (commit `6f89a13`), this is a **transparency + override layer**, not an admin panel.

## Layout

- `bff/` — Flask BFF (backend-for-frontend). Aggregates `/health` from every service, proxies API calls, serves Swagger UI (`/api/docs`, OpenAPI) and the AsyncAPI viewer (`/api/asyncapi-docs`, event contract), holds session state if needed.
- `web/` — frontend (React 18 + TypeScript, built with Vite). Pages:
  - Home — service health overview
  - Policy — read + diff-preview + commit
  - Live Engines — real-time anomaly / forecast / routing event stream (with an Engine Detail drill-down)
  - Audit — policy_changes + scaling_events table
  - Actions — manual scale / isolate
  - Dashboards — embedded charts (backlog, #131)
  - Logs — service log viewer (backlog, #124, optional)

## Status

**Running.** Built and wired into `docker-compose.yml` as the `operator-ui` service (port 8090): a multi-stage image bundling the Vite/React + TypeScript frontend with the Flask BFF runtime. The BFF fans out `/health` across the control plane, proxies the policy / actions / audit APIs, exposes an SSE activity stream, and serves the consolidated `/api/v1/status` aggregate. The web app ships the Home, Policy, Live Engines (+ Engine Detail), Audit, and Actions pages (#119–#123, #121 live-engines fully shipped). Remaining on the backlog: embedded dashboards (#131) and the log viewer (#124).

## Why it lives in services/

The operator UI is just another service in the deployment topology. Putting it anywhere else creates an inconsistent mental model.

## See also
- Issues: #119, #120, #121, #122, #123, #124, #125, #131
