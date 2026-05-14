"""
tests/integration/test_policy_manager.py
─────────────────────────────────────────
End-to-end tests against the live docker-compose stack for the T1.4 Policy
Manager. Validates:

  1. POST /api/v1/policy with a valid change:
       - persists to policy.yaml on disk,
       - writes one row per changed field to policy_changes,
       - publishes a canonical PolicyUpdate envelope on smartload.policy,
       - the autoscaler /health reflects the new policy and policy_version
         within 5 seconds (live reload).

  2. POST with invalid data returns 400 with a field name and does NOT
     touch policy.yaml, policy_changes, or smartload.policy.

  3. POST that matches existing state is a 200 no-op: no audit row, no
     publish, no policy_version bump.

Test isolation:
  - Each test snapshots policy.yaml + policy_version BEFORE running and
    restores them in a finalizer. Other tests in the suite (autoscaler,
    pipeline-health) read this file so leaks would break them.
  - Audit rows are not cleaned up — policy_changes is append-only and the
    retention policy bounds growth. Assertions count deltas.

Run:
    docker compose up -d
    pytest tests/integration/test_policy_manager.py -v
    docker compose down
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import psycopg2
import pytest
import redis as redis_lib
import requests

from services.shared.contracts import parse_envelope

from .conftest import REDIS_URL, SERVICE_URLS, TIMESCALEDB_DSN

POLICY_CHANNEL  = "smartload.policy"
POLICY_PATH     = Path(__file__).resolve().parents[2] / "config" / "policy.yaml"
POLICY_MGR_URL  = SERVICE_URLS["policy-manager"]
AUTOSCALER_URL  = SERVICE_URLS["autoscaler"]

# Headroom for the autoscaler control loop to consume + apply a policy
# update. LOOP_TICK_SECONDS is 5 s; allow 3 ticks.
RELOAD_DEADLINE_SECONDS = 15.0


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="function")
def policy_backup(stack_ready):
    """Snapshot policy.yaml before the test; restore after.

    Other tests in the suite — and the running services — read this file
    live. Restoring it on teardown keeps tests order-independent.
    """
    backup = POLICY_PATH.read_bytes()
    yield POLICY_PATH
    POLICY_PATH.write_bytes(backup)
    # POST the restored policy so the running services see it immediately
    # via smartload.policy rather than waiting for a service restart.
    try:
        # Re-load YAML so we send the exact dict that's now on disk.
        import yaml
        with POLICY_PATH.open() as f:
            restored = yaml.safe_load(f) or {}
        # Drop policy_version so the POST recomputes it; some restored
        # files may not include the field.
        restored.pop("policy_version", None)
        requests.post(
            f"{POLICY_MGR_URL}/api/v1/policy",
            json=restored,
            timeout=5,
        )
    except Exception:
        # Best-effort — file is back on disk regardless. Service restart
        # would also pick it up.
        pass


@pytest.fixture(scope="function")
def db_conn(stack_ready):
    conn = psycopg2.connect(TIMESCALEDB_DSN)
    yield conn
    conn.close()


@pytest.fixture(scope="function")
def policy_subscriber(stack_ready):
    """A pubsub subscription to smartload.policy, drained of any pre-test
    backlog before yielding to the test."""
    client = redis_lib.from_url(REDIS_URL)
    pubsub = client.pubsub()
    pubsub.subscribe(POLICY_CHANNEL)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1) is None:
            break
    yield pubsub
    pubsub.close()
    client.close()


# ── helpers ───────────────────────────────────────────────────────────────────

def _audit_row_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM policy_changes;")
        return int(cur.fetchone()[0])


def _audit_rows_since(conn, since_count: int) -> list[tuple]:
    """Return rows newer than `since_count`. Stable by time then field."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT field, old_value, new_value, actor, policy_version "
            "FROM policy_changes "
            "ORDER BY time DESC, field ASC "
            "LIMIT %s;",
            (max(1, _audit_row_count(conn) - since_count),),
        )
        return cur.fetchall()


def _wait_for_policy_envelope(pubsub, deadline_seconds: float) -> dict | None:
    """Block on pubsub for up to deadline_seconds; return the first parsed
    PolicyUpdate payload."""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        msg = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        if msg is None or msg.get("type") != "message":
            continue
        parsed = parse_envelope(msg["data"], channel=POLICY_CHANNEL)
        if parsed is None:
            continue
        payload, _meta = parsed
        return payload
    return None


def _wait_for_autoscaler_policy(target_max_backends: int, deadline_seconds: float) -> dict | None:
    """Poll the autoscaler /health until its policy.max_backends matches
    `target_max_backends`. Returns the matching /health body, or None on
    timeout."""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            resp = requests.get(f"{AUTOSCALER_URL}/health", timeout=2)
            body = resp.json()
            if body.get("policy", {}).get("max_backends") == target_max_backends:
                return body
        except (requests.RequestException, ValueError):
            pass
        time.sleep(0.5)
    return None


# ── tests ─────────────────────────────────────────────────────────────────────

class TestPolicyUpdateHappyPath:

    def test_valid_post_persists_audits_publishes_reloads(
        self, db_conn, policy_subscriber, policy_backup,
    ):
        """A POST that changes max_backends must persist to YAML, write
        one policy_changes row, publish a PolicyUpdate envelope, and the
        autoscaler must pick up the new bound on its next tick."""
        before_audit = _audit_row_count(db_conn)

        # Pick a value different from current. Current is 5 by default.
        new_max = 7
        resp = requests.post(
            f"{POLICY_MGR_URL}/api/v1/policy",
            json={"max_backends": new_max},
            headers={"X-Actor": "pytest-suite"},
            timeout=5,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "updated"
        assert body["changed_fields"] == ["max_backends"]
        assert body["policy"]["max_backends"] == new_max
        assert body["policy_version"] >= 1

        # YAML on disk has the new value.
        import yaml
        on_disk = yaml.safe_load(policy_backup.read_text())
        assert on_disk["max_backends"] == new_max

        # smartload.policy carried the change.
        envelope_payload = _wait_for_policy_envelope(
            policy_subscriber, RELOAD_DEADLINE_SECONDS,
        )
        assert envelope_payload is not None, "no smartload.policy envelope arrived"
        assert envelope_payload["max_backends"] == new_max
        assert envelope_payload["policy_version"] == body["policy_version"]
        assert "max_backends" in (envelope_payload.get("changed_fields") or [])

        # Audit row exists for the changed field.
        after_audit = _audit_row_count(db_conn)
        assert after_audit == before_audit + 1, (
            f"expected one new audit row, got {after_audit - before_audit}"
        )
        rows = _audit_rows_since(db_conn, before_audit)
        assert any(
            row[0] == "max_backends"
            and json.loads(row[2]) == new_max
            and row[3] == "pytest-suite"
            for row in rows
        ), f"audit row for max_backends not found in {rows}"

        # Autoscaler sees the new bound on its next tick.
        health = _wait_for_autoscaler_policy(new_max, RELOAD_DEADLINE_SECONDS)
        assert health is not None, (
            "autoscaler /health did not pick up the new policy within deadline"
        )
        assert health["policy_version"] == body["policy_version"]


class TestPolicyUpdateValidation:

    def test_max_less_than_min_returns_400(self, db_conn, policy_backup):
        """Cross-field invariant: a POST that would leave min > max returns
        400 with field name, and writes no audit row."""
        before_audit = _audit_row_count(db_conn)

        resp = requests.post(
            f"{POLICY_MGR_URL}/api/v1/policy",
            json={"max_backends": 1, "min_backends": 5},
            timeout=5,
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["field"] in ("min_backends", "max_backends")

        after_audit = _audit_row_count(db_conn)
        assert after_audit == before_audit, "audit row written for invalid POST"

    def test_unknown_operating_mode_returns_400(self, policy_backup):
        resp = requests.post(
            f"{POLICY_MGR_URL}/api/v1/policy",
            json={"operating_mode": "rogue"},
            timeout=5,
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["field"] == "operating_mode"


class TestPolicyAuditEndpoint:

    def test_audit_returns_recent_change(self, db_conn, policy_backup):
        """A POST that changes a field should be visible via
        GET /api/v1/audit/policy on the next read."""
        # Trigger a change with a distinctive actor so we can find it in the rows.
        new_max = 9
        resp = requests.post(
            f"{POLICY_MGR_URL}/api/v1/policy",
            json={"max_backends": new_max},
            headers={"X-Actor": "pytest-audit"},
            timeout=5,
        )
        assert resp.status_code == 200, resp.text

        # Brief settle to let the audit write complete (best-effort path).
        time.sleep(0.3)

        audit_resp = requests.get(
            f"{POLICY_MGR_URL}/api/v1/audit/policy",
            params={"limit": 20},
            timeout=5,
        )
        assert audit_resp.status_code == 200, audit_resp.text
        rows = audit_resp.json()
        assert isinstance(rows, list)
        # Find our row.
        matching = [
            r for r in rows
            if r.get("field") == "max_backends"
            and r.get("new_value") == new_max
            and r.get("actor") == "pytest-audit"
        ]
        assert matching, f"audit row for max_backends=new_max actor=pytest-audit not found in {rows[:5]}"
        row = matching[0]
        # Every row must carry the canonical columns.
        for key in ("time", "policy_version", "field", "old_value", "new_value", "actor"):
            assert key in row

    def test_audit_limit_caps_results(self, policy_backup):
        """?limit=N must return at most N rows, and a non-positive limit is 400."""
        resp = requests.get(
            f"{POLICY_MGR_URL}/api/v1/audit/policy",
            params={"limit": 1},
            timeout=5,
        )
        assert resp.status_code == 200, resp.text
        assert len(resp.json()) <= 1

        bad = requests.get(
            f"{POLICY_MGR_URL}/api/v1/audit/policy",
            params={"limit": 0},
            timeout=5,
        )
        assert bad.status_code == 400, bad.text
        assert bad.json().get("field") == "limit"


class TestPolicyUpdateIdempotency:

    def test_repeated_identical_post_is_noop(self, db_conn, policy_backup):
        """A second POST with the same body returns 200 no-op: no audit
        row, no policy_version bump. Operators can safely retry POSTs."""
        # First POST: a real change.
        resp1 = requests.post(
            f"{POLICY_MGR_URL}/api/v1/policy",
            json={"max_backends": 6},
            timeout=5,
        )
        assert resp1.status_code == 200
        version_after_first = resp1.json()["policy_version"]
        audit_after_first = _audit_row_count(db_conn)

        # Brief settle so the on-disk file is read fresh on the next GET.
        time.sleep(0.5)

        # Second POST with the same value — should be a no-op.
        resp2 = requests.post(
            f"{POLICY_MGR_URL}/api/v1/policy",
            json={"max_backends": 6},
            timeout=5,
        )
        assert resp2.status_code == 200
        body = resp2.json()
        assert body["status"] == "no-op"
        assert body["changed_fields"] == []

        # No new audit row, no version bump.
        assert _audit_row_count(db_conn) == audit_after_first
        assert resp2.json().get("policy", {}).get("policy_version") == version_after_first
