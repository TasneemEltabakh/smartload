"""
experiments/adaptive-bench/anomaly_injector.py
───────────────────────────────────────────────
Phase-D anomaly injector for adaptive-bench Round 2 (#156).

When the 5-phase Locust shape transitions into D_anomaly_scale_down
(t=240 s by default), the orchestrator schedules this coroutine to:

  1. Pick a running test-backend, preferring one labelled `smartload.dynamic=true`
     (provisioned by #155 R1's autoscaler `provision()` lifecycle) so the
     run exercises anomaly handling on a dynamic backend specifically. If
     no dynamic backend is up at injection time, fall back to any
     compose-provisioned `test-backend-*`.
  2. POST /_admin/delay {ms: 200} to the chosen backend via `docker exec`
     (the test-backend ports aren't host-published; we hit the in-container
     endpoint). Under the closed-loop backend this delay occupies a worker
     slot, collapsing the backend's throughput and building a queue, so its
     latency balloons to delay + queue-wait rather than a flat +200 ms. The
     request still completes (slow, not failed) and returns 200 as long as the
     queue does not overflow to a 503 shed — true here because phase D holds
     only ~30 users, well under the backend's QUEUE_MAX (default 64). NGINX's
     passive `max_fails` therefore does not trip on the slow responses, so this
     is the case where only SmartLoad's explicit anomaly signal can downweight
     the slow backend.
  3. POST /api/v1/isolate to the anomaly-detector to publish a synthetic
     AnomalyEvent. The detector's run loop hasn't observed enough latency
     yet to fire on its own this early — the manual call short-circuits
     the detection window so we can measure reroute latency cleanly.

After PHASE_D_END_SECS the injector restores backend latency to 0 ms and
publishes a recovery event. The orchestrator's post-flight safety-net
calls `clear_runtime_delay()` on every running backend regardless, so a
mid-run kill -9 still leaves the stack in a clean state.

Why split this out of run.py: keeping the injection logic in its own
module lets R3's analysis pipeline parse the `[anomaly]` log markers
without coupling to the orchestrator's lifecycle code.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone


DEFAULT_ANOMALY_DELAY_MS = 200
DEFAULT_ANOMALY_DETECTOR_URL = "http://localhost:8082"
DEFAULT_TEST_BACKEND_INTERNAL_PORT = 8080
DEFAULT_DYNAMIC_LABEL = "smartload.dynamic=true"


# ── container picking ────────────────────────────────────────────────────────

async def list_running_backends(
    *,
    prefer_dynamic_label: str = DEFAULT_DYNAMIC_LABEL,
) -> list[str]:
    """Return the names of running test-backend containers, dynamic ones first.

    Uses `docker ps --filter` rather than the SDK to avoid a docker-py
    dependency on the bench host. Two passes:
      1. label=smartload.dynamic=true status=running     (provisioned)
      2. label=com.docker.compose.service=test-backend status=running (compose)

    De-duplicates while preserving the first-pass-first ordering, so any
    caller that takes [0] will get a dynamic backend whenever one is up.
    """
    async def _pass(filter_arg: str) -> list[str]:
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps",
            "--filter", filter_arg,
            "--filter", "status=running",
            "--format", "{{.Names}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return []
        if proc.returncode != 0:
            return []
        return [name for name in stdout.decode("utf-8").splitlines() if name]

    dynamic = await _pass(f"label={prefer_dynamic_label}")
    compose = await _pass("label=com.docker.compose.service=test-backend")

    seen: set[str] = set()
    ordered: list[str] = []
    for name in (*dynamic, *compose):
        if name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return ordered


# ── delay control via docker exec ─────────────────────────────────────────────

async def set_runtime_delay(
    container: str,
    delay_ms: int,
    *,
    port: int = DEFAULT_TEST_BACKEND_INTERNAL_PORT,
) -> bool:
    """POST /_admin/delay {ms: delay_ms} on `container` via `docker exec`.

    Returns True on a successful exec exit (0). Any failure (container
    missing, exec error, network error inside the container) returns
    False — the injector logs the failure and continues."""
    payload = json.dumps({"ms": int(delay_ms)})
    cmd = [
        "docker", "exec", container,
        "sh", "-c",
        # wget is in the test-backend image (alpine/node base); avoids a
        # curl dependency. -q silences progress; -O - writes to stdout
        # (we don't need the body, just the exit code).
        f"wget -q -O - --post-data='{payload}' "
        f"--header='Content-Type: application/json' "
        f"http://localhost:{port}/_admin/delay",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return False
    return proc.returncode == 0


async def clear_runtime_delay_on_all(*, prefer_dynamic_label: str = DEFAULT_DYNAMIC_LABEL) -> int:
    """Defence-in-depth post-flight: set delay=0 on every running test-backend.

    Used by the orchestrator's post-flight cleanup so a partial bench leaves
    no backend permanently slow. Returns the number of containers reset."""
    names = await list_running_backends(prefer_dynamic_label=prefer_dynamic_label)
    if not names:
        return 0
    results = await asyncio.gather(
        *(set_runtime_delay(n, 0) for n in names),
        return_exceptions=True,
    )
    return sum(1 for r in results if r is True)


# ── anomaly-detector isolate publish ──────────────────────────────────────────

async def publish_anomaly_event(
    *,
    backend_id: str,
    status: str,
    reason: str,
    actor: str = "adaptive-bench",
    anomaly_detector_url: str = DEFAULT_ANOMALY_DETECTOR_URL,
) -> bool:
    """POST /api/v1/isolate to the anomaly-detector.

    Bypasses the engine's run loop — the operator's intent is the signal.
    Returns True on HTTP 200, False otherwise. Imports `aiohttp` lazily so
    a unit test importing this module without aiohttp installed doesn't
    blow up at import time."""
    import aiohttp

    payload = {
        "backend_id": backend_id,
        "status":     status,
        "actor":      actor,
        "reason":     reason,
    }
    url = anomaly_detector_url.rstrip("/") + "/api/v1/isolate"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=5.0),
            ) as resp:
                return resp.status == 200
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return False


# ── the injector coroutine itself ─────────────────────────────────────────────

async def inject_at(
    *,
    fire_at_secs: float,
    recover_at_secs: float,
    bench_start_monotonic: float,
    stop_event: asyncio.Event,
    output_log: list[dict] | None = None,
    anomaly_delay_ms: int = DEFAULT_ANOMALY_DELAY_MS,
    anomaly_detector_url: str = DEFAULT_ANOMALY_DETECTOR_URL,
) -> dict | None:
    """Sleep until `fire_at_secs` of bench time, inject, then sleep until
    `recover_at_secs` and clear the delay.

    `bench_start_monotonic` is `time.monotonic()` at orchestrator start, so
    the injector sleeps the right number of seconds regardless of how long
    pre-flight took. `output_log` (optional) collects the injection record
    for inclusion in MANIFEST.json — pass None if the caller doesn't need
    the trail.

    Returns the injection record (or None if the bench was stopped before
    fire_at_secs)."""
    import time as _time

    async def _sleep_until(absolute_secs: float) -> bool:
        delay = max(0.0, absolute_secs - (_time.monotonic() - bench_start_monotonic))
        if delay == 0.0 and not stop_event.is_set():
            return True
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
            return False  # stop_event fired first
        except asyncio.TimeoutError:
            return True

    # Sleep until phase-D begins
    if not await _sleep_until(fire_at_secs):
        return None

    targets = await list_running_backends()
    if not targets:
        record = {
            "injected_at": datetime.now(timezone.utc).isoformat(),
            "error":       "no running test-backend containers found",
        }
        if output_log is not None:
            output_log.append(record)
        return record

    target = targets[0]
    is_dynamic = await _is_dynamic_container(target)

    print(f"[anomaly] t={fire_at_secs:.0f}s injecting {target} "
          f"+{anomaly_delay_ms}ms (dynamic={is_dynamic})", flush=True)

    delay_ok = await set_runtime_delay(target, anomaly_delay_ms)
    isolate_ok = await publish_anomaly_event(
        backend_id=target,
        status="unhealthy",
        reason="adaptive-bench phase-D latency-spike injection",
        anomaly_detector_url=anomaly_detector_url,
    )

    record = {
        "injected_at": datetime.now(timezone.utc).isoformat(),
        "target":      target,
        "is_dynamic":  is_dynamic,
        "delay_ms":    anomaly_delay_ms,
        "delay_set":   delay_ok,
        "isolate_published": isolate_ok,
    }
    if output_log is not None:
        output_log.append(record)

    # Wait through phase D; recover at phase-D end
    if not await _sleep_until(recover_at_secs):
        # Bench cut short before recovery — still try to clear so we don't
        # leave the backend slow. Post-flight cleanup will do this too;
        # belt-and-braces here for the case where post-flight fails.
        await set_runtime_delay(target, 0)
        return record

    print(f"[anomaly] t={recover_at_secs:.0f}s recovering {target}", flush=True)
    await set_runtime_delay(target, 0)
    await publish_anomaly_event(
        backend_id=target,
        status="healthy",
        reason="adaptive-bench phase-D anomaly recovery",
        anomaly_detector_url=anomaly_detector_url,
    )
    record["recovered_at"] = datetime.now(timezone.utc).isoformat()
    return record


async def _is_dynamic_container(name: str) -> bool:
    """Whether `name` carries the smartload.dynamic=true label. Best-effort."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "inspect",
        "--format", "{{ index .Config.Labels \"smartload.dynamic\" }}",
        name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return False
    if proc.returncode != 0:
        return False
    return stdout.decode("utf-8").strip() == "true"
