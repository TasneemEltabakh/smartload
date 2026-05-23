"""
services/rl-engine/training/simulator.py
─────────────────────────────────────────
BackendSimulator — wraps TraceReplayDataset to provide the Gym-compatible
reset()/step() interface used by SmartLoadEnv.

Design notes
────────────
queue_depth synthesis:
  RL_STATE_QUERY returns SUM(request_count) per backend per window, aliased as
  queue_depth in BackendState. The Alibaba trace does not provide a separate
  queue-depth signal; we proxy it with request count per window, which is
  exactly what the production query would return. No alpha-scaling needed —
  queue_depth IS the request count, the same semantic as the serving path.

Episode structure:
  reset(seed) samples a fresh start timestamp from the dataset.  Each call to
  step(action) advances one window.  The episode ends after episode_length
  steps OR when the dataset runs out of windows — whichever comes first.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_RL_ENGINE = Path(__file__).resolve().parents[1]
if str(_RL_ENGINE) not in sys.path:
    sys.path.insert(0, str(_RL_ENGINE))

from policy_base import BackendState    # noqa: E402
from training.dataset import TraceReplayDataset  # noqa: E402

DEFAULT_EPISODE_LENGTH: int = 200


class BackendSimulator:
    """Replay-based environment backend.

    Parameters
    ----------
    dataset:
        Loaded TraceReplayDataset instance.
    episode_length:
        Maximum number of steps per episode (hyperparameter; default 200).
    """

    def __init__(
        self,
        dataset: TraceReplayDataset,
        episode_length: int = DEFAULT_EPISODE_LENGTH,
    ) -> None:
        self._dataset        = dataset
        self.episode_length  = episode_length
        self._current_ts: int   = dataset._min_ts
        self._step_count: int   = 0
        self._rng: np.random.Generator = np.random.default_rng()

    # ── Gym-style API ──────────────────────────────────────────────────────────

    def reset(self, seed: int | None = None) -> list[BackendState]:
        """Sample a new episode start and return the initial state."""
        self._rng = np.random.default_rng(seed)
        self._current_ts = self._dataset.sample_start_ts(self._rng)
        self._step_count = 0
        return self._dataset.get_window(self._current_ts)

    def step(self, action: int) -> tuple[list[BackendState], bool]:
        """Advance one window. Returns (next_state, done).

        `action` is the backend index chosen by the policy (0-based, sorted by
        backend_id). The simulator is stateless with respect to routing — it
        replays the trace regardless of what action was taken. The reward
        function reads the consequence from next_state.
        """
        self._current_ts += self._dataset.window_ms
        self._step_count += 1
        next_state = self._dataset.get_window(self._current_ts)
        done = (
            self._step_count >= self.episode_length
            or self._current_ts + self._dataset.window_ms > self._dataset._max_ts
        )
        return next_state, done

    @property
    def current_state(self) -> list[BackendState]:
        return self._dataset.get_window(self._current_ts)
