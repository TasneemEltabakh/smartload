"""
tools/anomaly-training/collect_production_data.py
────────────────────────────────────────────────────
Stage-B data collection: drives the live docker-compose stack's own
load-balancer + test-backends through alternating normal/anomalous
(latency-injected) phases, queries the exact same ANOMALY_QUERY +
build_features_from_rows pivot the live anomaly-detector run loop uses, and
writes a labeled CSV + manifest -- input to train_stage_b.py.

Requires:
    docker compose up -d   (load-balancer on :8080, test-backend replicas,
                             timescaledb on :5432)
    pip install -r tests/integration/requirements.txt   (docker SDK)

Usage:
    python tools/anomaly-training/collect_production_data.py
    python tools/anomaly-training/collect_production_data.py --quick   # ~3 min smoke

Output:
    datasets/smartload-live/<UTC-timestamp>/production_features.csv
    datasets/smartload-live/<UTC-timestamp>/manifest.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import docker as docker_sdk
import pandas as pd
import psycopg2

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from live_collection import ANOMALOUS_DELAY_MS, N_BACKENDS, run_collection  # noqa: E402
from preprocess_smd import FEATURE_COLUMNS  # noqa: E402

DEFAULT_BASE_URL = "http://localhost:8080/"
DEFAULT_TIMESCALEDB_URL = "postgresql://postgres:changeme@localhost:5432/smartloaddb"

OUT_DIR = _HERE.parent.parent / "datasets" / "smartload-live"


def main(*, quick: bool, base_url: str, db_url: str) -> None:
    phase_seconds = 15 if quick else 60
    n_phases = 12 if quick else 30  # quick: 3 min; full: 30 min

    docker_client = docker_sdk.from_env()
    db_conn = psycopg2.connect(db_url)
    db_conn.autocommit = True

    run_ts = datetime.now(timezone.utc)
    print(
        f"[collect_production_data] starting: {n_phases} phases x {phase_seconds}s "
        f"= {n_phases * phase_seconds}s total, anomalous_delay_ms={ANOMALOUS_DELAY_MS}"
    )

    def _on_phase(phase_index, rows):
        n_anom = sum(r["anomaly_truth"] for r in rows)
        print(
            f"[collect_production_data] phase {phase_index + 1}/{n_phases}: "
            f"{len(rows)} backends, {n_anom} anomalous"
        )

    rows = run_collection(
        db_conn=db_conn,
        docker_client=docker_client,
        base_url=base_url,
        n_phases=n_phases,
        phase_seconds=phase_seconds,
        n_backends=N_BACKENDS,
        on_phase=_on_phase,
    )

    db_conn.close()
    docker_client.close()

    if not rows:
        print(
            "[collect_production_data] no rows collected -- is the stack up "
            "and receiving traffic?",
            file=sys.stderr,
        )
        sys.exit(1)

    df = pd.DataFrame(rows)
    ts_dir = OUT_DIR / run_ts.strftime("%Y%m%dT%H%M%SZ")
    ts_dir.mkdir(parents=True, exist_ok=True)

    csv_path = ts_dir / "production_features.csv"
    df.to_csv(csv_path, index=False)

    manifest = {
        "collected_at": run_ts.isoformat(),
        "quick": quick,
        "phase_seconds": phase_seconds,
        "n_phases": n_phases,
        "n_backends": N_BACKENDS,
        "anomalous_delay_ms": ANOMALOUS_DELAY_MS,
        "feature_columns": FEATURE_COLUMNS,
        "n_rows": len(df),
        "anomaly_rate": float(df["anomaly_truth"].mean()),
        "base_url": base_url,
    }
    manifest_path = ts_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    print(f"\n[collect_production_data] {len(df)} rows -> {csv_path}")
    print(f"[collect_production_data] manifest -> {manifest_path}")
    print(f"[collect_production_data] anomaly_truth rate: {manifest['anomaly_rate']:.4f}")
    print(df.groupby("window")["anomaly_truth"].mean().describe().to_string())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collect labeled production features from a live SmartLoad stack")
    parser.add_argument("--quick", action="store_true", help="~3 min smoke collection instead of ~30 min")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--db-url", default=DEFAULT_TIMESCALEDB_URL)
    args = parser.parse_args()

    main(quick=args.quick, base_url=args.base_url, db_url=args.db_url)
