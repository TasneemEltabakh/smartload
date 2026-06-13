"""
tools/anomaly-training/evaluate_live.py
──────────────────────────────────────────
Phase 5 drift check: runs a fresh injection-labeled collection pass against
the live stack (same schedule as collect_production_data.py, via
live_collection.py) and scores it with the CURRENTLY SHIPPED
services/anomaly-detector/models/isolation_forest.pkl -- no training.

Compares the resulting F1 against the shipped bundle's recorded
metadata.test_f1 and appends a training_log.json entry with
pipeline="drift-check". A material drop signals that live traffic has
drifted from the data the shipped bundle was trained on, and that
collect_production_data.py + train_stage_b.py should be re-run.

This is an on-demand runbook step (see engines/isolation_forest/README.md),
not a CI gate.

Requires:
    docker compose up -d   (load-balancer on :8080, test-backend replicas,
                             timescaledb on :5432)
    pip install -r tests/integration/requirements.txt   (docker SDK)

Usage:
    python tools/anomaly-training/evaluate_live.py
    python tools/anomaly-training/evaluate_live.py --quick   # ~3 min smoke
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import docker as docker_sdk
import joblib
import psycopg2
from sklearn.metrics import f1_score, precision_score, recall_score

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_ANOMALY_DETECTOR_DIR = _HERE.parent.parent / "services" / "anomaly-detector"
if str(_ANOMALY_DETECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(_ANOMALY_DETECTOR_DIR))

from live_collection import (  # noqa: E402
    ANOMALOUS_DELAY_MS,
    N_BACKENDS,
    backend_container_names,
    backend_instance_id,
    generate_load,
    is_anomalous_phase,
    query_features,
    set_backend_delay,
)
from engines.isolation_forest.engine import DEFAULT_MODEL_PATH, IsolationForestEngine  # noqa: E402

TRAIN_LOG = _HERE / "training_log.json"
DEFAULT_BASE_URL = "http://localhost:8080/"
DEFAULT_TIMESCALEDB_URL = "postgresql://postgres:changeme@localhost:5432/smartloaddb"

# How much F1 may drop vs the shipped bundle's recorded test_f1 before this
# is flagged as drift. Generous on purpose -- live injected anomalies are a
# coarser signal than the held-out split the bundle was scored against.
DRIFT_F1_TOLERANCE = 0.15


def _append_run_log(entry: dict) -> None:
    history: list = []
    if TRAIN_LOG.exists():
        try:
            history = json.loads(TRAIN_LOG.read_text())
        except (json.JSONDecodeError, OSError):
            history = []
    history.append(entry)
    TRAIN_LOG.write_text(json.dumps(history, indent=2))
    print(f"[evaluate_live] run appended -> {TRAIN_LOG}")


def main(*, quick: bool, base_url: str, db_url: str, model_path: str | None) -> None:
    phase_seconds = 15 if quick else 60
    n_phases = 12 if quick else 30

    path = Path(model_path).resolve() if model_path else DEFAULT_MODEL_PATH
    bundle = joblib.load(path)
    shipped_metadata = bundle.get("metadata", {})
    engine = IsolationForestEngine(model_path=path)

    docker_client = docker_sdk.from_env()
    db_conn = psycopg2.connect(db_url)
    db_conn.autocommit = True

    run_ts = datetime.now(timezone.utc)
    print(
        f"[evaluate_live] starting: {n_phases} phases x {phase_seconds}s "
        f"= {n_phases * phase_seconds}s total, anomalous_delay_ms={ANOMALOUS_DELAY_MS}, "
        f"model={path}"
    )

    containers = backend_container_names(N_BACKENDS)
    instance_to_backend_index = {
        backend_instance_id(docker_client, c): i for i, c in enumerate(containers)
    }

    stop_event = threading.Event()
    load_thread = threading.Thread(target=generate_load, args=(base_url, stop_event), daemon=True)
    load_thread.start()

    truth: list[int] = []
    pred: list[int] = []

    try:
        for phase_index in range(n_phases):
            for i, container in enumerate(containers):
                ms = ANOMALOUS_DELAY_MS if is_anomalous_phase(i, phase_index) else 0
                set_backend_delay(docker_client, container, ms)

            time.sleep(phase_seconds)

            features_list = query_features(db_conn, phase_seconds)
            n_truth = n_pred = n_seen = 0
            for f in features_list:
                idx = instance_to_backend_index.get(f.backend_id)
                if idx is None:
                    continue
                truth_label = int(is_anomalous_phase(idx, phase_index))
                pred_label = int(engine.score(f).status != "healthy")
                truth.append(truth_label)
                pred.append(pred_label)
                n_truth += truth_label
                n_pred += pred_label
                n_seen += 1
            print(
                f"[evaluate_live] phase {phase_index + 1}/{n_phases}: "
                f"{n_seen} backends, truth_anomalous={n_truth} pred_anomalous={n_pred}"
            )
    finally:
        stop_event.set()
        load_thread.join(timeout=5)
        for container in containers:
            try:
                set_backend_delay(docker_client, container, 0)
            except Exception:  # noqa: BLE001
                pass

    db_conn.close()
    docker_client.close()

    if not truth:
        print(
            "[evaluate_live] no rows collected -- is the stack up and receiving traffic?",
            file=sys.stderr,
        )
        sys.exit(1)

    f1 = f1_score(truth, pred, zero_division=0)
    precision = precision_score(truth, pred, zero_division=0)
    recall = recall_score(truth, pred, zero_division=0)

    print("\n" + "-" * 50)
    print("DRIFT CHECK (shipped isolation_forest.pkl vs fresh live injection)")
    print("-" * 50)
    print(f"  shipped pipeline : {shipped_metadata.get('pipeline')!r} "
          f"(trained_at={shipped_metadata.get('trained_at')!r})")
    print(f"  shipped test_f1  : {shipped_metadata.get('test_f1')}")
    print(f"  live F1          : {f1:.4f}")
    print(f"  live precision   : {precision:.4f}")
    print(f"  live recall      : {recall:.4f}")
    print("-" * 50)

    shipped_f1 = shipped_metadata.get("test_f1")
    drift_detected = False
    if isinstance(shipped_f1, (int, float)):
        drift_detected = (shipped_f1 - f1) > DRIFT_F1_TOLERANCE
        if drift_detected:
            print(
                f"[evaluate_live] DRIFT WARNING: live F1 ({f1:.4f}) is more than "
                f"{DRIFT_F1_TOLERANCE} below the shipped test_f1 ({shipped_f1}). "
                f"Consider re-running collect_production_data.py + train_stage_b.py."
            )
        else:
            print(f"[evaluate_live] OK: live F1 within {DRIFT_F1_TOLERANCE} of shipped test_f1.")
    else:
        print("[evaluate_live] shipped bundle has no test_f1 in metadata -- skipping drift comparison.")

    _append_run_log({
        "run_at": run_ts.isoformat(),
        "pipeline": "drift-check",
        "model_path": str(path),
        "shipped_pipeline": shipped_metadata.get("pipeline"),
        "shipped_trained_at": shipped_metadata.get("trained_at"),
        "shipped_test_f1": shipped_f1,
        "live_f1": round(float(f1), 4),
        "live_precision": round(float(precision), 4),
        "live_recall": round(float(recall), 4),
        "live_anomaly_rate_truth": round(sum(truth) / len(truth), 4),
        "live_anomaly_rate_pred": round(sum(pred) / len(pred), 4),
        "n_phases": n_phases,
        "phase_seconds": phase_seconds,
        "anomalous_delay_ms": ANOMALOUS_DELAY_MS,
        "drift_detected": drift_detected,
        "passed": not drift_detected,
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Drift-check the shipped isolation_forest.pkl against live traffic")
    parser.add_argument("--quick", action="store_true", help="~3 min smoke collection instead of ~30 min")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--db-url", default=DEFAULT_TIMESCALEDB_URL)
    parser.add_argument("--model-path", default=None, help="override path to isolation_forest.pkl (default: shipped)")
    args = parser.parse_args()

    main(quick=args.quick, base_url=args.base_url, db_url=args.db_url, model_path=args.model_path)
