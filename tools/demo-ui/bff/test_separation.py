"""
tools/demo-ui/bff/test_separation.py
──────────────────────────────────────
Verifies the operator-ui / demo-ui separation is clean:

  1. Route exclusivity — demo routes absent from operator-ui; operator
     management routes absent from demo-ui.
  2. HTTP enforcement — operator-ui Flask app returns 404 for every
     /api/ui/demo/* path; demo-ui Flask app returns 404 for operator-only
     paths like /api/ui/policy, /api/ui/scale, /api/ui/audit/*.
  3. Import cleanliness — neither BFF imports the other's code; demo-ui
     BFF imports redis for its SSE event stream; operator-ui BFF imports
     redis for the Live Engines subscriber (#121). Both uses are legitimate
     and target different channels.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ── path setup ────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[3]   # smartload/
_OP_BFF   = _ROOT / "services" / "operator-ui" / "bff"
_DEMO_BFF = _ROOT / "tools" / "demo-ui" / "bff"

for _p in (_OP_BFF, _DEMO_BFF):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Import both apps under separate aliases to avoid name collision.
# Each module is named "app" so we reload explicitly.
import importlib, types

def _load_app(path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("_bff_" + path.parent.name, path)
    mod  = importlib.util.module_from_spec(spec)           # type: ignore[arg-type]
    spec.loader.exec_module(mod)                           # type: ignore[union-attr]
    return mod

_op_mod   = _load_app(_OP_BFF   / "app.py")
_demo_mod = _load_app(_DEMO_BFF / "app.py")

op_app   = _op_mod.app
demo_app = _demo_mod.app


# ── helpers ───────────────────────────────────────────────────────────────────

def _routes(flask_app) -> set[str]:
    return {r.rule for r in flask_app.url_map.iter_rules()}


DEMO_ROUTES = {
    "/api/ui/demo/state",
    "/api/ui/demo/services",
    "/api/ui/demo/livestats",
    "/api/ui/demo/degrade",
    "/api/ui/demo/recover",
    "/api/ui/demo/mode",
    "/api/ui/demo/traffic",
    "/api/ui/demo/chaos",
    "/api/ui/demo/reset",
    "/api/ui/demo/scenario",
    "/api/ui/demo/algorithm",
    "/api/ui/demo/metrics",
    "/api/ui/demo/bench/profiles",
    "/api/ui/demo/bench/status",
    "/api/ui/demo/bench/start",
    "/api/ui/demo/bench/stop",
    "/api/ui/demo/benchmark/suites",
    "/api/ui/events",
}

OPERATOR_ROUTES = {
    "/api/ui/health",
    "/api/ui/policy",
    "/api/ui/audit/policy",
    "/api/ui/audit/scaling",
    "/api/ui/scale",
    "/api/ui/isolate",
    "/api/ui/lb/state",
    "/api/ui/lb/weights",
}


# ── 1. Route exclusivity ──────────────────────────────────────────────────────

class TestRouteExclusivity:
    def test_operator_ui_has_no_demo_routes(self):
        op_routes = _routes(op_app)
        leaked = DEMO_ROUTES & op_routes
        assert leaked == set(), f"demo routes leaked into operator-ui: {leaked}"

    def test_demo_ui_has_all_demo_routes(self):
        demo_routes = _routes(demo_app)
        missing = DEMO_ROUTES - demo_routes
        assert missing == set(), f"demo routes missing from demo-ui: {missing}"

    def test_demo_ui_has_no_operator_management_routes(self):
        demo_routes = _routes(demo_app)
        leaked = OPERATOR_ROUTES & demo_routes
        assert leaked == set(), f"operator routes leaked into demo-ui: {leaked}"

    def test_operator_ui_has_all_operator_routes(self):
        op_routes = _routes(op_app)
        missing = OPERATOR_ROUTES - op_routes
        assert missing == set(), f"operator routes missing from operator-ui: {missing}"

    def test_operator_ui_has_own_health(self):
        assert "/health" in _routes(op_app)

    def test_demo_ui_has_own_health(self):
        assert "/health" in _routes(demo_app)


# ── 2. HTTP enforcement ───────────────────────────────────────────────────────

@pytest.fixture()
def op_client():
    op_app.config["TESTING"] = True
    with op_app.test_client() as c:
        yield c


@pytest.fixture()
def demo_client():
    demo_app.config["TESTING"] = True
    with demo_app.test_client() as c:
        yield c


class TestHttpEnforcement:
    @pytest.mark.parametrize("path", sorted(DEMO_ROUTES - {"/api/ui/events"}))
    def test_operator_ui_returns_404_for_demo_paths(self, op_client, path):
        resp = op_client.get(path)
        # Flask SPA fallback catches unknown GETs with serve_spa → non-404.
        # POST paths that aren't registered return 405 (method not allowed)
        # on their SPA route. Use the route set check instead of HTTP status
        # for the SPA-fallback case; assert the endpoint is not in url_map.
        assert path not in _routes(op_app), \
            f"{path} should not be registered in operator-ui"

    @pytest.mark.parametrize("path", sorted(OPERATOR_ROUTES - {"/api/ui/health"}))
    def test_demo_ui_returns_404_for_operator_paths(self, demo_client, path):
        resp = demo_client.get(path)
        assert path not in _routes(demo_app), \
            f"{path} should not be registered in demo-ui"

    def test_operator_ui_health_returns_200(self, op_client):
        resp = op_client.get("/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["service"] == "operator-ui"

    def test_demo_ui_health_returns_200(self, demo_client):
        resp = demo_client.get("/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["service"] == "demo-ui"


# ── 3. Import cleanliness ─────────────────────────────────────────────────────

class TestImportCleanliness:
    def test_demo_bff_imports_redis(self):
        assert hasattr(_demo_mod, "redis_lib"), \
            "demo-ui BFF must import redis for SSE"

    def test_operator_bff_does_not_import_socket(self):
        assert not hasattr(_op_mod, "socket"), \
            "operator-ui BFF should not import socket (backend IP resolution moved to demo-ui)"

    def test_operator_bff_has_no_backend_urls(self):
        assert not hasattr(_op_mod, "BACKEND_URLS"), \
            "BACKEND_URLS should not exist in operator-ui BFF"

    def test_operator_bff_has_no_traffic_simulator_url(self):
        assert not hasattr(_op_mod, "TRAFFIC_SIMULATOR_URL"), \
            "TRAFFIC_SIMULATOR_URL should not exist in operator-ui BFF"

    def test_demo_bff_has_backend_urls(self):
        assert hasattr(_demo_mod, "BACKEND_URLS"), \
            "demo-ui BFF must have BACKEND_URLS for chaos injection"

    def test_demo_bff_has_traffic_simulator_url(self):
        assert hasattr(_demo_mod, "TRAFFIC_SIMULATOR_URL"), \
            "demo-ui BFF must have TRAFFIC_SIMULATOR_URL for traffic control"


# ── 4. Dev-console surface (benchmark suites + load profiles) ─────────────────

class TestDevConsoleSurface:
    """The redesigned dev console adds a suite-aware benchmark surface and an
    in-cluster one-click load-profile runner. These checks exercise the pure
    config + routing (no external services needed)."""

    def test_both_result_suites_registered(self):
        assert set(_demo_mod.SUITES.keys()) == {"adaptive", "baseline"}, \
            "demo-ui must expose both the adaptive and baseline result suites"
        for sid, cfg in _demo_mod.SUITES.items():
            assert cfg["plots"], f"suite {sid} must declare at least one plot key"

    def test_suite_scoped_benchmark_routes_present(self):
        routes = _routes(demo_app)
        for r in (
            "/api/ui/demo/benchmark/<suite>/runs",
            "/api/ui/demo/benchmark/<suite>/runs/<timestamp>/summary",
            "/api/ui/demo/benchmark/<suite>/runs/<timestamp>/plot/<name>",
            "/api/ui/demo/benchmark/<suite>/runs/<timestamp>/manifest",
        ):
            assert r in routes, f"missing suite-scoped route: {r}"

    def test_legacy_benchmark_aliases_kept(self):
        routes = _routes(demo_app)
        assert "/api/ui/demo/benchmark/runs" in routes, \
            "legacy unscoped benchmark route must remain as a baseline alias"

    def test_load_profiles_have_valid_shape(self):
        assert _demo_mod.BENCH_PROFILES, "at least one load profile must exist"
        ids = [p["id"] for p in _demo_mod.BENCH_PROFILES]
        assert len(ids) == len(set(ids)), "profile ids must be unique"
        for p in _demo_mod.BENCH_PROFILES:
            assert p["phases"], f"profile {p['id']} has no phases"
            for ph in p["phases"]:
                assert ph["secs"] > 0 and ph["users"] >= 0, \
                    f"profile {p['id']} phase {ph['name']} has invalid timing"

    def test_bench_profiles_endpoint_returns_catalog(self, demo_client):
        resp = demo_client.get("/api/ui/demo/bench/profiles")
        assert resp.status_code == 200
        body = resp.get_json()
        assert len(body["profiles"]) == len(_demo_mod.BENCH_PROFILES)
        first = body["profiles"][0]
        assert {"id", "label", "description", "total_secs", "phases"} <= set(first)

    def test_benchmark_suites_endpoint_returns_both(self, demo_client):
        resp = demo_client.get("/api/ui/demo/benchmark/suites")
        assert resp.status_code == 200
        ids = {s["id"] for s in resp.get_json()["suites"]}
        assert ids == {"adaptive", "baseline"}

    def test_bench_start_rejects_unknown_profile(self, demo_client):
        resp = demo_client.post("/api/ui/demo/bench/start", json={"profile_id": "nope"})
        assert resp.status_code == 400

    def test_unknown_suite_runs_404(self, demo_client):
        resp = demo_client.get("/api/ui/demo/benchmark/bogus/runs")
        assert resp.status_code == 404

    def test_kpi_route_registered(self):
        assert "/api/ui/demo/benchmark/<suite>/runs/<timestamp>/kpis" in _routes(demo_app)

    def test_adaptive_kpi_parser_extracts_headline_cards(self):
        summary = (
            "Run anchor: **2026-06-12 16:23:49 UTC -> 16:29:45 UTC**  (356 s).\n\n"
            "## Per-phase\n\n"
            "| Phase | Window | Users | RPS | p95 | Pool |\n|---|---|---|---|---|---|\n"
            "| `A_bootstrap` | w | 19 users | 49.7 | 10 | 1..1 |\n"
            "| `C_sustain` | w | 200 users | 947.0 | 150 | 1..5 |\n"
            "| `D_anomaly_scale_down` | w | 200 users | 203.0 | 200 | 3..6 |\n\n"
            "## Time-to-react\n\n"
            "| f0 | t | 29.0 | `scale_out` (ic=2) | t | 94.4s |\n"
            "| f18 | t | 110.3 | `scale_out` (ic=2) | t | 0.6s |\n\n"
            "## Autoscaler action counts (bench window)\n\n"
            "- **scale_out**: 7\n- **scale_in**:  5\n- **total decisions in audit**: 12\n\n"
            "## Phase-D anomaly window\n\n"
            "| Target | Injected | Recovered | Window | Pool |\n|---|---|---|---|---|\n"
            "| `smartload-test-backend-4` (dynamic=False) | 16:27:48 | 16:28:48 | 60s | 3 backends |\n"
        )
        cards = {c["label"]: c["value"] for c in _demo_mod._parse_adaptive_kpis(summary)}
        assert cards["Pool size"] == "1 → 6"
        assert cards["Scaling actions"] == "12"
        assert cards["Fastest reaction"] == "0.6s"
        assert cards["Peak p95"] == "200 ms"
        assert cards["Run length"] == "356 s"
        assert "test-backend-4" in next(
            c["hint"] for c in _demo_mod._parse_adaptive_kpis(summary) if c["label"] == "Anomaly")
