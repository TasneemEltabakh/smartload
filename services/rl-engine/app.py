"""
services/rl-engine/app.py
──────────────────────────
RL-engine entry point.

Phase-0 mode (default):  /health only, no recommendations published.
Phase-1 mode:            enabled by RL_RUNLOOP_ENABLED=true. The service
                         polls TimescaleDB on POLL_INTERVAL_SECONDS, runs
                         the configured policy on the per-backend state,
                         and publishes RoutingRecommendation envelopes
                         to smartload.routing. Subscribes to
                         smartload.policy for live parameter reload.

Policy selection (RL_POLICY env var):
  - "random_shadow" (default)  — uniform-random scores; always reports
                                  mode="shadow"; no model artifact needed.
  - "ppo"                      — trained PPO policy from issue #27.
                                  Falls back to random_shadow if the
                                  policy.zip is missing.

Operator mode pin (RL_MODE env var):
  - "shadow" (default)  — published `mode` field is always "shadow",
                          regardless of what the policy returned.
  - "active"            — when paired with a policy that itself returns
                          mode="active", the LB sidecar will apply the
                          weights. safe_mode=true in operating policy
                          forces shadow regardless.

Safety:
  - The run loop is opt-in via RL_RUNLOOP_ENABLED=false default so the
    Phase-0 stub stays the default until the cutover is smoke-tested.
  - If the requested policy can't load, the service runs random_shadow
    and reports policy_ready=false on /health — never crashes on startup.

Health endpoint adds four engine fields when the run loop is enabled:
  policy_type, policy_ready, last_inference_age_seconds, rl_mode.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone

import psycopg2
import redis as redis_lib
from flask import Flask, Response, jsonify
from prometheus_client import Counter

# Resolve shared/ across container layout (/app/shared) and dev layout
# (services/shared/ relative to this file). Same pattern as siblings.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (_HERE, os.path.dirname(_HERE)):
    if os.path.isdir(os.path.join(_cand, "shared")):
        sys.path.insert(0, _cand)
        break
from shared.contracts import publish_envelope, parse_envelope  # noqa: E402
from shared.metrics import ServiceMetrics, metrics_response     # noqa: E402
from shared.queries import RL_STATE_QUERY                      # noqa: E402

from runloop import (                                          # noqa: E402
    EnginePolicy,
    action_to_event_payload,
    bootstrap_policy,
    build_state_from_rows,
    effective_mode,
    policy_from_payload,
    serialize_engine_state,
    should_publish,
)

app = Flask(__name__)

# Prometheus own-metrics (#161). RL_ACTION_TOTAL is the decision distribution:
# routing has no discrete action set, so we count inferences by policy +
# effective mode (active / shadow / safe_mode) — the operationally meaningful
# split (how many recommendations were actually applied vs observed).
METRICS = ServiceMetrics("rl_engine")
RL_ACTION_TOTAL = Counter(
    "rl_engine_action_total",
    "RL routing inferences by policy and effective mode",
    ["policy", "mode"],
)

SERVICE_NAME = os.environ.get("SERVICE_NAME", "rl-engine")
PORT = int(os.environ.get("PORT", "8084"))
TIMESCALEDB_URL = os.environ.get(
    "TIMESCALEDB_URL",
    "postgresql://postgres:changeme@timescaledb:5432/smartloaddb",
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")

RUNLOOP_ENABLED       = os.environ.get("RL_RUNLOOP_ENABLED", "false").lower() == "true"
RL_POLICY             = os.environ.get("RL_POLICY", "random_shadow")
RL_MODE               = os.environ.get("RL_MODE", "shadow")
RL_SERVICE            = os.environ.get("RL_SERVICE", "load-balancer")
POLL_INTERVAL_SECONDS = float(os.environ.get("POLL_INTERVAL_SECONDS", "5"))
WINDOW_SECONDS        = int(os.environ.get("RL_WINDOW_SECONDS", "30"))

# Liveness threshold for /health (#163). If the loop hasn't ticked in this
# many seconds, /health flips to degraded so the silent-thread-death pattern
# becomes visible.
LIVENESS_STALE_SECONDS         = 5 * POLL_INTERVAL_SECONDS
LOOP_RECOVERY_BACKOFF_SECONDS  = 2.0
POLICY_MANAGER_URL    = os.environ.get("POLICY_MANAGER_URL", "http://policy-manager:8086")

ROUTING_CHANNEL = "smartload.routing"
POLICY_CHANNEL  = "smartload.policy"
ANOMALY_CHANNEL = "smartload.anomaly"


# ── shared state ──────────────────────────────────────────────────────────────

_state_lock = threading.Lock()
_policy = None
_policy_name: str = RL_POLICY
_policy_requested: str = RL_POLICY
_policy_ready: bool = False
_policy_error: str | None = None
_engine_policy: EnginePolicy = EnginePolicy()
_last_inference_monotonic: float | None = None
# Health verdicts received from smartload.anomaly (SOT §9 health ownership).
# backend_id → (status, monotonic_seen_at). The monotonic timestamp drives
# eviction in _evict_stale_anomaly_health() so entries for vanished backends
# (k8s replica churn, autoscaler tear-down) don't accumulate indefinitely.
_anomaly_health: dict[str, tuple[str, float]] = {}

# How long an anomaly verdict stays authoritative before we re-derive health
# locally. Multiplier on RL_WINDOW_SECONDS so the eviction horizon scales with
# the query window rather than being a free parameter.
ANOMALY_HEALTH_TTL_MULTIPLIER: int = 6     # 6 × 30 s = 3 min by default
_anomaly_health_ttl_seconds: float = WINDOW_SECONDS * ANOMALY_HEALTH_TTL_MULTIPLIER

# Live Engines (#121) tracking — appended each cycle, read by /api/v1/engine/state.
_ticks_total: int = 0
_publishes_total: int = 0
_last_tick_at_iso: str | None = None
_last_publish_at_iso: str | None = None
_last_output_payload: dict | None = None

# Graceful shutdown signal — set by SIGTERM/SIGINT handler, observed by the
# run loop between cycles. Daemon thread + container SIGTERM gives us the
# basic behaviour for free, but explicit wiring lets the loop drain its
# current cycle and close the DB / pubsub instead of being yanked.
_shutdown_event = threading.Event()


def _set_policy_state(bootstrap) -> None:
    global _policy, _policy_name, _policy_requested, _policy_ready, _policy_error
    _policy = bootstrap.policy
    _policy_name = bootstrap.name
    _policy_requested = bootstrap.requested
    _policy_error = bootstrap.error
    # PPOPolicy (and future ML plugins) expose a policy_ready property that
    # reflects whether the model artifact loaded successfully, independent of
    # whether __init__ raised.  Use it when available; fall back to bootstrap.ready.
    plugin_ready = getattr(bootstrap.policy, "policy_ready", None)
    _policy_ready = plugin_ready if plugin_ready is not None else bootstrap.ready


# ── connectivity checks ───────────────────────────────────────────────────────

def check_redis() -> tuple[bool, str | None]:
    try:
        redis_lib.from_url(REDIS_URL, socket_connect_timeout=3).ping()
        return True, None
    except Exception as exc:                            # noqa: BLE001
        return False, str(exc)


def check_timescaledb() -> tuple[bool, str | None]:
    try:
        psycopg2.connect(TIMESCALEDB_URL, connect_timeout=5).close()
        return True, None
    except Exception as exc:                            # noqa: BLE001
        return False, str(exc)


# ── inference cycle ───────────────────────────────────────────────────────────

def _query_state(db_conn, health_snapshot: dict[str, str]) -> list:
    """Run RL_STATE_QUERY against the live DB and shape rows into
    list[BackendState]. Both the interval and service filter are bound as
    parameters per SOT §11. health_snapshot (from smartload.anomaly) takes
    precedence over local classification per SOT §9 health-ownership rule."""
    with db_conn.cursor() as cur:
        cur.execute(RL_STATE_QUERY, (f"{WINDOW_SECONDS} seconds", RL_SERVICE))
        rows = cur.fetchall()
    return build_state_from_rows(rows, anomaly_health=health_snapshot)


def _evict_stale_anomaly_health(now_monotonic: float) -> None:
    """Drop anomaly_health entries older than _anomaly_health_ttl_seconds.

    Caller must hold _state_lock. Run at the top of each cycle so the
    health_snapshot passed into _query_state reflects only verdicts that
    are still load-bearing — otherwise a backend that disappeared an hour
    ago would freeze its last status forever.
    """
    stale = [
        bid for bid, (_status, seen_at) in _anomaly_health.items()
        if now_monotonic - seen_at > _anomaly_health_ttl_seconds
    ]
    for bid in stale:
        del _anomaly_health[bid]


def _anomaly_health_status_snapshot() -> dict[str, str]:
    """Flatten the (status, ts) values to a plain status dict.

    Callers don't need the timestamp — that's an eviction concern.
    """
    return {bid: status for bid, (status, _ts) in _anomaly_health.items()}


def _inference_cycle(db_conn, redis_client) -> tuple[int, bool]:
    """One poll cycle.

    Returns (published, db_ok):
      - published — 1 if a recommendation was published, else 0
      - db_ok     — False iff the DB query hit a psycopg2.OperationalError
                    (the caller rebuilds the connection on False; other
                    failures are transient and don't trigger reconnect).
    """
    global _last_inference_monotonic, _ticks_total, _publishes_total
    global _last_tick_at_iso, _last_publish_at_iso, _last_output_payload

    now_monotonic = time.monotonic()

    with _state_lock:
        _evict_stale_anomaly_health(now_monotonic)
        pol_instance    = _policy
        eng_policy      = _engine_policy
        health_snapshot = _anomaly_health_status_snapshot()

    if pol_instance is None:
        return 0, True

    try:
        state = _query_state(db_conn, health_snapshot)
    except psycopg2.OperationalError as exc:
        # DB went away (restart, network partition). Surface to the loop so
        # it rebuilds the connection on the next tick — the current cycle
        # produces nothing.
        print(f"[{SERVICE_NAME}] DB connection lost: {exc}", flush=True)
        return 0, False
    except Exception as exc:                            # noqa: BLE001
        print(f"[{SERVICE_NAME}] DB query failed: {exc}", flush=True)
        return 0, True

    now_iso = datetime.now(timezone.utc).isoformat()

    if not should_publish(state):
        with _state_lock:
            _last_inference_monotonic = time.monotonic()
            _ticks_total += 1
            _last_tick_at_iso = now_iso
        return 0, True

    try:
        action = pol_instance.act(state)
    except Exception as exc:                            # noqa: BLE001
        print(f"[{SERVICE_NAME}] policy.act failed: {exc}", flush=True)
        return 0, True

    mode = effective_mode(action.mode, RL_MODE, eng_policy)
    payload = action_to_event_payload(action, mode, eng_policy.policy_version)
    RL_ACTION_TOTAL.labels(policy=RL_POLICY, mode=mode).inc()

    with METRICS.time_publish(ROUTING_CHANNEL):
        publish_envelope(
            redis_client,
            channel=ROUTING_CHANNEL,
            source=SERVICE_NAME,
            payload=payload,
        )

    with _state_lock:
        _last_inference_monotonic = time.monotonic()
        _ticks_total += 1
        _publishes_total += 1
        _last_tick_at_iso = now_iso
        _last_publish_at_iso = now_iso
        _last_output_payload = payload

    return 1, True


# ── policy subscription ───────────────────────────────────────────────────────

def _handle_policy_message(raw) -> None:
    """Parse a smartload.policy envelope, swap _engine_policy, and rebuild
    the RL policy so any constructor-derived params (exploration rate,
    confidence threshold) take effect immediately."""
    parsed = parse_envelope(raw, channel=POLICY_CHANNEL)
    if parsed is None:
        return
    payload, _meta = parsed

    with _state_lock:
        new_policy = policy_from_payload(payload, fallback=_engine_policy)
        if new_policy.policy_version < _engine_policy.policy_version:
            print(f"[{SERVICE_NAME}] ignoring policy v{new_policy.policy_version} "
                  f"(current v{_engine_policy.policy_version}) — stale publish",
                  flush=True)
            return
        _refresh_policy_under_lock(new_policy)


def _handle_anomaly_message(raw) -> None:
    """Update _anomaly_health from a smartload.anomaly envelope (SOT §9).

    Each entry carries a monotonic seen-at timestamp so old entries get
    swept by _evict_stale_anomaly_health on the next cycle. The TTL
    prevents the dict from growing without bound when backend IDs churn.
    """
    parsed = parse_envelope(raw, channel=ANOMALY_CHANNEL)
    if parsed is None:
        return
    payload, _meta = parsed
    backend_id = payload.get("backend_id")
    status     = payload.get("status")
    if backend_id and status in ("healthy", "degraded", "unhealthy"):
        with _state_lock:
            _anomaly_health[backend_id] = (status, time.monotonic())


def _refresh_policy_under_lock(new_policy: EnginePolicy) -> None:
    """Apply a new EnginePolicy to the running policy instance.

    Caller must hold _state_lock. Cheap path (no disk hit, no torch graph
    rebuild): if a policy is already loaded, push the new policy_kwargs via
    reload(**kwargs). Falls through to full bootstrap_policy only on first
    load or when the cheap path raises. Avoids the previous behaviour of
    reloading policy.zip on every safe_mode flip / exploration_rate tweak.
    """
    global _engine_policy
    _engine_policy = new_policy
    if _policy is not None:
        try:
            _policy.reload(**new_policy.policy_kwargs())
            return
        except (NotImplementedError, TypeError) as exc:
            # NotImplementedError → plugin opts out of in-place reload.
            # TypeError            → kwarg signature mismatch.
            # Either way, fall through to a full bootstrap.
            print(f"[{SERVICE_NAME}] policy.reload() bypassed "
                  f"({exc!r}); reconstructing policy", flush=True)
    boot = bootstrap_policy(_policy_requested, _engine_policy)
    _set_policy_state(boot)


# ── run loop ──────────────────────────────────────────────────────────────────

def _open_db_connection():
    """Build a new TimescaleDB connection. Isolated so the reconnect path
    in _run_loop can call the same constructor without inlining it."""
    conn = psycopg2.connect(TIMESCALEDB_URL)
    conn.autocommit = True
    return conn


def _run_loop(stop_event: threading.Event | None = None) -> None:
    print(f"[{SERVICE_NAME}] run loop starting "
          f"(policy={_policy_name} ready={_policy_ready} "
          f"rl_mode={RL_MODE} interval={POLL_INTERVAL_SECONDS}s)", flush=True)

    # Fall back to the module-level shutdown event when callers don't
    # supply one (unit tests pass their own to exit deterministically).
    if stop_event is None:
        stop_event = _shutdown_event

    redis_client = redis_lib.from_url(REDIS_URL)
    pubsub = redis_client.pubsub()
    pubsub.subscribe(POLICY_CHANNEL)
    pubsub.subscribe(ANOMALY_CHANNEL)

    db_conn = _open_db_connection()

    next_tick = time.monotonic()

    try:
        while not stop_event.is_set():
            try:
                # Drain one message per iteration — never block longer than
                # 1 s so we don't drift the poll cadence.
                message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is not None and message.get("type") == "message":
                    channel = message.get("channel")
                    if isinstance(channel, bytes):
                        channel = channel.decode()
                    if channel == POLICY_CHANNEL:
                        _handle_policy_message(message["data"])
                    elif channel == ANOMALY_CHANNEL:
                        _handle_anomaly_message(message["data"])

                now = time.monotonic()
                if now >= next_tick:
                    with METRICS.time_cycle() as _c:
                        published, db_ok = _inference_cycle(db_conn, redis_client)
                        _c["outcome"] = "published" if published else "idle"
                    if published:
                        print(f"[{SERVICE_NAME}] published routing recommendation "
                              f"(policy={_policy_name})", flush=True)
                    if not db_ok:
                        # OperationalError on the cycle — drop the dead
                        # connection and try a fresh one. Skip the next
                        # tick to give the DB a moment to come back without
                        # hot-looping.
                        try:
                            db_conn.close()
                        except Exception:                   # noqa: BLE001
                            pass
                        try:
                            db_conn = _open_db_connection()
                            print(f"[{SERVICE_NAME}] DB connection rebuilt", flush=True)
                        except Exception as exc:            # noqa: BLE001
                            print(f"[{SERVICE_NAME}] DB reconnect failed: {exc}; "
                                  "retrying next tick", flush=True)
                    next_tick = now + POLL_INTERVAL_SECONDS
            except Exception as exc:                        # noqa: BLE001
                # #163: an unexpected exception in the iteration body must
                # not kill the daemon thread. Log + back off + continue.
                print(f"[{SERVICE_NAME}] loop iteration raised "
                      f"{type(exc).__name__}: {exc}; continuing after "
                      f"{LOOP_RECOVERY_BACKOFF_SECONDS}s backoff", flush=True)
                if stop_event.wait(timeout=LOOP_RECOVERY_BACKOFF_SECONDS):
                    break
    finally:
        # Best-effort drain on shutdown — we don't propagate errors here
        # because we're already on the way out.
        try:
            pubsub.close()
        except Exception:                                    # noqa: BLE001
            pass
        try:
            db_conn.close()
        except Exception:                                    # noqa: BLE001
            pass
        print(f"[{SERVICE_NAME}] run loop exited", flush=True)


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
        "rl_mode": RL_MODE,
    }
    if RUNLOOP_ENABLED:
        with _state_lock:
            last = _last_inference_monotonic
            body["policy_type"]      = _policy_name
            body["policy_requested"] = _policy_requested
            body["policy_ready"]     = _policy_ready
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
    """Live Engines (#121) — policy bootstrap, operating-policy snapshot,
    runloop stats, last cycle output, env-pinned rl_mode. Read by the
    operator-ui BFF for per-engine cards. Always returns 200; runloop-disabled
    is a state, not an error."""
    with _state_lock:
        body = serialize_engine_state(
            service=SERVICE_NAME,
            channel=ROUTING_CHANNEL,
            runloop_enabled=RUNLOOP_ENABLED,
            policy_name=_policy_name,
            policy_requested=_policy_requested,
            policy_ready=_policy_ready,
            policy_error=_policy_error,
            engine_policy=_engine_policy,
            rl_mode_env=RL_MODE,
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
    return jsonify({"service": SERVICE_NAME, "status": "running", "rl_mode": RL_MODE})


# ── startup ───────────────────────────────────────────────────────────────────

def _pull_initial_policy() -> None:
    """Pull current policy from Policy Manager on startup (SOT §11 — pull on startup)."""
    url = f"{POLICY_MANAGER_URL}/api/v1/policy"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        payload = data.get("payload") or data
        new_policy = policy_from_payload(payload, fallback=_engine_policy)
        if new_policy.policy_version >= _engine_policy.policy_version:
            with _state_lock:
                _refresh_policy_under_lock(new_policy)
            print(f"[{SERVICE_NAME}] pulled initial policy v{new_policy.policy_version} "
                  f"from {url}", flush=True)
    except Exception as exc:                   # noqa: BLE001
        print(f"[{SERVICE_NAME}] startup policy pull failed ({exc}); "
              "using defaults", flush=True)


def _start_runloop_thread() -> None:
    boot = bootstrap_policy(RL_POLICY, _engine_policy)
    _set_policy_state(boot)
    if not boot.ready:
        print(f"[{SERVICE_NAME}] policy {boot.requested!r} unavailable "
              f"({boot.error}); falling back to {boot.name!r}", flush=True)

    t = threading.Thread(target=_run_loop, daemon=True, name="rl-runloop")
    t.start()


def _install_signal_handlers() -> None:
    """Wire SIGTERM/SIGINT to the module-level shutdown event.

    The run loop polls _shutdown_event between cycles and runs its finally:
    block (close pubsub + db) on its way out. Docker's SIGTERM during
    `compose down` now produces a clean exit instead of leaving an orphan
    pubsub subscription on Redis. Signals can only be installed in the main
    thread; called from __main__ only.
    """
    def _handler(_signum, _frame):
        print(f"[{SERVICE_NAME}] shutdown signal received", flush=True)
        _shutdown_event.set()

    try:
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
    except ValueError:
        # Raised when not called from the main thread (e.g. in tests
        # that exercise app.py via a worker). Safe to ignore.
        pass


if __name__ == "__main__":
    _install_signal_handlers()

    if RUNLOOP_ENABLED:
        _pull_initial_policy()
        _start_runloop_thread()
    else:
        print(f"[{SERVICE_NAME}] run loop disabled "
              f"(set RL_RUNLOOP_ENABLED=true to enable)", flush=True)

    print(f"[{SERVICE_NAME}] starting on port {PORT} (mode={RL_MODE})", flush=True)
    app.run(host="0.0.0.0", port=PORT)
