"""
services/policy-manager/app.py
────────────────────────────────
Phase 0 stub — wired to Redis, reads policy.yaml, reports connectivity on /health.
Phase 1 (T1.4): implement GET /api/v1/policy and POST /api/v1/policy with
                 YAML persistence and smartload.policy Redis publish.
"""

import os

import redis as redis_lib
import yaml
from flask import Flask, jsonify, request

app = Flask(__name__)

SERVICE_NAME = os.environ.get("SERVICE_NAME", "policy-manager")
PORT = int(os.environ.get("PORT", "8086"))
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/policy.yaml")


def check_redis():
    try:
        r = redis_lib.from_url(REDIS_URL, socket_connect_timeout=3)
        r.ping()
        return True, None
    except Exception as exc:
        return False, str(exc)


def load_policy() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


@app.route("/health")
def health():
    redis_ok, redis_err = check_redis()
    policy = load_policy()
    errors = [e for e in [redis_err] if e]
    status = "ok" if redis_ok else "degraded"
    code = 200 if status == "ok" else 207
    return jsonify(
        {
            "status": status,
            "service": SERVICE_NAME,
            "redis": redis_ok,
            "timescaledb": None,  # policy-manager does not use TimescaleDB
            "policy_loaded": bool(policy),
            **({"errors": errors} if errors else {}),
        }
    ), code


@app.route("/api/v1/policy", methods=["GET"])
def get_policy():
    policy = load_policy()
    if not policy:
        return jsonify({"error": f"policy file not found or empty: {CONFIG_PATH}"}), 404
    return jsonify(policy)


@app.route("/api/v1/policy", methods=["POST"])
def update_policy():
    """
    Update one or more policy fields.
    Persists change to CONFIG_PATH and publishes update to smartload.policy.
    Phase 1 (T1.4) will complete this implementation.
    """
    updates = request.get_json(force=True, silent=True)
    if not updates:
        return jsonify({"error": "request body must be JSON"}), 400

    policy = load_policy()
    policy.update(updates)

    try:
        with open(CONFIG_PATH, "w") as f:
            yaml.safe_dump(policy, f)
    except Exception as exc:
        return jsonify({"error": f"failed to persist policy: {exc}"}), 500

    # Publish update to control bus
    try:
        r = redis_lib.from_url(REDIS_URL, socket_connect_timeout=3)
        import json
        r.publish("smartload.policy", json.dumps(policy))
    except Exception:
        pass  # non-fatal at stub phase

    return jsonify({"status": "updated", "policy": policy})


@app.route("/")
def index():
    return jsonify({"service": SERVICE_NAME, "status": "running"})


if __name__ == "__main__":
    print(f"[{SERVICE_NAME}] starting on port {PORT}")
    app.run(host="0.0.0.0", port=PORT)
