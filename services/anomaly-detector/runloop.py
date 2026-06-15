"""
services/anomaly-detector/runloop.py
─────────────────────────────────────
Pure-Python pieces of the anomaly-detector run loop, separated from app.py
so they can be unit-tested without Flask, Redis, or a DB connection.

The Flask entry point (app.py) owns:
  - sockets and threads
  - the actual psycopg2 + redis clients
  - request/response handling

This module owns:
  - select-with-fallback engine bootstrap
  - building BackendFeatures from a DB result set
  - converting AnomalyScore → AnomalyEvent envelope payload
  - policy payload → engine kwargs
  - the "should I publish?" gate that respects safe_mode + advisory mode
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import asdict, dataclass

# Make engine_base + plugin folders importable when this file is loaded from
# /app (container) or from services/anomaly-detector/ (dev).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from engine_base import (  # noqa: E402
    AnomalyEngine,
    AnomalyScore,
    BackendFeatures,
    select_engine,
)


# ── policy-derived engine kwargs ──────────────────────────────────────────────

DEFAULT_LATENCY_MULTIPLIER = 3.0
DEFAULT_ERROR_RATE_THRESHOLD = 0.05
DEFAULT_MIN_SAMPLE_COUNT = 10
DEFAULT_FLIP_CONFIRMATION_CYCLES = 2
DEFAULT_RECOVERY_WINDOW_SECONDS = 30

# ── Fix A: peer-relative overload suppression defaults ───────────────────────
# Fraction of *live* backends that must be unhealthy/degraded in the same cycle
# before a backend's organic unhealthy verdict is treated as system-wide
# overload (a scale-out signal) rather than a single broken backend. At/above
# this fraction the verdict is downgraded so the sidecar does NOT exclude it.
# 0.5 ⇒ "if half-or-more of the pool is bad together, nobody is the outlier".
DEFAULT_OVERLOAD_PEER_FRACTION = 0.5
# Below this many live backends, peer comparison is meaningless (you can't tell
# an outlier from the pack with 1-2 samples), so suppression does not engage and
# the raw verdict stands — preserving the pre-fix behavior for tiny pools.
DEFAULT_OVERLOAD_MIN_PEERS = 3
# ── Fix D (D3): outlier margin for the busy-vs-broken suppressor ──────────────
# Under uniform overload ~half the pool sits above the cohort MEDIAN by
# construction, so a strict "> median" outlier test leaks that half through as
# false outliers and collapses the pool. A backend must instead be worse than the
# cohort median by THIS fractional margin on the tripping dimension before it
# counts as a genuine outlier. 0.5 ⇒ "must be > 50% worse than the median pack".
DEFAULT_OVERLOAD_OUTLIER_MARGIN = 0.5
# ── spike-transient hardening: exclusion hysteresis (#1) + surge-suppression (#2)
# #1: a backend must be a cohort-outlier for this many CONSECUTIVE cycles before
# it is benched, so a backend that is only transiently the worst (the first to
# feel a load ramp, before its peers catch up) is not excluded.
DEFAULT_OVERLOAD_EXCLUSION_CONFIRMATIONS = 2
# #2: if the cohort's typical latency or error rate climbs by more than this
# factor cycle-over-cycle, the whole pool is SURGING (a load spike) -> suppress
# every exclusion that cycle (scale-out is the answer, not benching).
DEFAULT_OVERLOAD_SURGE_FACTOR = 1.5


@dataclass
class EnginePolicy:
    """Subset of the operating policy that drives the anomaly engine.

    Built from a smartload.policy envelope payload (or from defaults at
    startup). Passed as kwargs to select_engine() so any engine that takes
    matching constructor params picks them up; engines that don't are free
    to ignore them.
    """
    latency_multiplier: float = DEFAULT_LATENCY_MULTIPLIER
    error_rate_threshold: float = DEFAULT_ERROR_RATE_THRESHOLD
    min_sample_count: int = DEFAULT_MIN_SAMPLE_COUNT
    safe_mode: bool = False
    anomaly_response: str = "auto-isolate"   # "auto-isolate" | "advisory"
    policy_version: int = 0
    # Cycles a raw status change must persist before apply_stability_gate()
    # confirms it (B2 fix). Not an engine constructor param -- excluded from
    # engine_kwargs(), consumed directly by app.py's _inference_cycle.
    flip_confirmation_cycles: int = DEFAULT_FLIP_CONFIRMATION_CYCLES
    # Seconds a backend may stay excluded (last organic verdict non-healthy)
    # before the run loop re-admits it for a probationary re-test (Fix B).
    # Sourced from the smartload.policy `anomaly_recovery_window_seconds` knob.
    # Not an engine constructor param -- excluded from engine_kwargs().
    recovery_window_seconds: float = DEFAULT_RECOVERY_WINDOW_SECONDS
    # ── Fix A: peer-relative overload suppression ────────────────────────────
    # See DEFAULT_OVERLOAD_* above. Not engine constructor params.
    overload_peer_fraction: float = DEFAULT_OVERLOAD_PEER_FRACTION
    overload_min_peers: int = DEFAULT_OVERLOAD_MIN_PEERS
    # Fix D (D3): a backend must be worse than the cohort median by this fraction
    # to count as a true outlier under pool-wide overload (stops the median-split
    # leak that collapsed the pool). Not an engine constructor param.
    overload_outlier_margin: float = DEFAULT_OVERLOAD_OUTLIER_MARGIN
    # Spike-transient hardening (#1 exclusion hysteresis / #2 surge-suppression).
    # Not engine constructor params.
    overload_exclusion_confirmations: int = DEFAULT_OVERLOAD_EXCLUSION_CONFIRMATIONS
    overload_surge_factor: float = DEFAULT_OVERLOAD_SURGE_FACTOR

    def engine_kwargs(self) -> dict:
        return {
            "latency_multiplier":    self.latency_multiplier,
            "error_rate_threshold":  self.error_rate_threshold,
            "min_sample_count":      self.min_sample_count,
        }


def policy_from_payload(payload: dict, fallback: EnginePolicy) -> EnginePolicy:
    """Build an EnginePolicy from a smartload.policy envelope payload.

    Fields missing or with wrong types fall back to the previous values so
    a malformed publish never wipes the live policy.
    """
    def _float(key: str, default: float) -> float:
        try:
            return float(payload.get(key, default))
        except (TypeError, ValueError):
            return default

    def _int(key: str, default: int) -> int:
        try:
            return int(payload.get(key, default))
        except (TypeError, ValueError):
            return default

    return EnginePolicy(
        latency_multiplier=_float("anomaly_latency_multiplier", fallback.latency_multiplier),
        error_rate_threshold=fallback.error_rate_threshold,
        min_sample_count=fallback.min_sample_count,
        safe_mode=bool(payload.get("safe_mode", fallback.safe_mode)),
        anomaly_response=str(payload.get("anomaly_response", fallback.anomaly_response)),
        policy_version=_int("policy_version", fallback.policy_version),
        flip_confirmation_cycles=_int("anomaly_flip_confirmation_cycles", fallback.flip_confirmation_cycles),
        recovery_window_seconds=_float("anomaly_recovery_window_seconds", fallback.recovery_window_seconds),
        overload_peer_fraction=_float("anomaly_overload_peer_fraction", fallback.overload_peer_fraction),
        overload_min_peers=_int("anomaly_overload_min_peers", fallback.overload_min_peers),
        overload_outlier_margin=_float("anomaly_overload_outlier_margin", fallback.overload_outlier_margin),
        overload_exclusion_confirmations=_int("anomaly_overload_exclusion_confirmations", fallback.overload_exclusion_confirmations),
        overload_surge_factor=_float("anomaly_overload_surge_factor", fallback.overload_surge_factor),
    )


# ── engine bootstrap with safety-net fallback ────────────────────────────────

BASELINE_ENGINE_NAME = "threshold"


@dataclass
class EngineBootstrap:
    """Outcome of engine startup. ready=False means we fell back to the baseline."""
    engine: AnomalyEngine
    name: str             # the engine that's actually loaded (may differ from `requested`)
    requested: str        # the engine the operator asked for via ANOMALY_ENGINE
    ready: bool           # True iff the requested engine loaded; False after fallback
    error: str | None     # exception message when ready=False


def bootstrap_engine(requested: str, policy: EnginePolicy) -> EngineBootstrap:
    """Try the requested engine; on any load failure, fall back to the baseline.

    The baseline (threshold) has no model artifact and never fails, so it's
    the safety net. If the baseline itself fails, the exception propagates —
    that's a deployment bug, not something to swallow.
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


# ── DB rows → features ────────────────────────────────────────────────────────

# Instances that are NOT real backends and must never be scored as one.
#
# NGINX records the upstream *block name* `backend_pool` as `$upstream_addr`
# when no live `server` is reachable (the all-down 502 sentinel), and the
# lb-otel-shipper emits `unknown` when NGINX never reached an upstream at all.
# Both leak into the metrics stream as an `instance` with a 100% error_rate
# during any 502 window. Scoring them like a backend yields a phantom
# `unhealthy` verdict that the lb-sidecar then "excludes" — which keeps the
# pool empty, which 502s every request, which re-fires the verdict: a
# self-sustaining outage (see audit/_findings/anomaly-pool-collapse-rootcause).
# They are dropped here so the engine only ever scores real backends.
NON_BACKEND_INSTANCES = frozenset({"backend_pool", "unknown"})


# ANOMALY_QUERY shape: one row per (instance, metric_name) pair, with columns
#   (instance, metric_name, avg_value, max_value, std_value, sample_count)
# We pivot it into one BackendFeatures per instance.

def build_features_from_rows(rows: list[tuple]) -> list[BackendFeatures]:
    """Pivot ANOMALY_QUERY rows into one BackendFeatures per backend.

    Rows are tuples: (instance, metric_name, avg, max, std, sample_count).
    Returns an empty list when no rows arrive (cold DB, idle stack).

    Non-backend instances (`NON_BACKEND_INSTANCES` — the NGINX all-down
    sentinel and the shipper's no-upstream fallback) are skipped so the
    load-balancer aggregate is never scored as if it were a backend.
    """
    by_instance: dict[str, dict] = {}
    for row in rows:
        try:
            instance, metric_name, avg, mx, std, samples = row
        except ValueError:
            continue   # malformed row — skip, don't poison the whole batch
        if instance in NON_BACKEND_INSTANCES:
            continue   # LB aggregate / no-upstream sentinel — not a real backend
        entry = by_instance.setdefault(instance, {})
        entry[metric_name] = {
            "avg":     float(avg) if avg is not None else 0.0,
            "max":     float(mx)  if mx  is not None else 0.0,
            "std":     float(std) if std is not None else 0.0,
            "samples": int(samples) if samples is not None else 0,
        }

    features: list[BackendFeatures] = []
    for instance, metrics in by_instance.items():
        latency = metrics.get("request_latency_ms", {})
        errors  = metrics.get("error_rate", {})
        # Total samples for the gate — sum across metric names since each is a
        # separate row in the query result.
        sample_count = max(latency.get("samples", 0), errors.get("samples", 0))
        features.append(BackendFeatures(
            backend_id=instance,
            latency_ms=latency.get("max", 0.0),
            latency_rolling_mean_ms=latency.get("avg", 0.0),
            error_rate=errors.get("avg", 0.0),
            sample_count=sample_count,
            latency_rolling_std_ms=latency.get("std", 0.0),
        ))
    return features


# ── publish gate ──────────────────────────────────────────────────────────────

def should_publish(score: AnomalyScore, policy: EnginePolicy) -> bool:
    """Decide whether a score becomes a published AnomalyEvent.

    Rules:
      - safe_mode=true               → never publish (operators have explicitly
                                       paused decision flow)
      - anomaly_response="advisory"  → publish every score (downstream callers
                                       see the signal but won't auto-isolate)
      - default ("auto-isolate")     → publish only non-healthy scores so
                                       healthy-noise doesn't flood the bus
    """
    if policy.safe_mode:
        return False
    if policy.anomaly_response == "advisory":
        return True
    return score.status != "healthy"


# ── stability gate (per-backend memory across cycles) ───────────────────────

@dataclass
class BackendState:
    """Per-backend memory carried across inference cycles by
    apply_stability_gate(). One instance per backend_id, owned by app.py's
    run loop (not thread-shared)."""
    last_status: str = "healthy"
    last_score: float = 0.0
    pending_status: str | None = None
    pending_count: int = 0
    # Consecutive cycles the low-sample hold (B1) has frozen a non-healthy
    # status. Used by the optional max_hold_cycles TTL so a backend that goes
    # permanently quiet can't be pinned non-healthy forever.
    low_sample_hold_count: int = 0
    # ── Fix B: time-based re-inclusion bookkeeping ───────────────────────────
    # time.monotonic() of the cycle that first put this backend into the
    # excluded (unhealthy) state. None ⇒ not currently excluded. Set when a
    # gated verdict first becomes "unhealthy", cleared when it returns to
    # "healthy" (organically or via a recovery re-admit). recovery_reinclude()
    # reads it to decide when the exclusion has aged past the recovery window.
    excluded_since_monotonic: float | None = None
    # True once recovery_reinclude() has emitted a probationary re-admit for the
    # CURRENT exclusion, so we re-admit exactly once per exclusion (no thrash /
    # no repeated healthy publishes while the backend keeps getting no traffic).
    recovery_reinclude_emitted: bool = False
    # #1 spike-transient hardening: consecutive cycles this backend has been a
    # cohort-outlier (past the suppressor margin). Reset when it falls back within
    # the pack; an exclusion is only kept once the streak reaches
    # overload_exclusion_confirmations, so a transient outlier isn't benched.
    outlier_streak: int = 0


def apply_stability_gate(
    raw: AnomalyScore,
    low_sample: bool,
    state: BackendState,
    confirmation_cycles: int,
    max_hold_cycles: int | None = None,
) -> AnomalyScore:
    """Wrap an engine's raw AnomalyScore with per-backend memory.

    Fixes two operational gaps:

    - B1 (sample-count blind spot): engines force "healthy"/0.0 when
      features.sample_count < min_sample_count (see
      IsolationForestEngine.score / ThresholdEngine.score) -- a backend
      failing fast on every request produces few samples and would
      otherwise be reported healthy. "No new evidence" should mean "no
      change": if `low_sample` is True and the backend's last confirmed
      status was non-healthy, that status/score is preserved instead.

    - B2 (no hysteresis/cooldown): a status change away from
      `state.last_status` must be observed for `confirmation_cycles`
      consecutive cycles before it is confirmed (returned as-is). Until
      confirmed, the previous stable status/score is returned, so a single
      noisy sample can't flip a backend's published status.

    `max_hold_cycles` (TTL) bounds the B1 hold: without it, a backend that
    goes permanently quiet (low_sample forever) after a non-healthy reading
    would be pinned non-healthy until process restart. When the hold has run
    for more than `max_hold_cycles` consecutive cycles, the hold is released
    and the raw (low-sample) reading is processed normally — so the status
    decays back toward healthy through the usual confirmation path instead of
    sticking forever. `None` (default) preserves the original unbounded hold.

    Mutates `state` in place and returns the gated AnomalyScore. Evidence
    fields (metric / observed_value / threshold) are carried from `raw` only
    when the raw verdict is the one returned; a held/pending verdict reuses
    the last confirmed status with no stale evidence attached.
    """
    if low_sample and state.last_status != "healthy":
        state.low_sample_hold_count += 1
        if max_hold_cycles is None or state.low_sample_hold_count <= max_hold_cycles:
            return AnomalyScore(raw.backend_id, state.last_status, state.last_score)
        # TTL exceeded: fall through and let the raw reading be processed by the
        # normal confirmation logic below, so the held status can decay.
    else:
        state.low_sample_hold_count = 0

    if raw.status == state.last_status:
        state.last_score = raw.score
        state.pending_status = None
        state.pending_count = 0
        return raw

    if state.pending_status == raw.status:
        state.pending_count += 1
    else:
        state.pending_status = raw.status
        state.pending_count = 1

    if state.pending_count >= confirmation_cycles:
        state.last_status = raw.status
        state.last_score = raw.score
        state.pending_status = None
        state.pending_count = 0
        return raw

    return AnomalyScore(raw.backend_id, state.last_status, state.last_score)


# ── Fix A: peer-relative overload suppression ────────────────────────────────

# Statuses that, when produced by the *latency or error* channels, the sidecar
# would act on by excluding the backend. Only these are candidates for the
# peer-relative downgrade. "healthy" is never suppressed (nothing to suppress).
_EXCLUDABLE_STATUSES = frozenset({"unhealthy", "degraded"})


def _median(values: list[float]) -> float:
    """Tiny dependency-free median (avoid importing statistics for one call)."""
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def peer_suppress_verdicts(
    scored: list[tuple[BackendFeatures, AnomalyScore]],
    policy: EnginePolicy,
    states: list[BackendState] | None = None,
    cohort_memory: dict | None = None,
) -> list[AnomalyScore]:
    """Fix A — busy-vs-broken: downgrade organic exclusions during pool-wide
    overload so a healthy-but-overloaded backend is NOT excluded.

    Operates on every backend's (features, gated_score) for ONE cycle, so it
    has the cross-backend/load context the per-backend engine.score() lacks.

    Decision (only engages when there are >= ``overload_min_peers`` live
    backends — with 1-2 backends you cannot tell an outlier from the pack, so
    the raw verdicts stand):

    A backend is a genuine fault only if it is *meaningfully worse* than its
    peers on the dimension that tripped it: we compare its error_rate and its
    typical (rolling-mean) latency against the cohort medians, with a margin. A
    backend within ``(1 + overload_outlier_margin)`` of the median pack is
    downgraded to "healthy" (it keeps its traffic — pool-wide overload is a
    scale-out signal, not a fault); a backend clearly past the margin keeps its
    exclusion (a real single bad apple among an overloaded pool is still
    flagged). This runs from the FIRST backend to trip — there is no
    pool-fraction gate, because waiting for half the pool to be excludable let
    exclusions CASCADE the pool down to ~half before the suppressor ever engaged.
    A lone fault among healthy peers is far past the margin, so it is still
    excluded exactly as before.

    Spike-transient hardening (active only when ``states`` / ``cohort_memory`` are
    supplied; without them the behaviour above is unchanged):
      #1 exclusion hysteresis — a backend must be a cohort-outlier for
         ``overload_exclusion_confirmations`` CONSECUTIVE cycles (tracked in
         ``BackendState.outlier_streak``) before it is benched, so a backend that
         is only transiently the worst (the first to feel a load ramp, before its
         peers catch up) is not excluded.
      #2 surge-suppression — when the cohort's typical latency or error climbs by
         more than ``overload_surge_factor`` cycle-over-cycle (carried in
         ``cohort_memory``), the whole pool is SURGING (a load spike), so EVERY
         exclusion is suppressed that cycle — a synchronized ramp is overload, not
         a fault. The cohort-wide mirror of the engine's per-backend "recovering"
         (falling-latency) guard.

    Returns a NEW list of AnomalyScore (input scores are not mutated). Order
    matches the input. Inputs whose verdict is left unchanged are returned
    as-is.
    """
    out = [score for _f, score in scored]
    live = len(scored)
    if live < max(2, policy.overload_min_peers):
        return out  # too few peers to compare — keep raw verdicts

    excludable_idx = [
        i for i, (_f, s) in enumerate(scored)
        if s.status in _EXCLUDABLE_STATUSES
    ]

    # Cohort baselines across ALL live backends this cycle. Typical (rolling-mean)
    # latency, NOT the window MAX, so one transient spike on an otherwise-normal
    # backend doesn't make it look like an outlier (D9/D3).
    err_median = _median([f.error_rate for f, _s in scored])
    lat_median = _median([f.latency_rolling_mean_ms for f, _s in scored])

    # #2 SURGE detection: did the whole cohort's load climb sharply since last
    # cycle? A synchronized ramp is a load spike (scale-out), not a fault, so we
    # suppress EVERY exclusion this cycle. Carried across cycles in cohort_memory.
    surging = False
    if cohort_memory is not None:
        f = max(1.0, policy.overload_surge_factor)
        prev_lat = cohort_memory.get("lat_median")
        prev_err = cohort_memory.get("err_median")
        if prev_lat is not None and prev_lat > 0.0 and lat_median > prev_lat * f:
            surging = True
        if (prev_err is not None and err_median > policy.error_rate_threshold
                and err_median > prev_err * f):
            surging = True
        cohort_memory["lat_median"] = lat_median
        cohort_memory["err_median"] = err_median

    if not excludable_idx:
        if states is not None:           # healthy cohort — reset every streak
            for st in states:
                st.outlier_streak = 0
        return out

    # NO pool-fraction gate — the per-backend MARGIN is the overload-vs-fault
    # discriminator. Waiting for >= overload_peer_fraction of the pool to be
    # excludable let exclusions CASCADE the pool to ~half before the suppressor
    # engaged; instead we evaluate every excludable backend from the first to trip.
    # (`overload_peer_fraction` is retained in the policy for back-compat.)
    margin = 1.0 + max(0.0, policy.overload_outlier_margin)
    err_bar = err_median * margin
    lat_bar = lat_median * margin
    excl_set = set(excludable_idx)
    confirmations = max(1, policy.overload_exclusion_confirmations)

    for i in range(live):
        if i not in excl_set:
            if states is not None:           # within-pack / healthy -> reset streak
                states[i].outlier_streak = 0
            continue
        feats, score = scored[i]
        is_outlier = (feats.error_rate > err_bar) or (feats.latency_rolling_mean_ms > lat_bar)

        # #2: during a cohort-wide surge nobody is the fault — keep everyone.
        if surging:
            out[i] = AnomalyScore(score.backend_id, "healthy", 0.0)
            if states is not None:
                states[i].outlier_streak = 0
            continue

        if not is_outlier:
            # Within the pack ⇒ pool-wide overload, not a fault: keep it serving.
            if states is not None:
                states[i].outlier_streak = 0
            out[i] = AnomalyScore(score.backend_id, "healthy", 0.0)
            continue

        # is_outlier == True. #1 exclusion hysteresis: only bench a SUSTAINED
        # outlier, so a backend that is merely the first to feel a ramp isn't
        # excluded before its peers catch up. Without state tracking this is a
        # no-op (bench immediately, as before).
        if states is not None:
            states[i].outlier_streak += 1
            if states[i].outlier_streak < confirmations:
                out[i] = AnomalyScore(score.backend_id, "healthy", 0.0)
                continue
        # sustained outlier (or no state) -> keep the exclusion verdict (out[i]
        # already holds the original unhealthy/degraded score).
    return out


# ── Fix B: time-based re-inclusion ───────────────────────────────────────────

def recovery_reinclude(
    backend_id: str,
    gated_status: str,
    state: BackendState,
    policy: EnginePolicy,
    now_monotonic: float,
) -> AnomalyScore | None:
    """Fix B — self-heal trap: re-admit a long-excluded backend so it can earn
    its health back (an excluded backend gets no traffic → no fresh healthy
    metrics → would stay excluded forever).

    Updates ``state`` exclusion bookkeeping for THIS cycle's gated status, then
    decides whether to emit a probationary "healthy" re-admit:

      - Track when the backend first entered the excluded ("unhealthy") state.
      - If it has been excluded for >= ``recovery_window_seconds`` AND this
        cycle did NOT produce a fresh unhealthy verdict (no new adverse
        evidence), emit ONE "healthy" include verdict so the sidecar re-admits
        it. It then either proves itself (stays healthy) or is re-excluded next
        cycle if still genuinely bad — a correct, non-thrashing probation.

    Returns the re-admit AnomalyScore to publish, or None when no re-admit is
    warranted (caller then publishes/handles the gated verdict normally).

    Call this AFTER peer-suppression so ``gated_status`` is the verdict that
    will actually be acted on. The manual-isolate path never flows through here
    (it bypasses the run loop entirely), so operator isolates are untouched.
    """
    window = policy.recovery_window_seconds

    if gated_status == "unhealthy":
        # Fresh adverse evidence. (Re)start the exclusion clock if this is a new
        # exclusion; an already-excluded backend keeps its original timestamp so
        # the window measures total time excluded, not time since last verdict.
        if state.excluded_since_monotonic is None:
            state.excluded_since_monotonic = now_monotonic
            state.recovery_reinclude_emitted = False
        return None

    if gated_status == "healthy":
        # Organically healthy again — exclusion is over, reset bookkeeping.
        state.excluded_since_monotonic = None
        state.recovery_reinclude_emitted = False
        return None

    # gated_status == "degraded": not a fresh *exclusion* and not a clean clear.
    # If the backend isn't currently excluded, there is nothing to re-admit.
    if state.excluded_since_monotonic is None:
        return None

    # Backend is excluded and this cycle produced no fresh unhealthy verdict.
    # Re-admit once the exclusion has aged past the recovery window.
    excluded_for = now_monotonic - state.excluded_since_monotonic
    if excluded_for >= window and not state.recovery_reinclude_emitted:
        state.recovery_reinclude_emitted = True
        # Clear the exclusion clock — we've handed it back to the pool. If it's
        # still bad it will be re-excluded next cycle and the clock restarts.
        state.excluded_since_monotonic = None
        return AnomalyScore(backend_id, "healthy", 0.0)
    return None


def recovery_reinclude_silent(
    backend_id: str,
    state: BackendState,
    policy: EnginePolicy,
    now_monotonic: float,
) -> AnomalyScore | None:
    """Fix B (silent-backend variant) — re-admit a backend that is on the
    exclusion clock but produced NO features this cycle.

    ``recovery_reinclude`` only runs for backends present in the metrics query.
    A benched backend gets zero NGINX traffic, so it emits no rows, drops out of
    the query entirely, and is never iterated — its exclusion clock freezes and it
    stays ``down;`` for the rest of the run (the no-recovery trap). This variant is
    driven off the detector's own per-backend ``state`` instead of query presence:
    the *absence* of fresh metrics IS the "no new adverse evidence" signal, so once
    the exclusion has aged past ``recovery_window_seconds`` we emit ONE probationary
    ``healthy`` re-admit (idempotent via ``recovery_reinclude_emitted``). The sidecar
    then routes a trickle to it at floor weight; it either proves healthy and stays
    or re-sheds and is re-excluded next cycle (the clock restarts via
    ``recovery_reinclude``).

    The stability-gate memory is reset to a clean ``healthy`` slate on re-admit, so
    the gate does not immediately re-confirm the stale ``unhealthy`` status and
    re-exclude the backend before it has had a fair re-test. The sidecar's live-pool
    membership guard drops the verdict if the backend was meanwhile scaled away, so
    re-admitting a departed backend is a harmless no-op.
    """
    if state.excluded_since_monotonic is None:
        return None
    excluded_for = now_monotonic - state.excluded_since_monotonic
    if excluded_for >= policy.recovery_window_seconds:
        # Re-ARM the exclusion clock to NOW (rather than clearing it to None) so
        # that if the backend stays silent — the sidecar never routed to it, or it
        # is stuck excluded across a bench/run boundary — we re-probe it again after
        # the next window instead of giving up after a single attempt and leaving it
        # in a "detector thinks it's fine, sidecar still has it down;" limbo. The
        # re-arm also rate-limits the probe to once per recovery window. Once the
        # backend returns to the metrics query and is confirmed healthy,
        # recovery_reinclude() clears the clock for real.
        state.excluded_since_monotonic = now_monotonic
        state.recovery_reinclude_emitted = True
        # Hand the backend back with a clean slate so apply_stability_gate doesn't
        # re-confirm the stale unhealthy status and re-exclude it next cycle.
        state.last_status = "healthy"
        state.last_score = 0.0
        state.pending_status = None
        state.pending_count = 0
        state.low_sample_hold_count = 0
        return AnomalyScore(backend_id, "healthy", 0.0)
    return None


def _severity_for_status(status: str) -> str | None:
    """Map the three-tier health status onto the operator-ui alert bucket.
    healthy verdicts aren't alerts, so they get no severity."""
    if status == "unhealthy":
        return "critical"
    if status == "degraded":
        return "warning"
    return None


def score_to_event_payload(score: AnomalyScore, model_version: str) -> dict:
    """Serialise an AnomalyScore + model id into the AnomalyEvent dict shape
    expected by publish_envelope. model_version goes onto the envelope for
    debug / provenance per the contracts.py docstring.

    When the engine attached evidence (metric / observed_value / threshold) the
    payload carries it through plus a derived UI severity, so the operator-ui
    Active Alerts panel can render "latency_ms 312 (threshold 250)" without a
    second round-trip. Evidence keys are omitted when absent to keep healthy /
    legacy payloads unchanged."""
    payload = {
        "backend_id":    score.backend_id,
        "status":        score.status,
        "score":         score.score,
        "model_version": model_version,
    }
    if score.metric is not None:
        payload["metric"] = score.metric
    if score.observed_value is not None:
        payload["observed_value"] = score.observed_value
    if score.threshold is not None:
        payload["threshold"] = score.threshold
    severity = _severity_for_status(score.status)
    if severity is not None:
        payload["severity"] = severity
    return payload


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
    last_output: list[dict] | dict | None,
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
