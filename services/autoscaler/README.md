# autoscaler

Adjusts the backend pool size in response to forecast signals and reactive load measurements.

## Role
- Subscribes to `smartload.forecast` for predicted-RPS signals
- Subscribes to `smartload.policy` for live `min_backends` / `max_backends` / `per_instance_capacity_rps` updates
- Scales the `test-backend` Docker pool out / in using the Docker SDK
- Enforces a cooldown between actions
- Writes every action to the `scaling_events` hypertable
- Publishes `ScaleEvent` to `smartload.scale`
- Reactive fallback: if no forecast has been seen for `FORECAST_STALE_SECONDS`, scales based on current load only

## HTTP endpoints
- `GET /health` — uniform health (also reports the active `controller`)
- `GET /metrics` — Prometheus metrics
- `POST /api/v1/scale` — manual scale override
- `POST /api/v1/actions/simulate` — side-effect-free dry-run of a manual scale
- `GET /api/v1/audit/scaling` — recent `scaling_events` rows

## Redis channels
- Subscribes: `smartload.forecast`, `smartload.policy`
- Publishes: `smartload.scale`

## Controller selection
Two controllers share the decision sites. The selector is read once at boot:

- `AUTOSCALER_CONTROLLER=step` (default) — the shipped ±1 bang-bang rule
  (`decisions.decide`): one instance per action.
- `AUTOSCALER_CONTROLLER=target` — the target-based controller
  (`controllers.decide_target`): sizes straight to a target instance count with
  multi-step jumps, asymmetric scale-out/scale-in cooldowns, and a scale-in
  deadband.

`min_backends`, `max_backends`, and `per_instance_capacity_rps` always come from
the live policy (`config/policy.yaml`, live-reloaded over `smartload.policy`).
The target controller's tuning is deploy-time env:

| Env var | Default | Meaning |
|---|---|---|
| `AUTOSCALER_HEADROOM` | `0.15` | fractional safety margin (headroom law) |
| `AUTOSCALER_SIZING` | `headroom` | `headroom` or `sqrt_staffing` |
| `AUTOSCALER_QOS_BETA` | `1.0` | β for the sqrt-staffing law |
| `AUTOSCALER_SCALE_OUT_COOLDOWN_SECONDS` | `0` | min seconds between scale-outs |
| `AUTOSCALER_SCALE_IN_COOLDOWN_SECONDS` | `120` | min seconds between scale-ins |
| `AUTOSCALER_MAX_STEP_OUT` | `0` | cap on instances added per action (0 = no cap) |
| `AUTOSCALER_MAX_STEP_IN` | `1` | cap on instances removed per action |
| `AUTOSCALER_SCALE_IN_DEADBAND` | `0.15` | extra slack required before shedding |

Other env: `TIMESCALEDB_URL`, `REDIS_URL`, `POLICY_PATH`, `LOOP_TICK_SECONDS`,
and the `AUTOSCALER_PROVISIONING_*` dynamic-pool flags.

## Status
Shipped — T1.3. Forecast-driven scaling + cooldown + reactive fallback live on
the default `step` controller. The `target` controller is wired and selectable
(v1.0.7bp) but off by default; a live end-to-end test under provisioning is the
remaining step before it becomes the default.

## See also
- Internals + diagrams: `docs/modules/autoscaler.md`
- Tests: `tests/unit/autoscaler/test_controllers.py`,
  `tests/unit/autoscaler/test_controller_wiring.py`,
  `tests/integration/test_autoscaler_decisions.py`
