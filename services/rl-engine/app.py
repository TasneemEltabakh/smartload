"""
services/rl-engine/app.py
──────────────────────────
RL-engine entry point.

Phase-0 mode (default):  /health only, no recommendations published.
Phase-1 mode:            enabled by RL_RUNLOOP_ENABLED=true. The service
                         polls TimescaleDB on POLL_INTERVAL_SECONDS, runs
                         the configured policy on the per-backend state,
                         and publishes RoutingRecommendation envelopes
                         to smartload.routing. Subscribes to
                         smartload.policy for live parameter reload.

Policy selection (RL_POLICY env var):
  - "random_shadow" (default)  — uniform-random scores; always reports
                                  mode="shadow"; no model artifact needed.
  - "ppo"                      — trained PPO policy from issue #27.
                                  Falls back to random_shadow if the
                                  policy.zip is missing.

Operator mode pin (RL_MODE env var):
  - "shadow" (default)  — published `mode` field is always "shadow",
                          regardless of what the policy returned.
  - "active"            — when paired with a policy that itself returns
                          mode="active", the LB sidecar will apply the
                          weights. safe_mode=true in operating policy
                          forces shadow regardless.

Safety:
  - The run loop is opt-in via RL_RUNLOOP_ENABLED=false default so the
    Phase-0 stub stays the default until the cutover is smoke-tested.
  - If the requested policy can't load, the service runs random_shadow
    and reports policy_ready=false on /health — never crashes on startup.

Health endpoint adds four engine fields when the run loop is enabled:
  policy_type, policy_ready, last_inference_age_seconds, rl_mode.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone

import psycopg2
import redis as redis_lib
from flask import Flask, jsonify

# Resolve shared/ across container layout (/app/shared) and dev layout
# (services/shared/ relative to this file). Same pattern as siblings.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (_HERE, os.path.dirname(_HERE)):
    if os.path.isdir(os.path.join(_cand, "shared")):
        sys.path.insert(0, _cand)
        break
from shared.contracts import publish_envelope, parse_envelope  # noqa: E402
from shared.queries import RL_STATE_QUERY                      # noqa: E402

from runloop import (                                          # noqa: E402
    EnginePolicy,
    action_to_event_payload,
    bootstrap_policy,
    build_state_from_rows,
    effective_mode,
    policy_from_payload,
    serialize_engine_state,
    should_publish,
)

app = Flask(__name__)

SERVICE_NAME = os.environ.get("SERVICE_NAME", "rl-engine")
PORT = int(os.environ.get("PORT", "8084"))
TIMESCALEDB_URL = os.environ.get(
    "TIMESCALEDB_URL",
    "postgresql://postgres:changeme@timescaledb:5432/smartloaddb",
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")

RUNLOOP_ENABLED       = os.environ.get("RL_RUNLOOP_ENABLED", "false").lower() == "true"
RL_POLICY             = os.environ.get("RL_POLICY", "random_shadow")
RL_MODE               = os.environ.get("RL_MODE", "shadow")
RL_SERVICE            = os.environ.get("RL_SERVICE", "load-balancer")
POLL_INTERVAL_SECONDS = float(os.environ.get("POLL_INTERVAL_SECONDS", "5"))
WINDOW_SECONDS        = int(os.environ.get("RL_WINDOW_SECONDS", "30"))
POLICY_MANAGER_URL    = os.environ.get("POLICY_MANAGER_URL", "http://policy-manager:8086")

ROUTING_CHANNEL = "smartload.routing"
POLICY_CHANNEL  = "smartload.policy"
ANOMALY_CHANNEL = "smartload.anomaly"


# ── shared state ──────────────────────────────────────────────────────────────

_state_lock = threading.Lock()
_policy = None
_policy_name: str = RL_POLICY
_policy_requested: str = RL_POLICY
_policy_ready: bool = False
_policy_error: str | None = None
_engine_policy: EnginePolicy = EnginePolicy()
_last_inference_monotonic: float | None = None
# Health verdicts received from smartload.anomaly (SOT §9 health ownership).
# backend_id → status ("healthy" | "degraded" | "unhealthy").
_anomaly_health: dict[str, str] = {}

# Live Engines (#121) tracking — appended each cycle, read by /api/v1/engine/state.
_ticks_total: int = 0
_publishes_total: int = 0
_last_tick_at_iso: str | None = None
_last_publish_at_iso: str | None = None
_last_output_payload: dict | None = None


def _set_policy_state(bootstrap) -> None:
    global _policy, _policy_name, _policy_requested, _policy_ready, _policy_error
    _policy = bootstrap.policy
    _policy_name = bootstrap.name
    _policy_requested = bootstrap.requested
    _policy_error = bootstrap.error
    # PPOPolicy (and future ML plugins) expose a policy_ready property that
    # reflects whether the model artifact loaded successfully, independent of
    # whether __init__ raised.  Use it when available; fall back to bootstrap.ready.
    plugin_ready = getattr(bootstrap.policy, "policy_ready", None)
    _policy_ready = plugin_ready if plugin_ready is not None else bootstrap.ready


# ── connectivity checks ───────────────────────────────────────────────────────

def check_redis() -> tuple[bool, str | None]:
    try:
        redis_lib.from_url(REDIS_URL, socket_connect_timeout=3).ping()
        return True, None
    except Exception as exc:                            # noqa: BLE001
        return False, str(exc)


def check_timescaledb() -> tuple[bool, str | None]:
    try:
        psycopg2.connect(TIMESCALEDB_URL, connect_timeout=5).close()
        return True, None
    except Exception as exc:                            # noqa: BLE001
        return False, str(exc)


# ── inference cycle ───────────────────────────────────────────────────────────

def _query_state(db_conn, health_snapshot: dict[str, str]) -> list:
    """Run RL_STATE_QUERY against the live DB and shape rows into
    list[BackendState]. Both the interval and service filter are bound as
    parameters per SOT §11. health_snapshot (from smartload.anomaly) takes
    precedence over local classification per SOT §9 health-ownership rule."""
    with db_conn.cursor() as cur:
        cur.execute(RL_STATE_QUERY, (f"{WINDOW_SECONDS} seconds", RL_SERVICE))
        rows = cur.fetchall()
    return build_state_from_rows(rows, anomaly_health=health_snapshot)


def _inference_cycle(db_conn, redis_client) -> int:
    """One poll cycle. Returns 1 if a recommendation was published, 0 otherwise."""
    global _last_inference_monotonic, _ticks_total, _publishes_total
    global _last_tick_at_iso, _last_publish_at_iso, _last_output_payload

    with _state_lock:
        pol_instance   = _policy
        eng_policy     = _engine_policy
        health_snapshot = dict(_anomaly_health)  # consistent snapshot for this cycle

    if pol_instance is None:
        return 0

    try:
        state = _query_state(db_conn, health_snapshot)
    except Exception as exc:                            # noqa: BLE001
        print(f"[{SERVICE_NAME}] DB query failed: {exc}", flush=True)
        return 0

    now_iso = datetime.now(timezone.utc).isoformat()

    if not should_publish(state):
        with _state_lock:
            _last_inference_monotonic = time.monotonic()
            _ticks_total += 1
            _last_tick_at_iso = now_iso
        return 0

    try:
        action = pol_instance.act(state)
    except Exception as exc:                            # noqa: BLE001
        print(f"[{SERVICE_NAME}] policy.act failed: {exc}", flush=True)
        return 0

    mode = effective_mode(action.mode, RL_MODE, eng_policy)
    payload = action_to_event_payload(action, mode, eng_policy.policy_version)

    publish_envelope(
        redis_client,
        channel=ROUTING_CHANNEL,
        source=SERVICE_NAME,
        payload=payload,
    )

    with _state_lock:
        _last_inference_monotonic = time.monotonic()
        _ticks_total += 1
        _publishes_total += 1
        _last_tick_at_iso = now_iso
        _last_publish_at_iso = now_iso
        _last_output_payload = payload

    return 1


# ── policy subscription ───────────────────────────────────────────────────────

def _handle_policy_message(raw) -> None:
    """Parse a smartload.policy envelope, swap _engine_policy, and rebuild
    the RL policy so any constructor-derived params (exploration rate,
    confidence threshold) take effect immediately."""
    parsed = parse_envelope(raw, channel=POLICY_CHANNEL)
    if parsed is None:
        return
    payload, _meta = parsed

    with _state_lock:
        new_policy = policy_from_payload(payload, fallback=_engine_policy)
        if new_policy.policy_version < _engine_policy.policy_version:
            print(f"[{SERVICE_NAME}] ignoring policy v{new_policy.policy_version} "
                  f"(current v{_engine_policy.policy_version}) — stale publish",
                  flush=True)
            return
        _refresh_policy_under_lock(new_policy)


def _handle_anomaly_message(raw) -> None:
    """Update _anomaly_health from a smartload.anomaly envelope (SOT §9)."""
    parsed = parse_envelope(raw, channel=ANOMALY_CHANNEL)
    if parsed is None:
        return
    payload, _meta = parsed
    backend_id = payload.get("backend_id")
    status     = payload.get("status")
    if backend_id and status in ("healthy", "degraded", "unhealthy"):
        with _state_lock:
            _anomaly_health[backend_id] = status


def _refresh_policy_under_lock(new_policy: EnginePolicy) -> None:
    """Replace _engine_policy and _policy atomically with the new values.

    Caller must hold _state_lock. The policy is reconstructed so any
    policy-derived constructor params take effect immediately; trained
    policies should keep their loaded artifact cached and re-applying
    kwargs should be cheap."""
    global _engine_policy
    _engine_policy = new_policy
    boot = bootstrap_policy(_policy_requested, _engine_policy)
    _set_policy_state(boot)
    try:
        boot.policy.reload()
    except Exception as exc:                            # noqa: BLE001
        print(f"[{SERVICE_NAME}] policy.reload() raised: {exc}", flush=True)


# ── run loop ──────────────────────────────────────────────────────────────────

def _run_loop(stop_event: threading.Event | None = None) -> None:
    print(f"[{SERVICE_NAME}] run loop starting "
          f"(policy={_policy_name} ready={_policy_ready} "
          f"rl_mode={RL_MODE} interval={POLL_INTERVAL_SECONDS}s)", flush=True)

    redis_client = redis_lib.from_url(REDIS_URL)
    pubsub = redis_client.pubsub()
    pubsub.subscribe(POLICY_CHANNEL)
    pubsub.subscribe(ANOMALY_CHANNEL)

    db_conn = psycopg2.connect(TIMESCALEDB_URL)
    db_conn.autocommit = True

    next_tick = time.monotonic()

    while True:
        if stop_event is not None and stop_event.is_set():
            break

        # Drain one message per iteration — never block longer than 1 s so we
        # don't drift the poll cadence.
        message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        if message is not None and message.get("type") == "message":
            channel = message.get("channel")
            if isinstance(channel, bytes):
                channel = channel.decode()
            if channel == POLICY_CHANNEL:
                _handle_policy_message(message["data"])
            elif channel == ANOMALY_CHANNEL:
                _handle_anomaly_message(message["data"])

        now = time.monotonic()
        if now >= next_tick:
            published = _inference_cycle(db_conn, redis_client)
            if published:
                print(f"[{SERVICE_NAME}] published routing recommendation "
                      f"(policy={_policy_name})", flush=True)
            next_tick = now + POLL_INTERVAL_SECONDS


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    redis_ok, redis_err = check_redis()
    db_ok, db_err = check_timescaledb()
    errors = [e for e in [redis_err, db_err] if e]
    status = "ok" if (redis_ok and db_ok) else "degraded"
    code = 200 if status == "ok" else 503

    body: dict = {
        "status": status,
        "service": SERVICE_NAME,
        "redis": redis_ok,
        "timescaledb": db_ok,
        "rl_mode": RL_MODE,
    }
    if RUNLOOP_ENABLED:
        with _state_lock:
            last = _last_inference_monotonic
            body["policy_type"]      = _policy_name
            body["policy_requested"] = _policy_requested
            body["policy_ready"]     = _policy_ready
        body["last_inference_age_seconds"] = (
            None if last is None else round(time.monotonic() - last, 2)
        )
    if errors:
        body["errors"] = errors
    return jsonify(body), code


@app.route("/api/v1/engine/state", methods=["GET"])
def get_engine_state():
    """Live Engines (#121) — policy bootstrap, operating-policy snapshot,
    runloop stats, last cycle output, env-pinned rl_mode. Read by the
    operator-ui BFF for per-engine cards. Always returns 200; runloop-disabled
    is a state, not an error."""
    with _state_lock:
        body = serialize_engine_state(
            service=SERVICE_NAME,
            channel=ROUTING_CHANNEL,
            runloop_enabled=RUNLOOP_ENABLED,
            policy_name=_policy_name,
            policy_requested=_policy_requested,
            policy_ready=_policy_ready,
            policy_error=_policy_error,
            engine_policy=_engine_policy,
            rl_mode_env=RL_MODE,
            ticks_total=_ticks_total,
            publishes_total=_publishes_total,
            last_tick_at=_last_tick_at_iso,
            last_publish_at=_last_publish_at_iso,
            last_tick_monotonic=_last_inference_monotonic,
            last_output=_last_output_payload,
        )
    return jsonify(body)


@app.route("/")
def index():
    return jsonify({"service": SERVICE_NAME, "status": "running", "rl_mode": RL_MODE})


# ── startup ───────────────────────────────────────────────────────────────────

def _pull_initial_policy() -> None:
    """Pull current policy from Policy Manager on startup (SOT §11 — pull on startup)."""
    url = f"{POLICY_MANAGER_URL}/api/v1/policy"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        payload = data.get("payload") or data
        new_policy = policy_from_payload(payload, fallback=_engine_policy)
        if new_policy.policy_version >= _engine_policy.policy_version:
            with _state_lock:
                _refresh_policy_under_lock(new_policy)
            print(f"[{SERVICE_NAME}] pulled initial policy v{new_policy.policy_version} "
                  f"from {url}", flush=True)
    except Exception as exc:                   # noqa: BLE001
        print(f"[{SERVICE_NAME}] startup policy pull failed ({exc}); "
              "using defaults", flush=True)


def _start_runloop_thread() -> None:
    boot = bootstrap_policy(RL_POLICY, _engine_policy)
    _set_policy_state(boot)
    if not boot.ready:
        print(f"[{SERVICE_NAME}] policy {boot.requested!r} unavailable "
              f"({boot.error}); falling back to {boot.name!r}", flush=True)

    t = threading.Thread(target=_run_loop, daemon=True, name="rl-runloop")
    t.start()


if __name__ == "__main__":
    if RUNLOOP_ENABLED:
        _pull_initial_policy()
        _start_runloop_thread()
    else:
        print(f"[{SERVICE_NAME}] run loop disabled "
              f"(set RL_RUNLOOP_ENABLED=true to enable)", flush=True)

    print(f"[{SERVICE_NAME}] starting on port {PORT} (mode={RL_MODE})", flush=True)
    app.run(host="0.0.0.0", port=PORT)
