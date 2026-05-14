# operator-ui / web

Frontend SPA for the operator UI.

## Status

Scaffolded only. Framework choice deferred (proposal: React + Vite + Recharts; finalise in #119).

## Pages (planned)

- Home (`#119`) — service health overview
- Policy (`#120`) — read, diff preview, commit
- Live Engines (`#121`) — real-time event stream
- Audit (`#122`) — policy + scaling change history
- Actions (`#123`) — manual scale / isolate
- Logs (`#124`) — service log viewer (optional)
- Auth (`#125`) — login + session
- Dashboards (`#131`) — embedded charts so the operator never leaves the UI

## Anti-pattern we explicitly reject

Wrapping Grafana in an iframe. Charts are first-class UI elements, fetched from the BFF, rendered via a single chosen chart library. See `#131`.
