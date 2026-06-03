"""
services/operator-ui/bff/aggregator.py
───────────────────────────────────────
Pure-Python fan-out for `GET /api/v1/status` (OUI.9 / #149).

No Flask, no Redis, no service clients — every IO action enters this module
as an injected callable. The whole composition is testable with stub
functions and zero network.

Behaviour contract (issue #149):
  - Hit each service's /health in parallel with a per-service timeout.
  - 200 always — service failures show up as `{"status": "down", ...}`.
  - `overall`:  "down" if any service is down,
                "degraded" if any is non-ok but reachable,
                "ok" if every service is ok.
  - active_policy + recent are best-effort — if either fetch fails the
    field is null. Service-level reachability dominates the overall pill.

The aggregator never raises. Caller wires this into a Flask route that
always returns 200 with whatever this returns.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Optional


# Default per-service HTTP timeout. Picked to keep the overall response
# under 3s even if one service hangs (issue acceptance criterion).
DEFAULT_TIMEOUT_S: float = 2.0


# Field names each service exposes on /health that we surface in the
# /api/v1/status response. Used for documentation only — the fetcher
# below copies every non-canonical key through so forward-compat fields
# from a future /health revision don't get dropped.
_KNOWN_HEALTH_EXTRAS: dict[str, set[str]] = {
    "policy-manager":   {"redis", "timescaledb", "policy_version"},
    "telemetry":        {"redis", "timescaledb", "rows_written_1m"},
    "anomaly-detector": {"runloop_enabled", "engine"},
    "forecasting":      {"runloop_enabled", "engine"},
    "rl-engine":        {"runloop_enabled", "policy", "mode"},
    "autoscaler":       {"active_target_count"},
    "lb-sidecar":       {"redis"},
    "load-balancer":    {"upstream_count"},
}


# ── per-service fetch ────────────────────────────────────────────────────────

def fetch_service_status(
    name: str,
    base_url: str,
    http_get: Callable[..., Any],
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> tuple[str, dict]:
    """Hit one service's /health and return (name, status_dict).

    Never raises. Failures (timeout, connection refused, malformed JSON,
    non-2xx) all collapse to `{"status": "down", ...}` so the caller can
    treat the result uniformly.
    """
    url = f"{base_url.rstrip('/')}/health"
    try:
        r = http_get(url, timeout=timeout_s)
    except Exception as exc:
        return name, {"status": "down", "error": type(exc).__name__}

    status_code = getattr(r, "status_code", 0)
    try:
        body = r.json()
        body_parsed = True
    except Exception:
        body = {}
        body_parsed = False

    if status_code != 200:
        return name, {"status": "down", "error": f"http_{status_code}"}
    if not body_parsed:
        return name, {"status": "down", "error": "malformed_json"}
    if not isinstance(body, dict):
        return name, {"status": "down", "error": "non_object_body"}

    # Forward every key on the /health response (minus `service`, which is
    # redundant once we're already grouping by name). The `status` field is
    # special — it's the pill colour driver for `overall`.
    out: dict[str, Any] = {"status": body.get("status", "unknown")}
    for k, v in body.items():
        if k in {"status", "service"}:
            continue
        out[k] = v
    return name, out


# ── overall composition ──────────────────────────────────────────────────────

def compute_overall(services: dict[str, dict]) -> str:
    """Roll up per-service status into the top-level pill.

    Rules (#149):
      - "down" if any service status is "down" (unreachable / non-2xx /
        timeout — the fetcher collapses all reachability failures to this).
      - "degraded" if no service is "down" but any returns a non-"ok"
        status (e.g. the service is up but reports "degraded").
      - "ok" if every service reports "ok".
    """
    if not services:
        return "ok"
    statuses = [v.get("status", "down") for v in services.values()]
    if any(s == "down" for s in statuses):
        return "down"
    if any(s != "ok" for s in statuses):
        return "degraded"
    return "ok"


# ── full response builder ────────────────────────────────────────────────────

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_status_response(
    service_urls: dict[str, str],
    http_get: Callable[..., Any],
    fetch_active_policy: Callable[[], Optional[dict]],
    fetch_last_policy_change: Callable[[], Optional[dict]],
    fetch_last_scaling_event: Callable[[], Optional[dict]],
    timeout_s: float = DEFAULT_TIMEOUT_S,
    now_iso: Callable[[], str] = _iso_now,
) -> dict:
    """Compose the full `/api/v1/status` response.

    Every IO is injected so unit tests can stub them without a network. The
    fan-out is parallel across services; active_policy + recent are fetched
    after the fan-out completes (the policy-manager + autoscaler audit
    routes are already covered by the service fan-out's timeout, so an
    additional layer of concurrency would over-complicate without speeding
    up the worst case).
    """
    if service_urls:
        with ThreadPoolExecutor(max_workers=len(service_urls)) as pool:
            results = list(pool.map(
                lambda kv: fetch_service_status(kv[0], kv[1], http_get, timeout_s),
                service_urls.items(),
            ))
        services = dict(results)
    else:
        services = {}

    try:
        active_policy = fetch_active_policy()
    except Exception:
        active_policy = None

    try:
        last_policy_change = fetch_last_policy_change()
    except Exception:
        last_policy_change = None

    try:
        last_scaling_event = fetch_last_scaling_event()
    except Exception:
        last_scaling_event = None

    return {
        "generated_at": now_iso(),
        "overall": compute_overall(services),
        "services": services,
        "active_policy": active_policy,
        "recent": {
            "last_policy_change": last_policy_change,
            "last_scaling_event": last_scaling_event,
        },
    }
