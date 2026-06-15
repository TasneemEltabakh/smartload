"""
experiments/adaptive-advantage/locust/locustfile.py
────────────────────────────────────────────────────
A harder, well-rounded SmartLoad-vs-NGINX-RR load profile.

Where the original baseline-vs-smartload benchmark deliberately kept the load
*below* the queue depth (closed-loop max-in-flight <= QUEUE_MAX, so nothing ever
sheds and a slow backend only adds queue-wait latency), this profile drives the
pool past that knee and adds a traffic spike, so the comparison exposes the three
things SmartLoad actually does that static round-robin cannot:

  1. Excluding a backend that DEGRADES INTO 503-SHEDDING. Past QUEUE_MAX a
     severely-slowed backend sheds 503. NGINX RR runs `max_fails=0`, so it can
     NEVER eject it and keeps routing 1/N of traffic onto it. SmartLoad's error
     channel detects it and the sidecar pulls it out.
  2. Absorbing a SPIKE by scaling out. A fixed 5-backend pool saturates under the
     spike; SmartLoad's autoscaler grows the pool.
  3. Re-routing around a backend that is SLOW-BUT-NOT-FAILING (latency channel),
     while keeping the healthy pack — without the over-exclusion cascade.

The orchestrator (run.sh) injects the backend anomalies on a schedule via
/_admin/delay ONLY (no manual /isolate hint) — SmartLoad must DETECT them.

Five phases (wall-clock seconds since shape start):
  A_ramp     0  -> R1   ramp to STEADY_USERS
  B_degrade  R1 -> B2   hold STEADY_USERS  (backend-1 driven into 503-shed)
  C_spike    B2 -> C2   SPIKE to SPIKE_USERS (backend-1 recovered; tests scale-out)
  D_slow     C2 -> D2   back to STEADY_USERS (backend-2 slowed; tests reroute)
  E_tail     D2 -> END  hold STEADY_USERS (backend-2 recovered; settle)

Env knobs (all optional):
  STEADY_USERS   concurrent users at steady state         (default 90)
  SPIKE_USERS    concurrent users during the spike         (default 180)
  RAMP_SECS      A_ramp duration                           (default 60)
  B_END_SECS     end of B_degrade                          (default 180)
  C_END_SECS     end of C_spike                            (default 240)
  D_END_SECS     end of D_slow                             (default 360)
  END_SECS       end of E_tail / shape                     (default 420)
  TARGET_HOST    LB url                                    (default http://load-balancer)
"""

from __future__ import annotations

import os
import random
from typing import Optional

from locust import HttpUser, LoadTestShape, between, events, task

random.seed(int(os.environ.get("BENCH_SEED", "0")))

STEADY_USERS = int(os.environ.get("STEADY_USERS", "90"))
SPIKE_USERS = int(os.environ.get("SPIKE_USERS", "180"))
RAMP_SECS = int(os.environ.get("RAMP_SECS", "60"))
B_END_SECS = int(os.environ.get("B_END_SECS", "180"))
C_END_SECS = int(os.environ.get("C_END_SECS", "240"))
D_END_SECS = int(os.environ.get("D_END_SECS", "360"))
END_SECS = int(os.environ.get("END_SECS", "420"))

_CURRENT_PHASE: str = "A_ramp"


def _set_phase(p: str) -> None:
    global _CURRENT_PHASE
    if _CURRENT_PHASE != p:
        _CURRENT_PHASE = p
        events.request.fire(
            request_type="PHASE", name=f"phase={p}", response_time=0,
            response_length=0, response=None, context={}, exception=None, url="-",
        )


class SmartLoadUser(HttpUser):
    # Think time tuned so STEADY users keep the HEALTHY pool UNDER its ~500 rps
    # ceiling (so baseline's non-anomaly phases are healthy and the difference is
    # purely SmartLoad's adaptation), while still > QUEUE_MAX concurrency so a
    # severely-degraded backend's queue overflows and sheds 503.
    wait_time = between(0.10, 0.20)
    host = os.environ.get("TARGET_HOST", "http://load-balancer")

    @task
    def hit_root(self) -> None:
        with self.client.get(
            "/", name=f"GET-/-{_CURRENT_PHASE}", catch_response=True,
        ) as resp:
            if resp.status_code >= 500:
                resp.failure(f"5xx: {resp.status_code}")
            elif resp.status_code >= 400:
                resp.failure(f"4xx: {resp.status_code}")
            else:
                resp.success()


class FivePhaseShape(LoadTestShape):
    def tick(self) -> Optional[tuple[int, int]]:
        t = self.get_run_time()
        if t >= END_SECS:
            return None
        if t < RAMP_SECS:
            _set_phase("A_ramp")
            return max(1, int(STEADY_USERS * (t / RAMP_SECS))), max(1, STEADY_USERS // RAMP_SECS)
        if t < B_END_SECS:
            _set_phase("B_degrade")
            return STEADY_USERS, max(1, STEADY_USERS // 4)
        if t < C_END_SECS:
            _set_phase("C_spike")
            # spawn fast so the spike actually arrives quickly
            return SPIKE_USERS, max(1, SPIKE_USERS // 6)
        if t < D_END_SECS:
            _set_phase("D_slow")
            return STEADY_USERS, max(1, STEADY_USERS // 4)
        _set_phase("E_tail")
        return STEADY_USERS, max(1, STEADY_USERS // 4)


@events.test_start.add_listener
def _on_start(environment, **_kwargs) -> None:
    print(
        f"[locust] adaptive-advantage: steady {STEADY_USERS}u / spike {SPIKE_USERS}u | "
        f"A_ramp<{RAMP_SECS} B_degrade<{B_END_SECS} C_spike<{C_END_SECS} "
        f"D_slow<{D_END_SECS} E_tail<{END_SECS} (target={SmartLoadUser.host})",
        flush=True,
    )
