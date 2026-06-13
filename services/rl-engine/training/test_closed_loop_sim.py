"""
Smoke test for closed_loop_sim.py — validates the *premise* the whole retrain
depends on, with no SB3/GPU: under the causal queue, (1) latency rises with load
and sheds past capacity, (2) spreading beats concentrating on a homogeneous
pool, (3) capacity-proportional beats even on a heterogeneous pool, (4) a
degradation spikes the degraded backend. Run: python test_closed_loop_sim.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from training.closed_loop_sim import (   # noqa: E402
    BackendProfile, ClosedLoopSimulator, queue_response, sample_scenario,
)


def _pool_latency(weights, profiles, total_rps):
    w = np.asarray(weights, float)
    w = w / w.sum()
    routed = w * total_rps
    lat, shed = [], []
    for i, p in enumerate(profiles):
        lt, s = queue_response(routed[i], p.workers, p.service_s, p.queue_max)
        lat.append(lt)
        shed.append(s)
    served = routed * (1 - np.array(shed))
    return float((np.array(lat) * served).sum() / served.sum()), float(
        (routed * np.array(shed)).sum() / routed.sum())


def test_latency_rises_and_sheds():
    # One backend, 2 workers, 20ms => capacity 100 rps.
    p = BackendProfile("b", workers=2, service_mean_ms=20.0, queue_max=64)
    l_low, s_low = queue_response(40, p.workers, p.service_s, p.queue_max)    # rho 0.4
    l_hi, s_hi = queue_response(95, p.workers, p.service_s, p.queue_max)      # rho 0.95
    l_over, s_over = queue_response(160, p.workers, p.service_s, p.queue_max)  # rho 1.6
    assert l_low < l_hi < l_over, (l_low, l_hi, l_over)
    assert s_low == 0 and s_hi == 0 and s_over > 0.2, (s_low, s_hi, s_over)
    print(f"  latency 40rps={l_low:.0f}ms  95rps={l_hi:.0f}ms  160rps={l_over:.0f}ms shed={s_over:.2f}  OK")


def test_spread_beats_concentrate_homogeneous():
    profs = [BackendProfile(f"b{i}", workers=2, service_mean_ms=20.0) for i in range(5)]
    cap = sum(p.capacity_rps for p in profs)           # 500 rps
    total = 0.8 * cap                                  # 400 rps, pool rho 0.8
    even, _ = _pool_latency([1, 1, 1, 1, 1], profs, total)
    conc, conc_shed = _pool_latency([0.7, 0.075, 0.075, 0.075, 0.075], profs, total)  # PPO shape
    assert even < conc, (even, conc)
    print(f"  homogeneous @80% load: even={even:.0f}ms  PPO-concentrate={conc:.0f}ms (shed={conc_shed:.2f})  OK")


def test_capacity_proportional_beats_even_heterogeneous():
    # One slow backend (60ms) among fast ones (15ms).
    profs = [BackendProfile("b0", 2, 60.0)] + [BackendProfile(f"b{i}", 2, 15.0) for i in range(1, 5)]
    caps = np.array([p.capacity_rps for p in profs])
    total = 0.7 * caps.sum()
    even, _ = _pool_latency(np.ones(5), profs, total)
    prop, _ = _pool_latency(caps, profs, total)        # least-connections-like
    assert prop < even, (prop, even)
    print(f"  heterogeneous @70% load: even={even:.0f}ms  capacity-proportional={prop:.0f}ms  OK")


def test_degradation_spikes_backend():
    sim = ClosedLoopSimulator(n_backends=5, episode_length=64)
    # find a degrading scenario
    for s in range(200):
        sim.reset(seed=s)
        if sim.scenario_kind == "degrading":
            break
    assert sim.scenario_kind == "degrading"
    idx, start, span, extra = sim._scn.degrade
    sim._step = start + 1
    state = sim.step(np.ones(5) / 5)[0]
    lat = sorted(state, key=lambda x: x.backend_id)[idx].latency_ms
    others = [s.latency_ms for j, s in enumerate(sorted(state, key=lambda x: x.backend_id)) if j != idx]
    assert lat > max(others), (lat, others)
    print(f"  degradation: backend_{idx+1} lat={lat:.0f}ms > others max={max(others):.0f}ms  OK")


def test_scenario_curriculum_covers_three_kinds():
    rng = np.random.default_rng(7)
    kinds = {sample_scenario(rng, 5, 64).kind for _ in range(60)}
    assert {"homogeneous", "heterogeneous", "degrading"} <= kinds, kinds
    print(f"  curriculum kinds present: {sorted(kinds)}  OK")


if __name__ == "__main__":
    for fn in [
        test_latency_rises_and_sheds,
        test_spread_beats_concentrate_homogeneous,
        test_capacity_proportional_beats_even_heterogeneous,
        test_degradation_spikes_backend,
        test_scenario_curriculum_covers_three_kinds,
    ]:
        print(f"- {fn.__name__}")
        fn()
    print("\nALL SMOKE CHECKS PASSED")
