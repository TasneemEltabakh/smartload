# Named Strategies

> **Feature slice #150 — shipped 2026-06-14.** Industry-vocabulary alias layer over the policy primitives. Integrators speak in named load-balancing strategies (`round-robin`, `latency-aware`, `ai-hybrid`, …); SmartLoad keeps the powerful primitive model (`operating_mode` + `safe_mode` + the deploy-time `RL_MODE` pin) intact. New endpoint + derived field + SDK method + scenario + e2e.

## What this slice delivers

Industry vocabulary uses named load-balancing strategies. SmartLoad exposes the composition primitives instead — `operating_mode` (canonical enum `classical-only` / `hybrid` / `rl-only`) + `safe_mode`, plus the `RL_MODE` env-var pin. Each named strategy is realisable as a combination of those primitives. This slice adds a thin translation layer so an operator or integrator can:

- **Apply a strategy by name** via `POST /api/v1/policy/strategy` — the name is translated to its primitives server-side and applied through the **same internal path** as `POST /api/v1/policy` (same validation, same audit row, same `smartload.policy` envelope). The response surfaces the **recommended `RL_MODE`** for the chosen strategy.
- **Read the active strategy** — `GET /api/v1/policy` gains a derived `strategy_name` field that reverse-maps the live primitives back to a strategy name, or `custom` when the primitives match no documented combination.

The primitive model stays the single source of truth. Named strategies are a vocabulary convenience, not a new store.

## The mapping table

| Strategy | `operating_mode` | `safe_mode` | `RL_MODE` (recommended pin) | Notes |
|---|---|---|---|---|
| `round-robin` | `classical-only` | `false` | n/a | Pure NGINX round-robin; no decision-plane signal |
| `least-connections` | `classical-only` | `false` | n/a | Same primitives; nginx `least_conn` directive in the LB template |
| `latency-aware` | `hybrid` | `false` | `shadow` | Anomaly + forecast feed routing weights; RL observe-only |
| `forecast-aware` | `hybrid` | `false` | `shadow` | Same operating mode; documents which signals dominate |
| `anomaly-aware` | `hybrid` | `false` | `shadow` | Same; emphasis on anomaly-driven exclusion |
| `ai-hybrid` | `hybrid` | `false` | `active` | All three signal sources, RL routes actively |
| `safe-fallback` | `classical-only` | `true` | n/a | Forces the deterministic baseline (kill switch) |

The `operating_mode` values are the **canonical policy enum** that policy-manager's validator accepts (`classical-only` / `hybrid` / `rl-only`), not the loose `classical` shorthand. The six non-fallback strategies come straight from `shared.config_loader.STRATEGY_PRIMITIVES` (#145) — the policy-manager endpoint imports that table rather than restating it. `safe-fallback` is the policy-manager-only kill switch (`safe_mode = true`), layered on in `services/policy-manager/strategies.py`.

### `RL_MODE` is a deploy-time pin, not a policy field

`RL_MODE` is pinned at deploy time as an environment variable on the rl-engine container; it is **not** a runtime policy field and is **never** set via `POST /api/v1/policy/strategy`. The endpoint only writes `operating_mode` + `safe_mode`. The recommended `RL_MODE` for the chosen strategy is surfaced in the response (`recommended_rl_mode`) and in this table so an operator knows which env pin matches the strategy they just selected. To actually change `RL_MODE`, redeploy the rl-engine with the new env value (or render it from `config/smartload.yml` via `scripts/bootstrap-config.py`, which already emits `RL_MODE` from the same table).

## The derived `strategy_name` field (reverse map)

`GET /api/v1/policy` returns the live policy with an added `strategy_name` field. It reverse-maps the `(operating_mode, safe_mode)` pair back to a strategy name.

**The reverse map is NOT unique.** Several strategies share the same primitive pair — most notably `latency-aware`, `forecast-aware`, `anomaly-aware` **and** `ai-hybrid` all map to `hybrid` + `safe_mode=false` (they differ only by the deploy-time `RL_MODE` pin, which is not a policy field and so is not reverse-distinguishable). The reverse map therefore makes a **documented canonical choice**: it returns the single **representative** name for each primitive pair.

| Live primitives | Derived `strategy_name` | Why |
|---|---|---|
| `classical-only` + `safe_mode=false` | `round-robin` | Representative pure-NGINX strategy (`least-connections` shares the pair) |
| `classical-only` + `safe_mode=true` | `safe-fallback` | Unambiguous — the only `safe_mode=true` pair |
| `hybrid` + `safe_mode=false` | `latency-aware` | Representative hybrid strategy; `forecast-aware` / `anomaly-aware` / `ai-hybrid` share the pair and are not reverse-distinguishable (RL_MODE is a deploy-time pin) |
| anything else (e.g. `rl-only`, `hybrid` + `safe_mode=true`) | `custom` | Valid primitives that match no documented strategy — the honest, clearer label |

So `set_strategy("forecast-aware")` followed by `get_policy()` returns `strategy_name == "latency-aware"`, **by design** — both produce identical policy primitives, and the reverse map returns the representative. Integrators that need the exact name they set should track it client-side; `strategy_name` describes the live primitive combination, not the last name posted. A subsequent slice could expose the recommended `RL_MODE` on GET (read from the rl-engine env) to disambiguate the four `hybrid` strategies — see follow-ups.

## Customer surfaces

| Surface | Detail |
|---|---|
| HTTP | `POST /api/v1/policy/strategy` on policy-manager (port 8086) — body `{"name": "<strategy>", "actor": "..."}`. `GET /api/v1/policy` gains the derived `strategy_name`. Actor precedence: `X-Actor` header, then body `actor`, then `anonymous`. |
| SDK | `client.set_strategy(name, actor=...)` (and `client.policy.set_strategy(...)`). `client.get_policy()["strategy_name"]` reads the derived field. |
| Redis | The applied change publishes the existing `smartload.policy` envelope (`PolicyUpdate`) — no new channel; the strategy endpoint reuses the policy publish path verbatim. |
| Audit | Each strategy change writes the same `policy_changes` rows as a primitive POST, with the actor recorded as `strategy:<name>:<actor>` so the change is grep-able by intent (mirrors the manual-actions `manual:<actor>:` convention). |

### Response shape — `POST /api/v1/policy/strategy`

On a real change (`status == "updated"`) or idempotent retry (`status == "no-op"`):

```json
{
  "status": "updated",
  "policy": { "operating_mode": "hybrid", "safe_mode": false, "policy_version": 42, "...": "..." },
  "changed_fields": ["operating_mode"],
  "policy_version": 42,
  "event_id": "…",
  "strategy": "ai-hybrid",
  "recommended_rl_mode": "active"
}
```

On an unknown name (HTTP 400):

```json
{
  "error": "unknown strategy 'bogus'; allowed: [...]",
  "field": "name",
  "allowed_strategies": ["ai-hybrid", "anomaly-aware", "forecast-aware", "latency-aware", "least-connections", "round-robin", "safe-fallback"]
}
```

## Implementation pointers

- New module: `services/policy-manager/strategies.py` — pure-Python mapping (`name_to_primitives`, `name_to_policy`, `recommended_rl_mode`, `primitives_to_name`, `StrategyError`). Imports `STRATEGY_PRIMITIVES` from `shared.config_loader` rather than restating it; layers `safe-fallback` on top.
- New endpoint: `services/policy-manager/app.py::post_strategy()` — translates the name and delegates to the shared `_apply_policy()` helper (refactored out of `update_policy()` so both routes share the audit + envelope flow).
- Derived field: `services/policy-manager/app.py::get_policy()` via `_with_derived_strategy()`.
- SDK: `clients/python/smartload_client/policy.py::PolicyClient.set_strategy()` + top-level `client.set_strategy()` convenience.
- Audit storage: existing `policy_changes` hypertable — no schema change (the strategy name rides in the actor field).
- Reuses the existing `smartload.policy` Redis channel and `PolicyUpdate` envelope.

## Status

- [x] `POST /api/v1/policy/strategy` — translate name → primitives, apply via the shared internal path, surface recommended `RL_MODE`
- [x] Unknown name → HTTP 400 with `field=name` and `allowed_strategies`
- [x] Derived `strategy_name` on `GET /api/v1/policy` (representative-name reverse map + `custom` fallback)
- [x] Audit row records the strategy intent (`strategy:<name>:<actor>`)
- [x] SDK `set_strategy()` + top-level convenience
- [x] 44 unit tests for translation + validation + reverse map + validator-contract (`tests/unit/policy-manager/test_strategies.py`)
- [x] 4 SDK unit tests (`tests/unit/test_smartload_client.py`)
- [x] E2E suite `tests/e2e/named-strategies/test_named_strategies.py` — set/derived-read, roundtrip property, many-to-one collapse, custom, unknown-name 400, audit round-trip
- [x] Runnable scenario `examples/scenarios/named-strategies/named_strategies_walk.py`

Open follow-ups (out of scope for this slice):

- Custom-strategy registration by name (`POST /api/v1/policy/strategies/register`) — strategies stay in the canonical table for this slice
- Strategy-specific knob exposure (per-strategy cooldown windows, etc.)
- Disambiguate the four `hybrid` strategies on GET by reading the live rl-engine `RL_MODE` env (would make `strategy_name` distinguish `ai-hybrid` from `latency-aware`)
- Operator UI strategy dropdown wired to the alias endpoint (the primitives editor stays for advanced operators)

## How to verify

Unit (no stack required):

```bash
python -m pytest tests/unit/policy-manager/test_strategies.py tests/unit/test_smartload_client.py -q
python scripts/lint-structure.py
```

End-to-end (needs the live stack):

```bash
# 1. Start the stack
docker compose up -d

# 2. Apply a strategy via the SDK
python - <<'PY'
from smartload_client import SmartLoadClient
with SmartLoadClient() as c:
    r = c.set_strategy("ai-hybrid", actor="demo")
    print(r["status"], "->", r["policy"]["operating_mode"],
          "recommended RL_MODE=", r["recommended_rl_mode"])
    print("derived strategy_name:", c.get_policy()["strategy_name"])
PY

# 3. Hit the endpoint directly
curl -X POST 'http://localhost:8086/api/v1/policy/strategy' \
  -H 'Content-Type: application/json' -H 'X-Actor: ops' \
  -d '{"name": "safe-fallback"}'

# Unknown name → 400 with the allowed list
curl -X POST 'http://localhost:8086/api/v1/policy/strategy' \
  -H 'Content-Type: application/json' \
  -d '{"name": "bogus"}'

# 4. Walk the slice end-to-end
python examples/scenarios/named-strategies/named_strategies_walk.py

# 5. Run the e2e suite
pytest tests/e2e/named-strategies/ -v
```

## Non-goals

- Custom-strategy registration / a mutable strategy table (separate slice)
- Setting `RL_MODE` at runtime via policy (it stays a deploy-time env pin)
- Per-tenant strategy scoping (Phase 2 SaaS, #129)
- Authn / authz on the endpoint (separate slice)
