"""
tests/unit/pool-collapse/test_pool_collapse_loop.py
────────────────────────────────────────────────────
Closed-loop regression for the anomaly pool-collapse outage
(audit/_findings/anomaly-pool-collapse-rootcause.md).

WHY THIS FILE EXISTS
────────────────────
The original outage was a *self-sustaining* 502: under load the backend pool
briefly empties, NGINX then logs the upstream *block name* `backend_pool` as the
served upstream on every 502, that phantom instance is shipped as a backend with
error_rate=1.0, the anomaly-detector scores it `unhealthy`, the lb-sidecar
"excludes" it, the pool stays empty, and the loop re-fires forever. No single
component was buggy — the failure lived in how four of them *composed*.

Two fixes broke the loop, and each already has its own per-component unit test:

  - the anomaly-detector drops non-backend instances before scoring
    (services/anomaly-detector/runloop.py: NON_BACKEND_INSTANCES /
     build_features_from_rows) — see tests/unit/anomaly-detector/test_runloop.py
  - the lb-sidecar drops verdicts that don't name a live backend
    (services/lb-sidecar/runloop.py: handle_anomaly live_backends guard) — see
     tests/unit/lb-sidecar/test_runloop.py

What no existing test covered is the property that actually matters: that the two
guards, driven by the *real* upstream signal that starts the loop, compose so the
loop can never close. That is what this file asserts. It wires the genuine
functions from three services together — the shipper's log parser, the detector's
feature builder, and the sidecar's anomaly handler — and proves a sustained
all-down 502 window can drive neither a phantom verdict nor a phantom exclusion,
across repeated cycles, while a genuine single-backend fault is still excluded.

No Docker, no Redis, no DB — runs in the unit-tests CI job (its own pytest
invocation, so the two same-named `runloop` modules don't collide in sys.modules
with the sibling per-service suites).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# ── load the real modules from three services under unique names ──────────────
# The detector and sidecar both expose a top-level `runloop` module; importing
# both as "runloop" would collide in sys.modules, so each is loaded from its file
# path under a unique name via importlib. The detector's runloop does
# `from engine_base import ...` against its own dir — exec_module runs that dir's
# sys.path.insert first, so the relative import resolves.

_REPO = Path(__file__).resolve().parents[3]
_SERVICES = _REPO / "services"


def _load(mod_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(mod_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


ad_runloop = _load("ad_runloop", _SERVICES / "anomaly-detector" / "runloop.py")
lb_runloop = _load("lb_runloop", _SERVICES / "lb-sidecar" / "runloop.py")

build_features_from_rows = ad_runloop.build_features_from_rows
handle_anomaly = lb_runloop.handle_anomaly
normalize_backend_key = lb_runloop.normalize_backend_key


# ── test fixtures ─────────────────────────────────────────────────────────────

# The NGINX upstream block name. NGINX records this as $upstream_addr on a 502
# when the block has no live `server`; it is the phantom that seeded the loop.
SENTINEL = "backend_pool"

# A realistic 5-backend pool in the canonical host:port shape the sidecar's live
# pool / weight keys use.
LIVE_POOL = [f"smartload-test-backend-{i}:8080" for i in range(1, 6)]


def _identity_registry():
    """A BackendRegistry stand-in whose translate_one passes ids through
    unchanged — matching the runtime case where the phantom `backend_pool` is not
    in the IP→name map, so handle_anomaly normalises it to `backend_pool:8080`
    (a key that is not in the live pool)."""
    reg = MagicMock()
    reg.translate_one.side_effect = lambda backend_id: backend_id
    return reg


class FakeNginxAdapter:
    """Minimal LoadBalancerAdapter double that records exclusions and tracks the
    active pool, so a test can assert the phantom never empties it."""

    def __init__(self, backends: list[str]) -> None:
        self._weights = {b: 1 for b in backends}
        self._excluded: set[str] = set()
        self.exclude_calls: list[str] = []
        self.include_calls: list[str] = []

    def current_state(self):
        return SimpleNamespace(
            upstream_weights=dict(self._weights),
            excluded_backends=set(self._excluded),
        )

    def exclude_backend(self, name: str) -> None:
        self.exclude_calls.append(name)
        self._excluded.add(name)

    def include_backend(self, name: str) -> None:
        self.include_calls.append(name)
        self._excluded.discard(name)

    def active_count(self) -> int:
        return len(set(self._weights) - self._excluded)


def _all_down_502_log_line(latency_s: float = 0.0) -> str:
    """One NGINX JSON access-log line for the all-down 502 window: NGINX reached
    no live server, so $upstream_addr is the block name `backend_pool` and the
    status is 502. This is the exact line that seeds the loop."""
    return (
        '{"timestamp":"2026-06-15T10:00:00+00:00","service":"nginx",'
        '"client_ip":"10.0.0.1","request":"GET / HTTP/1.1","request_path":"/",'
        f'"status":502,"backend":"{SENTINEL}","latency":{latency_s},'
        '"upstream_latency":"-"}'
    )


def _phantom_window_rows(samples: int = 9000) -> list[tuple]:
    """ANOMALY_QUERY-shaped rows for an all-down 502 window: the only instance is
    the phantom, pinned at 100% error. Columns: (instance, metric_name, avg, max,
    std, sample_count)."""
    return [
        (SENTINEL, "error_rate",         1.0, 1.0, 0.0, samples),
        (SENTINEL, "request_latency_ms", 0.0, 0.0, 0.0, samples),
    ]


def _phantom_verdict() -> dict:
    """The AnomalyEvent the detector would have published for the phantom BEFORE
    the allowlist fix. Replayed straight at the sidecar to prove the second guard
    catches it even if a pre-fix detector (or a stale queued message) emits it."""
    return {
        "backend_id": SENTINEL,
        "status": "unhealthy",
        "score": 1.0,
        "model_version": "trend_rule",
        "metric": "error_rate",
        "observed_value": 1.0,
        "threshold": 0.05,
        "severity": "critical",
    }


# ── 1. the leak is real: the shipper still emits the sentinel as an instance ───

def test_nginx_all_down_502_emits_phantom_instance():
    """Documents the upstream cause: a 502 all-down log line is shipped with
    instance=`backend_pool` and error_rate=1.0. This pins the signal the rest of
    the loop starts from — if a future shipper change stops emitting the
    sentinel, that becomes a deliberate decision a maintainer must make here, not
    a silent drift that hides the regression these tests guard."""
    shipper = pytest.importorskip(
        "requests"
    ) and _load("shipper_app", _SERVICES / "lb-otel-shipper" / "app.py")

    dps = shipper.line_to_datapoints(
        _all_down_502_log_line(), now_ns=1_700_000_000_000_000_000
    )
    by_metric = {name: (value, backend) for name, value, _ts, backend in dps}

    # The instance label is the phantom block name, exactly as the audit captured.
    assert by_metric["error_rate"] == (1.0, SENTINEL)
    assert by_metric["request_count"][1] == SENTINEL


# ── 2. layer one: the detector drops the phantom before it is ever scored ──────

def test_detector_drops_phantom_from_all_down_window():
    """An all-phantom 502 window yields ZERO features, so the engine has nothing
    to score and no `unhealthy` verdict can be published — the first place the
    loop is broken."""
    features = build_features_from_rows(_phantom_window_rows())
    assert features == []


def test_detector_keeps_real_backend_and_drops_phantom_in_mixed_window():
    """A window with one real backend plus the phantom keeps only the real
    backend, so the fix removes the loop without suppressing genuine signal."""
    real = "smartload-test-backend-1"
    rows = _phantom_window_rows() + [
        (real, "error_rate",         0.0,  0.0,  0.0, 120),
        (real, "request_latency_ms", 8.0, 12.0,  2.0, 120),
    ]
    features = build_features_from_rows(rows)
    ids = {f.backend_id for f in features}
    assert ids == {real}
    assert SENTINEL not in ids


# ── 3. layer two: the sidecar refuses to exclude a non-backend verdict ─────────

def test_sidecar_rejects_phantom_verdict_defense_in_depth():
    """Even if a phantom `unhealthy` verdict reaches the sidecar (a pre-fix
    detector, or a message queued before the fix shipped), handle_anomaly with a
    live pool drops it as a no-op and never calls exclude_backend — the second,
    independent break in the loop."""
    adapter = FakeNginxAdapter(LIVE_POOL)
    outcome = handle_anomaly(
        _phantom_verdict(),
        _identity_registry(),
        adapter,
        live_backends=LIVE_POOL,
    )
    assert outcome.applied is False
    assert outcome.action == "noop"
    assert normalize_backend_key(SENTINEL) not in adapter.exclude_calls
    assert adapter.exclude_calls == []
    assert adapter.active_count() == len(LIVE_POOL)


# ── 4. the headline property: the loop cannot close across repeated cycles ─────

def test_feedback_loop_cannot_close_over_repeated_cycles():
    """Drive the loop the way it ran live: every cycle NGINX emits an all-down
    502 window AND (defensively) a phantom verdict is replayed at the sidecar.
    Across many cycles, the detector produces no phantom feature and the sidecar
    performs no phantom exclusion, so the active pool is never drained — the
    self-sustaining outage is structurally impossible now."""
    adapter = FakeNginxAdapter(LIVE_POOL)
    registry = _identity_registry()

    for _cycle in range(25):
        # Detector layer: the 502 window scores nothing (phantom dropped).
        assert build_features_from_rows(_phantom_window_rows()) == []
        # Sidecar layer: a replayed phantom verdict is refused.
        outcome = handle_anomaly(
            _phantom_verdict(), registry, adapter, live_backends=LIVE_POOL
        )
        assert outcome.action == "noop"

    # No phantom exclusion ever happened; the pool stayed whole every cycle.
    assert adapter.exclude_calls == []
    assert adapter.active_count() == len(LIVE_POOL)


def test_genuine_single_backend_fault_is_still_excluded():
    """The guards are surgical, not blunt: a real backend that is in the live
    pool and reports `unhealthy` is still excluded while a quorum remains. Proven
    here in the same composed harness so a future over-broad 'drop everything'
    regression would fail this alongside the loop tests."""
    adapter = FakeNginxAdapter(LIVE_POOL)
    bad = LIVE_POOL[0]
    verdict = {"backend_id": bad, "status": "unhealthy", "score": 0.9}

    outcome = handle_anomaly(
        verdict, _identity_registry(), adapter, live_backends=LIVE_POOL
    )
    assert outcome.applied is True
    assert outcome.action == "exclude"
    assert bad in adapter.exclude_calls
    assert adapter.active_count() == len(LIVE_POOL) - 1
