"""
collectors/sse_collector.py
────────────────────────────
BFF SSE collector for adaptive-bench Round 2 (#156).

Streams the operator-UI BFF's merged decision-plane SSE feed at
`/api/ui/engines/stream` (NOT `/api/ui/events` — the issue body of
#155 had this wrong; the actual endpoint name in the codebase is
`/api/ui/engines/stream`).

The BFF merges the four decision-plane Redis channels (`smartload.anomaly`,
`smartload.forecast`, `smartload.routing`, `smartload.scale`, plus
`smartload.policy`) into a single SSE stream so this collector captures
every envelope in publish order without subscribing to Redis directly —
keeping the bench host's Redis attack surface zero.

Each `data: <json>` frame is appended as one JSONL line with a
`captured_at` ISO 8601 wall-clock tagged at the moment the bench host
parsed the frame. R3's analysis pipeline joins `captured_at` against
`prom_timeseries.ts` and Locust's history-CSV timestamps via
`pandas.merge_asof`.

SSE robustness:
- `: heartbeat` lines from the BFF are ignored (per SSE spec).
- A dropped connection is retried after a 1 s back-off so a BFF restart
  mid-bench costs at most one frame.
- The BFF's per-client bounded queue (`Queue(maxsize=256)`) means a slow
  consumer can silently lose envelopes. We minimise that risk by writing
  to disk on every frame — no in-memory batching between recv and write.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import aiohttp


DEFAULT_BFF_URL = "http://localhost:8090"
DEFAULT_STREAM_PATH = "/api/ui/engines/stream"
RETRY_BACKOFF_SECS = 1.0


def _parse_data_line(line: str) -> dict | None:
    """Parse a single `data: <json>` SSE line. Returns the decoded dict, or
    None if the line is a heartbeat / comment / malformed payload."""
    if not line:
        return None
    if line.startswith(":"):
        return None      # SSE comment / heartbeat
    if not line.startswith("data:"):
        return None      # we only consume data lines; ignore event:/id:/retry:
    payload = line[len("data:"):].strip()
    if not payload:
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


async def _stream_once(
    session: aiohttp.ClientSession,
    url: str,
    stop_event: asyncio.Event,
    sink,
) -> None:
    """One stream attempt. Returns when the connection closes or
    stop_event fires; caller wraps in a retry loop."""
    headers = {"Accept": "text/event-stream"}
    timeout = aiohttp.ClientTimeout(sock_read=None, total=None, connect=5.0)
    async with session.get(url, headers=headers, timeout=timeout) as resp:
        if resp.status != 200:
            return
        # aiohttp's iter_any() gives chunked bytes; SSE frames are line-
        # oriented so we iterate over decoded lines instead.
        async for raw in resp.content:
            if stop_event.is_set():
                return
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            envelope = _parse_data_line(line)
            if envelope is None:
                continue
            sink({
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "envelope":    envelope,
            })


async def run(
    *,
    stop_event: asyncio.Event,
    output_path: Path,
    bff_url: str = DEFAULT_BFF_URL,
    stream_path: str = DEFAULT_STREAM_PATH,
) -> None:
    """Collector coroutine. Writes one JSONL line per BFF SSE data frame.

    The output file is opened in append-binary mode with line buffering so
    a kill -9 mid-run leaves a syntactically valid JSONL file readable up
    to the last newline."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    url = bff_url.rstrip("/") + stream_path

    with open(output_path, "a", encoding="utf-8", buffering=1) as fh:
        def _sink(row: dict) -> None:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")

        async with aiohttp.ClientSession() as session:
            while not stop_event.is_set():
                try:
                    await _stream_once(session, url, stop_event, _sink)
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass
                # Either the stream closed cleanly (server-side hang-up
                # mid-bench) or we hit a transport error. In either case,
                # wait a beat and reconnect — unless the bench is over.
                if stop_event.is_set():
                    return
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=RETRY_BACKOFF_SECS)
                except asyncio.TimeoutError:
                    pass
