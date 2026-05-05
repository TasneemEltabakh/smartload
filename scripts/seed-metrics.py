#!/usr/bin/env python3
"""
scripts/seed-metrics.py
───────────────────────
Seed the canonical `metrics` hypertable with synthetic per-backend telemetry.

Purpose
    Mitigates the SOT risk-register entry "T1.1 delayed (no live metrics for
    N1.1 testing)". Lets Nada develop the threshold anomaly detector (N1.1),
    moving-average forecaster (N1.2), and shadow-mode RL scaffold (N1.3)
    end-to-end without waiting for Tasneem's OTLP receiver to land.

What it writes
    For each backend in --backends (default 3), one row per metric per
    --interval-seconds (default 5) over the last --window-minutes (default 60):

        request_latency_ms — log-normal around 80 ms, with one backend
                             intentionally degraded (3× latency) for the last
                             quarter of the window.
        request_count      — Poisson around the per-backend RPS budget.
        error_rate         — beta-distributed around 0.5%, spiking on the
                             degraded backend.

    Rows match the canonical schema and the metric names hardcoded in
    services/shared/queries.py (request_latency_ms / request_count /
    error_rate, service = "load-balancer").

Usage
    python scripts/seed-metrics.py                            # default 1h, 3 backends
    python scripts/seed-metrics.py --backends 5 --window-minutes 30
    python scripts/seed-metrics.py --dsn postgresql://postgres:changeme@localhost:5432/smartloaddb

Idempotency
    Not idempotent — re-running appends another window of rows. Truncate
    `metrics` first (`TRUNCATE metrics`) if you need a clean slate.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
from datetime import datetime, timedelta, timezone


DEFAULT_DSN = os.environ.get(
    "TIMESCALEDB_URL",
    "postgresql://postgres:changeme@localhost:5432/smartloaddb",
)

INSERT_SQL = """
INSERT INTO metrics (time, service, instance, metric_name, value)
VALUES (%s, %s, %s, %s, %s);
"""

SERVICE = "load-balancer"


def _latency_ms(degraded: bool) -> float:
    base = random.lognormvariate(mu=math.log(80), sigma=0.35)
    if degraded:
        base *= 3.0
    return round(min(base, 5000.0), 2)


def _request_count(rps_budget: int) -> int:
    # Poisson approx via gauss for stdlib only; clamp >= 0.
    return max(0, int(round(random.gauss(rps_budget, math.sqrt(rps_budget)))))


def _error_rate(degraded: bool) -> float:
    if degraded:
        return round(min(0.01 + random.expovariate(50.0), 0.5), 4)
    return round(min(random.expovariate(200.0), 0.05), 4)


def generate_rows(
    backends: int,
    window_minutes: int,
    interval_seconds: int,
    rps_per_backend: int,
):
    """
    Yield (time, service, instance, metric_name, value) tuples for the seed.

    The last quarter of the window marks backend `backend_{backends-1}` as
    degraded so the anomaly detector has a real signal to fire on.
    """
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(minutes=window_minutes)
    degrade_after = now - timedelta(minutes=window_minutes // 4)

    backend_ids = [f"backend_{i + 1}" for i in range(backends)]
    degraded_id = backend_ids[-1]

    t = start
    while t <= now:
        for instance in backend_ids:
            is_degraded = (instance == degraded_id) and (t >= degrade_after)
            yield (t, SERVICE, instance, "request_latency_ms", _latency_ms(is_degraded))
            yield (t, SERVICE, instance, "request_count",      _request_count(rps_per_backend))
            yield (t, SERVICE, instance, "error_rate",         _error_rate(is_degraded))
        t += timedelta(seconds=interval_seconds)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dsn", default=DEFAULT_DSN, help="TimescaleDB DSN (default: $TIMESCALEDB_URL or local stack)")
    p.add_argument("--backends", type=int, default=3, help="number of backends (default: 3)")
    p.add_argument("--window-minutes", type=int, default=60, help="historical window in minutes (default: 60)")
    p.add_argument("--interval-seconds", type=int, default=5, help="seconds between samples (default: 5)")
    p.add_argument("--rps-per-backend", type=int, default=20, help="target RPS per backend (default: 20)")
    p.add_argument("--seed", type=int, default=42, help="random seed for reproducibility (default: 42)")
    p.add_argument("--dry-run", action="store_true", help="print row count and the first 5 rows; do not insert")
    args = p.parse_args()

    random.seed(args.seed)
    rows = list(generate_rows(
        backends=args.backends,
        window_minutes=args.window_minutes,
        interval_seconds=args.interval_seconds,
        rps_per_backend=args.rps_per_backend,
    ))

    print(f"[seed-metrics] generated {len(rows):,} rows "
          f"({args.backends} backends × {args.window_minutes} min × "
          f"{args.interval_seconds}s × 3 metrics)")

    if args.dry_run:
        for row in rows[:5]:
            print(" ", row)
        return 0

    try:
        import psycopg2
        from psycopg2.extras import execute_batch
    except ImportError:
        sys.stderr.write("psycopg2 is required: pip install psycopg2-binary\n")
        return 1

    conn = psycopg2.connect(args.dsn, connect_timeout=10)
    try:
        with conn, conn.cursor() as cur:
            execute_batch(cur, INSERT_SQL, rows, page_size=500)
        print(f"[seed-metrics] inserted into {args.dsn.rsplit('@', 1)[-1]}")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
