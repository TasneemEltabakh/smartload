"""
services/policy-manager/app.py
────────────────────────────────
T1.4 — Policy & Configuration Manager.

Per SOT §8.9:
  - Sole writer of config/policy.yaml.
  - Sole publisher on smartload.policy.
  - Serves GET /api/v1/policy and POST /api/v1/policy.
  - Validates every POST and the YAML at startup; rejects incoherent state.
  - Writes one row per changed field to policy_changes on each successful POST.
  - Publishes a canonical PolicyUpdate envelope (SOT §11) carrying the full
    new state, policy_version, and the list of changed fields.
  - Idempotent: a POST that matches the on-disk policy is a 200 no-op
    (no audit row, no publish).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import psycopg2
import redis as redis_lib
import yaml
from flask import Flask, Response, jsonify, request

# Resolve the canonical shared/ module across two layouts:
#   container: /app/shared       (sibling of app.py — Dockerfile copies it)
#   dev / CI:  services/shared   (parent dir of services/policy-manager/app.py)
_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (_HERE, os.path.dirname(_HERE)):
    if os.path.isdir(os.path.join(_cand, "shared")):
        sys.path.insert(0, _cand)
        break

from shared.contracts import (  # noqa: E402
    PolicyUpdate,
    make_envelope,
)
from shared.metrics import ServiceMetrics, metrics_response  # noqa: E402
from shared import config                                       # noqa: E402
from shared.logging_setup import install_correlation_middleware # noqa: E402
from shared.queries import POLICY_CHANGE_INSERT  # noqa: E402

from validation import (  # noqa: E402
    PolicyValidationError,
    validate_merged_policy,
    validate_updates,
)


# ── config ────────────────────────────────────────────────────────────────────

# Env via the shared typed config helpers (v1.0.7av). Behaviour-preserving.
SERVICE_NAME    = config.env_str("SERVICE_NAME", "policy-manager")
PORT            = config.env_int("PORT", 8086)
REDIS_URL       = config.redis_url()
TIMESCALEDB_URL = config.timescaledb_url()
CONFIG_PATH     = config.env_str("CONFIG_PATH", "/config/policy.yaml")
POLICY_CHANNEL  = "smartload.policy"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [policy-manager] %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("policy-manager")

app = Flask(__name__)
# Per-request correlation IDs (#143).
install_correlation_middleware(app)

# Prometheus own-metrics (#161). policy-manager is event-driven (publishes on
# a successful POST /api/v1/policy), so only the publish_* surface is populated.
METRICS = ServiceMetrics("policy_manager")


# ── policy IO ─────────────────────────────────────────────────────────────────

def load_policy() -> dict:
    """Read the current policy from disk. Returns {} if the file is missing."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _atomic_write_yaml(path: str, policy: dict) -> None:
    """Write `policy` to `path` as atomically as the underlying filesystem
    allows.

    Preferred path: yaml.safe_dump to a temp file in the same directory, then
    os.replace onto the canonical path — the rename is atomic on POSIX and
    on Windows (Python 3.3+).

    Fallback path: when `path` is a single-file Docker bind mount, replacing
    it via rename fails with EBUSY because Docker mounts the inode itself.
    Detect that case and overwrite in place. yaml.safe_dump buffers the
    serialised text then writes once, so a partial-file read window is
    bounded by the OS write granularity — acceptable for SmartLoad's
    operator-initiated, low-rate POSTs.
    """
    parent = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".policy.", suffix=".yaml.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(policy, f, default_flow_style=False, sort_keys=True)
        try:
            os.replace(tmp, path)
        except OSError as exc:
            # EBUSY (16) = Linux/Docker bind-mount; EACCES (13) on some
            # platforms. Fall back to a direct overwrite so the bind-mount
            # case still works.
            if exc.errno not in (16, 13):
                raise
            with open(path, "w", encoding="utf-8") as f:
                yaml.safe_dump(policy, f, default_flow_style=False, sort_keys=True)
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── audit writes ──────────────────────────────────────────────────────────────

def _changed_fields(existing: dict, merged: dict) -> dict[str, tuple[Any, Any]]:
    """Return {field: (old, new)} for every field whose value changed.

    `policy_version` is excluded — it's a bookkeeping field bumped by this
    service on every change, and including it would make every POST look
    like at least one field changed (defeating idempotency).
    """
    out: dict[str, tuple[Any, Any]] = {}
    for key, new_val in merged.items():
        if key == "policy_version":
            continue
        old_val = existing.get(key)
        if old_val != new_val:
            out[key] = (old_val, new_val)
    return out


def _write_audit_rows(
    db_conn,
    diff: dict[str, tuple[Any, Any]],
    policy_version: int,
    actor: str,
) -> None:
    """Insert one row per changed field. Best-effort: log + swallow on
    failure — the audit table is observability, not consistency-critical.
    A failed audit must not roll back the policy publish."""
    now = datetime.now(timezone.utc)
    try:
        with db_conn.cursor() as cur:
            for field, (old, new) in sorted(diff.items()):
                cur.execute(
                    POLICY_CHANGE_INSERT,
                    (
                        now,
                        policy_version,
                        field,
                        None if old is None else json.dumps(old),
                        json.dumps(new),
                        actor,
                    ),
                )
        db_conn.commit()
    except Exception:
        log.exception("audit write failed (policy still persisted + published)")
        try:
            db_conn.rollback()
        except Exception:
            pass


# ── publish ───────────────────────────────────────────────────────────────────

# Subset of PolicyUpdate that must be present on every publish. If any are
# missing from the merged policy, the publish is skipped (the envelope would
# be malformed) — but the YAML write and audit still happen so the operator
# can observe + fix the on-disk state.
_REQUIRED_FOR_PUBLISH = (
    "operating_mode",
    "safe_mode",
    "min_backends",
    "max_backends",
    "slo_p95_latency_ms",
    "anomaly_latency_multiplier",
    "per_instance_capacity_rps",
    "autoscaler_cooldown_seconds",
)


def _publish_policy(
    redis_client,
    merged: dict,
    changed_field_names: list[str],
) -> str | None:
    """Publish a canonical PolicyUpdate envelope on smartload.policy.

    Returns the event_id on success, None on failure (logged). Failures here
    do not roll back the YAML write — the on-disk state is the source of
    truth, and a subsequent successful POST will republish."""
    missing = [k for k in _REQUIRED_FOR_PUBLISH if k not in merged]
    if missing:
        log.error(
            "policy missing required publish fields %s; skipping smartload.policy publish",
            missing,
        )
        return None
    try:
        update = PolicyUpdate(
            operating_mode=merged["operating_mode"],
            safe_mode=merged["safe_mode"],
            min_backends=merged["min_backends"],
            max_backends=merged["max_backends"],
            slo_p95_latency_ms=merged["slo_p95_latency_ms"],
            anomaly_latency_multiplier=merged["anomaly_latency_multiplier"],
            per_instance_capacity_rps=merged["per_instance_capacity_rps"],
            autoscaler_cooldown_seconds=merged["autoscaler_cooldown_seconds"],
            policy_version=merged["policy_version"],
            anomaly_response=merged.get("anomaly_response", "auto-isolate"),
            anomaly_recovery_window_seconds=merged.get("anomaly_recovery_window_seconds", 30),
            rl_exploration_rate=merged.get("rl_exploration_rate", 0.0),
            rl_confidence_threshold=merged.get("rl_confidence_threshold", 0.6),
            changed_fields=changed_field_names or None,
        )
        envelope = make_envelope(source=SERVICE_NAME, payload=update)
        with METRICS.time_publish(POLICY_CHANNEL):   # records outcome=error on raise
            redis_client.publish(POLICY_CHANNEL, json.dumps(asdict(envelope)))
        return envelope.event_id
    except Exception:
        log.exception("smartload.policy publish failed")
        return None


# ── startup validation ────────────────────────────────────────────────────────

def validate_at_startup() -> None:
    """Validate the on-disk policy at service boot. Logs a warning and
    proceeds for missing files (treated as empty, defaults apply). Logs
    an error for invalid values but does NOT crash — operators can fix
    via the running REST endpoint."""
    policy = load_policy()
    if not policy:
        log.warning("policy file %s is missing or empty — POST to populate", CONFIG_PATH)
        return
    try:
        validate_merged_policy(policy)
        log.info("startup policy validation OK (%d fields)", len(policy))
    except PolicyValidationError as exc:
        log.error(
            "startup policy validation FAILED: %s (field=%s). "
            "Service remains up; operator should POST a corrected policy.",
            exc, exc.field,
        )


# ── HTTP handlers ─────────────────────────────────────────────────────────────

def _check_redis() -> tuple[bool, str | None]:
    try:
        r = redis_lib.from_url(REDIS_URL, socket_connect_timeout=3)
        r.ping()
        return True, None
    except Exception as exc:
        return False, str(exc)


def _check_timescaledb() -> tuple[bool, str | None]:
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
    db_ok, db_err = _check_timescaledb()
    policy = load_policy()
    errors = [e for e in [redis_err, db_err] if e]
    ok = redis_ok and db_ok
    status = "ok" if ok else "degraded"
    code = 200 if ok else 503   # SOT §11: 503 on degraded, never 207
    return jsonify({
        "status": status,
        "service": SERVICE_NAME,
        "redis": redis_ok,
        "timescaledb": db_ok,
        "policy_loaded": bool(policy),
        "policy_version": policy.get("policy_version", 0),
        **({"errors": errors} if errors else {}),
    }), code


@app.route("/api/v1/policy", methods=["GET"])
def get_policy():
    policy = load_policy()
    if not policy:
        return jsonify({"error": f"policy file not found or empty: {CONFIG_PATH}"}), 404
    return jsonify(policy)


_AUDIT_QUERY = """
SELECT time, policy_version, field, old_value, new_value, actor
FROM policy_changes
ORDER BY time DESC
LIMIT %s;
"""

_AUDIT_LIMIT_DEFAULT = 50
_AUDIT_LIMIT_MAX     = 1000


@app.route("/api/v1/audit/policy", methods=["GET"])
def get_audit_policy():
    """Return recent policy_changes rows, newest first.

    Read-only. The audit table is observability, never a write surface
    from this endpoint.

      ?limit=N  — bounded by _AUDIT_LIMIT_MAX (1000). Defaults to 50.

    old_value / new_value are stored as JSON text in the hypertable
    (json.dumps at write time) and are JSON-decoded here so callers receive
    native JSON values rather than escaped strings.
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
    except Exception as exc:
        log.exception("audit DB connection failed")
        return jsonify({"error": f"audit unavailable: {exc}"}), 503

    try:
        with conn.cursor() as cur:
            cur.execute(_AUDIT_QUERY, (limit,))
            rows = cur.fetchall()
    finally:
        conn.close()

    def _maybe_json(raw):
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw

    return jsonify([
        {
            "time":           ts.isoformat() if ts is not None else None,
            "policy_version": policy_version,
            "field":          field_name,
            "old_value":      _maybe_json(old),
            "new_value":      _maybe_json(new),
            "actor":          actor,
        }
        for (ts, policy_version, field_name, old, new, actor) in rows
    ])


@app.route("/api/v1/policy", methods=["POST"])
def update_policy():
    """Update one or more policy fields atomically.

    Flow:
      1. Parse + validate the body (rejects 400 with field name on bad input).
      2. Merge with on-disk policy. Re-validate cross-field invariants.
      3. If the merge produces no field changes, return 200 no-op (no write,
         no audit, no publish — operators may safely retry POSTs).
      4. Bump policy_version (monotonic).
      5. Atomic YAML write.
      6. Write one row per changed field to policy_changes.
      7. Publish a PolicyUpdate envelope on smartload.policy.
    """
    raw = request.get_json(force=True, silent=True)
    if raw is None:
        return jsonify({"error": "request body must be valid JSON"}), 400
    if not isinstance(raw, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400

    existing = load_policy()
    try:
        merged_no_version = validate_updates(raw, existing)
    except PolicyValidationError as exc:
        return jsonify({"error": str(exc), "field": exc.field}), 400

    diff = _changed_fields(existing, merged_no_version)
    if not diff:
        # Idempotent: a POST that matches the on-disk state changes nothing.
        # Return 200 + the existing snapshot so the caller can confirm state.
        return jsonify({
            "status": "no-op",
            "policy": existing,
            "changed_fields": [],
        }), 200

    new_version = int(existing.get("policy_version", 0)) + 1
    merged = {**merged_no_version, "policy_version": new_version}

    try:
        _atomic_write_yaml(CONFIG_PATH, merged)
    except Exception as exc:
        log.exception("failed to persist policy")
        return jsonify({"error": f"failed to persist policy: {exc}"}), 500

    changed_field_names = sorted(diff.keys())
    actor = request.headers.get("X-Actor", "anonymous")

    # Audit write — best-effort, never rolls back the publish below.
    try:
        db_conn = psycopg2.connect(TIMESCALEDB_URL, connect_timeout=5)
        try:
            _write_audit_rows(db_conn, diff, new_version, actor)
        finally:
            db_conn.close()
    except Exception:
        log.exception("audit DB connection failed (policy still persisted + published)")

    # Publish — best-effort, errors logged, on-disk state remains authoritative.
    event_id = None
    try:
        r = redis_lib.from_url(REDIS_URL, socket_connect_timeout=3)
        event_id = _publish_policy(r, merged, changed_field_names)
    except Exception:
        log.exception("redis connection failed; smartload.policy publish skipped")

    return jsonify({
        "status": "updated",
        "policy": merged,
        "changed_fields": changed_field_names,
        "policy_version": new_version,
        "event_id": event_id,
    }), 200


@app.route("/")
def index():
    return jsonify({"service": SERVICE_NAME, "status": "running"})


if __name__ == "__main__":
    log.info("starting on port %d (CONFIG_PATH=%s)", PORT, CONFIG_PATH)
    validate_at_startup()
    # Production WSGI server (Flask's app.run dev server is single-threaded).
    from waitress import serve
    serve(app, host="0.0.0.0", port=PORT, threads=8)
