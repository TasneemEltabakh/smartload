"""
services/anomaly-detector/manual.py
────────────────────────────────────
Pure-Python validation + payload logic for the manual-action endpoints
(POST /api/v1/isolate and its dry-run sibling POST /api/v1/actions/simulate).

Lives next to app.py so the real isolate path and the simulate path share
ONE validation function — a failed simulate is therefore guaranteed to imply
a failed real isolate (and vice-versa). The plan function never talks to
Redis or the DB; those side effects happen in app.py against the payload it
returns.

Mirrors the shape of services/autoscaler/manual.py (ManualScaleError /
plan_manual_scale) so the two manual surfaces read the same way.
"""

from __future__ import annotations

from dataclasses import dataclass

VALID_ISOLATE_STATUSES = ("healthy", "degraded", "unhealthy")


class ManualIsolateError(ValueError):
    """Raised when a manual isolate request can't be validated.

    The `field` attribute names the offending input — the HTTP layer
    surfaces this as `{"error": ..., "field": ...}` 400 so SDK consumers
    can map back to a `ValidationError(field=...)`.
    """
    def __init__(self, message: str, field: str):
        super().__init__(message)
        self.message = message
        self.field = field


@dataclass(frozen=True)
class ManualIsolatePlan:
    """The synthetic AnomalyEvent that an isolate WOULD produce.

    `payload` is the exact dict handed to publish_envelope on the real path,
    so the simulate path can wrap it in a (non-published) envelope and the
    two surfaces stay byte-for-byte identical apart from the side effects.
    """
    backend_id: str
    status: str            # healthy | degraded | unhealthy
    score: float           # 0.0 for healthy, 1.0 otherwise
    actor: str
    reason: str            # audited reason — manual:<actor>: <user reason>
    severity: str          # UI bucket — critical | warning | info
    payload: dict          # AnomalyEvent payload (what would publish)


def _severity_for_status(status: str) -> str:
    """Map an isolate status to the UI severity bucket used across the
    operator-ui alert surfaces (critical / warning / info)."""
    if status == "unhealthy":
        return "critical"
    if status == "degraded":
        return "warning"
    return "info"


def plan_manual_isolate(
    *,
    backend_id,             # parsed from JSON — could be anything
    status,                 # parsed from JSON — could be anything
    actor: str,
    user_reason: str | None,
) -> ManualIsolatePlan:
    """Validate the isolate inputs + compose the synthetic-event payload.

    Validation (identical for the real and the simulate path):
      - backend_id must be a non-empty string
      - status must be one of VALID_ISOLATE_STATUSES

    The reason is composed so it's grep-able in the audit log:
    `manual:<actor>: <user_reason>`; when user_reason is empty/missing it
    falls back to "manual". score is fixed at 1.0 for unhealthy / degraded
    and 0.0 for healthy (the operator's intent is the signal).
    """
    if not isinstance(backend_id, str) or not backend_id.strip():
        raise ManualIsolateError(
            "backend_id must be a non-empty string",
            field="backend_id",
        )
    backend_id = backend_id.strip()

    if status not in VALID_ISOLATE_STATUSES:
        raise ManualIsolateError(
            f"status must be one of {list(VALID_ISOLATE_STATUSES)}",
            field="status",
        )

    safe_actor = (actor or "operator").strip() or "operator"
    safe_user_reason = (user_reason or "manual").strip() or "manual"
    audited_reason = f"manual:{safe_actor}: {safe_user_reason}"
    score = 0.0 if status == "healthy" else 1.0
    severity = _severity_for_status(status)

    payload = {
        "backend_id":    backend_id,
        "status":        status,
        "score":         score,
        "severity":      severity,
        "model_version": f"manual:{safe_actor}",
        "features":      {"reason": audited_reason},
    }

    return ManualIsolatePlan(
        backend_id=backend_id,
        status=status,
        score=score,
        actor=safe_actor,
        reason=audited_reason,
        severity=severity,
        payload=payload,
    )
