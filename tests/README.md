# SmartLoad — Test layout and acceptance-test pattern

This document defines **how tests are organised in SmartLoad and what the per-task acceptance pattern looks like**. It exists so future PRs don't have to re-invent what "acceptance" means for a task — the answer is "follow the template at `tests/integration/_template_acceptance.py` and cite the SOT section your test maps to".

> Closes #117 meta-infra. Backfilling acceptance tests for already-shipped tasks is out of scope; new tasks adopt the pattern going forward.

## Directory layout

```
tests/
├── unit/<service>/        # pure-Python, no live stack, no docker
├── integration/           # service-pair / contract tests, assume `docker compose up -d`
├── e2e/<feature>/         # feature-level tests via the SDK, also live-stack
├── conformance/           # one suite per plugin contract (e.g. lb_adapter/)
└── fixtures/              # shared inputs (trace samples, sklearn bundles, etc.)
```

PEP 420 namespace packages — no `__init__.py` files. `pytest.ini` sets `pythonpath = .` so `from services.shared.contracts import ...` resolves from any test file.

## Test layers and how to run them

```bash
pytest tests/unit/                                    # pure-function, no live stack
docker compose up -d && pytest tests/integration/     # service-pair + wire-protocol
docker compose up -d && pytest tests/e2e/             # feature-level via SDK
pytest tests/conformance/                             # interface conformance, per plugin
docker compose up traffic-simulator                   # Locust at :8089 for ad-hoc load
```

**Slow tests.** Some live-stack tests have natural multi-minute runtime (forecast → scale-out cycles, model-calibrated detectors). They carry `@pytest.mark.slow` so CI's `compose-test` job filters them out by default with `-m "not slow"`. Operators run them locally with `pytest -m slow` against an appropriately-configured stack.

## The per-task acceptance-test pattern

Every product task (T-x.x, N-x.x, R-x.x in SOT §18 Build Status) ships **two test artefacts**:

1. **One pure-Python unit test** — fast, deterministic, asserts the engine / handler / pure-function contract in isolation. Lives at `tests/unit/<service>/test_<topic>.py` or alongside the implementation as `engines/<name>/test_engine.py`.
2. **One live-stack acceptance test** — assumes `docker compose up -d`, asserts the SOT acceptance criteria for the task end-to-end. Lives at `tests/integration/test_<feature>.py` (or `tests/e2e/<feature>/` if it's customer-surface SDK work).

The acceptance file's **module docstring header cites the SOT section it maps to**. That's the load-bearing link — a reviewer reading the test should be able to jump to the spec and check that the asserts match the contract.

### Starter template

Copy `tests/integration/_template_acceptance.py` when adding a new task's acceptance test. It compiles, collects under pytest, and skips cleanly with a `_TASK_ID = "<fill-in>"` placeholder until you fill in the real assertions. The template includes:

- Module docstring header pattern (task ID + SOT section + what acceptance means here).
- Stack-readiness preconditions and the skip-with-reason idiom.
- Subscriber-before-publisher pubsub pattern (see `test_t23_control_loop.py:_subscribe` for the Redis race rationale).
- A `_wait_for_*` helper pattern with explicit timeouts and last-state reporting in the failure message.

### Reference implementations

These existing test files all follow the pattern and are good starting points to copy from:

| File | Task | What it acceptance-tests |
|---|---|---|
| [`tests/integration/test_autoscaler.py`](integration/test_autoscaler.py) | T1.3 | `smartload.scale` envelope shape + `scaling_events` audit row |
| [`tests/integration/test_autoscaler_decisions.py`](integration/test_autoscaler_decisions.py) | T1.3 | Cooldown + reactive fallback path under driven load |
| [`tests/integration/test_autoscaler_dynamic_pool.py`](integration/test_autoscaler_dynamic_pool.py) | #155 R1 | `provision()` / `decommission()` lifecycle against the live Docker daemon |
| [`tests/integration/test_telemetry_ingest.py`](integration/test_telemetry_ingest.py) | T1.1 | OTLP/HTTP-JSON ingress + DB persistence + read API |
| [`tests/integration/test_lb_otel_shipper.py`](integration/test_lb_otel_shipper.py) | T1.2 | Per-request fidelity (`STDDEV(latency) > 0` on live traffic) |
| [`tests/integration/test_t23_control_loop.py`](integration/test_t23_control_loop.py) | T2.3 | Closed-loop anomaly reroute + safe-mode + forecast scale-out |
| [`tests/integration/test_isolation_forest_artifact.py`](integration/test_isolation_forest_artifact.py) | #101 / N2.1 | Real `.pkl` loads with the pinned sklearn version |
| [`tests/integration/test_isolation_forest_live_stack.py`](integration/test_isolation_forest_live_stack.py) | #101 / N2.1 | `@pytest.mark.slow` — engine flags injected latency UNHEALTHY |
| [`tests/integration/test_loop_catchall.py`](integration/test_loop_catchall.py) | #163 | Run-loop survives a raising `_inference_cycle` |
| [`tests/integration/test_loop_liveness.py`](integration/test_loop_liveness.py) | #163 | `/health` flips to 503 after the loop goes stale |

## Benchmarks vs. acceptance tests

The acceptance tests in `tests/` answer **"does the feature work?"** Benchmarks answer **"how well does it work and is it regressing?"**

Benchmarks live under `experiments/<feature>_<UTC>/` — frozen one-off run artefacts that are readable via git log. Three exist today:

| Harness | What it measures |
|---|---|
| [`experiments/baseline-vs-smartload/`](../experiments/baseline-vs-smartload/) | NGINX-only baseline vs. SmartLoad's full decision plane under a steady-state Locust load — p50/p95/p99 latency, error rate, throughput. |
| [`experiments/adaptive-bench/`](../experiments/adaptive-bench/) | RQ4 quantitative measurement of the forecast → autoscaler → lb-sidecar closed loop under a 5-phase Locust shape with anomaly injection. |
| [`experiments/anomaly-engine-bench/`](../experiments/anomaly-engine-bench/) | Comparison sweep of `threshold` vs. `isolation_forest` engines on a synthetic feature grid. Investigation tool, surfaced the #165 production_scaler calibration gap. |

Each generates a JSON / CSV report and (where applicable) a `SUMMARY.md`. A new bench for a future task should land under `experiments/<task>_<UTC>/` following the same shape — not be folded into one of these.

## When you open a PR

The repo carries a PR template (`.github/pull_request_template.md`) that asks you to check off:

- [ ] Unit test added (or task is meta-infra / docs-only).
- [ ] Live-stack acceptance test added or extended.
- [ ] SOT cross-reference in the test docstring.
- [ ] CI green.

These aren't enforced by a hook — they're a discipline, kept honest by reviewers and by the structural lints (`scripts/lint-structure.py`) that flag a `docs/features/<feature>.md` without a matching `tests/e2e/<feature>/`. When in doubt, look at how PR #158 (Isolation Forest) shipped: a 6-row SOT changelog entry, a unit test suite, a smoke test against the real artifact, and a live-stack test sealed behind `@pytest.mark.slow`. That's the bar.

## Conventions worth knowing

- **No `decode_responses=True` on Redis pubsub clients** — the `ignore_subscribe_messages` flag has subtle quirks in this combination. See `tests/integration/test_t23_control_loop.py:_subscribe` for the proven bytes-mode pattern.
- **`stack_ready` fixture** in `tests/integration/conftest.py` waits for every service's `/health` before any test runs. Use it whenever you depend on the compose stack.
- **Skip with reason, don't crash** — when a precondition isn't met (engine not configured, autoscaler provisioning off, pool already saturated), call `pytest.skip(...)` with an explicit operator-actionable reason. Examples: `test_t23_control_loop.py::test_forecast_drives_scale_out`, `test_isolation_forest_live_stack.py`.
- **Compose backends are seeded by name** — `smartload-test-backend-1:8080` through `-5:8080` are the canonical IDs. See `COMPOSE_BACKENDS` in `test_t23_control_loop.py`.
