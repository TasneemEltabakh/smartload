"""
services/rl-engine/policies/ppo/policy.py
──────────────────────────────────────────
PPOPolicy — serving plugin that wraps a trained MaskablePPO artifact.

Loaded by select_policy("ppo") in policy_base.py.

Constructor:
    PPOPolicy(
        confidence_threshold = 0.6,   # unused by PPO itself; kept for API parity
        exploration_rate     = 0.0,   # unused by PPO itself; kept for API parity
        operating_mode       = "shadow",  # "shadow" | "hybrid" | "learning"
        model_path           = None,  # path to policy.zip (without .zip extension)
                                      # defaults to services/rl-engine/models/policy
    )

Operating mode semantics (Amendment B):
    "shadow"   — Full inference every cycle (obs + mask → logits → rankings)
                 but RoutingAction.mode is always "shadow".  The LB sidecar
                 ignores shadow envelopes.  Use for pre-production verification.
    "hybrid"   — Inference runs; RoutingAction.mode = "active".
    "learning" — Same as hybrid from PPOPolicy's perspective.

Artifact loading:
    __init__ reads artifact_meta.json (same directory as policy.zip) to
    validate n_max_backends and restore NormParams.
    - n_max_backends mismatch → raises ValueError immediately.
    - Missing artifact_meta.json or missing policy.zip → logs a WARNING,
      sets _policy_ready=False, act() returns uniform shadow rankings.

reload() contract (Amendment I):
    Raises NotImplementedError.  Artifact swap requires container restart.

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
    all_masked_fallback,
    build_action_mask,
    build_observation,
)
from policy_base import BackendState, Ranking, RoutingAction, RoutingPolicy  # noqa: E402

_log = logging.getLogger(__name__)

_DEFAULT_NORM = NormParams(latency_scale=2000.0, request_count_scale=200.0)


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
        self._operating_mode       = operating_mode
        self._model                = None
        self._norm: NormParams     = _DEFAULT_NORM
        self._policy_ready: bool   = False

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
          - Runs a single forward pass to obtain per-backend logit scores.
          - Excludes unhealthy backends from rankings.
          - mode = "active"  iff operating_mode ∈ {"hybrid", "learning"}
          - mode = "shadow"  otherwise

        When the model is not ready (artifact missing):
          - Returns uniform shadow rankings over non-unhealthy backends.
          - Never crashes.
        """
        if not self._policy_ready or self._model is None:
            return self._fallback_rankings(state)

        obs  = build_observation(state, N_MAX_BACKENDS, self._norm)
        mask = build_action_mask(state, N_MAX_BACKENDS)
        if not mask.any():
            mask = all_masked_fallback(state, N_MAX_BACKENDS)

        # Run inference — predict() returns the argmax action under the mask
        action_idx, _ = self._model.predict(obs, action_masks=mask, deterministic=True)
        action_idx = int(action_idx)

        # Build full ranking from logit vector (single forward pass)
        raw_logits = self._get_logits(obs)

        sorted_state = sorted(state, key=lambda s: s.backend_id)
        live_n = min(len(sorted_state), N_MAX_BACKENDS)

        # Collect non-unhealthy backends with their logit scores
        ranked: list[tuple[BackendState, float]] = []
        for i in range(live_n):
            s = sorted_state[i]
            if s.health == "unhealthy":
                continue
            ranked.append((s, float(raw_logits[i])))

        if not ranked:
            # Edge case: all backends unhealthy — include the best one anyway
            best_idx = int(np.argmax(raw_logits[:live_n]))
            if live_n > 0:
                ranked = [(sorted_state[best_idx], float(raw_logits[best_idx]))]

        # Convert logits to probability-like scores via softmax
        if ranked:
            logit_vals = np.array([lv for _, lv in ranked])
            exp_l = np.exp(logit_vals - logit_vals.max())
            scores = exp_l / exp_l.sum()
            rankings = [
                Ranking(backend_id=s.backend_id, score=float(sc))
                for (s, _), sc in zip(ranked, scores)
            ]
            rankings.sort(key=lambda r: r.score, reverse=True)
        else:
            rankings = []

        mode = (
            "active"
            if self._operating_mode in ("hybrid", "learning") and self._policy_ready
            else "shadow"
        )
        return RoutingAction(mode=mode, rankings=rankings)

    def reload(self) -> None:
        raise NotImplementedError(
            "hot-reload deferred; restart container to swap artifact"
        )

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

        # Step 2: load model
        if not self._model_zip.exists():
            _log.warning(
                "PPOPolicy: %s not found — policy not ready",
                self._model_zip,
            )
            return

        try:
            from sb3_contrib import MaskablePPO  # local import — not in serving image until N2.5
            self._model = MaskablePPO.load(str(self._model_zip.with_suffix("")))
            self._policy_ready = True
            _log.info("PPOPolicy: loaded %s (operating_mode=%s)", self._model_zip, self._operating_mode)
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
        """Uniform shadow rankings used when the model is not ready."""
        eligible = [b for b in state if b.health != "unhealthy"] or state
        n = len(eligible)
        score = 1.0 / max(n, 1)
        rankings = [Ranking(backend_id=b.backend_id, score=score) for b in eligible]
        return RoutingAction(mode="shadow", rankings=rankings)
