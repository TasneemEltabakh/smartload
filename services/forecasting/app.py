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
  - "harmonic_residual" (default) — robust harmonic-regression + AR(1)-residual
                                  forecaster with conformal bands; pure NumPy,
                                  no artifact. Beats arima/moving_average on
                                  every load shape (forecasting-engine-bench).
  - "moving_average"            — rolling-mean baseline; no model artifact. The
                                  never-fails fallback the run loop reverts to.
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
from datetime import datetime, timezone
from flask import Flask, Response, jsonify

# Resolve shared/ across container layout (/app/shared) and dev layout
# (services/shared/ relative to this file). Same pattern as anomaly-detector.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (_HERE, os.path.dirname(_HERE)):
    if os.path.isdir(os.path.join(_cand, "shared")):
        sys.path.insert(0, _cand)
        break
from shared.contracts import publish_envelope, parse_envelope  # noqa: E402
from shared.metrics import ServiceMetrics, metrics_response     # noqa: E402
from shared import bootstrap, config                            # noqa: E402
from shared.logging_setup import install_correlation_middleware # noqa: E402
from shared.queries import FORECAST_QUERY, FORECASTS_INSERT    # noqa: E402

from runloop import (                                          # noqa: E402
    EnginePolicy,
    bootstrap_engine,
    build_forecast_row,
    build_history_from_rows,
    forecast_to_event_payload,
    policy_from_payload,
    run_engine_forecast,
    serialize_engine_state,
    should_publish,
)

app = Flask(__name__)
# Per-request correlation IDs (#143).
install_correlation_middleware(app)

# Prometheus own-metrics (#161): forecasting_cycle_*/_publish_*/_up.
METRICS = ServiceMetrics("forecasting")

# Env via the shared typed config helpers (v1.0.7av). Behaviour-preserving.
SERVICE_NAME = config.env_str("SERVICE_NAME", "forecasting")
PORT = config.env_int("PORT", 8083)
TIMESCALEDB_URL = config.timescaledb_url()
REDIS_URL = config.redis_url()

RUNLOOP_ENABLED       = config.env_bool("FORECAST_RUNLOOP_ENABLED", False)
# Promoted default forecaster (forecasting-engine-bench/REPORT.md). The run loop
# still reverts to moving_average (BASELINE_ENGINE_NAME) if this fails to load.
FORECAST_ENGINE       = config.env_str("FORECAST_ENGINE", "harmonic_residual")
POLL_INTERVAL_SECONDS = config.env_float("POLL_INTERVAL_SECONDS", 60)
WINDOW_MINUTES        = config.env_int("FORECAST_WINDOW_MINUTES", 60)

# Scaler-facing look-ahead knobs (forecasting-engine-bench REPORT.md §6.1/§6.2/§7
# "scaler-facing mode"). Defaults reproduce the accuracy-optimal single-step
# behaviour exactly, so an unset deployment is byte-identical to before:
#   FORECAST_LEAD_STEPS  — look-ahead in FORECAST_QUERY buckets (1-min each). 1 →
#       single-step forecast(); >1 → forecast_ahead(steps=…) on engines that
#       support it (harmonic_residual). Deployed value = forecast horizon /
#       bucket cadence.
#   FORECAST_FIT_WINDOW  — trailing samples the harmonic engine fits per call.
#       Empty/0 → the engine keeps its own default (accuracy-optimal long window).
#   FORECAST_ROBUST_MODE — "symmetric" (default, accuracy-optimal) | "downward"
#       (asymmetric, scaler-facing — lifts the forecast under an upward spike).
FORECAST_LEAD_STEPS   = config.env_int("FORECAST_LEAD_STEPS", 1)
_fit_window_raw       = config.env_int("FORECAST_FIT_WINDOW", 0)
FORECAST_FIT_WINDOW   = _fit_window_raw if _fit_window_raw > 0 else None
FORECAST_ROBUST_MODE  = config.env_str("FORECAST_ROBUST_MODE", "symmetric")

# Liveness threshold for /health (#163): if the loop hasn't ticked in this
# many seconds, /health flips to degraded so the silent-thread-death pattern
# becomes visible to docker healthchecks + /api/v1/status consumers.
LIVENESS_STALE_SECONDS = bootstrap.liveness_stale_seconds(POLL_INTERVAL_SECONDS)
# Back-off when the catch-all in _run_loop swallows an exception. Short
# enough to recover quickly from transient blips; long enough to avoid
# hot-looping on a persistent failure.
LOOP_RECOVERY_BACKOFF_SECONDS = 2.0

FORECAST_CHANNEL = "smartload.forecast"
POLICY_CHANNEL   = "smartload.policy"


# ── shared state ──────────────────────────────────────────────────────────────

_state_lock = threading.Lock()
_engine = None
_engine_name: str = FORECAST_ENGINE
_engine_requested: str = FORECAST_ENGINE
_engine_ready: bool = False
_engine_error: str | None = None
# Seed the live policy with the env-sourced scaler-facing look-ahead config. A
# subsequent smartload.policy reload preserves these fields (they are not policy
# payload-driven — see runloop.policy_from_payload).
_policy: EnginePolicy = EnginePolicy(
    lead_steps=FORECAST_LEAD_STEPS,
    fit_window=FORECAST_FIT_WINDOW,
    robust_mode=FORECAST_ROBUST_MODE,
)
_last_inference_monotonic: float | None = None
# Live Engines (#121) tracking — appended each cycle, read by /api/v1/engine/state.
_ticks_total: int = 0
_publishes_total: int = 0
_last_tick_at_iso: str | None = None
_last_publish_at_iso: str | None = None
_last_output_payload: dict | None = None


def _set_engine_state(bootstrap) -> None:
    global _engine, _engine_name, _engine_requested, _engine_ready, _engine_error
    _engine = bootstrap.engine
    _engine_name = bootstrap.name
    _engine_requested = bootstrap.requested
    _engine_ready = bootstrap.ready
    _engine_error = bootstrap.error


# ── connectivity checks ───────────────────────────────────────────────────────

def check_redis() -> tuple[bool, str | None]:
    return bootstrap.check_redis(REDIS_URL)


def check_timescaledb() -> tuple[bool, str | None]:
    return bootstrap.check_timescaledb(TIMESCALEDB_URL)


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
    global _last_inference_monotonic, _ticks_total, _publishes_total
    global _last_tick_at_iso, _last_publish_at_iso, _last_output_payload

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
        forecast = run_engine_forecast(engine, history, policy)
    except Exception as exc:                            # noqa: BLE001
        print(f"[{SERVICE_NAME}] engine.forecast failed: {exc}", flush=True)
        return 0

    payload = forecast_to_event_payload(forecast, model_id)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    publish = should_publish(policy)

    try:
        with db_conn.cursor() as cur:
            cur.execute(FORECASTS_INSERT, build_forecast_row(forecast, model_id, now=now))
    except Exception as exc:                                # noqa: BLE001
        print(f"[{SERVICE_NAME}] forecasts insert failed: {exc}", flush=True)

    if publish:
        with METRICS.time_publish(FORECAST_CHANNEL):
            publish_envelope(
                redis_client,
                channel=FORECAST_CHANNEL,
                source=SERVICE_NAME,
                payload=payload,
            )

    with _state_lock:
        _last_inference_monotonic = time.monotonic()
        _ticks_total += 1
        _last_tick_at_iso = now_iso
        _last_output_payload = payload
        if publish:
            _publishes_total += 1
            _last_publish_at_iso = now_iso

    return 1 if publish else 0


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
    """Forecasting daemon thread.

    #163 invariant: a single iteration's unexpected exception must NOT kill
    the loop. The outer try/except catches every Exception, logs it, sleeps
    for `LOOP_RECOVERY_BACKOFF_SECONDS`, and continues. The thread can only
    exit via the stop_event path.
    """
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

        try:
            # Drain any policy messages that arrived since the last tick —
            # never block longer than 1 s so we don't drift the poll cadence.
            message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message is not None and message.get("type") == "message":
                channel = message.get("channel")
                if isinstance(channel, bytes):
                    channel = channel.decode()
                if channel == POLICY_CHANNEL:
                    _handle_policy_message(message["data"])

            now = time.monotonic()
            if now >= next_tick:
                with METRICS.time_cycle() as _c:
                    published = _inference_cycle(db_conn, redis_client)
                    _c["outcome"] = "published" if published else "idle"
                if published:
                    print(f"[{SERVICE_NAME}] published forecast "
                          f"(model={_engine_name})", flush=True)
                next_tick = now + POLL_INTERVAL_SECONDS
        except Exception as exc:                            # noqa: BLE001
            # #163: an unexpected exception in the iteration body must not
            # kill the daemon thread. Log + back off + continue. Operators
            # see the staleness via /health's last_inference_age_seconds.
            print(f"[{SERVICE_NAME}] loop iteration raised {type(exc).__name__}: "
                  f"{exc}; continuing after {LOOP_RECOVERY_BACKOFF_SECONDS}s "
                  f"backoff", flush=True)
            if stop_event is not None and stop_event.wait(timeout=LOOP_RECOVERY_BACKOFF_SECONDS):
                break
            elif stop_event is None:
                time.sleep(LOOP_RECOVERY_BACKOFF_SECONDS)


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/metrics")
def metrics():
    body, content_type = metrics_response()
    return Response(body, mimetype=content_type)


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
        last_age = None if last is None else round(time.monotonic() - last, 2)
        body["last_inference_age_seconds"] = last_age
        # #163 liveness check: if the loop has ticked at least once and that
        # tick is older than the staleness threshold, the daemon thread has
        # either died (pre-#163 behaviour) or is failing repeatedly inside
        # the catch-all (post-#163 with a persistent fault). Either way, the
        # service is no longer doing its job and downstream consumers
        # (operator-ui /api/v1/status, docker healthcheck, Prometheus blackbox)
        # must see this.
        if last_age is not None and last_age > LIVENESS_STALE_SECONDS:
            status = "degraded"
            code = 503
            body["status"] = status
            body["loop_stale"] = True
            errors.append(
                f"run loop has not ticked in {last_age:.0f}s "
                f"(threshold {LIVENESS_STALE_SECONDS:.0f}s)"
            )
    if errors:
        body["errors"] = errors
    return jsonify(body), code


@app.route("/api/v1/engine/state", methods=["GET"])
def get_engine_state():
    """Live Engines (#121) — engine bootstrap, policy snapshot, runloop stats,
    last cycle output. Read by the operator-ui BFF for per-engine cards on the
    Live Engines page. Always returns 200; runloop-disabled is a state, not an
    error."""
    with _state_lock:
        body = serialize_engine_state(
            service=SERVICE_NAME,
            channel=FORECAST_CHANNEL,
            runloop_enabled=RUNLOOP_ENABLED,
            engine_name=_engine_name,
            engine_requested=_engine_requested,
            engine_ready=_engine_ready,
            engine_error=_engine_error,
            policy=_policy,
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
    # Production WSGI server (Flask's app.run dev server is single-threaded);
    # single process keeps the background run loop a singleton.
    from waitress import serve
    serve(app, host="0.0.0.0", port=PORT, threads=8)
