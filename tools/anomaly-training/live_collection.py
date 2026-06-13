"""
tools/anomaly-training/live_collection.py
──────────────────────────────────────────
Shared "drive the live stack + collect labeled production features" logic.

Used by:
  - collect_production_data.py (Stage-B training data collection)
  - evaluate_live.py (drift-check against the shipped bundle, Phase 5)

Both reuse:
  - services.shared.queries.ANOMALY_QUERY -- the same query the live
    anomaly-detector run loop executes every cycle.
  - services.anomaly-detector.runloop.build_features_from_rows -- the same
    pivot from raw rows -> BackendFeatures, guaranteeing train/inference
    feature parity by construction.

Chaos injection reuses test-backends' existing
`POST /_admin/delay {"ms": N}` endpoint (test-backends/app.js, shipped, no
test-backend changes needed) via `docker exec node -e ...` -- the node:20
base image has no curl, but node itself can issue the HTTP request.

Design ("contiguous per backend", not interleaved like train_smd.py's
[::2]/[1::2] split):
  - The collection run is divided into PHASE_SECONDS-long phases.
  - Backend i is "anomalous" during phase p iff (p + i) % 2 == 1 -- every
    backend alternates normal/anomalous every phase, staggered so each
    phase has a mix of both classes, and (because the alternation repeats
    for the whole run) both the early and late portions of the resulting
    time series contain both classes.
  - At the end of each phase, ANOMALY_QUERY is run with
    window=PHASE_SECONDS (so the window's underlying raw rows fall entirely
    within one phase) and each backend's row is labeled
    anomaly_truth = is_anomalous_phase(i, p) -- unambiguous by
    construction, no fractional-overlap heuristic needed.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import requests

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_REPO_ROOT = _HERE.parent.parent
_SERVICES_DIR = _REPO_ROOT / "services"
if (_SERVICES_DIR / "shared").is_dir() and str(_SERVICES_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICES_DIR))
_ANOMALY_DETECTOR_DIR = _SERVICES_DIR / "anomaly-detector"
if str(_ANOMALY_DETECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(_ANOMALY_DETECTOR_DIR))

from shared.queries import ANOMALY_DEFAULT_SERVICE, ANOMALY_METRIC_NAMES, ANOMALY_QUERY  # noqa: E402
from runloop import build_features_from_rows  # noqa: E402
from preprocess_smd import FEATURE_COLUMNS  # noqa: E402


# ── chaos injection (docker exec -> POST /_admin/delay) ──────────────────────

BACKEND_CONTAINER_PREFIX = "smartload-test-backend-"
N_BACKENDS = 5
BACKEND_PORT = 8080
NETWORK_NAME = "smartload_smartload-net"
ANOMALOUS_DELAY_MS = 800  # well above latency_multiplier=3.0's default trigger


def backend_container_names(n_backends: int = N_BACKENDS) -> list[str]:
    return [f"{BACKEND_CONTAINER_PREFIX}{i}" for i in range(1, n_backends + 1)]


def backend_instance_id(docker_client, container_name: str, network: str = NETWORK_NAME) -> str:
    """ANOMALY_QUERY's `instance` label for a test-backend container:
    NGINX $upstream_addr, i.e. "<container-ip>:8080"."""
    container = docker_client.containers.get(container_name)
    networks = container.attrs["NetworkSettings"]["Networks"]
    net = networks.get(network) or next(iter(networks.values()))
    return f"{net['IPAddress']}:{BACKEND_PORT}"


def set_backend_delay(docker_client, container_name: str, ms: int) -> None:
    """POST {"ms": ms} to the container's own /_admin/delay via docker exec
    (no curl in node:20 -- node itself issues the request)."""
    container = docker_client.containers.get(container_name)
    script = (
        "const http=require('http');"
        f"const data=JSON.stringify({{ms:{int(ms)}}});"
        "const req=http.request({hostname:'localhost',port:" + str(BACKEND_PORT) + ","
        "path:'/_admin/delay',method:'POST',"
        "headers:{'Content-Type':'application/json','Content-Length':data.length}},"
        "res=>{res.resume();});"
        "req.write(data);req.end();"
    )
    exit_code, output = container.exec_run(["node", "-e", script])
    if exit_code != 0:
        raise RuntimeError(
            f"set_backend_delay({container_name}, {ms}) failed: {output.decode(errors='replace')}"
        )


# ── load generator ─────────────────────────────────────────────────────────

def generate_load(base_url: str, stop_event: threading.Event, interval_seconds: float = 0.2) -> None:
    """Background thread: GET base_url in a loop until stop_event is set.

    Best-effort -- request failures (including the deliberately slow
    responses from anomalous backends) are swallowed; we only care about
    producing traffic for the otel shipper to record.
    """
    while not stop_event.is_set():
        try:
            requests.get(base_url, timeout=5)
        except requests.RequestException:
            pass
        stop_event.wait(interval_seconds)


# ── schedule ───────────────────────────────────────────────────────────────

def is_anomalous_phase(backend_index: int, phase_index: int) -> bool:
    """Stagger backends so every phase has a mix of normal/anomalous
    backends, and every backend alternates normal/anomalous every phase
    (contiguous per backend over time)."""
    return (phase_index + backend_index) % 2 == 1


# ── feature collection ────────────────────────────────────────────────────

def query_features(db_conn, window_seconds: int) -> list:
    """Run ANOMALY_QUERY with the given window and pivot to BackendFeatures
    -- identical to anomaly-detector app.py's _query_features (feature
    parity by construction)."""
    with db_conn.cursor() as cur:
        cur.execute(
            ANOMALY_QUERY,
            (f"{window_seconds} seconds", ANOMALY_DEFAULT_SERVICE, ANOMALY_METRIC_NAMES),
        )
        rows = cur.fetchall()
    return build_features_from_rows(rows)


def label_features(features_list, instance_to_backend_index: dict[str, int], phase_index: int) -> list[dict]:
    """Turn a list of BackendFeatures into labeled rows.

    instance_to_backend_index maps ANOMALY_QUERY `instance` (IP:port) ->
    0-based backend index (for is_anomalous_phase). Instances not in the
    map (e.g. transient containers) are skipped.
    """
    rows = []
    for f in features_list:
        idx = instance_to_backend_index.get(f.backend_id)
        if idx is None:
            continue
        row = {col: getattr(f, col) for col in FEATURE_COLUMNS}
        row["backend_id"] = f.backend_id
        row["window"] = phase_index
        row["anomaly_truth"] = int(is_anomalous_phase(idx, phase_index))
        rows.append(row)
    return rows


def run_collection(
    *,
    db_conn,
    docker_client,
    base_url: str,
    n_phases: int,
    phase_seconds: int,
    n_backends: int = N_BACKENDS,
    on_phase=None,
) -> list[dict]:
    """Drive load + chaos injection for n_phases * phase_seconds and return
    labeled feature rows (one per backend per phase, where ANOMALY_QUERY
    returned data).

    on_phase(phase_index, rows) is called after each phase completes, for
    progress reporting -- optional.
    """
    containers = backend_container_names(n_backends)
    instance_to_backend_index = {
        backend_instance_id(docker_client, c): i for i, c in enumerate(containers)
    }

    stop_event = threading.Event()
    load_thread = threading.Thread(target=generate_load, args=(base_url, stop_event), daemon=True)
    load_thread.start()

    all_rows: list[dict] = []
    try:
        for phase_index in range(n_phases):
            for i, container in enumerate(containers):
                ms = ANOMALOUS_DELAY_MS if is_anomalous_phase(i, phase_index) else 0
                set_backend_delay(docker_client, container, ms)

            time.sleep(phase_seconds)

            features_list = query_features(db_conn, phase_seconds)
            rows = label_features(features_list, instance_to_backend_index, phase_index)
            all_rows.extend(rows)
            if on_phase is not None:
                on_phase(phase_index, rows)
    finally:
        stop_event.set()
        load_thread.join(timeout=5)
        # Always leave backends in a clean state.
        for container in containers:
            try:
                set_backend_delay(docker_client, container, 0)
            except Exception:  # noqa: BLE001
                pass

    return all_rows
