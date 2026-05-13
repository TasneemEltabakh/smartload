"""
services/autoscaler/app.py
───────────────────────────
T1.3 — Autoscaler / Resource Manager.

Per SOT §8.8:
  - Subscribes to smartload.forecast (ForecastResult envelopes).
  - Compares predicted_rps to current_backends × per_instance_capacity_rps.
  - Scales test-backend containers via Docker SDK (step = 1).
  - Honors min_backends, max_backends, autoscaler_cooldown_seconds from
    config/policy.yaml (loaded at startup). Live reload on smartload.policy
    is planned under T1.4 (issue #32) but not yet wired here.
  - Writes one row to scaling_events per action (SCALING_EVENT_INSERT).
  - Publishes ScalingEvent envelopes on smartload.scale.
  - Reactive fallback: if the last forecast is older than 2 × horizon,
    query observed request rate from TimescaleDB and decide on that.

Threading model:
  - Main thread runs Flask /health (used by tests/integration/conftest.py
    to gate the stack_ready fixture).
  - One background daemon thread runs the Redis subscriber + control loop.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone

import psycopg2
import redis as redis_lib
import yaml
from flask import Flask, jsonify

# Resolve the canonical shared/ module across two layouts:
#   container: /app/shared       (sibling of app.py — Dockerfile copies it)
#   dev / CI:  services/shared   (parent dir of services/autoscaler/app.py)
_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (_HERE, os.path.dirname(_HERE)):
    if os.path.isdir(os.path.join(_cand, "shared")):
        sys.path.insert(0, _cand)
        break

from shared.contracts import (  # noqa: E402
    ScalingEvent,
    make_envelope,
    parse_envelope,
)
from shared.queries import (  # noqa: E402
    OBSERVED_RPS_QUERY,
    SCALING_EVENT_INSERT,
)

from cluster_client import DockerClusterClient  # noqa: E402
from decisions import (  # noqa: E402
    ACTION_NOOP,
    ACTION_SCALE_IN,
    ACTION_SCALE_OUT,
    Decision,
    Policy,
    decide,
)


# ── config ────────────────────────────────────────────────────────────────────

SERVICE_NAME     = os.environ.get("SERVICE_NAME", "autoscaler")
PORT             = int(os.environ.get("PORT", "8085"))
TIMESCALEDB_URL  = os.environ.get(
    "TIMESCALEDB_URL",
    "postgresql://postgres:changeme@timescaledb:5432/smartloaddb",
)
REDIS_URL        = os.environ.get("REDIS_URL", "redis://redis:6379")
POLICY_PATH      = os.environ.get("POLICY_PATH", "/config/policy.yaml")
FORECAST_CHANNEL = "smartload.forecast"
SCALE_CHANNEL    = "smartload.scale"

# How long to block on a single pubsub.get_message() call. Doubles as the
# reactive-fallback poll interval — when no forecast arrives within this
# window, we check whether to fall back on observed RPS.
LOOP_TICK_SECONDS = float(os.environ.get("LOOP_TICK_SECONDS", "5.0"))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [autoscaler] %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("autoscaler")


# ── policy ────────────────────────────────────────────────────────────────────

def load_policy(path: str) -> Policy:
    """Load policy.yaml. Falls back to SOT §8.8 defaults if any field is missing."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        log.warning("policy file %s not found; using SOT defaults", path)
        data = {}
    return Policy(
        min_backends=int(data.get("min_backends", 1)),
        max_backends=int(data.get("max_backends", 5)),
        per_instance_capacity_rps=float(data.get("per_instance_capacity_rps", 100)),
        cooldown_seconds=float(data.get("autoscaler_cooldown_seconds", 60)),
    )


# ── runtime state (guarded by _state_lock) ────────────────────────────────────

_state_lock              = threading.Lock()
_policy: Policy          = Policy(1, 5, 100.0, 60.0)
_last_action_monotonic: float | None = None
_last_forecast_monotonic: float | None = None
_last_forecast_horizon_min: int        = 5
_actions_total           = 0
_actions_scale_out       = 0
_actions_scale_in        = 0
_actions_noop            = 0


def _bump_action(action: str) -> None:
    global _actions_total, _actions_scale_out, _actions_scale_in, _actions_noop
    with _state_lock:
        _actions_total += 1
        if action == ACTION_SCALE_OUT:
            _actions_scale_out += 1
        elif action == ACTION_SCALE_IN:
            _actions_scale_in += 1
        else:
            _actions_noop += 1


def _stats_snapshot() -> dict:
    with _state_lock:
        return {
            "actions_total":     _actions_total,
            "actions_scale_out": _actions_scale_out,
            "actions_scale_in":  _actions_scale_in,
            "actions_noop":      _actions_noop,
        }


# ── observed RPS for reactive fallback ────────────────────────────────────────

def observed_rps(db_conn) -> float:
    """Return request rate over the last 60 s. 0.0 if no rows."""
    with db_conn.cursor() as cur:
        cur.execute(OBSERVED_RPS_QUERY)
        row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


# ── action: scale + persist + publish ─────────────────────────────────────────

def apply_decision(
    decision: Decision,
    cluster,
    db_conn,
    redis_client,
    forecast_event_id: str | None,
) -> None:
    """Execute the decision: scale, write scaling_events row, publish envelope.

    NOOP decisions do not touch Docker or the DB — they are logged only.
    """
    if decision.action == ACTION_NOOP:
        log.info("noop: %s (current=%d)", decision.reason, decision.target_count)
        _bump_action(ACTION_NOOP)
        return

    if decision.action == ACTION_SCALE_OUT:
        name = cluster.scale_out()
    else:
        name = cluster.scale_in()

    if name is None:
        # Cluster could not actuate (e.g. no stopped container to start, or no
        # running container to stop). Log + skip the DB/publish writes — the
        # state never materialised.
        log.warning(
            "%s requested but cluster could not actuate (target=%d, reason=%r)",
            decision.action, decision.target_count, decision.reason,
        )
        _bump_action(ACTION_NOOP)
        return

    log.info(
        "%s container=%s target_count=%d reason=%r",
        decision.action, name, decision.target_count, decision.reason,
    )

    # scaling_events: autoscaler is the only writer (SOT §8.8).
    with db_conn.cursor() as cur:
        cur.execute(
            SCALING_EVENT_INSERT,
            (
                datetime.now(timezone.utc),
                decision.action,
                decision.target_count,
                decision.reason,
            ),
        )
    db_conn.commit()

    # smartload.scale: audit envelope.
    event = ScalingEvent(
        action=decision.action,
        instance_count=decision.target_count,
        reason=decision.reason,
        forecast_event_id=forecast_event_id,
    )
    envelope = make_envelope(source=SERVICE_NAME, payload=event)
    redis_client.publish(SCALE_CHANNEL, json.dumps(asdict(envelope)))

    with _state_lock:
        global _last_action_monotonic
        _last_action_monotonic = time.monotonic()
    _bump_action(decision.action)


# ── control loop ──────────────────────────────────────────────────────────────

def _seconds_since(monotonic_at: float | None) -> float | None:
    return None if monotonic_at is None else time.monotonic() - monotonic_at


def control_loop(stop_event: threading.Event | None = None) -> None:
    """Subscribe to smartload.forecast and act on each envelope.

    Between messages, run a reactive-fallback check every LOOP_TICK_SECONDS
    seconds — if the last forecast is older than 2 × horizon, scale on
    observed request rate instead.
    """
    log.info(
        "control loop starting (policy=%s)",
        _policy,
    )

    redis_client = redis_lib.from_url(REDIS_URL)
    pubsub = redis_client.pubsub()
    pubsub.subscribe(FORECAST_CHANNEL)

    db_conn = psycopg2.connect(TIMESCALEDB_URL)
    cluster = DockerClusterClient()

    while True:
        if stop_event is not None and stop_event.is_set():
            break

        message = pubsub.get_message(
            ignore_subscribe_messages=True,
            timeout=LOOP_TICK_SECONDS,
        )

        if message is not None and message.get("type") == "message":
            _handle_forecast_message(message["data"], cluster, db_conn, redis_client)
            continue

        # No forecast arrived this tick — consider reactive fallback.
        _maybe_reactive_fallback(cluster, db_conn, redis_client)


def _handle_forecast_message(raw, cluster, db_conn, redis_client) -> None:
    parsed = parse_envelope(raw, channel=FORECAST_CHANNEL)
    if parsed is None:
        return
    payload, envelope_meta = parsed
    try:
        predicted_rps   = float(payload["predicted_rps"])
        horizon_minutes = int(payload["horizon_minutes"])
    except (KeyError, TypeError, ValueError):
        log.warning("forecast payload missing required fields: %s", payload)
        return

    with _state_lock:
        global _last_forecast_monotonic, _last_forecast_horizon_min
        _last_forecast_monotonic    = time.monotonic()
        _last_forecast_horizon_min  = horizon_minutes
        seconds_since_action        = _seconds_since(_last_action_monotonic)
        policy                      = _policy

    current_count = cluster.get_backend_count()
    decision = decide(
        predicted_rps=predicted_rps,
        current_count=current_count,
        policy=policy,
        seconds_since_last_action=seconds_since_action,
        now_text="forecast",
    )
    apply_decision(decision, cluster, db_conn, redis_client, envelope_meta.get("event_id"))


def _maybe_reactive_fallback(cluster, db_conn, redis_client) -> None:
    with _state_lock:
        last_fc          = _last_forecast_monotonic
        horizon_minutes  = _last_forecast_horizon_min
        seconds_since_action = _seconds_since(_last_action_monotonic)
        policy           = _policy

    if last_fc is None:
        # No forecast has arrived yet — wait, don't react. SOT §8.8: reactive
        # fallback triggers when forecasts go STALE, not when absent on boot.
        return

    seconds_since_forecast = time.monotonic() - last_fc
    stale_threshold        = 2.0 * horizon_minutes * 60.0
    if seconds_since_forecast < stale_threshold:
        return

    rps = observed_rps(db_conn)
    current_count = cluster.get_backend_count()
    decision = decide(
        predicted_rps=rps,
        current_count=current_count,
        policy=policy,
        seconds_since_last_action=seconds_since_action,
        now_text="reactive",
    )
    apply_decision(decision, cluster, db_conn, redis_client, forecast_event_id=None)


# ── Flask /health (main thread) ───────────────────────────────────────────────

app = Flask(__name__)


def _check_redis():
    try:
        r = redis_lib.from_url(REDIS_URL, socket_connect_timeout=3)
        r.ping()
        return True, None
    except Exception as exc:
        return False, str(exc)


def _check_timescaledb():
    try:
        conn = psycopg2.connect(TIMESCALEDB_URL, connect_timeout=5)
        conn.close()
        return True, None
    except Exception as exc:
        return False, str(exc)


@app.route("/health")
def health():
    redis_ok, redis_err = _check_redis()
    db_ok,    db_err    = _check_timescaledb()
    errors = [e for e in [redis_err, db_err] if e]
    ok     = redis_ok and db_ok
    status = "ok" if ok else "degraded"
    code   = 200 if ok else 503  # SOT §11
    return jsonify({
        "status":      status,
        "service":     SERVICE_NAME,
        "redis":       redis_ok,
        "timescaledb": db_ok,
        "policy":      asdict(_policy),
        "stats":       _stats_snapshot(),
        **({"errors": errors} if errors else {}),
    }), code


@app.route("/")
def index():
    return jsonify({"service": SERVICE_NAME, "status": "running"})


# ── entrypoint ────────────────────────────────────────────────────────────────

def main() -> None:
    global _policy
    _policy = load_policy(POLICY_PATH)
    log.info("loaded policy from %s: %s", POLICY_PATH, _policy)

    t = threading.Thread(target=control_loop, name="autoscaler-control-loop", daemon=True)
    t.start()

    log.info("Flask /health starting on port %d", PORT)
    app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
