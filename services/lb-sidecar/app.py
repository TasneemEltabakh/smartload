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

SOT-anchored startup sequence (Phase-1):
  1. Build the LB adapter + Docker SDK BackendRegistry.
  2. Hydrate excluded_backends from TimescaleDB's backend_health table
     (SOT §30 lines 7635 + 7760 — "LB sidecar consumes BACKEND_HEALTH_QUERY").
     This makes exclusion state durable across Redis disconnects + restarts.
     Failure is non-fatal — the run loop proceeds with an empty exclusion
     set and recovers as anomaly-detector republishes.
  3. Subscribe to the three Redis channels and enter the drain loop.

Safety:
  - Run loop is opt-in via LB_SIDECAR_RUNLOOP_ENABLED=false default so the
    Phase-0 stub stays the default until smoke-tested.
  - Shadow-mode routing recommendations are logged but never applied
    (SOT v1.0.6 row line 5703).
  - Redis disconnects are caught and the loop attempts reconnect on the
    next iteration (SOT §8.1 line 2299 — sidecar failure must not affect
    request serving).
  - SIGTERM/SIGINT drain the current message and close subscriptions
    cleanly. Daemon thread + container SIGTERM works either way; the
    explicit wiring prevents orphan pubsub subscriptions on Redis.

Health endpoint (always):
  {"status": "ok|degraded", "service": "lb-sidecar", "redis": bool}

Additional fields when run loop is enabled:
  sidecar_ready, last_routing_age_seconds, excluded_backends,
  policy_safe_mode, rl_confidence_threshold
"""

from __future__ import annotations

import os
import signal
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
from shared.contracts import parse_envelope        # noqa: E402
from shared.queries import BACKEND_HEALTH_QUERY    # noqa: E402

from runloop import (  # noqa: E402
    BackendRegistry,
    PolicyState,
    discover_all_backends,
    handle_anomaly,
    handle_policy,
    handle_routing,
    invalidate_backend_discovery_cache,
)

app = Flask(__name__)

SERVICE_NAME          = os.environ.get("SERVICE_NAME", "lb-sidecar")
PORT                  = int(os.environ.get("PORT", "8087"))
REDIS_URL             = os.environ.get("REDIS_URL", "redis://redis:6379")
TIMESCALEDB_URL       = os.environ.get(
    "TIMESCALEDB_URL",
    "postgresql://postgres:changeme@timescaledb:5432/smartloaddb",
)
NGINX_CONTAINER       = os.environ.get("NGINX_CONTAINER", "smartload-load-balancer-1")
NGINX_CONF_PATH       = os.environ.get("NGINX_CONF_PATH", "/nginx-conf/upstream.conf")
LB_ADAPTER            = os.environ.get("LB_ADAPTER", "nginx")
POLL_INTERVAL_SECONDS = float(os.environ.get("POLL_INTERVAL_SECONDS", "5"))
# Window passed to BACKEND_HEALTH_QUERY on startup hydration. The query
# returns the latest row per backend within the window, so it should be
# wider than the anomaly-detector publish interval but tight enough that
# truly stale (hours-old) verdicts don't get applied to live state.
HEALTH_HYDRATION_WINDOW_SECONDS = int(
    os.environ.get("LB_SIDECAR_HEALTH_HYDRATION_WINDOW_SECONDS", "300")
)
# ALL_BACKENDS is now a cold-boot SEED used until the Docker daemon is
# reachable + `discover_all_backends()` returns a non-empty live list.
# Per #155: production runtime queries the test-backend label dynamically
# on every message so a freshly-provisioned backend appears in
# upstream.conf within one cache window (2 s by default). See
# runloop.discover_all_backends() for the cache + fallback semantics.
ALL_BACKENDS_RAW      = os.environ.get(
    "ALL_BACKENDS",
    "smartload-test-backend-1:8080,smartload-test-backend-2:8080,"
    "smartload-test-backend-3:8080,smartload-test-backend-4:8080,"
    "smartload-test-backend-5:8080",
)
ALL_BACKENDS_SEED = [b.strip() for b in ALL_BACKENDS_RAW.split(",") if b.strip()]
# Kept as `ALL_BACKENDS` too so callers / tests that import the name still
# resolve to the cold-boot seed list. The live discovery is
# discover_all_backends(_docker_client, seed_backends=ALL_BACKENDS).
ALL_BACKENDS = ALL_BACKENDS_SEED

RUNLOOP_ENABLED = os.environ.get("LB_SIDECAR_RUNLOOP_ENABLED", "false").lower() == "true"

ROUTING_CHANNEL = "smartload.routing"
ANOMALY_CHANNEL = "smartload.anomaly"
POLICY_CHANNEL  = "smartload.policy"

# How long to back off after a Redis disconnect before reconnecting.
# Short enough to recover quickly; long enough to avoid hot-looping.
_REDIS_RECONNECT_BACKOFF_SECONDS = 2.0


# ── shared state ──────────────────────────────────────────────────────────────

_state_lock = threading.Lock()
_sidecar_ready: bool = False
_last_routing_monotonic: float | None = None
_excluded_backends: set[str] = set()
_policy_state: PolicyState = PolicyState()

_adapter = None
_registry: BackendRegistry | None = None

# Shutdown signal — set by SIGTERM/SIGINT handler, observed by the run
# loop between message reads so the current cycle drains cleanly before
# pubsub + DB close.
_shutdown_event = threading.Event()


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


def _hydrate_excluded_from_db(adapter, registry: BackendRegistry) -> int:
    """Read latest backend_health rows and exclude any unhealthy entries.

    SOT §30 lines 7635 + 7760: the LB sidecar consumes BACKEND_HEALTH_QUERY.
    This runs once at startup so the sidecar inherits the durable view of
    unhealthy backends rather than waiting for anomaly-detector to
    republish on Redis. Returns the count of backends excluded.

    Failure is non-fatal: a missing TimescaleDB, an empty result set, or
    a connection error all degrade gracefully to "no hydration" and the
    loop continues. The anomaly channel will catch up the state.
    """
    try:
        import psycopg2
    except ImportError as exc:
        print(f"[{SERVICE_NAME}] psycopg2 not installed ({exc}); "
              "skipping backend_health hydration", flush=True)
        return 0

    excluded = 0
    try:
        conn = psycopg2.connect(TIMESCALEDB_URL, connect_timeout=5)
        conn.autocommit = True
    except Exception as exc:  # noqa: BLE001
        print(f"[{SERVICE_NAME}] backend_health hydration: DB unreachable "
              f"({exc}); proceeding with empty exclusion set", flush=True)
        return 0

    try:
        with conn.cursor() as cur:
            cur.execute(BACKEND_HEALTH_QUERY, (f"{HEALTH_HYDRATION_WINDOW_SECONDS} seconds",))
            rows = cur.fetchall()
        for row in rows:
            try:
                backend_id, status, _score, _ts = row
            except ValueError:
                continue
            if status != "unhealthy":
                continue
            translated = registry.translate_one(backend_id)
            adapter.exclude_backend(translated)
            excluded += 1
    except Exception as exc:  # noqa: BLE001
        print(f"[{SERVICE_NAME}] backend_health hydration query failed "
              f"({exc}); proceeding with whatever was applied so far", flush=True)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    return excluded


# ── connectivity ──────────────────────────────────────────────────────────────

def check_redis() -> tuple[bool, str | None]:
    try:
        redis_lib.from_url(REDIS_URL, socket_connect_timeout=3).ping()
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# ── run loop ──────────────────────────────────────────────────────────────────

def _open_redis_pubsub():
    """Build a fresh Redis pubsub + subscribe to the three sidecar channels.

    Isolated so the run loop's reconnect path can call the same constructor
    after a redis.ConnectionError without inlining the wiring.
    """
    client = redis_lib.from_url(REDIS_URL)
    pubsub = client.pubsub()
    pubsub.subscribe(ROUTING_CHANNEL, ANOMALY_CHANNEL, POLICY_CHANNEL)
    return client, pubsub


def _run_loop(stop_event: threading.Event | None = None) -> None:
    global _sidecar_ready, _last_routing_monotonic, _excluded_backends
    global _adapter, _registry

    print(f"[{SERVICE_NAME}] run loop starting "
          f"(nginx_container={NGINX_CONTAINER} conf={NGINX_CONF_PATH} "
          f"interval={POLL_INTERVAL_SECONDS}s)", flush=True)

    if stop_event is None:
        stop_event = _shutdown_event

    try:
        adapter, docker_client = _build_adapter()
        registry = BackendRegistry(docker_client, seed_backends=ALL_BACKENDS)
    except Exception as exc:  # noqa: BLE001
        print(f"[{SERVICE_NAME}] adapter/registry init failed: {exc}", flush=True)
        return

    # SOT §30 backend_health hydration. Best-effort — failure does not
    # block sidecar startup.
    hydrated = _hydrate_excluded_from_db(adapter, registry)
    if hydrated:
        print(f"[{SERVICE_NAME}] backend_health hydration: "
              f"excluded {hydrated} unhealthy backend(s) on startup", flush=True)

    with _state_lock:
        _adapter = adapter
        _registry = registry
        _excluded_backends = set(adapter.current_state().excluded_backends)
        _sidecar_ready = True

    redis_client, pubsub = _open_redis_pubsub()

    print(f"[{SERVICE_NAME}] sidecar ready — subscribed to "
          f"{ROUTING_CHANNEL}, {ANOMALY_CHANNEL}, {POLICY_CHANNEL}", flush=True)

    try:
        while not stop_event.is_set():
            try:
                message = pubsub.get_message(ignore_subscribe_messages=True,
                                             timeout=POLL_INTERVAL_SECONDS)
            except redis_lib.exceptions.ConnectionError as exc:
                # SOT §8.1 line 2299: sidecar failure must not affect NGINX
                # request serving. We log, back off, and rebuild the pubsub
                # subscription. NGINX keeps serving with the last applied
                # config in the meantime.
                print(f"[{SERVICE_NAME}] Redis connection lost ({exc}); "
                      f"reconnecting in {_REDIS_RECONNECT_BACKOFF_SECONDS}s", flush=True)
                try:
                    pubsub.close()
                except Exception:  # noqa: BLE001
                    pass
                if stop_event.wait(timeout=_REDIS_RECONNECT_BACKOFF_SECONDS):
                    break
                try:
                    redis_client, pubsub = _open_redis_pubsub()
                    print(f"[{SERVICE_NAME}] Redis reconnected", flush=True)
                except Exception as reconnect_exc:  # noqa: BLE001
                    print(f"[{SERVICE_NAME}] Redis reconnect failed "
                          f"({reconnect_exc}); will retry", flush=True)
                continue

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

            # #155 — invalidate the backend-discovery cache on every message so
            # a freshly-provisioned backend appears in the next routing /
            # policy decision. Cache rebuilds on the next discover() call;
            # the 2 s TTL caps the per-burst Docker-query rate.
            invalidate_backend_discovery_cache()

            if channel == ROUTING_CHANNEL:
                with _state_lock:
                    threshold = _policy_state.rl_confidence_threshold
                live_backends = discover_all_backends(
                    docker_client, seed_backends=ALL_BACKENDS_SEED,
                )
                outcome = handle_routing(
                    payload, registry, adapter, live_backends,
                    confidence_threshold=threshold,
                )
                if outcome.applied:
                    with _state_lock:
                        _last_routing_monotonic = time.monotonic()
                        _excluded_backends = set(adapter.current_state().excluded_backends)
                    print(f"[{SERVICE_NAME}] routing applied "
                          f"({outcome.weight_count} backends, "
                          f"confidence={outcome.confidence:.3f})", flush=True)
                elif outcome.rejected_below_threshold:
                    # SOT §13 line 3128 — confidence gate fired.
                    print(f"[{SERVICE_NAME}] routing rejected "
                          f"(confidence={outcome.confidence:.3f} < "
                          f"threshold={threshold:.3f}); using previous weights",
                          flush=True)
                elif outcome.mode == "shadow":
                    # SOT v1.0.6 line 5703 — shadow envelopes are received
                    # but not applied. Log so operators can confirm the
                    # gate is firing.
                    print(f"[{SERVICE_NAME}] routing shadow — not applied "
                          f"({outcome.weight_count} rankings)", flush=True)
                elif outcome.error:
                    print(f"[{SERVICE_NAME}] routing error: {outcome.error}",
                          flush=True)

            elif channel == ANOMALY_CHANNEL:
                outcome = handle_anomaly(payload, registry, adapter)
                if outcome.applied:
                    with _state_lock:
                        _excluded_backends = set(adapter.current_state().excluded_backends)
                    print(f"[{SERVICE_NAME}] anomaly: {outcome.action} "
                          f"{outcome.backend_id}", flush=True)
                elif outcome.error:
                    print(f"[{SERVICE_NAME}] anomaly error: {outcome.error}",
                          flush=True)

            elif channel == POLICY_CHANNEL:
                with _state_lock:
                    policy_state_ref = _policy_state
                live_backends = discover_all_backends(
                    docker_client, seed_backends=ALL_BACKENDS_SEED,
                )
                outcome = handle_policy(payload, adapter, live_backends,
                                        policy_state=policy_state_ref)
                if outcome.applied:
                    with _state_lock:
                        _last_routing_monotonic = time.monotonic()
                        _excluded_backends = set(adapter.current_state().excluded_backends)
                    print(f"[{SERVICE_NAME}] safe_mode active — "
                          f"reverted to equal weights", flush=True)
                elif outcome.error:
                    print(f"[{SERVICE_NAME}] policy error: {outcome.error}",
                          flush=True)
                else:
                    # Policy changes that don't trigger a rewrite still
                    # update PolicyState (rl_confidence_threshold, etc).
                    print(f"[{SERVICE_NAME}] policy snapshot updated "
                          f"(safe_mode={outcome.safe_mode}, "
                          f"rl_confidence_threshold={outcome.rl_confidence_threshold:.3f})",
                          flush=True)
    finally:
        try:
            pubsub.close()
        except Exception:  # noqa: BLE001
            pass
        print(f"[{SERVICE_NAME}] run loop exited", flush=True)


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
            safe_mode = _policy_state.safe_mode
            rl_threshold = _policy_state.rl_confidence_threshold
        body["sidecar_ready"] = ready
        body["last_routing_age_seconds"] = (
            None if last is None else round(time.monotonic() - last, 2)
        )
        body["excluded_backends"] = excluded
        body["policy_safe_mode"] = safe_mode
        body["rl_confidence_threshold"] = rl_threshold
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


def _install_signal_handlers() -> None:
    """Wire SIGTERM/SIGINT to the module-level shutdown event.

    The run loop polls _shutdown_event between message reads and exits
    via its finally: block (close pubsub). Docker SIGTERM during
    `compose down` now produces a clean exit instead of leaving an orphan
    pubsub subscription on Redis. Signals can only be installed from the
    main thread — calling this anywhere else raises ValueError, which we
    swallow so test runners aren't affected.
    """
    def _handler(_signum, _frame):
        print(f"[{SERVICE_NAME}] shutdown signal received", flush=True)
        _shutdown_event.set()

    try:
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
    except ValueError:
        pass


if __name__ == "__main__":
    _install_signal_handlers()

    if RUNLOOP_ENABLED:
        _start_runloop_thread()
    else:
        print(f"[{SERVICE_NAME}] run loop disabled "
              f"(set LB_SIDECAR_RUNLOOP_ENABLED=true to enable)", flush=True)

    print(f"[{SERVICE_NAME}] starting on port {PORT}", flush=True)
    app.run(host="0.0.0.0", port=PORT)
