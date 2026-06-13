"""
experiments/adaptive-bench/locust/locustfile.py
────────────────────────────────────────────────
Five-phase load shape for the adaptive-bench Round 2 harness (#156).

The phases are designed to exercise both adaptive paths SmartLoad ships:
forecast-driven scale-out (RQ4) and anomaly-driven reroute / scale-in.

| Phase                  | Window         | Users         | What it tests                                          |
|------------------------|----------------|---------------|--------------------------------------------------------|
| A_bootstrap            | 0 → 60 s       | ramp 0 → 20   | RQ4 first forecast — does the engine see the ramp?     |
| B_forecast_burst       | 60 → 90 s      | spike to 200  | Autoscaler grows the pool 1 → ~4 ahead of saturation   |
| C_sustain              | 90 → 240 s     | hold 200      | Larger pool sustains the load without tail-latency rot |
| D_anomaly_scale_down   | 240 → 300 s    | drop to 30    | Two adaptive paths concurrent: anomaly reroute +       |
|                        |                | + anomaly     | autoscaler scale-in. Anomaly injection done by run.py. |
| E_steady               | 300 → 360 s    | hold 30       | Stabilisation — system holds without oscillation       |

Each request is tagged with the current phase via the request name, so
locust's per-name stats produce phase-sliced latency distributions in the
CSV output. Phase boundaries also fire `events.request` so the
orchestrator's collectors can see phase transitions on the bench timeline.

Tuning knobs (env vars, all optional):
  RAMP_USERS         peak users in B_forecast_burst (default 200)
  STEADY_USERS       users in A_bootstrap top + E_steady (default 20 / 30)
  PHASE_A_END_SECS   end of A_bootstrap        (default 60)
  PHASE_B_END_SECS   end of B_forecast_burst   (default 90)
  PHASE_C_END_SECS   end of C_sustain          (default 240)
  PHASE_D_END_SECS   end of D_anomaly_scale_down (default 300)
  PHASE_E_END_SECS   end of E_steady (== run end) (default 360)
  TARGET_HOST        override the LB URL (default http://load-balancer)

The compressed harness-validation profile (SHORT=1) compresses the same
shape into 60 s total so the e2e test in `tests/e2e/adaptive-bench/` can
run in CI under 5 minutes. The orchestrator sets these env vars; this
file just reads them.
"""

from __future__ import annotations

import os
import random
from typing import Optional

from locust import HttpUser, LoadTestShape, between, events, task


# ── deterministic load-gen RNG (multi-run batching, #160 / SOT §35.3) ─────────
# Each run in a `--runs N` batch is launched with a distinct BENCH_SEED so the
# Locust wait-time jitter follows an independent-but-reproducible path per run.
# Caveat: this only fixes the *load-generation* RNG. Run-to-run variance from
# cold caches, JIT warm-up and container start ordering is NOT controlled by
# the seed — that residual spread is exactly what the per-metric confidence
# interval the harness reports is meant to capture.
random.seed(int(os.environ.get("BENCH_SEED", "0")))


# ── phase boundaries (absolute seconds since shape start) ─────────────────────

PHASE_A_END_SECS = int(os.environ.get("PHASE_A_END_SECS", "60"))
PHASE_B_END_SECS = int(os.environ.get("PHASE_B_END_SECS", "90"))
PHASE_C_END_SECS = int(os.environ.get("PHASE_C_END_SECS", "240"))
PHASE_D_END_SECS = int(os.environ.get("PHASE_D_END_SECS", "300"))
PHASE_E_END_SECS = int(os.environ.get("PHASE_E_END_SECS", "360"))

# ── user-count knobs ──────────────────────────────────────────────────────────

A_USERS = int(os.environ.get("A_USERS", "20"))      # top of A_bootstrap ramp
B_USERS = int(os.environ.get("RAMP_USERS", "200"))  # forecast-burst peak
C_USERS = int(os.environ.get("C_USERS", str(B_USERS)))  # held through C
D_USERS = int(os.environ.get("D_USERS", "30"))      # scale-down target in D
E_USERS = int(os.environ.get("STEADY_USERS", str(D_USERS)))  # held through E

# Spawn-rate target (users/sec) when transitioning between phases. Locust
# enforces this as an upper bound, not a curve — fast enough that the
# 30-second B_forecast_burst window can reach 200u without saturating spawn.
SPAWN_RATE = max(1, int(os.environ.get("SPAWN_RATE", "20")))


# ── per-request phase tagging ─────────────────────────────────────────────────

_CURRENT_PHASE: str = "A_bootstrap"


def _set_phase(p: str) -> None:
    """Flip the phase marker and emit a Locust event so the orchestrator's
    SSE / stats consumers can see the boundary on the same timeline as
    request stats."""
    global _CURRENT_PHASE
    if _CURRENT_PHASE != p:
        _CURRENT_PHASE = p
        events.request.fire(
            request_type="PHASE",
            name=f"phase={p}",
            response_time=0,
            response_length=0,
            response=None,
            context={},
            exception=None,
            url="-",
        )


class AdaptiveBenchUser(HttpUser):
    """Single-task user that GETs the LB root path and tags each request
    with the current phase. Short wait keeps per-user RPS modest so the
    shape's user-count curve dominates the aggregate load."""

    wait_time = between(0.05, 0.20)
    host = os.environ.get("TARGET_HOST", "http://load-balancer")

    @task
    def hit_root(self) -> None:
        with self.client.get(
            "/",
            name=f"GET-/-{_CURRENT_PHASE}",
            catch_response=True,
        ) as resp:
            if resp.status_code >= 500:
                resp.failure(f"5xx: {resp.status_code}")
            elif resp.status_code >= 400:
                resp.failure(f"4xx: {resp.status_code}")
            else:
                resp.success()


class FivePhaseShape(LoadTestShape):
    """Drives the 5-phase user-count curve. Each tick returns
    (target_users, spawn_rate) and flips the module-level phase marker so
    per-request tagging stays in sync with the curve.

    Locust polls `tick()` on a ~1s cadence by default, which is enough
    resolution for 30s-and-up phases. Returning None ends the run cleanly."""

    def tick(self) -> Optional[tuple[int, int]]:
        t = self.get_run_time()

        if t >= PHASE_E_END_SECS:
            return None  # shape exhausted; locust shuts down

        if t < PHASE_A_END_SECS:
            # A_bootstrap: ramp 0 → A_USERS linearly across the window
            users = max(1, int(A_USERS * (t / max(1, PHASE_A_END_SECS))))
            _set_phase("A_bootstrap")
            return users, SPAWN_RATE

        if t < PHASE_B_END_SECS:
            # B_forecast_burst: spike to B_USERS. With ~30s window and a
            # spawn rate of 20/s, locust reaches 200u in ~10s and holds.
            _set_phase("B_forecast_burst")
            return B_USERS, SPAWN_RATE

        if t < PHASE_C_END_SECS:
            # C_sustain: hold at C_USERS (== B_USERS by default).
            _set_phase("C_sustain")
            return C_USERS, SPAWN_RATE

        if t < PHASE_D_END_SECS:
            # D_anomaly_scale_down: drop to D_USERS. The anomaly itself is
            # injected by run.py at this phase's start — this shape only
            # drives the user-count curve.
            _set_phase("D_anomaly_scale_down")
            return D_USERS, SPAWN_RATE

        # E_steady: hold at E_USERS through PHASE_E_END_SECS
        _set_phase("E_steady")
        return E_USERS, SPAWN_RATE


# ── lifecycle hooks for orchestrator visibility ──────────────────────────────

@events.test_start.add_listener
def _on_start(environment, **_kwargs) -> None:
    print(
        f"[locust] adaptive-bench shape -- "
        f"A({PHASE_A_END_SECS}s, 0->{A_USERS}u) | "
        f"B({PHASE_A_END_SECS}->{PHASE_B_END_SECS}s, spike {B_USERS}u) | "
        f"C({PHASE_B_END_SECS}->{PHASE_C_END_SECS}s, hold {C_USERS}u) | "
        f"D({PHASE_C_END_SECS}->{PHASE_D_END_SECS}s, drop {D_USERS}u + anomaly) | "
        f"E({PHASE_D_END_SECS}->{PHASE_E_END_SECS}s, hold {E_USERS}u) "
        f"(target={AdaptiveBenchUser.host})",
        flush=True,
    )


@events.test_stop.add_listener
def _on_stop(environment, **_kwargs) -> None:
    print(f"[locust] adaptive-bench complete -- final phase was {_CURRENT_PHASE}", flush=True)
