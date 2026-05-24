"""
services/forecasting/runloop.py
────────────────────────────────
Pure-Python pieces of the forecasting run loop, separated from app.py
so they can be unit-tested without Flask, Redis, or a DB connection.

The Flask entry point (app.py) owns:
  - sockets and threads
  - the actual psycopg2 + redis clients
  - request/response handling

This module owns:
  - select-with-fallback engine bootstrap
  - building HistoryWindow from a FORECAST_QUERY result set
  - converting Forecast → ForecastResult envelope payload
  - policy payload → engine kwargs
  - the safe_mode publish gate
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import asdict, dataclass

# Make engine_base + plugin folders importable when this file is loaded from
# /app (container) or from services/forecasting/ (dev).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from engine_base import (  # noqa: E402
    Forecast,
    ForecastEngine,
    HistoryWindow,
    select_engine,
)


# ── policy-derived engine kwargs ──────────────────────────────────────────────

DEFAULT_HORIZON_MINUTES = 5
DEFAULT_WINDOW_SAMPLES = 60


@dataclass
class EnginePolicy:
    """Subset of the operating policy that drives the forecast engine.

    Built from a smartload.policy envelope payload (or from defaults at
    startup). Passed as kwargs to select_engine() so any engine that takes
    matching constructor params picks them up; engines that don't are free
    to ignore them.

    No forecast-specific fields are defined in PolicyUpdate today; this
    dataclass tracks safe_mode (to gate publishing) and policy_version
    (for stale-publish guards). Horizon and window-samples remain engine
    defaults until a future policy field is introduced.
    """
    horizon_minutes: int = DEFAULT_HORIZON_MINUTES
    window_samples: int = DEFAULT_WINDOW_SAMPLES
    safe_mode: bool = False
    policy_version: int = 0

    def engine_kwargs(self) -> dict:
        return {
            "horizon_minutes": self.horizon_minutes,
            "window_samples":  self.window_samples,
        }


def policy_from_payload(payload: dict, fallback: EnginePolicy) -> EnginePolicy:
    """Build an EnginePolicy from a smartload.policy envelope payload.

    Fields missing or with wrong types fall back to the previous values so
    a malformed publish never wipes the live policy. PolicyUpdate has no
    forecast-specific fields yet, so horizon and window stay at fallback
    values regardless of payload content.
    """
    def _int(key: str, default: int) -> int:
        try:
            return int(payload.get(key, default))
        except (TypeError, ValueError):
            return default

    return EnginePolicy(
        horizon_minutes=fallback.horizon_minutes,
        window_samples=fallback.window_samples,
        safe_mode=bool(payload.get("safe_mode", fallback.safe_mode)),
        policy_version=_int("policy_version", fallback.policy_version),
    )


# ── engine bootstrap with safety-net fallback ────────────────────────────────

BASELINE_ENGINE_NAME = "moving_average"


@dataclass
class EngineBootstrap:
    """Outcome of engine startup. ready=False means we fell back to the baseline."""
    engine: ForecastEngine
    name: str             # the engine that's actually loaded (may differ from `requested`)
    requested: str        # the engine the operator asked for via FORECAST_ENGINE
    ready: bool           # True iff the requested engine loaded; False after fallback
    error: str | None     # exception message when ready=False


def bootstrap_engine(requested: str, policy: EnginePolicy) -> EngineBootstrap:
    """Try the requested engine; on any load failure, fall back to the baseline.

    The baseline (moving_average) has no model artifact and never fails, so
    it's the safety net. If the baseline itself fails, the exception
    propagates — that's a deployment bug, not something to swallow.
    """
    try:
        engine = select_engine(requested, **policy.engine_kwargs())
        return EngineBootstrap(engine=engine, name=requested, requested=requested,
                               ready=True, error=None)
    except Exception as exc:                                # noqa: BLE001
        if requested == BASELINE_ENGINE_NAME:
            raise
        baseline = select_engine(BASELINE_ENGINE_NAME, **policy.engine_kwargs())
        return EngineBootstrap(engine=baseline, name=BASELINE_ENGINE_NAME,
                               requested=requested, ready=False, error=str(exc))


# ── DB rows → history window ──────────────────────────────────────────────────

# FORECAST_QUERY shape: one row per minute bucket, columns (bucket, request_rate).
# We turn it into a HistoryWindow of ISO timestamps and floats.

def build_history_from_rows(rows: list[tuple]) -> HistoryWindow:
    """Convert FORECAST_QUERY rows into a HistoryWindow.

    Rows are tuples: (bucket: datetime, request_rate: numeric).
    Returns an empty HistoryWindow on no rows (cold DB, idle stack).
    """
    timestamps: list[str] = []
    rates:      list[float] = []
    for row in rows:
        try:
            bucket, rate = row
        except ValueError:
            continue   # malformed row — skip, don't poison the whole batch
        if bucket is None:
            continue
        # bucket comes back as a psycopg2 datetime; isoformat() handles both
        # tz-aware and naive cases. Engines treat timestamps as opaque labels
        # — only the order matters.
        try:
            ts = bucket.isoformat()
        except AttributeError:
            ts = str(bucket)
        try:
            rates.append(float(rate) if rate is not None else 0.0)
        except (TypeError, ValueError):
            rates.append(0.0)
        timestamps.append(ts)
    return HistoryWindow(timestamps=timestamps, request_rates=rates)


# ── publish gate ──────────────────────────────────────────────────────────────

def should_publish(policy: EnginePolicy) -> bool:
    """Decide whether the cycle's forecast becomes a published envelope.

    Rules:
      - safe_mode=true  → never publish (operators have paused decision flow)
      - default         → publish every cycle (downstream consumers — autoscaler
                          and the operator UI forecast chart — drive their own
                          tick rate, so the publish is non-optional)
    """
    return not policy.safe_mode


def forecast_to_event_payload(forecast: Forecast, model_id: str) -> dict:
    """Serialise a Forecast + model id into the ForecastResult dict shape
    expected by publish_envelope. model_id is the loaded engine name and
    rides on the envelope payload for downstream provenance."""
    return {
        "horizon_minutes":  forecast.horizon_minutes,
        "predicted_rps":    forecast.predicted_rps,
        "confidence_lower": forecast.confidence_lower,
        "confidence_upper": forecast.confidence_upper,
        "model_id":         model_id,
    }


# ── /api/v1/engine/state serialisation ───────────────────────────────────────

def serialize_engine_state(
    *,
    service: str,
    channel: str,
    runloop_enabled: bool,
    engine_name: str,
    engine_requested: str,
    engine_ready: bool,
    engine_error: str | None,
    policy: EnginePolicy,
    ticks_total: int,
    publishes_total: int,
    last_tick_at: str | None,
    last_publish_at: str | None,
    last_tick_monotonic: float | None,
    last_output: dict | None,
) -> dict:
    """Build the /api/v1/engine/state response dict for the Live Engines page.

    Pure-Python so it can be unit-tested without Flask. The caller (app.py)
    snapshots all runloop globals under _state_lock and passes them here.
    """
    last_tick_age = (
        None if last_tick_monotonic is None
        else round(time.monotonic() - last_tick_monotonic, 2)
    )
    return {
        "service": service,
        "channel": channel,
        "runloop_enabled": runloop_enabled,
        "engine": {
            "kind": "engine",
            "requested": engine_requested,
            "loaded": engine_name,
            "ready": engine_ready,
            "error": engine_error,
        },
        "policy_snapshot": asdict(policy),
        "stats": {
            "ticks_total": ticks_total,
            "publishes_total": publishes_total,
            "last_tick_at": last_tick_at,
            "last_publish_at": last_publish_at,
            "last_tick_age_seconds": last_tick_age,
        },
        "last_output": last_output,
    }
