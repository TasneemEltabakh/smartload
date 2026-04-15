"""
Issue #12: Grafana Dashboard — Integration Tests

Verifies:
  - Grafana container is running and reachable
  - TimescaleDB data source is provisioned and healthy
  - SmartLoad telemetry dashboard exists with correct UID
  - Key panels are present (requests, latency, CPU, spikes)
  - Dashboard auto-refreshes (refresh != "")

Usage:
  cd tests/integration
  pip install requests pytest
  python -m pytest test_grafana_issue12.py -v -s
"""

import pytest
import requests
import time

GRAFANA_URL = "http://localhost:3000"
GRAFANA_AUTH = ("admin", "smartload123")
DASHBOARD_UID = "smartload-telemetry-v1"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def grafana_get(path: str, timeout: int = 10) -> requests.Response:
    return requests.get(f"{GRAFANA_URL}{path}", auth=GRAFANA_AUTH, timeout=timeout)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGrafanaRunning:
    def test_grafana_health_endpoint(self):
        """Grafana /api/health must return 200 with database=ok."""
        resp = grafana_get("/api/health")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        body = resp.json()
        assert body.get("database") == "ok", f"Grafana DB not ok: {body}"

    def test_grafana_login_works(self):
        """Admin credentials must authenticate successfully."""
        resp = grafana_get("/api/user")
        assert resp.status_code == 200, "Admin login failed"
        assert resp.json().get("login") == "admin"


class TestDataSource:
    def test_timescaledb_datasource_provisioned(self):
        """TimescaleDB data source must be auto-provisioned."""
        resp = grafana_get("/api/datasources")
        assert resp.status_code == 200
        sources = resp.json()
        names = [ds["name"] for ds in sources]
        assert "TimescaleDB" in names, f"TimescaleDB not in data sources: {names}"

    def test_timescaledb_datasource_is_postgres_type(self):
        """Data source must be of type postgres (Grafana's PostgreSQL plugin)."""
        resp = grafana_get("/api/datasources/name/TimescaleDB")
        assert resp.status_code == 200
        ds = resp.json()
        assert ds["type"] in ("postgres", "grafana-postgresql-datasource"), f"Wrong type: {ds['type']}"

    def test_timescaledb_datasource_health(self):
        """Data source health check must pass (proves DB connectivity)."""
        resp = grafana_get("/api/datasources/name/TimescaleDB")
        ds_uid = resp.json()["uid"]

        health_resp = grafana_get(f"/api/datasources/uid/{ds_uid}/health")
        assert health_resp.status_code == 200, (
            f"Data source health check failed: {health_resp.text}"
        )
        body = health_resp.json()
        assert body.get("status") == "OK", f"Data source unhealthy: {body}"


class TestDashboard:
    def test_dashboard_exists_by_uid(self):
        """SmartLoad telemetry dashboard must exist with the correct UID."""
        resp = grafana_get(f"/api/dashboards/uid/{DASHBOARD_UID}")
        assert resp.status_code == 200, (
            f"Dashboard '{DASHBOARD_UID}' not found: {resp.status_code}"
        )

    def test_dashboard_title(self):
        """Dashboard must have the correct title."""
        resp = grafana_get(f"/api/dashboards/uid/{DASHBOARD_UID}")
        meta = resp.json()
        title = meta["dashboard"]["title"]
        assert "SmartLoad" in title, f"Unexpected title: {title}"

    def test_dashboard_has_required_panels(self):
        """Dashboard must include panels for request rate, latency, CPU, and spikes."""
        resp = grafana_get(f"/api/dashboards/uid/{DASHBOARD_UID}")
        panels = resp.json()["dashboard"]["panels"]
        titles_lower = [p["title"].lower() for p in panels]

        required_keywords = ["request", "latency", "cpu", "spike"]
        for keyword in required_keywords:
            assert any(keyword in t for t in titles_lower), (
                f"No panel found containing '{keyword}' in title. Panels: {titles_lower}"
            )

    def test_dashboard_panel_count(self):
        """Dashboard must have at least 4 panels (acceptance criteria minimum)."""
        resp = grafana_get(f"/api/dashboards/uid/{DASHBOARD_UID}")
        panels = resp.json()["dashboard"]["panels"]
        assert len(panels) >= 4, f"Expected >= 4 panels, got {len(panels)}"

    def test_dashboard_auto_refresh_enabled(self):
        """Dashboard must have auto-refresh configured (live data requirement)."""
        resp = grafana_get(f"/api/dashboards/uid/{DASHBOARD_UID}")
        refresh = resp.json()["dashboard"].get("refresh", "")
        assert refresh != "", "Dashboard auto-refresh is not set"

    def test_dashboard_has_timescaledb_datasource(self):
        """All panels must reference the TimescaleDB data source."""
        resp = grafana_get(f"/api/dashboards/uid/{DASHBOARD_UID}")
        panels = resp.json()["dashboard"]["panels"]
        for panel in panels:
            ds = panel.get("datasource", {})
            if isinstance(ds, dict):
                assert ds.get("type") == "postgres", (
                    f"Panel '{panel['title']}' uses wrong datasource type: {ds}"
                )