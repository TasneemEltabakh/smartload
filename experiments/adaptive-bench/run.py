"""
experiments/adaptive-bench/run.py
──────────────────────────────────
Round 2 orchestrator for the adaptive-bench programme (#156).

Lifecycle (strict order — post-flight runs even if mid-flight fails):

  pre-flight
  ├── docker compose ps health check
  ├── snapshot current policy.yaml (for restore)
  ├── push temporary policy override: autoscaler_cooldown_seconds=10
  │     (Risk-1 de-risk: the default 60 s cooldown blocks the
  │      Phase-B 30 s spike from firing more than one scale-out.)
  ├── set AUTOSCALER_PROVISIONING_ENABLED=true via .env file
  └── docker compose up -d --force-recreate autoscaler
        (picks up the new env var so the dynamic-pool lifecycle is live)

  concurrent
  ├── prom_collector       (1 Hz Prometheus → prom_timeseries.parquet)
  ├── sse_collector        (BFF SSE → decision_envelopes.jsonl)
  ├── upstream_watcher     (2 s docker exec cat → upstream_changes.jsonl)
  ├── anomaly_injector     (sleeps until phase-D start, then injects)
  └── locust subprocess    (5-phase shape; --csv-full-history)

  post-flight  (runs in `finally:` so failure modes are caught)
  ├── snapshot /api/v1/status → post_status.json
  ├── snapshot /api/v1/audit/scaling?limit=200 → scaling_audit.json
  ├── flip env-file back (AUTOSCALER_PROVISIONING_ENABLED=false)
  ├── docker compose up -d --force-recreate autoscaler
  ├── restore policy (cooldown_seconds back to file value)
  └── tear down any leftover smartload.dynamic=true containers
        (defence-in-depth — the autoscaler's own scale-in path should
         have done this already)

Usage:
  python experiments/adaptive-bench/run.py --output-root experiments/adaptive-bench/results
  python experiments/adaptive-bench/run.py --output-root /tmp/runs --short

The --short flag compresses the 360 s shape into 60 s with the same
five-phase structure — used by the CI e2e test
(tests/e2e/adaptive-bench/test_compressed_run.py).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from collectors import prom_collector, sse_collector, upstream_watcher
import anomaly_injector


# ── constants ─────────────────────────────────────────────────────────────────

BENCH_VERSION = "r2.0.0"   # bump when the artefact set or shape changes

# Phase boundaries — full-length defaults; --short overrides these
FULL_PHASES = {
    "PHASE_A_END_SECS": 60,
    "PHASE_B_END_SECS": 90,
    "PHASE_C_END_SECS": 240,
    "PHASE_D_END_SECS": 300,
    "PHASE_E_END_SECS": 360,
    "A_USERS":          20,
    "RAMP_USERS":       200,   # B peak
    "C_USERS":          200,
    "D_USERS":          30,
    "STEADY_USERS":     30,
    "SPAWN_RATE":       20,
}

SHORT_PHASES = {
    "PHASE_A_END_SECS": 10,
    "PHASE_B_END_SECS": 15,
    "PHASE_C_END_SECS": 40,
    "PHASE_D_END_SECS": 50,
    "PHASE_E_END_SECS": 60,
    "A_USERS":          5,
    "RAMP_USERS":       20,
    "C_USERS":          20,
    "D_USERS":          8,
    "STEADY_USERS":     8,
    "SPAWN_RATE":       10,
}

# Service URLs (all on localhost — orchestrator runs on the bench host)
STATUS_URL          = "http://localhost:8090/api/v1/status"
SCALING_AUDIT_URL   = "http://localhost:8085/api/v1/audit/scaling?limit=200"
POLICY_GET_URL      = "http://localhost:8086/api/v1/policy"
POLICY_POST_URL     = "http://localhost:8086/api/v1/policy"
LB_HOST_URL         = "http://localhost:8080"

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE_PATH = REPO_ROOT / "experiments" / "adaptive-bench" / ".env.bench"


# ── small synchronous helpers ─────────────────────────────────────────────────

def _utc_ts_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _git_sha() -> tuple[str, str]:
    """Return (sha, dirty|clean). Best-effort — falls back to ('unknown', 'unknown')."""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown", "unknown"
    try:
        porcelain = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=REPO_ROOT,
            text=True, stderr=subprocess.DEVNULL,
        )
        dirty = "dirty" if porcelain.strip() else "clean"
    except subprocess.CalledProcessError:
        dirty = "unknown"
    return sha, dirty


def _http_get_json(url: str, *, timeout: float = 5.0) -> dict | None:
    """One-shot GET that returns the parsed JSON or None on any failure.

    Used for the pre/post snapshots — failure is logged but not fatal so a
    transient hiccup doesn't sink the whole bench run."""
    import urllib.request
    import urllib.error

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None


def _http_post_json(url: str, payload: dict, *, timeout: float = 5.0) -> tuple[int, dict | None]:
    """One-shot POST that returns (status_code, parsed_body_or_None).

    -1 status indicates a network/transport failure (no HTTP response)."""
    import urllib.request
    import urllib.error

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Actor":      "adaptive-bench",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(data) if data else None
            except json.JSONDecodeError:
                return resp.status, None
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except (json.JSONDecodeError, ValueError):
            return e.code, None
    except (urllib.error.URLError, TimeoutError):
        return -1, None


# ── pre-flight ───────────────────────────────────────────────────────────────

def _wait_for_status(deadline_secs: float = 60.0) -> str:
    """Poll /api/v1/status until overall is ok or degraded. Returns the
    final overall value (or 'unreachable')."""
    deadline = time.monotonic() + deadline_secs
    while time.monotonic() < deadline:
        body = _http_get_json(STATUS_URL, timeout=3.0)
        if body is not None:
            overall = body.get("overall", "?")
            if overall in ("ok", "degraded"):
                return overall
        time.sleep(2.0)
    return "unreachable"


def _write_env_file(provisioning_enabled: bool) -> None:
    """Write the bench's compose env-file. We keep just the autoscaler-
    provisioning knob here so other deployment defaults stay in
    docker-compose.yml's `environment` block."""
    ENV_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    flag = "true" if provisioning_enabled else "false"
    ENV_FILE_PATH.write_text(
        f"# adaptive-bench env-file — overridden by run.py; restored on teardown.\n"
        f"AUTOSCALER_PROVISIONING_ENABLED={flag}\n"
    )


def _force_recreate_autoscaler() -> int:
    """Bring the autoscaler back up with the env-file's current value."""
    proc = subprocess.run(
        [
            "docker", "compose",
            "--env-file", str(ENV_FILE_PATH),
            "up", "-d", "--force-recreate", "autoscaler",
        ],
        cwd=REPO_ROOT,
        capture_output=True, text=True,
    )
    return proc.returncode


def _snapshot_policy() -> dict | None:
    body = _http_get_json(POLICY_GET_URL, timeout=3.0)
    if body is None:
        return None
    # Some policy-manager versions wrap the policy under "policy"; others
    # return the policy fields at the top level. Normalise to the dict the
    # POST endpoint accepts.
    if isinstance(body, dict) and "policy" in body and isinstance(body["policy"], dict):
        return body["policy"]
    return body


def _push_policy_overrides(updates: dict) -> bool:
    """POST a partial policy. Returns True on 200, False otherwise."""
    code, _body = _http_post_json(POLICY_POST_URL, updates, timeout=5.0)
    return code == 200


# ── post-flight cleanup helpers ──────────────────────────────────────────────

def _list_dynamic_backends() -> list[str]:
    proc = subprocess.run(
        [
            "docker", "ps",
            "--filter", "label=smartload.dynamic=true",
            "--filter", "status=running",
            "--format", "{{.Names}}",
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line]


def _stop_containers(names: list[str]) -> int:
    if not names:
        return 0
    proc = subprocess.run(
        ["docker", "stop", "--time", "5", *names],
        capture_output=True, text=True,
    )
    return len(names) if proc.returncode == 0 else 0


# ── locust subprocess ────────────────────────────────────────────────────────

def _build_locust_cmd(run_dir: Path, *, phases: dict) -> list[str]:
    """Assemble the locust headless command for this run."""
    return [
        sys.executable, "-m", "locust",
        "-f", str(Path(__file__).parent / "locust" / "locustfile.py"),
        "--host", LB_HOST_URL,
        "--headless",
        "-u", str(phases["RAMP_USERS"]),
        "--spawn-rate", str(phases["SPAWN_RATE"]),
        "--run-time", f"{phases['PHASE_E_END_SECS']}s",
        "--csv", str(run_dir / "locust"),
        "--csv-full-history",
        "--logfile", str(run_dir / "locust.log"),
        "--only-summary",
    ]


def _locust_env(phases: dict) -> dict:
    """Environment for the locust subprocess — phase knobs + target host."""
    env = os.environ.copy()
    env["TARGET_HOST"] = LB_HOST_URL
    for k, v in phases.items():
        env[k] = str(v)
    return env


# ── orchestrator main ────────────────────────────────────────────────────────

async def _run_collectors_and_locust(
    run_dir: Path,
    phases: dict,
    injection_log: list[dict],
) -> int:
    """Concurrent block. Returns the locust subprocess exit code."""
    stop_event = asyncio.Event()
    bench_start = time.monotonic()

    locust_proc = await asyncio.create_subprocess_exec(
        *_build_locust_cmd(run_dir, phases=phases),
        env=_locust_env(phases),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(REPO_ROOT),
    )

    # Collectors run as background tasks. We don't gather them with the
    # locust subprocess — they need to keep streaming after locust exits
    # in case the BFF emits a trailing envelope.
    collector_tasks = [
        asyncio.create_task(prom_collector.run(
            stop_event=stop_event,
            output_path=run_dir / "prom_timeseries.parquet",
        )),
        asyncio.create_task(sse_collector.run(
            stop_event=stop_event,
            output_path=run_dir / "decision_envelopes.jsonl",
        )),
        asyncio.create_task(upstream_watcher.run(
            stop_event=stop_event,
            output_path=run_dir / "upstream_changes.jsonl",
        )),
        asyncio.create_task(anomaly_injector.inject_at(
            fire_at_secs=phases["PHASE_C_END_SECS"],     # phase-D start
            recover_at_secs=phases["PHASE_D_END_SECS"],  # phase-D end
            bench_start_monotonic=bench_start,
            stop_event=stop_event,
            output_log=injection_log,
        )),
    ]

    async def _drain_pipe(stream, prefix: str):
        if stream is None:
            return
        async for raw in stream:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                print(f"[{prefix}] {line}", flush=True)

    drain_tasks = [
        asyncio.create_task(_drain_pipe(locust_proc.stdout, "locust:out")),
        asyncio.create_task(_drain_pipe(locust_proc.stderr, "locust:err")),
    ]

    await locust_proc.wait()
    # Give collectors a 2 s grace window to catch any trailing envelopes
    await asyncio.sleep(2.0)
    stop_event.set()

    # Wait for collectors to flush. Cap at 10 s — a stuck collector
    # shouldn't block teardown forever.
    try:
        await asyncio.wait_for(asyncio.gather(*collector_tasks, return_exceptions=True), timeout=10.0)
    except asyncio.TimeoutError:
        for t in collector_tasks:
            if not t.done():
                t.cancel()
    for t in drain_tasks:
        if not t.done():
            t.cancel()

    return locust_proc.returncode if locust_proc.returncode is not None else -1


def _make_run_dir(output_root: Path) -> Path:
    timestamp = _utc_ts_tag()
    run_dir = output_root / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _write_manifest(run_dir: Path, *, phases: dict, short: bool, injection_log: list[dict]) -> None:
    sha, dirty = _git_sha()
    manifest = {
        "timestamp_utc":  _utc_ts_tag(),
        "bench_version":  BENCH_VERSION,
        "git_sha":        sha,
        "git_state":      dirty,
        "short":          short,
        "phases":         phases,
        "service_urls": {
            "status":         STATUS_URL,
            "scaling_audit":  SCALING_AUDIT_URL,
            "policy":         POLICY_GET_URL,
            "lb_host":        LB_HOST_URL,
        },
        "injections":     injection_log,
    }
    (run_dir / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="adaptive-bench Round 2 orchestrator (#156)")
    parser.add_argument(
        "--output-root",
        required=True,
        help="Parent directory under which to create the timestamped run folder.",
    )
    parser.add_argument(
        "--short",
        action="store_true",
        help="Compress the 5-phase shape to 60 s for harness validation / CI e2e test.",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help=argparse.SUPPRESS,   # used by the e2e test
    )
    args = parser.parse_args()

    output_root = Path(args.output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    phases = SHORT_PHASES if args.short else FULL_PHASES
    run_dir = _make_run_dir(output_root)
    print(f"[run] adaptive-bench R2 — run_dir={run_dir} short={args.short}", flush=True)

    # ── pre-flight ─────────────────────────────────────────────────────────
    print("[pre-flight] waiting for /api/v1/status overall != down...", flush=True)
    overall = _wait_for_status(60.0)
    print(f"[pre-flight] overall={overall}", flush=True)

    saved_policy = _snapshot_policy()
    if saved_policy is None:
        print("[pre-flight] WARN — could not snapshot policy; will not be restored on teardown", flush=True)
    else:
        push_ok = _push_policy_overrides({"autoscaler_cooldown_seconds": 10})
        if not push_ok:
            print("[pre-flight] WARN — temporary cooldown override failed; continuing", flush=True)

    _write_env_file(provisioning_enabled=True)
    if not args.skip_preflight:
        rc = _force_recreate_autoscaler()
        if rc != 0:
            print(f"[pre-flight] WARN — autoscaler recreate exit={rc}; continuing", flush=True)
        # Re-wait for status; the autoscaler restart blips the operator-UI
        # status panel briefly.
        _wait_for_status(30.0)

    # Pre-flight snapshot
    pre_status = _http_get_json(STATUS_URL, timeout=5.0)
    if pre_status is not None:
        (run_dir / "pre_status.json").write_text(json.dumps(pre_status, indent=2))

    injection_log: list[dict] = []
    locust_rc = -1

    try:
        # ── concurrent run ──────────────────────────────────────────────────
        print(f"[run] launching locust ({phases['PHASE_E_END_SECS']}s) + 3 collectors + anomaly injector...", flush=True)
        locust_rc = asyncio.run(_run_collectors_and_locust(run_dir, phases, injection_log))
        print(f"[run] locust exit={locust_rc}", flush=True)
    finally:
        # ── post-flight (runs even on failure) ──────────────────────────────
        print("[post-flight] snapshot status + scaling audit...", flush=True)
        post_status = _http_get_json(STATUS_URL, timeout=5.0)
        if post_status is not None:
            (run_dir / "post_status.json").write_text(json.dumps(post_status, indent=2))

        scaling_audit = _http_get_json(SCALING_AUDIT_URL, timeout=5.0)
        if scaling_audit is not None:
            (run_dir / "scaling_audit.json").write_text(json.dumps(scaling_audit, indent=2))

        print("[post-flight] flip env-file back + recreate autoscaler...", flush=True)
        _write_env_file(provisioning_enabled=False)
        if not args.skip_preflight:
            _force_recreate_autoscaler()

        if saved_policy is not None:
            # Only restore the field we touched, not the whole snapshot — the
            # policy-manager's POST is partial-update friendly, and posting the
            # whole snapshot would log every field as a "change" in the audit.
            restore_value = saved_policy.get("autoscaler_cooldown_seconds")
            if restore_value is not None:
                print(f"[post-flight] restoring autoscaler_cooldown_seconds={restore_value}", flush=True)
                _push_policy_overrides({"autoscaler_cooldown_seconds": int(restore_value)})

        leftovers = _list_dynamic_backends()
        if leftovers:
            stopped = _stop_containers(leftovers)
            print(f"[post-flight] stopped {stopped}/{len(leftovers)} leftover dynamic backends", flush=True)

        _write_manifest(run_dir, phases=phases, short=args.short, injection_log=injection_log)
        print(f"[post-flight] manifest written → {run_dir / 'MANIFEST.json'}", flush=True)

    return 0 if locust_rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
