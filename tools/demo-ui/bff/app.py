"""
tools/demo-ui/bff/app.py
─────────────────────────
Demo UI backend-for-frontend (BFF).

Purpose: developer end-to-end validation of the SmartLoad AI pipeline.
         Exposes scenario orchestration, fault injection, and a live event
         stream so developers can trigger failures and watch the system react.

This service is intentionally separate from the operator-ui (port 8090).
The operator-ui is for managers controlling a live system; this is a test
harness for developers validating system behaviour.

Endpoints:
  GET  /api/ui/demo/state         aggregated lb + rl + anomaly + policy state
  GET  /api/ui/demo/services      health grid across every SmartLoad service
  GET  /api/ui/demo/livestats     1-shot live RPS / p95 / pool-size sample
  POST /api/ui/demo/degrade       mark a backend degraded/unhealthy
  POST /api/ui/demo/recover       restore a backend to healthy
  POST /api/ui/demo/mode          toggle safe_mode on the policy
  POST /api/ui/demo/traffic       start/stop Locust traffic load
  POST /api/ui/demo/chaos         inject latency/failure into a backend
  POST /api/ui/demo/reset         full orchestrated reset to baseline
  POST /api/ui/demo/scenario      run a named multi-step scenario
  POST /api/ui/demo/algorithm     pick the LB routing algorithm
  GET  /api/ui/demo/metrics       last-5m latency snapshot from TimescaleDB
  GET  /api/ui/demo/bench/profiles               list one-click load profiles
  GET  /api/ui/demo/bench/status                 current/last automated-run state
  POST /api/ui/demo/bench/start                  start a named load profile
  POST /api/ui/demo/bench/stop                   stop the active load profile
  GET  /api/ui/demo/benchmark/suites             list result suites (adaptive/baseline)
  GET  /api/ui/demo/benchmark/<suite>/runs               list runs for a suite
  GET  /api/ui/demo/benchmark/<suite>/runs/<ts>/manifest MANIFEST.json for one run
  GET  /api/ui/demo/benchmark/<suite>/runs/<ts>/summary  SUMMARY.md for one run
  GET  /api/ui/demo/benchmark/<suite>/runs/<ts>/plot/<n> one PNG plot for a run
  GET  /api/ui/demo/benchmark/runs[...]          back-compat aliases → baseline suite
  GET  /api/ui/events             SSE stream of routing/anomaly/policy/scale
  GET  /health                    own health check

The automated-run state machine ("one-click load profiles") lives in Redis so
it stays consistent across the gunicorn worker pool: the worker that accepts
/bench/start runs the profile thread; every worker reads/writes the shared
`demo:bench:state` key for /bench/status and the `demo:bench:stop` flag.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import redis as redis_lib
from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

try:
    import psycopg2 as _psycopg2
    _psycopg2_available = True
except ImportError:   # not installed — metrics endpoint returns 503
    _psycopg2 = None  # type: ignore[assignment]
    _psycopg2_available = False


SERVICE_NAME = os.environ.get("SERVICE_NAME", "demo-ui")
PORT         = int(os.environ.get("PORT", "8091"))

REDIS_URL             = os.environ.get("REDIS_URL",             "redis://redis:6379")
TIMESCALEDB_URL       = os.environ.get("TIMESCALEDB_URL",       "")
TRAFFIC_SIMULATOR_URL = os.environ.get("TRAFFIC_SIMULATOR_URL", "http://traffic-simulator:8089")
BACKEND_URLS          = os.environ.get("BACKEND_URLS",          "")
DEMO_TOKEN            = os.environ.get("DEMO_TOKEN",            "")
DEMO_METRICS_WINDOW   = os.environ.get("DEMO_METRICS_WINDOW",   "5 minutes")
BENCHMARK_RESULTS_DIR = os.environ.get("BENCHMARK_RESULTS_DIR", "/benchmark-results")
ADAPTIVE_RESULTS_DIR  = os.environ.get("ADAPTIVE_RESULTS_DIR",  "/adaptive-results")

# Service URLs — only the ones the demo BFF actually calls.
SERVICE_URLS: dict[str, str] = {
    "policy-manager":   os.environ.get("POLICY_MANAGER_URL",   "http://policy-manager:8086"),
    "anomaly-detector": os.environ.get("ANOMALY_DETECTOR_URL", "http://anomaly-detector:8082"),
    "forecasting":      os.environ.get("FORECASTING_URL",      "http://forecasting:8083"),
    "rl-engine":        os.environ.get("RL_ENGINE_URL",        "http://rl-engine:8084"),
    "autoscaler":       os.environ.get("AUTOSCALER_URL",       "http://autoscaler:8085"),
    "telemetry":        os.environ.get("TELEMETRY_URL",        "http://telemetry:8081"),
    "lb-sidecar":       os.environ.get("LB_SIDECAR_URL",       "http://lb-sidecar:8087"),
}

# Health-grid order: which services the Dashboard polls, and their role tag.
SERVICE_GRID: list[tuple[str, str]] = [
    ("policy-manager",   "control"),
    ("anomaly-detector", "decision"),
    ("forecasting",      "decision"),
    ("rl-engine",        "decision"),
    ("autoscaler",       "decision"),
    ("telemetry",        "data"),
    ("lb-sidecar",       "data"),
]

# ── Benchmark result suites ───────────────────────────────────────────────────
# Each suite is a results root + the canonical plot-key → filename map for that
# harness. The Benchmarks page renders one tab per suite.
SUITES: dict[str, dict] = {
    "adaptive": {
        "label":  "Adaptive-bench (RQ4)",
        "root":   ADAPTIVE_RESULTS_DIR,
        "harness": "experiments/adaptive-bench/run.py",
        "plots": {
            "pool_size":       ("plot_pool_size.png",       "Pool size vs. offered load"),
            "time_to_react":   ("plot_time_to_react.png",   "Forecast → autoscaler reaction delay"),
            "upstream_timeline": ("plot_upstream_timeline.png", "Upstream rewrites over the run"),
            "anomaly_recovery": ("plot_anomaly_recovery.png", "Latency recovery after the phase-D anomaly"),
        },
    },
    "baseline": {
        "label":  "Baseline vs. SmartLoad (#148)",
        "root":   BENCHMARK_RESULTS_DIR,
        "harness": "experiments/baseline-vs-smartload/scripts/run_experiment.sh",
        "plots": {
            "rps":            ("plot_rps.png",            "Sustained RPS over time"),
            "p50_p95_p99":    ("plot_p50_p95_p99.png",    "Latency percentiles (p50 / p95 / p99)"),
            "error_rate":     ("plot_error_rate.png",     "Failure rate during the run"),
            "recovery_curve": ("plot_recovery_curve.png", "Recovery curve near the anomaly window"),
            "per_phase_p95":  ("plot_per_phase_p95.png",  "Per-phase p95"),
            "total_requests": ("plot_total_requests.png", "Cumulative request count"),
        },
    },
}

# ── One-click load profiles ───────────────────────────────────────────────────
# Each profile is a timed sequence of phases the BFF drives over HTTP against
# the traffic-simulator (swarm), plus an optional phase-D anomaly injected via
# the same /admin/chaos + /api/v1/isolate path the manual scenarios already use.
# These reproduce the adaptive-bench 5-phase shape without the host-side
# run.py orchestrator — the live autoscaler reacts within the compose pool
# (replicas 1..5), which the live monitor charts.
BENCH_PROFILES: list[dict] = [
    {
        "id": "adaptive_quick",
        "label": "Adaptive · Quick (60s)",
        "description": "1-minute 5-phase shape — fastest way to see the pool grow + recover.",
        "phases": [
            {"name": "A_bootstrap",          "secs": 10, "users": 5,  "spawn": 5},
            {"name": "B_forecast_burst",     "secs": 10, "users": 30, "spawn": 30},
            {"name": "C_sustain",            "secs": 20, "users": 30, "spawn": 30},
            {"name": "D_anomaly_scale_down", "secs": 10, "users": 8,  "spawn": 8, "anomaly": True},
            {"name": "E_steady",             "secs": 10, "users": 8,  "spawn": 8},
        ],
    },
    {
        "id": "adaptive_standard",
        "label": "Adaptive · Standard (3m)",
        "description": "3-minute 5-phase shape with a longer sustain window for clearer scale-out.",
        "phases": [
            {"name": "A_bootstrap",          "secs": 30, "users": 10,  "spawn": 5},
            {"name": "B_forecast_burst",     "secs": 20, "users": 120, "spawn": 20},
            {"name": "C_sustain",            "secs": 70, "users": 120, "spawn": 20},
            {"name": "D_anomaly_scale_down", "secs": 30, "users": 25,  "spawn": 10, "anomaly": True},
            {"name": "E_steady",             "secs": 30, "users": 25,  "spawn": 10},
        ],
    },
    {
        "id": "spike",
        "label": "Spike & drop (90s)",
        "description": "Idle → hard spike → idle. Watch the autoscaler chase a step change.",
        "phases": [
            {"name": "idle",  "secs": 20, "users": 5,   "spawn": 5},
            {"name": "spike", "secs": 40, "users": 150, "spawn": 50},
            {"name": "drop",  "secs": 30, "users": 5,   "spawn": 5},
        ],
    },
    {
        "id": "anomaly_storm",
        "label": "Anomaly under load (90s)",
        "description": "Steady load with a mid-run latency anomaly — exercises the reroute path.",
        "phases": [
            {"name": "warmup",  "secs": 25, "users": 40, "spawn": 20},
            {"name": "anomaly", "secs": 40, "users": 40, "spawn": 20, "anomaly": True},
            {"name": "recover", "secs": 25, "users": 40, "spawn": 20},
        ],
    },
]

BENCH_STATE_KEY = "demo:bench:state"
BENCH_STOP_KEY  = "demo:bench:stop"
BENCH_ANOMALY_DELAY_MS = 200

WEB_DIST = os.environ.get(
    "WEB_DIST",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web", "dist"),
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [demo-ui] %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("demo-ui")

app = Flask(
    __name__,
    static_folder=os.path.join(WEB_DIST, "assets"),
    static_url_path="/assets",
)

_http = httpx.Client(timeout=httpx.Timeout(5.0, connect=2.0))


# ── backend helpers ───────────────────────────────────────────────────────────

def _backend_map() -> dict[str, str]:
    """Parse BACKEND_URLS (comma-separated host:port) into {hostname: http_url}."""
    result: dict[str, str] = {}
    for entry in BACKEND_URLS.split(","):
        entry = entry.strip()
        if not entry:
            continue
        host, _, port = entry.partition(":")
        result[host] = f"http://{host}:{port or '8080'}"
    return result


def _ip_to_name_map() -> dict[str, str]:
    """Resolve backend hostnames → IPs via Docker DNS to translate RL ranking IDs.

    The RL engine sources backend_ids from TimescaleDB metrics (IP:port), while
    the lb-sidecar uses container-name:port.  This map lets the BFF normalise
    rankings to the same hostnames shown in upstream_weights.
    """
    result: dict[str, str] = {}
    prev_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(2.0)
    try:
        for host in _backend_map():
            try:
                ip = socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
                result[f"{ip}:8080"] = f"{host}:8080"
            except (socket.gaierror, OSError):
                pass
    finally:
        socket.setdefaulttimeout(prev_timeout)
    return result


# ── demo state aggregator ─────────────────────────────────────────────────────

def _fan_out_demo_state() -> dict:
    """Parallel fetch of lb-sidecar, rl-engine, anomaly-detector, and policy state."""
    def fetch_lb():
        try:
            r = _http.get(f"{SERVICE_URLS['lb-sidecar']}/api/v1/lb/state")
            return r.json() if r.status_code == 200 else {}
        except Exception:
            return {}

    def fetch_rl():
        try:
            r = _http.get(f"{SERVICE_URLS['rl-engine']}/health")
            return r.json() if r.status_code in (200, 503) else {}
        except Exception:
            return {}

    def fetch_anomaly():
        try:
            r = _http.get(f"{SERVICE_URLS['anomaly-detector']}/health")
            return r.json() if r.status_code in (200, 503) else {}
        except Exception:
            return {}

    def fetch_policy():
        try:
            r = _http.get(f"{SERVICE_URLS['policy-manager']}/api/v1/policy")
            return r.json() if r.status_code == 200 else {}
        except Exception:
            return {}

    with ThreadPoolExecutor(max_workers=4) as pool:
        lb_fut      = pool.submit(fetch_lb)
        rl_fut      = pool.submit(fetch_rl)
        anomaly_fut = pool.submit(fetch_anomaly)
        policy_fut  = pool.submit(fetch_policy)
        lb_state    = lb_fut.result()
        rl_health   = rl_fut.result()
        anomaly     = anomaly_fut.result()
        policy      = policy_fut.result()

    weights = lb_state.get("upstream_weights", {})
    all_excluded = lb_state.get("excluded_backends", [])
    filtered_excluded = [b for b in all_excluded if b in weights]

    raw_rankings = rl_health.get("last_rankings") or []
    if raw_rankings:
        ip_map = _ip_to_name_map()
        raw_rankings = [
            {"backend_id": ip_map.get(r["backend_id"], r["backend_id"]),
             "score": r["score"]}
            for r in raw_rankings
        ]

    return {
        "upstream_weights":           weights,
        "excluded_backends":          filtered_excluded,
        "algorithm":                  lb_state.get("algorithm", "round_robin"),
        "rl_mode":                    rl_health.get("rl_mode"),
        "policy_type":                rl_health.get("policy_type"),
        "policy_ready":               rl_health.get("policy_ready"),
        "last_inference_age_seconds": rl_health.get("last_inference_age_seconds"),
        "last_rankings":              raw_rankings or None,
        "anomaly_engine":             anomaly.get("engine_type"),
        "safe_mode":                  policy.get("safe_mode"),
        "backend_names":              list(_backend_map().keys()),
    }


# ── demo endpoints ────────────────────────────────────────────────────────────

@app.route("/api/ui/demo/state", methods=["GET"])
def ui_demo_state():
    return jsonify(_fan_out_demo_state())


@app.route("/api/ui/demo/degrade", methods=["POST"])
def ui_demo_degrade():
    body    = request.get_json(silent=True) or {}
    backend = body.get("backend_id", "")
    level   = body.get("level", "unhealthy")
    if not backend:
        return jsonify({"error": "backend_id required"}), 400
    upstream = SERVICE_URLS["anomaly-detector"]
    try:
        r = _http.post(
            f"{upstream}/api/v1/isolate",
            json={"backend_id": backend, "status": level,
                  "actor": "demo", "reason": "demo scenario"},
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    return (r.text, r.status_code, {"Content-Type": "application/json"})


@app.route("/api/ui/demo/recover", methods=["POST"])
def ui_demo_recover():
    body    = request.get_json(silent=True) or {}
    backend = body.get("backend_id", "")
    if not backend:
        return jsonify({"error": "backend_id required"}), 400
    upstream = SERVICE_URLS["anomaly-detector"]
    try:
        r = _http.post(
            f"{upstream}/api/v1/isolate",
            json={"backend_id": backend, "status": "healthy",
                  "actor": "demo", "reason": "demo recovery"},
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    return (r.text, r.status_code, {"Content-Type": "application/json"})


@app.route("/api/ui/demo/mode", methods=["POST"])
def ui_demo_mode():
    body      = request.get_json(silent=True) or {}
    safe_mode = body.get("safe_mode")
    if safe_mode is None:
        return jsonify({"error": "safe_mode required"}), 400
    upstream = SERVICE_URLS["policy-manager"]
    try:
        r = _http.post(
            f"{upstream}/api/v1/policy",
            json={"safe_mode": bool(safe_mode)},
            headers={"Content-Type": "application/json", "X-Actor": "demo"},
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    return (r.text, r.status_code, {"Content-Type": "application/json"})


@app.route("/api/ui/demo/traffic", methods=["POST"])
def ui_demo_traffic():
    body       = request.get_json(silent=True) or {}
    users      = int(body.get("users", 0))
    spawn_rate = int(body.get("spawn_rate", 1))
    base       = TRAFFIC_SIMULATOR_URL.rstrip("/")
    try:
        if users == 0:
            r = _http.get(f"{base}/stop")
        else:
            r = _http.post(
                f"{base}/swarm",
                data={"user_count": users, "spawn_rate": spawn_rate},
            )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    try:
        return jsonify(r.json())
    except Exception:
        return (r.text, r.status_code, {"Content-Type": "text/plain"})


@app.route("/api/ui/demo/chaos", methods=["POST"])
def ui_demo_chaos():
    body       = request.get_json(silent=True) or {}
    backend_id = body.get("backend_id", "")
    if not backend_id:
        return jsonify({"error": "backend_id required"}), 400
    bmap = _backend_map()
    url  = bmap.get(backend_id)
    if not url:
        return jsonify({"error": f"unknown backend_id: {backend_id}"}), 404
    headers = {"Content-Type": "application/json"}
    if DEMO_TOKEN:
        headers["X-Demo-Token"] = DEMO_TOKEN
    chaos_body = {
        "delay_ms":    int(body.get("delay_ms",    0)),
        "fail_health": bool(body.get("fail_health", False)),
        "fail_all":    bool(body.get("fail_all",    False)),
    }
    try:
        r = _http.post(f"{url}/admin/chaos", json=chaos_body, headers=headers)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    return (r.text, r.status_code, {"Content-Type": "application/json"})


@app.route("/api/ui/demo/reset", methods=["POST"])
def ui_demo_reset():
    """Orchestrated reset: safe_mode=false → equal weights → recover all backends
    → clear chaos → low traffic.
    """
    steps = []

    def _step(name, fn):
        try:
            fn()
            steps.append({"step": name, "ok": True})
        except Exception as exc:
            steps.append({"step": name, "ok": False, "error": str(exc)})

    _step("safe_mode_off", lambda: _http.post(
        f"{SERVICE_URLS['policy-manager']}/api/v1/policy",
        json={"safe_mode": False},
        headers={"Content-Type": "application/json", "X-Actor": "demo-reset"},
    ))

    bmap   = _backend_map()
    all_be = [f"{h}:8080" if ":8080" not in h else h for h in bmap]
    if all_be:
        equal = {b: 1 for b in all_be}
        _step("equal_weights", lambda: _http.post(
            f"{SERVICE_URLS['lb-sidecar']}/api/v1/lb/weights",
            json=equal,
            headers={"Content-Type": "application/json"},
        ))

    state = _fan_out_demo_state()
    for backend in state.get("excluded_backends", []):
        _step(f"recover_{backend}", lambda b=backend: _http.post(
            f"{SERVICE_URLS['anomaly-detector']}/api/v1/isolate",
            json={"backend_id": b, "status": "healthy",
                  "actor": "demo-reset", "reason": "reset all"},
        ))

    chaos_clear = {"delay_ms": 0, "fail_health": False, "fail_all": False}
    headers = {"Content-Type": "application/json"}
    if DEMO_TOKEN:
        headers["X-Demo-Token"] = DEMO_TOKEN
    for host, url in bmap.items():
        _step(f"chaos_clear_{host}", lambda u=url: _http.post(
            f"{u}/admin/chaos", json=chaos_clear, headers=headers,
        ))

    _step("traffic_low", lambda: _http.post(
        f"{TRAFFIC_SIMULATOR_URL.rstrip('/')}/swarm",
        data={"user_count": 5, "spawn_rate": 1},
    ))

    return jsonify({"ok": True, "steps": steps})


_SCENARIO_BACKEND3 = "smartload-test-backend-3"
_SCENARIO_BACKEND1 = "smartload-test-backend-1"


@app.route("/api/ui/demo/scenario", methods=["POST"])
def ui_demo_scenario():
    """Execute a named demo scenario as a server-side sequence."""
    body     = request.get_json(silent=True) or {}
    scenario = body.get("scenario", "")
    steps    = []

    def _step(name, fn):
        try:
            fn()
            steps.append({"step": name, "ok": True})
        except Exception as exc:
            steps.append({"step": name, "ok": False, "error": str(exc)})

    anomaly_url   = f"{SERVICE_URLS['anomaly-detector']}/api/v1/isolate"
    traffic_base  = TRAFFIC_SIMULATOR_URL.rstrip("/")
    bmap          = _backend_map()
    chaos_headers = {"Content-Type": "application/json"}
    if DEMO_TOKEN:
        chaos_headers["X-Demo-Token"] = DEMO_TOKEN

    if scenario == "backend_failure":
        _step("degrade_backend3", lambda: _http.post(anomaly_url, json={
            "backend_id": f"{_SCENARIO_BACKEND3}:8080",
            "status": "unhealthy", "actor": "demo", "reason": "Backend Failure scenario",
        }))
        _step("traffic_medium", lambda: _http.post(
            f"{traffic_base}/swarm", data={"user_count": 50, "spawn_rate": 10},
        ))

    elif scenario == "latency_spike":
        url1 = bmap.get(_SCENARIO_BACKEND1)
        if url1:
            _step("chaos_backend1_800ms", lambda: _http.post(
                f"{url1}/admin/chaos",
                json={"delay_ms": 800, "fail_health": False, "fail_all": False},
                headers=chaos_headers,
            ))
        _step("traffic_medium", lambda: _http.post(
            f"{traffic_base}/swarm", data={"user_count": 50, "spawn_rate": 10},
        ))

    elif scenario == "recovery":
        state = _fan_out_demo_state()
        for backend in state.get("excluded_backends", []):
            _step(f"recover_{backend}", lambda b=backend: _http.post(anomaly_url, json={
                "backend_id": b, "status": "healthy",
                "actor": "demo", "reason": "Recovery scenario",
            }))
        chaos_clear = {"delay_ms": 0, "fail_health": False, "fail_all": False}
        for host, url in bmap.items():
            _step(f"chaos_clear_{host}", lambda u=url: _http.post(
                f"{u}/admin/chaos", json=chaos_clear, headers=chaos_headers,
            ))
        _step("safe_mode_off", lambda: _http.post(
            f"{SERVICE_URLS['policy-manager']}/api/v1/policy",
            json={"safe_mode": False},
            headers={"Content-Type": "application/json", "X-Actor": "demo"},
        ))
        _step("traffic_low", lambda: _http.post(
            f"{traffic_base}/swarm", data={"user_count": 5, "spawn_rate": 1},
        ))

    elif scenario == "high_traffic":
        state = _fan_out_demo_state()
        for backend in state.get("excluded_backends", []):
            _step(f"recover_{backend}", lambda b=backend: _http.post(anomaly_url, json={
                "backend_id": b, "status": "healthy",
                "actor": "demo", "reason": "High Traffic scenario",
            }))
        _step("safe_mode_off", lambda: _http.post(
            f"{SERVICE_URLS['policy-manager']}/api/v1/policy",
            json={"safe_mode": False},
            headers={"Content-Type": "application/json", "X-Actor": "demo"},
        ))
        _step("traffic_high", lambda: _http.post(
            f"{traffic_base}/swarm", data={"user_count": 200, "spawn_rate": 50},
        ))

    elif scenario == "ai_disabled":
        _step("safe_mode_on", lambda: _http.post(
            f"{SERVICE_URLS['policy-manager']}/api/v1/policy",
            json={"safe_mode": True},
            headers={"Content-Type": "application/json", "X-Actor": "demo"},
        ))

    else:
        return jsonify({"error": f"unknown scenario: {scenario}"}), 400

    return jsonify({"ok": True, "scenario": scenario, "steps": steps})


# ── algorithm endpoint ────────────────────────────────────────────────────────

@app.route("/api/ui/demo/algorithm", methods=["POST"])
def ui_demo_algorithm():
    """Switch the active routing mode.

    For NGINX-native baselines (round_robin, least_conn, random):
      - Writes the appropriate upstream directive via lb-sidecar.
      - Enables safe_mode so the RL engine stops overwriting weights.

    For "ppo":
      - Resets NGINX to round_robin (no directive, equal weights).
      - Disables safe_mode so RL recommendations can be applied.
    """
    body      = request.get_json(silent=True) or {}
    algorithm = body.get("algorithm", "round_robin")

    nginx_algo = "round_robin" if algorithm == "ppo" else algorithm
    lbs = SERVICE_URLS["lb-sidecar"]
    try:
        r = _http.post(f"{lbs}/api/v1/lb/algorithm", json={"algorithm": nginx_algo})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    if r.status_code not in (200,):
        return jsonify({"error": r.text}), r.status_code

    # Prevent RL from overwriting weights when a static baseline is active.
    safe_mode = (algorithm != "ppo")
    try:
        _http.post(
            f"{SERVICE_URLS['policy-manager']}/api/v1/policy",
            json={"safe_mode": safe_mode},
            headers={"Content-Type": "application/json", "X-Actor": "demo"},
        )
    except Exception:
        pass  # non-fatal — algorithm was set; policy toggle failed

    return jsonify({"ok": True, "algorithm": algorithm, "safe_mode": safe_mode})


# ── live metrics endpoint ─────────────────────────────────────────────────────

def _query_live_metrics(window: str) -> dict:
    """Query TimescaleDB for aggregate metrics over the given window."""
    conn = _psycopg2.connect(TIMESCALEDB_URL, connect_timeout=5)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY value) AS p95_latency_ms,
                    AVG(value)                                           AS mean_latency_ms,
                    COUNT(*)                                             AS sample_count,
                    COALESCE(
                        SUM(CASE WHEN value > 200 THEN 1 ELSE 0 END)::float
                        / NULLIF(COUNT(*), 0),
                        0
                    )                                                    AS slo_violation_rate
                FROM metrics
                WHERE time > NOW() - %s::interval
                  AND metric_name = 'request_latency_ms'
                  AND service = 'load-balancer'
            """, (window,))
            lat = cur.fetchone()  # (p95, mean, count, slo_rate)

            cur.execute("""
                SELECT COALESCE(SUM(value), 0) AS total_requests
                FROM metrics
                WHERE time > NOW() - %s::interval
                  AND metric_name = 'request_count'
                  AND service = 'load-balancer'
            """, (window,))
            cnt = cur.fetchone()
    finally:
        conn.close()

    p95, mean, samples, slo = lat if lat else (None, None, 0, 0.0)
    total = cnt[0] if cnt else 0
    return {
        "window":             window,
        "p95_latency_ms":     round(float(p95),  2) if p95  is not None else None,
        "mean_latency_ms":    round(float(mean), 2) if mean is not None else None,
        "slo_violation_pct":  round(float(slo) * 100, 1),
        "sample_count":       int(samples or 0),
        "total_requests":     int(total or 0),
    }


@app.route("/api/ui/demo/metrics", methods=["GET"])
def ui_demo_metrics():
    """Live session metrics from TimescaleDB for the last N minutes."""
    if not _psycopg2_available:
        return jsonify({"error": "psycopg2 not installed"}), 503
    if not TIMESCALEDB_URL:
        return jsonify({"error": "TIMESCALEDB_URL not configured"}), 503
    window = request.args.get("window", DEMO_METRICS_WINDOW)
    try:
        data = _query_live_metrics(window)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify(data)


# ── Service health grid ───────────────────────────────────────────────────────

def _probe_service(name: str, role: str) -> dict:
    """GET <service>/health and normalise to a grid row. A 200 (or a 503 with
    a JSON body — services report degraded-but-alive that way) counts as
    reachable; a transport error is `down`."""
    base = SERVICE_URLS.get(name, "")
    row = {"name": name, "role": role, "healthy": False, "status": "down", "detail": None}
    try:
        r = _http.get(f"{base}/health")
    except Exception as exc:
        row["detail"] = str(exc)[:120]
        return row
    body: dict = {}
    try:
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    except Exception:
        body = {}
    row["healthy"] = r.status_code == 200
    row["status"] = body.get("status") or ("ok" if r.status_code == 200 else f"http {r.status_code}")
    # Surface the one most useful field per service, when present.
    for key in ("engine_type", "policy_type", "rl_mode", "algorithm", "mode"):
        if body.get(key) is not None:
            row["detail"] = f"{key}={body[key]}"
            break
    return row


@app.route("/api/ui/demo/services", methods=["GET"])
def ui_demo_services():
    """Health grid across the SmartLoad services the demo console watches."""
    with ThreadPoolExecutor(max_workers=len(SERVICE_GRID)) as pool:
        rows = list(pool.map(lambda nr: _probe_service(*nr), SERVICE_GRID))
    healthy = sum(1 for r in rows if r["healthy"])
    return jsonify({"services": rows, "healthy": healthy, "total": len(rows)})


# ── Live sample (one-shot RPS / p95 / pool size) ─────────────────────────────

def _query_live_sample(window_secs: int) -> dict:
    """Aggregate the load-balancer's last `window_secs` of telemetry into a
    single live sample: requests-per-second, p95/mean latency, SLO-violation
    fraction. Used by the live run monitor, polled ~1 Hz."""
    conn = _psycopg2.connect(TIMESCALEDB_URL, connect_timeout=3)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY value) AS p95,
                    AVG(value)                                           AS mean,
                    COUNT(*)                                             AS samples,
                    COALESCE(SUM(CASE WHEN value > 200 THEN 1 ELSE 0 END)::float
                             / NULLIF(COUNT(*), 0), 0)                   AS slo
                FROM metrics
                WHERE time > NOW() - (%s || ' seconds')::interval
                  AND metric_name = 'request_latency_ms'
                  AND service = 'load-balancer'
            """, (window_secs,))
            p95, mean, samples, slo = cur.fetchone() or (None, None, 0, 0.0)
    finally:
        conn.close()
    rps = round(float(samples or 0) / max(1, window_secs), 1)
    return {
        "rps":             rps,
        "p95_latency_ms":  round(float(p95), 1) if p95 is not None else None,
        "mean_latency_ms": round(float(mean), 1) if mean is not None else None,
        "slo_violation_pct": round(float(slo) * 100, 1),
        "samples":         int(samples or 0),
    }


def _pool_size() -> int:
    """Current backend pool size = number of upstreams the lb-sidecar serves."""
    try:
        r = _http.get(f"{SERVICE_URLS['lb-sidecar']}/api/v1/lb/state")
        if r.status_code == 200:
            return len(r.json().get("upstream_weights", {}) or {})
    except Exception:
        pass
    return 0


@app.route("/api/ui/demo/livestats", methods=["GET"])
def ui_demo_livestats():
    """One live sample for the run monitor — RPS / p95 / pool size. The client
    accumulates these into a rolling time series (no server-side history)."""
    window_secs = int(request.args.get("window_secs", "10"))
    sample: dict = {"pool_size": _pool_size()}
    if _psycopg2_available and TIMESCALEDB_URL:
        try:
            sample.update(_query_live_sample(window_secs))
        except Exception as exc:
            sample["metrics_error"] = str(exc)[:120]
    return jsonify(sample)


# ── One-click load-profile runner (Redis-backed, worker-pool safe) ───────────

def _bench_redis():
    """A short-lived redis client for bench state. Cheap to create; avoids
    sharing a connection across the gunicorn worker pool."""
    return redis_lib.from_url(REDIS_URL)


def _bench_get_state() -> dict:
    try:
        raw = _bench_redis().get(BENCH_STATE_KEY)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return {"status": "idle"}


def _bench_set_state(state: dict) -> None:
    try:
        _bench_redis().set(BENCH_STATE_KEY, json.dumps(state), ex=3600)
    except Exception:
        log.warning("failed to persist bench state", exc_info=True)


def _bench_stop_requested() -> bool:
    try:
        return bool(_bench_redis().get(BENCH_STOP_KEY))
    except Exception:
        return False


def _profile_by_id(profile_id: str) -> dict | None:
    return next((p for p in BENCH_PROFILES if p["id"] == profile_id), None)


def _swarm(users: int, spawn: int) -> None:
    """Re-swarm the traffic-simulator to `users`. Best-effort."""
    base = TRAFFIC_SIMULATOR_URL.rstrip("/")
    try:
        if users <= 0:
            _http.get(f"{base}/stop")
        else:
            _http.post(f"{base}/swarm", data={"user_count": users, "spawn_rate": spawn})
    except Exception:
        log.warning("swarm(%d) failed", users, exc_info=True)


def _inject_anomaly(active: bool) -> str | None:
    """Slow the first backend by BENCH_ANOMALY_DELAY_MS (or clear it) and
    publish the matching isolate event — same path the manual scenarios use.
    Returns the targeted backend host, or None."""
    bmap = _backend_map()
    if not bmap:
        return None
    host = next(iter(bmap))
    url = bmap[host]
    chaos_headers = {"Content-Type": "application/json"}
    if DEMO_TOKEN:
        chaos_headers["X-Demo-Token"] = DEMO_TOKEN
    delay = BENCH_ANOMALY_DELAY_MS if active else 0
    try:
        _http.post(f"{url}/admin/chaos",
                   json={"delay_ms": delay, "fail_health": False, "fail_all": False},
                   headers=chaos_headers)
    except Exception:
        log.warning("anomaly chaos on %s failed", host, exc_info=True)
    try:
        _http.post(f"{SERVICE_URLS['anomaly-detector']}/api/v1/isolate",
                   json={"backend_id": f"{host}:8080",
                         "status": "unhealthy" if active else "healthy",
                         "actor": "demo-bench",
                         "reason": "load-profile phase-D anomaly"})
    except Exception:
        log.warning("anomaly isolate on %s failed", host, exc_info=True)
    return host


def _bench_runner(profile: dict, run_id: str) -> None:
    """Drive one load profile, phase by phase. Runs in a daemon thread on the
    worker that accepted /bench/start; publishes progress to Redis so any
    worker can answer /bench/status."""
    phases = profile["phases"]
    total_secs = sum(p["secs"] for p in phases)
    base_state = {
        "status":     "running",
        "run_id":     run_id,
        "profile_id": profile["id"],
        "profile_label": profile["label"],
        "total_secs": total_secs,
        "phase_names": [p["name"] for p in phases],
        "phase_index": 0,
        "phase": phases[0]["name"],
        "elapsed_secs": 0,
        "anomaly_active": False,
    }
    _bench_set_state(base_state)
    anomaly_host: str | None = None
    elapsed = 0
    stopped = False
    try:
        for idx, phase in enumerate(phases):
            if _bench_stop_requested():
                stopped = True
                break
            _swarm(phase["users"], phase.get("spawn", phase["users"]))
            if phase.get("anomaly"):
                anomaly_host = _inject_anomaly(True)
            # Tick through the phase a second at a time so /status stays fresh
            # and a stop request lands within ~1 s.
            for _ in range(phase["secs"]):
                if _bench_stop_requested():
                    stopped = True
                    break
                time.sleep(1)
                elapsed += 1
                state = dict(base_state)
                state.update({
                    "phase_index": idx, "phase": phase["name"],
                    "elapsed_secs": elapsed,
                    "anomaly_active": bool(phase.get("anomaly")),
                })
                _bench_set_state(state)
            if phase.get("anomaly") and anomaly_host:
                _inject_anomaly(False)   # recover at end of the anomaly phase
                anomaly_host = None
            if stopped:
                break
    finally:
        # Cleanup: clear any lingering anomaly, ease traffic back down.
        if anomaly_host:
            _inject_anomaly(False)
        _swarm(5, 1)
        final = dict(base_state)
        final.update({
            "status": "stopped" if stopped else "done",
            "phase": "stopped" if stopped else "complete",
            "elapsed_secs": elapsed,
            "anomaly_active": False,
        })
        _bench_set_state(final)
        try:
            _bench_redis().delete(BENCH_STOP_KEY)
        except Exception:
            pass


@app.route("/api/ui/demo/bench/profiles", methods=["GET"])
def ui_bench_profiles():
    """List the one-click load profiles (id / label / description / shape)."""
    return jsonify({"profiles": [
        {
            "id": p["id"], "label": p["label"], "description": p["description"],
            "total_secs": sum(ph["secs"] for ph in p["phases"]),
            "phases": [{"name": ph["name"], "secs": ph["secs"], "users": ph["users"],
                        "anomaly": bool(ph.get("anomaly"))} for ph in p["phases"]],
        } for p in BENCH_PROFILES
    ]})


@app.route("/api/ui/demo/bench/status", methods=["GET"])
def ui_bench_status():
    return jsonify(_bench_get_state())


@app.route("/api/ui/demo/bench/start", methods=["POST"])
def ui_bench_start():
    body = request.get_json(silent=True) or {}
    profile = _profile_by_id(body.get("profile_id", ""))
    if profile is None:
        return jsonify({"error": "unknown profile_id"}), 400
    current = _bench_get_state()
    if current.get("status") == "running":
        return jsonify({"error": "a run is already in progress",
                        "run_id": current.get("run_id")}), 409
    try:
        _bench_redis().delete(BENCH_STOP_KEY)
    except Exception:
        pass
    run_id = uuid.uuid4().hex[:12]
    threading.Thread(target=_bench_runner, args=(profile, run_id), daemon=True).start()
    return jsonify({"ok": True, "run_id": run_id, "profile_id": profile["id"]})


@app.route("/api/ui/demo/bench/stop", methods=["POST"])
def ui_bench_stop():
    try:
        _bench_redis().set(BENCH_STOP_KEY, "1", ex=120)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify({"ok": True})


# ── Benchmark surface — read-only over experiments/*/results/ ────────────────
#
# Surfaces BOTH harness suites (see SUITES): the adaptive-bench RQ4 runs and
# the baseline-vs-smartload runs. The harnesses are the canonical way to
# *produce* runs; this BFF only *surfaces* their outputs. Each suite's results
# dir is bind-mounted read-only (ADAPTIVE_RESULTS_DIR / BENCHMARK_RESULTS_DIR).
# Routes are suite-scoped (/benchmark/<suite>/...); the legacy unscoped routes
# remain as aliases onto the baseline suite for back-compat.

def _suite_root(suite: str) -> str | None:
    cfg = SUITES.get(suite)
    return cfg["root"] if cfg else None


def _safe_run_dir(suite: str, timestamp: str) -> str | None:
    """Resolve <suite-root>/<timestamp> safely. Rejects traversal (`..`,
    absolute paths, separators) and returns the absolute path, or None if the
    suite is unknown or the timestamp doesn't name a directory under its root."""
    root_dir = _suite_root(suite)
    if root_dir is None:
        return None
    if not timestamp or "/" in timestamp or "\\" in timestamp or ".." in timestamp:
        return None
    root = os.path.abspath(root_dir)
    candidate = os.path.abspath(os.path.join(root, timestamp))
    if not candidate.startswith(root + os.sep):
        return None
    if not os.path.isdir(candidate):
        return None
    return candidate


def _list_suite_runs(suite: str) -> dict:
    cfg = SUITES[suite]
    root = os.path.abspath(cfg["root"])
    plot_map: dict = cfg["plots"]
    if not os.path.isdir(root):
        return {"suite": suite, "label": cfg["label"], "results_dir": cfg["root"],
                "runs": [], "note": "results dir not mounted"}
    entries: list[dict] = []
    for name in os.listdir(root):
        run_dir = os.path.join(root, name)
        if not os.path.isdir(run_dir) or name.startswith("."):
            continue
        manifest: dict = {}
        manifest_path = os.path.join(run_dir, "MANIFEST.json")
        if os.path.isfile(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as fh:
                    manifest = json.load(fh)
            except Exception:
                manifest = {"parse_error": True}
        plots_present = sorted(
            key for key, (fname, _label) in plot_map.items()
            if os.path.isfile(os.path.join(run_dir, fname))
        )
        sides = sorted(
            d for d in os.listdir(run_dir)
            if os.path.isdir(os.path.join(run_dir, d))
        )
        entries.append({
            "timestamp":     name,
            "manifest":      manifest,
            "plots":         plots_present,
            "has_summary":   os.path.isfile(os.path.join(run_dir, "SUMMARY.md")),
            "sides_present": sides,
        })
    entries.sort(key=lambda e: e["timestamp"], reverse=True)
    return {"suite": suite, "label": cfg["label"], "results_dir": cfg["root"], "runs": entries}


@app.route("/api/ui/demo/benchmark/suites", methods=["GET"])
def ui_benchmark_suites():
    """List the available result suites + a plot-key → label map for each."""
    return jsonify({"suites": [
        {
            "id": sid,
            "label": cfg["label"],
            "harness": cfg["harness"],
            "plots": [{"key": k, "label": lbl} for k, (_f, lbl) in cfg["plots"].items()],
        } for sid, cfg in SUITES.items()
    ]})


@app.route("/api/ui/demo/benchmark/<suite>/runs", methods=["GET"])
def ui_benchmark_runs_suite(suite: str):
    """List a suite's runs, newest first, with manifest + present artefacts."""
    if suite not in SUITES:
        return jsonify({"error": "unknown suite"}), 404
    return jsonify(_list_suite_runs(suite))


@app.route("/api/ui/demo/benchmark/<suite>/runs/<timestamp>/summary", methods=["GET"])
def ui_benchmark_summary_suite(suite: str, timestamp: str):
    """Return a run's SUMMARY.md as text. 404 if the run or file is missing."""
    run_dir = _safe_run_dir(suite, timestamp)
    if run_dir is None:
        return jsonify({"error": "unknown run"}), 404
    path = os.path.join(run_dir, "SUMMARY.md")
    if not os.path.isfile(path):
        return jsonify({"error": "no SUMMARY.md for this run yet"}), 404
    try:
        # `errors="replace"` keeps the endpoint robust against SUMMARY.md files
        # written by older plot_results.py runs on a cp1252 Windows host.
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception as exc:
        return jsonify({"error": f"read failed: {exc}"}), 500
    return Response(text, mimetype="text/markdown; charset=utf-8")


@app.route("/api/ui/demo/benchmark/<suite>/runs/<timestamp>/plot/<name>", methods=["GET"])
def ui_benchmark_plot_suite(suite: str, timestamp: str, name: str):
    """Serve one PNG plot for a run. `name` is the suite's short plot key, not
    the filename — keeps the URL surface stable across file-naming changes."""
    if suite not in SUITES:
        return jsonify({"error": "unknown suite"}), 404
    run_dir = _safe_run_dir(suite, timestamp)
    if run_dir is None:
        return jsonify({"error": "unknown run"}), 404
    entry = SUITES[suite]["plots"].get(name)
    if entry is None:
        return jsonify({"error": "unknown plot key"}), 404
    filename = entry[0]
    if not os.path.isfile(os.path.join(run_dir, filename)):
        return jsonify({"error": "plot not generated for this run"}), 404
    return send_from_directory(run_dir, filename, mimetype="image/png")


@app.route("/api/ui/demo/benchmark/<suite>/runs/<timestamp>/manifest", methods=["GET"])
def ui_benchmark_manifest_suite(suite: str, timestamp: str):
    """Return a run's MANIFEST.json as JSON (convenient for direct deep-links)."""
    run_dir = _safe_run_dir(suite, timestamp)
    if run_dir is None:
        return jsonify({"error": "unknown run"}), 404
    path = os.path.join(run_dir, "MANIFEST.json")
    if not os.path.isfile(path):
        return jsonify({"error": "no manifest"}), 404
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return jsonify(json.load(fh))
    except Exception as exc:
        return jsonify({"error": f"manifest parse failed: {exc}"}), 500


# ── Headline KPI extraction ───────────────────────────────────────────────────
# The adaptive SUMMARY.md is generated by our own plot_results.py with a stable
# shape (Run anchor line + a Per-phase table + a Time-to-react table + an
# Autoscaler-action-counts block + a Phase-D anomaly table). We parse those
# specific, labelled rows into headline cards — no fragile free-prose scraping.

_PHASE_ROW_RE = re.compile(
    r"^\|\s*`(?P<phase>\w+)`\s*\|[^|]*\|\s*(?P<users>\d+)\s*users\s*\|\s*"
    r"(?P<rps>[\d.]+)\s*\|\s*(?P<p95>\d+)\s*\|\s*(?P<lo>\d+)\.\.(?P<hi>\d+)\s*\|",
    re.MULTILINE,
)


def _parse_adaptive_kpis(text: str) -> list[dict]:
    """Derive RQ4 headline cards from an adaptive SUMMARY.md."""
    cards: list[dict] = []

    phases = list(_PHASE_ROW_RE.finditer(text))
    pool_los = [int(m["lo"]) for m in phases]
    pool_his = [int(m["hi"]) for m in phases]
    p95s     = [int(m["p95"]) for m in phases]
    users    = [int(m["users"]) for m in phases]
    rpss     = [float(m["rps"]) for m in phases]

    if pool_los and pool_his:
        lo, hi = min(pool_los), max(pool_his)
        cards.append({
            "label": "Pool size", "value": f"{lo} → {hi}",
            "hint": "min → max active backends", "tone": "ok" if hi > lo else "muted",
        })

    so = re.search(r"\*\*scale_out\*\*:\s*(\d+)", text)
    si = re.search(r"\*\*scale_in\*\*:\s*(\d+)", text)
    tot = re.search(r"\*\*total decisions in audit\*\*:\s*(\d+)", text)
    if tot:
        hint = []
        if so: hint.append(f"{so.group(1)} scale-out")
        if si: hint.append(f"{si.group(1)} scale-in")
        cards.append({
            "label": "Scaling actions", "value": tot.group(1),
            "hint": " · ".join(hint) or "forecast-driven", "tone": "ok",
        })

    delays = [float(x) for x in re.findall(r"\|\s*([\d.]+)s\s*\|\s*$", text, re.MULTILINE)]
    if delays:
        cards.append({
            "label": "Fastest reaction", "value": f"{min(delays):.1f}s",
            "hint": "forecast publish → autoscaler action", "tone": "ok",
        })

    if p95s:
        peak = max(p95s)
        cards.append({
            "label": "Peak p95", "value": f"{peak} ms",
            "hint": "highest per-phase p95", "tone": "warn" if peak > 200 else "ok",
        })

    if users and rpss:
        cards.append({
            "label": "Peak load", "value": f"{max(users)} users",
            "hint": f"{max(rpss):.0f} rps observed", "tone": "muted",
        })

    # Phase-D anomaly row: `target` (dynamic=..) | injected | recovered | window | N backends
    anom = re.search(
        r"\|\s*`(?P<target>[\w-]+)`[^|]*\|[^|]*\|\s*(?P<rec>[\d:—-]+)\s*\|\s*(?P<window>[\dsm—-]+)\s*\|",
        text,
    )
    if anom and anom["target"].startswith("smartload"):
        recovered = anom["rec"].strip() not in ("—", "-", "")
        cards.append({
            "label": "Anomaly", "value": "recovered" if recovered else "injected",
            "hint": f"{anom['target']} · {anom['window'].strip()}", "tone": "ok" if recovered else "warn",
        })

    dur = re.search(r"\((\d+)\s*s\)", text)
    if dur:
        cards.append({
            "label": "Run length", "value": f"{dur.group(1)} s",
            "hint": "wall-clock", "tone": "muted",
        })

    return cards


@app.route("/api/ui/demo/benchmark/<suite>/runs/<timestamp>/kpis", methods=["GET"])
def ui_benchmark_kpis(suite: str, timestamp: str):
    """Headline KPI cards parsed from a run's SUMMARY.md. Adaptive runs get the
    full RQ4 card set; other suites return an empty list (the page hides the
    KPI strip when empty) until a parser is written for them."""
    run_dir = _safe_run_dir(suite, timestamp)
    if run_dir is None:
        return jsonify({"error": "unknown run"}), 404
    path = os.path.join(run_dir, "SUMMARY.md")
    if not os.path.isfile(path):
        return jsonify({"kpis": [], "note": "no SUMMARY.md yet"})
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception as exc:
        return jsonify({"error": f"read failed: {exc}"}), 500
    kpis = _parse_adaptive_kpis(text) if suite == "adaptive" else []
    return jsonify({"kpis": kpis})


# ── Back-compat aliases: legacy unscoped routes → baseline suite ─────────────

@app.route("/api/ui/demo/benchmark/runs", methods=["GET"])
def ui_benchmark_runs():
    return jsonify(_list_suite_runs("baseline"))


@app.route("/api/ui/demo/benchmark/runs/<timestamp>/summary", methods=["GET"])
def ui_benchmark_summary(timestamp: str):
    return ui_benchmark_summary_suite("baseline", timestamp)


@app.route("/api/ui/demo/benchmark/runs/<timestamp>/plot/<name>", methods=["GET"])
def ui_benchmark_plot(timestamp: str, name: str):
    return ui_benchmark_plot_suite("baseline", timestamp, name)


@app.route("/api/ui/demo/benchmark/runs/<timestamp>/manifest", methods=["GET"])
def ui_benchmark_manifest(timestamp: str):
    return ui_benchmark_manifest_suite("baseline", timestamp)


# ── SSE event stream ──────────────────────────────────────────────────────────

@app.route("/api/ui/events")
def ui_event_stream():
    """SSE stream: forwards routing / anomaly / policy / scale from Redis.

    Polls with `get_message(timeout=...)` rather than the blocking `listen()`
    so an idle stretch on a quiet control bus surfaces as a periodic SSE
    comment heartbeat instead of a socket-read timeout bubbling up as a 500.
    A redis socket TimeoutError is likewise treated as "no message yet"."""
    def generate():
        r = redis_lib.from_url(REDIS_URL, socket_timeout=20)
        ps = r.pubsub(ignore_subscribe_messages=True)
        ps.subscribe("smartload.routing", "smartload.anomaly",
                     "smartload.policy", "smartload.scale")
        try:
            yield ": connected\n\n"
            while True:
                try:
                    msg = ps.get_message(ignore_subscribe_messages=True, timeout=15)
                except redis_lib.exceptions.RedisError:
                    yield ": heartbeat\n\n"   # transient read timeout — keep the stream alive
                    continue
                if msg is None:
                    yield ": heartbeat\n\n"
                    continue
                channel = msg["channel"].decode() if isinstance(msg["channel"], bytes) else msg["channel"]
                data    = msg["data"].decode()    if isinstance(msg["data"],    bytes) else msg["data"]
                try:
                    envelope = json.loads(data)
                except (TypeError, ValueError):
                    continue
                yield f"data: {json.dumps({'channel': channel, 'envelope': envelope})}\n\n"
        finally:
            try:
                ps.close()
            except Exception:
                pass
            try:
                r.close()
            except Exception:
                pass

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── own health ────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": SERVICE_NAME})


# ── SPA fallback ──────────────────────────────────────────────────────────────

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
        "message": "Demo UI BFF up; web/ build not found",
        "web_dist": WEB_DIST,
    })


if __name__ == "__main__":
    log.info("starting on port %d (web_dist=%s)", PORT, WEB_DIST)
    app.run(host="0.0.0.0", port=PORT)
