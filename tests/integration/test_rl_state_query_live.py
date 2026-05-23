"""
tests/integration/test_rl_state_query_live.py
──────────────────────────────────────────────
Live-DB semantic verification for RL_STATE_QUERY.

Requires TimescaleDB to be running:
    docker compose up -d timescaledb
    pytest tests/integration/test_rl_state_query_live.py -v

These tests seed synthetic rows, run RL_STATE_QUERY, and verify the resulting
classify_health output. They are skipped automatically when the DB is
unreachable — no import side-effects.

Critical regression test (MAX→AVG fix):
  The old RL_STATE_QUERY used MAX(error_rate). Because lb-otel-shipper emits
  1.0 per errored request and 0.0 per success, MAX returns 1.0 whenever any
  single request errors in the window — even if 29 out of 30 requests succeeded
  (true error rate 3.3%, well below the 5% UNHEALTHY_ERROR_RATE threshold).
  The fix changes this to AVG, which yields the true fraction. The test below
  seeds exactly this case and asserts the correct "healthy" classification.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ── path bootstrap (mirrors tests/unit/rl-engine/test_runloop.py) ────────────
_RL_ENGINE = Path(__file__).resolve().parents[2] / "services" / "rl-engine"
if str(_RL_ENGINE) not in sys.path:
    sys.path.insert(0, str(_RL_ENGINE))

_SHARED = Path(__file__).resolve().parents[2] / "services"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

# ── availability guard ────────────────────────────────────────────────────────

def _can_reach_db() -> bool:
    try:
        import psycopg2
        from tests.integration.conftest import TIMESCALEDB_DSN
        conn = psycopg2.connect(TIMESCALEDB_DSN, connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


pytestmark_db = pytest.mark.skipif(
    not _can_reach_db(),
    reason="TimescaleDB not reachable; run `docker compose up -d timescaledb` first",
)

# ── constants (imported lazily inside tests to avoid hard import failures) ───

_BACKEND_ID = "__preflight_avg_fix__"
_TIMESCALEDB_DSN = "host=localhost port=5432 dbname=smartloaddb user=postgres password=changeme"


# ── helpers ───────────────────────────────────────────────────────────────────

def _seed_and_query(n_errors: int, n_successes: int, latency_ms: float):
    """Insert metrics for _BACKEND_ID, run RL_STATE_QUERY, clean up, return rows."""
    import psycopg2
    import psycopg2.extras
    from shared.queries import METRICS_INSERT, RL_STATE_QUERY, RL_DEFAULT_SERVICE

    now = datetime.now(timezone.utc)
    rows: list[tuple] = []

    for _ in range(n_errors):
        rows.append((now, "load-balancer", _BACKEND_ID, "error_rate", 1.0))
        rows.append((now, "load-balancer", _BACKEND_ID, "request_count", 1.0))
    for _ in range(n_successes):
        rows.append((now, "load-balancer", _BACKEND_ID, "error_rate", 0.0))
        rows.append((now, "load-balancer", _BACKEND_ID, "request_count", 1.0))
    rows.append((now, "load-balancer", _BACKEND_ID, "request_latency_ms", latency_ms))

    with psycopg2.connect(_TIMESCALEDB_DSN) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, METRICS_INSERT, rows)
            cur.execute(RL_STATE_QUERY, ("10 seconds", RL_DEFAULT_SERVICE))
            db_rows = cur.fetchall()
            cur.execute("DELETE FROM metrics WHERE instance = %s", (_BACKEND_ID,))

    return [r for r in db_rows if r[0] == _BACKEND_ID]


# ── tests ─────────────────────────────────────────────────────────────────────

@pytestmark_db
class TestRLStateQuerySeeded:
    """Semantic correctness of RL_STATE_QUERY against live TimescaleDB rows."""

    def test_service_filter_returns_only_load_balancer_rows(self):
        """Rows inserted with service='load-balancer' must be returned;
        rows with a different service must not appear."""
        import psycopg2
        import psycopg2.extras
        from shared.queries import METRICS_INSERT, RL_STATE_QUERY, RL_DEFAULT_SERVICE

        now = datetime.now(timezone.utc)
        lb_backend   = "__preflight_lb_service__"
        other_backend = "__preflight_other_service__"

        rows = [
            (now, "load-balancer", lb_backend,    "error_rate",        0.0),
            (now, "load-balancer", lb_backend,    "request_count",     1.0),
            (now, "load-balancer", lb_backend,    "request_latency_ms", 50.0),
            (now, "other-service", other_backend, "error_rate",        0.0),
            (now, "other-service", other_backend, "request_count",     1.0),
            (now, "other-service", other_backend, "request_latency_ms", 50.0),
        ]

        with psycopg2.connect(_TIMESCALEDB_DSN) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur, METRICS_INSERT, rows)
                cur.execute(RL_STATE_QUERY, ("10 seconds", RL_DEFAULT_SERVICE))
                db_rows = cur.fetchall()
                cur.execute(
                    "DELETE FROM metrics WHERE instance IN (%s, %s)",
                    (lb_backend, other_backend),
                )

        instance_names = [r[0] for r in db_rows]
        assert lb_backend in instance_names, (
            "load-balancer rows not returned by RL_STATE_QUERY with service filter"
        )
        assert other_backend not in instance_names, (
            "other-service rows leaked through RL_STATE_QUERY service filter"
        )

    def test_avg_fix_low_error_rate_backend_classified_healthy(self):
        """Core MAX→AVG regression: 1 error in 30 requests (3.3% true rate).

        With the old MAX aggregation:
          MAX(1.0, 0.0, 0.0, ...) = 1.0  → classify_health → "unhealthy" (WRONG)

        With the fixed AVG aggregation:
          AVG(1.0, 0.0, ...) ≈ 0.033     → classify_health → "healthy"   (CORRECT)

        This is the semantically meaningful check that the SQL-string assertions
        in test_s2_baseline.py cannot perform.
        """
        from runloop import build_state_from_rows, classify_health, UNHEALTHY_ERROR_RATE

        # 1 error + 29 successes: true error rate = 3.3%, below the 5% threshold
        our_rows = _seed_and_query(n_errors=1, n_successes=29, latency_ms=50.0)

        assert our_rows, (
            f"RL_STATE_QUERY returned no rows for backend {_BACKEND_ID!r}; "
            "check that service='load-balancer' rows are present"
        )

        _instance, latency, _req_count, error_rate = our_rows[0]

        # Numeric check: AVG must give the true fraction, not 1.0
        assert error_rate is not None, "error_rate column is NULL in query result"
        assert error_rate < 1.0, (
            f"error_rate={error_rate:.4f} — looks like MAX (1.0) not AVG. "
            "Check that RL_STATE_QUERY uses AVG for error_rate."
        )
        assert abs(error_rate - 1 / 30) < 0.01, (
            f"error_rate={error_rate:.4f}, expected ~{1/30:.4f} (1/30). "
            "AVG aggregation may be incorrect."
        )
        assert error_rate < UNHEALTHY_ERROR_RATE, (
            f"error_rate={error_rate:.4f} must be below UNHEALTHY_ERROR_RATE="
            f"{UNHEALTHY_ERROR_RATE} for 1/30 requests"
        )

        # classify_health check: must NOT be unhealthy
        state = build_state_from_rows(our_rows)
        assert state, "build_state_from_rows returned empty list"
        health = state[0].health
        assert health != "unhealthy", (
            f"classify_health={health!r} for 1/30 error rate "
            f"(error_rate={error_rate:.4f}). "
            "With the old MAX aggregation this would return 1.0→'unhealthy'. "
            "With AVG it should be 'healthy'."
        )
        assert health in ("healthy", "degraded"), (
            f"Unexpected health classification: {health!r}"
        )

    def test_high_error_rate_backend_correctly_classified_unhealthy(self):
        """Verify that genuinely unhealthy backends are still detected.

        5 errors in 10 requests = 50% error rate — well above the 5% threshold.
        Both AVG and MAX would return > 0.05 here; this test confirms the fix
        did not break genuine unhealthy detection.
        """
        from runloop import build_state_from_rows, UNHEALTHY_ERROR_RATE

        our_rows = _seed_and_query(n_errors=5, n_successes=5, latency_ms=50.0)
        assert our_rows, f"RL_STATE_QUERY returned no rows for {_BACKEND_ID!r}"

        _instance, _latency, _req_count, error_rate = our_rows[0]
        assert error_rate is not None
        assert error_rate > UNHEALTHY_ERROR_RATE, (
            f"error_rate={error_rate:.4f} should be above {UNHEALTHY_ERROR_RATE} "
            "for 5/10 requests"
        )

        state = build_state_from_rows(our_rows)
        assert state[0].health == "unhealthy", (
            f"Expected 'unhealthy' for 50% error rate, got {state[0].health!r}"
        )

    def test_query_returns_correct_column_count_and_types(self):
        """RL_STATE_QUERY must return exactly 4 columns in the expected order:
        (instance: str, latency: float|None, request_count: float|None, error_rate: float|None).
        build_state_from_rows depends on this positional contract."""
        our_rows = _seed_and_query(n_errors=0, n_successes=5, latency_ms=120.0)
        assert our_rows, f"No rows returned for {_BACKEND_ID!r}"
        row = our_rows[0]
        assert len(row) == 4, f"Expected 4 columns, got {len(row)}: {row}"
        instance, latency, request_count, error_rate = row
        assert isinstance(instance, str)
        assert latency is None or isinstance(latency, float)
        assert request_count is None or isinstance(request_count, (int, float))
        assert error_rate is None or isinstance(error_rate, float)
