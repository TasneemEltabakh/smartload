"""
services/autoscaler/app.py
───────────────────────────
T1.3 — Autoscaler / Resource Manager.

Per SOT §8.8:
  - Subscribes to smartload.forecast (ForecastResult envelopes).
  - Compares predicted_rps to current_backends × per_instance_capacity_rps.
  - Scales test-backend containers via Docker SDK (step = 1).
  - Honors min_backends, max_backends, autoscaler_cooldown_seconds from
    config/policy.yaml (loaded at startup). Live reloads arrive on
    smartload.policy (T1.4) — the control loop subscribes to both channels
    on a single pubsub and dispatches by channel name. Policy swaps happen
    under _state_lock so the next forecast tick sees the new bounds.
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
from flask import Flask, Response, jsonify, request
from prometheus_client import Counter

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
from shared.metrics import ServiceMetrics, metrics_response  # noqa: E402
from shared.queries import (  # noqa: E402
    OBSERVED_RPS_QUERY,
    SCALING_AUDIT_QUERY,
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
    policy_from_payload,
)
from manual import ManualScaleError, plan_manual_scale  # noqa: E402


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
POLICY_CHANNEL   = "smartload.policy"
SCALE_CHANNEL    = "smartload.scale"

# Dynamic-pool provisioning config (#155 adaptive bench). The flag is OFF by
# default — the legacy #148 routing bench harness relies on the toggle-only
# path. Operators flip it ON via env var at the start of an adaptive-bench
# run (see experiments/adaptive-bench/scripts/run.py) and OFF at teardown.
PROVISIONING_ENABLED = os.environ.get(
    "AUTOSCALER_PROVISIONING_ENABLED", "false"
).lower() == "true"
PROVISIONING_IMAGE   = os.environ.get(
    "AUTOSCALER_PROVISIONING_IMAGE", "smartload-test-backend:latest"
)
PROVISIONING_NETWORK = os.environ.get(
    "AUTOSCALER_PROVISIONING_NETWORK", "smartload_smartload-net"
)
PROVISIONING_HEALTHCHECK_TIMEOUT_SECONDS = int(os.environ.get(
    "AUTOSCALER_PROVISIONING_HEALTHCHECK_TIMEOUT_SECONDS", "30"
))

# How long to block on a single pubsub.get_message() call. Doubles as the
# reactive-fallback poll interval — when no forecast arrives within this
# window, we check whether to fall back on observed RPS.
LOOP_TICK_SECONDS = float(os.environ.get("LOOP_TICK_SECONDS", "5.0"))

# Liveness threshold for /health (#163). If the loop hasn't ticked in this
# many seconds, /health flips to degraded so the silent-thread-death pattern
# becomes visible.
LIVENESS_STALE_SECONDS         = 5 * LOOP_TICK_SECONDS
LOOP_RECOVERY_BACKOFF_SECONDS  = 2.0

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
_policy_version: int     = 0
_last_action_monotonic: float | None = None
_last_forecast_monotonic: float | None = None
_last_forecast_horizon_min: int        = 5
# #163 liveness signal — updated at the top of every control-loop iteration
# (whether a message arrived or not) so /health can detect a silent daemon-
# thread death regardless of forecast cadence.
_last_loop_tick_monotonic: float | None = None
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

def _make_cluster_client() -> DockerClusterClient:
    """Build a DockerClusterClient with provisioning config from env vars.

    The `provisioning_enabled` flag controls the #155 dynamic-pool path;
    legacy #148 deployments keep it OFF and pay no extra cost. The
    `max_backends_ceiling` is wired from the live policy so a runtime
    policy change to `max_backends` is honoured on the next decision
    cycle (the autoscaler reconstructs the client via this helper inside
    each control-loop call site).
    """
    with _state_lock:
        ceiling = _policy.max_backends
    return DockerClusterClient(
        provisioning_enabled=PROVISIONING_ENABLED,
        provisioning_image=PROVISIONING_IMAGE,
        provisioning_network=PROVISIONING_NETWORK,
        max_backends_ceiling=ceiling,
        healthcheck_timeout_seconds=PROVISIONING_HEALTHCHECK_TIMEOUT_SECONDS,
    )


def apply_decision(
    decision: Decision,
    cluster,
    db_conn,
    redis_client,
    forecast_event_id: str | None,
) -> None:
    """Execute the decision: scale, write scaling_events row, publish envelope.

    Order is strict for the #155 adaptive bench: cluster.scale_out() blocks
    on the new container's healthcheck reaching `healthy` BEFORE returning
    a non-None name. Only then does this function publish the
    ScalingEvent envelope. This prevents the provision-then-announce race
    where the lb-sidecar would rewrite upstream.conf with a hostname
    Docker DNS hasn't propagated yet (Risk 1 in the #155 plan).

    NOOP decisions do not touch Docker or the DB — they are logged only.
    """
    if decision.action == ACTION_NOOP:
        log.info("noop: %s (current=%d)", decision.reason, decision.target_count)
        _bump_action(ACTION_NOOP)
        return

    if decision.action == ACTION_SCALE_OUT:
        result = cluster.scale_out()
    else:
        result = cluster.scale_in()

    if result is None:
        # Cluster could not actuate (no stopped container to start, provisioning
        # disabled or capped, no running container to stop, no dynamic container
        # to decommission). Log + skip the DB/publish writes — the state never
        # materialised.
        log.warning(
            "%s requested but cluster could not actuate (target=%d, reason=%r)",
            decision.action, decision.target_count, decision.reason,
        )
        _bump_action(ACTION_NOOP)
        return

    name, mechanism = result

    log.info(
        "%s mechanism=%s container=%s target_count=%d reason=%r",
        decision.action, mechanism, name, decision.target_count, decision.reason,
    )

    # The DB schema's `action` column is unchanged ("scale_out" | "scale_in").
    # The new mechanism is recorded textually in `reason` for searchable
    # audit (e.g. "forecast predicted 450 rps > capacity 400 [provision]")
    # AND structurally in the envelope's `mechanism` field for the
    # adaptive-bench analysis pipeline.
    reason_with_mechanism = f"{decision.reason} [{mechanism}]"

    # scaling_events: autoscaler is the only writer (SOT §8.8).
    with db_conn.cursor() as cur:
        cur.execute(
            SCALING_EVENT_INSERT,
            (
                datetime.now(timezone.utc),
                decision.action,
                decision.target_count,
                reason_with_mechanism,
            ),
        )
    db_conn.commit()

    # smartload.scale: audit envelope.
    event = ScalingEvent(
        action=decision.action,
        instance_count=decision.target_count,
        reason=reason_with_mechanism,
        forecast_event_id=forecast_event_id,
        mechanism=mechanism,
    )
    envelope = make_envelope(source=SERVICE_NAME, payload=event)
    with METRICS.time_publish(SCALE_CHANNEL):
        redis_client.publish(SCALE_CHANNEL, json.dumps(asdict(envelope)))
    SCALE_TOTAL.labels(direction=decision.action, mechanism=mechanism or "none").inc()

    with _state_lock:
        global _last_action_monotonic
        _last_action_monotonic = time.monotonic()
    _bump_action(decision.action)


# ── control loop ──────────────────────────────────────────────────────────────

def _seconds_since(monotonic_at: float | None) -> float | None:
    return None if monotonic_at is None else time.monotonic() - monotonic_at


def control_loop(stop_event: threading.Event | None = None) -> None:
    """Subscribe to smartload.forecast + smartload.policy and act on each
    envelope.

    Forecasts trigger scaling decisions; policy updates swap _policy under
    _state_lock so the next tick sees the new bounds. Channel dispatch
    happens on each pubsub message — single thread, single connection.

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
    pubsub.subscribe(FORECAST_CHANNEL, POLICY_CHANNEL)

    db_conn = psycopg2.connect(TIMESCALEDB_URL)
    cluster = _make_cluster_client()

    while True:
        if stop_event is not None and stop_event.is_set():
            break

        try:
            # #163 liveness signal — record that the loop iteration is
            # happening, regardless of whether a message arrived or the
            # reactive fallback fired.
            with _state_lock:
                global _last_loop_tick_monotonic
                _last_loop_tick_monotonic = time.monotonic()

            message = pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=LOOP_TICK_SECONDS,
            )

            if message is not None and message.get("type") == "message":
                channel = message.get("channel")
                if isinstance(channel, bytes):
                    channel = channel.decode()
                if channel == FORECAST_CHANNEL:
                    _handle_forecast_message(message["data"], cluster, db_conn, redis_client)
                elif channel == POLICY_CHANNEL:
                    _handle_policy_message(message["data"])
                else:
                    log.warning("unexpected channel: %s", channel)
                continue

            # No forecast arrived this tick — consider reactive fallback.
            _maybe_reactive_fallback(cluster, db_conn, redis_client)
        except Exception as exc:                            # noqa: BLE001
            # #163: an unexpected exception in the iteration body must not
            # kill the daemon thread. Log + back off + continue. Operators
            # see the staleness via /health's last_loop_tick_age_seconds.
            log.exception(
                "control loop iteration raised %s: continuing after %.1fs backoff",
                type(exc).__name__, LOOP_RECOVERY_BACKOFF_SECONDS,
            )
            if stop_event is not None and stop_event.wait(timeout=LOOP_RECOVERY_BACKOFF_SECONDS):
                break
            elif stop_event is None:
                time.sleep(LOOP_RECOVERY_BACKOFF_SECONDS)


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
    with METRICS.time_cycle() as _c:
        decision = decide(
            predicted_rps=predicted_rps,
            current_count=current_count,
            policy=policy,
            seconds_since_last_action=seconds_since_action,
            now_text="forecast",
        )
        apply_decision(decision, cluster, db_conn, redis_client, envelope_meta.get("event_id"))
        _c["outcome"] = decision.action


def _handle_policy_message(raw) -> None:
    """Parse a smartload.policy envelope and swap _policy under the lock.

    Drops malformed messages silently — the existing on-disk policy.yaml
    is the durable source of truth, and the next valid publish will
    correct any missed reload."""
    parsed = parse_envelope(raw, channel=POLICY_CHANNEL)
    if parsed is None:
        return
    payload, envelope_meta = parsed

    with _state_lock:
        global _policy, _policy_version
        old_policy = _policy
        new_policy = policy_from_payload(payload, fallback=_policy)
        new_version = int(payload.get("policy_version", _policy_version))
        # Reject backwards version moves so out-of-order publishes can't
        # roll back the live policy.
        if new_version < _policy_version:
            log.warning(
                "ignoring policy update v%d (current v%d) — stale publish",
                new_version, _policy_version,
            )
            return
        _policy = new_policy
        _policy_version = new_version

    log.info(
        "policy reloaded v%d (event_id=%s, changed=%s): %s -> %s",
        new_version,
        envelope_meta.get("event_id"),
        payload.get("changed_fields"),
        old_policy,
        new_policy,
    )


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

# Prometheus own-metrics (#161). The autoscaler is event-driven (forecast
# messages + a reactive tick), so `cycle_*` counts each forecast-driven
# evaluation; SCALE_TOTAL is the decision distribution by direction + mechanism.
METRICS = ServiceMetrics("autoscaler")
SCALE_TOTAL = Counter(
    "autoscaler_scale_total",
    "Scaling decisions actuated, by direction and mechanism",
    ["direction", "mechanism"],
)


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


@app.route("/metrics")
def metrics():
    body, content_type = metrics_response()
    return Response(body, mimetype=content_type)


@app.route("/health")
def health():
    redis_ok, redis_err = _check_redis()
    db_ok,    db_err    = _check_timescaledb()
    errors = [e for e in [redis_err, db_err] if e]
    ok     = redis_ok and db_ok
    status = "ok" if ok else "degraded"
    code   = 200 if ok else 503  # SOT §11
    with _state_lock:
        policy_snapshot   = asdict(_policy)
        version_snapshot  = _policy_version
        last_tick         = _last_loop_tick_monotonic
    last_tick_age = None if last_tick is None else round(time.monotonic() - last_tick, 2)
    # #163 liveness check.
    loop_stale = (
        last_tick_age is not None
        and last_tick_age > LIVENESS_STALE_SECONDS
    )
    if loop_stale:
        status = "degraded"
        code = 503
        errors.append(
            f"control loop has not ticked in {last_tick_age:.0f}s "
            f"(threshold {LIVENESS_STALE_SECONDS:.0f}s)"
        )
    body = {
        "status":                       status,
        "service":                      SERVICE_NAME,
        "redis":                        redis_ok,
        "timescaledb":                  db_ok,
        "policy":                       policy_snapshot,
        "policy_version":               version_snapshot,
        "stats":                        _stats_snapshot(),
        "last_loop_tick_age_seconds":   last_tick_age,
        **({"loop_stale": True}    if loop_stale else {}),
        **({"errors":     errors}  if errors     else {}),
    }
    return jsonify(body), code


@app.route("/")
def index():
    return jsonify({"service": SERVICE_NAME, "status": "running"})


# ── audit read API (slice #2, issue #122) ─────────────────────────────────────

_AUDIT_LIMIT_DEFAULT = 50
_AUDIT_LIMIT_MAX     = 1000


@app.route("/api/v1/audit/scaling", methods=["GET"])
def get_audit_scaling():
    """Return recent scaling_events rows, newest first.

    Read-only. Mirrors the audit/policy endpoint on policy-manager so the
    SDK + UI can treat both audit streams uniformly.

      ?limit=N  — bounded by _AUDIT_LIMIT_MAX (1000). Defaults to 50.

    Response shape (per row):
      {time, action, instance_count, reason}
    """
    try:
        limit = int(request.args.get("limit", _AUDIT_LIMIT_DEFAULT))
    except (TypeError, ValueError):
        return jsonify({"error": "limit must be an integer", "field": "limit"}), 400
    if limit <= 0:
        return jsonify({"error": "limit must be > 0", "field": "limit"}), 400
    limit = min(limit, _AUDIT_LIMIT_MAX)

    try:
        conn = psycopg2.connect(TIMESCALEDB_URL, connect_timeout=5)
    except Exception as exc:                            # noqa: BLE001
        log.exception("audit DB connection failed")
        return jsonify({"error": f"audit unavailable: {exc}"}), 503

    try:
        with conn.cursor() as cur:
            cur.execute(SCALING_AUDIT_QUERY, (limit,))
            rows = cur.fetchall()
    finally:
        conn.close()

    return jsonify([
        {
            "time":           ts.isoformat() if ts is not None else None,
            "action":         action,
            "instance_count": instance_count,
            "reason":         reason,
        }
        for (ts, action, instance_count, reason) in rows
    ])


# ── manual actions: POST /api/v1/scale (slice #3, #123) ───────────────────────


@app.route("/api/v1/scale", methods=["POST"])
def post_manual_scale():
    """Manual scale to a specific target count, bypassing forecast + cooldown.

    Body:
      {
        "target_count": <int, in [min_backends, max_backends]>,
        "actor":        <string, default "operator">,
        "reason":       <string, default "manual override">
      }

    Bounds-validates the target against the live policy (rejecting outside
    [min, max] with 400). On success, scales the cluster step-by-step, writes
    a single `scaling_events` row with `reason = "manual:<actor>: <reason>"`,
    publishes one `ScalingEvent` envelope on `smartload.scale`, and bumps the
    cooldown clock so the next auto-decide tick honours the operator's
    intent.
    """
    raw = request.get_json(force=True, silent=True)
    if not isinstance(raw, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400

    actor = (raw.get("actor") or request.headers.get("X-Actor") or "operator")
    reason = raw.get("reason")

    with _state_lock:
        policy = _policy

    cluster = _make_cluster_client()
    try:
        current_count = cluster.get_backend_count()
    except Exception as exc:                            # noqa: BLE001
        log.exception("could not read backend count")
        return jsonify({"error": f"cluster query failed: {exc}"}), 503

    try:
        plan = plan_manual_scale(
            target_count=raw.get("target_count"),
            current_count=current_count,
            policy=policy,
            actor=actor,
            user_reason=reason,
        )
    except ManualScaleError as exc:
        return jsonify({"error": exc.message, "field": exc.field}), 400

    # Actuate — best-effort step-by-step. If any step fails to actuate (no
    # container to start / stop), stop early and record the partial result so
    # the audit row matches what actually happened.
    actuated_steps = 0
    last_name: str | None = None
    last_mechanism: str | None = None
    for _ in range(plan.steps):
        if plan.action == ACTION_SCALE_OUT:
            result = cluster.scale_out()
        elif plan.action == ACTION_SCALE_IN:
            result = cluster.scale_in()
        else:
            result = None
            break
        if result is None:
            log.warning(
                "manual %s reached cluster limit after %d/%d steps",
                plan.action, actuated_steps, plan.steps,
            )
            break
        last_name, last_mechanism = result
        actuated_steps += 1

    final_count = current_count + actuated_steps if plan.action == ACTION_SCALE_OUT \
        else current_count - actuated_steps if plan.action == ACTION_SCALE_IN \
        else current_count

    # Effective action — if no steps actuated (target == current OR cluster
    # rejected all steps), record as noop. Otherwise the plan's own action.
    effective_action = plan.action if actuated_steps > 0 else ACTION_NOOP

    log.info(
        "manual %s actor=%s steps=%d/%d final_count=%d reason=%r container=%s",
        effective_action, actor, actuated_steps, plan.steps, final_count,
        plan.reason, last_name,
    )

    # scaling_events: one row per operator click, regardless of step count.
    try:
        db_conn = psycopg2.connect(TIMESCALEDB_URL, connect_timeout=5)
    except Exception as exc:                            # noqa: BLE001
        log.exception("scaling_events write — DB connection failed")
        return jsonify({"error": f"audit write failed: {exc}"}), 503
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                SCALING_EVENT_INSERT,
                (
                    datetime.now(timezone.utc),
                    effective_action,
                    final_count,
                    plan.reason,
                ),
            )
        db_conn.commit()
    finally:
        db_conn.close()

    # smartload.scale: one envelope per operator click. forecast_event_id is
    # null per the issue spec — manual actions don't have a forecast origin.
    # `mechanism` carries the lifecycle path of the last actuated step (or
    # None if no step actuated — effective_action will be ACTION_NOOP in that
    # case, so analysis pipelines can join on (action != noop) ⇒ mechanism).
    redis_client = redis_lib.from_url(REDIS_URL)
    event = ScalingEvent(
        action=effective_action,
        instance_count=final_count,
        reason=plan.reason,
        forecast_event_id=None,
        mechanism=last_mechanism,
    )
    envelope = make_envelope(source=SERVICE_NAME, payload=event)
    redis_client.publish(SCALE_CHANNEL, json.dumps(asdict(envelope)))

    # Bump cooldown clock so the next forecast tick doesn't immediately undo.
    with _state_lock:
        global _last_action_monotonic
        _last_action_monotonic = time.monotonic()
    _bump_action(effective_action)

    return jsonify({
        "status":          "applied" if actuated_steps > 0 else "noop",
        "action":          effective_action,
        "target_count":    plan.target_count,
        "previous_count":  current_count,
        "final_count":     final_count,
        "steps_actuated":  actuated_steps,
        "steps_requested": plan.steps,
        "reason":          plan.reason,
        "event_id":        envelope.event_id,
    })


# ── entrypoint ────────────────────────────────────────────────────────────────

def main() -> None:
    global _policy, _policy_version
    _policy = load_policy(POLICY_PATH)
    # policy_version lives in policy.yaml and is bumped by policy-manager on
    # each successful POST. Read it at boot so out-of-order publishes can be
    # rejected (see _handle_policy_message).
    try:
        with open(POLICY_PATH, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        _policy_version = int(raw.get("policy_version", 0))
    except FileNotFoundError:
        _policy_version = 0
    log.info(
        "loaded policy from %s: %s (version=%d)",
        POLICY_PATH, _policy, _policy_version,
    )

    t = threading.Thread(target=control_loop, name="autoscaler-control-loop", daemon=True)
    t.start()

    log.info("Flask /health starting on port %d", PORT)
    app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
