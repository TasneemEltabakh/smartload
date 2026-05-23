"""
services/rl-engine/training/dataset.py
───────────────────────────────────────
TraceReplayDataset — loads Alibaba trace CSVs and produces windowed
BackendState snapshots equivalent to RL_STATE_QUERY output.

Schema (8 columns):
  traceid   — trace identifier
  timestamp — ms since trace epoch (integer string)
  rpcid     — call position in the trace tree
  um        — caller service hash
  rpctype   — "http" | "rpc" | "mc"
  dm        — callee service hash (used as backend identifier)
  interface — method hash (may be empty)
  rt        — response time in ms; NEGATIVE values indicate errors

Filtering:
  - rpctype == "http" only
  - dm must be non-empty
  - rt coerced to float; rows with unparseable rt are dropped

Backend mapping:
  dm hashes are sorted lexicographically across the entire loaded dataset and
  mapped to stable names "backend_1", "backend_2", ... so that the same
  backend keeps the same name across partitions and random episode starts.
  Only the first N_BACKENDS backends (sorted) are kept; the rest are dropped.

Window aggregation:
  Given a start timestamp T and window size W (seconds), all rows in
  [T, T + W*1000) ms are aggregated per backend:
    latency_ms  = mean(rt) for rt >= 0 (non-error rows only); 0.0 if none
    queue_depth = count of rows (proxy for request volume per RL_STATE_QUERY)
    error_rate  = fraction of rows with rt < 0
  Then classify_health is applied to produce the health field.

This mirrors what RL_STATE_QUERY + build_state_from_rows() produce at
serving time, so the environment trains on the same state representation.
"""

from __future__ import annotations

import bisect
import csv
import sys
from pathlib import Path
from typing import Iterator

# Allow running from training/ directory during development
_RL_ENGINE = Path(__file__).resolve().parents[1]
if str(_RL_ENGINE) not in sys.path:
    sys.path.insert(0, str(_RL_ENGINE))

from policy_base import BackendState  # noqa: E402
from runloop import classify_health   # noqa: E402

# Maximum number of distinct backends to track.  Matches N_MAX_BACKENDS in
# obs_builder.py; backends sorted beyond this count are ignored.
N_BACKENDS: int = 5
WINDOW_MS: int = 30_000   # default 30-second window in milliseconds


class TraceReplayDataset:
    """Load Alibaba CSVs and iterate windowed BackendState snapshots.

    Parameters
    ----------
    partitions:
        List of CSV file paths to load.  All are loaded into memory at init.
    n_backends:
        How many distinct dm hashes to track (sorted, stable across calls).
    window_ms:
        Aggregation window in milliseconds.
    """

    def __init__(
        self,
        partitions: list[str | Path],
        n_backends: int = N_BACKENDS,
        window_ms: int = WINDOW_MS,
    ) -> None:
        self.n_backends = n_backends
        self.window_ms  = window_ms

        raw = _load_http_rows(partitions)
        if not raw:
            raise ValueError("No valid http rows found in the given partitions.")

        # Build stable backend mapping (sorted dm hash → "backend_N")
        all_dm = sorted({row[0] for row in raw})
        self._backend_map: dict[str, str] = {
            dm: f"backend_{i + 1}"
            for i, dm in enumerate(all_dm[:n_backends])
        }

        # Filter to tracked backends and sort by timestamp
        self._rows: list[tuple[str, int, float]] = sorted(
            (
                (self._backend_map[dm], ts, rt)
                for dm, ts, rt in raw
                if dm in self._backend_map
            ),
            key=lambda r: r[1],
        )

        if not self._rows:
            raise ValueError(
                f"No rows remain after filtering to the top-{n_backends} backends."
            )

        self._min_ts: int = self._rows[0][1]
        self._max_ts: int = self._rows[-1][1]
        # Sorted timestamp list for O(log n) bisect lookups in get_window()
        self._ts_index: list[int] = [r[1] for r in self._rows]

    # ── public API ─────────────────────────────────────────────────────────────

    @property
    def n_windows(self) -> int:
        """Number of non-overlapping windows in the full dataset."""
        span = self._max_ts - self._min_ts
        return max(1, span // self.window_ms)

    @property
    def distinct_backends(self) -> list[str]:
        return sorted(self._backend_map.values())

    def get_window(self, start_ts: int) -> list[BackendState]:
        """Aggregate rows in [start_ts, start_ts + window_ms) into BackendState list.

        Uses bisect for O(log n) boundary lookup instead of linear scan.
        """
        end_ts = start_ts + self.window_ms
        lo = bisect.bisect_left(self._ts_index, start_ts)
        hi = bisect.bisect_left(self._ts_index, end_ts)
        per_backend: dict[str, list[float]] = {b: [] for b in self._backend_map.values()}

        for backend_id, ts, rt in self._rows[lo:hi]:
            per_backend[backend_id].append(rt)

        states: list[BackendState] = []
        for backend_id in sorted(per_backend):
            rts = per_backend[backend_id]
            if not rts:
                continue
            n_total  = len(rts)
            n_errors = sum(1 for r in rts if r < 0)
            valid_rts = [r for r in rts if r >= 0]
            latency_ms  = (sum(valid_rts) / len(valid_rts)) if valid_rts else 0.0
            queue_depth = n_total
            error_rate  = n_errors / n_total
            health = classify_health(latency_ms, error_rate)
            states.append(BackendState(
                backend_id=backend_id,
                latency_ms=latency_ms,
                queue_depth=queue_depth,
                health=health,
            ))
        return states

    def iter_windows(self) -> Iterator[tuple[int, list[BackendState]]]:
        """Yield (start_ts, states) for every non-overlapping window."""
        ts = self._min_ts
        while ts + self.window_ms <= self._max_ts:
            yield ts, self.get_window(ts)
            ts += self.window_ms

    def sample_start_ts(self, rng, reserve_windows: int = 5) -> int:
        """Return a random valid episode start timestamp.

        Leaves the last `reserve_windows` windows as held-out eval territory
        so the training distribution doesn't contaminate the eval episodes.
        """
        latest_start = self._max_ts - self.window_ms * (reserve_windows + 1)
        if latest_start <= self._min_ts:
            return self._min_ts
        return int(rng.integers(self._min_ts, latest_start + 1))


# ── internal helpers ──────────────────────────────────────────────────────────

def _load_http_rows(
    partitions: list[str | Path],
) -> list[tuple[str, int, float]]:
    """Load all CSV partitions and return (dm, timestamp_ms, rt) triples.

    Filters: rpctype == "http", dm non-empty, rt parseable.
    Negative rt is kept — it signals an error (used by error_rate calculation).
    """
    rows: list[tuple[str, int, float]] = []
    for path in partitions:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("rpctype") != "http":
                    continue
                dm = (row.get("dm") or "").strip()
                if not dm:
                    continue
                try:
                    ts = int(row["timestamp"])
                    rt = float(row["rt"])
                except (KeyError, ValueError):
                    continue
                rows.append((dm, ts, rt))
    return rows
