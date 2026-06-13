"""
tests/integration/_chaos.py
─────────────────────────────
Thin re-export of tools/anomaly-training/live_collection.py's chaos-injection
helpers for live-stack integration tests.

live_collection.py is the single source of truth for "how do we make a
test-backend container slow" (used by collect_production_data.py and
evaluate_live.py for Stage-B data collection / drift checks). This module
just makes those same helpers importable from tests/integration/ without
duplicating the docker-exec logic.

Not a test module itself (leading underscore -- not collected by pytest).
"""

from __future__ import annotations

import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools" / "anomaly-training"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from live_collection import (  # noqa: E402,F401
    ANOMALOUS_DELAY_MS,
    BACKEND_PORT,
    N_BACKENDS,
    NETWORK_NAME,
    backend_container_names,
    backend_instance_id,
    set_backend_delay,
)
