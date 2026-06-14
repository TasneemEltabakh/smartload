"""
experiments/rl-routing-bench/run.py
─────────────────────────────────────
RL routing benchmark for SmartLoad. Closed-loop, multi-seed EVALUATION of eight
routing contenders on the causal M/G/c queue simulator.

Run (numpy-2 interpreter — loads all five shipped artifacts):
    /tmp/np2env/Scripts/python.exe experiments/rl-routing-bench/run.py

What it does
────────────
For each contender x scenario-kind x seed-band:
  • plays ~40 episodes (episode_length windows each) on the frozen closed-loop
    sim, routing every window by the contender's weight vector;
  • records the per-window served latency, shed fraction, reward (diagnostic)
    and routing-weight HHI;
  • reduces a seed-band's window samples to scalar metrics (p50/p95/p99/mean
    served latency, SLA-violation %, shed %, reward, HHI).
Across the 5 seed-bands it reports mean ± 95% CI (normal approx,
mean ± 1.96*std/sqrt(n_bands)). A result inside another's CI is a TIE.

Fairness notes baked in:
  • Each learned model uses its OWN norm_params for observation building.
  • Each learned weight rule mirrors the serving layer for that artifact kind.
  • Classical policies drive the REAL policy classes (round_robin keeps its
    rotation pointer per episode; random_shadow is seeded by the episode seed).
  • Scenarios scored separately, never pooled into one mean.
  • Primary ranking key: p95 served latency and SLA-violation %. Reward is
    diagnostic only (it embeds the trainer's weightings).

No file under services/ is modified; this harness only imports it.
"""

from __future__ import annotations

import csv
import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
_RL_ENGINE = _REPO / "services" / "rl-engine"
for p in (str(_HERE), str(_RL_ENGINE)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Quiet the obs_builder all-masked WARNING (expected on all-unhealthy windows).
import logging  # noqa: E402
logging.getLogger("obs_builder").setLevel(logging.ERROR)

from obs_builder import N_MAX_BACKENDS, build_observation             # noqa: E402
from training.reward_v2 import RewardConfig, compute_reward           # noqa: E402

import contenders as C                                                # noqa: E402
import scenarios as S                                                 # noqa: E402


# ── protocol constants ──────────────────────────────────────────────────────────

N_BACKENDS = N_MAX_BACKENDS              # 5
EPISODE_LENGTH = 128                     # closed-loop sim default; candidates trained here
EPISODES_PER_BAND = 40
SEED_BANDS = [
    (30000, 30040),
    (31000, 31040),
    (32000, 32040),
    (33000, 33040),
    (34000, 34040),
]
SLA_THRESHOLD_MS = 200.0                 # served latency > this counts as an SLA violation
CI_Z = 1.96                              # 95% normal-approx CI

# Diagnostic reward config: the candidates' shared closed-loop reward weights
# (latency_scale 200, w_tail 0.5, w_shed 5.0, w_spread 0.3). Reported for
# transparency only; never used to rank.
_REWARD_CFG = RewardConfig(latency_scale=200.0, w_tail=0.5, w_shed=5.0, w_spread=0.3)


# ── one episode ─────────────────────────────────────────────────────────────────

def run_episode(weight_fn, norm, kind, seed, classical_ctor=None):
    """Play one episode. Returns per-window arrays:
    (served_lat_ms, shed_frac, reward, weight_hhi). For classical contenders
    `classical_ctor(seed)` builds a fresh stateful policy and `weight_fn` is
    rebound to it; for learned contenders `weight_fn` is fixed and
    `classical_ctor` is None."""
    sim = S.make_sim(kind, N_BACKENDS, EPISODE_LENGTH)
    state = S.reset_for_kind(sim, kind, seed, N_BACKENDS)

    if classical_ctor is not None:
        policy_obj = classical_ctor(seed=seed)
        wfn = C.make_classical(policy_obj)
    else:
        wfn = weight_fn

    lat, shed, rew, hhi = [], [], [], []
    done = False
    while not done:
        obs = build_observation(state, N_MAX_BACKENDS, norm)
        w = wfn(obs, state)
        state, metrics, done = sim.step(w)
        lat.append(metrics.served_mean_latency_ms)
        shed.append(metrics.shed_fraction)
        rew.append(compute_reward(metrics, _REWARD_CFG))
        hhi.append(metrics.weight_hhi)
    return (np.asarray(lat), np.asarray(shed), np.asarray(rew), np.asarray(hhi))


def band_metrics(weight_fn, norm, kind, seed_lo, seed_hi, classical_ctor=None):
    """Aggregate one seed-band (seeds [seed_lo, seed_hi)) into scalar metrics
    computed over ALL the band's per-window samples."""
    lat, shed, rew, hhi = [], [], [], []
    for seed in range(seed_lo, seed_hi):
        l, s, r, h = run_episode(weight_fn, norm, kind, seed, classical_ctor)
        lat.append(l); shed.append(s); rew.append(r); hhi.append(h)
    lat = np.concatenate(lat); shed = np.concatenate(shed)
    rew = np.concatenate(rew); hhi = np.concatenate(hhi)
    return {
        "p50": float(np.percentile(lat, 50)),
        "p95": float(np.percentile(lat, 95)),
        "p99": float(np.percentile(lat, 99)),
        "mean_lat": float(lat.mean()),
        "sla_pct": float(100.0 * np.mean(lat > SLA_THRESHOLD_MS)),
        "shed_pct": float(100.0 * shed.mean()),
        "reward": float(rew.mean()),
        "hhi": float(hhi.mean()),
    }


# ── aggregation across seed-bands ────────────────────────────────────────────────

_METRIC_KEYS = ["p50", "p95", "p99", "mean_lat", "sla_pct", "shed_pct", "reward", "hhi"]


def mean_ci(values):
    """Mean and 95% CI half-width (normal approx) across seed-bands."""
    a = np.asarray(values, dtype=float)
    n = len(a)
    mean = float(a.mean())
    if n < 2:
        return mean, 0.0
    std = float(a.std(ddof=1))
    return mean, float(CI_Z * std / np.sqrt(n))


def fmt(mean, ci, decimals=1):
    return f"{mean:.{decimals}f} ± {ci:.{decimals}f}"


# ── main ─────────────────────────────────────────────────────────────────────────

def main() -> int:
    t0 = time.time()
    print("Loading learned contenders (numpy-2 interpreter)...", flush=True)
    learned = C.load_learned()             # name -> (fn, norm, kind, meta_path)
    classical = C.classical_factory()      # name -> (ctor, norm)

    # Stable contender order: learned first, then classical.
    contender_order = [
        "policy_shipped", "candidate_v2", "candidate_a2c", "candidate_sac", "candidate_dqn",
        "round_robin", "least_connections", "random_shadow",
    ]
    is_classical = {n: (n in classical) for n in contender_order}

    # grid_rows: one dict per contender x scenario x seed-band
    grid_rows = []
    # cell_metrics[(contender, scenario)] = {metric: [band0, band1, ...]}
    cell = {}

    for kind in S.ALL_KINDS:
        print(f"\n=== scenario: {kind} ===", flush=True)
        for name in contender_order:
            if is_classical[name]:
                ctor, norm = classical[name]
                wfn = None
            else:
                wfn, norm, _kind, _meta = learned[name]
                ctor = None
            per_band = {m: [] for m in _METRIC_KEYS}
            for bi, (lo, hi) in enumerate(SEED_BANDS):
                bm = band_metrics(wfn, norm, kind, lo, hi, classical_ctor=ctor)
                for m in _METRIC_KEYS:
                    per_band[m].append(bm[m])
                grid_rows.append({
                    "contender": name,
                    "scenario": kind,
                    "seed_band": f"{lo}-{hi - 1}",
                    "n_episodes": hi - lo,
                    "episode_length": EPISODE_LENGTH,
                    **{m: bm[m] for m in _METRIC_KEYS},
                })
            cell[(name, kind)] = per_band
            p95_m, p95_ci = mean_ci(per_band["p95"])
            sla_m, sla_ci = mean_ci(per_band["sla_pct"])
            tag = "classical" if is_classical[name] else "learned"
            print(f"  {name:<20} [{tag:<9}] "
                  f"p95={fmt(p95_m, p95_ci)} ms  SLA%={fmt(sla_m, sla_ci)}",
                  flush=True)

    runtime_s = time.time() - t0

    # ── write outputs ───────────────────────────────────────────────────────────
    tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = _HERE / "results" / tag
    out_dir.mkdir(parents=True, exist_ok=True)

    _write_grid_csv(out_dir / "grid.csv", grid_rows)
    _write_summary(out_dir / "SUMMARY.md", cell, contender_order, is_classical, runtime_s, tag)
    _write_meta(out_dir / "meta.json", learned, classical, runtime_s, tag)

    print(f"\nDONE in {runtime_s:.1f}s")
    print(f"Outputs: {out_dir}")
    print(f"  grid.csv  SUMMARY.md  meta.json")
    return 0


def _write_grid_csv(path: Path, rows: list[dict]) -> None:
    fields = ["contender", "scenario", "seed_band", "n_episodes", "episode_length"] + _METRIC_KEYS
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)


_PRETTY = {
    "policy_shipped": "policy.zip (shipped MaskablePPO)",
    "candidate_v2": "candidate_v2 (PPO)",
    "candidate_a2c": "candidate_a2c (A2C)",
    "candidate_sac": "candidate_sac (SAC)",
    "candidate_dqn": "candidate_dqn (DQN)",
    "round_robin": "round_robin",
    "least_connections": "least_connections",
    "random_shadow": "random_shadow",
}

_SCEN_PRETTY = {
    "homogeneous": "Homogeneous",
    "heterogeneous": "Heterogeneous",
    "degrading": "Degrading (1 backend)",
    "near-idle": "Near-idle",
    "held_out_dual_degrade": "Held-out (dual-degrade, disjoint means)",
}


def _winner_p95(cell, contenders, kind):
    """Return the set of contenders that are statistically best on p95 for a
    scenario: lowest mean p95, plus anyone whose CI overlaps the leader's CI
    (a tie). Returns (leader_name, tie_set)."""
    stats = {}
    for n in contenders:
        m, ci = mean_ci(cell[(n, kind)]["p95"])
        stats[n] = (m, ci)
    leader = min(contenders, key=lambda n: stats[n][0])
    lm, lci = stats[leader]
    tie = set()
    for n in contenders:
        m, ci = stats[n]
        # overlap if the intervals [m-ci, m+ci] intersect the leader's
        if (m - ci) <= (lm + lci) and (lm - lci) <= (m + ci):
            tie.add(n)
    return leader, tie


def _write_summary(path, cell, contenders, is_classical, runtime_s, tag) -> None:
    lines = []
    lines.append("# RL Routing Benchmark — SmartLoad")
    lines.append("")
    lines.append(f"Run tag: `{tag}`  |  Total runtime: {runtime_s:.1f}s")
    lines.append("")
    lines.append(
        f"Closed-loop causal M/G/c queue sim. {len(SEED_BANDS)} disjoint seed-bands "
        f"x {EPISODES_PER_BAND} episodes x {EPISODE_LENGTH} windows. "
        f"Cells are **mean ± 95% CI** across seed-bands (normal approx, "
        f"`mean ± {CI_Z}·std/√{len(SEED_BANDS)}`). "
        f"SLA threshold: served latency > **{SLA_THRESHOLD_MS:.0f} ms**. "
        "Primary ranking key: **p95 served latency** and **SLA-violation %** "
        "(lower is better). Reward is diagnostic only. A result inside another's "
        "95% CI is a tie, not a win."
    )
    lines.append("")
    lines.append(
        "Latencies in ms. `SLA%` = % of windows with served latency > threshold. "
        "`shed%` = mean shed fraction. `HHI` = mean routing-weight Herfindahl "
        "(1/N≈0.2 = perfectly even, 1.0 = all on one backend). **Bold** p95 cell "
        "marks the per-scenario leader and any statistical tie with it."
    )
    lines.append("")

    cols = ["p95", "sla_pct", "p50", "p99", "mean_lat", "shed_pct", "reward", "hhi"]
    headers = ["p95", "SLA%", "p50", "p99", "mean", "shed%", "reward*", "HHI"]
    dec = {"p95": 1, "sla_pct": 1, "p50": 1, "p99": 1, "mean_lat": 1,
           "shed_pct": 1, "reward": 3, "hhi": 3}

    def render_block(kind):
        leader, tie = _winner_p95(cell, contenders, kind)
        out = []
        out.append(f"### {_SCEN_PRETTY.get(kind, kind)}")
        out.append("")
        out.append("| contender | type | " + " | ".join(headers) + " |")
        out.append("|" + "---|" * (len(headers) + 2))
        for n in contenders:
            row = cell[(n, kind)]
            cells = []
            for c in cols:
                m, ci = mean_ci(row[c])
                s = fmt(m, ci, dec[c])
                if c == "p95" and n in tie:
                    s = f"**{s}**"
                cells.append(s)
            typ = "cls" if is_classical[n] else "rl"
            out.append(f"| {_PRETTY[n]} | {typ} | " + " | ".join(cells) + " |")
        out.append("")
        out.append(f"Leader on p95: **{_PRETTY[leader]}** "
                   f"(tie set: {', '.join(sorted(_PRETTY[t] for t in tie))}).")
        out.append("")
        return out

    lines.append("## Per-scenario results")
    lines.append("")
    for kind in S.ALL_KINDS:
        lines += render_block(kind)

    # Overall: average each metric across scenarios (per-scenario scalars first,
    # then mean ± CI across the 5 per-scenario means). Stated as a coarse summary;
    # per-scenario blocks above are authoritative.
    lines.append("## Overall (mean of per-scenario means)")
    lines.append("")
    lines.append(
        "Coarse roll-up: for each contender, the per-scenario mean of each metric "
        "is averaged across the 5 scenarios; CI is across those 5 scenario means. "
        "Per-scenario blocks above are authoritative — scenarios are not pooled at "
        "the window level."
    )
    lines.append("")
    lines.append("| contender | type | " + " | ".join(headers) + " |")
    lines.append("|" + "---|" * (len(headers) + 2))
    for n in contenders:
        cells = []
        for c in cols:
            per_scen_means = [mean_ci(cell[(n, k)][c])[0] for k in S.ALL_KINDS]
            m, ci = mean_ci(per_scen_means)
            cells.append(fmt(m, ci, dec[c]))
        typ = "cls" if is_classical[n] else "rl"
        lines.append(f"| {_PRETTY[n]} | {typ} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("*reward is diagnostic only (embeds the trainer's weightings); never used to rank.")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def _write_meta(path, learned, classical, runtime_s, tag) -> None:
    import numpy as _np
    try:
        import stable_baselines3 as _sb3
        sb3_ver = _sb3.__version__
    except Exception:
        sb3_ver = "unknown"
    try:
        import sb3_contrib as _sbc
        sbc_ver = _sbc.__version__
    except Exception:
        sbc_ver = "unknown"

    provenance = {}
    for name, (_fn, norm, kind, meta_path) in learned.items():
        meta = json.loads(Path(meta_path).read_text())
        provenance[name] = {
            "kind": kind,
            "norm_params": norm.to_dict(),
            "policy_type": meta.get("policy_type"),
            "sb3_version_at_train": meta.get("sb3_version"),
            "meta_path": meta_path,
            "real_network": True,
        }
    for name in classical:
        provenance[name] = {"kind": "classical", "real_network": False}

    meta = {
        "tag": tag,
        "runtime_seconds": round(runtime_s, 1),
        "interpreter": sys.executable,
        "python_version": platform.python_version(),
        "numpy_version": _np.__version__,
        "stable_baselines3_version": sb3_ver,
        "sb3_contrib_version": sbc_ver,
        "n_backends": N_BACKENDS,
        "episode_length": EPISODE_LENGTH,
        "episodes_per_band": EPISODES_PER_BAND,
        "seed_bands": [{"lo": lo, "hi": hi - 1, "count": hi - lo} for lo, hi in SEED_BANDS],
        "sla_threshold_ms": SLA_THRESHOLD_MS,
        "ci_z": CI_Z,
        "ci_method": "normal approx: mean +/- 1.96*std/sqrt(n_bands), n_bands=5",
        "scenarios": S.ALL_KINDS,
        "held_out_family": {
            "name": S.HELD_OUT_KIND,
            "description": "service means in [50,90] ms (disjoint from training [12,45]); "
                           "TWO backends degrade simultaneously over overlapping spans "
                           "(training degraded only one).",
        },
        "sim": "services/rl-engine/training/closed_loop_sim.py (causal M/G/c, "
               "synthetic hetero demand); held-out via harness HeldOutSimulator subclass",
        "reward_config_diagnostic": {
            "latency_scale": _REWARD_CFG.latency_scale,
            "w_tail": _REWARD_CFG.w_tail,
            "w_shed": _REWARD_CFG.w_shed,
            "w_spread": _REWARD_CFG.w_spread,
            "note": "reward reported for transparency only; never used to rank",
        },
        "contender_provenance": provenance,
        "served_latency_note": "the closed-loop sim emits one served-mean-latency per "
                               "1s window (load-weighted across backends); percentiles "
                               "and SLA% are over the window distribution within a "
                               "seed-band, not per-request.",
        "training_note": "training was single-seed; this is multi-seed EVALUATION only.",
    }
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
