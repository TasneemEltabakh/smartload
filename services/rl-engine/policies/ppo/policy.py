"""
services/rl-engine/policies/ppo/policy.py
──────────────────────────────────────────
PPOPolicy — serving plugin that wraps a contextual-bandit policy trained
with MaskablePPO on logged Alibaba traces.

Loaded by select_policy("ppo") in policy_base.py.

Why bandit, not RL: the offline simulator replays trace windows independent
of the agent's action, so there are no environment dynamics — the trained
policy is a contextual bandit (state → chosen backend) that happens to use
PPO machinery. See services/rl-engine/training/simulator.py docstring.

Constructor:
    PPOPolicy(
        confidence_threshold = 0.6,   # unused by PPO itself; kept for API parity
        exploration_rate     = 0.0,   # unused by PPO itself; kept for API parity
        operating_mode       = "shadow",  # "shadow" | "hybrid"
        model_path           = None,  # path to policy.zip (without .zip extension)
                                      # defaults to services/rl-engine/models/policy
    )

Operating mode semantics:
    "shadow"   — Full inference every cycle (obs + mask → logits → ranking)
                 but RoutingAction.mode is always "shadow".  The LB sidecar
                 ignores shadow envelopes.  Use for pre-production verification.
    "hybrid"   — Inference runs; RoutingAction.mode = "active".  The LB
                 sidecar applies the weights iff every other gate agrees
                 (see runloop.effective_mode).

    "learning" is accepted for backwards-compat with older policy.yaml
    schemas; it is treated as "hybrid".

Ranking shape (argmax-dominant weighting):
    The Discrete(N) action space PPO was optimised on chooses one backend
    per step. Using the softmax of logits as a weight distribution is not
    what the policy was trained for. Instead we route _DOMINANT_WEIGHT of
    traffic to the argmax backend and split the remainder evenly among
    other eligible backends — heavy emphasis on the policy's choice, small
    floor so NGINX health probing doesn't starve the rest.

Artifact loading:
    __init__ reads artifact_meta.json (same directory as policy.zip) to
    validate n_max_backends and restore NormParams.
    - n_max_backends mismatch → raises ValueError immediately.
    - Missing artifact_meta.json or missing policy.zip → logs a WARNING,
      sets _policy_ready=False, act() returns uniform shadow rankings over
      eligible backends.

reload() contract:
    Cheap in-place update of mutable runtime kwargs (operating_mode,
    confidence_threshold, exploration_rate). Does NOT re-read policy.zip;
    artifact swap still requires a container restart. This lets policy
    republishes on smartload.policy take effect without paying a torch
    model-load cost every time.

Serving / training separation:
    This file is COPY'd into the runtime Docker image.  It must NEVER import
    from training/*.  Only obs_builder and policy_base are allowed as local
    imports.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parents[2]   # policies/ → rl-engine/
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from obs_builder import (          # noqa: E402
    N_MAX_BACKENDS,
    NormParams,
    build_action_mask,
    build_observation,
)
from policy_base import (          # noqa: E402
    BackendState,
    Ranking,
    RoutingAction,
    RoutingPolicy,
    is_eligible,
)

_log = logging.getLogger(__name__)

_DEFAULT_NORM = NormParams(latency_scale=2000.0, request_count_scale=200.0)

# Argmax-dominant weighting (C2 from the RL review). Chosen backend gets
# this share; the remainder is split evenly across other eligibles. 0.7
# leaves a meaningful floor for NGINX health checks while still strongly
# preferring the policy's choice.
_DOMINANT_WEIGHT: float = 0.7


class PPOPolicy(RoutingPolicy):
    """MaskablePPO serving plugin.

    Loads policy.zip from models/ at construction time and runs inference on
    each act() call.  Gracefully degrades to uniform shadow rankings when the
    artifact is unavailable.
    """

    def __init__(
        self,
        confidence_threshold: float = 0.6,
        exploration_rate: float = 0.0,
        operating_mode: str = "shadow",
        model_path: str | None = None,
    ) -> None:
        self._confidence_threshold = confidence_threshold
        self._exploration_rate     = exploration_rate
        self._operating_mode       = self._normalise_operating_mode(operating_mode)
        self._model                = None
        self._norm: NormParams     = _DEFAULT_NORM
        self._policy_ready: bool   = False
        # Artifact kind: "discrete_argmax" (legacy MaskablePPO single-pick) or
        # "continuous_weights" (closed-loop PPO that emits a weight vector).
        self._kind: str            = "discrete_argmax"
        self._action_bound: float  = 10.0

        # Resolve model path: default is <rl-engine root>/models/policy
        if model_path is None:
            model_path = str(_HERE / "models" / "policy")
        mp = Path(model_path)
        if mp.suffix == ".zip":
            mp = mp.with_suffix("")
        self._model_zip = mp.with_suffix(".zip")
        self._meta_path = mp.parent / "artifact_meta.json"

        self._load_artifact()

    # ── RoutingPolicy interface ────────────────────────────────────────────────

    def act(self, state: list[BackendState]) -> RoutingAction:
        """Return a RoutingAction.

        When the model is ready:
          - Builds observation and action mask from state.
          - Runs a single forward pass to obtain per-backend logits.
          - Picks the argmax over the masked logits as the chosen backend.
          - Assigns argmax-dominant weights: chosen → _DOMINANT_WEIGHT,
            remaining mass split evenly across other eligibles.
          - Excludes unhealthy AND unknown backends from rankings.
          - mode = "active"  iff operating_mode == "hybrid"
          - mode = "shadow"  otherwise

        When the model is not ready (artifact missing):
          - Returns uniform shadow rankings over eligible backends.
          - Never crashes.

        Empty-rankings cases (intentional):
          - No live backends in state → empty rankings, mode="shadow".
          - All live backends unhealthy/unknown → empty rankings, mode="shadow".
            The anomaly detector's existing exclusions in the LB sidecar
            remain in effect; we do NOT manufacture a "best of the bad" pick.
        """
        if not self._policy_ready or self._model is None:
            return self._fallback_rankings(state)

        sorted_state = sorted(state, key=lambda s: s.backend_id)
        live_n = min(len(sorted_state), N_MAX_BACKENDS)
        if live_n == 0:
            return RoutingAction(mode="shadow", rankings=[])

        obs  = build_observation(state, N_MAX_BACKENDS, self._norm)
        mask = build_action_mask(state, N_MAX_BACKENDS)

        eligible = [s for s in sorted_state[:live_n] if is_eligible(s.health)]
        if not eligible:
            return RoutingAction(mode="shadow", rankings=[])

        # Closed-loop artifact: emit the full weight vector, not an argmax pick.
        if self._kind == "continuous_weights":
            rankings = self._continuous_weight_rankings(obs, sorted_state, live_n, eligible)
            mode = "active" if self._operating_mode == "hybrid" else "shadow"
            return RoutingAction(mode=mode, rankings=rankings)

        # Single forward pass — argmax over masked logits gives the chosen
        # backend in one shot. No second predict() call.
        raw_logits = self._get_logits(obs)
        masked_logits = np.where(mask, raw_logits, -np.inf)
        chosen_slot = int(np.argmax(masked_logits))

        # The mask is built from is_eligible, so the argmax should land on an
        # eligible slot. Defensive guard: if it ever drifts (mid-update race),
        # fall back to the highest-logit eligible.
        if chosen_slot < live_n and is_eligible(sorted_state[chosen_slot].health):
            chosen_backend = sorted_state[chosen_slot]
        else:
            chosen_backend = max(
                eligible,
                key=lambda b: float(raw_logits[sorted_state.index(b)]),
            )

        rankings = self._argmax_dominant_rankings(eligible, chosen_backend)

        mode = "active" if self._operating_mode == "hybrid" else "shadow"
        return RoutingAction(mode=mode, rankings=rankings)

    def reload(self, **kwargs) -> None:
        """Cheap in-place runtime-kwarg update — no artifact reload.

        Called by app.py on every smartload.policy publish. Updates the
        mutable knobs (operating_mode, confidence_threshold, exploration_rate)
        without re-reading policy.zip from disk or rebuilding the torch
        graph. Unknown kwargs are silently ignored so the policy plugin
        can evolve independently of the engine_policy payload shape.
        """
        if "operating_mode" in kwargs:
            self._operating_mode = self._normalise_operating_mode(kwargs["operating_mode"])
        if "confidence_threshold" in kwargs:
            try:
                self._confidence_threshold = float(kwargs["confidence_threshold"])
            except (TypeError, ValueError):
                pass
        if "exploration_rate" in kwargs:
            try:
                self._exploration_rate = float(kwargs["exploration_rate"])
            except (TypeError, ValueError):
                pass

    # ── properties ────────────────────────────────────────────────────────────

    @property
    def policy_ready(self) -> bool:
        return self._policy_ready

    # ── internal helpers ──────────────────────────────────────────────────────

    def _load_artifact(self) -> None:
        """Read artifact_meta.json + load policy.zip.

        Raises:
            ValueError — if artifact_meta.json exists but n_max_backends mismatches.
        Sets _policy_ready=False (no exception) for all other load failures.
        """
        # Step 1: validate meta
        if not self._meta_path.exists():
            _log.warning(
                "PPOPolicy: artifact_meta.json not found at %s — policy not ready",
                self._meta_path,
            )
            return

        meta = json.loads(self._meta_path.read_text())
        stored_n = int(meta.get("n_max_backends", N_MAX_BACKENDS))
        if stored_n != N_MAX_BACKENDS:
            raise ValueError(
                f"PPOPolicy: artifact n_max_backends={stored_n} does not match "
                f"runtime N_MAX_BACKENDS={N_MAX_BACKENDS}. "
                "Rebuild the artifact with the correct backend count."
            )
        self._norm = NormParams.from_dict(meta["norm_params"])
        self._kind = str(meta.get("policy_kind", "discrete_argmax"))
        self._action_bound = float(meta.get("action_bound", 10.0))

        # Step 2: load model
        if not self._model_zip.exists():
            _log.warning(
                "PPOPolicy: %s not found — policy not ready",
                self._model_zip,
            )
            return

        try:
            if self._kind == "continuous_weights":
                # Closed-loop artifact: a plain SB3 PPO with a Box(weights) head.
                from stable_baselines3 import PPO
                self._model = PPO.load(str(self._model_zip.with_suffix("")))
            else:
                from sb3_contrib import MaskablePPO  # local import — not in serving image until N2.5
                self._model = MaskablePPO.load(str(self._model_zip.with_suffix("")))
            self._policy_ready = True
            _log.info("PPOPolicy: loaded %s (kind=%s, operating_mode=%s)",
                      self._model_zip, self._kind, self._operating_mode)
        except Exception as exc:  # noqa: BLE001
            _log.error("PPOPolicy: failed to load model: %s", exc)
            self._model = None
            self._policy_ready = False

    def _get_logits(self, obs: np.ndarray) -> np.ndarray:
        """Return raw logits for all N_MAX_BACKENDS actions via a single forward pass.

        Falls back to a zero vector if torch / the SB3 policy API is unavailable.
        """
        try:
            import torch
            obs_t = torch.as_tensor(obs[None], dtype=torch.float32)
            dist  = self._model.policy.get_distribution(obs_t)
            logits = dist.distribution.logits.detach().cpu().numpy().flatten()
            return logits[:N_MAX_BACKENDS]
        except Exception as exc:  # noqa: BLE001
            _log.debug("PPOPolicy: logit extraction failed (%s) — using zeros", exc)
            result = np.zeros(N_MAX_BACKENDS, dtype=float)
            return result

    def _fallback_rankings(self, state: list[BackendState]) -> RoutingAction:
        """Uniform shadow rankings used when the model is not ready.

        Falls back to all live backends only if zero are eligible — that
        guarantees the envelope still carries something for operator
        explainability when telemetry is the problem.
        """
        eligible = [b for b in state if is_eligible(b.health)] or list(state)
        n = len(eligible)
        if n == 0:
            return RoutingAction(mode="shadow", rankings=[])
        score = 1.0 / n
        rankings = [Ranking(backend_id=b.backend_id, score=score) for b in eligible]
        return RoutingAction(mode="shadow", rankings=rankings)

    @staticmethod
    def _normalise_operating_mode(raw: str) -> str:
        """Canonicalise the operating_mode kwarg.

        Accepts "shadow" / "hybrid" verbatim. Maps the deprecated "learning"
        value to "hybrid" so legacy policy.yaml configs and saved fixtures
        keep working without a thrown error. Any other value defaults to
        "shadow" — the safest behaviour when the input is ambiguous.
        """
        if raw == "hybrid" or raw == "learning":
            return "hybrid"
        if raw == "shadow":
            return "shadow"
        return "shadow"

    def _continuous_weight_rankings(
        self,
        obs: np.ndarray,
        sorted_state: list[BackendState],
        live_n: int,
        eligible: list[BackendState],
    ) -> list[Ranking]:
        """Closed-loop serving: the deterministic action IS a per-backend weight
        logit vector. Masked-softmax over eligible slots → a routing
        distribution (no argmax concentration). Renormalised over eligibles."""
        raw, _ = self._model.predict(np.asarray(obs, dtype=np.float32), deterministic=True)
        raw = np.asarray(raw, dtype=float).flatten()[:N_MAX_BACKENDS]
        mask = build_action_mask(sorted_state, N_MAX_BACKENDS)
        masked = np.where(mask, raw, -1e9)
        masked = masked - masked.max()
        exp = np.exp(masked)
        total = float(exp.sum())
        weights = exp / total if np.isfinite(total) and total > 0 else mask.astype(float)

        eligible_ids = {b.backend_id for b in eligible}
        out = [
            Ranking(backend_id=s.backend_id, score=float(weights[i]))
            for i, s in enumerate(sorted_state[:live_n])
            if s.backend_id in eligible_ids
        ]
        ssum = sum(r.score for r in out) or 1.0
        out = [Ranking(backend_id=r.backend_id, score=r.score / ssum) for r in out]
        out.sort(key=lambda r: r.score, reverse=True)
        return out

    @staticmethod
    def _argmax_dominant_rankings(
        eligible: list[BackendState],
        chosen: BackendState,
    ) -> list[Ranking]:
        """Assign _DOMINANT_WEIGHT to chosen and split the remainder evenly
        across the other eligibles. Sorted descending by score so consumers
        can rely on rankings[0] being the chosen backend.
        """
        n_other = len(eligible) - 1
        if n_other <= 0:
            return [Ranking(backend_id=chosen.backend_id, score=1.0)]
        floor = (1.0 - _DOMINANT_WEIGHT) / n_other
        out: list[Ranking] = []
        for b in eligible:
            score = _DOMINANT_WEIGHT if b.backend_id == chosen.backend_id else floor
            out.append(Ranking(backend_id=b.backend_id, score=score))
        out.sort(key=lambda r: r.score, reverse=True)
        return out
