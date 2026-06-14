"""
experiments/rl-routing-bench/live_stack.py
───────────────────────────────────────────
LIVE-STACK cross-check of the sim ranking on the REAL HTTP backend.

Docker-in-Docker is blocked in this environment, so instead of docker-compose we
launch the real Node test-backends (test-backends/app.js — the same M/G/c queue
the sim mirrors: WORKERS slots, bounded QUEUE_MAX, lognormal service, 503 shed)
as local processes, and drive closed-loop HTTP load through a routing policy.

Each 1s window: observe each backend (EWMA of last window's served latency +
live in_flight/queue from /_admin/stats) -> build BackendState -> policy weights
-> fan out the window's requests split by those weights (threaded) -> record
per-request latency / 503. Reports REAL served p95 + SLA% per policy, so we can
confirm the sim's ranking (round_robin vs candidate_mono vs candidate_v2) holds
on the actual stack.

Usage:
  CUDA_VISIBLE_DEVICES="" .venv/bin/python experiments/rl-routing-bench/live_stack.py \
      --policies round_robin candidate_mono candidate_v2 --scenario heterogeneous
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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

from obs_builder import N_MAX_BACKENDS, build_observation, NormParams        # noqa: E402
from policy_base import BackendState, is_eligible                            # noqa: E402
from runloop import classify_health                                          # noqa: E402
from training.monotone_router import MonotoneRouter, MonotoneConfig          # noqa: E402

_BACKENDS_DIR = _REPO / "test-backends"
_MODELS = _RL_ENGINE / "models"
_BASE_PORT = 18200
SLA_MS = 200.0


# ── backend process management ───────────────────────────────────────────────

def start_backends(means, workers=2, queue_max=64, seed0=1337):
    procs = []
    node = _resolve_node()
    for i, mean in enumerate(means):
        port = _BASE_PORT + i
        env = dict(os.environ)
        env.update(SERVER_ID=f"backend_{i+1}", PORT=str(port),
                   SERVICE_MEAN_MS=str(mean), SERVICE_DIST="lognormal",
                   SERVICE_CV="1.0", SERVICE_SEED=str(seed0 + i),
                   WORKERS=str(workers), QUEUE_MAX=str(queue_max))
        p = subprocess.Popen([node, str(_BACKENDS_DIR / "app.js")], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append((p, port))
    return procs


def _resolve_node():
    for c in ("/opt/nvm/versions/node",):
        base = Path(c)
        if base.exists():
            vers = sorted(base.iterdir())
            if vers:
                cand = vers[-1] / "bin" / "node"
                if cand.exists():
                    return str(cand)
    return "node"


def wait_healthy(procs, timeout=30):
    deadline = time.time() + timeout
    for _, port in procs:
        ok = False
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                    if r.status == 200:
                        ok = True
                        break
            except Exception:
                time.sleep(0.2)
        if not ok:
            return False
    return True


def set_delay(port, ms):
    data = json.dumps({"ms": ms}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/_admin/delay", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass


def get_stats(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/_admin/stats", timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return {"in_flight": 0, "queue_depth": 0}


def hit(port):
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
            r.read()
            ok = r.status == 200
    except urllib.error.HTTPError as e:
        ok = False  # 503 shed
    except Exception:
        ok = False
    return (time.perf_counter() - t0) * 1000.0, ok


# ── policies ─────────────────────────────────────────────────────────────────

def make_policy(name, n):
    if name == "round_robin":
        return _RoundRobin(n)
    if name.startswith("candidate_mono"):
        cfg = MonotoneConfig.from_dict(json.loads((_MODELS / "candidate_mono" / "params.json").read_text())["monotone_config"])
        return _Mono(MonotoneRouter(cfg, n))
    if name == "candidate_v2":
        from stable_baselines3 import PPO
        import contenders as C
        meta = json.loads((_MODELS / "candidate_v2" / "artifact_meta.json").read_text())
        norm = NormParams.from_dict(meta["norm_params"])
        return _Learned(C.make_continuous(PPO.load(str(_MODELS / "candidate_v2" / "policy"), device="cpu"), norm), norm)
    if name.startswith("candidate_maxxer"):
        from stable_baselines3 import PPO
        import contenders as C
        seed = name.split("seed")[-1] if "seed" in name else "1"
        d = _MODELS / (name if "seed" in name else "candidate_maxxer_seed1")
        meta = json.loads((d / "artifact_meta.json").read_text())
        norm = NormParams.from_dict(meta["norm_params"])
        return _Learned(C.make_continuous(PPO.load(str(d / "policy"), device="cpu"), norm), norm)
    raise ValueError(name)


class _RoundRobin:
    def __init__(self, n=None):
        pass
    def weights(self, state):
        elig = [is_eligible(s.health) for s in state]
        w = np.array([1.0 if e else 0.0 for e in elig])
        return w / w.sum() if w.sum() else np.ones(len(state)) / len(state)


class _Mono:
    def __init__(self, router):
        self.r = router
    def weights(self, state):
        ss = sorted(state, key=lambda s: s.backend_id)
        n = len(ss)
        lat = np.array([s.latency_ms for s in ss]); load = np.array([s.queue_depth for s in ss])
        mask = np.array([is_eligible(s.health) for s in ss])
        if not mask.any():
            mask = mask.copy(); mask[0] = True
        full_lat = np.zeros(N_MAX_BACKENDS); full_load = np.zeros(N_MAX_BACKENDS); full_mask = np.zeros(N_MAX_BACKENDS, bool)
        full_lat[:n] = lat; full_load[:n] = load; full_mask[:n] = mask
        return self.r.weights(full_lat, full_load, full_mask)[:n]


class _Learned:
    def __init__(self, fn, norm):
        self.fn = fn; self.norm = norm
    def weights(self, state):
        ss = sorted(state, key=lambda s: s.backend_id)
        obs = build_observation(state, N_MAX_BACKENDS, self.norm)
        return np.asarray(self.fn(obs, state))[:len(ss)]


# ── driver ───────────────────────────────────────────────────────────────────

def run_policy(name, procs, n_windows, rps, degrade=None, ewma=0.5):
    ports = [p for _, p in procs]
    n = len(ports)
    policy = make_policy(name, n)
    lat_ewma = np.full(n, 20.0)   # observed latency estimate per backend
    pool = ThreadPoolExecutor(max_workers=256)

    window_served = []     # per-window mean served latency (sim-comparable)
    all_lat = []           # per-request served latency
    all_status = []
    for w in range(n_windows):
        if degrade is not None and degrade["start"] <= w < degrade["start"] + degrade["span"]:
            set_delay(ports[degrade["idx"]], degrade["ms"])
        elif degrade is not None and w == degrade["start"] + degrade["span"]:
            set_delay(ports[degrade["idx"]], 0)

        stats = [get_stats(p) for p in ports]
        loads = np.array([float(s.get("in_flight", 0) + s.get("queue_depth", 0)) for s in stats])
        state = [BackendState(backend_id=f"backend_{i+1}", latency_ms=float(lat_ewma[i]),
                              queue_depth=float(loads[i]),
                              health=classify_health(float(lat_ewma[i]), 0.0)) for i in range(n)]
        weights = np.asarray(policy.weights(state), dtype=float)
        weights = np.clip(weights, 0, None)
        weights = weights / weights.sum() if weights.sum() else np.ones(n) / n

        counts = np.random.multinomial(rps, weights)
        t_start = time.perf_counter()
        futures = []
        for i, c in enumerate(counts):
            for _ in range(int(c)):
                futures.append(pool.submit(hit, ports[i]))
        results_by_backend = [[] for _ in range(n)]
        win_lat = []
        idx = 0
        for i, c in enumerate(counts):
            for _ in range(int(c)):
                ms, ok = futures[idx].result(); idx += 1
                all_status.append(ok)
                if ok:
                    all_lat.append(ms); win_lat.append(ms); results_by_backend[i].append(ms)
        # update EWMA from this window's observed served latency per backend
        for i in range(n):
            if results_by_backend[i]:
                lat_ewma[i] = (1 - ewma) * lat_ewma[i] + ewma * float(np.mean(results_by_backend[i]))
        if win_lat:
            window_served.append(float(np.mean(win_lat)))
        # pace to ~1s windows (but don't sleep negative)
        dt = time.perf_counter() - t_start
        if dt < 1.0:
            time.sleep(min(1.0 - dt, 1.0))
    pool.shutdown(wait=True)
    if degrade is not None:
        set_delay(ports[degrade["idx"]], 0)

    ws = np.asarray(window_served); al = np.asarray(all_lat); st = np.asarray(all_status)
    return {
        "policy": name,
        "window_p95": float(np.percentile(ws, 95)) if ws.size else None,
        "window_sla_pct": float(100 * np.mean(ws > SLA_MS)) if ws.size else None,
        "req_p95": float(np.percentile(al, 95)) if al.size else None,
        "req_mean": float(al.mean()) if al.size else None,
        "shed_pct": float(100 * (1 - st.mean())) if st.size else None,
        "n_req": int(st.size),
    }


SCENARIOS = {
    "heterogeneous": dict(means=[12, 20, 30, 40, 45], degrade=None),
    "degrading": dict(means=[18, 22, 28, 35, 40],
                      degrade={"idx": 2, "start": 12, "span": 14, "ms": 300}),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--policies", nargs="+", default=["round_robin", "candidate_mono", "candidate_v2"])
    ap.add_argument("--scenario", default="heterogeneous", choices=list(SCENARIOS))
    ap.add_argument("--windows", type=int, default=40)
    ap.add_argument("--rps", type=int, default=260)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    np.random.seed(a.seed)

    scn = SCENARIOS[a.scenario]
    print(f"[live] scenario={a.scenario} means={scn['means']} rps={a.rps} windows={a.windows}", flush=True)
    procs = start_backends(scn["means"])
    try:
        if not wait_healthy(procs):
            print("[live] backends failed to start", flush=True); return 1
        print("[live] backends healthy", flush=True)
        results = []
        for name in a.policies:
            # fresh warmup: clear any delay
            for _, port in procs:
                set_delay(port, 0)
            time.sleep(1.0)
            r = run_policy(name, procs, a.windows, a.rps, degrade=scn["degrade"])
            results.append(r)
            print(f"[live] {name:<22} window_p95={r['window_p95']:.0f}ms "
                  f"window_SLA%={r['window_sla_pct']:.1f} req_p95={r['req_p95']:.0f} "
                  f"shed%={r['shed_pct']:.1f} n={r['n_req']}", flush=True)
        out = _HERE / "results" / "live_stack.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        prev = json.loads(out.read_text()) if out.exists() else {}
        prev[a.scenario] = results
        out.write_text(json.dumps(prev, indent=2))
        print(f"[live] wrote {out}", flush=True)
    finally:
        for p, _ in procs:
            p.terminate()
        for p, _ in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
