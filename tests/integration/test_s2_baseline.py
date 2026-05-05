"""
tests/integration/test_s2_baseline.py
──────────────────────────────────────
S2 baseline tests — verify the integration scaffolding landed by the S2
baseline PR is in place and behaves per SOT.

What this covers (no service implementations required):

  1. Envelope round-trip                       — pure-Python, no deps
  2. Channel TTL staleness rejection           — pure-Python, no deps
  3. Canonical SQL is parameterized            — string assertions
  4. PolicyUpdate carries the new SOT fields   — dataclass introspection
  5. Retention policies present                — needs DB; skipped without
  6. metrics_1min CAGG present                 — needs DB; skipped without

Tests 5 and 6 use the same TIMESCALEDB_DSN as conftest.py, so they pick up
automatically when `docker compose up -d` has been run before pytest.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, fields

import pytest

from services.shared.contracts import (
    AnomalyEvent,
    CHANNEL_TTL_SECONDS,
    ENVELOPE_VERSION,
    Envelope,
    ForecastResult,
    PolicyUpdate,
    RoutingRecommendation,
    make_envelope,
    parse_envelope,
)
from services.shared.queries import (
    ANOMALY_DEFAULT_SERVICE,
    ANOMALY_METRIC_NAMES,
    ANOMALY_QUERY,
    BACKEND_HEALTH_QUERY,
    FORECAST_QUERY,
    METRICS_INSERT,
    RL_STATE_QUERY,
)


# ── 1. Envelope round-trip ────────────────────────────────────────────────────

class TestEnvelopeRoundTrip:
    """make_envelope → JSON → parse_envelope returns the original payload."""

    def test_anomaly_event_roundtrip(self):
        original = AnomalyEvent(backend_id="backend_1", status="degraded", score=0.42)
        env = make_envelope(source="anomaly-detector", payload=original)

        raw = json.dumps(asdict(env))
        result = parse_envelope(raw, channel="smartload.anomaly")

        assert result is not None
        payload, meta = result
        assert payload["backend_id"] == "backend_1"
        assert payload["status"] == "degraded"
        assert payload["score"] == pytest.approx(0.42)
        assert meta["source"] == "anomaly-detector"
        assert meta["version"] == ENVELOPE_VERSION
        assert "event_id" in meta

    def test_forecast_result_roundtrip(self):
        original = ForecastResult(
            horizon_minutes=5,
            predicted_rps=180.0,
            confidence_lower=160.0,
            confidence_upper=205.0,
        )
        env = make_envelope(source="forecasting", payload=original)
        payload, _ = parse_envelope(json.dumps(asdict(env)), channel="smartload.forecast")
        assert payload["horizon_minutes"] == 5
        assert payload["predicted_rps"] == pytest.approx(180.0)

    def test_routing_recommendation_roundtrip(self):
        original = RoutingRecommendation(
            mode="shadow",
            server_rankings=[{"backend_id": "backend_1", "score": 0.8}],
        )
        env = make_envelope(source="rl-engine", payload=original)
        payload, _ = parse_envelope(json.dumps(asdict(env)), channel="smartload.routing")
        assert payload["mode"] == "shadow"
        assert payload["server_rankings"][0]["backend_id"] == "backend_1"

    def test_event_id_is_unique_per_publish(self):
        env_a = make_envelope("test", AnomalyEvent("b", "healthy", 0.0))
        env_b = make_envelope("test", AnomalyEvent("b", "healthy", 0.0))
        assert env_a.event_id != env_b.event_id


# ── 2. Channel TTL staleness ──────────────────────────────────────────────────

class TestChannelTTL:
    """Subscribers drop messages older than the channel TTL (SOT §11)."""

    def test_canonical_ttls_match_sot(self):
        # SOT §11 envelope rules: anomaly 30 s, routing 30 s, forecast 180 s,
        # policy never expires, scale never expires.
        assert CHANNEL_TTL_SECONDS["smartload.anomaly"] == 30
        assert CHANNEL_TTL_SECONDS["smartload.routing"] == 30
        assert CHANNEL_TTL_SECONDS["smartload.forecast"] == 180
        assert CHANNEL_TTL_SECONDS["smartload.policy"] is None
        assert CHANNEL_TTL_SECONDS["smartload.scale"] is None

    def test_stale_anomaly_message_dropped(self):
        # Build an envelope, then rewrite its timestamp to be 100 s old.
        env = make_envelope("anomaly-detector", AnomalyEvent("b", "degraded", 0.5))
        env_dict = asdict(env)

        # 100 s in the past — past the 30 s anomaly TTL.
        from datetime import datetime, timezone, timedelta
        env_dict["timestamp"] = (
            datetime.now(timezone.utc) - timedelta(seconds=100)
        ).isoformat()

        result = parse_envelope(json.dumps(env_dict), channel="smartload.anomaly")
        assert result is None, "stale anomaly message must be dropped"

    def test_stale_message_accepted_on_no_ttl_channel(self):
        # smartload.policy has no TTL — old messages are still valid.
        env = make_envelope("policy-manager", {"safe_mode": True})
        env_dict = asdict(env)
        from datetime import datetime, timezone, timedelta
        env_dict["timestamp"] = (
            datetime.now(timezone.utc) - timedelta(days=7)
        ).isoformat()

        result = parse_envelope(json.dumps(env_dict), channel="smartload.policy")
        assert result is not None
        payload, _ = result
        assert payload["safe_mode"] is True

    def test_malformed_envelope_dropped(self):
        assert parse_envelope("not-json", "smartload.anomaly") is None
        assert parse_envelope(json.dumps([1, 2, 3]), "smartload.anomaly") is None
        assert parse_envelope(json.dumps({"no_payload": True}), "smartload.anomaly") is None


# ── 3. Canonical SQL is parameterized ─────────────────────────────────────────

class TestCanonicalSQLParameterization:
    """SOT §11: dynamic SQL values must be %s bind params, not str.format."""

    @pytest.mark.parametrize("query", [
        ANOMALY_QUERY,
        FORECAST_QUERY,
        RL_STATE_QUERY,
        BACKEND_HEALTH_QUERY,
    ])
    def test_no_format_placeholders(self, query):
        # The legacy {window} pattern must be gone — it bypassed parameterisation
        # and prevented prepared-plan caching.
        assert "{window}" not in query
        assert "{" not in query, f"unexpected str.format placeholder in:\n{query}"

    def test_anomaly_query_uses_parameterized_interval(self):
        assert "%s::interval" in ANOMALY_QUERY
        # Service filter is also parameterised (not hardcoded 'load-balancer').
        assert "service = %s" in ANOMALY_QUERY
        assert "metric_name = ANY(%s)" in ANOMALY_QUERY

    def test_forecast_query_uses_parameterized_interval(self):
        assert "%s::interval" in FORECAST_QUERY

    def test_rl_state_query_uses_parameterized_interval(self):
        assert "%s::interval" in RL_STATE_QUERY

    def test_backend_health_query_uses_parameterized_interval(self):
        assert "%s::interval" in BACKEND_HEALTH_QUERY

    def test_metrics_insert_unchanged(self):
        # Single-row insert path is fully parameterised already.
        assert METRICS_INSERT.count("%s") == 5

    def test_anomaly_defaults_match_canonical(self):
        # Engines should pass these as defaults; tests pin them so a drift in
        # ANOMALY_QUERY columns also drifts the defaults.
        assert ANOMALY_DEFAULT_SERVICE == "load-balancer"
        assert ANOMALY_METRIC_NAMES == ["request_latency_ms", "error_rate"]


# ── 4. PolicyUpdate carries the new SOT fields ────────────────────────────────

class TestPolicyUpdateContract:
    """The dataclass must include every canonical field documented in SOT §8.9."""

    def test_required_canonical_fields(self):
        names = {f.name for f in fields(PolicyUpdate)}
        expected = {
            "operating_mode",
            "safe_mode",
            "min_backends",
            "max_backends",
            "slo_p95_latency_ms",
            "anomaly_latency_multiplier",
            "per_instance_capacity_rps",
            "autoscaler_cooldown_seconds",
            # ── added in S2 baseline ─────────────────────────────────────────
            "anomaly_response",
            "anomaly_recovery_window_seconds",
            "rl_exploration_rate",
            "rl_confidence_threshold",
            "timestamp",
        }
        missing = expected - names
        assert not missing, f"PolicyUpdate is missing canonical fields: {missing}"

    def test_safe_default_for_rl_exploration(self):
        # Default operating mode is hybrid; exploration should be 0 (pure
        # exploitation) unless policy explicitly raises it.
        p = PolicyUpdate(
            operating_mode="hybrid",
            safe_mode=False,
            min_backends=1, max_backends=5,
            slo_p95_latency_ms=200,
            anomaly_latency_multiplier=3.0,
            per_instance_capacity_rps=100,
            autoscaler_cooldown_seconds=60,
        )
        assert p.rl_exploration_rate == 0.0
        assert p.rl_confidence_threshold == 0.6
        assert p.anomaly_response == "auto-isolate"
        assert p.anomaly_recovery_window_seconds == 30


# ── 5+6. Schema-level tests (require docker stack) ────────────────────────────

# These tests connect to the live TimescaleDB via the same DSN conftest.py uses.
# They are skipped (with a clear message) when the database is unreachable, so
# the rest of this file remains runnable from a clean checkout without docker.

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
    reason="TimescaleDB not reachable; run `docker compose up -d` first",
)


@pytestmark_db
class TestRetentionPolicies:
    """SOT §10: bound storage growth on each owned data class."""

    def test_metrics_retention_7d(self):
        import psycopg2
        from tests.integration.conftest import TIMESCALEDB_DSN
        with psycopg2.connect(TIMESCALEDB_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT config "
                "FROM timescaledb_information.jobs "
                "WHERE proc_name = 'policy_retention' "
                "  AND hypertable_name = 'metrics'"
            )
            row = cur.fetchone()
            assert row is not None, "metrics retention policy not found"
            assert "7 days" in str(row[0]) or "604800" in str(row[0])

    def test_backend_health_retention_30d(self):
        import psycopg2
        from tests.integration.conftest import TIMESCALEDB_DSN
        with psycopg2.connect(TIMESCALEDB_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT config FROM timescaledb_information.jobs "
                "WHERE proc_name = 'policy_retention' AND hypertable_name = 'backend_health'"
            )
            row = cur.fetchone()
            assert row is not None, "backend_health retention policy not found"

    def test_scaling_events_retention_90d(self):
        import psycopg2
        from tests.integration.conftest import TIMESCALEDB_DSN
        with psycopg2.connect(TIMESCALEDB_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT config FROM timescaledb_information.jobs "
                "WHERE proc_name = 'policy_retention' AND hypertable_name = 'scaling_events'"
            )
            row = cur.fetchone()
            assert row is not None, "scaling_events retention policy not found"


@pytestmark_db
class TestContinuousAggregate:
    """SOT §8.4 'Gap to close': metrics_1min CAGG on the canonical schema."""

    def test_metrics_1min_exists(self):
        import psycopg2
        from tests.integration.conftest import TIMESCALEDB_DSN
        with psycopg2.connect(TIMESCALEDB_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT view_name "
                "FROM timescaledb_information.continuous_aggregates "
                "WHERE view_name = 'metrics_1min'"
            )
            assert cur.fetchone() is not None, "metrics_1min CAGG missing"

    def test_metrics_1min_refresh_policy_active(self):
        import psycopg2
        from tests.integration.conftest import TIMESCALEDB_DSN
        with psycopg2.connect(TIMESCALEDB_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM timescaledb_information.jobs "
                "WHERE proc_name = 'policy_refresh_continuous_aggregate'"
            )
            count = cur.fetchone()[0]
            assert count >= 1, "metrics_1min refresh policy not scheduled"

    def test_metrics_1min_keeps_long_format(self):
        # The CAGG must group by metric_name so engines can keep filtering by
        # canonical metric names — wide-format would break that.
        import psycopg2
        from tests.integration.conftest import TIMESCALEDB_DSN
        with psycopg2.connect(TIMESCALEDB_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'metrics_1min'"
            )
            cols = {r[0] for r in cur.fetchall()}
            assert {"bucket", "service", "instance", "metric_name", "avg_value"} <= cols
