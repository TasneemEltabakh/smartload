# Ablation harness — measuring each C_spike fix's contribution (T1.1)

Leave-one-out ablation of the SmartLoad spike fix stack: run the equal-capacity
(5v5) `adaptive-advantage` scenario once with the **full** stack and once per fix
**removed**, so each fix's marginal contribution is a measured number rather than a
claim. This is the thesis's headline evidence — it turns "I fixed the spike" into a
decomposition of *which mechanism bought how much*.

## Files
- `ablation.sh` — orchestrator. Defines the configs, runs each via `run.sh`, writes a
  manifest, calls the aggregator.
- `ablation_compare.py` — reads the manifest, averages per-phase error% across runs,
  prints an **absolute** table and a **Δ-vs-full contribution** table (Markdown).

## The configs (each flips exactly one toggle off the full stack)

| Config | Toggle changed | Fix removed |
|---|---|---|
| `full` | — (also runs `baseline` as the floor reference) | none |
| `no-clamp` | `LB_SIDECAR_CLAMP_MIN_FRACTION=0` | anti-concentration clamp (T0.1/T0.2) |
| `no-guard` | `ANOMALY_ABSOLUTE_OVERLOAD_SUPPRESSION=false` | `#3` absolute-overload guard (T0.3) |
| `no-pin` | `MIN_BACKENDS=1` | equal-capacity pin → autoscaler free to flap (T0.6a) |
| `no-reset` | `RESET_UPSTREAM=0` | per-side routing reset (T0.6b) |

The restart-correctness fixes (T0.4 detector exclusion-clock hydration, T0.5 sidecar
health reconciliation) have **no runtime toggle** — they stay on; the `no-reset`
config exercises the stale-`down` path they protect.

Each toggle reaches the services through **docker-compose env substitution** (shell
env > `--env-file`), and `run.sh` recreates the plane per side, so **no per-config
rebuild** is needed — only one rebuild beforehand to bake in the env-reading code.

## Run it
```bash
# one-time: bake the env-reading knobs into the images
docker compose build lb-sidecar anomaly-detector
# then (RUNS>=2 strongly recommended for the final table — per-config variance):
RUNS=2 bash experiments/adaptive-advantage/ablation.sh
# report lands at results/ablation-<ts>/ablation_report.md
```

## Reading the output
- **Absolute table** — error% per phase for baseline / full / each ablated config.
- **Contribution table** — `Δfull` = ablated − full = *the cost of removing that fix*
  (positive ⇒ errors got worse without it). On the **C_spike** row, the largest
  positive Δ is the fix that contributes most to surviving the spike.

## Wiring (knobs this harness depends on)

Service-side knobs (already in the code; read at service startup):
- `LB_SIDECAR_CLAMP_MIN_FRACTION` → `services/lb-sidecar/app.py` → `handle_routing(clamp_min_fraction=…)`
- `ANOMALY_ABSOLUTE_OVERLOAD_SUPPRESSION` → `services/anomaly-detector/app.py` → `EnginePolicy.absolute_overload_suppression` (also live-settable via the `anomaly_absolute_overload_suppression` policy knob)

Harness-side knobs in `run.sh`:
- `MIN_BACKENDS` — already present (the equal-capacity pin).
- `RESET_UPSTREAM` — gates the per-side `_reset_upstream` call (default 1).

Two wiring edits are required before the first run (they touch `run.sh` and
`docker-compose.yml`, deferred while the validation benchmark was holding `run.sh`):

1. **`docker-compose.yml`** — expose the two service knobs for env substitution:
   - under `lb-sidecar:` → `environment:` add
     `LB_SIDECAR_CLAMP_MIN_FRACTION: ${LB_SIDECAR_CLAMP_MIN_FRACTION:-0.75}`
   - under `anomaly-detector:` → `environment:` add
     `ANOMALY_ABSOLUTE_OVERLOAD_SUPPRESSION: ${ANOMALY_ABSOLUTE_OVERLOAD_SUPPRESSION:-true}`
2. **`run.sh`** — make the per-side reset toggleable:
   - add near the other knobs: `RESET_UPSTREAM="${RESET_UPSTREAM:-1}"`
   - guard the call: `[[ "$RESET_UPSTREAM" == "1" ]] && _reset_upstream`

After those two edits + the one-time rebuild, `ablation.sh` runs end-to-end.
