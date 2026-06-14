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
  - Serve the AsyncAPI viewer at /api/asyncapi-docs reading
    docs/asyncapi/smartload-v1.yaml (the async/event contract)
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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

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
from aggregator import build_status_response  # noqa: E402
from docs_pages import asyncapi_docs_html  # noqa: E402

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
    "lb-sidecar":       os.environ.get("LB_SIDECAR_URL",       "http://lb-sidecar:8087"),
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
ASYNCAPI_PATH = os.environ.get(
    "ASYNCAPI_PATH",
    "/app/asyncapi/smartload-v1.yaml",
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


# ── AsyncAPI (event contract) ───────────────────────────────────────────────────

@app.route("/api/asyncapi.yaml")
def serve_asyncapi():
    """Serve the canonical AsyncAPI spec for the viewer (and SDK tooling) to
    consume — the async analogue of /api/openapi.yaml."""
    if not os.path.isfile(ASYNCAPI_PATH):
        return jsonify({"error": f"asyncapi spec not found: {ASYNCAPI_PATH}"}), 404
    return send_from_directory(
        os.path.dirname(ASYNCAPI_PATH),
        os.path.basename(ASYNCAPI_PATH),
        mimetype="application/x-yaml",
    )


@app.route("/api/asyncapi-docs")
def asyncapi_docs():
    """Render the AsyncAPI spec in-browser — the async analogue of the Swagger
    UI at /api/docs. The page fetches the spec from /api/asyncapi.yaml."""
    return Response(
        asyncapi_docs_html("/api/asyncapi.yaml"),
        mimetype="text/html",
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


# ── /api/v1/status: consolidated read across every service + policy + audit ─

@app.route("/api/v1/status", methods=["GET"])
def get_status_v1():
    """One-shot aggregate read for programmatic operators (slice #149 / OUI.9).

    Replaces a 7-call polling burst (six /health + GET /api/v1/policy) with a
    single read on the BFF. Always 200 — per-service failures surface in
    `services[name].status` as "down"; `overall` rolls them up to one pill.
    Fan-out is parallel with a 2s per-service timeout so a stuck service can
    only delay the response by that bound.
    """
    pm_url = SERVICE_URLS["policy-manager"]
    as_url = SERVICE_URLS["autoscaler"]

    def _fetch_active_policy() -> dict | None:
        r = _http.get(f"{pm_url}/api/v1/policy", timeout=2.0)
        if r.status_code != 200:
            return None
        body = r.json()
        if not isinstance(body, dict):
            return None
        return {
            "operating_mode":     body.get("operating_mode"),
            "safe_mode":          body.get("safe_mode"),
            "slo_p95_latency_ms": body.get("slo_p95_latency_ms"),
            "policy_version":     body.get("policy_version"),
        }

    def _fetch_last_policy_change() -> dict | None:
        r = _http.get(f"{pm_url}/api/v1/audit/policy", params={"limit": 1}, timeout=2.0)
        if r.status_code != 200:
            return None
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            return None
        row = rows[0]
        return {
            "actor":  row.get("actor"),
            "field":  row.get("field"),
            "from":   row.get("old_value"),
            "to":     row.get("new_value"),
            "at":     row.get("time"),
        }

    def _fetch_last_scaling_event() -> dict | None:
        r = _http.get(f"{as_url}/api/v1/audit/scaling", params={"limit": 1}, timeout=2.0)
        if r.status_code != 200:
            return None
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            return None
        row = rows[0]
        return {
            "action":         row.get("action"),
            "instance_count": row.get("instance_count"),
            "reason":         row.get("reason"),
            "at":             row.get("time"),
        }

    response = build_status_response(
        service_urls=SERVICE_URLS,
        http_get=_http.get,
        fetch_active_policy=_fetch_active_policy,
        fetch_last_policy_change=_fetch_last_policy_change,
        fetch_last_scaling_event=_fetch_last_scaling_event,
    )
    return jsonify(response), 200


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


@app.route("/api/ui/policy/strategy", methods=["POST"])
def ui_policy_strategy():
    """Proxy the named-strategy alias to policy-manager (#150).

    Lets the operator-ui Policy page POST a strategy dropdown selection to one
    origin. Mirrors ui_policy_post: forwards the body verbatim and stamps the
    X-Actor header (operator-ui default when the page does not supply one)."""
    upstream = SERVICE_URLS["policy-manager"]
    body = request.get_data(as_text=True) or "{}"
    headers = {"Content-Type": "application/json"}
    headers["X-Actor"] = request.headers.get("X-Actor") or "operator-ui"
    try:
        r = _http.post(
            f"{upstream}/api/v1/policy/strategy", content=body, headers=headers,
        )
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


@app.route("/api/ui/actions/simulate/scale", methods=["POST"])
def ui_simulate_scale():
    """Proxy to autoscaler's POST /api/v1/actions/simulate — dry-run scale (#146).

    Both services expose the same path (/api/v1/actions/simulate) on different
    upstreams, so the BFF disambiguates by suffixing the UI route with the
    action kind. The operator UI hits one origin for the preview-before-apply
    flow."""
    upstream = SERVICE_URLS["autoscaler"]
    body = request.get_data(as_text=True) or "{}"
    headers = {"Content-Type": "application/json"}
    actor = request.headers.get("X-Actor") or "operator-ui"
    headers["X-Actor"] = actor
    try:
        r = _http.post(f"{upstream}/api/v1/actions/simulate", content=body, headers=headers)
    except Exception as exc:
        return jsonify({"error": f"upstream unreachable: {exc}"}), 502
    return (r.text, r.status_code, {"Content-Type": "application/json"})


@app.route("/api/ui/actions/simulate/isolate", methods=["POST"])
def ui_simulate_isolate():
    """Proxy to anomaly-detector's POST /api/v1/actions/simulate — dry-run
    isolate (#146). Returns the synthetic AnomalyEvent envelope that an isolate
    would publish, without publishing it."""
    upstream = SERVICE_URLS["anomaly-detector"]
    body = request.get_data(as_text=True) or "{}"
    headers = {"Content-Type": "application/json"}
    actor = request.headers.get("X-Actor") or "operator-ui"
    headers["X-Actor"] = actor
    try:
        r = _http.post(f"{upstream}/api/v1/actions/simulate", content=body, headers=headers)
    except Exception as exc:
        return jsonify({"error": f"upstream unreachable: {exc}"}), 502
    return (r.text, r.status_code, {"Content-Type": "application/json"})


# ── lb-sidecar proxies (T2.1) ────────────────────────────────────────────────

@app.route("/api/ui/lb/state", methods=["GET"])
def ui_lb_state():
    """Proxy to lb-sidecar's GET /api/v1/lb/state."""
    upstream = SERVICE_URLS["lb-sidecar"]
    try:
        r = _http.get(f"{upstream}/api/v1/lb/state")
    except Exception as exc:
        return jsonify({"error": f"upstream unreachable: {exc}"}), 502
    return (r.text, r.status_code, {"Content-Type": "application/json"})


@app.route("/api/ui/lb/weights", methods=["POST"])
def ui_lb_set_weights():
    """Proxy operator weight override to lb-sidecar's POST /api/v1/lb/weights."""
    upstream = SERVICE_URLS["lb-sidecar"]
    body = request.get_data(as_text=True) or "{}"
    try:
        r = _http.post(
            f"{upstream}/api/v1/lb/weights",
            content=body,
            headers={"Content-Type": "application/json"},
        )
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


# ── shared helpers for /api/ui aggregations (#132 redesign) ───────────────────

# Window over which a smartload.anomaly envelope with status != "healthy"
# still counts as an "active alert". Tuned to match the operator's mental
# refresh cycle (Home polls every ~10s; 5min is long enough to be visible
# but short enough that resolved blips drop off quickly).
ACTIVE_ALERT_WINDOW_SECONDS = 300

# Bounds on /api/ui/activity?limit=N. Default mirrors the audit endpoints so
# the Home and Audit pages get the same depth without an explicit override.
ACTIVITY_LIMIT_DEFAULT = 50
ACTIVITY_LIMIT_MAX     = 500

# Cap upstream audit fan-out for counts so a flood of rows on either source
# doesn't translate into a multi-second BFF call.
AUDIT_COUNTS_FETCH_LIMIT = 500

# Lightweight client-side validation rules for /api/ui/policy/preview. Fields
# not listed here pass through unvalidated — the policy-manager is still the
# authoritative validator on actual POST. Preview is a UX nicety, not a gate.
_PREVIEW_NUMERIC_POSITIVE_FIELDS: tuple[str, ...] = (
    "min_backends",
    "max_backends",
    "slo_p95_latency_ms",
    "anomaly_latency_multiplier",
    "per_instance_capacity_rps",
    "autoscaler_cooldown_seconds",
    "anomaly_recovery_window_seconds",
)
_PREVIEW_NUMERIC_NONNEGATIVE_FIELDS: tuple[str, ...] = (
    "rl_exploration_rate",
    "rl_confidence_threshold",
)
_PREVIEW_ALLOWED_OPERATING_MODES: tuple[str, ...] = (
    "hybrid", "rl-only", "rule-based", "shadow",
)
_PREVIEW_ALLOWED_ANOMALY_RESPONSES: tuple[str, ...] = (
    "auto-isolate", "advisory",
)


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse an RFC 3339/ISO 8601 timestamp into an aware datetime, or None
    on malformed input. Tolerates the trailing 'Z' shorthand."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _count_active_alerts(entries: list[dict]) -> int:
    """Count smartload.anomaly envelopes whose payload status is not
    "healthy" within ACTIVE_ALERT_WINDOW_SECONDS of now. Defensive against
    malformed entries — anything we can't parse is skipped, not counted."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ACTIVE_ALERT_WINDOW_SECONDS)
    n = 0
    for e in entries:
        if e.get("channel") != "smartload.anomaly":
            continue
        ts = _parse_iso(e.get("envelope", {}).get("timestamp"))
        if ts is None or ts < cutoff:
            continue
        status = e.get("payload", {}).get("status")
        if status and status != "healthy":
            n += 1
    return n


# ── Operations metrics (#132) ─────────────────────────────────────────────────

@app.route("/api/ui/metrics/ops", methods=["GET"])
def ui_metrics_ops():
    """Operations Overview KPI bundle for the Home page.

    Aggregates three sources with graceful degradation:
      - service health via the same fan-out as /api/ui/health,
      - active alerts via the live engines ring buffer,
      - throughput / requests_total — not yet wired (see notes / TODO below).

    Any field that can't be computed becomes null and is explained in
    `notes` so the UI can show a clear placeholder rather than a zero."""
    _start_engines_subscriber()
    notes: list[str] = []

    # Health fan-out. Reuses _fetch_health so a single upstream failure
    # surfaces as "unreachable" for that service rather than tanking the
    # whole bundle.
    try:
        with ThreadPoolExecutor(max_workers=len(SERVICE_URLS)) as pool:
            health_results = list(pool.map(
                lambda kv: _fetch_health(*kv),
                SERVICE_URLS.items(),
            ))
        services_summary = dict(health_results)
        services_total    = len(services_summary)
        services_healthy  = sum(1 for v in services_summary.values()
                                if v.get("status") == "ok")
        services_degraded = services_total - services_healthy
        if services_total > 0:
            policy_compliance_pct = round(
                100.0 * services_healthy / services_total, 1
            )
        else:
            policy_compliance_pct = None
            notes.append("policy_compliance_pct: no services discovered")
    except Exception as exc:                                # noqa: BLE001
        log.warning("ops metrics: health fan-out failed: %s", exc)
        services_total = 0
        services_healthy = 0
        services_degraded = 0
        policy_compliance_pct = None
        notes.append(f"health fan-out failed: {exc}")

    # Active alerts via the engines ring buffer.
    try:
        active_alerts = _count_active_alerts(_engines_buf.recent())
    except Exception as exc:                                # noqa: BLE001
        log.warning("ops metrics: alert count failed: %s", exc)
        active_alerts = 0
        notes.append(f"active_alerts: ring buffer read failed: {exc}")

    # Throughput + cumulative request count from telemetry's per-minute
    # request-rate series (/api/v1/metrics/rpm): throughput_rpm is the most
    # recent complete-minute rate, requests_total the windowed sum. On any
    # upstream failure we fall back to null + a note so the UI shows "—"
    # rather than misleading zeros — same graceful-degradation contract as
    # /api/ui/metrics/throughput.
    throughput_rpm = None
    requests_total = None
    try:
        r = _http.get(
            f"{SERVICE_URLS['telemetry']}/api/v1/metrics/rpm",
            params={"window": 24},
        )
        if r.status_code == 200:
            body = r.json() or {}
            throughput_rpm = body.get("current_rpm")
            requests_total = body.get("total_requests")
        else:
            notes.append(f"throughput_rpm: telemetry returned {r.status_code}")
    except Exception as exc:                                # noqa: BLE001
        log.warning("ops metrics: rpm fetch failed: %s", exc)
        notes.append(f"throughput_rpm: telemetry upstream failed: {exc}")

    return jsonify({
        "services_total":        services_total,
        "services_healthy":      services_healthy,
        "services_degraded":     services_degraded,
        "active_alerts":         active_alerts,
        "policy_compliance_pct": policy_compliance_pct,
        "throughput_rpm":        throughput_rpm,
        "requests_total":        requests_total,
        "last_refreshed":        datetime.now(timezone.utc).isoformat(),
        "notes":                 notes,
    })


# ── Unified activity feed (#132) ──────────────────────────────────────────────

def _severity_for_anomaly(payload: dict) -> str:
    """Map an AnomalyEvent payload to a UI severity bucket. Mirrors the
    three-tier status convention used elsewhere (healthy/degraded/unhealthy)."""
    status = (payload or {}).get("status")
    if status == "unhealthy":
        return "bad"
    if status == "degraded":
        return "warn"
    return "info"


def _summary_for_anomaly(payload: dict) -> str:
    backend = payload.get("backend_id", "unknown")
    status  = payload.get("status", "unknown")
    score   = payload.get("score")
    try:
        score_str = f" ({float(score):.2f})" if score is not None else ""
    except (TypeError, ValueError):
        score_str = ""
    return f"{backend} → {status}{score_str}"


def _summary_for_policy(row: dict) -> str:
    field    = row.get("field", "?")
    old_val  = row.get("old_value")
    new_val  = row.get("new_value")
    return f"{field} {old_val} → {new_val}"


def _summary_for_scaling(row: dict) -> str:
    action = row.get("action", "scale")
    count  = row.get("instance_count")
    if count is None:
        return action
    return f"{action} → {count} backends"


@app.route("/api/ui/activity", methods=["GET"])
def ui_activity():
    """Unified recent-activity feed merging policy audit, scaling audit, and
    anomaly events from the engines ring buffer. Sorted by time descending.

    Graceful degradation: each upstream is fetched independently and its
    contribution is skipped (with a note logged) on failure — the operator
    still gets a partial feed."""
    _start_engines_subscriber()

    try:
        limit = int(request.args.get("limit", ACTIVITY_LIMIT_DEFAULT))
    except (TypeError, ValueError):
        return jsonify({"error": "limit must be an integer", "field": "limit"}), 400
    if limit <= 0:
        return jsonify({"error": "limit must be > 0", "field": "limit"}), 400
    limit = min(limit, ACTIVITY_LIMIT_MAX)

    items: list[dict] = []

    # Policy audit. Fetch enough rows to fill a merged feed of `limit`
    # entries — we'll trim after sorting.
    try:
        r = _http.get(
            f"{SERVICE_URLS['policy-manager']}/api/v1/audit/policy",
            params={"limit": limit},
        )
        if r.status_code == 200:
            for row in r.json() or []:
                items.append({
                    "kind":     "policy",
                    "time":     row.get("time"),
                    "actor":    row.get("actor"),
                    "summary":  _summary_for_policy(row),
                    "source":   "policy-manager",
                    "severity": "info",
                })
        else:
            log.warning("activity: policy audit upstream returned %s", r.status_code)
    except Exception as exc:                                # noqa: BLE001
        log.warning("activity: policy audit upstream failed: %s", exc)

    # Scaling audit.
    try:
        r = _http.get(
            f"{SERVICE_URLS['autoscaler']}/api/v1/audit/scaling",
            params={"limit": limit},
        )
        if r.status_code == 200:
            for row in r.json() or []:
                items.append({
                    "kind":     "scaling",
                    "time":     row.get("time"),
                    "actor":    None,
                    "summary":  _summary_for_scaling(row),
                    "source":   "autoscaler",
                    "severity": "info",
                })
        else:
            log.warning("activity: scaling audit upstream returned %s", r.status_code)
    except Exception as exc:                                # noqa: BLE001
        log.warning("activity: scaling audit upstream failed: %s", exc)

    # Anomaly events from the ring buffer — most recent first via slicing
    # after sort, so just dump everything available here.
    try:
        for entry in _engines_buf.recent():
            if entry.get("channel") != "smartload.anomaly":
                continue
            payload = entry.get("payload", {}) or {}
            items.append({
                "kind":     "anomaly",
                "time":     entry.get("envelope", {}).get("timestamp"),
                "actor":    None,
                "summary":  _summary_for_anomaly(payload),
                "source":   "anomaly-detector",
                "severity": _severity_for_anomaly(payload),
            })
    except Exception as exc:                                # noqa: BLE001
        log.warning("activity: ring buffer read failed: %s", exc)

    # Sort newest-first; missing timestamps sink to the bottom rather than
    # raising a comparison error.
    items.sort(key=lambda it: it.get("time") or "", reverse=True)
    return jsonify(items[:limit])


# ── Active alerts (structured) ────────────────────────────────────────────────

def _alert_summary(payload: dict) -> str:
    """Human one-liner for an alert row. Uses the evidence the anomaly engine
    attached (metric / observed_value / threshold) when present, falling back
    to the bare status when an older event predates the evidence fields."""
    backend = payload.get("backend_id", "unknown")
    metric  = payload.get("metric")
    observed = payload.get("observed_value")
    threshold = payload.get("threshold")
    if metric is not None and observed is not None:
        obs = f"{observed:.2f}".rstrip("0").rstrip(".") if isinstance(observed, (int, float)) else observed
        if threshold is not None and isinstance(threshold, (int, float)):
            thr = f"{threshold:.2f}".rstrip("0").rstrip(".")
            return f"{backend}: {metric} {obs} (threshold {thr})"
        return f"{backend}: {metric} {obs}"
    return f"{backend} → {payload.get('status', 'unknown')}"


@app.route("/api/ui/alerts", methods=["GET"])
def ui_alerts():
    """Structured active alerts for the Home page Active Alerts panel.

    Reads smartload.anomaly envelopes from the engines ring buffer, keeps those
    with status != "healthy" inside ?window=N seconds (default
    ACTIVE_ALERT_WINDOW_SECONDS), and returns one row per backend (newest wins)
    carrying severity + the metric/observed/threshold evidence. Degrades to an
    empty list — never errors the page."""
    _start_engines_subscriber()

    try:
        window = int(request.args.get("window", ACTIVE_ALERT_WINDOW_SECONDS))
    except (TypeError, ValueError):
        window = ACTIVE_ALERT_WINDOW_SECONDS
    if window <= 0:
        window = ACTIVE_ALERT_WINDOW_SECONDS

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window)
    latest_by_backend: dict[str, dict] = {}
    try:
        for entry in _engines_buf.recent():
            if entry.get("channel") != "smartload.anomaly":
                continue
            ts_raw = entry.get("envelope", {}).get("timestamp")
            ts = _parse_iso(ts_raw)
            if ts is None or ts < cutoff:
                continue
            payload = entry.get("payload", {}) or {}
            status = payload.get("status")
            if not status or status == "healthy":
                continue
            backend = payload.get("backend_id", "unknown")
            severity = payload.get("severity") or (
                "critical" if status == "unhealthy" else "warning"
            )
            row = {
                "backend_id":     backend,
                "status":         status,
                "severity":       severity,
                "metric":         payload.get("metric"),
                "observed_value": payload.get("observed_value"),
                "threshold":      payload.get("threshold"),
                "score":          payload.get("score"),
                "time":           ts_raw,
                "summary":        _alert_summary(payload),
            }
            prev = latest_by_backend.get(backend)
            if prev is None or (row["time"] or "") > (prev["time"] or ""):
                latest_by_backend[backend] = row
    except Exception as exc:                                # noqa: BLE001
        log.warning("alerts: ring buffer read failed: %s", exc)
        return jsonify([])

    # Critical before warning, then newest first. Two stable sorts: newest-first
    # by time, then by severity rank — ISO-8601 sorts lexically so a plain
    # string compare is a correct chronological order.
    rank = {"critical": 0, "warning": 1}
    alerts = list(latest_by_backend.values())
    alerts.sort(key=lambda a: a["time"] or "", reverse=True)
    alerts.sort(key=lambda a: rank.get(a["severity"], 2))
    return jsonify(alerts)


# ── Policy dry-run preview (#132) ─────────────────────────────────────────────

def _validate_policy_patch(merged: dict) -> list[str]:
    """Lightweight client-side validation of a merged (current + patch)
    policy. Returns a list of human-readable error strings; empty list means
    valid. policy-manager remains the authoritative validator on real POST."""
    errors: list[str] = []

    for f in _PREVIEW_NUMERIC_POSITIVE_FIELDS:
        if f not in merged:
            continue
        v = merged.get(f)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
            errors.append(f"{f} must be a number > 0")

    for f in _PREVIEW_NUMERIC_NONNEGATIVE_FIELDS:
        if f not in merged:
            continue
        v = merged.get(f)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
            errors.append(f"{f} must be a number >= 0")

    min_b = merged.get("min_backends")
    max_b = merged.get("max_backends")
    if (isinstance(min_b, int) and isinstance(max_b, int)
            and not isinstance(min_b, bool) and not isinstance(max_b, bool)
            and max_b < min_b):
        errors.append("max_backends must be >= min_backends")

    op_mode = merged.get("operating_mode")
    if op_mode is not None and op_mode not in _PREVIEW_ALLOWED_OPERATING_MODES:
        errors.append(
            f"operating_mode must be one of {list(_PREVIEW_ALLOWED_OPERATING_MODES)}"
        )

    ar = merged.get("anomaly_response")
    if ar is not None and ar not in _PREVIEW_ALLOWED_ANOMALY_RESPONSES:
        errors.append(
            f"anomaly_response must be one of {list(_PREVIEW_ALLOWED_ANOMALY_RESPONSES)}"
        )

    safe_mode = merged.get("safe_mode")
    if safe_mode is not None and not isinstance(safe_mode, bool):
        errors.append("safe_mode must be a boolean")

    rl_thresh = merged.get("rl_confidence_threshold")
    if isinstance(rl_thresh, (int, float)) and not isinstance(rl_thresh, bool):
        if rl_thresh > 1.0:
            errors.append("rl_confidence_threshold must be <= 1.0")

    return errors


def _policy_preview_warnings(current: dict, patch: dict) -> list[str]:
    """Heuristic warnings — things that pass validation but the operator
    probably wants flagged. Cheap to add to and intentionally non-blocking."""
    warnings: list[str] = []

    if ("max_backends" in patch
            and patch["max_backends"] != current.get("max_backends")
            and "autoscaler_cooldown_seconds" not in patch):
        warnings.append(
            "raising max_backends without cooldown change may cause flapping"
        )

    if patch.get("safe_mode") is True and current.get("safe_mode") is False:
        warnings.append("safe_mode=true freezes scaling; verify intent")

    if ("operating_mode" in patch
            and patch["operating_mode"] != current.get("operating_mode")
            and patch["operating_mode"] == "rl-only"
            and current.get("rl_exploration_rate", 0) == 0):
        warnings.append("rl-only mode with zero exploration rate may stall learning")

    return warnings


@app.route("/api/ui/policy/preview", methods=["POST"])
def ui_policy_preview():
    """Dry-run preview of a policy patch. Fetches the current policy from
    policy-manager, computes the diff, and runs cheap local validation. Does
    NOT apply the patch — the real POST to /api/ui/policy is still required
    to commit a change."""
    body = request.get_json(silent=True) or {}
    patch = body.get("patch")
    if not isinstance(patch, dict):
        return jsonify({"error": "request body must be {\"patch\": {...}}"}), 400

    try:
        r = _http.get(f"{SERVICE_URLS['policy-manager']}/api/v1/policy")
    except Exception as exc:                                # noqa: BLE001
        return jsonify({"error": f"upstream unreachable: {exc}"}), 502
    if r.status_code != 200:
        return jsonify({"error": f"policy-manager returned {r.status_code}"}), 502

    try:
        current = r.json() or {}
    except (ValueError, TypeError):
        return jsonify({"error": "policy-manager returned non-JSON body"}), 502

    # changed_fields are limited to keys actually present in the patch whose
    # values differ from current. Keys present in patch but equal to current
    # are intentionally omitted — they're a no-op.
    changed_fields: list[str] = []
    diff: list[dict] = []
    for k, new_val in patch.items():
        old_val = current.get(k)
        if old_val != new_val:
            changed_fields.append(k)
            diff.append({"field": k, "old": old_val, "new": new_val})

    merged = {**current, **patch}
    errors = _validate_policy_patch(merged)
    warnings = _policy_preview_warnings(current, patch)

    return jsonify({
        "valid":          len(errors) == 0,
        "errors":         errors,
        "changed_fields": changed_fields,
        "diff":           diff,
        "warnings":       warnings,
    })


# ── Audit page counts (#132) ──────────────────────────────────────────────────

@app.route("/api/ui/audit/counts", methods=["GET"])
def ui_audit_counts():
    """KPI bundle for the Audit page header. Each upstream is fetched
    independently; a failure contributes 0 to its count rather than tanking
    the whole bundle."""
    _start_engines_subscriber()

    policy_changes  = 0
    scaling_actions = 0
    last_event_at: str | None = None
    # Distinct human/system actors across the audited sources. Scaling rows have
    # no actor column (autoscaler acts autonomously) so they fold into a single
    # "autoscaler" actor; policy rows carry the operator id.
    actors: set[str] = set()

    def _track_last(ts: str | None) -> None:
        nonlocal last_event_at
        if not ts:
            return
        if last_event_at is None or ts > last_event_at:
            last_event_at = ts

    try:
        r = _http.get(
            f"{SERVICE_URLS['policy-manager']}/api/v1/audit/policy",
            params={"limit": AUDIT_COUNTS_FETCH_LIMIT},
        )
        if r.status_code == 200:
            rows = r.json() or []
            policy_changes = len(rows)
            for row in rows:
                _track_last(row.get("time"))
                actor = row.get("actor")
                if actor:
                    actors.add(str(actor))
    except Exception as exc:                                # noqa: BLE001
        log.warning("audit counts: policy upstream failed: %s", exc)

    try:
        r = _http.get(
            f"{SERVICE_URLS['autoscaler']}/api/v1/audit/scaling",
            params={"limit": AUDIT_COUNTS_FETCH_LIMIT},
        )
        if r.status_code == 200:
            rows = r.json() or []
            scaling_actions = len(rows)
            if rows:
                actors.add("autoscaler")
            for row in rows:
                _track_last(row.get("time"))
    except Exception as exc:                                # noqa: BLE001
        log.warning("audit counts: scaling upstream failed: %s", exc)

    anomaly_entries = [
        e for e in _engines_buf.recent()
        if e.get("channel") == "smartload.anomaly"
    ]
    anomaly_events = len(anomaly_entries)
    for e in anomaly_entries:
        _track_last(e.get("envelope", {}).get("timestamp"))

    active_alerts = _count_active_alerts(_engines_buf.recent())

    return jsonify({
        "total_events":    policy_changes + scaling_actions + anomaly_events,
        "policy_changes":  policy_changes,
        "scaling_actions": scaling_actions,
        "anomaly_events":  anomaly_events,
        "active_alerts":   active_alerts,
        "actors_unique":   len(actors),
        "last_event_at":   last_event_at,
    })


# ── Throughput + routing/scaling aggregations (#132 follow-up) ────────────────

# Window over which routing envelopes are counted toward "decisions / min". 60s
# matches the Home page refresh cadence; tighter would over-weight bursts,
# wider would lag operator perception.
ROUTING_DECISIONS_WINDOW_SECONDS = 60


@app.route("/api/ui/metrics/throughput", methods=["GET"])
def ui_metrics_throughput():
    """Pass-through to telemetry's per-minute request-rate series.

    ?buckets=N — forwarded as ?window=N to telemetry. The BFF degrades to
    an empty/zero response on upstream failure rather than 502'ing, so the
    Home throughput card never renders an error state."""
    buckets = request.args.get("buckets", "24")
    empty = {"buckets": [], "current_rpm": 0, "total_requests": 0}
    try:
        r = _http.get(
            f"{SERVICE_URLS['telemetry']}/api/v1/metrics/rpm",
            params={"window": buckets},
        )
    except Exception as exc:                                # noqa: BLE001
        log.warning("throughput: telemetry upstream failed: %s", exc)
        return jsonify(empty)
    if r.status_code != 200:
        log.warning("throughput: telemetry returned %s", r.status_code)
        return jsonify(empty)
    try:
        return jsonify(r.json() or empty)
    except (ValueError, TypeError):
        return jsonify(empty)


def _count_routing_decisions(entries: list[dict]) -> int:
    """Count smartload.routing envelopes inside the rolling decisions window
    and scale to a per-minute rate.

    Returns an integer. If the observed window is shorter than the configured
    one (process freshly started, ring buffer not full) we still scale to
    decisions / minute so the number stays comparable across restarts."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ROUTING_DECISIONS_WINDOW_SECONDS)
    now    = datetime.now(timezone.utc)
    n = 0
    earliest: datetime | None = None
    for e in entries:
        if e.get("channel") != "smartload.routing":
            continue
        ts = _parse_iso(e.get("envelope", {}).get("timestamp"))
        if ts is None or ts < cutoff:
            continue
        n += 1
        if earliest is None or ts < earliest:
            earliest = ts
    if n == 0:
        return 0
    observed = max((now - (earliest or cutoff)).total_seconds(), 1.0)
    return int(round(n * 60.0 / observed))


@app.route("/api/ui/metrics/routing", methods=["GET"])
def ui_metrics_routing():
    """Composite for the Home "Routing & Scaling" card.

    Combines three signals:
      - routing decisions / minute, computed locally from the engines ring
        buffer (smartload.routing channel),
      - scale events in the last hour, from the autoscaler audit endpoint,
      - current cluster size, from the most recent scaling audit row with a
        fallback to policy-manager's min_backends when no scaling has
        happened yet.

    Each upstream is independent — a single failure degrades that field to
    a sensible default (0 or null) rather than tanking the bundle."""
    _start_engines_subscriber()

    try:
        routing_per_min = _count_routing_decisions(_engines_buf.recent())
    except Exception as exc:                                # noqa: BLE001
        log.warning("routing metrics: ring buffer read failed: %s", exc)
        routing_per_min = 0

    scale_events_1h: int = 0
    cluster_size_current: int | None = None
    try:
        r = _http.get(
            f"{SERVICE_URLS['autoscaler']}/api/v1/audit/scaling",
            params={"limit": AUDIT_COUNTS_FETCH_LIMIT},
        )
        if r.status_code == 200:
            rows = r.json() or []
            one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
            for row in rows:
                ts = _parse_iso(row.get("time"))
                if ts is not None and ts >= one_hour_ago:
                    scale_events_1h += 1
            # Rows are newest-first per SCALING_AUDIT_QUERY, so the first
            # row with a non-null instance_count is the current cluster
            # size. Don't rely on the absolute top row in case the latest
            # event happens to record null for some reason.
            for row in rows:
                ic = row.get("instance_count")
                if isinstance(ic, int):
                    cluster_size_current = ic
                    break
        else:
            log.warning("routing metrics: scaling upstream returned %s", r.status_code)
    except Exception as exc:                                # noqa: BLE001
        log.warning("routing metrics: scaling upstream failed: %s", exc)

    if cluster_size_current is None:
        try:
            pr = _http.get(f"{SERVICE_URLS['policy-manager']}/api/v1/policy")
            if pr.status_code == 200:
                pol = pr.json() or {}
                mb = pol.get("min_backends")
                if isinstance(mb, int):
                    cluster_size_current = mb
        except Exception as exc:                            # noqa: BLE001
            log.warning("routing metrics: policy fallback failed: %s", exc)

    # Autoscaler heartbeat. Even when the cluster has been stable and no
    # scaling event has fired in hours, the autoscaler is still ticking — the
    # noop counter on its /health is the proof. We surface it so the UI can
    # show "alive, 3,620 noop decisions since last actuation" instead of
    # making the operator wonder whether the service died.
    autoscaler_info: dict | None = None
    try:
        hr = _http.get(f"{SERVICE_URLS['autoscaler']}/health")
        if hr.status_code == 200:
            hb = hr.json() or {}
            stats = hb.get("stats") or {}
            actuated = (
                int(stats.get("actions_scale_in", 0))
                + int(stats.get("actions_scale_out", 0))
            )
            autoscaler_info = {
                "decisions_total":    int(stats.get("actions_total", 0)),
                "decisions_noop":     int(stats.get("actions_noop", 0)),
                "decisions_actuated": actuated,
                "policy_version":     hb.get("policy_version"),
                "status":             hb.get("status"),
                "redis":              hb.get("redis"),
                "timescaledb":        hb.get("timescaledb"),
            }
    except Exception as exc:                                # noqa: BLE001
        log.warning("routing metrics: autoscaler heartbeat failed: %s", exc)

    # Last actuation (skip rows where the action is 'noop' if any leaked in
    # despite the autoscaler suppressing noop inserts — defensive only).
    last_actuation: dict | None = None
    try:
        r = _http.get(
            f"{SERVICE_URLS['autoscaler']}/api/v1/audit/scaling",
            params={"limit": 1},
        )
        if r.status_code == 200:
            rows = r.json() or []
            for row in rows:
                if (row.get("action") or "").startswith("scale"):
                    last_actuation = {
                        "time":   row.get("time"),
                        "action": row.get("action"),
                        "instance_count": row.get("instance_count"),
                        "reason": row.get("reason"),
                    }
                    break
    except Exception as exc:                                # noqa: BLE001
        log.warning("routing metrics: last actuation lookup failed: %s", exc)

    if autoscaler_info is not None and last_actuation is not None:
        autoscaler_info["last_actuation"] = last_actuation

    return jsonify({
        "routing_decisions_per_min": routing_per_min,
        "scale_events_1h":           scale_events_1h,
        "cluster_size_current":      cluster_size_current,
        "autoscaler":                autoscaler_info,
    })


# ── Policy page sidecards (#132 follow-up) ────────────────────────────────────

# Defaults used when the operator-ui container starts without the env-vars
# set. Matches the docker-compose.yml block so dev and prod render the same.
_DEFAULT_SMARTLOAD_ENV       = "production"
_DEFAULT_SMARTLOAD_AVAILABLE = "dev,staging,prod"


@app.route("/api/ui/policy/environment", methods=["GET"])
def ui_policy_environment():
    """Environment Scope card on the Policy page.

    Sourced from operator-ui env vars rather than a live upstream — the
    deployment topology is fixed at container start, not derived from
    runtime data. Comma-separated SMARTLOAD_AVAILABLE_ENVS is split into a
    JSON list with empty entries dropped."""
    active = os.environ.get("SMARTLOAD_ENV", _DEFAULT_SMARTLOAD_ENV)
    raw    = os.environ.get("SMARTLOAD_AVAILABLE_ENVS", _DEFAULT_SMARTLOAD_AVAILABLE)
    available = [s.strip() for s in raw.split(",") if s.strip()]
    return jsonify({
        "active":    active,
        "available": available,
    })


@app.route("/api/ui/policy/related-metrics", methods=["GET"])
def ui_policy_related_metrics():
    """Related Metrics card on the Policy page.

    Parallel fan-out to telemetry for SLO compliance (1h), p95 latency (60s),
    and current minute throughput. Each call is independent — an individual
    failure surfaces as null for that field, the others still render."""

    def _slo() -> float | None:
        try:
            r = _http.get(f"{SERVICE_URLS['telemetry']}/api/v1/metrics/slo",
                          params={"window": 3600})
            if r.status_code != 200:
                return None
            body = r.json() or {}
            v = body.get("compliant_pct")
            return float(v) if isinstance(v, (int, float)) else None
        except Exception:                                   # noqa: BLE001
            return None

    def _p95() -> int | None:
        try:
            r = _http.get(f"{SERVICE_URLS['telemetry']}/api/v1/metrics/latency",
                          params={"window": 60})
            if r.status_code != 200:
                return None
            body = r.json() or {}
            v = body.get("p95_ms")
            return int(v) if isinstance(v, (int, float)) else None
        except Exception:                                   # noqa: BLE001
            return None

    def _rps() -> float | None:
        # window=2 so telemetry returns at least one COMPLETE bucket as
        # current_rpm (the trailing partial minute is split off into
        # `partial` by the telemetry endpoint). window=1 used to slip
        # between time_bucket boundaries and return 0 buckets.
        try:
            r = _http.get(f"{SERVICE_URLS['telemetry']}/api/v1/metrics/rpm",
                          params={"window": 2})
            if r.status_code != 200:
                return None
            body = r.json() or {}
            v = body.get("current_rpm")
            if not isinstance(v, (int, float)):
                return None
            return round(v / 60.0, 2)
        except Exception:                                   # noqa: BLE001
            return None

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_slo = pool.submit(_slo)
        f_p95 = pool.submit(_p95)
        f_rps = pool.submit(_rps)
        slo_val = f_slo.result()
        p95_val = f_p95.result()
        rps_val = f_rps.result()

    return jsonify({
        "slo_compliance_pct": slo_val,
        "p95_latency_ms":     p95_val,
        "rps_current":        rps_val,
    })


@app.route("/api/ui/metrics/resources", methods=["GET"])
def ui_metrics_resources():
    """Per-instance CPU / memory for the engine cards + service-health rows.

    Thin proxy to telemetry's /api/v1/metrics/resources (which pivots the
    resource-collector's gauges into one record per container). `?window=N`
    seconds is forwarded through; on any upstream failure we return an empty
    instance list with HTTP 200 so the resource widgets degrade gracefully
    rather than erroring the whole page."""
    window = request.args.get("window", "120")
    empty = {"instances": [], "window_seconds": window, "count": 0}
    try:
        r = _http.get(f"{SERVICE_URLS['telemetry']}/api/v1/metrics/resources",
                      params={"window": window})
        if r.status_code != 200:
            return jsonify(empty), 200
        return jsonify(r.json()), 200
    except Exception:                                       # noqa: BLE001
        return jsonify(empty), 200


@app.route("/api/ui/metrics/backends", methods=["GET"])
def ui_metrics_backends():
    """Per-backend request stats (p95 / req-min / error rate) for the Home
    Service Health table. Thin proxy to telemetry's /api/v1/metrics/backends.
    `?window=N` seconds is forwarded; any upstream failure degrades to an empty
    list with HTTP 200 so the table renders CPU-only rather than erroring."""
    window = request.args.get("window", "60")
    empty = {"backends": [], "aggregate": None, "window_seconds": window}
    try:
        r = _http.get(f"{SERVICE_URLS['telemetry']}/api/v1/metrics/backends",
                      params={"window": window})
        if r.status_code != 200:
            return jsonify(empty), 200
        return jsonify(r.json()), 200
    except Exception:                                       # noqa: BLE001
        return jsonify(empty), 200


# ── Forecast history + anomaly verdict history (pass-through proxies) ──────────

@app.route("/api/ui/metrics/forecast-history", methods=["GET"])
def ui_metrics_forecast_history():
    """Proxy to the forecasting service's GET /api/v1/forecasts.

    Surfaces the predicted-vs-actual forecast series (predicted_rps with
    confidence bounds, model name/version) for the Foresight page so the
    forward forecast tail and accuracy callout can be driven from real
    predictions rather than the throughput-derived stand-in. `?window=N`
    seconds and `?limit=N` are forwarded verbatim; only set params are sent so
    the upstream applies its own defaults. Same 502-on-unreachable contract as
    the other /api/ui pass-through proxies (ui_policy_audit / ui_scaling_audit)."""
    upstream = SERVICE_URLS["forecasting"]
    params = {}
    window = request.args.get("window")
    if window is not None:
        params["window"] = window
    limit = request.args.get("limit")
    if limit is not None:
        params["limit"] = limit
    try:
        r = _http.get(f"{upstream}/api/v1/forecasts", params=params)
    except Exception as exc:                                # noqa: BLE001
        return jsonify({"error": f"upstream unreachable: {exc}"}), 502
    return (r.text, r.status_code, {"Content-Type": "application/json"})


@app.route("/api/ui/anomaly/history", methods=["GET"])
def ui_anomaly_history():
    """Proxy to the anomaly-detector's GET /api/v1/anomaly/history.

    Surfaces the per-backend status/score verdict history over time for the
    Verdicts page detail Drawer (a score sparkline + status-change timeline)
    rather than only the latest ruling. `?window=N` seconds, optional
    `?backend=<id>`, and `?limit=N` are forwarded verbatim; only set params
    are sent so the upstream applies its own defaults. Same 502-on-unreachable
    contract as the other /api/ui pass-through proxies."""
    upstream = SERVICE_URLS["anomaly-detector"]
    params = {}
    window = request.args.get("window")
    if window is not None:
        params["window"] = window
    backend = request.args.get("backend")
    if backend is not None:
        params["backend"] = backend
    limit = request.args.get("limit")
    if limit is not None:
        params["limit"] = limit
    try:
        r = _http.get(f"{upstream}/api/v1/anomaly/history", params=params)
    except Exception as exc:                                # noqa: BLE001
        return jsonify({"error": f"upstream unreachable: {exc}"}), 502
    return (r.text, r.status_code, {"Content-Type": "application/json"})


# ── Trends / deltas (KPI sparklines + window-over-window deltas) ──────────────
#
# Powers the headline KPI tiles so they stop hardcoding a sample sparkline and
# a "+12.6%" delta. Each KPI carries a compact recent series (for the
# sparkline), a delta vs the prior comparable window, and a human label. All
# numbers are real telemetry; any KPI whose source is unreachable degrades to a
# null delta + empty series with a note — never a 500.

# Number of trailing minute buckets used to build a sparkline / split into the
# two comparison windows. 12 buckets ≈ a 12-minute view; the recent half is the
# last 6 buckets, the prior half the 6 before it.
TRENDS_BUCKET_COUNT = 12


def _pct_delta(recent: float | None, prior: float | None) -> float | None:
    """Percentage change from prior → recent, rounded to 1 dp. None when either
    side is missing or the prior window is zero (no meaningful base)."""
    if recent is None or prior is None or prior == 0:
        return None
    return round((recent - prior) / abs(prior) * 100.0, 1)


def _split_mean(values: list[float]) -> tuple[float | None, float | None]:
    """Split a series into prior half + recent half and return (recent_mean,
    prior_mean). Either side is None when its half is empty."""
    if not values:
        return None, None
    mid = len(values) // 2
    prior = values[:mid]
    recent = values[mid:]
    prior_mean = round(sum(prior) / len(prior), 2) if prior else None
    recent_mean = round(sum(recent) / len(recent), 2) if recent else None
    return recent_mean, prior_mean


@app.route("/api/ui/metrics/trends", methods=["GET"])
def ui_metrics_trends():
    """Headline-KPI trend bundle: recent series + window-over-window delta for
    each tile (throughput rpm, p95 latency, SLO compliance %, error rate %,
    active backends).

    Sources, all real, each independent:
      - throughput_rpm / error_rate_pct: telemetry per-minute series
        (/api/v1/metrics/rpm buckets, /api/v1/metrics/backends aggregate).
      - p95_latency_ms / slo_compliance_pct: telemetry latency + slo over the
        recent vs the prior window.
      - active_backends: the current cluster size from the scaling audit.

    Each KPI returns {series, current, delta_pct, label, unit}. A KPI whose
    upstream is down degrades to an empty series + null delta and a note, so the
    tile shows a calm placeholder rather than an error."""
    notes: list[str] = []
    tele = SERVICE_URLS["telemetry"]

    def _trend(series: list[float], unit: str, label: str,
               current: float | None = None) -> dict:
        recent_mean, prior_mean = _split_mean(series)
        cur = current if current is not None else (
            series[-1] if series else None
        )
        return {
            "series":    series,
            "current":   cur,
            "delta_pct": _pct_delta(recent_mean, prior_mean),
            "label":     label,
            "unit":      unit,
        }

    # ── throughput rpm — per-minute buckets give us the series directly. ──────
    rpm_series: list[float] = []
    rpm_current: float | None = None
    try:
        r = _http.get(f"{tele}/api/v1/metrics/rpm", params={"window": TRENDS_BUCKET_COUNT})
        if r.status_code == 200:
            body = r.json() or {}
            rpm_series = [
                float(b.get("rpm", 0))
                for b in (body.get("buckets") or [])
                if isinstance(b.get("rpm"), (int, float))
            ]
            cv = body.get("current_rpm")
            rpm_current = float(cv) if isinstance(cv, (int, float)) else None
        else:
            notes.append(f"throughput_rpm: telemetry returned {r.status_code}")
    except Exception as exc:                                # noqa: BLE001
        log.warning("trends: rpm fetch failed: %s", exc)
        notes.append(f"throughput_rpm: telemetry upstream failed: {exc}")

    # ── error rate — single aggregate point per window; sample the recent and
    # prior windows so the tile can still show a delta even without a series. ─
    err_recent: float | None = None
    err_prior: float | None = None
    half_window_s = TRENDS_BUCKET_COUNT * 60 // 2
    try:
        r = _http.get(f"{tele}/api/v1/metrics/backends", params={"window": half_window_s})
        if r.status_code == 200:
            agg = (r.json() or {}).get("aggregate") or {}
            v = agg.get("error_rate_pct")
            err_recent = round(float(v), 2) if isinstance(v, (int, float)) else None
        else:
            notes.append(f"error_rate_pct: telemetry returned {r.status_code}")
        r2 = _http.get(f"{tele}/api/v1/metrics/backends",
                       params={"window": TRENDS_BUCKET_COUNT * 60})
        if r2.status_code == 200:
            agg2 = (r2.json() or {}).get("aggregate") or {}
            v2 = agg2.get("error_rate_pct")
            err_prior = round(float(v2), 2) if isinstance(v2, (int, float)) else None
    except Exception as exc:                                # noqa: BLE001
        log.warning("trends: backends fetch failed: %s", exc)
        notes.append(f"error_rate_pct: telemetry upstream failed: {exc}")

    # ── p95 latency — recent vs prior window. Two latency reads. ──────────────
    p95_recent: float | None = None
    p95_prior: float | None = None
    try:
        r = _http.get(f"{tele}/api/v1/metrics/latency", params={"window": half_window_s})
        if r.status_code == 200:
            v = (r.json() or {}).get("p95_ms")
            p95_recent = float(v) if isinstance(v, (int, float)) else None
        r2 = _http.get(f"{tele}/api/v1/metrics/latency",
                       params={"window": TRENDS_BUCKET_COUNT * 60})
        if r2.status_code == 200:
            v2 = (r2.json() or {}).get("p95_ms")
            p95_prior = float(v2) if isinstance(v2, (int, float)) else None
    except Exception as exc:                                # noqa: BLE001
        log.warning("trends: latency fetch failed: %s", exc)
        notes.append(f"p95_latency_ms: telemetry upstream failed: {exc}")

    # ── SLO compliance — recent vs prior window. ──────────────────────────────
    slo_recent: float | None = None
    slo_prior: float | None = None
    try:
        r = _http.get(f"{tele}/api/v1/metrics/slo", params={"window": half_window_s})
        if r.status_code == 200:
            v = (r.json() or {}).get("compliant_pct")
            slo_recent = float(v) if isinstance(v, (int, float)) else None
        r2 = _http.get(f"{tele}/api/v1/metrics/slo",
                       params={"window": TRENDS_BUCKET_COUNT * 60})
        if r2.status_code == 200:
            v2 = (r2.json() or {}).get("compliant_pct")
            slo_prior = float(v2) if isinstance(v2, (int, float)) else None
    except Exception as exc:                                # noqa: BLE001
        log.warning("trends: slo fetch failed: %s", exc)
        notes.append(f"slo_compliance_pct: telemetry upstream failed: {exc}")

    # ── active backends — current cluster size from the scaling audit. We
    # synthesise a flat 2-point series (prior, current) so the tile can render
    # a sparkline and a step delta from the two most recent distinct counts. ──
    active_series: list[float] = []
    active_current: float | None = None
    try:
        r = _http.get(f"{SERVICE_URLS['autoscaler']}/api/v1/audit/scaling",
                      params={"limit": 50})
        if r.status_code == 200:
            counts = [
                int(row["instance_count"])
                for row in (r.json() or [])
                if isinstance(row.get("instance_count"), int)
            ]
            if counts:
                active_current = float(counts[0])    # newest-first
                # Oldest→newest for the sparkline; cap to the bucket count.
                active_series = [float(c) for c in reversed(counts[:TRENDS_BUCKET_COUNT])]
        else:
            notes.append(f"active_backends: autoscaler returned {r.status_code}")
    except Exception as exc:                                # noqa: BLE001
        log.warning("trends: scaling audit fetch failed: %s", exc)
        notes.append(f"active_backends: autoscaler upstream failed: {exc}")

    label = f"vs prior {TRENDS_BUCKET_COUNT // 2}m"
    return jsonify({
        "throughput_rpm":     _trend(rpm_series, "rpm", label, current=rpm_current),
        "p95_latency_ms": {
            "series":    [],
            "current":   p95_recent,
            "delta_pct": _pct_delta(p95_recent, p95_prior),
            "label":     label,
            "unit":      "ms",
        },
        "slo_compliance_pct": {
            "series":    [],
            "current":   slo_recent,
            "delta_pct": _pct_delta(slo_recent, slo_prior),
            "label":     label,
            "unit":      "%",
        },
        "error_rate_pct": {
            "series":    [],
            "current":   err_recent,
            "delta_pct": _pct_delta(err_recent, err_prior),
            "label":     label,
            "unit":      "%",
        },
        "active_backends":    _trend(active_series, "count", label, current=active_current),
        "window_minutes":     TRENDS_BUCKET_COUNT,
        "last_refreshed":     datetime.now(timezone.utc).isoformat(),
        "notes":              notes,
    })


# ── Forecast summary (flagship hero chart) ────────────────────────────────────
#
# Composes the REAL forecast for the hero chart: aligned actual-vs-forecast
# series, the forecast confidence band, and the scale-ahead decision marker.
# Sources: forecasting /api/v1/forecasts (predicted_rps + confidence bounds),
# telemetry rpm (the actual line, normalised to rps), and the autoscaler
# scaling audit (the most recent forecast-driven actuation = scale-ahead
# marker). Each source degrades independently to empty/null.

# A scaling audit reason produced by the forecast-driven controller path
# carries one of these markers (autoscaler annotates reasons, e.g.
# "forecast predicted 450 rps ... [provision]"). We surface the latest such
# actuation as the scale-ahead decision marker on the hero chart.
_FORECAST_DECISION_HINTS = ("forecast", "predict", "provision")


@app.route("/api/ui/metrics/forecast-summary", methods=["GET"])
def ui_metrics_forecast_summary():
    """Forecast hero-chart bundle: actual rps, forecast rps with confidence
    band, and the scale-ahead decision marker.

    ?window=N seconds (default 3600) bounds both the actual series and the
    forecast lookback. Composed from three independent upstreams; any failure
    degrades that part to empty/null with a note rather than erroring the
    flagship chart."""
    notes: list[str] = []
    try:
        window = int(request.args.get("window", 3600))
    except (TypeError, ValueError):
        window = 3600
    if window <= 0:
        window = 3600

    # ── actual throughput, normalised to rps (telemetry rpm buckets / 60). ────
    actual: list[dict] = []
    try:
        buckets = max(1, window // 60)
        r = _http.get(f"{SERVICE_URLS['telemetry']}/api/v1/metrics/rpm",
                      params={"window": buckets})
        if r.status_code == 200:
            for b in (r.json() or {}).get("buckets") or []:
                rpm = b.get("rpm")
                if isinstance(rpm, (int, float)):
                    actual.append({
                        "time": b.get("time"),
                        "rps":  round(rpm / 60.0, 2),
                    })
        else:
            notes.append(f"actual: telemetry returned {r.status_code}")
    except Exception as exc:                                # noqa: BLE001
        log.warning("forecast-summary: actual fetch failed: %s", exc)
        notes.append(f"actual: telemetry upstream failed: {exc}")

    # ── forecast series + confidence band from the forecasting service. ───────
    forecast: list[dict] = []
    model_name: str | None = None
    model_version: str | None = None
    horizon_minutes: int | None = None
    try:
        r = _http.get(f"{SERVICE_URLS['forecasting']}/api/v1/forecasts",
                      params={"window": window})
        if r.status_code == 200:
            body = r.json() or {}
            rows = body.get("forecasts") or []
            # Oldest→newest so the chart's forecast tail reads left→right.
            for row in reversed(rows):
                pr = row.get("predicted_rps")
                if not isinstance(pr, (int, float)):
                    continue
                forecast.append({
                    "time":             row.get("time"),
                    "predicted_rps":    round(float(pr), 2),
                    "confidence_lower": row.get("confidence_lower"),
                    "confidence_upper": row.get("confidence_upper"),
                    "horizon_minutes":  row.get("horizon_minutes"),
                })
                if model_name is None:
                    model_name = row.get("model_name")
                    model_version = row.get("model_version")
                    hm = row.get("horizon_minutes")
                    horizon_minutes = hm if isinstance(hm, int) else horizon_minutes
        else:
            notes.append(f"forecast: forecasting returned {r.status_code}")
    except Exception as exc:                                # noqa: BLE001
        log.warning("forecast-summary: forecast fetch failed: %s", exc)
        notes.append(f"forecast: forecasting upstream failed: {exc}")

    # ── scale-ahead decision marker: the most recent forecast-driven scaling
    # actuation. We scan the scaling audit for a scale_* row whose reason looks
    # forecast-driven. None when no such actuation exists yet. ────────────────
    scale_ahead: dict | None = None
    try:
        r = _http.get(f"{SERVICE_URLS['autoscaler']}/api/v1/audit/scaling",
                      params={"limit": 50})
        if r.status_code == 200:
            for row in r.json() or []:
                action = (row.get("action") or "")
                reason = (row.get("reason") or "").lower()
                if action.startswith("scale") and any(
                    h in reason for h in _FORECAST_DECISION_HINTS
                ):
                    scale_ahead = {
                        "time":           row.get("time"),
                        "action":         row.get("action"),
                        "instance_count": row.get("instance_count"),
                        "reason":         row.get("reason"),
                    }
                    break
        else:
            notes.append(f"scale_ahead: autoscaler returned {r.status_code}")
    except Exception as exc:                                # noqa: BLE001
        log.warning("forecast-summary: scaling audit fetch failed: %s", exc)
        notes.append(f"scale_ahead: autoscaler upstream failed: {exc}")

    return jsonify({
        "actual":          actual,
        "forecast":        forecast,
        "scale_ahead":     scale_ahead,
        "model_name":      model_name,
        "model_version":   model_version,
        "horizon_minutes": horizon_minutes,
        "window_seconds":  window,
        "notes":           notes,
    })


# ── RL mode (read-only; promotion is deploy-time gated — see notes) ───────────
#
# CASE B: there is NO safe runtime write path to promote RL from shadow to
# active. The mode is pinned at deploy time by the rl-engine's RL_MODE env var;
# `rl_mode` is deliberately NOT a policy field (the policy-manager rejects it).
# The published routing mode is composed from three gates (rl-engine
# runloop.effective_mode): RL_MODE env, policy.safe_mode, policy.operating_mode.
#
# So we expose the TRUE current mode + the recommended mode + whether promotion
# is operator-actionable (it is not, at runtime), letting the frontend present
# an HONEST control instead of a fake "Promote to active" success. The two
# policy gates that ARE operator-writable (safe_mode, operating_mode) are
# surfaced so the UI can explain what would still need to change.

# Strategy → recommended deploy-time RL_MODE pin. Mirrors
# services/shared/config_loader.py STRATEGY_PRIMITIVES (advisory only — never a
# policy field). Used to derive the recommended mode from the live strategy.
_STRATEGY_RECOMMENDED_RL_MODE: dict[str, str | None] = {
    "round-robin":       None,
    "least-connections": None,
    "latency-aware":     "shadow",
    "forecast-aware":    "shadow",
    "anomaly-aware":     "shadow",
    "ai-hybrid":         "active",
    "safe-fallback":     None,
}


@app.route("/api/ui/engines/rl/mode", methods=["GET"])
def ui_engines_rl_mode():
    """Current + recommended RL routing mode, and whether promotion is operator
    actionable.

    CASE B (documented in app.py + README): RL mode is a deploy-time env pin
    (RL_MODE on the rl-engine), not a runtime-writable policy field, so
    `actionable` is always false and there is no POST counterpart. The frontend
    must present the Helmsman "Promote to active" control honestly — as a
    deploy-time recommendation, not a live toggle.

    Reads:
      - rl-engine /api/v1/engine/state for the env-pinned `rl_mode_env`
        (falls back to /health `rl_mode`),
      - policy-manager /api/v1/policy for the operator-writable gates
        (safe_mode, operating_mode) and the derived strategy_name.

    Degrades to current=null with a note when rl-engine is unreachable —
    never a 500."""
    notes: list[str] = []
    current_mode: str | None = None
    runloop_enabled: bool | None = None

    rl_url = SERVICE_URLS["rl-engine"]
    try:
        r = _http.get(f"{rl_url}/api/v1/engine/state")
        if r.status_code == 200:
            body = r.json() or {}
            current_mode = body.get("rl_mode_env")
            runloop_enabled = body.get("runloop_enabled")
        else:
            notes.append(f"current_mode: rl-engine state returned {r.status_code}")
    except Exception as exc:                                # noqa: BLE001
        log.warning("rl mode: engine state fetch failed: %s", exc)
        notes.append(f"current_mode: rl-engine upstream failed: {exc}")

    # Fall back to /health rl_mode if engine/state didn't carry the env pin.
    if current_mode is None:
        try:
            r = _http.get(f"{rl_url}/health")
            if r.status_code in (200, 503):
                current_mode = (r.json() or {}).get("rl_mode")
        except Exception as exc:                            # noqa: BLE001
            log.warning("rl mode: health fallback failed: %s", exc)

    # Operator-writable policy gates + live strategy → recommended mode.
    safe_mode: bool | None = None
    operating_mode: str | None = None
    strategy_name: str | None = None
    recommended_mode: str | None = None
    try:
        r = _http.get(f"{SERVICE_URLS['policy-manager']}/api/v1/policy")
        if r.status_code == 200:
            pol = r.json() or {}
            safe_mode = pol.get("safe_mode")
            operating_mode = pol.get("operating_mode")
            strategy_name = pol.get("strategy_name")
            recommended_mode = _STRATEGY_RECOMMENDED_RL_MODE.get(strategy_name)
        else:
            notes.append(f"recommended_mode: policy-manager returned {r.status_code}")
    except Exception as exc:                                # noqa: BLE001
        log.warning("rl mode: policy fetch failed: %s", exc)
        notes.append(f"recommended_mode: policy-manager upstream failed: {exc}")

    return jsonify({
        # CASE B: deploy-time pin, no runtime write path.
        "current_mode":     current_mode,           # "shadow" | "active" | null
        "recommended_mode": recommended_mode,        # advisory, from live strategy
        "actionable":       False,                   # promotion is NOT a live toggle
        "write_path":       "deploy-time",           # RL_MODE env on rl-engine
        "runloop_enabled":  runloop_enabled,
        # The operator-writable gates that still shape the EFFECTIVE routing
        # mode even when RL_MODE=active (rl-engine runloop.effective_mode).
        "policy_gates": {
            "safe_mode":      safe_mode,
            "operating_mode": operating_mode,
            "strategy_name":  strategy_name,
        },
        "explanation": (
            "RL routing mode is pinned at deploy time by the rl-engine RL_MODE "
            "environment variable and is not a runtime-writable policy field. "
            "Promotion to active is an operational/deploy change, not a live "
            "control. The effective published mode is also gated by the policy "
            "safe_mode and operating_mode, which are operator-writable."
        ),
        "notes": notes,
    })


# ── Isolation audit (Ledger isolation rows) ───────────────────────────────────
#
# Real isolation / exclusion events for the Ledger, derived from the
# anomaly-detector's verdict history (backend_health) cross-referenced with the
# live smartload.anomaly ring buffer (which carries severity + the
# metric/observed/threshold evidence + the publishing source = actor). A
# verdict of status != "healthy" is an isolation/exclusion event. The
# backend_health table has no actor/reason column, so we default actor to the
# anomaly-detector engine and derive a human reason from the evidence; manual
# isolations surface through the ring-buffer source.

# How far back the isolation audit reads by default, in seconds.
ISOLATION_AUDIT_WINDOW_SECONDS = 86400


def _isolation_reason(metric, observed, threshold, status) -> str:
    """Human reason for an isolation row from whatever evidence is present."""
    if metric is not None and observed is not None:
        obs = (f"{observed:.2f}".rstrip("0").rstrip(".")
               if isinstance(observed, (int, float)) else observed)
        if threshold is not None and isinstance(threshold, (int, float)):
            thr = f"{threshold:.2f}".rstrip("0").rstrip(".")
            return f"{metric} {obs} crossed threshold {thr}"
        return f"{metric} {obs}"
    return f"health verdict: {status}"


@app.route("/api/ui/audit/isolation", methods=["GET"])
def ui_audit_isolation():
    """Isolation / exclusion event list for the Ledger.

    A row is emitted for each backend_health verdict with status != "healthy"
    inside ?window=N seconds (default 1 day). Shape per row: time, backend_id,
    status, score, actor, reason. Evidence (severity / metric / observed /
    threshold / actor) is enriched from the live smartload.anomaly ring buffer
    keyed by (backend_id, time); manual isolations carry their actor through the
    envelope source.

    ?limit=N caps the rows (newest first). Degrades to an empty list with HTTP
    200 if the anomaly-detector is unreachable — the Ledger renders calmly."""
    _start_engines_subscriber()

    try:
        window = int(request.args.get("window", ISOLATION_AUDIT_WINDOW_SECONDS))
    except (TypeError, ValueError):
        window = ISOLATION_AUDIT_WINDOW_SECONDS
    if window <= 0:
        window = ISOLATION_AUDIT_WINDOW_SECONDS
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    if limit <= 0:
        limit = 100
    limit = min(limit, 1000)

    # Build an evidence index from the ring buffer keyed by (backend_id, time)
    # so we can attach severity / metric evidence / actor to a verdict row. The
    # AnomalyEvent carries the source (publishing service) on its envelope, used
    # as the actor when present.
    evidence: dict[tuple, dict] = {}
    try:
        for entry in _engines_buf.recent():
            if entry.get("channel") != "smartload.anomaly":
                continue
            payload = entry.get("payload", {}) or {}
            env = entry.get("envelope", {}) or {}
            bid = payload.get("backend_id")
            ts = env.get("timestamp")
            if bid is None or ts is None:
                continue
            evidence[(bid, ts)] = {
                "severity":       payload.get("severity"),
                "metric":         payload.get("metric"),
                "observed_value": payload.get("observed_value"),
                "threshold":      payload.get("threshold"),
                "actor":          env.get("source"),
            }
    except Exception as exc:                                # noqa: BLE001
        log.warning("isolation audit: ring buffer read failed: %s", exc)

    rows: list[dict] = []
    try:
        r = _http.get(
            f"{SERVICE_URLS['anomaly-detector']}/api/v1/anomaly/history",
            params={"window": window, "limit": limit * 5},
        )
        if r.status_code != 200:
            log.warning("isolation audit: anomaly history returned %s", r.status_code)
            return jsonify([])
        history = (r.json() or {}).get("history") or []
    except Exception as exc:                                # noqa: BLE001
        log.warning("isolation audit: anomaly history fetch failed: %s", exc)
        return jsonify([])

    for h in history:
        status = h.get("status")
        if not status or status == "healthy":
            continue   # only isolation/exclusion events
        bid = h.get("backend_id")
        ts = h.get("time")
        ev = evidence.get((bid, ts), {})
        severity = ev.get("severity") or (
            "critical" if status == "unhealthy" else "warning"
        )
        rows.append({
            "time":       ts,
            "backend_id": bid,
            "status":     status,
            "score":      h.get("score"),
            "severity":   severity,
            "actor":      ev.get("actor") or "anomaly-detector",
            "reason":     _isolation_reason(
                ev.get("metric"), ev.get("observed_value"),
                ev.get("threshold"), status,
            ),
        })
        if len(rows) >= limit:
            break

    # History is already newest-first from the upstream; keep that ordering.
    return jsonify(rows)


# ── System topology (powers the System view — every service, live) ────────────
#
# One payload that guarantees the System view can show the WHOLE architecture
# live: every SmartLoad service as a node (id, display name, role, health,
# last-activity, one key live metric) plus the data-flow edges between them.
# Built from the existing health fan-out + SERVICE_URLS + the engines ring
# buffer (for channel activity). The two headless shippers (resource-collector,
# lb-otel-shipper) have no HTTP surface, so they appear as nodes with a
# "headless" status and no health probe — never omitted.

# Static node catalogue. (id, display_name, role, http) — http=False for the
# headless OTLP shippers, which have no /health to probe. Order is roughly the
# data-flow order so a layout pass reads top-to-bottom.
_TOPOLOGY_NODES: tuple[tuple[str, str, str, bool], ...] = (
    ("load-balancer",     "Load Balancer",      "NGINX upstream proxy routing client traffic to backends", True),
    ("lb-sidecar",        "LB Sidecar",         "Applies routing weights and backend exclusions to the load balancer", True),
    ("lb-otel-shipper",   "LB OTel Shipper",    "Ships load-balancer access logs to the telemetry pipeline", False),
    ("resource-collector", "Resource Collector", "Ships per-container CPU and memory stats to the telemetry pipeline", False),
    ("telemetry",         "Telemetry",          "Receives metrics and persists the time-series store", True),
    ("forecasting",       "Forecasting",        "Predicts near-term demand to drive scale-ahead decisions", True),
    ("anomaly-detector",  "Anomaly Detector",   "Scores backend health and isolates unhealthy backends", True),
    ("rl-engine",         "RL Engine",          "Recommends routing weights from a learned policy (shadow by default)", True),
    ("autoscaler",        "Autoscaler",         "Scales the backend pool from forecast and capacity signals", True),
    ("policy-manager",    "Policy Manager",     "Owns the operating policy and named strategies", True),
    ("operator-ui",       "Operator UI",        "This console — reads every service and presents the system", True),
)

# Data-flow edges (source → target, label). Mirrors the SmartLoad event +
# request topology: client traffic through the LB, metrics into telemetry, the
# AI plane reasoning off telemetry and acting through the sidecar/autoscaler,
# and the policy plane configuring everyone.
_TOPOLOGY_EDGES: tuple[tuple[str, str, str], ...] = (
    ("load-balancer",      "lb-otel-shipper",   "access logs"),
    ("lb-otel-shipper",    "telemetry",         "request metrics"),
    ("resource-collector", "telemetry",         "cpu / memory"),
    ("telemetry",          "forecasting",       "demand history"),
    ("telemetry",          "anomaly-detector",  "backend signals"),
    ("telemetry",          "rl-engine",         "routing state"),
    ("telemetry",          "autoscaler",        "capacity signals"),
    ("forecasting",        "autoscaler",        "forecast"),
    ("anomaly-detector",   "lb-sidecar",        "health verdicts"),
    ("rl-engine",          "lb-sidecar",        "routing recommendation"),
    ("lb-sidecar",         "load-balancer",     "weights / exclusions"),
    ("autoscaler",         "load-balancer",     "backend pool size"),
    ("policy-manager",     "forecasting",       "policy"),
    ("policy-manager",     "anomaly-detector",  "policy"),
    ("policy-manager",     "rl-engine",         "policy"),
    ("policy-manager",     "autoscaler",        "policy"),
    ("policy-manager",     "lb-sidecar",        "policy"),
)

# Per-service Redis channel → used to surface the last-activity timestamp from
# the ring buffer for the AI plane nodes.
_TOPOLOGY_NODE_CHANNEL: dict[str, str] = {
    "anomaly-detector": "smartload.anomaly",
    "forecasting":      "smartload.forecast",
    "rl-engine":        "smartload.routing",
    "autoscaler":       "smartload.scale",
}


def _topology_key_metric(node_id: str, health: dict) -> dict | None:
    """Pick one human key metric per node from its /health body. Returns
    {label, value} or None when nothing meaningful is available."""
    extra = health.get("extra") or {}
    status = health.get("status")
    if node_id == "load-balancer":
        uc = extra.get("upstream_count")
        if uc is not None:
            return {"label": "upstreams", "value": uc}
    if node_id == "autoscaler":
        stats = extra.get("stats") or {}
        if isinstance(stats, dict) and "actions_total" in stats:
            return {"label": "decisions", "value": stats.get("actions_total")}
        tc = extra.get("active_target_count")
        if tc is not None:
            return {"label": "target backends", "value": tc}
    if node_id == "policy-manager":
        pv = extra.get("policy_version")
        if pv is not None:
            return {"label": "policy version", "value": pv}
    if node_id == "rl-engine":
        rm = extra.get("rl_mode")
        if rm is not None:
            return {"label": "rl mode", "value": rm}
    if node_id in ("forecasting", "anomaly-detector"):
        age = extra.get("last_inference_age_seconds")
        if age is not None:
            return {"label": "last inference", "value": f"{age}s ago"}
    if node_id == "lb-sidecar":
        ex = extra.get("excluded_backends")
        if isinstance(ex, list):
            return {"label": "excluded backends", "value": len(ex)}
    # Generic fallback: surface the health status itself as the key metric.
    if status is not None:
        return {"label": "status", "value": status}
    return None


@app.route("/api/ui/system/topology", methods=["GET"])
def ui_system_topology():
    """Whole-system live topology for the System view: every SmartLoad service
    as a node + the data-flow edges between them.

    Per node: id, display name, one-line role, health status, last-activity
    timestamp (where knowable, from the engines ring buffer), and one key live
    metric pulled from the service's /health. The two headless OTLP shippers
    have no HTTP surface, so they appear with status "headless" and no probe —
    they are never omitted, so the operator sees the complete architecture.

    Always 200. Each health probe is independent; an unreachable service shows
    status "unreachable" rather than dropping out of the graph."""
    _start_engines_subscriber()

    # Probe health for every HTTP node in parallel (reuse _fetch_health).
    http_nodes = [n for n in _TOPOLOGY_NODES if n[3]]
    health_by_id: dict[str, dict] = {}
    probe_targets = [
        (node_id, SERVICE_URLS[node_id])
        for (node_id, _disp, _role, _http_ok) in http_nodes
        if node_id in SERVICE_URLS
    ]
    # operator-ui is this process — synthesise its own health rather than
    # calling back into ourselves.
    health_by_id["operator-ui"] = {
        "status": "ok", "status_code": 200, "extra": {},
    }
    if probe_targets:
        try:
            with ThreadPoolExecutor(max_workers=len(probe_targets)) as pool:
                for name, h in pool.map(lambda kv: _fetch_health(*kv), probe_targets):
                    health_by_id[name] = h
        except Exception as exc:                            # noqa: BLE001
            log.warning("topology: health fan-out failed: %s", exc)

    # Last-activity per AI-plane node from the ring buffer (latest envelope
    # timestamp on that node's channel).
    last_activity: dict[str, str] = {}
    try:
        latest_on_channel: dict[str, str] = {}
        for entry in _engines_buf.recent():
            ch = entry.get("channel")
            ts = entry.get("envelope", {}).get("timestamp")
            if ch and ts and (ch not in latest_on_channel or ts > latest_on_channel[ch]):
                latest_on_channel[ch] = ts
        for node_id, ch in _TOPOLOGY_NODE_CHANNEL.items():
            if ch in latest_on_channel:
                last_activity[node_id] = latest_on_channel[ch]
    except Exception as exc:                                # noqa: BLE001
        log.warning("topology: ring buffer read failed: %s", exc)

    nodes: list[dict] = []
    for node_id, display_name, role, http_ok in _TOPOLOGY_NODES:
        if not http_ok:
            # Headless shipper — no /health to probe.
            nodes.append({
                "id":            node_id,
                "display_name":  display_name,
                "role":          role,
                "status":        "headless",
                "http":          False,
                "last_activity": None,
                "key_metric":    None,
            })
            continue
        health = health_by_id.get(node_id, {"status": "unreachable", "extra": {}})
        nodes.append({
            "id":            node_id,
            "display_name":  display_name,
            "role":          role,
            "status":        health.get("status", "unreachable"),
            "http":          True,
            "last_activity": last_activity.get(node_id),
            "key_metric":    _topology_key_metric(node_id, health),
        })

    edges = [
        {"source": s, "target": t, "label": label}
        for (s, t, label) in _TOPOLOGY_EDGES
    ]

    return jsonify({
        "nodes":          nodes,
        "edges":          edges,
        "generated_at":   datetime.now(timezone.utc).isoformat(),
    })


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
    log.info("starting on port %d (web_dist=%s, openapi=%s, asyncapi=%s)",
             PORT, WEB_DIST, OPENAPI_PATH, ASYNCAPI_PATH)
    app.run(host="0.0.0.0", port=PORT)
