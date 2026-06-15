# `public/results/` — the data seam

The presentation UI reads **all** of its numbers from this folder.

- **`results.json`** — the active bundle the app loads (`/results/results.json`).
  Currently the *stale / pre-VPS sample* (real numbers from the last local run,
  flagged `kind: "stale"`). **Replace this with the VPS output to go live.**
- **`results.pending.json`** — the finalized structure with every value `null`;
  what the UI renders before any run lands. Copy over `results.json` to preview
  the empty state.

To point at an endpoint instead of this file, build/run with
`VITE_RESULTS_URL=https://host/results.json`.

Full contract, shape, and the gaps the harness must fill:
see [`../../RESULTS_INJECTION_GUIDE.md`](../../RESULTS_INJECTION_GUIDE.md).
