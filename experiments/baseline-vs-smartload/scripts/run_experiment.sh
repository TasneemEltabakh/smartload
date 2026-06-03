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
#   RAMP_USERS          per locustfile.py (default 50)
#   RAMP_SECS           per locustfile.py (default 60)
#   ANOMALY_AT_SECS     per locustfile.py (default 120)
#   ANOMALY_HOLD_SECS   per locustfile.py (default 60)
#   SUSTAIN_END_SECS    per locustfile.py (default 360 — ~6 min per side)
#   SHORT               if set to "1", overrides the four duration knobs to
#                       a 3-minute total run (smoke / harness validation).

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
EXPERIMENT_ROOT="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$(cd "$EXPERIMENT_ROOT/../.." && pwd)"

# ── knobs ───────────────────────────────────────────────────────────────────

SIDES="${SIDES:-baseline smartload}"
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

_inject_anomaly_at() {
    # Run in the background; at t=anomaly_at, freezes backend-1 with
    # `docker pause` (TCP succeeds, requests hang → time out) AND publishes
    # the synthetic AnomalyEvent via POST /api/v1/isolate so SmartLoad's
    # signal flow has something to react to. At t+hold, unpauses the
    # backend and publishes a healthy event so the run returns to baseline.
    #
    # The pause is what creates the actual failure signal — in baseline
    # mode NGINX RR keeps dispatching 1/5 of traffic to a frozen backend
    # and those requests fail. In SmartLoad mode the lb-sidecar should
    # see the AnomalyEvent and rewrite the upstream weight so the bad
    # backend stops receiving requests; failures should drop to near zero
    # within the sidecar's reaction time.
    local at_secs="$1"
    local hold_secs="$2"
    local label="$3"
    (
        sleep "$at_secs"
        echo "[anomaly] t=${at_secs}s injecting backend-1 unhealthy + paused ($label)" >&2
        docker pause smartload-test-backend-1 >/dev/null 2>&1 || true
        curl -fsS -X POST http://localhost:8082/api/v1/isolate \
            -H 'Content-Type: application/json' \
            -H "X-Actor: bench-$label" \
            -d '{"backend_id":"smartload-test-backend-1","status":"unhealthy","reason":"benchmark anomaly injection"}' \
            > /dev/null 2>&1 || true
        sleep "$hold_secs"
        echo "[anomaly] t=$((at_secs + hold_secs))s recovering backend-1 ($label)" >&2
        docker unpause smartload-test-backend-1 >/dev/null 2>&1 || true
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
    local env_file="$EXPERIMENT_ROOT/env/$side.env"
    local out="$RUN_ROOT/$side"
    mkdir -p "$out"

    if [[ ! -f "$env_file" ]]; then
        echo "[run] ERROR — env file missing: $env_file" >&2
        exit 1
    fi

    echo
    echo "============================================================"
    echo "[run] === side=$side  out=$out ==="
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

    # Post-run snapshots.
    curl -fsS http://localhost:8090/api/v1/status > "$out/post_status.json" 2>/dev/null || true
    curl -fsS http://localhost:9090/api/v1/query?query=up > "$out/post_prom.json" 2>/dev/null || true
    curl -fsS "http://localhost:8085/api/v1/audit/scaling?limit=50" > "$out/scaling_audit.json" 2>/dev/null || true

    echo "[run] side=$side complete; outputs in $out"
}

# ── main loop ───────────────────────────────────────────────────────────────

for side in $SIDES; do
    _run_side "$side"
done

# Restart the standing traffic-simulator so the dev stack returns to the
# usual background-load steady state.
echo
echo "[run] restoring standing traffic-simulator..."
(cd "$REPO_ROOT" && docker compose start traffic-simulator >/dev/null 2>&1 || true)

echo
echo "[run] DONE — results: $RUN_ROOT"
echo "[run] next: python $EXPERIMENT_ROOT/scripts/plot_results.py $RUN_ROOT"
