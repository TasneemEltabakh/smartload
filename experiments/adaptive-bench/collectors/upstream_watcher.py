"""
collectors/upstream_watcher.py
───────────────────────────────
NGINX upstream.conf watcher for adaptive-bench Round 2 (#156).

Polls the load-balancer container every 2 s, copying its current
`/etc/nginx/conf.d/upstream.conf` to memory via `docker cp`, and appends
a JSONL row to disk **only when the content changes**. The 2 s cadence
trades latency for noise — the lb-sidecar's reload latency is order ~100
ms, so a 2 s poll is sure to see every rewrite without flooding the
output with no-change snapshots.

JSONL row shape:

  {
    "ts":     "<UTC ISO 8601>",
    "sha256": "<content hash, for cheap dedup downstream>",
    "body":   "<full upstream.conf content as a single string>"
  }

We capture the full body, not a diff. R3's analysis pipeline can compute
diffs between consecutive rows if needed — but storing the body verbatim
means later questions ("what was the weight on backend-3 at t=185s?")
can be answered without reconstruction from a chain of diffs.

The collector uses `docker cp <container>:<path> -` (dash sink → stdout)
so we never write a tempfile. The container name defaults to
`smartload-load-balancer-1` (compose's standard prefix for the
`load-balancer` service); override via `LB_CONTAINER_NAME` env var.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_LB_CONTAINER = "smartload-load-balancer-1"
DEFAULT_UPSTREAM_PATH = "/etc/nginx/conf.d/upstream.conf"
DEFAULT_POLL_INTERVAL_SECS = 2.0


async def _docker_cp_to_stdout(container: str, path: str) -> bytes | None:
    """Run `docker cp container:path -` and return raw stdout.

    docker cp's tar-stream-to-stdout mode wraps the file in a tar archive;
    we'd need to untar it for the actual content. To keep this simple, we
    use `docker exec ... cat <path>` instead which gives the raw content
    on stdout — the lb-sidecar already trusts the container, so exec'ing
    cat against it is no escalation of access.

    Returns None on any failure (container missing, file missing, exec
    error) — the caller swallows that and just doesn't write a row for
    that tick.
    """
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", container, "cat", path,
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
        return None
    if proc.returncode != 0:
        return None
    return stdout


async def run(
    *,
    stop_event: asyncio.Event,
    output_path: Path,
    container: str = DEFAULT_LB_CONTAINER,
    upstream_path: str = DEFAULT_UPSTREAM_PATH,
    poll_interval_secs: float = DEFAULT_POLL_INTERVAL_SECS,
) -> None:
    """Collector coroutine. Returns when stop_event fires.

    First successful poll writes a row (the bench's initial state).
    Subsequent rows only land when the content hash changes."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    last_hash: str | None = None

    with open(output_path, "a", encoding="utf-8", buffering=1) as fh:
        while not stop_event.is_set():
            body = await _docker_cp_to_stdout(container, upstream_path)
            if body is not None:
                content = body.decode("utf-8", errors="replace")
                digest = hashlib.sha256(body).hexdigest()
                if digest != last_hash:
                    fh.write(json.dumps({
                        "ts":     datetime.now(timezone.utc).isoformat(),
                        "sha256": digest,
                        "body":   content,
                    }, separators=(",", ":")) + "\n")
                    last_hash = digest

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_secs)
            except asyncio.TimeoutError:
                pass
