# Results Injection Guide (schema v2)

This UI is **strictly read-only**. It presents already-computed benchmark and
audit comparisons across **systems × parameter configurations × metrics**; it
never triggers, schedules, or simulates a run. Every number on every surface
flows through **one file**, and the *list of suites is itself part of the data* —
so when the VPS re-run finishes (with however many suites), you update only the
data and touch no component.

---

## 1. The one place numbers come from

```
tools/demo-ui/web/public/results/results.json      ← the live data seam (active)
```

Served at `/results/results.json`. To inject the finished VPS run, do **one** of:

1. **Drop the file in.** Overwrite `public/results/results.json` with the VPS
   output (same shape — see §3) and rebuild (`npm run build`) or refresh in dev.
2. **Point at an endpoint.** Build/run with
   `VITE_RESULTS_URL=https://your-host/results.json`.

No component edits are required. Verified by swapping `results.json` ⇄
`results.sample.json` live: every KPI, parameter grid, matrix cell, chart, audit
item, and freshness line repopulated/depopulated with zero code changes.

Two fixtures ship in `public/results/`:

| File | State | Purpose |
|---|---|---|
| `results.json` | **Active — fully PENDING** (all 8 suites, every value absent) | The honest current state while the benchmark is re-processed on the VPS. Replace with the VPS output. |
| `results.sample.json` | Sample (4 suites populated with real stale numbers, 4 results-pending) | Design reference — shows the populated layout and the exact data shape. Copy over `results.json`, or set `VITE_RESULTS_URL` to it, to preview. |

> The active default is intentionally all-pending: the structure (suites,
> systems, parameter configurations, metrics, copy, chart axes) is final now;
> only the numbers arrive later.

---

## 2. The contract (where it lives)

| File | Role |
|---|---|
| `web/src/results/schema.ts` | The typed contract (v2). Components consume these types and nothing else. |
| `web/src/results/adapter.ts` | The **one** normalization + derivation module. Raw JSON → validated bundle; all formatting / winner-detection / deltas / grouping live here. Adjust this one file if the VPS emits a different raw shape. |
| `web/src/results/load.ts` | The single fetch seam (`RESULTS_URL`). |

If the VPS emits the schema shape directly (§3), you change **nothing in code**.
If it emits a different raw shape, you adjust **only `adapter.ts`**.

---

## 3. The shape the VPS run must emit

A single JSON object. A **suite is three axes**: `systems` × `configurations`
(the parameter/scenario/phase points) × `metrics`, with values in a dense
`matrix[systemId][configId][metricKey]`. Full working examples are in the two
fixtures — copy `results.sample.json` and replace the numbers.

```jsonc
{
  "schemaVersion": 2,
  "provenance": { "runId": "vps-…", "generatedUtc": "2026-…Z", "host": "vps",
                  "gitCommit": "abc1234", "kind": "final", "note": "VPS re-run" },
  "groups": ["System comparison", "Routing", "Autoscaling", "Forecasting", "Anomaly detection"],
  "suites": [
    {
      "id": "rl-routing",
      "group": "Routing",                          // bucket in the benchmark hierarchy
      "label": "RL routing",
      "question": "…", "summary": "…",
      "provenance": { … },                          // per-suite freshness
      "verdict": { "tone": "ok", "text": "…" },     // ok|warn|bad|muted
      "subjectId": "policy_shipped",                // which system is "this system"
      "primaryMetricKey": "p95",                    // metric the parameter grid shows first
      "defaultConfigId": "homogeneous",             // config the system×metric matrix shows first
      "kpis": [ { "key":"…","label":"…","value": 67.5,"unit":"%","direction":"lower-better",
                  "baselineValue": 32.5,"baselineLabel":"round-robin","tone":"bad" } ],
      "systems": [ { "id":"policy_shipped","label":"PPO policy","role":"subject" },
                   { "id":"round_robin","label":"Round-robin","role":"baseline" } ],
                   // role: subject|baseline|candidate|ceiling|floor|reference
      "configurations": [                           // THE PARAMETER AXIS
        { "id":"homogeneous","label":"Homogeneous","params": { "scenario":"homogeneous" } },
        { "id":"heterogeneous","label":"Heterogeneous","params": { "scenario":"heterogeneous" } }
        // … or an aggregate: { "id":"aggregate","label":"All profiles","isAggregate": true }
      ],
      "metrics": [ { "key":"p95","label":"p95 latency","unit":"ms","direction":"lower-better" } ],
                   // direction: lower-better|higher-better|target|neutral ; "target" uses "target": n
      "matrix": {                                   // [systemId][configId][metricKey] = measure
        "policy_shipped": {
          "homogeneous":   { "p95": { "value": 453.1, "ci95": 3.7 } },
          "heterogeneous": { "p95": { "value": 736.0, "ci95": 17.1 } }
        }
        // value:null (or a missing cell) ⇒ renders PENDING
      },
      "charts": [ { "key":"…","title":"…","kind":"bars","yUnit":"ms","direction":"lower-better",
                    "bars":[ { "label":"…","value": 453.1,"emphasis": true } ] } ]
                    // kind "lines": "series":[{ "id","label","emphasis","points":[{ "x","y" }] }]
    }
  ],
  "audit": [ { "key":"…","title":"…","summary":"…","provenance":{ … },
               "verdict":{ "tone":"ok","text":"…" },
               "kpis":[ … ],
               "stages":[ { "label":"Before any fix","value": 54.5,"unit":"%","tone":"bad" } ],
               "items":[ { "id":"D1","label":"…","status":"fixed","severity":"high","ref":"…","detail":"…" } ] } ],
               // status: pass|fail|warn|fixed|info|pending
  "grafana": { "baseUrl":"http://localhost:3000","embedQuery":"kiosk&theme=light&refresh=30s",
               "dashboards":[ { "uid":"smartload-overview","title":"Overview","description":"…" } ] }
}
```

**Rules the renderer relies on:**

- **Open suite set.** Add, remove, or reorder suites freely; the UI reads the
  list and the `groups` and renders dynamically. 2 suites or 20, no code change.
- **Parameter axis.** `configurations` is the third axis. A no-sweep suite uses a
  single `{ "isAggregate": true }` configuration. With ≥2 parameter
  configurations the UI shows the systems × parameters grid automatically.
- **Pending.** Any `value: null`, missing matrix cell, or empty `bars`/`series`
  renders the defined PENDING placeholder — never a fake number, never a broken
  layout. An empty `matrix: {}` makes the whole suite render pending.
- **Direction** drives winner highlighting (per metric, per configuration) and
  the "better" caption. `subjectId` marks "this system". Winners are computed
  across contender roles only (subject/baseline/candidate); ceiling/floor/
  reference rows are shown but excluded from "who wins".
- Keep `id`/`key` values stable across runs so deep-links (`?suite=<id>`) and
  diffs stay meaningful. Only the numbers should change between runs.

---

## 4. Gaps the harness must fill (validation of current artifacts)

The on-disk artifacts do **not** match the contract shape directly — they are
`SUMMARY.md` tables + `meta.json` (and `grid.csv` for some). To inject VPS output
the run must emit the schema JSON above. See `BENCHMARK_INVENTORY.md` for the
per-suite systems/parameters/metrics. In general:

| Source | Has today | Gap to close |
|---|---|---|
| `experiments/*/results/*/meta.json` | `generated_utc`, `git_commit`, params | Add `host` (e.g. `"vps"`); map → `provenance`; set `kind: "final"`. |
| `experiments/*/results/*/SUMMARY.md` | `mean ± 95% CI` per (system, **parameter**) in markdown | Emit machine-readable `matrix[system][config][metric] = { value, ci95 }` — the parameter breakdown the per-config tables already contain. |
| `rl-routing-bench` grid.csv / probe.json | per-(contender, scenario, band) CSV + monotonicity | Reduce to per-system per-**scenario** scalars; the probe PASS/FAIL can become an audit item. |
| `anomaly-detection-bench` grid.csv | per-(engine, profile, gate, seed) | Reduce to per-engine per-**profile** P/R/F1/etc. (results/ is currently empty — no committed run). |
| `adaptive-bench`, `baseline-vs-smartload` | per-**phase** Locust CSVs (results/ empty) | Emit per-side/per-phase p50/p95/p99/error/rps. |
| Grafana | n/a (deploy config) | Set `grafana.baseUrl` for the presentation environment. |

Two ways to produce `results.json`: (1) have each harness write a schema-shaped
`results.json` and merge the per-suite objects, or (2) a small converter that
parses `meta.json` + `SUMMARY.md` (per-parameter tables) into the schema — that
converter is then the only new code; the UI's `adapter.ts` stays untouched.

---

## 5. What is finalized now vs. what flows in later

**Finalized now:** every layout, copy string, comparison framing, the suite list
and grouping, each suite's systems/baselines, the parameter configurations, the
metric definitions and direction-of-better, chart axes and units, KPI
definitions, the audit narrative, the Grafana dashboard list, and all
pending/empty states.

**Flows in later (only this):** the numeric `value`/`ci95` of each measure, the
KPI/stage values, the chart `bars`/`series` values, and the `provenance`
timestamps/host/commit/kind. Replace the numbers; the presentation is done.
