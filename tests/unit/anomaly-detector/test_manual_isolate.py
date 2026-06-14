"""
tests/unit/anomaly-detector/test_manual.py
─────────────────────────────────────────────
Pure-Python unit tests for services/anomaly-detector/manual.py.

No Docker, no DB, no Redis — runs in the unit-tests CI job.

Coverage:
  1. Validation:
     - empty / whitespace / non-string backend_id → ManualIsolateError(field='backend_id')
     - unknown status                              → ManualIsolateError(field='status')
  2. Payload composition:
     - score = 1.0 for unhealthy / degraded, 0.0 for healthy
     - severity bucket (critical / warning / info)
     - reason prefix `manual:<actor>:`, actor + reason fallbacks
     - payload carries backend_id / status / score / severity / model_version /
       features.reason — the AnomalyEvent shape the engine would publish.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Load the anomaly-detector's manual.py by explicit path under a unique module
# name. A bare `from manual import ...` would collide with the autoscaler's
# manual.py (same module basename) under pytest's prepend import mode. The
# module is registered in sys.modules before exec so @dataclass can resolve
# its own __module__ during class processing.
_MODNAME = "anomaly_detector_manual"
_SERVICE = Path(__file__).resolve().parents[2].parent / "services" / "anomaly-detector"
_spec = importlib.util.spec_from_file_location(_MODNAME, _SERVICE / "manual.py")
_manual = importlib.util.module_from_spec(_spec)
sys.modules[_MODNAME] = _manual
_spec.loader.exec_module(_manual)

ManualIsolateError = _manual.ManualIsolateError
plan_manual_isolate = _manual.plan_manual_isolate


def _plan(backend_id="backend_1", status="unhealthy", actor="op", reason="manual"):
    return plan_manual_isolate(
        backend_id=backend_id,
        status=status,
        actor=actor,
        user_reason=reason,
    )


# ── validation ───────────────────────────────────────────────────────────────

class TestValidation:

    def test_empty_backend_id_raises(self):
        with pytest.raises(ManualIsolateError) as exc:
            _plan(backend_id="")
        assert exc.value.field == "backend_id"

    def test_whitespace_backend_id_raises(self):
        with pytest.raises(ManualIsolateError) as exc:
            _plan(backend_id="   ")
        assert exc.value.field == "backend_id"

    def test_non_string_backend_id_raises(self):
        with pytest.raises(ManualIsolateError) as exc:
            _plan(backend_id=123)  # type: ignore[arg-type]
        assert exc.value.field == "backend_id"

    def test_unknown_status_raises(self):
        with pytest.raises(ManualIsolateError) as exc:
            _plan(status="bogus")
        assert exc.value.field == "status"

    def test_missing_status_raises(self):
        with pytest.raises(ManualIsolateError) as exc:
            _plan(status=None)  # type: ignore[arg-type]
        assert exc.value.field == "status"

    @pytest.mark.parametrize("status", ["healthy", "degraded", "unhealthy"])
    def test_valid_statuses_accepted(self, status):
        plan = _plan(status=status)
        assert plan.status == status

    def test_backend_id_is_trimmed(self):
        plan = _plan(backend_id="  backend_9  ")
        assert plan.backend_id == "backend_9"


# ── score + severity ─────────────────────────────────────────────────────────

class TestScoreAndSeverity:

    def test_healthy_zero_score_info_severity(self):
        plan = _plan(status="healthy")
        assert plan.score == 0.0
        assert plan.severity == "info"

    def test_degraded_full_score_warning_severity(self):
        plan = _plan(status="degraded")
        assert plan.score == 1.0
        assert plan.severity == "warning"

    def test_unhealthy_full_score_critical_severity(self):
        plan = _plan(status="unhealthy")
        assert plan.score == 1.0
        assert plan.severity == "critical"


# ── reason composition ───────────────────────────────────────────────────────

class TestReason:

    def test_reason_prefixed_with_actor(self):
        plan = _plan(actor="alice", reason="failover drill")
        assert plan.reason == "manual:alice: failover drill"

    def test_missing_actor_falls_back_to_operator(self):
        plan = _plan(actor="", reason="r")
        assert plan.reason.startswith("manual:operator:")
        assert plan.actor == "operator"

    def test_whitespace_actor_falls_back_to_operator(self):
        plan = _plan(actor="   ", reason="r")
        assert plan.reason.startswith("manual:operator:")

    def test_missing_user_reason_falls_back_to_default(self):
        plan = _plan(actor="bob", reason=None)
        assert plan.reason == "manual:bob: manual"

    def test_empty_user_reason_falls_back_to_default(self):
        plan = _plan(actor="bob", reason="   ")
        assert plan.reason == "manual:bob: manual"


# ── payload (the AnomalyEvent the engine would publish) ───────────────────────

class TestPayload:

    def test_payload_shape(self):
        plan = _plan(backend_id="backend_3", status="unhealthy",
                     actor="op", reason="drill")
        assert plan.payload == {
            "backend_id":    "backend_3",
            "status":        "unhealthy",
            "score":         1.0,
            "severity":      "critical",
            "model_version": "manual:op",
            "features":      {"reason": "manual:op: drill"},
        }

    def test_payload_reason_matches_plan_reason(self):
        plan = _plan(actor="carol", reason="x")
        assert plan.payload["features"]["reason"] == plan.reason
