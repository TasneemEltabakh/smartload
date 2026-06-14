"""
services/rl-engine/training/train_monotone.py
──────────────────────────────────────────────
Fit the latency-monotone router (monotone_router.MonotoneConfig) by black-box
search and emit per-seed params.json artifacts.

Training uses ONLY the curriculum kinds (homogeneous, heterogeneous, degrading,
near-idle) on TRAINING seeds (20000-band, disjoint from the 30000-34000 eval
bands). The held-out dual-degrade family is NEVER used in training, so its
benchmark number measures true generalisation.

"Training seeds" = the gate's robustness axis: the whole search is run N_SEEDS
times with different RNG (controlling the training-scenario draw and the search
population). Each run yields one fitted MonotoneConfig -> one artifact; the
benchmark aggregates them to mean ± 95% CI.

Usage:
  python training/train_monotone.py --seeds 5
  python training/train_monotone.py --smoke
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

_RL_ENGINE = Path(__file__).resolve().parents[1]
if str(_RL_ENGINE) not in sys.path:
    sys.path.insert(0, str(_RL_ENGINE))
logging.getLogger("obs_builder").setLevel(logging.ERROR)

from policy_base import is_eligible                                   # noqa: E402
from training.closed_loop_sim import ClosedLoopSimulator             # noqa: E402
from training.monotone_router import MonotoneRouter, MonotoneConfig  # noqa: E402

_MODELS_DIR = _RL_ENGINE / "models"
N = 5
EP_LEN = 128
SLA_MS = 200.0
TRAIN_KINDS = ["homogeneous", "heterogeneous", "degrading", "near-idle"]
_P95_REF = {"homogeneous": 660.0, "heterogeneous": 700.0, "degrading": 1000.0, "near-idle": 60.0}
_IDLE_UTIL_MAX = 0.15


def _slots(state):
    ss = sorted(state, key=lambda s: s.backend_id)
    lat = np.full(N, np.inf); load = np.zeros(N); mask = np.zeros(N, dtype=bool)
    for i, s in enumerate(ss[:N]):
        lat[i] = s.latency_ms; load[i] = s.queue_depth; mask[i] = is_eligible(s.health)
    return lat, load, mask


def _reset_train(sim, kind, seed):
    if kind in ("homogeneous", "heterogeneous", "degrading"):
        return sim.reset(seed=seed, force_kind=kind)
    for k in range(400):
        trial = seed + k * 1_000_003
        state = sim.reset(seed=trial)
        cap = sum(p.capacity_rps for p in sim._scn.profiles)
        if cap > 0 and float(np.mean(sim._scn.demand_rps)) / cap <= _IDLE_UTIL_MAX:
            return state
    return state


def evaluate(cfg: MonotoneConfig, seeds, kinds=TRAIN_KINDS):
    out = {}
    for kind in kinds:
        lat = []
        for seed in seeds:
            sim = ClosedLoopSimulator(N, episode_length=EP_LEN)
            state = _reset_train(sim, kind, seed)
            router = MonotoneRouter(cfg, N)
            done = False
            while not done:
                l, ld, m = _slots(state)
                if not m.any():
                    m = m.copy(); m[0] = True
                state, met, done = sim.step(router.weights(l, ld, m))
                lat.append(met.served_mean_latency_ms)
        a = np.asarray(lat)
        out[kind] = (float(np.percentile(a, 95)), float(np.mean(a > SLA_MS)))
    return out


def composite_loss(per_kind):
    tot = 0.0
    for kind, (p95, sla) in per_kind.items():
        tot += 0.5 * (p95 / _P95_REF[kind]) + 0.5 * (sla / 0.25)
    return tot / len(per_kind)


def _sample_cfg(rng):
    return MonotoneConfig(
        degr_pow=float(rng.uniform(0.3, 2.5)),
        alpha=float(rng.uniform(0.2, 0.8)),
        cut=float(rng.uniform(2.0, 6.0)),
        cap_floor_ms=5.0,
        idle_load=float(rng.uniform(4.0, 16.0)),
    )


def fit_one_seed(seed, budget, n_train_seeds):
    rng = np.random.default_rng(seed)
    train_seeds = [20000 + seed * 137 + i for i in range(n_train_seeds)]
    pop = [MonotoneConfig(), MonotoneConfig(degr_pow=1.0, alpha=0.3, cut=3.0)]
    pop += [_sample_cfg(rng) for _ in range(budget)]

    best, best_loss = None, np.inf
    for c in pop:
        loss = composite_loss(evaluate(c, train_seeds))
        if loss < best_loss:
            best, best_loss = c, loss

    # coordinate refinement
    steps = {"degr_pow": 0.4, "alpha": 0.15, "cut": 0.8, "idle_load": 3.0}
    for _ in range(10):
        improved = False
        for k, st in steps.items():
            for d in (st, -st):
                params = best.to_dict(); params[k] = max(0.05, params[k] + d)
                cand = MonotoneConfig.from_dict(params)
                loss = composite_loss(evaluate(cand, train_seeds))
                if loss < best_loss:
                    best, best_loss, improved = cand, loss, True
        if not improved:
            steps = {k: v * 0.5 for k, v in steps.items()}
    return best, best_loss


def _write(d: Path, cfg: MonotoneConfig, extra: dict):
    d.mkdir(parents=True, exist_ok=True)
    meta = {
        "policy_type": "monotone_router",
        "policy_kind": "monotone_capacity_aware",
        "training_date": datetime.now(timezone.utc).isoformat(),
        "n_max_backends": N,
        "monotone_config": cfg.to_dict(),
        "train_kinds": TRAIN_KINDS,
        "held_out_used_in_training": False,
        "monotone_by_construction": True,
        "sim": "closed_loop_sim (causal M/G/c, synthetic hetero demand)",
        **extra,
    }
    (d / "params.json").write_text(json.dumps(meta, indent=2))


def main(n_seeds, budget, n_train_seeds, out_dir: Path):
    fitted = []
    for s in range(n_seeds):
        cfg, loss = fit_one_seed(s, budget, n_train_seeds)
        print(f"[train_monotone] seed {s}: {cfg.to_dict()} train_loss={loss:.4f}", flush=True)
        fitted.append((s, cfg, loss))
        _write(out_dir / f"candidate_mono_seed{s}", cfg,
               {"training_seed": s, "train_loss": loss, "n_train_seeds": n_train_seeds})

    # headline = median by degr_pow (robust central choice)
    order = sorted(fitted, key=lambda t: t[1].degr_pow)
    _, med_cfg, med_loss = order[len(order) // 2]
    _write(out_dir / "candidate_mono", med_cfg,
           {"selected_as": "median-degr_pow over training seeds",
            "fitted_seeds": [{"seed": s, **c.to_dict(), "train_loss": l} for s, c, l in fitted]})
    print(f"[train_monotone] headline candidate_mono: {med_cfg.to_dict()}", flush=True)
    return fitted


def _parse():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--budget", type=int, default=30)
    ap.add_argument("--train-seeds", type=int, default=24)
    ap.add_argument("--out-dir", default=str(_MODELS_DIR))
    ap.add_argument("--smoke", action="store_true")
    return ap.parse_args()


if __name__ == "__main__":
    a = _parse()
    if a.smoke:
        main(2, 6, 8, Path(a.out_dir))
    else:
        main(a.seeds, a.budget, a.train_seeds, Path(a.out_dir))
