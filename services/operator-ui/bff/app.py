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

    # TODO(#telemetry): once telemetry-service exposes a /api/v1/metrics/rpm
    # endpoint (or equivalent Prom scrape proxy), wire throughput_rpm and
    # requests_total here. Until then we return null + a note so the UI can
    # display "—" rather than misleading zeros.
    throughput_rpm = None
    requests_total = None
    notes.append("throughput_rpm: requires telemetry-service /api/v1/metrics/rpm")
    notes.append("requests_total: requires telemetry-service /api/v1/metrics/rpm")

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
