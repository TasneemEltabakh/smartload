"""
services/lb-sidecar/app.py
───────────────────────────
LB-sidecar entry point.

Phase-0 mode (default):  /health only, no config rewriting.
Phase-1 mode:            enabled by LB_SIDECAR_RUNLOOP_ENABLED=true.
                         Subscribes to smartload.routing, smartload.anomaly,
                         and smartload.policy via Redis pub/sub, then rewrites
                         /nginx-conf/upstream.conf and signals nginx -s reload
                         via Docker exec on each qualifying message.

Safety:
  - Run loop is opt-in via LB_SIDECAR_RUNLOOP_ENABLED=false default so the
    Phase-0 stub stays the default until smoke-tested.
  - Shadow-mode routing recommendations are logged but never applied.
  - A failed docker exec raises; the run loop logs and continues.

Health endpoint (always):
  {"status": "ok|degraded", "service": "lb-sidecar", "redis": bool}

Additional fields when run loop is enabled:
  sidecar_ready, last_routing_age_seconds, excluded_backends
"""

from __future__ import annotations

import os
import sys
import threading
import time

import redis as redis_lib
from flask import Flask, jsonify, request

_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (_HERE, os.path.dirname(_HERE)):
    if os.path.isdir(os.path.join(_cand, "shared")):
        sys.path.insert(0, _cand)
        break
from shared.contracts import parse_envelope  # noqa: E402

from runloop import (  # noqa: E402
    BackendRegistry,
    handle_anomaly,
    handle_policy,
    handle_routing,
)

app = Flask(__name__)

SERVICE_NAME          = os.environ.get("SERVICE_NAME", "lb-sidecar")
PORT                  = int(os.environ.get("PORT", "8087"))
REDIS_URL             = os.environ.get("REDIS_URL", "redis://redis:6379")
NGINX_CONTAINER       = os.environ.get("NGINX_CONTAINER", "smartload-load-balancer-1")
NGINX_CONF_PATH       = os.environ.get("NGINX_CONF_PATH", "/nginx-conf/upstream.conf")
LB_ADAPTER            = os.environ.get("LB_ADAPTER", "nginx")
POLL_INTERVAL_SECONDS = float(os.environ.get("POLL_INTERVAL_SECONDS", "5"))
ALL_BACKENDS_RAW      = os.environ.get(
    "ALL_BACKENDS",
    "smartload-test-backend-1:8080,smartload-test-backend-2:8080,"
    "smartload-test-backend-3:8080,smartload-test-backend-4:8080,"
    "smartload-test-backend-5:8080",
)
ALL_BACKENDS = [b.strip() for b in ALL_BACKENDS_RAW.split(",") if b.strip()]

RUNLOOP_ENABLED = os.environ.get("LB_SIDECAR_RUNLOOP_ENABLED", "false").lower() == "true"

ROUTING_CHANNEL = "smartload.routing"
ANOMALY_CHANNEL = "smartload.anomaly"
POLICY_CHANNEL  = "smartload.policy"


# ── shared state ──────────────────────────────────────────────────────────────

_state_lock = threading.Lock()
_sidecar_ready: bool = False
_last_routing_monotonic: float | None = None
_excluded_backends: set[str] = set()

_adapter = None
_registry: BackendRegistry | None = None


def _build_adapter():
    """Instantiate the configured LoadBalancerAdapter."""
    if LB_ADAPTER != "nginx":
        raise ValueError(f"Unknown LB_ADAPTER: {LB_ADAPTER!r}")
    import docker
    from shared.lb_adapters.nginx import NginxAdapter

    docker_client = docker.from_env()
    adapter = NginxAdapter(
        conf_path=NGINX_CONF_PATH,
        nginx_container=NGINX_CONTAINER,
        docker_client=docker_client,
        all_backends=ALL_BACKENDS,
    )
    return adapter, docker_client


# ── connectivity ──────────────────────────────────────────────────────────────

def check_redis() -> tuple[bool, str | None]:
    try:
        redis_lib.from_url(REDIS_URL, socket_connect_timeout=3).ping()
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# ── run loop ──────────────────────────────────────────────────────────────────

def _run_loop(stop_event: threading.Event | None = None) -> None:
    global _sidecar_ready, _last_routing_monotonic, _excluded_backends
    global _adapter, _registry

    print(f"[{SERVICE_NAME}] run loop starting "
          f"(nginx_container={NGINX_CONTAINER} conf={NGINX_CONF_PATH} "
          f"interval={POLL_INTERVAL_SECONDS}s)", flush=True)

    try:
        adapter, docker_client = _build_adapter()
        registry = BackendRegistry(docker_client, seed_backends=ALL_BACKENDS)
    except Exception as exc:  # noqa: BLE001
        print(f"[{SERVICE_NAME}] adapter/registry init failed: {exc}", flush=True)
        return

    with _state_lock:
        _adapter = adapter
        _registry = registry
        _sidecar_ready = True

    redis_client = redis_lib.from_url(REDIS_URL)
    pubsub = redis_client.pubsub()
    pubsub.subscribe(ROUTING_CHANNEL, ANOMALY_CHANNEL, POLICY_CHANNEL)

    print(f"[{SERVICE_NAME}] sidecar ready — subscribed to "
          f"{ROUTING_CHANNEL}, {ANOMALY_CHANNEL}, {POLICY_CHANNEL}", flush=True)

    while True:
        if stop_event is not None and stop_event.is_set():
            break

        message = pubsub.get_message(ignore_subscribe_messages=True,
                                     timeout=POLL_INTERVAL_SECONDS)
        if message is None or message.get("type") != "message":
            continue

        channel = message.get("channel", b"")
        if isinstance(channel, bytes):
            channel = channel.decode()

        raw = message.get("data")
        parsed = parse_envelope(raw, channel=channel)
        if parsed is None:
            continue
        payload, _meta = parsed

        if channel == ROUTING_CHANNEL:
            outcome = handle_routing(payload, registry, adapter, ALL_BACKENDS)
            if outcome.applied:
                with _state_lock:
                    _last_routing_monotonic = time.monotonic()
                    _excluded_backends = set(adapter.current_state().excluded_backends)
                print(f"[{SERVICE_NAME}] routing applied "
                      f"({outcome.weight_count} backends)", flush=True)
            elif outcome.mode == "shadow":
                pass  # shadow mode: log nothing extra, just no-op
            elif outcome.error:
                print(f"[{SERVICE_NAME}] routing error: {outcome.error}", flush=True)

        elif channel == ANOMALY_CHANNEL:
            outcome = handle_anomaly(payload, registry, adapter)
            if outcome.applied:
                with _state_lock:
                    _excluded_backends = set(adapter.current_state().excluded_backends)
                print(f"[{SERVICE_NAME}] anomaly: {outcome.action} "
                      f"{outcome.backend_id}", flush=True)
            elif outcome.error:
                print(f"[{SERVICE_NAME}] anomaly error: {outcome.error}", flush=True)

        elif channel == POLICY_CHANNEL:
            outcome = handle_policy(payload, adapter, ALL_BACKENDS)
            if outcome.applied:
                with _state_lock:
                    _last_routing_monotonic = time.monotonic()
                    _excluded_backends = set(adapter.current_state().excluded_backends)
                print(f"[{SERVICE_NAME}] safe_mode active — "
                      f"reverted to equal weights", flush=True)
            elif outcome.error:
                print(f"[{SERVICE_NAME}] policy error: {outcome.error}", flush=True)


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    redis_ok, redis_err = check_redis()
    status = "ok" if redis_ok else "degraded"
    code = 200 if redis_ok else 503

    body: dict = {
        "status": status,
        "service": SERVICE_NAME,
        "redis": redis_ok,
    }
    if RUNLOOP_ENABLED:
        with _state_lock:
            ready = _sidecar_ready
            last = _last_routing_monotonic
            excluded = sorted(_excluded_backends)
        body["sidecar_ready"] = ready
        body["last_routing_age_seconds"] = (
            None if last is None else round(time.monotonic() - last, 2)
        )
        body["excluded_backends"] = excluded
    if redis_err:
        body["errors"] = [redis_err]
    return jsonify(body), code


@app.route("/api/v1/lb/state")
def lb_state():
    """Return the adapter's current view of upstream weights and exclusions."""
    with _state_lock:
        adapter = _adapter
    if adapter is None:
        return jsonify({"error": "run loop not enabled or not ready"}), 503
    state = adapter.current_state()
    return jsonify({
        "upstream_weights": state.upstream_weights,
        "excluded_backends": sorted(state.excluded_backends),
        "algorithm": state.algorithm,
    })


@app.route("/api/v1/lb/weights", methods=["POST"])
def lb_set_weights():
    """Operator-supplied weight override (POST JSON: {backend_id: weight, ...})."""
    with _state_lock:
        adapter = _adapter
    if adapter is None:
        return jsonify({"error": "run loop not enabled or not ready"}), 503
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "empty body"}), 400
    try:
        weights = {k: int(v) for k, v in data.items()}
        adapter.set_upstream_weights(weights)
        with _state_lock:
            _excluded_backends.clear()
            _excluded_backends.update(adapter.current_state().excluded_backends)
        return jsonify({"ok": True, "applied_weights": weights})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/v1/lb/algorithm", methods=["POST"])
def lb_set_algorithm():
    """Switch the NGINX upstream algorithm (round_robin | least_conn | random).

    Resets all server weights to equal before writing the new directive so
    the algorithm operates without any prior RL-applied bias.
    """
    with _state_lock:
        adapter = _adapter
    if adapter is None:
        return jsonify({"error": "run loop not enabled or not ready"}), 503
    data = request.get_json(silent=True) or {}
    algo = data.get("algorithm", "round_robin")
    try:
        adapter.set_algorithm(algo)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True, "algorithm": algo})


@app.route("/")
def index():
    return jsonify({"service": SERVICE_NAME, "status": "running"})


# ── startup ───────────────────────────────────────────────────────────────────

def _start_runloop_thread() -> None:
    t = threading.Thread(target=_run_loop, daemon=True, name="lb-sidecar-runloop")
    t.start()


if __name__ == "__main__":
    if RUNLOOP_ENABLED:
        _start_runloop_thread()
    else:
        print(f"[{SERVICE_NAME}] run loop disabled "
              f"(set LB_SIDECAR_RUNLOOP_ENABLED=true to enable)", flush=True)

    print(f"[{SERVICE_NAME}] starting on port {PORT}", flush=True)
    app.run(host="0.0.0.0", port=PORT)
