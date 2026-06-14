"""
services/policy-manager/strategies.py
──────────────────────────────────────
Named-strategy ⇄ primitive translation for the policy-manager (#150).

Industry vocabulary uses named load-balancing strategies (``round-robin``,
``least-connections``, ``latency-aware``, ``forecast-aware``, ``anomaly-aware``,
``ai-hybrid``, ``safe-fallback``). SmartLoad's runtime policy is composed from
primitives instead — ``operating_mode`` + ``safe_mode``, with the ``RL_MODE``
env-var pin as a deploy-time concern. This module is the thin translation layer
that lets integrators speak in industry terms while the powerful primitive model
stays the single source of truth.

Pure-Python and stdlib-light (no Flask / Redis / DB imports), same pattern as
``validation.py`` and ``services/autoscaler/decisions.py``, so the mapping logic
unit-tests without the Docker stack.

The base named-strategy → primitive table is imported from
``shared.config_loader.STRATEGY_PRIMITIVES`` (#145) — that is the single
definition; this module never restates it. ``safe-fallback`` is layered on here
because it is a policy-manager concern (the deterministic kill switch:
``safe_mode = True``) rather than a bootstrap-render strategy, so it is not in
the bootstrap table.

Two public functions:

  - :func:`name_to_primitives` — strategy name → the policy fields to write
    (``operating_mode`` + ``safe_mode`` ONLY) plus the *recommended* ``RL_MODE``
    surfaced for the operator. ``rl_mode`` is never written to policy; it is a
    deploy-time env pin.
  - :func:`primitives_to_name` — reverse map a live (``operating_mode``,
    ``safe_mode``) pair back to a strategy name for the derived ``strategy_name``
    field on GET.

Reverse-map note (NOT unique): ``latency-aware``, ``forecast-aware`` and
``anomaly-aware`` all share the same primitive combination
(``hybrid`` + ``safe_mode=False``). The reverse map therefore makes a documented
canonical choice — it returns the single *representative* name for each primitive
combination (see :data:`_CANONICAL_REVERSE`). Primitive pairs that match no known
strategy reverse-map to ``"custom"``.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, Optional, Tuple

# Resolve the canonical shared/ module across the two layouts policy-manager
# already supports (container /app/shared, dev services/shared) — identical to
# the resolver in app.py so this module imports cleanly in both.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (_HERE, os.path.dirname(_HERE)):
    if os.path.isdir(os.path.join(_cand, "shared")):
        if _cand not in sys.path:
            sys.path.insert(0, _cand)
        break

from shared.config_loader import STRATEGY_PRIMITIVES  # noqa: E402


class StrategyError(ValueError):
    """Raised when a named strategy is unknown. Carries the allowed list so the
    HTTP layer can echo a helpful 400."""

    def __init__(self, message: str, *, allowed: Optional[Tuple[str, ...]] = None):
        super().__init__(message)
        self.message = message
        self.allowed = allowed

    def __str__(self) -> str:
        return self.message


# ── the canonical strategy table ────────────────────────────────────────────────
#
# The six bootstrap strategies come straight from shared.config_loader (#145);
# safe-fallback is the policy-manager-only kill switch (deterministic baseline via
# safe_mode=True), so it is layered on here. operating_mode values are the
# canonical policy.yaml enum the validator accepts
# (classical-only / hybrid / rl-only).
STRATEGIES: Dict[str, Dict[str, Any]] = {
    **STRATEGY_PRIMITIVES,
    "safe-fallback": {"operating_mode": "classical-only", "safe_mode": True, "rl_mode": None},
}

# Allowed strategy names, sorted, for stable error messages + docs.
ALLOWED_STRATEGIES: Tuple[str, ...] = tuple(sorted(STRATEGIES))


# ── canonical reverse map ────────────────────────────────────────────────────────
#
# The forward map is many-to-one: several strategies share the same
# (operating_mode, safe_mode) primitive pair (notably latency/forecast/
# anomaly-aware all → hybrid + safe_mode=False). Reversing it therefore requires
# a documented canonical CHOICE — one representative name per primitive pair.
#
# Choices (documented in docs/features/named-strategies.md):
#   (classical-only, False) → "round-robin"   (representative pure-NGINX strategy;
#                                               least-connections shares the pair)
#   (classical-only, True)  → "safe-fallback" (unambiguous: the only safe_mode pair)
#   (hybrid,         False) → "latency-aware"  (representative hybrid+shadow strategy;
#                                               forecast/anomaly-aware + ai-hybrid
#                                               share the pair — ai-hybrid differs
#                                               only by the deploy-time RL_MODE pin,
#                                               which is NOT a policy field, so it is
#                                               not reverse-distinguishable here)
#
# Any primitive pair NOT in this table reverse-maps to "custom" (e.g. rl-only,
# or hybrid + safe_mode=True): the primitives are valid but match no documented
# strategy, so "custom" is the honest, clearer label.
_CANONICAL_REVERSE: Dict[Tuple[str, bool], str] = {
    ("classical-only", False): "round-robin",
    ("classical-only", True): "safe-fallback",
    ("hybrid", False): "latency-aware",
}

# Sentinel returned when the live primitives match no documented strategy.
CUSTOM_STRATEGY = "custom"


# ── public API ────────────────────────────────────────────────────────────────

def name_to_primitives(name: Any) -> Dict[str, Any]:
    """Translate a named strategy to its primitives.

    Returns a dict with exactly:
      - ``operating_mode``  — canonical enum value to write to policy
      - ``safe_mode``       — bool to write to policy
      - ``rl_mode``         — the RECOMMENDED deploy-time ``RL_MODE`` pin
                              (``"shadow"`` / ``"active"`` / ``None``). Surfaced
                              to the operator only; NEVER written to policy.

    Raises :class:`StrategyError` (with ``allowed``) on an unknown / non-string
    name so the HTTP layer can return a 400 listing the allowed strategies.
    """
    if not isinstance(name, str) or not name:
        raise StrategyError(
            f"strategy name must be a non-empty string (got {name!r}); "
            f"allowed: {list(ALLOWED_STRATEGIES)}",
            allowed=ALLOWED_STRATEGIES,
        )
    spec = STRATEGIES.get(name)
    if spec is None:
        raise StrategyError(
            f"unknown strategy {name!r}; allowed: {list(ALLOWED_STRATEGIES)}",
            allowed=ALLOWED_STRATEGIES,
        )
    # Return a fresh dict so callers can't mutate the table.
    return {
        "operating_mode": spec["operating_mode"],
        "safe_mode": spec["safe_mode"],
        "rl_mode": spec["rl_mode"],
    }


def name_to_policy(name: Any) -> Dict[str, Any]:
    """The policy-field subset of :func:`name_to_primitives`.

    Returns ONLY the fields that are written through ``POST /api/v1/policy``
    (``operating_mode`` + ``safe_mode``). ``rl_mode`` is deliberately excluded —
    it is a deploy-time env pin, not a runtime policy field.
    """
    prims = name_to_primitives(name)
    return {"operating_mode": prims["operating_mode"], "safe_mode": prims["safe_mode"]}


def recommended_rl_mode(name: Any) -> Optional[str]:
    """The recommended ``RL_MODE`` env pin for a named strategy (or ``None`` when
    the strategy does not engage the RL plane). Surfaced to operators; never set
    as a policy field."""
    return name_to_primitives(name)["rl_mode"]


def primitives_to_name(operating_mode: Any, safe_mode: Any) -> str:
    """Reverse-map a live primitive pair to its representative strategy name.

    Returns the canonical representative strategy for the
    ``(operating_mode, safe_mode)`` pair, or :data:`CUSTOM_STRATEGY` (``"custom"``)
    when the pair matches no documented strategy. The reverse map is
    intentionally many-to-one → one representative (see :data:`_CANONICAL_REVERSE`
    and the module docstring); callers wanting the exact set of names a pair could
    have come from should consult the forward table instead.

    Defensive against malformed on-disk policy: a missing / non-bool ``safe_mode``
    is treated as ``False`` for lookup purposes, falling through to ``"custom"``
    when the resulting pair is still unknown.
    """
    sm = bool(safe_mode) if isinstance(safe_mode, bool) else False
    return _CANONICAL_REVERSE.get((operating_mode, sm), CUSTOM_STRATEGY)
