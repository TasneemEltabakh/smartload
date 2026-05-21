"""
services/forecasting/app.py
────────────────────────────
Forecasting service entry point.

Phase-0 mode (default):  /health only, no inference. Backwards-compatible.
Phase-1 mode:            enabled by FORECAST_RUNLOOP_ENABLED=true. The
                         service polls TimescaleDB on POLL_INTERVAL_SECONDS,
                         runs the configured engine on the rolling request-
                         rate history, and publishes ForecastResult envelopes
                         to smartload.forecast. Subscribes to smartload.policy
                         for live parameter reload.

Engine selection (FORECAST_ENGINE env var):
  - "moving_average" (default)  — rolling-mean baseline; no model artifact.
  - "arima"                     — trained model from issue #102. Falls back
                                  to moving_average if the .pkl is missing.

Safety:
  - The run loop is opt-in by env var so the Phase-0 stub stays the default
    until the cutover is smoke-tested per-service per issue #138.
  - If the requested engine can't load, the service runs the baseline and
    reports engine_ready=false on /health — never crashes on startup.

Health endpoint adds three engine fields when the run loop is enabled:
  engine_type, engine_ready, last_inference_age_seconds.
"""

from __future__ import annotations

import os
import sys
import threading
import time

import psycopg2
import redis as redis_lib
from flask import Flask, jsonify

# Resolve shared/ across container layout (/app/shared) and dev layout
# (services/shared/ relative to this file). Same pattern as anomaly-detector.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (_HERE, os.path.dirname(_HERE)):
    if os.path.isdir(os.path.join(_cand, "shared")):
        sys.path.insert(0, _cand)
        break
from shared.contracts import publish_envelope, parse_envelope  # noqa: E402
from shared.queries import FORECAST_QUERY                      # noqa: E402

from runloop import (                                          # noqa: E402
    EnginePolicy,
    bootstrap_engine,
    build_history_from_rows,
    forecast_to_event_payload,
    policy_from_payload,
    should_publish,
)

app = Flask(__name__)

SERVICE_NAME = os.environ.get("SERVICE_NAME", "forecasting")
PORT = int(os.environ.get("PORT", "8083"))
TIMESCALEDB_URL = os.environ.get(
    "TIMESCALEDB_URL",
    "postgresql://postgres:changeme@timescaledb:5432/smartloaddb",
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")

RUNLOOP_ENABLED       = os.environ.get("FORECAST_RUNLOOP_ENABLED", "false").lower() == "true"
FORECAST_ENGINE       = os.environ.get("FORECAST_ENGINE", "moving_average")
POLL_INTERVAL_SECONDS = float(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
WINDOW_MINUTES        = int(os.environ.get("FORECAST_WINDOW_MINUTES", "60"))

FORECAST_CHANNEL = "smartload.forecast"
POLICY_CHANNEL   = "smartload.policy"


# ── shared state ──────────────────────────────────────────────────────────────

_state_lock = threading.Lock()
_engine = None
_engine_name: str = FORECAST_ENGINE
_engine_requested: str = FORECAST_ENGINE
_engine_ready: bool = False
_engine_error: str | None = None
_policy: EnginePolicy = EnginePolicy()
_last_inference_monotonic: float | None = None


def _set_engine_state(bootstrap) -> None:
    global _engine, _engine_name, _engine_requested, _engine_ready, _engine_error
    _engine = bootstrap.engine
    _engine_name = bootstrap.name
    _engine_requested = bootstrap.requested
    _engine_ready = bootstrap.ready
    _engine_error = bootstrap.error


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

def _query_history(db_conn):
    """Run FORECAST_QUERY against the live DB and shape rows into a
    HistoryWindow. The interval is bound as a parameter per the SOT §11
    parameterisation rule."""
    with db_conn.cursor() as cur:
        cur.execute(FORECAST_QUERY, (f"{WINDOW_MINUTES} minutes",))
        rows = cur.fetchall()
    return build_history_from_rows(rows)


def _inference_cycle(db_conn, redis_client) -> int:
    """One poll cycle. Returns 1 if a forecast was published, 0 otherwise."""
    global _last_inference_monotonic

    with _state_lock:
        engine = _engine
        policy = _policy
        model_id = _engine_name

    if engine is None:
        return 0

    try:
        history = _query_history(db_conn)
    except Exception as exc:                            # noqa: BLE001
        print(f"[{SERVICE_NAME}] DB query failed: {exc}", flush=True)
        return 0

    try:
        forecast = engine.forecast(history)
    except Exception as exc:                            # noqa: BLE001
        print(f"[{SERVICE_NAME}] engine.forecast failed: {exc}", flush=True)
        return 0

    _last_inference_monotonic = time.monotonic()

    if not should_publish(policy):
        return 0

    publish_envelope(
        redis_client,
        channel=FORECAST_CHANNEL,
        source=SERVICE_NAME,
        payload=forecast_to_event_payload(forecast, model_id),
    )
    return 1


# ── policy subscription ───────────────────────────────────────────────────────

def _handle_policy_message(raw) -> None:
    """Parse a smartload.policy envelope, swap _policy, and call
    engine.reload() so trained models can re-read their artifact."""
    parsed = parse_envelope(raw, channel=POLICY_CHANNEL)
    if parsed is None:
        return
    payload, _meta = parsed

    with _state_lock:
        new_policy = policy_from_payload(payload, fallback=_policy)
        if new_policy.policy_version < _policy.policy_version:
            print(f"[{SERVICE_NAME}] ignoring policy v{new_policy.policy_version} "
                  f"(current v{_policy.policy_version}) — stale publish", flush=True)
            return
        _refresh_engine_under_lock(new_policy)


def _refresh_engine_under_lock(new_policy: EnginePolicy) -> None:
    """Replace _policy and _engine atomically with the new policy values.

    Caller must hold _state_lock. The engine is reconstructed so any
    policy-derived constructor params take effect immediately; trained-
    model engines should keep their loaded artifact cached and re-applying
    kwargs should be cheap."""
    global _policy
    _policy = new_policy
    boot = bootstrap_engine(_engine_requested, _policy)
    _set_engine_state(boot)
    try:
        boot.engine.reload()
    except Exception as exc:                            # noqa: BLE001
        print(f"[{SERVICE_NAME}] engine.reload() raised: {exc}", flush=True)


# ── run loop ──────────────────────────────────────────────────────────────────

def _run_loop(stop_event: threading.Event | None = None) -> None:
    print(f"[{SERVICE_NAME}] run loop starting "
          f"(engine={_engine_name} ready={_engine_ready} "
          f"interval={POLL_INTERVAL_SECONDS}s window={WINDOW_MINUTES}m)", flush=True)

    redis_client = redis_lib.from_url(REDIS_URL)
    pubsub = redis_client.pubsub()
    pubsub.subscribe(POLICY_CHANNEL)

    db_conn = psycopg2.connect(TIMESCALEDB_URL)
    db_conn.autocommit = True

    next_tick = time.monotonic()

    while True:
        if stop_event is not None and stop_event.is_set():
            break

        # Drain any policy messages that arrived since the last tick — never
        # block longer than 1 s so we don't drift the poll cadence.
        message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        if message is not None and message.get("type") == "message":
            channel = message.get("channel")
            if isinstance(channel, bytes):
                channel = channel.decode()
            if channel == POLICY_CHANNEL:
                _handle_policy_message(message["data"])

        now = time.monotonic()
        if now >= next_tick:
            published = _inference_cycle(db_conn, redis_client)
            if published:
                print(f"[{SERVICE_NAME}] published forecast (model={_engine_name})", flush=True)
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
    }
    if RUNLOOP_ENABLED:
        with _state_lock:
            last = _last_inference_monotonic
            body["engine_type"]      = _engine_name
            body["engine_ready"]     = _engine_ready
            body["engine_requested"] = _engine_requested
        body["last_inference_age_seconds"] = (
            None if last is None else round(time.monotonic() - last, 2)
        )
    if errors:
        body["errors"] = errors
    return jsonify(body), code


@app.route("/")
def index():
    return jsonify({"service": SERVICE_NAME, "status": "running"})


# ── startup ───────────────────────────────────────────────────────────────────

def _start_runloop_thread() -> None:
    boot = bootstrap_engine(FORECAST_ENGINE, _policy)
    _set_engine_state(boot)
    if not boot.ready:
        print(f"[{SERVICE_NAME}] engine {boot.requested!r} unavailable "
              f"({boot.error}); falling back to {boot.name!r}", flush=True)

    t = threading.Thread(target=_run_loop, daemon=True, name="forecast-runloop")
    t.start()


if __name__ == "__main__":
    if RUNLOOP_ENABLED:
        _start_runloop_thread()
    else:
        print(f"[{SERVICE_NAME}] run loop disabled "
              f"(set FORECAST_RUNLOOP_ENABLED=true to enable)", flush=True)

    print(f"[{SERVICE_NAME}] starting on port {PORT}", flush=True)
    app.run(host="0.0.0.0", port=PORT)
