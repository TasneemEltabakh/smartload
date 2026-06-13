#!/usr/bin/env python3
"""
experiments/adaptive-bench/fortio/fortio_probe.py
─────────────────────────────────────────────────
Minimal constant-QPS (open-loop) probe that sits *alongside* the Locust
harness — it does not replace it and is not wired into run.py.

Why a second load tool at all?
  Locust's user model is closed-loop: each simulated user waits for its
  response before issuing the next request, so when backend latency rises the
  effective request rate falls with it. That is the right model for "how does
  the system behave for N concurrent users", but it can NOT hold a fixed
  arrival rate, so it cannot pin the backend at a chosen offered load and read
  off the resulting tail latency / shedding.

  Fortio is open-loop: it fires at a *requested* QPS regardless of how slow the
  backend gets. Sweeping QPS past the pool's service rate is what makes the
  closed-loop backend model (test-backends/app.js: WORKERS service slots + a
  bounded QUEUE_MAX, 503 on overflow) show its knee — queue-wait inflates the
  tail, then the LB starts returning 503 once QUEUE_MAX is exceeded.

  This probe exists only to validate that saturation curve and the tail
  latency at each offered load. It is a smoke/diagnostic, not a benchmark.

What it does
  For each offered QPS in the sweep it runs `fortio load` for a short window
  against the LB root path ("/", same target Locust uses) and prints one row:
  offered QPS, achieved QPS, p50/p90/p99/p99.9 latency, the 2xx/503 split
  (503% == edge shed rate), and any non-503 errors.

Running fortio
  By default fortio runs in its official Docker image attached to the Compose
  network, so nothing has to be installed and it reaches the LB by service
  name (http://load-balancer). Pass --local to use a `fortio` binary already
  on PATH, in which case it targets the published port (http://localhost:8080).

Examples
  # Saturation curve out of the box (~50 s: five 10 s points), via Docker:
  python fortio_probe.py

  # Single quick smoke at 200 QPS for 15 s:
  python fortio_probe.py --qps 200 --duration 15s

  # Custom sweep, dump fortio's raw JSON per point for later inspection:
  python fortio_probe.py --qps 100,300,600 --out ./results

  # Using a locally-installed fortio binary against the published port:
  python fortio_probe.py --local

Exit status is 0 as long as every point produced a parseable fortio report;
a high 503% is a *finding*, not a failure, so it does not change the exit code.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Mirror the locustfile defaults so both tools hit the same edge by default.
DOCKER_TARGET = "http://load-balancer"        # service name on the Compose net
LOCAL_TARGET = "http://localhost:8080"        # published port for a host binary
DEFAULT_NETWORK = "smartload_smartload-net"   # COMPOSE_PROJECT_NAME=smartload
DEFAULT_IMAGE = "fortio/fortio:latest"
PERCENTILES = "50,90,99,99.9"                 # the tail we care about


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="fortio_probe",
        description="Constant-QPS open-loop probe for the closed-loop test backends.",
    )
    p.add_argument(
        "--qps",
        default="50,100,200,400,800",
        help="Comma-separated offered QPS points (a single value = one smoke). "
        "Default sweeps 50,100,200,400,800.",
    )
    p.add_argument(
        "--duration",
        default="10s",
        help="Per-point duration as a fortio time string (default 10s).",
    )
    p.add_argument(
        "--connections",
        type=int,
        default=64,
        help="Concurrent connections fortio paces the QPS across (default 64). "
        "Must comfortably exceed QPS*latency or fortio cannot sustain the rate.",
    )
    p.add_argument(
        "--target",
        default=None,
        help="Override the LB URL. Defaults to %s (Docker) or %s (--local)."
        % (DOCKER_TARGET, LOCAL_TARGET),
    )
    p.add_argument(
        "--local",
        action="store_true",
        help="Use a `fortio` binary on PATH instead of the Docker image.",
    )
    p.add_argument(
        "--network",
        default=DEFAULT_NETWORK,
        help="Docker network to attach to (default %s). Ignored with --local."
        % DEFAULT_NETWORK,
    )
    p.add_argument(
        "--image",
        default=DEFAULT_IMAGE,
        help="Fortio Docker image (default %s). Ignored with --local." % DEFAULT_IMAGE,
    )
    p.add_argument(
        "--out",
        default=None,
        help="Optional directory; dump fortio's raw JSON per QPS point into it.",
    )
    return p.parse_args(argv)


def build_cmd(qps: int, url: str, args: argparse.Namespace) -> list[str]:
    """Assemble the fortio load command (Docker-wrapped unless --local)."""
    fortio = [
        "load",
        "-quiet",
        "-c",
        str(args.connections),
        "-qps",
        str(qps),
        "-t",
        args.duration,
        "-p",
        PERCENTILES,
        "-json",
        "-",  # write the JSON report to stdout
        url,
    ]
    if args.local:
        return ["fortio", *fortio]
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        args.network,
        args.image,
        *fortio,
    ]


def extract_report(stdout: str) -> dict:
    """Fortio writes its JSON report to stdout under `-json -`. Be tolerant of
    any stray non-JSON line by falling back to the outermost brace span."""
    text = stdout.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def summarise(qps: int, report: dict) -> dict:
    """Pull the few numbers we report out of a fortio result object."""
    hist = report.get("DurationHistogram", {})
    pctile_ms: dict[float, float] = {}
    for entry in hist.get("Percentiles") or []:
        # fortio Values are in seconds; we report milliseconds.
        pctile_ms[round(float(entry["Percentile"]), 1)] = float(entry["Value"]) * 1000.0

    ret = {str(k): int(v) for k, v in (report.get("RetCodes") or {}).items()}
    total = sum(ret.values())
    ok = ret.get("200", 0)
    shed = ret.get("503", 0)
    # `errs` is reserved for failures that are neither a clean 200 nor an
    # intentional 503 shed (connection resets, timeouts) — sheds get their own
    # column, so don't double-count them here.
    errors = total - ok - shed

    return {
        "offered_qps": qps,
        "actual_qps": float(report.get("ActualQPS", 0.0)),
        "count": total,
        "p50_ms": pctile_ms.get(50.0, 0.0),
        "p90_ms": pctile_ms.get(90.0, 0.0),
        "p99_ms": pctile_ms.get(99.0, 0.0),
        "p999_ms": pctile_ms.get(99.9, 0.0),
        "ok_pct": (100.0 * ok / total) if total else 0.0,
        "shed_pct": (100.0 * shed / total) if total else 0.0,
        "errors": errors,
    }


HEADER = (
    f"{'offered':>8} {'actual':>8} {'p50':>8} {'p90':>8} {'p99':>8} "
    f"{'p99.9':>8} {'2xx%':>7} {'503%':>7} {'errs':>6}"
)


def fmt_row(s: dict) -> str:
    return (
        f"{s['offered_qps']:>8} {s['actual_qps']:>8.1f} {s['p50_ms']:>8.1f} "
        f"{s['p90_ms']:>8.1f} {s['p99_ms']:>8.1f} {s['p999_ms']:>8.1f} "
        f"{s['ok_pct']:>7.1f} {s['shed_pct']:>7.1f} {s['errors']:>6}"
    )


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    target = args.target or (LOCAL_TARGET if args.local else DOCKER_TARGET)
    url = target.rstrip("/") + "/"  # hit the LB root, same path Locust uses

    try:
        qps_points = [int(q) for q in args.qps.split(",") if q.strip()]
    except ValueError:
        print(f"error: --qps must be comma-separated integers, got {args.qps!r}", file=sys.stderr)
        return 2
    if not qps_points:
        print("error: --qps produced no points", file=sys.stderr)
        return 2

    out_dir: Path | None = None
    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)

    runner = "fortio (local)" if args.local else f"docker {args.image} on {args.network}"
    print(
        f"# fortio constant-QPS probe -> {url}\n"
        f"# duration={args.duration} connections={args.connections} runner={runner}",
        flush=True,
    )
    print(HEADER, flush=True)

    failures = 0
    for qps in qps_points:
        cmd = build_cmd(qps, url, args)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        try:
            report = extract_report(proc.stdout)
        except json.JSONDecodeError:
            failures += 1
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            print(
                f"{qps:>8}  <no parseable fortio report; exit={proc.returncode}>",
                flush=True,
            )
            for line in tail:
                print(f"         | {line}", file=sys.stderr)
            continue

        if out_dir is not None:
            (out_dir / f"fortio_qps{qps}.json").write_text(
                json.dumps(report, indent=2), encoding="utf-8"
            )

        print(fmt_row(summarise(qps, report)), flush=True)

    if failures:
        print(f"# {failures}/{len(qps_points)} point(s) produced no report", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
