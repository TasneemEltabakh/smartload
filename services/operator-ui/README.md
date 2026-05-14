# operator-ui

Operator-facing control surface. Lets a human see what the decision plane is doing, propose policy changes with diff preview, see audit history, and trigger manual actions (scale, isolate).

Per the SOT lock (commit `6f89a13`), this is a **transparency + override layer**, not an admin panel.

## Layout

- `bff/` — Flask BFF (backend-for-frontend). Aggregates `/health` from every service, proxies API calls, serves Swagger UI, holds session state if needed.
- `web/` — frontend (React or chosen stack). Pages:
  - Home — service health overview
  - Policy — read + diff-preview + commit
  - Live Engines — real-time anomaly / forecast / routing event stream
  - Audit — policy_changes + scaling_events table
  - Actions — manual scale / isolate
  - Dashboards — embedded charts (#131)
  - Logs — service log viewer (#124, optional)

## Status

Scaffolded only. Not yet in `docker-compose.yml`, does not run. Implementation lands across issues #119–#125, #131.

## Why it lives in services/

The operator UI is just another service in the deployment topology. Putting it anywhere else creates an inconsistent mental model.

## See also
- Issues: #119, #120, #121, #122, #123, #124, #125, #131
