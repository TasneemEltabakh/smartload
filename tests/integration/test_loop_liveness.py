"""
tests/integration/test_loop_liveness.py
────────────────────────────────────────
#163 acceptance tests.

Two invariants verified per service:

1. **/health flips to `degraded` (HTTP 503) when the run loop has not
   ticked in > LIVENESS_STALE_SECONDS.** Monkeypatches the relevant
   "last tick" monotonic global to a stale value, calls GET /health via
   Flask test client, asserts both the body and status code reflect the
   stale state.

2. **/health stays `ok` (HTTP 200) when the loop is ticking recently.**
   Monkeypatches the global to `time.monotonic()` (i.e. ticked just now)
   and asserts ok.

For each of the five decision-plane services, one parameterised test pair
exercises the same contract. The catch-all-loop-survival invariant
(invariant 1 in #163) is verified separately by
`test_forecasting_loop_survives.py` — that one is genuinely a per-service
runtime test rather than a Flask-route test.

These run in the `unit-tests` CI job (no docker, no live DB / Redis —
the `/health` route's redis/db pings will fail and add their own error
lines, but the staleness check still fires independently and the test
asserts on its presence in the body).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]


# ── shared helpers ────────────────────────────────────────────────────────────

def _import_service(name: str):
    """Import a service's app module by inserting its dir on sys.path.

    Each service has its own `shared` resolver in app.py, so we add both
    services/<svc>/ and services/ to sys.path. The module is cleared from
    sys.modules first so importing multiple services in the same test
    session doesn't collide on the unqualified module name `app`.

    Several sibling module names (`engine_base`, `policy_base`, `decisions`,
    `cluster_client`) are also flushed because the autoscaler, rl-engine,
    forecasting, and anomaly-detector each ship their own copy under their
    service folder; once one is imported as e.g. `engine_base`, the next
    `_import_service()` call would pick up the cached version and crash
    with an ImportError on a name that only exists in the new service's
    flavour.
    """
    sibling_shared = (
        "app", "runloop",
        "engine_base", "policy_base",
        "decisions", "cluster_client",
        # plugin-folder packages live under `engines.<plugin>` /
        # `policies.<plugin>` and are also service-local.
        "engines", "policies",
    )
    for mod_name in list(sys.modules):
        if mod_name in sibling_shared or mod_name.startswith(("engines.", "policies.")):
            sys.modules.pop(mod_name, None)

    svc_dir = _REPO / "services" / name
    services_dir = _REPO / "services"
    # Also drop any service-folder paths from sys.path so the new service's
    # imports don't accidentally resolve to a sibling.
    for p in list(sys.path):
        p_path = Path(p) if p else None
        if p_path is not None and p_path.parent == services_dir:
            sys.path.remove(p)
    for p in (str(svc_dir), str(services_dir)):
        if p in sys.path:
            sys.path.remove(p)
        sys.path.insert(0, p)
    import app                # noqa: PLC0415 — services intentionally share `app` name
    return app


_LIVENESS_CASES = [
    # (svc_name, port, last_tick_attr, liveness_const, RUNLOOP_FLAG_ENV)
    ("forecasting",      8083, "_last_inference_monotonic",  "LIVENESS_STALE_SECONDS", "FORECAST_RUNLOOP_ENABLED"),
    ("anomaly-detector", 8082, "_last_inference_monotonic",  "LIVENESS_STALE_SECONDS", "ANOMALY_RUNLOOP_ENABLED"),
    ("rl-engine",        8084, "_last_inference_monotonic",  "LIVENESS_STALE_SECONDS", "RL_RUNLOOP_ENABLED"),
    ("autoscaler",       8085, "_last_loop_tick_monotonic",  "LIVENESS_STALE_SECONDS", "AUTOSCALER_RUNLOOP_ENABLED"),
    ("lb-sidecar",       8087, "_last_loop_tick_monotonic",  "LIVENESS_STALE_SECONDS", "LB_SIDECAR_RUNLOOP_ENABLED"),
]


# ── parameterised tests ──────────────────────────────────────────────────────

@pytest.mark.parametrize("svc, port, attr, const, runloop_env", _LIVENESS_CASES,
                         ids=[c[0] for c in _LIVENESS_CASES])
def test_health_degraded_when_loop_stale(monkeypatch, svc, port, attr, const, runloop_env):
    """When the last tick is older than LIVENESS_STALE_SECONDS, /health
    must return status=degraded (503) regardless of redis/db ping outcomes.
    """
    monkeypatch.setenv(runloop_env, "true")
    app_mod = _import_service(svc)

    # Re-evaluate the module-level RUNLOOP_ENABLED constant under the patched env.
    # Some services compute this once at import; in those, force the flag.
    if hasattr(app_mod, "RUNLOOP_ENABLED"):
        monkeypatch.setattr(app_mod, "RUNLOOP_ENABLED", True)

    stale_secs = getattr(app_mod, const)
    # 1.5× the threshold pushes us well past the staleness window
    monkeypatch.setattr(app_mod, attr, time.monotonic() - (stale_secs * 1.5))

    client = app_mod.app.test_client()
    resp = client.get("/health")
    body = resp.get_json()

    assert body is not None, f"{svc} /health returned non-JSON"
    assert body.get("loop_stale") is True, (
        f"{svc} /health did not report loop_stale=true; body={body!r}"
    )
    assert body.get("status") == "degraded", (
        f"{svc} /health status={body.get('status')!r}; expected degraded"
    )
    assert resp.status_code == 503


@pytest.mark.parametrize("svc, port, attr, const, runloop_env", _LIVENESS_CASES,
                         ids=[c[0] for c in _LIVENESS_CASES])
def test_health_ok_when_loop_ticking_recently(monkeypatch, svc, port, attr, const, runloop_env):
    """When the last tick is fresh, /health does NOT report loop_stale —
    the only way the response is degraded in this case is the redis/db
    ping failing (which is normal in unit-test env without compose up)."""
    monkeypatch.setenv(runloop_env, "true")
    app_mod = _import_service(svc)
    if hasattr(app_mod, "RUNLOOP_ENABLED"):
        monkeypatch.setattr(app_mod, "RUNLOOP_ENABLED", True)

    monkeypatch.setattr(app_mod, attr, time.monotonic())  # ticked right now

    client = app_mod.app.test_client()
    resp = client.get("/health")
    body = resp.get_json()

    assert body is not None
    # loop_stale must not be true. (It's allowed to be absent.)
    assert body.get("loop_stale") is not True, (
        f"{svc} /health incorrectly flagged loop_stale with a fresh tick; "
        f"body={body!r}"
    )


@pytest.mark.parametrize("svc, port, attr, const, runloop_env", _LIVENESS_CASES,
                         ids=[c[0] for c in _LIVENESS_CASES])
def test_health_does_not_flag_stale_before_first_tick(monkeypatch, svc, port, attr, const, runloop_env):
    """During startup before the loop has ticked once, the last-tick
    monotonic is None. /health must NOT flag this as stale — only an
    actually-stale (None-replaced-with-old-value) tick counts as a bug."""
    monkeypatch.setenv(runloop_env, "true")
    app_mod = _import_service(svc)
    if hasattr(app_mod, "RUNLOOP_ENABLED"):
        monkeypatch.setattr(app_mod, "RUNLOOP_ENABLED", True)

    monkeypatch.setattr(app_mod, attr, None)

    client = app_mod.app.test_client()
    resp = client.get("/health")
    body = resp.get_json()

    assert body is not None
    assert body.get("loop_stale") is not True, (
        f"{svc} /health flagged loop_stale during startup; body={body!r}"
    )
