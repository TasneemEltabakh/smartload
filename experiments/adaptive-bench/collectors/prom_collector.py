"""
collectors/prom_collector.py
─────────────────────────────
1 Hz Prometheus collector for adaptive-bench Round 2 (#156).

Polls the standing Prometheus server (`http://localhost:9090`) on a fixed
cadence over a fixed metric list, and streams each snapshot to a parquet
file via `pyarrow`'s incremental writer. Row layout:

  ts (timestamptz)  metric (str)  labels_json (str)  value (float)

Storing labels as a JSON string keeps the schema flat — different metrics
emit different label sets, and union-typing structs in parquet is fragile
across pyarrow versions. R3's `join_run.py` parses `labels_json` on read.

Why parquet and not CSV: 1 Hz × ~5 metrics × 360 s = ~1800 rows per metric
per run. CSV is fine at that scale, but R3 needs column-oriented reads
(`pandas.read_parquet(columns=...)`) for `merge_asof` joins against the
SSE timeline. Parquet here saves a re-encode step in R3.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import pyarrow as pa
import pyarrow.parquet as pq


# ── canonical metric surface ──────────────────────────────────────────────────
#
# These are the metrics R3's analysis pipeline joins against the SSE
# timeline + Locust history. The set is intentionally small — every metric
# here costs ~1800 rows per run and any addition is paid forever in storage.
# Add new metrics here only when R3 has a join that needs them.
#
# `up` and `nginx_*` are emitted by the OTel Collector's `:8889` scrape
# endpoint (SOT §6.4 data plane); `otel_*` are the Collector's internal
# pipeline counters.

DEFAULT_METRICS = (
    "up",
    "nginx_http_requests_total",
    "otel_spans_processed_total",
)

DEFAULT_PROM_URL = "http://localhost:9090"
DEFAULT_POLL_HZ = 1.0


PROM_SCHEMA = pa.schema([
    pa.field("ts",          pa.timestamp("us", tz="UTC")),
    pa.field("metric",      pa.string()),
    pa.field("labels_json", pa.string()),
    pa.field("value",       pa.float64()),
])


async def _query_one(session: aiohttp.ClientSession, base_url: str, metric: str) -> list[dict]:
    """Issue a single instant query for `metric` against the Prometheus HTTP API.

    Returns a list of `{metric, labels_json, value}` dicts (timestamp added
    by the caller so all rows in a snapshot share an identical poll wall-clock).
    Errors are swallowed and yield an empty list — a single failed scrape
    must not break the collector loop.
    """
    url = f"{base_url}/api/v1/query"
    try:
        async with session.get(url, params={"query": metric}, timeout=aiohttp.ClientTimeout(total=2.0)) as resp:
            if resp.status != 200:
                return []
            body = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError):
        return []

    if body.get("status") != "success":
        return []

    rows: list[dict] = []
    for item in body.get("data", {}).get("result", []):
        labels = item.get("metric", {})
        # Prometheus puts the metric name into the labels dict too; drop it
        # so labels_json is just the dimensional labels.
        labels.pop("__name__", None)
        value_pair = item.get("value")
        if not value_pair or len(value_pair) < 2:
            continue
        try:
            value = float(value_pair[1])
        except (TypeError, ValueError):
            continue
        rows.append({
            "metric":      metric,
            "labels_json": json.dumps(labels, sort_keys=True, separators=(",", ":")),
            "value":       value,
        })
    return rows


async def run(
    *,
    stop_event: asyncio.Event,
    output_path: Path,
    base_url: str = DEFAULT_PROM_URL,
    metrics: tuple[str, ...] = DEFAULT_METRICS,
    poll_hz: float = DEFAULT_POLL_HZ,
) -> None:
    """Collector coroutine. Returns when `stop_event` is set.

    Writes to `output_path` (parquet) via `pyarrow.parquet.ParquetWriter`.
    The writer flushes a row group on every snapshot, so a kill -9 at any
    point leaves a partial but valid parquet that pandas can read.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    interval = 1.0 / max(0.1, poll_hz)

    async with aiohttp.ClientSession() as session:
        writer = pq.ParquetWriter(str(output_path), PROM_SCHEMA, compression="snappy")
        try:
            next_tick = time.monotonic()
            while not stop_event.is_set():
                ts = datetime.now(timezone.utc)
                # Gather all metrics in parallel — one round trip per metric
                # but they fire concurrently so the snapshot's wall-clock skew
                # stays under the per-call timeout (~2 s) regardless of count.
                results = await asyncio.gather(
                    *(_query_one(session, base_url, m) for m in metrics)
                )
                rows = [
                    {**r, "ts": ts}
                    for sublist in results
                    for r in sublist
                ]
                if rows:
                    table = pa.Table.from_pylist(rows, schema=PROM_SCHEMA)
                    writer.write_table(table)

                next_tick += interval
                # If we drifted (e.g. slow scrape), don't try to "catch up" —
                # just realign to now so the next snapshot is fresh.
                delay = max(0.0, next_tick - time.monotonic())
                if delay == 0.0:
                    next_tick = time.monotonic()
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            writer.close()
