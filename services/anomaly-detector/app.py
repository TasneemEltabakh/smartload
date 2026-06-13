"""
services/anomaly-detector/app.py
─────────────────────────────────
Anomaly-detector entry point.

Phase-0 mode (default):  /health only, no inference. Backwards-compatible.
Phase-1 mode:            enabled by ANOMALY_RUNLOOP_ENABLED=true. The service
                         polls TimescaleDB on POLL_INTERVAL_SECONDS, scores
                         each backend via the configured engine, and publishes
                         AnomalyEvent envelopes to smartload.anomaly. Subscribes
                         to smartload.policy for live parameter reload.

Engine selection (ANOMALY_ENGINE env var):
  - "threshold" (default)  — rule-based baseline; no model artifact needed.
  - "isolation_forest"     — trained model from issue #101. Falls back to
                             threshold if the .pkl is missing.

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

from flask import Flask, Response, jsonify, request
from prometheus_client import Counter

# Resolve shared/ across container layout (/app/shared) and dev layout
# (services/shared/ relative to this file). Same pattern as telemetry/app.py.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (_HERE, os.path.dirname(_HERE)):
    if os.path.isdir(os.path.join(_cand, "shared")):
        sys.path.insert(0, _cand)
        break
from shared.contracts import publish_envelope, parse_envelope  # noqa: E402
from shared.metrics import ServiceMetrics, metrics_response     # noqa: E402
from shared import bootstrap, config                            # noqa: E402
from shared.logging_setup import install_correlation_middleware # noqa: E402
from shared.queries import (                                   # noqa: E402
    ANOMALY_QUERY,
    ANOMALY_DEFAULT_SERVICE,
    ANOMALY_METRIC_NAMES,
    BACKEND_HEALTH_INSERT,
)

from runloop import (                                          # noqa: E402
    EnginePolicy,
    bootstrap_engine,
    build_features_from_rows,
    policy_from_payload,
    score_to_event_payload,
    serialize_engine_state,
    should_publish,
)

app = Flask(__name__)
# Per-request correlation IDs (#143): mint/propagate X-Correlation-ID (or a W3C
# traceparent trace-id) and echo it on the response.
install_correlation_middleware(app)

# Prometheus own-metrics (#161). METRICS provides the common surface
# (anomaly_detector_cycle_*/_publish_*/_up); ISOLATE_TOTAL is the
# service-specific decision distribution.
METRICS = ServiceMetrics("anomaly_detector")
ISOLATE_TOTAL = Counter(
    "anomaly_detector_isolate_total",
    "Anomaly verdicts published, by backend and status",
    ["backend", "status"],
)

# Env via the shared typed config helpers (v1.0.7av) — consistent parsing +
# single-source defaults. Behaviour-preserving: same vars, same defaults.
SERVICE_NAME = config.env_str("SERVICE_NAME", "anomaly-detector")
PORT = config.env_int("PORT", 8082)
TIMESCALEDB_URL = config.timescaledb_url()
REDIS_URL = config.redis_url()

RUNLOOP_ENABLED         = config.env_bool("ANOMALY_RUNLOOP_ENABLED", False)
ANOMALY_ENGINE          = config.env_str("ANOMALY_ENGINE", "threshold")
POLL_INTERVAL_SECONDS   = config.env_float("POLL_INTERVAL_SECONDS", 10)
WINDOW_SECONDS          = config.env_int("ANOMALY_WINDOW_SECONDS", 60)
TELEMETRY_SERVICE       = config.env_str("ANOMALY_TELEMETRY_SERVICE", ANOMALY_DEFAULT_SERVICE)

# Liveness threshold for /health (#163). If the loop hasn't ticked in this
# many seconds, /health flips to degraded so the silent-thread-death pattern
# becomes visible.
LIVENESS_STALE_SECONDS         = bootstrap.liveness_stale_seconds(POLL_INTERVAL_SECONDS)
LOOP_RECOVERY_BACKOFF_SECONDS  = 2.0

ANOMALY_CHANNEL = "smartload.anomaly"
POLICY_CHANNEL  = "smartload.policy"


# ── shared state ──────────────────────────────────────────────────────────────

_state_lock = threading.Lock()
_engine = None
_engine_name: str = ANOMALY_ENGINE
_engine_requested: str = ANOMALY_ENGINE
_engine_ready: bool = False
_engine_error: str | None = None
_policy: EnginePolicy = EnginePolicy()
_last_inference_monotonic: float | None = None
# Live Engines (#121) tracking — appended each cycle, read by /api/v1/engine/state.
_ticks_total: int = 0
_publishes_total: int = 0
_last_tick_at_iso: str | None = None
_last_publish_at_iso: str | None = None
_last_output_payload: list[dict] | None = None


def _set_engine_state(bootstrap) -> None:
    global _engine, _engine_name, _engine_requested, _engine_ready, _engine_error
    _engine = bootstrap.engine
    _engine_name = bootstrap.name
    _engine_requested = bootstrap.requested
    _engine_ready = bootstrap.ready
    _engine_error = bootstrap.error


# ── connectivity checks ───────────────────────────────────────────────────────

def check_redis() -> tuple[bool, str | None]:
    # Delegates to the shared probe (v1.0.7aw); same (ok, detail) contract.
    return bootstrap.check_redis(REDIS_URL)


def check_timescaledb() -> tuple[bool, str | None]:
    return bootstrap.check_timescaledb(TIMESCALEDB_URL)


# ── inference cycle ───────────────────────────────────────────────────────────

def _query_features(db_conn) -> list:
    """Run ANOMALY_QUERY against the live DB and pivot rows → BackendFeatures."""
    with db_conn.cursor() as cur:
        cur.execute(
            ANOMALY_QUERY,
            (f"{WINDOW_SECONDS} seconds", TELEMETRY_SERVICE, ANOMALY_METRIC_NAMES),
        )
        rows = cur.fetchall()
    return build_features_from_rows(rows)


def _inference_cycle(db_conn, redis_client) -> int:
    """One poll cycle. Returns the number of envelopes published."""
    global _last_inference_monotonic, _ticks_total, _publishes_total
    global _last_tick_at_iso, _last_publish_at_iso, _last_output_payload

    with _state_lock:
        engine = _engine
        policy = _policy
        model_version = f"{_engine_name}"   # baseline name as version tag

    if engine is None:
        return 0

    try:
        features_list = _query_features(db_conn)
    except Exception as exc:                            # noqa: BLE001
        print(f"[{SERVICE_NAME}] DB query failed: {exc}", flush=True)
        return 0

    published = 0
    cycle_outputs: list[dict] = []
    for features in features_list:
        try:
            score = engine.score(features)
        except Exception as exc:                        # noqa: BLE001
            print(f"[{SERVICE_NAME}] engine.score failed for {features.backend_id}: {exc}", flush=True)
            continue
        cycle_outputs.append(score_to_event_payload(score, model_version))
        if not should_publish(score, policy):
            continue
        with METRICS.time_publish(ANOMALY_CHANNEL):
            publish_envelope(
                redis_client,
                channel=ANOMALY_CHANNEL,
                source=SERVICE_NAME,
                payload=score_to_event_payload(score, model_version),
            )
        ISOLATE_TOTAL.labels(backend=score.backend_id, status=score.status).inc()
        published += 1

    now_iso = datetime.now(timezone.utc).isoformat()
    with _state_lock:
        _last_inference_monotonic = time.monotonic()
        _ticks_total += 1
        _last_tick_at_iso = now_iso
        if cycle_outputs:
            _last_output_payload = cycle_outputs
        if published:
            _publishes_total += published
            _last_publish_at_iso = now_iso
    return published


# ── policy subscription ───────────────────────────────────────────────────────

def _handle_policy_message(raw) -> None:
    """Parse a smartload.policy envelope, rebuild the engine on relevant
    parameter changes, and call engine.reload() so trained models can
    re-read their artifact if they want to."""
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
    policy-derived constructor params (latency multiplier etc.) take effect
    immediately; trained-model engines should keep their loaded artifact
    cached and re-applying kwargs should be cheap."""
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
    """Anomaly-detector daemon thread.

    #163 invariant: a single iteration's unexpected exception must NOT kill
    the loop. The outer try/except catches every Exception, logs it, sleeps
    for `LOOP_RECOVERY_BACKOFF_SECONDS`, and continues. The thread can only
    exit via the stop_event path.
    """
    print(f"[{SERVICE_NAME}] run loop starting "
          f"(engine={_engine_name} ready={_engine_ready} "
          f"interval={POLL_INTERVAL_SECONDS}s)", flush=True)

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
                    print(f"[{SERVICE_NAME}] published {published} anomaly events", flush=True)
                next_tick = now + POLL_INTERVAL_SECONDS
        except Exception as exc:                            # noqa: BLE001
            # #163: an unexpected exception in the iteration body must not
            # kill the daemon thread. Log + back off + continue.
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
            body["engine_type"]     = _engine_name
            body["engine_ready"]    = _engine_ready
            body["engine_requested"] = _engine_requested
        last_age = None if last is None else round(time.monotonic() - last, 2)
        body["last_inference_age_seconds"] = last_age
        # #163 liveness check.
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
            channel=ANOMALY_CHANNEL,
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


# ── manual actions: POST /api/v1/isolate (slice #3, #123) ─────────────────────

_VALID_ISOLATE_STATUSES = ("healthy", "degraded", "unhealthy")


@app.route("/api/v1/isolate", methods=["POST"])
def post_manual_isolate():
    """Manually publish an AnomalyEvent for a specific backend.

    Body:
      {
        "backend_id": "<host:port or instance label>",
        "status":     "healthy" | "degraded" | "unhealthy",
        "actor":      <string, default "operator">,
        "reason":     <string, default "manual">
      }

    Side effects:
      - Publishes a synthetic AnomalyEvent envelope on smartload.anomaly so
        the load-balancer sidecar (T2.1, when wired) reacts as if the engine
        had produced it.
      - Writes a backend_health row (the anomaly-detector is the only writer
        of backend_health per SOT §8.5 design contract).

    Bypasses the engine's run loop and the publish gate entirely — the
    operator's intent is the signal. score is fixed at 1.0 for unhealthy /
    degraded and 0.0 for healthy.
    """
    raw = request.get_json(force=True, silent=True)
    if not isinstance(raw, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400

    backend_id = raw.get("backend_id")
    if not isinstance(backend_id, str) or not backend_id.strip():
        return jsonify({
            "error": "backend_id must be a non-empty string",
            "field": "backend_id",
        }), 400
    backend_id = backend_id.strip()

    status = raw.get("status")
    if status not in _VALID_ISOLATE_STATUSES:
        return jsonify({
            "error": f"status must be one of {list(_VALID_ISOLATE_STATUSES)}",
            "field": "status",
        }), 400

    actor = (raw.get("actor") or request.headers.get("X-Actor") or "operator")
    user_reason = (raw.get("reason") or "manual").strip() or "manual"
    audited_reason = f"manual:{actor}: {user_reason}"
    score = 0.0 if status == "healthy" else 1.0

    payload = {
        "backend_id":    backend_id,
        "status":        status,
        "score":         score,
        "model_version": f"manual:{actor}",
        "features":      {"reason": audited_reason},
    }

    redis_client = redis_lib.from_url(REDIS_URL)
    try:
        event_id = publish_envelope(
            redis_client,
            channel=ANOMALY_CHANNEL,
            source=SERVICE_NAME,
            payload=payload,
        )
    except Exception as exc:                            # noqa: BLE001
        return jsonify({"error": f"envelope publish failed: {exc}"}), 503

    try:
        db_conn = psycopg2.connect(TIMESCALEDB_URL, connect_timeout=5)
    except Exception as exc:                            # noqa: BLE001
        return jsonify({"error": f"backend_health write failed: {exc}"}), 503
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                BACKEND_HEALTH_INSERT,
                (datetime.now(timezone.utc), backend_id, status, score),
            )
        db_conn.commit()
    finally:
        db_conn.close()

    print(f"[{SERVICE_NAME}] manual isolate actor={actor} backend_id={backend_id} "
          f"status={status} reason={user_reason!r}", flush=True)

    return jsonify({
        "status":     "applied",
        "backend_id": backend_id,
        "anomaly_status": status,
        "score":      score,
        "actor":      actor,
        "reason":     audited_reason,
        "event_id":   event_id,
    })


# ── startup ───────────────────────────────────────────────────────────────────

def _start_runloop_thread() -> None:
    boot = bootstrap_engine(ANOMALY_ENGINE, _policy)
    _set_engine_state(boot)
    if not boot.ready:
        print(f"[{SERVICE_NAME}] engine {boot.requested!r} unavailable "
              f"({boot.error}); falling back to {boot.name!r}", flush=True)

    t = threading.Thread(target=_run_loop, daemon=True, name="anomaly-runloop")
    t.start()


if __name__ == "__main__":
    if RUNLOOP_ENABLED:
        _start_runloop_thread()
    else:
        print(f"[{SERVICE_NAME}] run loop disabled "
              f"(set ANOMALY_RUNLOOP_ENABLED=true to enable)", flush=True)

    print(f"[{SERVICE_NAME}] starting on port {PORT}", flush=True)
    # Production WSGI server (Flask's app.run dev server is single-threaded);
    # single process keeps the background run loop a singleton.
    from waitress import serve
    serve(app, host="0.0.0.0", port=PORT, threads=8)
