# Engine authoring guide

The strict contract for anyone writing or modifying a plugin in `services/anomaly-detector/engines/`, `services/forecasting/engines/`, or `services/rl-engine/policies/`.

If your change ends up touching a file outside the patterns described here, stop and re-read this document. The plugin-host services are designed so that a new engine drops in as **one folder, three files, zero edits to the run loop**. Anything more than that is a sign the slice belongs in the run loop or in `services/shared/`, not in your plugin.

This guide is referenced from each plugin's own README and is part of the slice acceptance contract — `docs/features/SLICE_CHECKLIST.md` Layer 1 ("Service code") implicitly requires conformance to it.

---

## 1. Axioms — three rules nothing in this guide overrides

1. **Plugins are pure.** An engine class takes structured inputs (dataclasses) and returns structured outputs (dataclasses). It does not open sockets, query databases, talk to Redis, log, sleep, read environment variables, or read files outside its own plugin folder. Every side effect lives in the run loop.
2. **The run loop owns the envelope.** Plugins never construct an `Envelope`, never call `publish_envelope`, never reach into `services/shared/contracts.py` for anything except the input/output dataclass shapes the ABC declares. Conversion from engine output → envelope payload is the run loop's job (`*_to_event_payload` in each `runloop.py`).
3. **The baseline never fails.** Every plugin-host service has one baseline plugin that has no model artefact, no external dependency, and constructor params that cannot raise. The baseline is the fallback the run loop installs when the requested plugin fails to load. If you are not writing the baseline, your plugin must tolerate being absent (the operator can pin the baseline at any time) and must not assume any sibling plugin exists.

If a teammate's change violates one of these three, reject the PR — no amount of cleverness is worth breaking them.

---

## 2. The three plugin-host services

| Service | Plugin folder | ABC module | ABC class | Baseline | Factory entry |
|---|---|---|---|---|---|
| `anomaly-detector` | `engines/<name>/` | `engine_base.py` | `AnomalyEngine` | `threshold` | `select_engine(name)` |
| `forecasting`      | `engines/<name>/` | `engine_base.py` | `ForecastEngine` | `moving_average` | `select_engine(name)` |
| `rl-engine`        | `policies/<name>/` | `policy_base.py` | `RoutingPolicy` | `random_shadow` | `select_policy(name)` |

The shape of each is identical; only the names differ. From here on, the guide says **"engine"** to mean any of the three. Where rl-engine diverges (it's `policies/` + `RoutingPolicy` + `select_policy`), the divergence is purely textual.

The run loop for each service lives in `services/<svc>/runloop.py` and is the only consumer of your plugin. Read it before writing new code — the shape of the dataclasses it hands you and the dataclasses it expects back is the entire interface.

---

## 3. Anatomy of a plugin — exact file layout

A plugin is **one folder under the host's plugin directory**. The folder is self-contained: implementation, tests, README. No flat dumps.

```
services/<svc>/engines/<your_name>/
├── __init__.py          # empty (1 line, or just blank). The folder is a package, not a namespace dump.
├── engine.py            # the single implementation file. Class lives here.
├── test_engine.py       # pytest module. Same folder so it ships with the plugin.
└── README.md            # what the engine does, why it ships, how it's tuned.
```

For rl-engine the names are `policy.py` and `test_policy.py`. Everything else is identical.

**Hard rules:**

- The folder name is `snake_case` and matches exactly the string the factory dispatches on (`select_engine("threshold")` ⇒ `engines/threshold/`).
- Do not add subfolders, sibling helper files, or `utils.py`. If you need a helper, either inline it in `engine.py` (one helper class is fine; a `utils/` folder is not) or — if it is genuinely cross-engine — propose moving it into the service's `runloop.py` or `services/shared/`.
- The model artefact, if any, does **not** live in the plugin folder. It lives at `services/<svc>/models/<name>.<ext>` (e.g. `services/anomaly-detector/models/isolation_forest.pkl`). The plugin's `__init__` loads it by an explicit path; the plugin folder stays small and reviewable.

---

## 4. Step-by-step — adding a new engine

The mechanical recipe. If you find yourself doing anything that isn't on this list, you are leaving the pattern.

1. **Create the folder** `services/<svc>/engines/<name>/`. Add the empty `__init__.py`.
2. **Write `engine.py`.** Import the ABC from `engine_base` using the sibling-import idiom (see §5). Subclass the ABC. Implement the one abstract method. Implement `__init__` such that **no constructor argument can raise** for valid types — your engine must construct cleanly even when the policy is at defaults.
3. **Write `test_engine.py`.** Cover every branch of your decision logic + the empty-input case + the degenerate-input case (zero rolling mean, empty history, etc.). Use the same sibling-import idiom; no Flask/Redis/DB in the tests.
4. **Write `README.md`** following the template in §10.
5. **Register the engine** in the host's factory (`engine_base.py::select_engine` or `policy_base.py::select_policy`) — one new `if name == "..."` branch with a deferred import. See §11.
6. **If the engine takes policy-driven kwargs**, ensure the run loop's `EnginePolicy.engine_kwargs()` exposes them (§8).
7. **Run the host service's unit tests** locally: `pytest services/<svc>/engines/<name>/`.
8. **Stop.** Do not edit `app.py`. Do not edit `contracts.py`. Do not add new Redis channels. Do not add new HTTP routes. If the slice you are implementing requires any of those, it is bigger than an engine — it is a slice, and `docs/features/SLICE_CHECKLIST.md` applies in full.

---

## 5. The ABC contract — what every implementation must satisfy

Each plugin-host service declares its ABC in `<svc>/{engine_base.py,policy_base.py}`. Read those files first; this section just summarises the invariants.

### 5.1 The one abstract method

| ABC | Method | Input | Output |
|---|---|---|---|
| `AnomalyEngine` | `score(features: BackendFeatures) -> AnomalyScore` | single backend | single classification |
| `ForecastEngine` | `forecast(history: HistoryWindow) -> Forecast` | rolling window | next-horizon prediction |
| `RoutingPolicy` | `act(state: list[BackendState]) -> RoutingAction` | all backends | mode + per-backend rankings |

Your subclass implements exactly this one method. Anything that smells like "but I also need to publish X" or "but I also need to read Y" is a run-loop change, not an engine change.

### 5.2 `reload()` is optional and pure

The ABC declares `reload()` as a no-op default. Override it only if your engine has internal state that should refresh when policy changes (e.g. cached thresholds derived from policy at construction time). `reload()` is allowed to mutate `self` but **must not** open files, talk to the network, or block — the run loop calls it on the same thread as `score`/`forecast`/`act`.

### 5.3 Constructor signature

- All constructor parameters take defaults. The factory must be able to call `MyEngine()` with no kwargs and get a working instance — this is what `bootstrap_engine` does when policy is at startup defaults.
- All parameters are simple Python types (`int`, `float`, `str`, `bool`). No dataclasses, no objects from `services/shared/`. The run loop's `EnginePolicy.engine_kwargs()` produces a flat `dict` of these types.
- Your engine must tolerate **unknown kwargs being absent**: do not add a required kwarg. The run loop builds kwargs from policy fields shared across siblings, so a kwarg that only your engine cares about must default to a working value.
- For rl-engine, the run loop wraps `select_policy` in a `TypeError` fallback (`runloop.py::_safe_select`) so a policy whose constructor doesn't accept the standard kwargs (like `random_shadow(seed=...)`) still loads. Take advantage of this only if you genuinely have no use for the standard kwargs — don't use it to dodge wiring up a useful policy field.

### 5.4 Sibling-import idiom

Every plugin uses the same import preamble — copy it verbatim, do not invent variants. From `engines/<name>/engine.py`:

```python
from __future__ import annotations

import sys
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from engine_base import <Whatever, You, Need>  # noqa: E402
```

This makes the file work both when loaded from `/app` in the container and from the repo in dev. Tests use the same idiom (with `parents[2]` resolving to the service root). Do **not** use relative imports (`from ..engine_base import …`) — the run loop adds the service root to `sys.path` and loads plugins by absolute import; relative imports break that flow.

---

## 6. The forbidden list — things plugin code MUST NOT do

If your `engine.py` or `policy.py` contains any of these, the PR is rejected:

| Forbidden | Belongs in | Why |
|---|---|---|
| `import redis` or any `publish_envelope(...)` | `runloop.py` / `app.py` | The run loop owns the bus; plugins return data. |
| `import psycopg2` or any SQL string | `runloop.py` / `services/shared/queries.py` | The run loop owns DB; plugins consume the parsed rows. |
| `from flask import …` | `app.py` | Plugins know nothing about HTTP. |
| `os.environ[...]` reads | `runloop.py` / `app.py` | The run loop reads env once at startup and hands you the resolved value. |
| `time.sleep(...)` or threads | run loop | Plugins are called synchronously from the run-loop tick. |
| `print(...)` or `logging.*` | run loop | Plugin output is the return value, not stderr. (The run loop logs the bootstrap result + drop reasons.) |
| Construction of `Envelope`, `AnomalyEvent`, `ForecastResult`, `RoutingRecommendation`, `ScalingEvent`, `PolicyUpdate` | `runloop.py` | These are wire types. The run loop converts your dataclass output into the wire payload via `*_to_event_payload`. |
| Reading or writing `config/policy.yaml` directly | policy-manager / run loop | Policy reaches the engine as kwargs, never as a file read. |
| Network or file-system I/O at runtime (not at `__init__`) | run loop | One model load at startup is fine; per-tick I/O is not. |

Loading a model artefact from `services/<svc>/models/...` **once during `__init__`** is allowed. If the file is missing, raise — the run loop's `bootstrap_engine` catches the exception and falls back to the baseline. Do not silently degrade to a fallback inside your engine; let the run loop handle it.

---

## 7. Inputs and outputs — the dataclass surfaces

Plugins talk to the run loop in dataclasses, never in dicts. The dataclasses live in the ABC module (`engine_base.py` / `policy_base.py`). Do not redefine them, do not subclass them, do not add fields to them in your plugin file.

| Service | Input dataclass | Output dataclass |
|---|---|---|
| anomaly-detector | `BackendFeatures(backend_id, latency_ms, latency_rolling_mean_ms, error_rate, sample_count)` | `AnomalyScore(backend_id, status, score)` |
| forecasting      | `HistoryWindow(timestamps, request_rates)` | `Forecast(horizon_minutes, predicted_rps, confidence_lower, confidence_upper)` |
| rl-engine        | `list[BackendState(backend_id, latency_ms, queue_depth, health)]` | `RoutingAction(mode, rankings: list[Ranking])` |

If you need a field that isn't on the input dataclass:

1. **First check** whether the run loop already has it but isn't passing it through. The run loop builds the dataclass from a `services/shared/queries.py` row — extending the query and the build function is the right place.
2. **If the field genuinely doesn't exist**, the change is cross-cutting and lands as: SQL update in `services/shared/queries.py` + run-loop `build_*_from_rows` update + dataclass extension in `engine_base.py`. **All sibling engines must continue to work** — make the new field optional with a sane default.

Output dataclass values are constrained:

- `AnomalyScore.status` is one of `"healthy" | "degraded" | "unhealthy"`. Other strings are not parsed by downstream consumers.
- `AnomalyScore.score` is in `[0.0, 1.0]`. Outside that range is undefined.
- `Forecast.predicted_rps`, `confidence_lower`, `confidence_upper` are non-negative floats. Lower ≤ predicted ≤ upper.
- `RoutingAction.mode` is `"shadow"` or `"active"`. The run loop's `effective_mode` may still override your `"active"` to `"shadow"` based on operator pin / safe_mode — do not try to second-guess it; just return what your policy genuinely chose.
- `Ranking.score` is a float; relative ordering is what the sidecar reads. Higher means more preferred.

---

## 8. Adding a policy-driven knob

If your engine needs a tunable that operators can change at runtime, the wiring touches three places — in this order:

1. **The canonical policy field.** Add the field to `config/policy.yaml`, to `PolicyUpdate` in `services/shared/contracts.py`, and (when relevant) to policy-manager validation. This is a policy-management change, not an engine change, and follows the policy slice's own discipline. Coordinate with whoever owns policy.
2. **The run loop.** Extend `runloop.py::EnginePolicy` with the new field + a default. Extend `policy_from_payload` to parse it from the smartload.policy envelope payload (using the `_int`/`_float` helpers that fall back on missing/malformed values). Extend `engine_kwargs()` so the value reaches the constructor.
3. **Your engine's `__init__`.** Accept the new kwarg with a sane default. **Other engines in the same service must not break.** They inherit the same `engine_kwargs()`, so either give it a default they tolerate, or accept it and ignore it.

Do not read the new field from a global, an env var, or a YAML file directly. It reaches you through `__init__` kwargs, period.

A worked example is in `services/anomaly-detector/runloop.py` — the `anomaly_latency_multiplier` policy field flows through `policy_from_payload` → `EnginePolicy.latency_multiplier` → `engine_kwargs()` → `ThresholdEngine(latency_multiplier=...)`.

---

## 9. Tests — what must exist before the PR opens

`test_engine.py` (or `test_policy.py`) lives in the plugin folder. It runs in the `unit-tests` CI job — no Docker, no DB, no Redis.

Required cases — copy this list as your test plan, then add domain-specific cases:

1. **Happy path on representative input.** One canonical sample produces the expected classification / prediction / ranking.
2. **Every decision branch.** If your engine has N status outcomes or N mode outcomes, you need N tests that exercise exactly those branches. The threshold engine's `test_engine.py` is the reference shape (`test_healthy_when_...`, `test_degraded_when_...`, `test_unhealthy_when_...`).
3. **Empty input.** An empty `HistoryWindow` / `list[BackendState]` / a `BackendFeatures` with `sample_count=0` must produce a defined, non-throwing result.
4. **Degenerate input.** Any divide-by-zero, log-of-zero, etc. that your math could hit. Cover them with a fixture that names the degeneracy.
5. **Reproducibility (when stochastic).** If your engine uses randomness, seed it. Two seeded runs must produce identical output — see `random_shadow/test_policy.py::test_seeded_runs_are_reproducible`.

Tests must not import `app.py`, `runloop.py`, `contracts.py`, `redis`, `psycopg2`, or `flask`. They depend only on the ABC module and your `engine.py`. If you need any of those imports, your test is at the wrong layer — move it under `tests/e2e/<feature>/` instead.

---

## 10. README — the template

Every plugin folder has a `README.md` matching this shape. Sections in this order, no extras:

```markdown
# <name> engine

One-paragraph statement of what this engine computes and when it ships.

## Behavior

How the algorithm decides. Math or rules in plain English. Cite the input
fields it uses by name. No code blocks unless a formula needs them.

## Why it ships

The role this engine plays in the service: is it the baseline (fallback
when others fail)? Is it the trained replacement? Is it experimental? Be
explicit — operators read this to decide whether to set the env var.

## Tuning

Which policy fields drive it. If none, say "no runtime tuning".

## Tests

One bullet per `test_engine.py` test. The bullet describes what the test
proves, not how it's structured.
```

Optional `## Status` section when the plugin is scaffolded but not implemented — see `engines/isolation_forest/README.md` for the pattern.

---

## 11. Factory registration

The factory is the single dispatch point. Adding a new engine means adding **one** branch to `select_engine` (or `select_policy`):

```python
def select_engine(name: str, **kwargs) -> AnomalyEngine:
    if name == "threshold":
        from engines.threshold.engine import ThresholdEngine
        return ThresholdEngine(**kwargs)
    if name == "isolation_forest":
        from engines.isolation_forest.engine import IsolationForestEngine
        return IsolationForestEngine(**kwargs)
    if name == "<your_new_name>":
        from engines.<your_new_name>.engine import <YourClass>
        return <YourClass>(**kwargs)
    raise ValueError(f"Unknown anomaly engine: {name!r}")
```

- Imports are **deferred inside the branch**, never at module top-level. This keeps `engine_base.py` importable without dragging every plugin's transitive deps (`statsmodels`, `scikit-learn`, `torch`) into the run loop's startup path.
- The string in `if name == "..."` matches your folder name exactly.
- The `raise` at the bottom is the contract — unknown engine names are a deployment error and must blow up loudly. Do **not** silently default to the baseline here; that decision belongs to `bootstrap_engine`, which has the right context (it knows what was requested and can log the fallback).

---

## 12. Model artefacts and the fallback contract

If your engine has a trained model:

- Store it at `services/<svc>/models/<name>.<ext>`. The container build copies the directory in; nothing else needs to change.
- Load it **once** in `__init__`. If the file is missing or the load fails, **raise** — propagate the exception out of the constructor. The run loop's `bootstrap_engine` catches it and falls back to the baseline. Do not wrap the load in a try/except that swallows.
- Pin the loader's expected schema in code (file size, feature count, model class) and raise on mismatch. Model files drift; an explicit shape check is cheap.
- Record the model version on the output where the dataclass allows it (`AnomalyScore` does not carry it directly, but `score_to_event_payload` does — the run loop adds the loaded engine's name as `model_version`). Talk to whoever owns the run loop if your engine wants to expose a more specific version string.

The baseline engine — `threshold`, `moving_average`, `random_shadow` — must have **no** model artefact and must not raise during construction for any valid policy. The whole fallback story rests on this.

---

## 13. Don't reinvent — things the run loop and shared layer already do

Before you write a helper, check these:

| You might be about to write… | It already exists at |
|---|---|
| Code that pulls metrics from TimescaleDB | `services/shared/queries.py` (canonical SQL) + run-loop `build_*_from_rows` |
| Code that publishes to Redis | `services/shared/contracts.py::publish_envelope` (called from run loop) |
| Code that subscribes to `smartload.policy` and refreshes state | `runloop.py::policy_from_payload` + `app.py` subscriber thread |
| Code that decides whether to publish (safe_mode / advisory / cold start) | `runloop.py::should_publish` |
| Code that converts your engine output to the wire shape | `runloop.py::*_to_event_payload` |
| Code that picks an engine with fallback to baseline | `runloop.py::bootstrap_engine` |
| Code that classifies a backend's health from raw metrics (rl-engine only) | `runloop.py::classify_health` |
| Code that composes the published `mode` from policy + env + safe_mode (rl-engine only) | `runloop.py::effective_mode` |
| Code that builds an `Envelope` around a payload | `services/shared/contracts.py::make_envelope` (used by `publish_envelope`) |
| Code that drops stale Redis messages | `services/shared/contracts.py::parse_envelope` (via `subscribe_envelope`) |

If a feature you need is **almost** in this list but not quite, propose extending the run loop or the shared layer in a separate commit. Don't duplicate into your plugin.

---

## 14. Pre-PR checklist

Run through this before opening a PR. Every item is verifiable.

- [ ] Plugin folder lives at `services/<svc>/{engines,policies}/<snake_case_name>/` and contains exactly `__init__.py`, `engine.py` (or `policy.py`), `test_engine.py` (or `test_policy.py`), `README.md`.
- [ ] `engine.py` uses the sibling-import idiom from §5.4 verbatim.
- [ ] The class subclasses the ABC and implements exactly the one abstract method.
- [ ] The class constructor has defaults for every parameter and cannot raise on valid policy defaults.
- [ ] None of the items in §6's forbidden list appear in `engine.py` or `test_engine.py`.
- [ ] If a model file is loaded, it's at `services/<svc>/models/`, loaded once in `__init__`, and a missing/corrupt file raises out of the constructor.
- [ ] `select_engine` (or `select_policy`) has one new branch with a deferred import, and the folder name matches the dispatch string.
- [ ] If a new policy field is read, all three layers from §8 are wired and every sibling engine still constructs cleanly with the new kwarg present.
- [ ] `test_engine.py` covers every decision branch + empty input + degenerate input (+ seeded reproducibility if stochastic).
- [ ] `README.md` follows the §10 template (no extra sections, sections in order).
- [ ] `pytest services/<svc>/engines/<name>/` passes locally.
- [ ] The diff does not touch `app.py`, `services/shared/contracts.py`, `docs/openapi/smartload-v1.yaml`, or `docs/redis-channels.md`. If it does, the change is bigger than an engine — re-scope it.

If every box is checked, the PR is shaped correctly. Review then focuses on the engine's math, not on whether the wiring is right.

---

## 15. When this guide is wrong

This document describes the patterns in `engine_base.py`, `policy_base.py`, and the three `runloop.py` modules as they exist today. If those files change in a way that contradicts this guide, the **code is canonical** — update this guide in the same PR that changes the run loop. Out-of-sync guidance is worse than no guidance.

The canonical references this guide derives from:

- `services/anomaly-detector/engine_base.py` + `services/anomaly-detector/runloop.py`
- `services/forecasting/engine_base.py` + `services/forecasting/runloop.py`
- `services/rl-engine/policy_base.py` + `services/rl-engine/runloop.py`
- `services/shared/contracts.py` (envelope + payload dataclasses)
- `services/shared/queries.py` (canonical SQL that produces the dataclass inputs)
- `docs/features/SLICE_CHECKLIST.md` (the wider slice contract this guide sits inside)

If a teammate asks "where does this rule come from?", point them at one of those.
