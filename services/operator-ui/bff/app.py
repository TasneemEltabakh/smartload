"""
services/operator-ui/bff/app.py
────────────────────────────────
Operator UI backend-for-frontend (BFF).

Responsibilities:
  - Aggregate /health from every SmartLoad service for the Home page
  - Proxy /api/ui/policy and /api/ui/audit/* to policy-manager / autoscaler
  - Proxy manual actions (scale, isolate) to autoscaler / anomaly-detector
  - Live Engines (#121): subscribe to smartload.{anomaly,forecast,routing,scale},
    expose /api/ui/engines/snapshot (per-engine state) + /api/ui/engines/stream
    (SSE feed of recent + live envelopes)
  - Serve Swagger UI at /api/docs reading docs/openapi/smartload-v1.yaml
  - Serve the React build at / (production) — Vite dev server handles dev

Out of scope (separate issues):
  - Operator login (#125)
  - Embedded charts (#131)
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import redis as redis_lib
from flask import Flask, Response, jsonify, request, send_from_directory

# Make sibling modules (engines.py) importable when this file is loaded as
# `bff.app` by gunicorn (in which case the bff/ folder isn't on sys.path
# by default). Same dual-mode pattern as the AI services use for shared/.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from engines import (  # noqa: E402
    ENGINE_SERVICES,
    HEARTBEAT_INTERVAL_SECONDS,
    EngineEventBus,
    RingBuffer,
    format_sse_event,
    format_sse_heartbeat,
    subscriber_loop,
)

try:
    from flask_swagger_ui import get_swaggerui_blueprint
except ImportError:  # pragma: no cover
    get_swaggerui_blueprint = None  # type: ignore


SERVICE_NAME = os.environ.get("SERVICE_NAME", "operator-ui")
PORT         = int(os.environ.get("PORT", "8090"))
REDIS_URL    = os.environ.get("REDIS_URL", "redis://redis:6379")

# Service-name → base URL. Defaults match docker-compose service names + ports.
SERVICE_URLS: dict[str, str] = {
    "policy-manager":   os.environ.get("POLICY_MANAGER_URL",   "http://policy-manager:8086"),
    "autoscaler":       os.environ.get("AUTOSCALER_URL",       "http://autoscaler:8085"),
    "telemetry":        os.environ.get("TELEMETRY_URL",        "http://telemetry:8081"),
    "anomaly-detector": os.environ.get("ANOMALY_DETECTOR_URL", "http://anomaly-detector:8082"),
    "forecasting":      os.environ.get("FORECASTING_URL",      "http://forecasting:8083"),
    "rl-engine":        os.environ.get("RL_ENGINE_URL",        "http://rl-engine:8084"),
    "load-balancer":    os.environ.get("LOAD_BALANCER_URL",    "http://load-balancer:80"),
}

WEB_DIST = os.environ.get(
    "WEB_DIST",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web", "dist"),
)
OPENAPI_PATH = os.environ.get(
    "OPENAPI_PATH",
    "/app/openapi/smartload-v1.yaml",
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [operator-ui] %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("operator-ui")

app = Flask(
    __name__,
    static_folder=os.path.join(WEB_DIST, "assets"),
    static_url_path="/assets",
)
# Mount only `/assets/*` as static. Without this scoping, Flask's default
# `static_url_path=""` puts the auto-static route at `/<filename>`, which
# intercepts SPA paths like `/policy` and `/audit` with a 404 before the
# SPA fallback (serve_spa) can return index.html for React Router to
# resolve client-side.

# Single shared HTTP client for upstream calls.
_http = httpx.Client(timeout=httpx.Timeout(5.0, connect=2.0))


# ── Swagger UI ────────────────────────────────────────────────────────────────

if get_swaggerui_blueprint is not None:
    swagger_bp = get_swaggerui_blueprint(
        "/api/docs",
        "/api/openapi.yaml",
        config={"app_name": "SmartLoad API"},
    )
    app.register_blueprint(swagger_bp, url_prefix="/api/docs")


@app.route("/api/openapi.yaml")
def serve_openapi():
    """Serve the canonical OpenAPI spec for Swagger UI to consume."""
    if not os.path.isfile(OPENAPI_PATH):
        return jsonify({"error": f"openapi spec not found: {OPENAPI_PATH}"}), 404
    return send_from_directory(
        os.path.dirname(OPENAPI_PATH),
        os.path.basename(OPENAPI_PATH),
        mimetype="application/x-yaml",
    )


# ── /api/ui/health: aggregate every service ───────────────────────────────────

def _fetch_health(name: str, base_url: str) -> tuple[str, dict]:
    url = f"{base_url.rstrip('/')}/health"
    try:
        r = _http.get(url)
        try:
            body = r.json()
        except (ValueError, TypeError):
            body = {"raw": r.text[:200]}
        return name, {
            "status_code": r.status_code,
            "status":      body.get("status", "unknown"),
            "redis":       body.get("redis"),
            "timescaledb": body.get("timescaledb"),
            "extra":       {k: v for k, v in body.items()
                            if k not in {"status", "redis", "timescaledb", "service"}},
        }
    except Exception as exc:
        return name, {
            "status_code": None,
            "status":      "unreachable",
            "error":       str(exc),
        }


@app.route("/api/ui/health", methods=["GET"])
def ui_health():
    """Fan out /health to every service in parallel and return a summary map."""
    with ThreadPoolExecutor(max_workers=len(SERVICE_URLS)) as pool:
        results = list(pool.map(
            lambda kv: _fetch_health(*kv),
            SERVICE_URLS.items(),
        ))
    summary = dict(results)
    any_unhealthy = any(
        v.get("status") not in {"ok"} for v in summary.values()
    )
    return jsonify({
        "all_ok": not any_unhealthy,
        "services": summary,
    })


# ── /api/ui/policy: proxy to policy-manager ───────────────────────────────────

@app.route("/api/ui/policy", methods=["GET"])
def ui_policy_get():
    upstream = SERVICE_URLS["policy-manager"]
    try:
        r = _http.get(f"{upstream}/api/v1/policy")
    except Exception as exc:
        return jsonify({"error": f"upstream unreachable: {exc}"}), 502
    return (r.text, r.status_code, {"Content-Type": "application/json"})


@app.route("/api/ui/policy", methods=["POST"])
def ui_policy_post():
    upstream = SERVICE_URLS["policy-manager"]
    body = request.get_data(as_text=True) or "{}"
    headers = {"Content-Type": "application/json"}
    actor = request.headers.get("X-Actor") or "operator-ui"
    headers["X-Actor"] = actor
    try:
        r = _http.post(f"{upstream}/api/v1/policy", content=body, headers=headers)
    except Exception as exc:
        return jsonify({"error": f"upstream unreachable: {exc}"}), 502
    return (r.text, r.status_code, {"Content-Type": "application/json"})


@app.route("/api/ui/audit/policy", methods=["GET"])
def ui_policy_audit():
    upstream = SERVICE_URLS["policy-manager"]
    limit = request.args.get("limit", "50")
    try:
        r = _http.get(f"{upstream}/api/v1/audit/policy", params={"limit": limit})
    except Exception as exc:
        return jsonify({"error": f"upstream unreachable: {exc}"}), 502
    return (r.text, r.status_code, {"Content-Type": "application/json"})


@app.route("/api/ui/audit/scaling", methods=["GET"])
def ui_scaling_audit():
    """Proxy to autoscaler's GET /api/v1/audit/scaling — slice #2 (#122).

    The scaling audit stream is owned by the autoscaler service (it's the
    writer of scaling_events), so this proxy points at a different upstream
    than ui_policy_audit. The frontend treats both as one Audit page; the
    BFF is what normalises the two origins into one URL space."""
    upstream = SERVICE_URLS["autoscaler"]
    limit = request.args.get("limit", "50")
    try:
        r = _http.get(f"{upstream}/api/v1/audit/scaling", params={"limit": limit})
    except Exception as exc:
        return jsonify({"error": f"upstream unreachable: {exc}"}), 502
    return (r.text, r.status_code, {"Content-Type": "application/json"})


# ── manual actions (slice #3, #123) ───────────────────────────────────────────

@app.route("/api/ui/scale", methods=["POST"])
def ui_manual_scale():
    """Proxy to autoscaler's POST /api/v1/scale — slice #3 manual actions."""
    upstream = SERVICE_URLS["autoscaler"]
    body = request.get_data(as_text=True) or "{}"
    headers = {"Content-Type": "application/json"}
    actor = request.headers.get("X-Actor") or "operator-ui"
    headers["X-Actor"] = actor
    try:
        r = _http.post(f"{upstream}/api/v1/scale", content=body, headers=headers)
    except Exception as exc:
        return jsonify({"error": f"upstream unreachable: {exc}"}), 502
    return (r.text, r.status_code, {"Content-Type": "application/json"})


@app.route("/api/ui/isolate", methods=["POST"])
def ui_manual_isolate():
    """Proxy to anomaly-detector's POST /api/v1/isolate — slice #3 manual actions."""
    upstream = SERVICE_URLS["anomaly-detector"]
    body = request.get_data(as_text=True) or "{}"
    headers = {"Content-Type": "application/json"}
    actor = request.headers.get("X-Actor") or "operator-ui"
    headers["X-Actor"] = actor
    try:
        r = _http.post(f"{upstream}/api/v1/isolate", content=body, headers=headers)
    except Exception as exc:
        return jsonify({"error": f"upstream unreachable: {exc}"}), 502
    return (r.text, r.status_code, {"Content-Type": "application/json"})


# ── Live Engines (#121): snapshot + SSE stream ────────────────────────────────

_engines_buf = RingBuffer()
_engines_bus = EngineEventBus()
_engines_thread_started = False
_engines_thread_lock = threading.Lock()


def _start_engines_subscriber() -> None:
    """Spawn the Redis subscriber thread exactly once per process. Called on
    first /api/ui/engines/* request so unit tests that import this module
    without a live Redis don't pay the connection cost."""
    global _engines_thread_started
    with _engines_thread_lock:
        if _engines_thread_started:
            return
        _engines_thread_started = True
        t = threading.Thread(
            target=subscriber_loop,
            kwargs={
                "redis_client_factory": lambda: redis_lib.from_url(REDIS_URL),
                "buf": _engines_buf,
                "bus": _engines_bus,
                "log": lambda msg: log.info(msg),
            },
            daemon=True,
            name="engines-subscriber",
        )
        t.start()
        log.info("engines: subscriber thread started")


def _fetch_engine_state(name: str, base_url: str) -> tuple[str, dict]:
    """Pull one AI service's /api/v1/engine/state. Same shape as
    _fetch_health — failures collapse to a status object the UI can render
    without exploding."""
    url = f"{base_url.rstrip('/')}/api/v1/engine/state"
    try:
        r = _http.get(url)
        try:
            body = r.json()
        except (ValueError, TypeError):
            return name, {"reachable": False, "error": f"non-json body ({r.status_code})"}
        return name, {"reachable": True, **body}
    except Exception as exc:                                # noqa: BLE001
        return name, {"reachable": False, "error": str(exc)}


@app.route("/api/ui/engines/snapshot", methods=["GET"])
def ui_engines_snapshot():
    """Fan-out: read /api/v1/engine/state from each AI service in parallel,
    plus the most recent envelopes from each Redis channel. The UI's per-
    engine cards consume `services`; the recent-activity panel consumes
    `recent` (sorted oldest-first)."""
    _start_engines_subscriber()
    targets = [
        (name, os.environ.get(env_key, default))
        for (name, env_key, default) in ENGINE_SERVICES
    ]
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        results = list(pool.map(lambda t: _fetch_engine_state(*t), targets))
    services = dict(results)
    return jsonify({
        "services": services,
        "channels": _engines_buf.snapshot(),
        "recent":   _engines_buf.recent(limit=50),
    })


@app.route("/api/ui/engines/stream", methods=["GET"])
def ui_engines_stream():
    """SSE: replay the ring buffer once, then push every new envelope as it
    arrives. Sends a heartbeat comment every HEARTBEAT_INTERVAL_SECONDS so
    intermediate proxies don't kill idle connections.

    Each connection gets its own bounded queue; slow clients drop events
    rather than back-pressuring the publisher. Disconnects unsubscribe via
    the generator's finally block."""
    _start_engines_subscriber()
    q = _engines_bus.subscribe()
    replay = _engines_buf.recent()

    def generate():
        try:
            # Backfill so a fresh page isn't blank.
            for entry in replay:
                yield format_sse_event(entry, json.dumps)
            # Live.
            while True:
                try:
                    entry = q.get(timeout=HEARTBEAT_INTERVAL_SECONDS)
                    yield format_sse_event(entry, json.dumps)
                except queue.Empty:
                    yield format_sse_heartbeat()
        except (GeneratorExit, Exception):                  # noqa: BLE001
            return
        finally:
            _engines_bus.unsubscribe(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",       # disable buffering at any nginx in front
            "Connection":        "keep-alive",
        },
    )


# ── BFF own health ────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({
        "status":  "ok",
        "service": SERVICE_NAME,
    })


# ── SPA fallback: serve index.html for any unknown path ───────────────────────

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_spa(path: str):
    if path and os.path.isfile(os.path.join(WEB_DIST, path)):
        return send_from_directory(WEB_DIST, path)
    index_path = os.path.join(WEB_DIST, "index.html")
    if os.path.isfile(index_path):
        return send_from_directory(WEB_DIST, "index.html")
    return jsonify({
        "service": SERVICE_NAME,
        "message": "Operator UI BFF up; web/ build not found",
        "web_dist": WEB_DIST,
    })


if __name__ == "__main__":
    log.info("starting on port %d (web_dist=%s, openapi=%s)", PORT, WEB_DIST, OPENAPI_PATH)
    app.run(host="0.0.0.0", port=PORT)
