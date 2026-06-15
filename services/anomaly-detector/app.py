"""
services/anomaly-detector/app.py
─────────────────────────────────
Anomaly-detector entry point.

Phase-0 mode (default):  /health only, no inference. Backwards-compatible.
Phase-1 mode:            enabled by ANOMALY_RUNLOOP_ENABLED=true. The service
                         polls TimescaleDB on POLL_INTERVAL_SECONDS, scores
                         each backend via the configured engine, and publishes
                         AnomalyEvent envelopes to smartload.anomaly. Subscribes
                         to smartload.policy for live parameter reload.

Engine selection (ANOMALY_ENGINE env var):
  - "trend_rule" (default) — interpretable, stateful trend-aware rule engine;
                             no model artifact needed. Closes the gradual-
                             degradation gap the stateless engines miss.
  - "threshold"            — stateless rule-based baseline; no model artifact.
  - "trend_forest"         — trained model over the enriched temporal vector.
  - "isolation_forest"     — trained model from issue #101. Falls back to
                             threshold if the .pkl is missing.

Safety:
  - The run loop is opt-in by env var so the Phase-0 stub stays the default
    until the cutover is smoke-tested per-service per issue #138.
  - If the requested engine can't load, the service runs the baseline and
    reports engine_ready=false on /health — never crashes on startup.

Health endpoint adds three engine fields when the run loop is enabled:
  engine_type, engine_ready, last_inference_age_seconds.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import asdict

import psycopg2
import redis as redis_lib
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, request
from prometheus_client import Counter

# Resolve shared/ across container layout (/app/shared) and dev layout
# (services/shared/ relative to this file). Same pattern as telemetry/app.py.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (_HERE, os.path.dirname(_HERE)):
    if os.path.isdir(os.path.join(_cand, "shared")):
        sys.path.insert(0, _cand)
        break
from shared.contracts import (                                # noqa: E402
    make_envelope,
    parse_envelope,
    publish_envelope,
)
from shared.metrics import ServiceMetrics, metrics_response     # noqa: E402
from shared import bootstrap, config                            # noqa: E402
from shared.logging_setup import install_correlation_middleware # noqa: E402
from shared.queries import (                                   # noqa: E402
    ANOMALY_DEFAULT_SERVICE,
    ANOMALY_HISTORY_QUERY,
    ANOMALY_METRIC_NAMES,
    ANOMALY_QUERY,
    BACKEND_HEALTH_INSERT,
    BACKEND_HEALTH_QUERY,
)

from runloop import (                                          # noqa: E402
    BackendState,
    EnginePolicy,
    NON_BACKEND_INSTANCES,
    apply_stability_gate,
    bootstrap_engine,
    build_features_from_rows,
    peer_suppress_verdicts,
    policy_from_payload,
    recovery_reinclude,
    recovery_reinclude_silent,
    score_to_event_payload,
    serialize_engine_state,
    should_publish,
)
from manual import (                                          # noqa: E402
    ManualIsolateError,
    plan_manual_isolate,
)

app = Flask(__name__)
# Per-request correlation IDs (#143): mint/propagate X-Correlation-ID (or a W3C
# traceparent trace-id) and echo it on the response.
install_correlation_middleware(app)

# Prometheus own-metrics (#161). METRICS provides the common surface
# (anomaly_detector_cycle_*/_publish_*/_up); ISOLATE_TOTAL is the
# service-specific decision distribution.
METRICS = ServiceMetrics("anomaly_detector")
ISOLATE_TOTAL = Counter(
    "anomaly_detector_isolate_total",
    "Anomaly verdicts published, by backend and status",
    ["backend", "status"],
)

# Env via the shared typed config helpers (v1.0.7av) — consistent parsing +
# single-source defaults. Behaviour-preserving: same vars, same defaults.
SERVICE_NAME = config.env_str("SERVICE_NAME", "anomaly-detector")
PORT = config.env_int("PORT", 8082)
TIMESCALEDB_URL = config.timescaledb_url()
REDIS_URL = config.redis_url()

RUNLOOP_ENABLED         = config.env_bool("ANOMALY_RUNLOOP_ENABLED", False)
ANOMALY_ENGINE          = config.env_str("ANOMALY_ENGINE", "trend_rule")
POLL_INTERVAL_SECONDS   = config.env_float("POLL_INTERVAL_SECONDS", 10)
WINDOW_SECONDS          = config.env_int("ANOMALY_WINDOW_SECONDS", 60)
TELEMETRY_SERVICE       = config.env_str("ANOMALY_TELEMETRY_SERVICE", ANOMALY_DEFAULT_SERVICE)
# Cycles a raw status change must persist before apply_stability_gate() confirms
# it (B2 hysteresis). Seeds the startup EnginePolicy; a smartload.policy publish
# can still override it live via anomaly_flip_confirmation_cycles. Default tracks
# EnginePolicy.flip_confirmation_cycles so behaviour is unchanged when unset.
FLIP_CONFIRMATION_CYCLES = config.env_int(
    "ANOMALY_FLIP_CONFIRMATION_CYCLES", EnginePolicy().flip_confirmation_cycles
)
# Fix B — re-inclusion window: how long a backend may stay excluded before the
# run loop re-admits it for a probationary re-test. Seeds the startup
# EnginePolicy; a smartload.policy publish overrides it live via
# anomaly_recovery_window_seconds. Default tracks EnginePolicy so behaviour is
# unchanged when unset.
RECOVERY_WINDOW_SECONDS = config.env_float(
    "ANOMALY_RECOVERY_WINDOW_SECONDS", EnginePolicy().recovery_window_seconds
)
# Window for the startup exclusion-clock hydration (see _hydrate_exclusion_clocks).
# Matches the sidecar's LB_SIDECAR_HEALTH_HYDRATION_WINDOW_SECONDS default so both
# services inherit the same durable view of which backends were last excluded.
EXCLUSION_HYDRATION_WINDOW_SECONDS = config.env_int(
    "ANOMALY_EXCLUSION_HYDRATION_WINDOW_SECONDS", 300
)
# Fix A — peer-relative overload suppression knobs. overload_peer_fraction:
# fraction of live backends that must be degraded together before exclusions are
# treated as system-wide overload and suppressed. overload_min_peers: minimum
# live backends for peer comparison to engage at all. Both seed the startup
# EnginePolicy and can be overridden live via anomaly_overload_peer_fraction /
# anomaly_overload_min_peers in a smartload.policy publish.
OVERLOAD_PEER_FRACTION = config.env_float(
    "ANOMALY_OVERLOAD_PEER_FRACTION", EnginePolicy().overload_peer_fraction
)
OVERLOAD_MIN_PEERS = config.env_int(
    "ANOMALY_OVERLOAD_MIN_PEERS", EnginePolicy().overload_min_peers
)
# #3 absolute pool-overload guard master switch. On by default; the ablation
# benchmark flips it off (ANOMALY_ABSOLUTE_OVERLOAD_SUPPRESSION=false) to isolate
# the guard's contribution. Also overridable live via the policy envelope.
ABSOLUTE_OVERLOAD_SUPPRESSION = config.env_bool(
    "ANOMALY_ABSOLUTE_OVERLOAD_SUPPRESSION", EnginePolicy().absolute_overload_suppression
)

# Liveness threshold for /health (#163). If the loop hasn't ticked in this
# many seconds, /health flips to degraded so the silent-thread-death pattern
# becomes visible.
LIVENESS_STALE_SECONDS         = bootstrap.liveness_stale_seconds(POLL_INTERVAL_SECONDS)
LOOP_RECOVERY_BACKOFF_SECONDS  = 2.0

ANOMALY_CHANNEL = "smartload.anomaly"
POLICY_CHANNEL  = "smartload.policy"


# ── shared state ──────────────────────────────────────────────────────────────

_state_lock = threading.Lock()
_engine = None
_engine_name: str = ANOMALY_ENGINE
_engine_requested: str = ANOMALY_ENGINE
_engine_ready: bool = False
_engine_error: str | None = None
_policy: EnginePolicy = EnginePolicy(
    flip_confirmation_cycles=FLIP_CONFIRMATION_CYCLES,
    recovery_window_seconds=RECOVERY_WINDOW_SECONDS,
    overload_peer_fraction=OVERLOAD_PEER_FRACTION,
    overload_min_peers=OVERLOAD_MIN_PEERS,
    absolute_overload_suppression=ABSOLUTE_OVERLOAD_SUPPRESSION,
)
_last_inference_monotonic: float | None = None
# Live Engines (#121) tracking — appended each cycle, read by /api/v1/engine/state.
_ticks_total: int = 0
_publishes_total: int = 0
_last_tick_at_iso: str | None = None
_last_publish_at_iso: str | None = None
_last_output_payload: list[dict] | None = None
# Per-backend stability-gate memory (B1 low-sample hold / B2 flip confirmation).
# Only touched by the run-loop thread (_inference_cycle) — not under _state_lock.
_backend_states: dict[str, BackendState] = {}
# Cross-cycle memory for the peer-suppressor's #2 surge detection (last cycle's
# cohort medians). Owned by the single-threaded run loop.
_suppressor_cohort: dict = {}


def _set_engine_state(bootstrap) -> None:
    global _engine, _engine_name, _engine_requested, _engine_ready, _engine_error
    _engine = bootstrap.engine
    _engine_name = bootstrap.name
    _engine_requested = bootstrap.requested
    _engine_ready = bootstrap.ready
    _engine_error = bootstrap.error


# ── connectivity checks ───────────────────────────────────────────────────────

def check_redis() -> tuple[bool, str | None]:
    # Delegates to the shared probe (v1.0.7aw); same (ok, detail) contract.
    return bootstrap.check_redis(REDIS_URL)


def check_timescaledb() -> tuple[bool, str | None]:
    return bootstrap.check_timescaledb(TIMESCALEDB_URL)


# ── inference cycle ───────────────────────────────────────────────────────────

def _query_features(db_conn) -> list:
    """Run ANOMALY_QUERY against the live DB and pivot rows → BackendFeatures."""
    with db_conn.cursor() as cur:
        cur.execute(
            ANOMALY_QUERY,
            (f"{WINDOW_SECONDS} seconds", TELEMETRY_SERVICE, ANOMALY_METRIC_NAMES),
        )
        rows = cur.fetchall()
    return build_features_from_rows(rows)


def _inference_cycle(db_conn, redis_client) -> int:
    """One poll cycle. Returns the number of envelopes published."""
    global _last_inference_monotonic, _ticks_total, _publishes_total
    global _last_tick_at_iso, _last_publish_at_iso, _last_output_payload

    with _state_lock:
        engine = _engine
        policy = _policy
        model_version = f"{_engine_name}"   # baseline name as version tag

    if engine is None:
        return 0

    try:
        features_list = _query_features(db_conn)
    except Exception as exc:                            # noqa: BLE001
        print(f"[{SERVICE_NAME}] DB query failed: {exc}", flush=True)
        return 0

    published = 0
    cycle_outputs: list[dict] = []
    now_dt = datetime.now(timezone.utc)
    now_mono = time.monotonic()

    # ── Pass 1: score + stability-gate every backend, collecting the whole
    # cycle's verdicts. Fix A (peer-relative overload suppression) needs every
    # backend's features+verdict together, so we gather first and decide after.
    scored: list[tuple] = []   # (features, gated_score, BackendState)
    for features in features_list:
        try:
            raw_score = engine.score(features)
        except Exception as exc:                        # noqa: BLE001
            print(f"[{SERVICE_NAME}] engine.score failed for {features.backend_id}: {exc}", flush=True)
            continue

        # Wrap the engine's raw verdict with per-backend stability memory
        # (B1 low-sample hold + B2 flip confirmation) before it is persisted
        # or published — see runloop.apply_stability_gate.
        low_sample = features.sample_count < policy.min_sample_count
        state = _backend_states.setdefault(features.backend_id, BackendState())
        score = apply_stability_gate(raw_score, low_sample, state, policy.flip_confirmation_cycles)
        scored.append((features, score, state))

    # ── Pass 2 (Fix A): peer-relative overload suppression. If a configurable
    # majority of live backends are degraded together this is system-wide
    # overload (a scale-out signal), so an organic exclusion on a backend that
    # is no worse than its peers is downgraded to healthy — it keeps its
    # traffic. A genuine lone outlier still gets flagged. Manual isolates never
    # reach this path (they bypass the run loop via POST /api/v1/isolate).
    suppressed_scores = peer_suppress_verdicts(
        [(f, s) for (f, s, _st) in scored], policy,
        states=[st for (_f, _s, st) in scored],
        cohort_memory=_suppressor_cohort,
    )

    # ── Pass 3: persist + publish each backend's final verdict, applying Fix B
    # (time-based re-inclusion) so a backend excluded longer than the recovery
    # window with no fresh unhealthy verdict is re-admitted to earn its health
    # back.
    for (features, _gated, state), final_score in zip(scored, suppressed_scores):
        score = final_score

        # Fix B: update exclusion bookkeeping and, when due, swap in a
        # probationary "healthy" re-admit. Driven by the post-suppression
        # status so we never re-admit a backend we just (correctly) excluded.
        reinclude = recovery_reinclude(
            score.backend_id, score.status, state, policy, now_mono
        )
        force_publish = False
        if reinclude is not None:
            print(f"[{SERVICE_NAME}] recovery re-admit backend_id={score.backend_id} "
                  f"(excluded > {policy.recovery_window_seconds}s, no fresh fault)", flush=True)
            score = reinclude
            # A re-admit is a "healthy" verdict; in auto-isolate mode
            # should_publish() drops healthy scores, but the whole point of the
            # re-admit is to tell the sidecar to re-route to this backend — so
            # this one healthy verdict MUST be published.
            force_publish = True

        cycle_outputs.append(score_to_event_payload(score, model_version))

        # Persist every backend's verdict every cycle so lb-sidecar startup
        # hydration always has fresh data (previously only /api/v1/isolate
        # wrote backend_health). db_conn.autocommit is True (see _run_loop),
        # so each insert commits independently and a failure can't poison the
        # rest of the cycle.
        try:
            with db_conn.cursor() as cur:
                cur.execute(
                    BACKEND_HEALTH_INSERT,
                    (now_dt, score.backend_id, score.status, score.score),
                )
        except Exception as exc:                        # noqa: BLE001
            print(f"[{SERVICE_NAME}] backend_health write failed for {score.backend_id}: {exc}", flush=True)

        # safe_mode still pauses ALL publishes (operators paused decision flow);
        # otherwise a recovery re-admit forces its healthy verdict onto the bus.
        if not (force_publish and not policy.safe_mode) and not should_publish(score, policy):
            continue
        with METRICS.time_publish(ANOMALY_CHANNEL):
            publish_envelope(
                redis_client,
                channel=ANOMALY_CHANNEL,
                source=SERVICE_NAME,
                payload=score_to_event_payload(score, model_version),
            )
        ISOLATE_TOTAL.labels(backend=score.backend_id, status=score.status).inc()
        published += 1

    # ── Pass 3b (Fix B, silent-backend recovery): a benched backend gets zero
    # NGINX traffic, so it emits no metric rows, drops out of the query, and never
    # appears in `scored` — which means recovery_reinclude (Pass 3) is NEVER called
    # for it and it stays `down;` for the rest of the run (the no-recovery trap).
    # Drive recovery off the detector's own per-backend state, not query presence:
    # for any backend on the exclusion clock that produced no features this cycle,
    # emit the same probationary "healthy" re-admit once it has aged past the
    # recovery window so the sidecar can re-route a trickle and let it earn its
    # health back. The sidecar's live-pool membership guard drops the verdict if the
    # backend was meanwhile scaled away, so this is safe.
    scored_ids = {f.backend_id for (f, _s, _st) in scored}
    for backend_id, state in list(_backend_states.items()):
        if backend_id in scored_ids:
            continue
        readmit = recovery_reinclude_silent(backend_id, state, policy, now_mono)
        if readmit is None:
            continue
        print(f"[{SERVICE_NAME}] recovery re-admit (silent backend) backend_id={backend_id} "
              f"(excluded > {policy.recovery_window_seconds}s, no metrics this cycle)", flush=True)
        readmit_payload = score_to_event_payload(readmit, model_version)
        cycle_outputs.append(readmit_payload)
        try:
            with db_conn.cursor() as cur:
                cur.execute(
                    BACKEND_HEALTH_INSERT,
                    (now_dt, readmit.backend_id, readmit.status, readmit.score),
                )
        except Exception as exc:                        # noqa: BLE001
            print(f"[{SERVICE_NAME}] backend_health write failed for {readmit.backend_id}: {exc}", flush=True)
        # safe_mode pauses ALL publishes; otherwise the re-admit MUST reach the bus
        # (should_publish would otherwise drop a healthy verdict).
        if policy.safe_mode:
            continue
        with METRICS.time_publish(ANOMALY_CHANNEL):
            publish_envelope(
                redis_client,
                channel=ANOMALY_CHANNEL,
                source=SERVICE_NAME,
                payload=readmit_payload,
            )
        ISOLATE_TOTAL.labels(backend=readmit.backend_id, status=readmit.status).inc()
        published += 1

    now_iso = now_dt.isoformat()
    with _state_lock:
        _last_inference_monotonic = time.monotonic()
        _ticks_total += 1
        _last_tick_at_iso = now_iso
        if cycle_outputs:
            _last_output_payload = cycle_outputs
        if published:
            _publishes_total += published
            _last_publish_at_iso = now_iso
    return published


# ── policy subscription ───────────────────────────────────────────────────────

def _handle_policy_message(raw) -> None:
    """Parse a smartload.policy envelope, rebuild the engine on relevant
    parameter changes, and call engine.reload() so trained models can
    re-read their artifact if they want to."""
    parsed = parse_envelope(raw, channel=POLICY_CHANNEL)
    if parsed is None:
        return
    payload, _meta = parsed

    with _state_lock:
        new_policy = policy_from_payload(payload, fallback=_policy)
        if new_policy.policy_version < _policy.policy_version:
            print(f"[{SERVICE_NAME}] ignoring policy v{new_policy.policy_version} "
                  f"(current v{_policy.policy_version}) — stale publish", flush=True)
            return
        _refresh_engine_under_lock(new_policy)


def _refresh_engine_under_lock(new_policy: EnginePolicy) -> None:
    """Replace _policy and _engine atomically with the new policy values.

    Caller must hold _state_lock. The engine is reconstructed so any
    policy-derived constructor params (latency multiplier etc.) take effect
    immediately; trained-model engines should keep their loaded artifact
    cached and re-applying kwargs should be cheap."""
    global _policy
    _policy = new_policy
    boot = bootstrap_engine(_engine_requested, _policy)
    _set_engine_state(boot)
    try:
        boot.engine.reload()
    except Exception as exc:                            # noqa: BLE001
        print(f"[{SERVICE_NAME}] engine.reload() raised: {exc}", flush=True)


# ── run loop ──────────────────────────────────────────────────────────────────

def _hydrate_exclusion_clocks(db_conn) -> int:
    """Seed exclusion clocks for backends left ``down`` across a detector restart.

    The silent-backend recovery path (Pass 3b / recovery_reinclude_silent) only
    re-admits a benched backend the detector is already tracking in _backend_states
    with an armed exclusion clock. But a fresh detector process starts with an
    EMPTY _backend_states: a backend that is already excluded (``down;`` in
    upstream.conf — the sidecar hydrates that from backend_health on its own
    restart) receives zero NGINX traffic, emits no metric rows, never enters
    _backend_states, and is therefore never recovered — it stays ``down`` for the
    whole life of the process. That is the no-recovery-ACROSS-RESTART deadlock: it
    bites every time the decision plane is recreated (e.g. each benchmark side)
    while a prior exclusion is still live, silently shrinking the pool.

    Mirror the sidecar's startup hydration: read the latest backend_health row per
    backend and, for any left ``unhealthy``, seed a _backend_states entry with the
    exclusion clock armed to NOW. Pass 3b then re-probes it once it ages past the
    recovery window — exactly as if this process had benched it itself. Non-fatal:
    any DB error degrades to "no hydration" (the prior behaviour).
    """
    try:
        with db_conn.cursor() as cur:
            cur.execute(BACKEND_HEALTH_QUERY, (f"{EXCLUSION_HYDRATION_WINDOW_SECONDS} seconds",))
            rows = cur.fetchall()
    except Exception as exc:                                # noqa: BLE001
        print(f"[{SERVICE_NAME}] exclusion-clock hydration query failed ({exc}); "
              "proceeding with empty state", flush=True)
        return 0

    now_mono = time.monotonic()
    seeded = 0
    for row in rows:
        try:
            backend_id, status, _score, _ts = row
        except ValueError:
            continue
        if status != "unhealthy" or backend_id in NON_BACKEND_INSTANCES:
            continue
        st = _backend_states.setdefault(backend_id, BackendState())
        if st.excluded_since_monotonic is None:
            st.excluded_since_monotonic = now_mono
            st.last_status = "unhealthy"
            st.recovery_reinclude_emitted = False
            seeded += 1
    if seeded:
        print(f"[{SERVICE_NAME}] hydrated {seeded} exclusion clock(s) from backend_health; "
              f"stuck-down backends will be re-probed after {RECOVERY_WINDOW_SECONDS}s", flush=True)
    return seeded


def _run_loop(stop_event: threading.Event | None = None) -> None:
    """Anomaly-detector daemon thread.

    #163 invariant: a single iteration's unexpected exception must NOT kill
    the loop. The outer try/except catches every Exception, logs it, sleeps
    for `LOOP_RECOVERY_BACKOFF_SECONDS`, and continues. The thread can only
    exit via the stop_event path.
    """
    print(f"[{SERVICE_NAME}] run loop starting "
          f"(engine={_engine_name} ready={_engine_ready} "
          f"interval={POLL_INTERVAL_SECONDS}s)", flush=True)

    redis_client = redis_lib.from_url(REDIS_URL)
    pubsub = redis_client.pubsub()
    pubsub.subscribe(POLICY_CHANNEL)

    db_conn = psycopg2.connect(TIMESCALEDB_URL)
    db_conn.autocommit = True

    # Re-arm exclusion clocks for backends left `down` by a prior process so the
    # silent-backend recovery can free them (no-recovery-across-restart deadlock).
    _hydrate_exclusion_clocks(db_conn)

    next_tick = time.monotonic()

    while True:
        if stop_event is not None and stop_event.is_set():
            break

        try:
            # Drain any policy messages that arrived since the last tick —
            # never block longer than 1 s so we don't drift the poll cadence.
            message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message is not None and message.get("type") == "message":
                channel = message.get("channel")
                if isinstance(channel, bytes):
                    channel = channel.decode()
                if channel == POLICY_CHANNEL:
                    _handle_policy_message(message["data"])

            now = time.monotonic()
            if now >= next_tick:
                with METRICS.time_cycle() as _c:
                    published = _inference_cycle(db_conn, redis_client)
                    _c["outcome"] = "published" if published else "idle"
                if published:
                    print(f"[{SERVICE_NAME}] published {published} anomaly events", flush=True)
                next_tick = now + POLL_INTERVAL_SECONDS
        except Exception as exc:                            # noqa: BLE001
            # #163: an unexpected exception in the iteration body must not
            # kill the daemon thread. Log + back off + continue.
            print(f"[{SERVICE_NAME}] loop iteration raised {type(exc).__name__}: "
                  f"{exc}; continuing after {LOOP_RECOVERY_BACKOFF_SECONDS}s "
                  f"backoff", flush=True)
            if stop_event is not None and stop_event.wait(timeout=LOOP_RECOVERY_BACKOFF_SECONDS):
                break
            elif stop_event is None:
                time.sleep(LOOP_RECOVERY_BACKOFF_SECONDS)


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/metrics")
def metrics():
    body, content_type = metrics_response()
    return Response(body, mimetype=content_type)


@app.route("/health")
def health():
    redis_ok, redis_err = check_redis()
    db_ok, db_err = check_timescaledb()
    errors = [e for e in [redis_err, db_err] if e]
    status = "ok" if (redis_ok and db_ok) else "degraded"
    code = 200 if status == "ok" else 503

    body: dict = {
        "status": status,
        "service": SERVICE_NAME,
        "redis": redis_ok,
        "timescaledb": db_ok,
    }
    if RUNLOOP_ENABLED:
        with _state_lock:
            last = _last_inference_monotonic
            body["engine_type"]     = _engine_name
            body["engine_ready"]    = _engine_ready
            body["engine_requested"] = _engine_requested
        last_age = None if last is None else round(time.monotonic() - last, 2)
        body["last_inference_age_seconds"] = last_age
        # #163 liveness check.
        if last_age is not None and last_age > LIVENESS_STALE_SECONDS:
            status = "degraded"
            code = 503
            body["status"] = status
            body["loop_stale"] = True
            errors.append(
                f"run loop has not ticked in {last_age:.0f}s "
                f"(threshold {LIVENESS_STALE_SECONDS:.0f}s)"
            )
    if errors:
        body["errors"] = errors
    return jsonify(body), code


@app.route("/api/v1/engine/state", methods=["GET"])
def get_engine_state():
    """Live Engines (#121) — engine bootstrap, policy snapshot, runloop stats,
    last cycle output. Read by the operator-ui BFF for per-engine cards on the
    Live Engines page. Always returns 200; runloop-disabled is a state, not an
    error."""
    with _state_lock:
        body = serialize_engine_state(
            service=SERVICE_NAME,
            channel=ANOMALY_CHANNEL,
            runloop_enabled=RUNLOOP_ENABLED,
            engine_name=_engine_name,
            engine_requested=_engine_requested,
            engine_ready=_engine_ready,
            engine_error=_engine_error,
            policy=_policy,
            ticks_total=_ticks_total,
            publishes_total=_publishes_total,
            last_tick_at=_last_tick_at_iso,
            last_publish_at=_last_publish_at_iso,
            last_tick_monotonic=_last_inference_monotonic,
            last_output=_last_output_payload,
        )
    return jsonify(body)


@app.route("/api/v1/anomaly/history", methods=["GET"])
def get_anomaly_history():
    """Recent per-backend health verdicts for the operator-UI history view.

    Query params:
      ?window=N  — how far back to read, in seconds (default 3600, cap 86400).
      ?backend=  — optional backend_id filter (e.g. "backend_1"); omit for all.
      ?limit=N   — maximum rows to return, newest first (default 500, cap 5000).

    Returns the verdict rows plus the distinct set of backend_ids present in
    the window (so the UI can build a backend filter without a second call).
    Any DB failure collapses to an empty result with HTTP 200 — operator-UI
    must degrade gracefully rather than render an error state for a history
    view."""
    try:
        window = int(request.args.get("window", 3600))
    except (TypeError, ValueError):
        window = 3600
    if window <= 0:
        window = 3600
    window = min(window, 86400)

    try:
        limit = int(request.args.get("limit", 500))
    except (TypeError, ValueError):
        limit = 500
    if limit <= 0:
        limit = 500
    limit = min(limit, 5000)

    backend = request.args.get("backend", type=str) or None
    interval = f"{window} seconds"
    empty = {"history": [], "backends": [], "window_seconds": window}

    try:
        conn = psycopg2.connect(TIMESCALEDB_URL, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                # backend bound twice: the NULL-guard predicate and the
                # equality both reference it so one statement serves the
                # all-backends and single-backend cases (SOT §11).
                cur.execute(ANOMALY_HISTORY_QUERY, (interval, backend, backend, limit))
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as exc:                                # noqa: BLE001
        app.logger.warning("[%s] anomaly history query failed: %s", SERVICE_NAME, exc)
        return jsonify(empty), 200

    history = [
        {
            "time":       r[0].isoformat() if r[0] else None,
            "backend_id": r[1],
            "status":     r[2],
            "score":      r[3],
        }
        for r in rows
    ]
    # Distinct backend_ids in the window, stable-ordered by first appearance.
    backends: list[str] = []
    for h in history:
        bid = h["backend_id"]
        if bid is not None and bid not in backends:
            backends.append(bid)

    return jsonify({
        "history":        history,
        "backends":       backends,
        "window_seconds": window,
    }), 200


@app.route("/")
def index():
    return jsonify({"service": SERVICE_NAME, "status": "running"})


# ── manual actions: POST /api/v1/isolate (slice #3, #123) ─────────────────────


@app.route("/api/v1/isolate", methods=["POST"])
def post_manual_isolate():
    """Manually publish an AnomalyEvent for a specific backend.

    Body:
      {
        "backend_id": "<host:port or instance label>",
        "status":     "healthy" | "degraded" | "unhealthy",
        "actor":      <string, default "operator">,
        "reason":     <string, default "manual">
      }

    Side effects:
      - Publishes a synthetic AnomalyEvent envelope on smartload.anomaly so
        the load-balancer sidecar (T2.1, when wired) reacts as if the engine
        had produced it.
      - Writes a backend_health row (the anomaly-detector is the only writer
        of backend_health per SOT §8.5 design contract).

    Bypasses the engine's run loop and the publish gate entirely — the
    operator's intent is the signal. Validation + payload composition live in
    manual.plan_manual_isolate so the dry-run sibling
    (POST /api/v1/actions/simulate) shares the exact same path.
    """
    raw = request.get_json(force=True, silent=True)
    if not isinstance(raw, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400

    actor = (raw.get("actor") or request.headers.get("X-Actor") or "operator")

    try:
        plan = plan_manual_isolate(
            backend_id=raw.get("backend_id"),
            status=raw.get("status"),
            actor=actor,
            user_reason=raw.get("reason"),
        )
    except ManualIsolateError as exc:
        return jsonify({"error": exc.message, "field": exc.field}), 400

    redis_client = redis_lib.from_url(REDIS_URL)
    try:
        event_id = publish_envelope(
            redis_client,
            channel=ANOMALY_CHANNEL,
            source=SERVICE_NAME,
            payload=plan.payload,
        )
    except Exception as exc:                            # noqa: BLE001
        return jsonify({"error": f"envelope publish failed: {exc}"}), 503

    try:
        db_conn = psycopg2.connect(TIMESCALEDB_URL, connect_timeout=5)
    except Exception as exc:                            # noqa: BLE001
        return jsonify({"error": f"backend_health write failed: {exc}"}), 503
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                BACKEND_HEALTH_INSERT,
                (datetime.now(timezone.utc), plan.backend_id, plan.status, plan.score),
            )
        db_conn.commit()
    finally:
        db_conn.close()

    print(f"[{SERVICE_NAME}] manual isolate actor={plan.actor} "
          f"backend_id={plan.backend_id} status={plan.status} "
          f"reason={plan.reason!r}", flush=True)

    return jsonify({
        "status":     "applied",
        "backend_id": plan.backend_id,
        "anomaly_status": plan.status,
        "score":      plan.score,
        "actor":      plan.actor,
        "reason":     plan.reason,
        "event_id":   event_id,
    })


# ── manual actions: POST /api/v1/actions/simulate (dry-run, #146) ─────────────


@app.route("/api/v1/actions/simulate", methods=["POST"])
def post_simulate():
    """Dry-run a manual isolate: return the synthetic AnomalyEvent envelope
    that POST /api/v1/isolate WOULD publish — WITHOUT publishing and WITHOUT
    writing a backend_health row.

    Accepts the SAME request body as POST /api/v1/isolate and runs the SAME
    validation path (`plan_manual_isolate` — backend_id non-empty, status
    enum). A failed simulate implies a failed real isolate: both return 400
    with the same `field`.

    The envelope is built with make_envelope (the same wrapper publish_envelope
    uses), so the returned shape is byte-identical to what would land on
    smartload.anomaly — full envelope (event_id, source, version, timestamp)
    plus the AnomalyEvent payload (backend_id, status, score, severity, …).
    Nothing is published; the cluster + DB are untouched.

    Body (identical to /isolate):
      {
        "backend_id": "<host:port or instance label>",
        "status":     "healthy" | "degraded" | "unhealthy",
        "actor":      <string, default "operator">,
        "reason":     <string, default "manual">
      }

    Returns:
      {
        "would_publish": true,
        "channel":       "smartload.anomaly",
        "envelope": {
          "event_id":  <uuid>,
          "source":    "anomaly-detector",
          "version":   <int>,
          "timestamp": <rfc3339>,
          "payload": {
            "backend_id": <str>, "status": <str>, "score": <float>,
            "severity": <str>, "model_version": <str>, "features": {...}
          }
        },
        "backend_id": <str>,
        "status":     <str>,
        "severity":   <str>,
        "reason":     "manual:<actor>: <reason>"
      }
    """
    raw = request.get_json(force=True, silent=True)
    if not isinstance(raw, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400

    actor = (raw.get("actor") or request.headers.get("X-Actor") or "operator")

    # SAME validation + payload-composition path as POST /api/v1/isolate.
    try:
        plan = plan_manual_isolate(
            backend_id=raw.get("backend_id"),
            status=raw.get("status"),
            actor=actor,
            user_reason=raw.get("reason"),
        )
    except ManualIsolateError as exc:
        return jsonify({"error": exc.message, "field": exc.field}), 400

    # Build the envelope WITHOUT publishing it — make_envelope is the exact
    # wrapper publish_envelope uses, so the synthetic envelope mirrors the
    # real one (modulo event_id/timestamp which are minted per-call).
    envelope = make_envelope(source=SERVICE_NAME, payload=plan.payload)

    return jsonify({
        "would_publish": True,
        "channel":       ANOMALY_CHANNEL,
        "envelope":      asdict(envelope),
        "backend_id":    plan.backend_id,
        "status":        plan.status,
        "severity":      plan.severity,
        "reason":        plan.reason,
    })


# ── startup ───────────────────────────────────────────────────────────────────

def _start_runloop_thread() -> None:
    boot = bootstrap_engine(ANOMALY_ENGINE, _policy)
    _set_engine_state(boot)
    if not boot.ready:
        print(f"[{SERVICE_NAME}] engine {boot.requested!r} unavailable "
              f"({boot.error}); falling back to {boot.name!r}", flush=True)

    t = threading.Thread(target=_run_loop, daemon=True, name="anomaly-runloop")
    t.start()


if __name__ == "__main__":
    if RUNLOOP_ENABLED:
        _start_runloop_thread()
    else:
        print(f"[{SERVICE_NAME}] run loop disabled "
              f"(set ANOMALY_RUNLOOP_ENABLED=true to enable)", flush=True)

    print(f"[{SERVICE_NAME}] starting on port {PORT}", flush=True)
    # Production WSGI server (Flask's app.run dev server is single-threaded);
    # single process keeps the background run loop a singleton.
    from waitress import serve
    serve(app, host="0.0.0.0", port=PORT, threads=8)
