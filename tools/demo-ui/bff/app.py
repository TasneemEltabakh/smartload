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
  POST /api/ui/demo/degrade       mark a backend degraded/unhealthy
  POST /api/ui/demo/recover       restore a backend to healthy
  POST /api/ui/demo/mode          toggle safe_mode on the policy
  POST /api/ui/demo/traffic       start/stop Locust traffic load
  POST /api/ui/demo/chaos         inject latency/failure into a backend
  POST /api/ui/demo/reset         full orchestrated reset to baseline
  POST /api/ui/demo/scenario      run a named multi-step scenario
  POST /api/ui/demo/algorithm     pick the LB routing algorithm
  GET  /api/ui/demo/metrics       last-5m latency snapshot from TimescaleDB
  GET  /api/ui/demo/benchmark/runs                       list baseline-vs-smartload runs
  GET  /api/ui/demo/benchmark/runs/<ts>/manifest         MANIFEST.json for one run
  GET  /api/ui/demo/benchmark/runs/<ts>/summary          SUMMARY.md for one run
  GET  /api/ui/demo/benchmark/runs/<ts>/plot/<name>      one of the six PNG plots
  GET  /api/ui/events             SSE stream of smartload.routing/anomaly/policy
  GET  /health                    own health check
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
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

# Service URLs — only the ones the demo BFF actually calls.
SERVICE_URLS: dict[str, str] = {
    "policy-manager":   os.environ.get("POLICY_MANAGER_URL",   "http://policy-manager:8086"),
    "anomaly-detector": os.environ.get("ANOMALY_DETECTOR_URL", "http://anomaly-detector:8082"),
    "rl-engine":        os.environ.get("RL_ENGINE_URL",        "http://rl-engine:8084"),
    "lb-sidecar":       os.environ.get("LB_SIDECAR_URL",       "http://lb-sidecar:8087"),
}

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


# ── Benchmark surface — read-only over experiments/baseline-vs-smartload/ ────
#
# Drives the demo-ui Benchmark page (#148 / v1.0.7r harness consumer). The
# bash script `experiments/baseline-vs-smartload/scripts/run_experiment.sh`
# is the canonical way to *produce* runs; this BFF only *surfaces* their
# outputs. The results directory is bind-mounted into the container at
# BENCHMARK_RESULTS_DIR (default /benchmark-results) read-only.

_BENCHMARK_PLOT_NAMES = {
    "rps":            "plot_rps.png",
    "p50_p95_p99":    "plot_p50_p95_p99.png",
    "error_rate":     "plot_error_rate.png",
    "total_requests": "plot_total_requests.png",
    "per_phase_p95":  "plot_per_phase_p95.png",
    "recovery_curve": "plot_recovery_curve.png",
}


def _safe_run_dir(timestamp: str) -> str | None:
    """Resolve a results/<timestamp> path safely. Rejects any traversal
    attempt (`..`, absolute paths, embedded separators) and returns the
    absolute path on success or None if the timestamp doesn't name a
    directory under BENCHMARK_RESULTS_DIR."""
    if not timestamp or "/" in timestamp or "\\" in timestamp or ".." in timestamp:
        return None
    root = os.path.abspath(BENCHMARK_RESULTS_DIR)
    candidate = os.path.abspath(os.path.join(root, timestamp))
    if not candidate.startswith(root + os.sep):
        return None
    if not os.path.isdir(candidate):
        return None
    return candidate


@app.route("/api/ui/demo/benchmark/runs", methods=["GET"])
def ui_benchmark_runs():
    """List historical benchmark runs, newest first. Each entry includes
    the timestamp, the manifest knobs, and which artefacts are present."""
    root = os.path.abspath(BENCHMARK_RESULTS_DIR)
    if not os.path.isdir(root):
        return jsonify({
            "results_dir": BENCHMARK_RESULTS_DIR,
            "runs": [],
            "note": "benchmark results dir not mounted",
        })
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
            k for k, fname in _BENCHMARK_PLOT_NAMES.items()
            if os.path.isfile(os.path.join(run_dir, fname))
        )
        has_summary = os.path.isfile(os.path.join(run_dir, "SUMMARY.md"))
        sides = sorted(
            d for d in os.listdir(run_dir)
            if os.path.isdir(os.path.join(run_dir, d))
        )
        entries.append({
            "timestamp":     name,
            "manifest":      manifest,
            "plots":         plots_present,
            "has_summary":   has_summary,
            "sides_present": sides,
        })
    entries.sort(key=lambda e: e["timestamp"], reverse=True)
    return jsonify({"results_dir": BENCHMARK_RESULTS_DIR, "runs": entries})


@app.route("/api/ui/demo/benchmark/runs/<timestamp>/summary", methods=["GET"])
def ui_benchmark_summary(timestamp: str):
    """Return SUMMARY.md as plain text. 404 if the run or file is missing."""
    run_dir = _safe_run_dir(timestamp)
    if run_dir is None:
        return jsonify({"error": "unknown run"}), 404
    path = os.path.join(run_dir, "SUMMARY.md")
    if not os.path.isfile(path):
        return jsonify({"error": "no SUMMARY.md for this run yet"}), 404
    try:
        # `errors="replace"` keeps the endpoint robust against files written
        # by older plot_results.py runs on Windows hosts (cp1252 default
        # encoding before the UTF-8 fix landed). New runs are pure UTF-8.
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception as exc:
        return jsonify({"error": f"read failed: {exc}"}), 500
    return Response(text, mimetype="text/markdown; charset=utf-8")


@app.route("/api/ui/demo/benchmark/runs/<timestamp>/plot/<name>", methods=["GET"])
def ui_benchmark_plot(timestamp: str, name: str):
    """Serve one of the six PNG plots. `name` is the short key
    (rps / p50_p95_p99 / error_rate / total_requests / per_phase_p95 /
    recovery_curve), not the filename — keeps the URL surface stable
    even if file naming changes."""
    run_dir = _safe_run_dir(timestamp)
    if run_dir is None:
        return jsonify({"error": "unknown run"}), 404
    filename = _BENCHMARK_PLOT_NAMES.get(name)
    if filename is None:
        return jsonify({"error": "unknown plot key"}), 404
    path = os.path.join(run_dir, filename)
    if not os.path.isfile(path):
        return jsonify({"error": "plot not generated for this run"}), 404
    return send_from_directory(run_dir, filename, mimetype="image/png")


@app.route("/api/ui/demo/benchmark/runs/<timestamp>/manifest", methods=["GET"])
def ui_benchmark_manifest(timestamp: str):
    """Return MANIFEST.json for a single run as JSON (the listing endpoint
    already includes this; the per-run endpoint is convenient for direct
    deep-links)."""
    run_dir = _safe_run_dir(timestamp)
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


# ── SSE event stream ──────────────────────────────────────────────────────────

@app.route("/api/ui/events")
def ui_event_stream():
    """SSE stream: forwards smartload.routing / .anomaly / .policy from Redis."""
    def generate():
        r = redis_lib.from_url(REDIS_URL)
        ps = r.pubsub(ignore_subscribe_messages=True)
        ps.subscribe("smartload.routing", "smartload.anomaly", "smartload.policy")
        try:
            for msg in ps.listen():
                channel = msg["channel"].decode() if isinstance(msg["channel"], bytes) else msg["channel"]
                data    = msg["data"].decode()    if isinstance(msg["data"],    bytes) else msg["data"]
                yield f"data: {json.dumps({'channel': channel, 'envelope': json.loads(data)})}\n\n"
        finally:
            try:
                ps.unsubscribe()
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
