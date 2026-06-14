"""
experiments/rl-routing-bench/run_ext.py
────────────────────────────────────────
EXTENDED routing benchmark — superset of run.py. Same frozen closed-loop sim,
same scenarios, same metrics, same seed-bands and SLA threshold; nothing is
relaxed. It ADDS:

  • 4 strong classical baselines: power_of_two_choices, join_shortest_queue,
    least_response_time, weighted_least_connections (baselines_ext.py).
  • the new contenders: candidate_mono (latency-monotone capacity-aware router,
    5 training seeds) and candidate_maxxer (non-monotone SLA-targeted PPO,
    5 training seeds), each aggregated to mean ± 95% CI across TRAINING seeds.
  • a latency-monotonicity probe (monotonicity_probe.py) run on every learned /
    structured policy, reported pass/fail.

Ranking key is unchanged: p95 served latency + SLA-violation% (lower is better);
reward is diagnostic only. The per-band CI (across the 5 eval seed-bands) matches
run.py; for the multi-seed model groups we additionally report the CI ACROSS the
5 training seeds (the gate's robustness axis).

Run (CPU — avoids the SB3 GPU device-mismatch and is faster for MLP policies):
    CUDA_VISIBLE_DEVICES="" .venv/bin/python experiments/rl-routing-bench/run_ext.py
"""

from __future__ import annotations

import csv
import json
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

import logging
logging.getLogger("obs_builder").setLevel(logging.ERROR)

from obs_builder import N_MAX_BACKENDS, build_observation, NormParams   # noqa: E402
from policy_base import is_eligible                                     # noqa: E402
from training.reward_v2 import RewardConfig, compute_reward            # noqa: E402
from training.monotone_router import MonotoneRouter, MonotoneConfig    # noqa: E402

import contenders as C            # noqa: E402
import scenarios as S             # noqa: E402
import baselines_ext as B         # noqa: E402
import monotonicity_probe as MP   # noqa: E402

N_BACKENDS = N_MAX_BACKENDS
EPISODE_LENGTH = 128
SEED_BANDS = [(30000, 30040), (31000, 31040), (32000, 32040), (33000, 33040), (34000, 34040)]
SLA_THRESHOLD_MS = 200.0
CI_Z = 1.96
_REWARD_CFG = RewardConfig(latency_scale=200.0, w_tail=0.5, w_shed=5.0, w_spread=0.3)
_MODELS = _RL_ENGINE / "models"
_METRIC_KEYS = ["p50", "p95", "p99", "mean_lat", "sla_pct", "shed_pct", "reward", "hhi"]


# ── slot extraction for structured/stateful policies ─────────────────────────

def _slots(state):
    ss = sorted(state, key=lambda s: s.backend_id)
    lat = np.full(N_BACKENDS, np.inf); load = np.zeros(N_BACKENDS); mask = np.zeros(N_BACKENDS, bool)
    for i, s in enumerate(ss[:N_BACKENDS]):
        lat[i] = s.latency_ms; load[i] = s.queue_depth; mask[i] = is_eligible(s.health)
    return lat, load, mask


# ── episode / band ───────────────────────────────────────────────────────────

def run_episode(make_wfn, norm, kind, seed):
    """make_wfn(seed) -> wfn(obs, state)->weights (fresh per episode for stateful
    policies). norm is the obs NormParams used to build obs for learned models;
    structured/classical policies ignore obs."""
    sim = S.make_sim(kind, N_BACKENDS, EPISODE_LENGTH)
    state = S.reset_for_kind(sim, kind, seed, N_BACKENDS)
    wfn = make_wfn(seed)
    lat, shed, rew, hhi = [], [], [], []
    done = False
    while not done:
        obs = build_observation(state, N_MAX_BACKENDS, norm)
        w = wfn(obs, state)
        state, m, done = sim.step(w)
        lat.append(m.served_mean_latency_ms); shed.append(m.shed_fraction)
        rew.append(compute_reward(m, _REWARD_CFG)); hhi.append(m.weight_hhi)
    return np.asarray(lat), np.asarray(shed), np.asarray(rew), np.asarray(hhi)


def band_metrics(make_wfn, norm, kind, lo, hi):
    lat, shed, rew, hhi = [], [], [], []
    for seed in range(lo, hi):
        l, s, r, h = run_episode(make_wfn, norm, kind, seed)
        lat.append(l); shed.append(s); rew.append(r); hhi.append(h)
    lat = np.concatenate(lat); shed = np.concatenate(shed)
    rew = np.concatenate(rew); hhi = np.concatenate(hhi)
    return {"p50": float(np.percentile(lat, 50)), "p95": float(np.percentile(lat, 95)),
            "p99": float(np.percentile(lat, 99)), "mean_lat": float(lat.mean()),
            "sla_pct": float(100 * np.mean(lat > SLA_THRESHOLD_MS)),
            "shed_pct": float(100 * shed.mean()), "reward": float(rew.mean()),
            "hhi": float(hhi.mean())}


def mean_ci(vals):
    a = np.asarray(vals, float); n = len(a); m = float(a.mean())
    if n < 2:
        return m, 0.0
    return m, float(CI_Z * a.std(ddof=1) / np.sqrt(n))


def fmt(m, ci, d=1):
    return f"{m:.{d}f} ± {ci:.{d}f}"


# ── contender registry ───────────────────────────────────────────────────────

DEFAULT_NORM = NormParams(latency_scale=200.0, request_count_scale=100.0)


def build_contenders():
    """Return (specs, groups, probe_targets).
    specs: list of (name, make_wfn, norm, is_classical_label) evaluated individually.
    groups: {group_name: [member names...]} for training-seed aggregation.
    probe_targets: {name: policy_builder} for the monotonicity probe."""
    learned = C.load_learned()
    classical = C.classical_factory()
    new_base = B.factory()

    specs = []         # (name, make_wfn, norm, type_label)
    probe_targets = {}

    # original learned (fixed model; make_wfn ignores seed)
    for name in ["policy_shipped", "candidate_v2", "candidate_a2c", "candidate_sac", "candidate_dqn"]:
        wfn, norm, _kind, _meta = learned[name]
        specs.append((name, (lambda s, f=wfn: f), norm, "rl"))
        probe_targets[name] = (lambda f=wfn, nm=norm: (lambda state: f(build_observation(state, N_MAX_BACKENDS, nm), state)))

    # original classical (stateful via real policy classes -> rankings -> weights)
    for name, (ctor, norm) in classical.items():
        def mk(seed, ctor=ctor):
            obj = ctor(seed=seed); return C.make_classical(obj)
        specs.append((name, mk, norm, "cls"))

    # new strong classical baselines (return weights directly)
    for name, ctor in new_base.items():
        def mk(seed, ctor=ctor):
            obj = ctor(seed=seed); return (lambda obs, state, o=obj: o(obs, state))
        specs.append((name, mk, DEFAULT_NORM, "cls"))
        # probe: fresh instance per state, weights(state)
        probe_targets[name] = (lambda ctor=ctor: (lambda state: ctor(seed=0)(None, state)))

    groups = {}

    # candidate_mono training seeds (monotone router)
    mono_members = []
    for s in range(5):
        d = _MODELS / f"candidate_mono_seed{s}"
        if not (d / "params.json").exists():
            continue
        cfg = MonotoneConfig.from_dict(json.loads((d / "params.json").read_text())["monotone_config"])
        nm = f"candidate_mono_seed{s}"
        def mk(seed, cfg=cfg):
            r = MonotoneRouter(cfg, N_BACKENDS)
            def wfn(obs, state, r=r):
                lat, load, mask = _slots(state)
                if not mask.any():
                    mask = mask.copy(); mask[0] = True
                return r.weights(lat, load, mask)
            return wfn
        specs.append((nm, mk, DEFAULT_NORM, "rl-mono"))
        mono_members.append(nm)
        probe_targets[nm] = (lambda cfg=cfg: _mono_probe_builder(cfg))
    if mono_members:
        groups["candidate_mono"] = mono_members

    # candidate_maxxer training seeds (SB3 continuous PPO)
    maxxer_members = []
    for s in range(5):
        d = _MODELS / f"candidate_maxxer_seed{s}"
        if not (d / "policy.zip").exists():
            continue
        from stable_baselines3 import PPO
        meta = json.loads((d / "artifact_meta.json").read_text())
        nm = f"candidate_maxxer_seed{s}"
        norm = NormParams.from_dict(meta["norm_params"])
        model = PPO.load(str(d / "policy"), device="cpu")
        wfn = C.make_continuous(model, norm)
        specs.append((nm, (lambda sd, f=wfn: f), norm, "rl-maxx"))
        maxxer_members.append(nm)
        probe_targets[nm] = (lambda f=wfn, nm2=norm: (lambda state: f(build_observation(state, N_MAX_BACKENDS, nm2), state)))
    if maxxer_members:
        groups["candidate_maxxer"] = maxxer_members

    return specs, groups, probe_targets


def _mono_probe_builder(cfg):
    r = MonotoneRouter(cfg, N_BACKENDS)
    def f(state):
        lat, load, mask = _slots(state)
        if not mask.any():
            mask = mask.copy(); mask[0] = True
        return r.weights(lat, load, mask)
    return f


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    t0 = time.time()
    print("Building contenders (CPU)...", flush=True)
    specs, groups, probe_targets = build_contenders()

    # cell[(name, kind)] = {metric: [per-band values]}
    cell = {}
    grid_rows = []
    names = [s[0] for s in specs]
    for kind in S.ALL_KINDS:
        print(f"\n=== scenario: {kind} ===", flush=True)
        for name, make_wfn, norm, typ in specs:
            per_band = {m: [] for m in _METRIC_KEYS}
            for lo, hi in SEED_BANDS:
                bm = band_metrics(make_wfn, norm, kind, lo, hi)
                for m in _METRIC_KEYS:
                    per_band[m].append(bm[m])
                grid_rows.append({"contender": name, "scenario": kind,
                                  "seed_band": f"{lo}-{hi-1}", "n_episodes": hi - lo,
                                  "episode_length": EPISODE_LENGTH, **{m: bm[m] for m in _METRIC_KEYS}})
            cell[(name, kind)] = per_band
            p95m, p95c = mean_ci(per_band["p95"]); slam, slac = mean_ci(per_band["sla_pct"])
            print(f"  {name:<28} [{typ:<7}] p95={fmt(p95m,p95c)}  SLA%={fmt(slam,slac)}", flush=True)

    # monotonicity probe
    print("\n=== monotonicity probe ===", flush=True)
    probe_results = {}
    for name, builder in probe_targets.items():
        try:
            res = MP.run_probe(builder, name)
        except Exception as exc:  # noqa: BLE001
            res = {"label": name, "passed": None, "error": str(exc), "max_violation": None}
        probe_results[name] = res
        status = "PASS" if res.get("passed") else ("ERR" if res.get("passed") is None else "FAIL")
        print(f"  {name:<28} monotone: {status}  max_rise={res.get('max_violation')}", flush=True)

    runtime = time.time() - t0
    tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = _HERE / "results" / tag
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_grid(out_dir / "grid.csv", grid_rows)
    _write_summary(out_dir / "SUMMARY.md", cell, names, specs, groups, probe_results, runtime, tag)
    (out_dir / "probe.json").write_text(json.dumps(
        {k: {kk: vv for kk, vv in v.items() if kk != "worst"} for k, v in probe_results.items()}, indent=2))
    print(f"\nDONE in {runtime:.1f}s\nOutputs: {out_dir}")
    return 0


def _write_grid(path, rows):
    fields = ["contender", "scenario", "seed_band", "n_episodes", "episode_length"] + _METRIC_KEYS
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        for r in rows:
            w.writerow(r)


def _group_scenario_stat(cell, members, kind, metric):
    """Per-training-seed values (each = mean over eval bands), then mean±CI across seeds."""
    per_seed = [mean_ci(cell[(m, kind)][metric])[0] for m in members]
    return mean_ci(per_seed)


def _write_summary(path, cell, names, specs, groups, probe, runtime, tag):
    type_of = {n: t for n, _, _, t in specs}
    L = []
    L.append("# RL Routing Benchmark (EXTENDED) — SmartLoad\n")
    L.append(f"Run tag: `{tag}`  |  Runtime: {runtime:.1f}s\n")
    L.append(f"Closed-loop causal M/G/c sim. {len(SEED_BANDS)} eval seed-bands × 40 ep × "
             f"{EPISODE_LENGTH} windows. Cells = **mean ± 95% CI** across eval bands. SLA > "
             f"**{SLA_THRESHOLD_MS:.0f} ms**. Ranking: **p95 + SLA%** (lower=better); reward "
             "diagnostic only. Superset of run.py: adds 4 strong classical baselines, the new "
             "candidate_mono / candidate_maxxer (each 5 training seeds), and a monotonicity probe. "
             "Nothing in the metric/seed/scenario protocol is relaxed.\n")

    cols = ["p95", "sla_pct", "p50", "p99", "mean_lat", "shed_pct", "reward", "hhi"]
    hdr = ["p95", "SLA%", "p50", "p99", "mean", "shed%", "reward*", "HHI"]
    dec = {"p95": 1, "sla_pct": 1, "p50": 1, "p99": 1, "mean_lat": 1, "shed_pct": 1, "reward": 3, "hhi": 3}

    L.append("## Per-scenario results (all contenders, per-eval-band CI)\n")
    for kind in S.ALL_KINDS:
        # leader on p95
        stats = {n: mean_ci(cell[(n, kind)]["p95"]) for n in names}
        leader = min(names, key=lambda n: stats[n][0])
        lm, lci = stats[leader]
        tie = {n for n in names if (stats[n][0]-stats[n][1]) <= (lm+lci) and (lm-lci) <= (stats[n][0]+stats[n][1])}
        L.append(f"### {kind}\n")
        L.append("| contender | type | mono | " + " | ".join(hdr) + " |")
        L.append("|" + "---|" * (len(hdr) + 3))
        for n in names:
            cells = []
            for c in cols:
                m, ci = mean_ci(cell[(n, kind)][c]); s = fmt(m, ci, dec[c])
                if c == "p95" and n in tie:
                    s = f"**{s}**"
                cells.append(s)
            mono = probe.get(n, {}).get("passed")
            mono_s = "✓" if mono else ("—" if mono is None else "✗")
            L.append(f"| {n} | {type_of[n]} | {mono_s} | " + " | ".join(cells) + " |")
        L.append(f"\nLeader on p95: **{leader}**.\n")

    # multi-seed group aggregation vs candidate_v2
    L.append("## Multi-seed model groups vs candidate_v2 (CI across TRAINING seeds)\n")
    L.append("For each new model, per-scenario value = mean over the 5 eval bands for each of the "
             "5 training seeds; reported as **mean ± 95% CI across the 5 training seeds**. A WIN "
             "needs lower p95 AND lower SLA% than candidate_v2 with non-overlapping CIs.\n")
    for gname, members in groups.items():
        L.append(f"### {gname}  (training seeds: {len(members)})\n")
        L.append("| scenario | metric | candidate_v2 (eval-band CI) | "
                 f"{gname} (train-seed CI) | win? |")
        L.append("|---|---|---|---|---|")
        wins = 0
        for kind in S.ALL_KINDS:
            v2p = mean_ci(cell[("candidate_v2", kind)]["p95"])
            v2s = mean_ci(cell[("candidate_v2", kind)]["sla_pct"])
            gp = _group_scenario_stat(cell, members, kind, "p95")
            gs = _group_scenario_stat(cell, members, kind, "sla_pct")
            def nonover(a, b):  # a beats b if a.hi < b.lo
                return (a[0] + a[1]) < (b[0] - b[1])
            p_win = nonover(gp, v2p); s_win = nonover(gs, v2s)
            win = p_win and s_win
            wins += win
            L.append(f"| {kind} | p95 | {fmt(*v2p)} | {fmt(*gp)} | {'Y' if p_win else 'n'} |")
            L.append(f"| {kind} | SLA% | {fmt(*v2s)} | {fmt(*gs)} | {'Y' if s_win else 'n'} |")
        held = "held_out_dual_degrade"
        v2p = mean_ci(cell[("candidate_v2", held)]["p95"]); v2s = mean_ci(cell[("candidate_v2", held)]["sla_pct"])
        gp = _group_scenario_stat(cell, members, held, "p95"); gs = _group_scenario_stat(cell, members, held, "sla_pct")
        held_win = ((gp[0]+gp[1]) < (v2p[0]-v2p[1])) and ((gs[0]+gs[1]) < (v2s[0]-v2s[1]))
        mono_ok = all(probe.get(m, {}).get("passed") for m in members)
        L.append(f"\n**{gname}: both-metric wins (non-overlapping CI) = {wins}/5** "
                 f"(held-out won: {'Y' if held_win else 'n'}; monotone: {'PASS' if mono_ok else 'FAIL'}).\n")

    L.append("## Monotonicity probe\n")
    L.append("| policy | result | max weight-rise vs latency |")
    L.append("|---|---|---|")
    for n in names:
        if n in probe:
            r = probe[n]
            st = "PASS" if r.get("passed") else ("ERR" if r.get("passed") is None else "FAIL")
            L.append(f"| {n} | {st} | {r.get('max_violation')} |")
    L.append("\n*reward is diagnostic only (embeds the trainer's weightings); never used to rank.\n")

    path.write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
