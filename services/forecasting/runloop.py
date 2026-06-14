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
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone

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

# Scaler-facing look-ahead defaults. The CODE defaults reproduce the
# accuracy-optimal single-step behaviour exactly (lead_steps=1, symmetric
# robustness, engine's own fit_window), so an unconfigured deployment is
# byte-identical to the pre-look-ahead run loop. The deployment flips these on
# for the autoscaler path (see services/forecasting/README.md + the bench
# REPORT.md §6.1/§6.2/§7 scaler-facing contract).
DEFAULT_LEAD_STEPS = 1
DEFAULT_ROBUST_MODE = "symmetric"


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

    The scaler-facing look-ahead fields (lead_steps, fit_window, robust_mode)
    are sourced from deployment env at startup, not from the policy payload, so
    a live policy reload preserves them (same treatment as horizon/window). They
    only take effect on an engine that supports them — see run_engine_forecast()
    and engine_base.select_engine().
    """
    horizon_minutes: int = DEFAULT_HORIZON_MINUTES
    window_samples: int = DEFAULT_WINDOW_SAMPLES
    safe_mode: bool = False
    policy_version: int = 0
    # Scaler-facing look-ahead (defaults preserve single-step accuracy mode).
    lead_steps: int = DEFAULT_LEAD_STEPS
    fit_window: int | None = None
    robust_mode: str = DEFAULT_ROBUST_MODE

    def engine_kwargs(self) -> dict:
        """Uniform kwargs set handed to every engine via select_engine().

        select_engine() filters this down to the params each engine's __init__
        actually accepts, so the scaler-facing fit_window / robust_mode only
        reach engines (the harmonic forecaster) that declare them. fit_window is
        omitted when None so the engine keeps its own default window.
        """
        kwargs = {
            "horizon_minutes": self.horizon_minutes,
            "window_samples":  self.window_samples,
            "robust_mode":     self.robust_mode,
        }
        if self.fit_window is not None:
            kwargs["fit_window"] = self.fit_window
        return kwargs

    def lead_steps_normalized(self) -> int:
        """lead_steps clamped to ≥1 (a step of <1 is a no-op single step)."""
        try:
            return max(int(self.lead_steps), 1)
        except (TypeError, ValueError):
            return DEFAULT_LEAD_STEPS


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
        # Scaler-facing look-ahead fields are env-sourced, not policy-driven:
        # carry them through a reload unchanged so a policy publish never
        # silently drops the autoscaler's lead-time configuration.
        lead_steps=fallback.lead_steps,
        fit_window=fallback.fit_window,
        robust_mode=fallback.robust_mode,
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


def run_engine_forecast(
    engine: ForecastEngine, history: HistoryWindow, policy: EnginePolicy
) -> Forecast:
    """Run one inference, dispatching to the look-ahead path when configured.

    Default (lead_steps=1): calls engine.forecast(history) — byte-identical to
    the pre-look-ahead run loop. When lead_steps>1 and the loaded engine exposes
    forecast_ahead(), the run loop asks for a true multi-step lead-time
    projection instead (the scaler-facing mode from the bench REPORT.md §6/§7):
    the autoscaler consumes a forecast warmup_lead buckets ahead, not one bucket.

    Engines without forecast_ahead (moving_average, arima) silently keep their
    single-step forecast() regardless of lead_steps, so flipping the env knob on
    never crashes the loop or forces a fallback to the baseline.

    On the look-ahead path the emitted Forecast is relabelled to the actual lead
    time so the published horizon_minutes stays coherent with how far ahead the
    point estimate really projects: FORECAST_QUERY buckets at 1 minute, so a
    `steps`-bucket lead is `steps` minutes ahead (see relabel_horizon()).
    """
    steps = policy.lead_steps_normalized()
    if steps > 1 and hasattr(engine, "forecast_ahead"):
        forecast = engine.forecast_ahead(history, steps=steps)
        return relabel_horizon(forecast, steps)
    return engine.forecast(history)


# FORECAST_QUERY buckets the request-rate series at one-minute resolution
# (time_bucket('1 minute', ...)), so one look-ahead bucket equals one minute of
# horizon. The published horizon_minutes is derived from the lead in buckets via
# this cadence rather than the engine's own (single-step) label.
FORECAST_BUCKET_MINUTES = 1


def relabel_horizon(forecast: Forecast, lead_steps: int) -> Forecast:
    """Return a copy of `forecast` with horizon_minutes set to the true lead.

    lead_steps buckets × FORECAST_BUCKET_MINUTES min/bucket = minutes ahead.
    Keeps the published/persisted horizon coherent with the projected distance
    when the run loop runs the engine in look-ahead mode.
    """
    return replace(forecast, horizon_minutes=lead_steps * FORECAST_BUCKET_MINUTES)


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


def build_forecast_row(
    forecast: Forecast,
    model_id: str,
    *,
    now: datetime | None = None,
    model_version: str | None = None,
) -> tuple:
    """Build the bind-parameter tuple for FORECASTS_INSERT.

    Order matches the parameter docstring on FORECASTS_INSERT:
      (time, horizon_minutes, predicted_rps,
       confidence_lower, confidence_upper, model_name, model_version)

    `now` is injectable so tests can pin the timestamp; production callers
    pass datetime.now(timezone.utc) once per cycle.
    """
    when = now if now is not None else datetime.now(timezone.utc)
    return (
        when,
        forecast.horizon_minutes,
        forecast.predicted_rps,
        forecast.confidence_lower,
        forecast.confidence_upper,
        model_id,
        model_version,
    )


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
