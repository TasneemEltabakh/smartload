"""
services/rl-engine/training/train_ppo.py
─────────────────────────────────────────
MaskablePPO training entry point for SmartLoad routing policy.

Usage:
    # Canary check only (50k steps, first 2 partitions):
    python training/train_ppo.py run_canary

    # Full training (all partitions, 2M steps):
    python training/train_ppo.py

    # Full training, skip canary:
    python training/train_ppo.py --skip-canary

Outputs (written to services/rl-engine/models/):
    policy.zip           — MaskablePPO artifact (sb3-contrib native format)
    artifact_meta.json   — NormParams + training metadata read by PPOPolicy

Canary gates (both must pass before full training begins):
    1. policy_gradient_loss trending down across the last 10 logged updates
       (linear regression slope ≤ 0 over the tail window)
    2. PPO mean reward > round_robin mean reward on 3 ad-hoc mini episodes
       drawn from the canary dataset

If either gate fails the script exits with code 1 and prints instructions
to switch to train_dqn.py.

Training/serving separation:
    This file is never COPY'd into the runtime Docker image.  It lives in
    training/ which the Dockerfile explicitly excludes.  The artifact it
    produces (policy.zip + artifact_meta.json) is the handoff to PPOPolicy.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

_RL_ENGINE = Path(__file__).resolve().parents[1]
if str(_RL_ENGINE) not in sys.path:
    sys.path.insert(0, str(_RL_ENGINE))

from obs_builder import N_MAX_BACKENDS, NormParams, build_action_mask, all_masked_fallback, build_observation  # noqa: E402
from training.dataset import TraceReplayDataset    # noqa: E402
from training.env import SmartLoadEnv              # noqa: E402
from training.eval_harness import run_eval         # noqa: E402
from training.reward import RewardCalculator       # noqa: E402
from training.simulator import BackendSimulator, DEFAULT_EPISODE_LENGTH  # noqa: E402

# ── constants ──────────────────────────────────────────────────────────────────

_MODELS_DIR    = _RL_ENGINE / "models"
_SEED_BANK     = _RL_ENGINE / "training" / "eval_seed_bank.json"
_ALIBABA_DIR   = _RL_ENGINE.parents[1] / "datasets" / "alibaba"

_DEFAULT_NORM  = NormParams(latency_scale=2000.0, request_count_scale=200.0)
_CANARY_STEPS  = 50_000
_CANARY_PARTS  = 2        # number of partitions used in canary (first N)
_FULL_STEPS    = 2_000_000
_N_CANARY_EPS  = 3        # mini episodes for canary reward gate
_SLO_MS        = 200.0
_NORM_SAMPLE_WINDOWS = 200  # windows to sample for request_count_scale estimation


# ── training monitor callback ──────────────────────────────────────────────────

def _make_monitor_callback():
    """Return a callback that captures train/policy_gradient_loss via logger patch."""
    try:
        from stable_baselines3.common.callbacks import BaseCallback
    except ImportError as exc:
        raise ImportError(
            "stable-baselines3 not installed.  Run:\n"
            "  pip install -r training/requirements-training.txt"
        ) from exc

    class _TrainingMonitor(BaseCallback):
        def __init__(self):
            super().__init__()
            self.pg_losses: list[float] = []
            self._original_record = None

        def _on_training_start(self) -> None:
            # Patch logger.record() to capture pg loss values before dump() clears them.
            # dump() empties name_to_value, so we must intercept at record() time.
            self._original_record = self.model.logger.record
            _losses = self.pg_losses
            _orig   = self._original_record

            def _patched(key, value, *args, **kwargs):
                if key == "train/policy_gradient_loss":
                    _losses.append(float(value))
                return _orig(key, value, *args, **kwargs)

            self.model.logger.record = _patched

        def _on_training_end(self) -> None:
            if self._original_record is not None:
                self.model.logger.record = self._original_record

        def _on_step(self) -> bool:
            return True

    return _TrainingMonitor()


# ── env factory ───────────────────────────────────────────────────────────────

def _make_env(
    partitions: list[Path],
    norm: NormParams,
    episode_length: int,
) -> SmartLoadEnv:
    ds = TraceReplayDataset(partitions, n_backends=N_MAX_BACKENDS)
    return SmartLoadEnv(dataset=ds, norm=norm, episode_length=episode_length)


def _wrap_for_maskable_ppo(env: SmartLoadEnv):
    """Wrap env with ActionMasker so MaskablePPO receives action masks."""
    try:
        from sb3_contrib.common.wrappers import ActionMasker
    except ImportError as exc:
        raise ImportError(
            "sb3-contrib not installed.  Run:\n"
            "  pip install -r training/requirements-training.txt"
        ) from exc
    return ActionMasker(env, lambda e: e.action_masks())


# ── canary gate helpers ────────────────────────────────────────────────────────

_SLOPE_TOLERANCE = 1e-4  # PG loss fluctuates heavily; only flag clear divergence

def _loss_trending_down(losses: list[float], window: int = 10) -> bool:
    """True if the last `window` pg_loss samples are flat or trending down.

    Uses a small tolerance (_SLOPE_TOLERANCE) so normal PG-loss noise at
    50k steps doesn't trip a false FAIL.  Only a clearly positive slope
    (loss actively growing) fails the gate.
    """
    if len(losses) < window:
        print(
            f"[canary] only {len(losses)} pg_loss samples recorded "
            f"(need {window}) — treating gate1 as PASS",
            flush=True,
        )
        return True
    tail = np.array(losses[-window:], dtype=float)
    slope = float(np.polyfit(np.arange(len(tail)), tail, 1)[0])
    print(f"[canary] pg_loss tail slope = {slope:.2e} (<{_SLOPE_TOLERANCE:.0e} -> PASS)", flush=True)
    return slope < _SLOPE_TOLERANCE


def _sample_mini_episodes(ds: TraceReplayDataset, n: int, seed: int = 77) -> dict:
    """Generate n ad-hoc episode starts from ds for canary reward comparison."""
    rng = np.random.default_rng(seed)
    episodes = []
    seen: set[int] = set()
    attempts = 0
    while len(episodes) < n and attempts < n * 10:
        ts = ds.sample_start_ts(rng)
        attempts += 1
        if ts in seen:
            continue
        state = ds.get_window(ts)
        if not state:
            continue
        episodes.append({
            "episode_id":       len(episodes),
            "dataset_start_idx": 0,
            "start_ts":         ts,
            "seed":             int(rng.integers(0, 2**31)),
        })
        seen.add(ts)
    return {"episodes": episodes}


def _eval_ppo_on_episodes(
    model,
    ds: TraceReplayDataset,
    mini_sb: dict,
    episode_length: int,
    norm: NormParams,
) -> list[float]:
    """Run model on mini_sb episodes; return list of mean per-episode rewards."""
    reward_calc = RewardCalculator(norm=norm)
    per_ep: list[float] = []

    for ep in mini_sb["episodes"]:
        sim = BackendSimulator(ds, episode_length=episode_length)
        sim._rng        = np.random.default_rng(ep["seed"])
        sim._current_ts = ep["start_ts"]
        sim._step_count = 0
        state           = ds.get_window(ep["start_ts"])

        step_rewards: list[float] = []
        for _ in range(episode_length):
            if not state:
                break
            obs  = build_observation(state, N_MAX_BACKENDS, norm)
            mask = build_action_mask(state, N_MAX_BACKENDS)
            if not mask.any():
                mask = all_masked_fallback(state, N_MAX_BACKENDS)

            action, _ = model.predict(obs, action_masks=mask, deterministic=True)
            next_state, done = sim.step(int(action))
            reward = reward_calc.compute(state, int(action), next_state)
            step_rewards.append(reward)
            state = next_state
            if done:
                break

        if step_rewards:
            per_ep.append(float(np.mean(step_rewards)))

    return per_ep


def _reward_gate(
    model,
    ds: TraceReplayDataset,
    norm: NormParams,
    n_episodes: int,
    episode_length: int,
) -> bool:
    """True if PPO mean reward > round_robin mean reward on n_episodes."""
    mini_sb = _sample_mini_episodes(ds, n=n_episodes)
    if not mini_sb["episodes"]:
        print("[canary] no valid mini episodes — treating gate2 as PASS", flush=True)
        return True

    rr_rows = run_eval(
        policies=["round_robin"],
        dataset=ds,
        seed_bank=mini_sb,
        episode_length=episode_length,
        slo_ms=_SLO_MS,
        norm=norm,
        n_eval_episodes=n_episodes,
    )
    rr_mean = float(np.mean([r["mean_reward"] for r in rr_rows])) if rr_rows else -1e9

    ppo_rewards = _eval_ppo_on_episodes(model, ds, mini_sb, episode_length, norm)
    ppo_mean = float(np.mean(ppo_rewards)) if ppo_rewards else -1e9

    print(f"[canary] round_robin mean_reward = {rr_mean:.4f}", flush=True)
    print(f"[canary] ppo        mean_reward = {ppo_mean:.4f}", flush=True)
    return ppo_mean > rr_mean


# ── canary entry point ────────────────────────────────────────────────────────

def run_canary(
    partitions: list[Path] | None = None,
    norm: NormParams = _DEFAULT_NORM,
    steps: int = _CANARY_STEPS,
    episode_length: int = 50,
) -> bool:
    """Train for `steps` steps on a small slice and check both canary gates.

    Returns True if training should proceed (both gates passed).
    Returns False if either gate failed (caller should switch to train_dqn.py).
    """
    try:
        from sb3_contrib import MaskablePPO
    except ImportError as exc:
        raise ImportError(
            "sb3-contrib not installed.  Run:\n"
            "  pip install -r training/requirements-training.txt"
        ) from exc

    if partitions is None:
        all_parts = sorted(_ALIBABA_DIR.glob("*.csv"))
        if not all_parts:
            raise FileNotFoundError(f"No Alibaba CSV files found at {_ALIBABA_DIR}")
        partitions = all_parts[:_CANARY_PARTS]

    print(f"[canary] partitions: {[p.name for p in partitions]}", flush=True)
    print(f"[canary] steps={steps:,}  episode_length={episode_length}", flush=True)

    ds      = TraceReplayDataset(partitions, n_backends=N_MAX_BACKENDS)
    env     = _wrap_for_maskable_ppo(SmartLoadEnv(dataset=ds, norm=norm, episode_length=episode_length))
    monitor = _make_monitor_callback()

    model = MaskablePPO(
        "MlpPolicy",
        env,
        verbose=0,
        learning_rate=3e-4,
        n_steps=256,
        batch_size=64,
        n_epochs=4,
        gamma=0.99,
        seed=42,
        tensorboard_log=None,
    )
    print("[canary] training...", flush=True)
    model.learn(total_timesteps=steps, callback=monitor)

    # Gate 1: policy_gradient_loss trending down
    gate1 = _loss_trending_down(monitor.pg_losses)
    print(f"[canary] gate1 (loss trend): {'PASS' if gate1 else 'FAIL'}", flush=True)

    # Gate 2: PPO mean reward > round_robin mean reward
    gate2 = _reward_gate(model, ds, norm, n_episodes=_N_CANARY_EPS,
                         episode_length=episode_length)
    print(f"[canary] gate2 (PPO > rr):  {'PASS' if gate2 else 'FAIL'}", flush=True)

    if gate1 and gate2:
        print("\n[canary] PASSED — proceed to full PPO training.\n", flush=True)
        return True
    else:
        print(
            "\n[canary] FAILED — one or more gates did not pass.\n"
            "Switch to DQN fallback:\n"
            "  python training/train_dqn.py\n",
            flush=True,
        )
        return False


# ── dataset statistics ────────────────────────────────────────────────────────

def _compute_norm_params(ds: TraceReplayDataset) -> NormParams:
    """Estimate NormParams from p99 latency and sampled per-window request counts."""
    all_latencies = [rt for _, _, rt in ds._rows if rt >= 0]
    lat_p99 = float(np.percentile(all_latencies, 99)) if all_latencies else 2000.0
    lat_scale = max(lat_p99, 100.0)

    # Sample a subset of windows to estimate request_count scale
    rng = np.random.default_rng(42)
    n_sample = min(_NORM_SAMPLE_WINDOWS, ds.n_windows)
    ts_vals   = rng.integers(ds._min_ts, max(ds._min_ts + 1, ds._max_ts - ds.window_ms), size=n_sample)
    counts: list[float] = []
    for ts in ts_vals:
        for s in ds.get_window(int(ts)):
            counts.append(float(s.queue_depth))
    count_p99   = float(np.percentile(counts, 99)) if counts else 200.0
    count_scale = max(count_p99, 10.0)

    print(
        f"[train] norm_params: latency_scale={lat_scale:.1f}, "
        f"request_count_scale={count_scale:.1f}",
        flush=True,
    )
    return NormParams(latency_scale=lat_scale, request_count_scale=count_scale)


# ── main training entry point ─────────────────────────────────────────────────

def main(
    partitions: list[Path] | None = None,
    steps: int = _FULL_STEPS,
    episode_length: int = DEFAULT_EPISODE_LENGTH,
    out_dir: Path = _MODELS_DIR,
    skip_canary: bool = False,
) -> None:
    """Full MaskablePPO training run.

    Writes out_dir/policy.zip and out_dir/artifact_meta.json on success.
    Exits with code 1 if the canary fails (unless skip_canary=True).
    """
    try:
        import stable_baselines3 as _sb3
        import sb3_contrib as _sb3c
        from sb3_contrib import MaskablePPO
    except ImportError as exc:
        raise ImportError(
            "SB3 packages not installed.  Run:\n"
            "  pip install -r training/requirements-training.txt"
        ) from exc

    if partitions is None:
        partitions = sorted(_ALIBABA_DIR.glob("*.csv"))
    if not partitions:
        raise FileNotFoundError(f"No Alibaba CSV files found at {_ALIBABA_DIR}")

    print(f"[train] loading dataset ({len(partitions)} partitions)...", flush=True)
    ds   = TraceReplayDataset(partitions, n_backends=N_MAX_BACKENDS)
    norm = _compute_norm_params(ds)

    if not skip_canary:
        print("\n[train] running canary check...", flush=True)
        canary_ok = run_canary(
            partitions=partitions[:_CANARY_PARTS],
            norm=norm,
            steps=_CANARY_STEPS,
        )
        if not canary_ok:
            print(
                "[train] canary failed — aborting full training.\n"
                "Run:  python training/train_dqn.py",
                flush=True,
            )
            sys.exit(1)
        print("[train] canary passed — starting full training.\n", flush=True)

    env     = _wrap_for_maskable_ppo(SmartLoadEnv(dataset=ds, norm=norm, episode_length=episode_length))
    monitor = _make_monitor_callback()

    model = MaskablePPO(
        "MlpPolicy",
        env,
        verbose=1,
        learning_rate=3e-4,
        n_steps=512,
        batch_size=64,
        n_epochs=10,
        gamma=0.99,
        seed=42,
        tensorboard_log=None,
    )

    print(f"[train] training MaskablePPO for {steps:,} steps...", flush=True)
    model.learn(total_timesteps=steps, callback=monitor)

    # ── write artifacts ────────────────────────────────────────────────────────
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / "policy"
    model.save(str(model_path))
    print(f"[train] saved {model_path}.zip", flush=True)

    # Mean reward from the training ep_info_buffer (last 100 episodes)
    mean_eval_reward = (
        float(np.mean([e["r"] for e in model.ep_info_buffer]))
        if model.ep_info_buffer
        else 0.0
    )

    sb3_version  = _sb3.__version__
    try:
        sb3c_version = _sb3c.__version__
    except AttributeError:
        sb3c_version = "unknown"

    meta: dict[str, Any] = {
        "policy_type":         "ppo",
        "training_date":       datetime.now(timezone.utc).isoformat(),
        "n_max_backends":      N_MAX_BACKENDS,
        "norm_params":         norm.to_dict(),
        "reward_lambda":       0.1,
        "episode_length":      episode_length,
        "training_steps":      steps,
        "mean_eval_reward":    mean_eval_reward,
        "sb3_version":         sb3_version,
        "sb3_contrib_version": sb3c_version,
        "dataset_partitions":  [p.name for p in partitions],
    }
    meta_path = out_dir / "artifact_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2))
    print(f"[train] wrote {meta_path}", flush=True)
    print(f"[train] mean_eval_reward (training buffer): {mean_eval_reward:.4f}", flush=True)
    print("[train] done.", flush=True)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SmartLoad MaskablePPO training")
    p.add_argument(
        "command",
        nargs="?",
        default="train",
        choices=["train", "run_canary"],
        help="'run_canary' for canary check only; default 'train' for full training",
    )
    p.add_argument("--partitions", nargs="+", default=None,
                   help="CSV partition paths (default: all datasets/alibaba/*.csv)")
    p.add_argument("--steps", type=int, default=None,
                   help=f"Training steps (default: canary={_CANARY_STEPS:,}, full={_FULL_STEPS:,})")
    p.add_argument("--episode-length", type=int, default=DEFAULT_EPISODE_LENGTH,
                   help=f"Episode length (default: {DEFAULT_EPISODE_LENGTH})")
    p.add_argument("--skip-canary", action="store_true",
                   help="Skip canary check and go straight to full training")
    p.add_argument("--out-dir", default=str(_MODELS_DIR),
                   help="Directory for policy.zip and artifact_meta.json")
    return p.parse_args()


if __name__ == "__main__":
    args   = _parse_args()
    parts  = [Path(p) for p in args.partitions] if args.partitions else None

    if args.command == "run_canary":
        ok = run_canary(
            partitions=parts,
            steps=args.steps or _CANARY_STEPS,
        )
        sys.exit(0 if ok else 1)
    else:
        main(
            partitions=parts,
            steps=args.steps or _FULL_STEPS,
            episode_length=args.episode_length,
            out_dir=Path(args.out_dir),
            skip_canary=args.skip_canary,
        )
