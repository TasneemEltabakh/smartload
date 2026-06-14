"""
services/shared/config_loader.py
────────────────────────────────
Single-file client bootstrap. Read ``config/smartload.yml`` — the externally
shared, camelCase integration shape — and normalise it into the two things the
running stack already consumes:

  - the canonical runtime policy (the ``config/policy.yaml`` field set,
    snake_case) that policy-manager validates and every decision-plane service
    reads on boot;
  - a flat env mapping (matching ``config/.env.example``) for compose to inject.

Why a translation layer rather than a new canonical store: ``policy.yaml`` stays
the single source of truth for *runtime* operating policy — live-updated over
``smartload.policy`` and sole-written by policy-manager. ``smartload.yml`` is a
*bootstrap* convenience read once to seed the stack, so a client edits one file
instead of three (``policy.yaml`` + ``.env`` + a compose override). When
``smartload.yml`` is absent the legacy dual-file setup is untouched, so the
transition is backwards-compatible.

Scope of what this renders today: the runtime-policy knobs that have a real
consumer right now (strategy, SLO latency, cooldown, evaluation interval, RL
mode). Deployment topology in the file (metrics URL, load-balancer endpoint,
orchestrator type, backend scrape targets) is validated and accepted but not
rendered here — that is consumed by the Helm packaging work (#133), per the
issue's own out-of-scope note. Reserved fields are validated so a typo fails
loudly instead of silently doing nothing.

Module-first and stdlib-light: the pure ``validate`` / ``to_policy`` / ``to_env``
/ ``merge_policy`` functions operate on plain dicts and import nothing heavy, so
they unit-test without PyYAML, redis or flask. Only :func:`read_file` imports
yaml, lazily. Wiring this into per-service startup / compose is a separate,
CI-watched step, matching ``shared/config.py`` and ``shared/bootstrap.py``.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

# Default bootstrap-file location (host path; container path is /config/...).
DEFAULT_SMARTLOAD_PATH = "config/smartload.yml"


class SmartLoadConfigError(ValueError):
    """The bootstrap file is malformed or carries an out-of-range value.

    The message always names the offending field path (e.g. ``strategy.name``)
    so a client can fix it without reading this module.
    """


# ── named-strategy → primitive mapping ──────────────────────────────────────────
#
# Industry vocabulary (round-robin, latency-aware, ai-hybrid, ...) translated to
# SmartLoad's composition primitives. This is the same table #150 documents in
# docs/features/named-strategies.md; it lives here as the single definition so
# the named-strategy endpoint can import it rather than restating it.
#
# operating_mode values are the canonical policy.yaml enum that policy-manager's
# validator accepts (VALID_OPERATING_MODES = classical-only | hybrid | rl-only);
# the strategy *names* are the industry-vocabulary aliases #150 documents. The
# classical strategies map to "classical-only" (pure NGINX routing, no
# decision-plane signal), not the loose "classical" shorthand in the issue table.
#
# rl_mode is None where the strategy does not engage the RL plane at all
# (operating_mode=classical-only) — the bootstrap then leaves RL_MODE at its
# default rather than pinning an irrelevant value.
STRATEGY_PRIMITIVES: Dict[str, Dict[str, Any]] = {
    "round-robin":       {"operating_mode": "classical-only", "safe_mode": False, "rl_mode": None},
    "least-connections": {"operating_mode": "classical-only", "safe_mode": False, "rl_mode": None},
    "latency-aware":     {"operating_mode": "hybrid",         "safe_mode": False, "rl_mode": "shadow"},
    "forecast-aware":    {"operating_mode": "hybrid",         "safe_mode": False, "rl_mode": "shadow"},
    "anomaly-aware":     {"operating_mode": "hybrid",         "safe_mode": False, "rl_mode": "shadow"},
    "ai-hybrid":         {"operating_mode": "hybrid",         "safe_mode": False, "rl_mode": "active"},
}

_LB_TYPES = {"nginx", "haproxy", "envoy", "alb"}
_ORCHESTRATOR_TYPES = {"docker", "kubernetes"}
_METRICS_PROVIDERS = {"prometheus"}


# ── small typed accessors ───────────────────────────────────────────────────────

def _require_mapping(value: Any, path: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise SmartLoadConfigError(f"{path} must be a mapping, got {type(value).__name__}")
    return value


def _section(raw: Mapping[str, Any], key: str) -> Dict[str, Any]:
    """Return raw[key] as a dict, or {} if absent. Raises if present-but-not-a-map."""
    if key not in raw or raw[key] is None:
        return {}
    return _require_mapping(raw[key], key)


def _positive_number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SmartLoadConfigError(f"{path} must be a number, got {value!r}")
    if value <= 0:
        raise SmartLoadConfigError(f"{path} must be > 0, got {value!r}")
    return value


def _positive_int(value: Any, path: str) -> int:
    num = _positive_number(value, path)
    if isinstance(num, float) and not num.is_integer():
        raise SmartLoadConfigError(f"{path} must be a whole number, got {value!r}")
    return int(num)


# ── validation ──────────────────────────────────────────────────────────────────

def validate(raw: Mapping[str, Any]) -> None:
    """Validate a parsed ``smartload.yml`` mapping in place, raising
    :class:`SmartLoadConfigError` (field-named) on the first problem.

    ``strategy.name`` is the one required knob — it selects the operating mode
    the whole decision plane keys off. Every other section is optional; absent
    sections fall back to the canonical ``policy.yaml`` defaults.
    """
    _require_mapping(raw, "<root>")

    strategy = _section(raw, "strategy")
    name = strategy.get("name")
    if not name:
        raise SmartLoadConfigError("strategy.name is required")
    if name not in STRATEGY_PRIMITIVES:
        allowed = ", ".join(sorted(STRATEGY_PRIMITIVES))
        raise SmartLoadConfigError(
            f"strategy.name {name!r} is not a known strategy (allowed: {allowed})"
        )
    if "cooldownSeconds" in strategy:
        _positive_int(strategy["cooldownSeconds"], "strategy.cooldownSeconds")
    if "evaluationIntervalSeconds" in strategy:
        _positive_int(strategy["evaluationIntervalSeconds"], "strategy.evaluationIntervalSeconds")

    slo = _section(raw, "slo")
    if "p95LatencyMs" in slo:
        _positive_number(slo["p95LatencyMs"], "slo.p95LatencyMs")
    if "errorRate" in slo:
        rate = slo["errorRate"]
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not (0 <= rate <= 1):
            raise SmartLoadConfigError(f"slo.errorRate must be a fraction in [0, 1], got {rate!r}")

    metrics = _section(raw, "metrics")
    provider = metrics.get("provider")
    if provider is not None and provider not in _METRICS_PROVIDERS:
        allowed = ", ".join(sorted(_METRICS_PROVIDERS))
        raise SmartLoadConfigError(
            f"metrics.provider {provider!r} is not supported (allowed: {allowed})"
        )

    lb = _section(raw, "loadBalancer")
    lb_type = lb.get("type")
    if lb_type is not None and lb_type not in _LB_TYPES:
        allowed = ", ".join(sorted(_LB_TYPES))
        raise SmartLoadConfigError(
            f"loadBalancer.type {lb_type!r} is not supported (allowed: {allowed})"
        )

    orch = _section(raw, "orchestrator")
    orch_type = orch.get("type")
    if orch_type is not None and orch_type not in _ORCHESTRATOR_TYPES:
        allowed = ", ".join(sorted(_ORCHESTRATOR_TYPES))
        raise SmartLoadConfigError(
            f"orchestrator.type {orch_type!r} is not supported (allowed: {allowed})"
        )

    backends = raw.get("backends")
    if backends is not None:
        if not isinstance(backends, list):
            raise SmartLoadConfigError("backends must be a list")
        for i, b in enumerate(backends):
            _require_mapping(b, f"backends[{i}]")
            if not b.get("name"):
                raise SmartLoadConfigError(f"backends[{i}].name is required")


# ── normalisation ───────────────────────────────────────────────────────────────

def to_policy(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the canonical ``policy.yaml`` fields derived from ``smartload.yml``.

    Only fields with a real runtime consumer today are emitted (operating mode,
    safe mode, SLO latency, autoscaler cooldown). ``policy_version`` is *not*
    set here — :func:`merge_policy` preserves the existing version so a re-render
    never rolls the live policy back.

    Assumes ``raw`` already passed :func:`validate`.
    """
    strategy = _section(raw, "strategy")
    prims = STRATEGY_PRIMITIVES[strategy["name"]]

    policy: Dict[str, Any] = {
        "operating_mode": prims["operating_mode"],
        "safe_mode": prims["safe_mode"],
    }

    slo = _section(raw, "slo")
    if "p95LatencyMs" in slo:
        policy["slo_p95_latency_ms"] = int(slo["p95LatencyMs"])
    if "cooldownSeconds" in strategy:
        policy["autoscaler_cooldown_seconds"] = int(strategy["cooldownSeconds"])

    return policy


def to_env(raw: Mapping[str, Any]) -> Dict[str, str]:
    """Return the env defaults derived from ``smartload.yml``.

    Limited to the vars an existing consumer reads: ``POLL_INTERVAL_SECONDS``
    (evaluation cadence) and ``RL_MODE`` (the RL-plane pin implied by the named
    strategy). Topology vars (metrics URL, LB endpoint) are intentionally left
    to the Helm packaging work (#133).

    Assumes ``raw`` already passed :func:`validate`.
    """
    env: Dict[str, str] = {}

    strategy = _section(raw, "strategy")
    if "evaluationIntervalSeconds" in strategy:
        env["POLL_INTERVAL_SECONDS"] = str(int(strategy["evaluationIntervalSeconds"]))

    rl_mode = STRATEGY_PRIMITIVES[strategy["name"]]["rl_mode"]
    if rl_mode is not None:
        env["RL_MODE"] = rl_mode

    return env


def merge_policy(existing: Optional[Mapping[str, Any]], raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Overlay the ``smartload.yml``-derived policy onto an existing ``policy.yaml``.

    Fields the bootstrap does not drive are carried through untouched, and
    ``policy_version`` is preserved (defaulting to 1 only when there is no
    existing policy at all) so seeding from ``smartload.yml`` never resets a
    live deployment's version counter.

    Assumes ``raw`` already passed :func:`validate`.
    """
    merged: Dict[str, Any] = dict(existing or {})
    merged.update(to_policy(raw))
    if not existing or "policy_version" not in existing:
        merged.setdefault("policy_version", 1)
    return merged


# ── file IO (the only part that needs PyYAML) ───────────────────────────────────

def read_file(path: str) -> Optional[Dict[str, Any]]:
    """Read + parse a ``smartload.yml`` file, returning the mapping, ``{}`` for
    an empty file, or ``None`` if the file does not exist (the backwards-compat
    signal: fall back to ``policy.yaml`` + ``.env``).

    The returned mapping is *not* validated — call :func:`validate` on it.
    """
    import os

    if not os.path.exists(path):
        return None
    import yaml  # lazy: keeps the pure functions import-light

    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {}
    return _require_mapping(data, "<root>")
