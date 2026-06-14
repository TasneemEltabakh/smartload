"""
experiments/rl-routing-bench/contenders.py
───────────────────────────────────────────
Adapter layer that wraps every routing contender into one common signature:

    weight_fn(obs: np.ndarray, sim_state: list[BackendState]) -> np.ndarray

`obs` is the normalised observation built with the contender's OWN norm_params
(never a global norm); `sim_state` is the live list[BackendState] from the
closed-loop sim. The returned weight vector has length N_MAX_BACKENDS, is
non-negative and sums to 1 over the live/eligible backends, exactly as the
serving layer would route.

Contenders (8):
  Learned (loaded via SB3 in the numpy-2 interpreter):
    policy.zip      MaskablePPO  Discrete(5)  -> discrete_argmax
    candidate_v2    PPO          Box(5)       -> continuous_weights
    candidate_a2c   A2C          Box(5)       -> continuous_weights
    candidate_sac   SAC          Box(5)       -> continuous_weights
    candidate_dqn   DQN          Discrete(4)  -> discrete_templates
  Classical (real policy classes, no model):
    round_robin      RoundRobinPolicy
    least_connections LeastConnectionsPolicy
    random_shadow    RandomShadowPolicy

Each learned adapter mirrors the EXACT weight-building rule the serving layer
(policies/ppo/policy.py) uses for that artifact kind, and each scoring rule
(action_to_weights / template_weights / argmax-dominant) is imported from the
frozen training/serving code rather than reimplemented.

No model code under services/ is modified; this file only imports it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

# Make the frozen rl-engine importable (obs_builder, policy_base, training/*).
_REPO = Path(__file__).resolve().parents[2]
_RL_ENGINE = _REPO / "services" / "rl-engine"
if str(_RL_ENGINE) not in sys.path:
    sys.path.insert(0, str(_RL_ENGINE))

from obs_builder import (                       # noqa: E402
    N_MAX_BACKENDS,
    NormParams,
    build_action_mask,
    build_observation,
)
from policy_base import BackendState, is_eligible            # noqa: E402
from training.env_v2 import action_to_weights               # noqa: E402
from routing_templates import template_weights              # noqa: E402

# Argmax-dominant share used by the shipped serving layer (policies/ppo/policy.py
# _DOMINANT_WEIGHT). The chosen backend gets this share; the remainder is split
# evenly across the other eligible backends.
_DOMINANT_WEIGHT: float = 0.7

_MODELS = _RL_ENGINE / "models"


# ── helpers ────────────────────────────────────────────────────────────────────

def _live_n(sim_state: list[BackendState]) -> int:
    return min(len(sim_state), N_MAX_BACKENDS)


def _rankings_to_weights(rankings, sim_state: list[BackendState]) -> np.ndarray:
    """Convert a classical RoutingAction.rankings (backend_id, score) into a
    normalised weight vector aligned to the sim's backend slot order (sorted by
    backend_id, the canonical observation order). Only eligible backends carry
    weight; if a ranking references an ineligible/absent backend it is dropped.

    Scores are the policy's own descending ranks; we treat them as relative
    routing shares and renormalise — the standard "rankings -> weights" mapping
    the LB sidecar applies."""
    sorted_ids = [s.backend_id for s in sorted(sim_state, key=lambda s: s.backend_id)]
    eligible_ids = {s.backend_id for s in sim_state if is_eligible(s.health)}
    score_by_id = {r.backend_id: float(r.score) for r in rankings}
    w = np.zeros(N_MAX_BACKENDS, dtype=float)
    for i, bid in enumerate(sorted_ids[:N_MAX_BACKENDS]):
        if bid in eligible_ids and bid in score_by_id and score_by_id[bid] > 0:
            w[i] = score_by_id[bid]
    total = w.sum()
    if total > 0:
        return w / total
    # No eligible scored backend: fall back to uniform over eligible slots.
    mask = build_action_mask(sim_state, N_MAX_BACKENDS).astype(float)
    return mask / mask.sum() if mask.sum() else np.ones(N_MAX_BACKENDS) / N_MAX_BACKENDS


# ── learned-model adapters ──────────────────────────────────────────────────────

def make_continuous(model, norm: NormParams):
    """v2 / a2c / sac: predict a raw Box action, masked-softmax -> weights.
    Mirrors PPOPolicy._continuous_weight_rankings (= env_v2.action_to_weights)."""
    def fn(obs: np.ndarray, sim_state: list[BackendState]) -> np.ndarray:
        raw, _ = model.predict(np.asarray(obs, dtype=np.float32), deterministic=True)
        raw = np.asarray(raw, dtype=float).flatten()[:N_MAX_BACKENDS]
        mask = build_action_mask(sim_state, N_MAX_BACKENDS)
        if not mask.any():
            mask = np.zeros(N_MAX_BACKENDS, dtype=bool)
            mask[0] = True
        return action_to_weights(raw, mask)
    return fn


def make_templates(model, norm: NormParams):
    """dqn: predict a template id -> routing_templates.template_weights.
    Mirrors PPOPolicy._template_rankings."""
    def fn(obs: np.ndarray, sim_state: list[BackendState]) -> np.ndarray:
        raw, _ = model.predict(np.asarray(obs, dtype=np.float32), deterministic=True)
        template = int(np.asarray(raw).reshape(-1)[0])
        n = _live_n(sim_state)
        w_live = template_weights(template, sim_state, n)   # length n, slot order
        w = np.zeros(N_MAX_BACKENDS, dtype=float)
        w[:n] = w_live
        total = w.sum()
        return w / total if total > 0 else np.ones(N_MAX_BACKENDS) / N_MAX_BACKENDS
    return fn


def make_discrete_argmax(model, norm: NormParams):
    """policy.zip (MaskablePPO): masked-argmax over logits -> argmax-dominant
    weight vector. Mirrors PPOPolicy._get_logits + _argmax_dominant_rankings:
    chosen backend gets _DOMINANT_WEIGHT, remainder split evenly across other
    eligibles."""
    import torch

    def _logits(obs: np.ndarray) -> np.ndarray:
        obs_t = torch.as_tensor(np.asarray(obs, dtype=np.float32)[None], dtype=torch.float32)
        dist = model.policy.get_distribution(obs_t)
        return dist.distribution.logits.detach().cpu().numpy().flatten()[:N_MAX_BACKENDS]

    def fn(obs: np.ndarray, sim_state: list[BackendState]) -> np.ndarray:
        sorted_state = sorted(sim_state, key=lambda s: s.backend_id)
        live_n = _live_n(sorted_state)
        mask = build_action_mask(sim_state, N_MAX_BACKENDS)
        eligible_slots = [i for i in range(live_n) if is_eligible(sorted_state[i].health)]
        w = np.zeros(N_MAX_BACKENDS, dtype=float)
        if not eligible_slots:
            # All-unhealthy: route the lone least-bad slot (serving fallback parity).
            best = min(range(live_n), key=lambda i: sorted_state[i].latency_ms) if live_n else 0
            w[best] = 1.0
            return w
        raw_logits = _logits(obs)
        masked = np.where(mask, raw_logits, -np.inf)
        chosen = int(np.argmax(masked))
        if chosen not in eligible_slots:
            chosen = max(eligible_slots, key=lambda i: float(raw_logits[i]))
        n_other = len(eligible_slots) - 1
        if n_other <= 0:
            w[chosen] = 1.0
            return w
        floor = (1.0 - _DOMINANT_WEIGHT) / n_other
        for i in eligible_slots:
            w[i] = _DOMINANT_WEIGHT if i == chosen else floor
        total = w.sum()
        return w / total if total > 0 else w
    return fn


# ── classical-policy adapters (drive the REAL policy classes) ────────────────────

def make_classical(policy_obj):
    """round_robin / least_connections / random_shadow: call the real
    policy_obj.act(state) -> RoutingAction, convert rankings -> weights.
    `policy_obj` is a stateful instance (round-robin keeps a rotation pointer),
    so one instance is constructed per (contender, scenario, seed-band, episode)
    by the harness to keep determinism."""
    def fn(obs: np.ndarray, sim_state: list[BackendState]) -> np.ndarray:
        action = policy_obj.act(sim_state)
        return _rankings_to_weights(action.rankings, sim_state)
    return fn


# ── model loading registry ──────────────────────────────────────────────────────

def load_learned():
    """Load all five learned contenders with their own norm_params.
    Returns a dict name -> (weight_fn_factory_result, NormParams, kind, meta_path).
    Factory is already bound to the model so the harness just calls fn(obs, state).
    """
    import json
    from stable_baselines3 import PPO, A2C, SAC, DQN
    from sb3_contrib import MaskablePPO

    out = {}

    def _norm(meta_path: Path) -> NormParams:
        d = json.loads(meta_path.read_text())
        return NormParams.from_dict(d["norm_params"])

    # shipped MaskablePPO
    p = _MODELS / "policy"
    norm = _norm(_MODELS / "artifact_meta.json")
    out["policy_shipped"] = (make_discrete_argmax(MaskablePPO.load(str(p)), norm),
                             norm, "discrete_argmax", str(_MODELS / "artifact_meta.json"))

    # candidate_v2 PPO
    d = _MODELS / "candidate_v2"
    norm = _norm(d / "artifact_meta.json")
    out["candidate_v2"] = (make_continuous(PPO.load(str(d / "policy")), norm),
                           norm, "continuous_weights", str(d / "artifact_meta.json"))

    # candidate_a2c A2C
    d = _MODELS / "candidate_a2c"
    norm = _norm(d / "artifact_meta.json")
    out["candidate_a2c"] = (make_continuous(A2C.load(str(d / "policy")), norm),
                            norm, "continuous_weights", str(d / "artifact_meta.json"))

    # candidate_sac SAC
    d = _MODELS / "candidate_sac"
    norm = _norm(d / "artifact_meta.json")
    out["candidate_sac"] = (make_continuous(SAC.load(str(d / "policy")), norm),
                            norm, "continuous_weights", str(d / "artifact_meta.json"))

    # candidate_dqn DQN
    d = _MODELS / "candidate_dqn"
    norm = _norm(d / "artifact_meta.json")
    out["candidate_dqn"] = (make_templates(DQN.load(str(d / "policy")), norm),
                            norm, "discrete_templates", str(d / "artifact_meta.json"))

    return out


def classical_factory():
    """Return dict name -> (constructor, NormParams). The constructor builds a
    FRESH policy instance (classical policies use a global norm only to build the
    observation, which they ignore; we pass DEFAULT_NORM so obs construction is
    well-defined and identical across episodes). random_shadow is seeded per
    episode by the harness for determinism."""
    from policies.round_robin.policy import RoundRobinPolicy
    from policies.least_connections.policy import LeastConnectionsPolicy
    from policies.random_shadow.policy import RandomShadowPolicy

    # Classical policies do not consume obs; any fixed norm is fine for building
    # it. Use the shipped global norm so the harness always has a defined obs.
    cls_norm = NormParams(latency_scale=200.0, request_count_scale=100.0)
    return {
        "round_robin": (lambda seed=None: RoundRobinPolicy(), cls_norm),
        "least_connections": (lambda seed=None: LeastConnectionsPolicy(), cls_norm),
        "random_shadow": (lambda seed=None: RandomShadowPolicy(seed=seed), cls_norm),
    }
