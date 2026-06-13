#!/usr/bin/env bash
# experiments/baseline-vs-smartload/scripts/run_experiment.sh
# Runs the SmartLoad vs NGINX RR benchmark end-to-end.
#
# Sequence (per side):
#   1. apply env-file (baseline.env / smartload.env)
#   2. recreate the decision-plane services so the new env takes effect
#   3. wait for /api/v1/status overall=ok|degraded (not down)
#   4. start a fresh locust headless run, recording the CSV to results/<side>/
#   5. at t=ANOMALY_AT_SECS, slow backend-1 via the manual /isolate endpoint
#      (in SmartLoad mode the sidecar should route around it; in baseline
#      mode the LB has no signal and will keep sending traffic to it)
#   6. let the run finish, snapshot prometheus counters, tear down
#
# Outputs:
#   results/<timestamp>/<side>/{locust_stats.csv,locust_stats_history.csv,
#                                locust_failures.csv,locust_exceptions.csv,
#                                phase_marks.txt,prom_snapshot.txt,run.log}
#   results/<timestamp>/MANIFEST.json — knobs + git SHA for repro
#
# Tuning (env vars, all optional):
#   SIDES               which sides to run (default "baseline smartload")
#   RUNS                independently-seeded repeats per side (default 5; §35.3/#160)
#   SEED_BASE           base RNG seed; run k uses SEED_BASE + (k-1) (default 1337)
#   RAMP_USERS          per locustfile.py (default 50)
#   RAMP_SECS           per locustfile.py (default 60)
#   ANOMALY_AT_SECS     per locustfile.py (default 120)
#   ANOMALY_HOLD_SECS   per locustfile.py (default 60)
#   SUSTAIN_END_SECS    per locustfile.py (default 360 — ~6 min per side)
#   SHORT               if set to "1", overrides the four duration knobs to
#                       a 3-minute total run (smoke / harness validation).
#
# A multi-run batch lands per-run folders under results/<timestamp>/run-NN/<side>/
# and is aggregated to per-metric mean ± confidence interval by
#   python scripts/aggregate_runs.py results/<timestamp>

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
EXPERIMENT_ROOT="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$(cd "$EXPERIMENT_ROOT/../.." && pwd)"

# ── knobs ───────────────────────────────────────────────────────────────────

SIDES="${SIDES:-baseline smartload}"
RUNS="${RUNS:-5}"
SEED_BASE="${SEED_BASE:-1337}"
RAMP_USERS="${RAMP_USERS:-50}"
RAMP_SECS="${RAMP_SECS:-60}"
ANOMALY_AT_SECS="${ANOMALY_AT_SECS:-120}"
ANOMALY_HOLD_SECS="${ANOMALY_HOLD_SECS:-60}"
SUSTAIN_END_SECS="${SUSTAIN_END_SECS:-360}"

if [[ "${SHORT:-0}" == "1" ]]; then
    # Harness validation profile — total run = 180 s. Keeps the phase
    # structure but compressed so the operator can iterate on the script.
    RAMP_SECS=30
    ANOMALY_AT_SECS=60
    ANOMALY_HOLD_SECS=30
    SUSTAIN_END_SECS=120
fi

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_ROOT="$EXPERIMENT_ROOT/results/$TIMESTAMP"
mkdir -p "$RUN_ROOT"

# ── manifest (knobs + git SHA for reproducibility) ──────────────────────────

GIT_SHA="$(cd "$REPO_ROOT" && git rev-parse HEAD 2>/dev/null || echo unknown)"
GIT_DIRTY="$(cd "$REPO_ROOT" && [ -n "$(git status --porcelain 2>/dev/null)" ] && echo dirty || echo clean)"

cat > "$RUN_ROOT/MANIFEST.json" <<EOF
{
  "timestamp_utc": "$TIMESTAMP",
  "git_sha": "$GIT_SHA",
  "git_state": "$GIT_DIRTY",
  "sides": "$SIDES",
  "knobs": {
    "RUNS": $RUNS,
    "SEED_BASE": $SEED_BASE,
    "RAMP_USERS": $RAMP_USERS,
    "RAMP_SECS": $RAMP_SECS,
    "ANOMALY_AT_SECS": $ANOMALY_AT_SECS,
    "ANOMALY_HOLD_SECS": $ANOMALY_HOLD_SECS,
    "SUSTAIN_END_SECS": $SUSTAIN_END_SECS,
    "SHORT": "${SHORT:-0}"
  }
}
EOF

echo "[run] timestamp=$TIMESTAMP  sides=$SIDES  git=$GIT_SHA ($GIT_DIRTY)"
echo "[run] results: $RUN_ROOT"

# ── helpers ─────────────────────────────────────────────────────────────────

_wait_for_status_not_down() {
    local deadline=$(($(date +%s) + 60))
    while [[ $(date +%s) -lt $deadline ]]; do
        overall=$(curl -fsS http://localhost:8090/api/v1/status 2>/dev/null \
            | python -c "import sys,json; print(json.load(sys.stdin).get('overall','?'))" \
            2>/dev/null || echo "unreachable")
        if [[ "$overall" == "ok" || "$overall" == "degraded" ]]; then
            echo "[run] /api/v1/status overall=$overall — ready"
            return 0
        fi
        echo "[run] waiting (overall=$overall)..."
        sleep 2
    done
    echo "[run] WARN — /api/v1/status never came up; proceeding anyway"
    return 0
}

_set_backend_delay_via_exec() {
    # POST /_admin/delay {ms: N} on backend-1 from inside its own container
    # (the test-backend port isn't published on the host). Used for both the
    # baseline-heterogeneity setup (a constant per-side slowness) and the
    # mid-run anomaly spike.
    local target_ms="$1"
    docker exec smartload-test-backend-1 sh -c \
        "wget -q -O /dev/null --post-data='{\"ms\": ${target_ms}}' --header='Content-Type: application/json' http://localhost:8080/_admin/delay" \
        2>/dev/null || true
}

_inject_anomaly_at() {
    # Schedule a latency-spike anomaly. At t=anomaly_at:
    #   1. POST /_admin/delay {ms: ANOMALY_DELAY_MS} on backend-1 via
    #      `docker exec` (we hit the in-container endpoint, not host:8080,
    #      since backend-1 isn't published). The request still completes —
    #      it's slow, not failed. This is the case where NGINX's passive
    #      max_fails check NEVER trips (no 5xx, no timeout on the LB side),
    #      so baseline RR keeps sending 1/5 of traffic to a slow backend
    #      indefinitely. SmartLoad's lb-sidecar, in contrast, reacts to
    #      the published AnomalyEvent within ~1 s and pulls the bad
    #      backend out of rotation.
    #   2. POST /api/v1/isolate to publish the AnomalyEvent that SmartLoad's
    #      signal flow needs (the anomaly-detector hasn't observed enough
    #      latency yet to fire on its own this early in the run).
    #
    # At t+hold: clear the runtime delay (back to the static baseline
    # latency from SLOW_HOSTNAME/SLOW_DELAY_MS) and publish the recovery
    # event so SmartLoad re-enables routing to the backend.
    local at_secs="$1"
    local hold_secs="$2"
    local label="$3"
    local anomaly_delay="${ANOMALY_DELAY_MS:-200}"
    (
        sleep "$at_secs"
        echo "[anomaly] t=${at_secs}s injecting backend-1 +${anomaly_delay}ms latency ($label)" >&2
        docker exec smartload-test-backend-1 sh -c \
            "wget -q -O - --post-data='{\"ms\": ${anomaly_delay}}' --header='Content-Type: application/json' http://localhost:8080/_admin/delay" \
            > /dev/null 2>&1 || true
        curl -fsS -X POST http://localhost:8082/api/v1/isolate \
            -H 'Content-Type: application/json' \
            -H "X-Actor: bench-$label" \
            -d '{"backend_id":"smartload-test-backend-1","status":"unhealthy","reason":"benchmark latency-spike anomaly"}' \
            > /dev/null 2>&1 || true
        sleep "$hold_secs"
        echo "[anomaly] t=$((at_secs + hold_secs))s recovering backend-1 ($label)" >&2
        docker exec smartload-test-backend-1 sh -c \
            "wget -q -O - --post-data='{\"ms\": 0}' --header='Content-Type: application/json' http://localhost:8080/_admin/delay" \
            > /dev/null 2>&1 || true
        curl -fsS -X POST http://localhost:8082/api/v1/isolate \
            -H 'Content-Type: application/json' \
            -H "X-Actor: bench-$label" \
            -d '{"backend_id":"smartload-test-backend-1","status":"healthy","reason":"benchmark anomaly recovery"}' \
            > /dev/null 2>&1 || true
    ) &
    echo $!
}

_run_side() {
    local side="$1"
    local run_idx="$2"
    local seed="$3"
    local env_file="$EXPERIMENT_ROOT/env/$side.env"
    local run_label
    run_label="run-$(printf '%02d' "$run_idx")"
    local out="$RUN_ROOT/$run_label/$side"
    mkdir -p "$out"

    if [[ ! -f "$env_file" ]]; then
        echo "[run] ERROR — env file missing: $env_file" >&2
        exit 1
    fi

    echo
    echo "============================================================"
    echo "[run] === $run_label  side=$side  seed=$seed  out=$out ==="
    echo "============================================================"

    # Stop the standing traffic-simulator so it doesn't pollute results.
    docker compose stop traffic-simulator >/dev/null 2>&1 || true

    # Apply the env-file by recreating the decision-plane + sidecar containers.
    # The load-balancer container itself doesn't depend on these env vars, so
    # we don't bounce it.
    echo "[run] applying $side env-file + recreating decision plane..."
    (
        cd "$REPO_ROOT" && \
        docker compose --env-file "$env_file" up -d --force-recreate \
            anomaly-detector forecasting rl-engine lb-sidecar >> "$out/run.log" 2>&1
    ) || {
        echo "[run] ERROR — compose recreate failed; see $out/run.log"
        exit 1
    }

    _wait_for_status_not_down

    # Backend heterogeneity setup: backend-1 gets a constant baseline
    # slowness of BASELINE_SLOW_MS (default 15 ms) for the full run.
    # In baseline (NGINX RR) mode this means 1/5 of traffic eats the
    # extra latency on every request — a deterministic p95/p99 drag.
    # In SmartLoad mode the RL engine + lb-sidecar can in principle
    # downweight the slow backend; whether the *currently trained*
    # PPO model actually does so depends on training data (see SOT
    # §22 v1.0.7i — Alibaba traces had homogeneous latencies so the
    # model may not have learned strong latency discrimination — Rghda
    # is retraining on a heterogeneous dataset as a separate workstream).
    # The lb-sidecar's reaction to the mid-run AnomalyEvent is the
    # cleaner SmartLoad signal regardless of PPO discrimination.
    echo "[run] setting backend-1 baseline slowness to ${BASELINE_SLOW_MS:-15} ms ($side)"
    _set_backend_delay_via_exec "${BASELINE_SLOW_MS:-15}"

    # Pre-run prometheus + status snapshot.
    curl -fsS http://localhost:8090/api/v1/status > "$out/pre_status.json" 2>/dev/null || true
    curl -fsS http://localhost:9090/api/v1/query?query=up > "$out/pre_prom.json" 2>/dev/null || true

    # Schedule the anomaly injection.
    anomaly_pid=$(_inject_anomaly_at "$ANOMALY_AT_SECS" "$ANOMALY_HOLD_SECS" "$side")

    # Run locust headless. host is hard-coded in the locustfile; the LB
    # listens on :8080 on the host, but the locust container talks to the
    # internal hostname http://load-balancer (port 80) since it runs inside
    # the smartload-net network.
    #
    # MSYS_NO_PATHCONV=1 disables Git Bash's path translation so the volume-
    # mount source paths reach docker unchanged. Without this, /g/smartload/...
    # gets mangled into a Windows path docker can't resolve.
    echo "[run] starting locust headless ($side) — total runtime ${SUSTAIN_END_SECS}s"
    (
        cd "$REPO_ROOT" && \
        MSYS_NO_PATHCONV=1 docker run --rm \
            --network smartload_smartload-net \
            -e BENCH_SEED="$seed" \
            -e RAMP_USERS="$RAMP_USERS" \
            -e RAMP_SECS="$RAMP_SECS" \
            -e ANOMALY_AT_SECS="$ANOMALY_AT_SECS" \
            -e ANOMALY_HOLD_SECS="$ANOMALY_HOLD_SECS" \
            -e SUSTAIN_END_SECS="$SUSTAIN_END_SECS" \
            -v "$EXPERIMENT_ROOT/locust:/locust:ro" \
            -v "$out:/out" \
            python:3.11-slim \
            sh -c "pip install --quiet locust && locust \
                -f /locust/locustfile.py \
                --host=http://load-balancer \
                --headless \
                --users $RAMP_USERS \
                --spawn-rate $RAMP_USERS \
                --run-time ${SUSTAIN_END_SECS}s \
                --csv /out/locust \
                --csv-full-history \
                --html /out/locust_report.html \
                --logfile /out/locust.log \
                --loglevel INFO" >> "$out/run.log" 2>&1
    ) || {
        echo "[run] WARN — locust container exited non-zero (it may still have produced CSVs)"
    }

    # Wait for the anomaly background process to finish.
    wait "$anomaly_pid" 2>/dev/null || true

    # Tear down the per-side backend-1 slowness so the next side starts
    # from the same on-disk baseline and the dev stack (if anyone leaves
    # the harness mid-run) doesn't keep a stuck slow backend.
    echo "[run] clearing backend-1 runtime delay ($side)"
    _set_backend_delay_via_exec 0

    # Post-run snapshots.
    curl -fsS http://localhost:8090/api/v1/status > "$out/post_status.json" 2>/dev/null || true
    curl -fsS http://localhost:9090/api/v1/query?query=up > "$out/post_prom.json" 2>/dev/null || true
    curl -fsS "http://localhost:8085/api/v1/audit/scaling?limit=50" > "$out/scaling_audit.json" 2>/dev/null || true

    echo "[run] side=$side complete; outputs in $out"
}

# ── main loop ───────────────────────────────────────────────────────────────
# Outer loop over runs, inner over sides. Each run is independently seeded so
# the per-metric confidence interval the aggregator reports reflects genuine
# run-to-run variance (§35.3 / #160).

for run_idx in $(seq 1 "$RUNS"); do
    seed=$((SEED_BASE + run_idx - 1))
    echo
    echo "################  RUN $run_idx / $RUNS  (seed=$seed)  ################"
    for side in $SIDES; do
        _run_side "$side" "$run_idx" "$seed"
    done
done

# Restart the standing traffic-simulator so the dev stack returns to the
# usual background-load steady state.
echo
echo "[run] restoring standing traffic-simulator..."
(cd "$REPO_ROOT" && docker compose start traffic-simulator >/dev/null 2>&1 || true)

echo
echo "[run] aggregating $RUNS run(s) -> summary.parquet + SUMMARY.md + error-band plots..."
( cd "$REPO_ROOT" && python "$EXPERIMENT_ROOT/scripts/aggregate_runs.py" "$RUN_ROOT" ) \
    || echo "[run] WARN — aggregation failed; re-run: python $EXPERIMENT_ROOT/scripts/aggregate_runs.py $RUN_ROOT"

echo
echo "[run] DONE — results: $RUN_ROOT  ($RUNS run(s) × {$SIDES})"
