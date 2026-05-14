"""
services/operator-ui/bff/app.py
────────────────────────────────
Operator UI backend-for-frontend (BFF).

Responsibilities (slice #1 scope):
  - Aggregate /health from every SmartLoad service for the Home page
  - Proxy /api/ui/policy and /api/ui/audit/policy to policy-manager
  - Serve Swagger UI at /api/docs reading docs/openapi/smartload-v1.yaml
  - Serve the React build at / (production) — Vite dev server handles dev

Out of scope for slice #1 (separate issues):
  - Live engines event stream (#121)
  - Manual actions (#123)
  - Audit log viewer for scaling events (#122)
  - Operator login (#125)
  - Embedded charts (#131)
"""

from __future__ import annotations

import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import httpx
from flask import Flask, jsonify, request, send_from_directory

try:
    from flask_swagger_ui import get_swaggerui_blueprint
except ImportError:  # pragma: no cover
    get_swaggerui_blueprint = None  # type: ignore


SERVICE_NAME = os.environ.get("SERVICE_NAME", "operator-ui")
PORT         = int(os.environ.get("PORT", "8090"))

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
    static_folder=WEB_DIST,
    static_url_path="",
)

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


# ── BFF own health ────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({
        "status":  "ok",
        "service": SERVICE_NAME,
    })


# ── SPA fallback ──────────────────────────────────────────────────────────────
# Flask auto-registers a static endpoint when static_folder is set, and that
# endpoint returns 404 for paths that don't map to real files. To make the
# React Router routes (/, /policy, /<anything>) survive page reloads + direct
# URL access, we serve index.html on every 404 that isn't an /api/* call. The
# 404 handler runs AFTER the static handler and AFTER all routed endpoints,
# so real files still win and real /api/* 404s still surface.

@app.route("/")
def serve_root():
    return send_from_directory(WEB_DIST, "index.html")


@app.errorhandler(404)
def spa_fallback(_err):
    # Real API misses stay as 404 (don't shadow them with a 200 + HTML body).
    if request.path.startswith("/api/"):
        return jsonify({"error": "not found", "path": request.path}), 404
    index_path = os.path.join(WEB_DIST, "index.html")
    if os.path.isfile(index_path):
        return send_from_directory(WEB_DIST, "index.html")
    return jsonify({
        "service": SERVICE_NAME,
        "message": "Operator UI BFF up; web/ build not found",
        "web_dist": WEB_DIST,
    }), 404


if __name__ == "__main__":
    log.info("starting on port %d (web_dist=%s, openapi=%s)", PORT, WEB_DIST, OPENAPI_PATH)
    app.run(host="0.0.0.0", port=PORT)
