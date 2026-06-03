"""
experiments/baseline-vs-smartload/locust/locustfile.py
───────────────────────────────────────────────────────
Three-phase load profile for the SmartLoad vs NGINX RR benchmark (#148).

Phase A — Steady ramp (0 → RAMP_USERS over RAMP_SECS).
  Exercises forecast-driven scale-out before backends saturate. In SmartLoad
  mode the forecasting service should publish a `ForecastResult` ahead of
  saturation and the autoscaler should add backends pre-emptively. In
  baseline mode there is no such anticipation.

Phase B — Anomaly injection (RAMP_USERS held; one backend slowed at t=ANOMALY_AT_SECS).
  Tests if the anomaly-detector + RL-engine + lb-sidecar pipeline can pull
  the bad backend out of rotation. The injection itself is done by the
  orchestration script via `docker exec` against backend-1; this file just
  drives traffic.

Phase C — Sustained tail (RAMP_USERS held until SUSTAIN_END_SECS).
  High RPS held to expose tail-latency differences. Caller observes
  per-request status + duration; we tag each request with the phase so
  the post-run plotter can slice cleanly.

All phase boundaries are in wall-clock seconds since the load shape starts.
Locust's `LoadTestShape` ABC drives the user count; tasks run independently.

Tuning knobs (env vars, all optional):
  RAMP_USERS         total concurrent users at the top of the ramp (default 50)
  RAMP_SECS          ramp duration in seconds (default 60)
  ANOMALY_AT_SECS    absolute time (since shape start) when phase B begins (default 120)
  ANOMALY_HOLD_SECS  how long phase B holds before phase C (default 60)
  SUSTAIN_END_SECS   absolute time when the shape ends (default 360)
  TARGET_HOST        override the load-balancer URL (default http://load-balancer)
"""

from __future__ import annotations

import os
import time
from typing import Optional

from locust import HttpUser, LoadTestShape, between, events, task


RAMP_USERS = int(os.environ.get("RAMP_USERS", "50"))
RAMP_SECS = int(os.environ.get("RAMP_SECS", "60"))
ANOMALY_AT_SECS = int(os.environ.get("ANOMALY_AT_SECS", "120"))
ANOMALY_HOLD_SECS = int(os.environ.get("ANOMALY_HOLD_SECS", "60"))
SUSTAIN_END_SECS = int(os.environ.get("SUSTAIN_END_SECS", "360"))


# Each user logs the phase it's running in so post-run analysis can slice
# clean per-phase. The shape transitions update this module-level marker.
_CURRENT_PHASE: str = "A_ramp"


def _set_phase(p: str) -> None:
    global _CURRENT_PHASE
    if _CURRENT_PHASE != p:
        _CURRENT_PHASE = p
        # Emit a Locust event so the operator-side console + per-request
        # tagging line up on the timeline.
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


class SmartLoadUser(HttpUser):
    """Single-task user that GETs the LB's root path and tags each request
    with the current phase. Wait time keeps per-user RPS modest so the
    ramp determines aggregate load, not bursty user behaviour."""

    wait_time = between(0.05, 0.20)
    host = os.environ.get("TARGET_HOST", "http://load-balancer")

    @task
    def hit_root(self) -> None:
        # Tag the request name with the phase so locust's per-name stats
        # produce phase-sliced latency distributions in the CSV.
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


class ThreePhaseShape(LoadTestShape):
    """Ramp to RAMP_USERS over RAMP_SECS, hold until SUSTAIN_END_SECS,
    flipping the phase marker at each boundary so per-phase stats are
    distinguishable in the output."""

    def tick(self) -> Optional[tuple[int, int]]:
        run_time = self.get_run_time()
        if run_time >= SUSTAIN_END_SECS:
            return None  # done

        if run_time < RAMP_SECS:
            users = max(1, int(RAMP_USERS * (run_time / RAMP_SECS)))
            spawn_rate = max(1, RAMP_USERS // max(1, RAMP_SECS))
            _set_phase("A_ramp")
            return users, spawn_rate

        # Phase boundary B (anomaly injected).
        if ANOMALY_AT_SECS <= run_time < (ANOMALY_AT_SECS + ANOMALY_HOLD_SECS):
            _set_phase("B_anomaly")
            return RAMP_USERS, max(1, RAMP_USERS // 4)

        if run_time >= (ANOMALY_AT_SECS + ANOMALY_HOLD_SECS):
            _set_phase("C_sustain")
            return RAMP_USERS, max(1, RAMP_USERS // 4)

        # Between ramp end and anomaly start: hold the ramp users.
        _set_phase("A_hold")
        return RAMP_USERS, max(1, RAMP_USERS // 4)


@events.test_start.add_listener
def _on_start(environment, **_kwargs) -> None:
    print(
        f"[locust] phase-plan: ramp {RAMP_USERS}u over {RAMP_SECS}s | "
        f"anomaly at {ANOMALY_AT_SECS}s for {ANOMALY_HOLD_SECS}s | "
        f"sustain until {SUSTAIN_END_SECS}s "
        f"(target={SmartLoadUser.host})"
    )


@events.test_stop.add_listener
def _on_stop(environment, **_kwargs) -> None:
    print(f"[locust] phase-plan: final phase was {_CURRENT_PHASE}")
