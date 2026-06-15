#!/usr/bin/env bash
# experiments/adaptive-advantage/run.sh
# ─────────────────────────────────────
# Harder SmartLoad-vs-NGINX-RR comparison. For each side (baseline RR / full
# SmartLoad) it drives the 5-phase load (locust/locustfile.py) past the queue
# knee and through a spike, injecting backend anomalies ORGANICALLY (via
# /_admin/delay only — NO manual /isolate hint, so SmartLoad's detector must
# find them itself). Captures locust CSV per side and prints a comparison.
#
# Knobs (env): SIDES, RUNS, SEED_BASE, STEADY_USERS, SPIKE_USERS, RAMP_SECS,
#   B_END_SECS, C_END_SECS, D_END_SECS, END_SECS, SEVERE_MS, MODERATE_MS, SHORT.
set -uo pipefail

SIDES="${SIDES:-baseline smartload}"
RUNS="${RUNS:-1}"
SEED_BASE="${SEED_BASE:-1337}"
STEADY_USERS="${STEADY_USERS:-90}"
SPIKE_USERS="${SPIKE_USERS:-180}"
RAMP_SECS="${RAMP_SECS:-60}"
B_END_SECS="${B_END_SECS:-180}"
C_END_SECS="${C_END_SECS:-240}"
D_END_SECS="${D_END_SECS:-360}"
END_SECS="${END_SECS:-420}"
SEVERE_MS="${SEVERE_MS:-1500}"     # backend-1: drives it past QUEUE_MAX -> 503 shed
MODERATE_MS="${MODERATE_MS:-300}"  # backend-2: slow-but-not-failing -> latency channel

if [[ "${SHORT:-0}" == "1" ]]; then
    STEADY_USERS=80; SPIKE_USERS=140
    RAMP_SECS=15; B_END_SECS=45; C_END_SECS=70; D_END_SECS=100; END_SECS=120
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_ROOT="$HERE/results/$TS"; mkdir -p "$RUN_ROOT"

echo "[run] adaptive-advantage  ts=$TS  sides=[$SIDES]  runs=$RUNS"
echo "[run] load: steady=${STEADY_USERS}u spike=${SPIKE_USERS}u  shape A<$RAMP_SECS B<$B_END_SECS C<$C_END_SECS D<$D_END_SECS E<$END_SECS"
echo "[run] anomalies: backend-1 +${SEVERE_MS}ms (B_degrade, organic 503) ; backend-2 +${MODERATE_MS}ms (D_slow, organic latency)"

# instance ceiling for the smartload side: MAX_BACKENDS=10 lets it scale out under
# the spike (full-system comparison); MAX_BACKENDS=5 caps it to the baseline budget
# (equal-capacity, isolates pure routing intelligence).
MAX_BACKENDS="${MAX_BACKENDS:-10}"
curl -fsS -X POST http://localhost:8086/api/v1/policy -H 'Content-Type: application/json' \
     -H 'X-Actor: adaptive-adv' -d "{\"max_backends\":${MAX_BACKENDS}}" >/dev/null 2>&1 \
     && echo "[run] policy max_backends=${MAX_BACKENDS}" || echo "[run] WARN could not set max_backends"

_wait_for_status() {
    local deadline=$(( $(date +%s) + 90 ))
    while (( $(date +%s) < deadline )); do
        local o
        o=$(curl -fsS http://localhost:8090/api/v1/status 2>/dev/null \
            | python3 -c 'import sys,json;print(json.load(sys.stdin).get("overall","?"))' 2>/dev/null || echo unreachable)
        [[ "$o" == "ok" || "$o" == "degraded" ]] && { echo "[run] status=$o — ready"; return 0; }
        echo "[run] waiting (status=$o)..."; sleep 2
    done
    echo "[run] WARN status never came up; proceeding"
}

_delay() {  # _delay <N> <ms>
    docker exec "smartload-test-backend-$1" sh -c \
      "wget -q -O /dev/null --post-data='{\"ms\": $2}' --header='Content-Type: application/json' http://localhost:8080/_admin/delay" 2>/dev/null || true
}
_reset_delays() { for n in 1 2 3 4 5; do _delay "$n" 0; done; }

_schedule_anomalies() {  # organic anomaly timeline; CALL IN BACKGROUND: `_schedule_anomalies x &`
    local label="$1"
    local s1=$(( RAMP_SECS + 10 ))
    sleep "$s1";                            echo "[anomaly:$label] t=${s1}s backend-1 +${SEVERE_MS}ms (severe/503)" >&2; _delay 1 "$SEVERE_MS"
    sleep "$(( B_END_SECS - s1 ))";         echo "[anomaly:$label] t=${B_END_SECS}s backend-1 recover" >&2;            _delay 1 0
    sleep "$(( C_END_SECS - B_END_SECS ))"; echo "[anomaly:$label] t=${C_END_SECS}s backend-2 +${MODERATE_MS}ms (slow)" >&2; _delay 2 "$MODERATE_MS"
    sleep "$(( D_END_SECS - C_END_SECS ))"; echo "[anomaly:$label] t=${D_END_SECS}s backend-2 recover" >&2;            _delay 2 0
}

_run_side() {
    local side="$1" run_idx="$2" seed="$3"
    local env_file="$HERE/env/$side.env"
    local out="$RUN_ROOT/run-$(printf '%02d' "$run_idx")/$side"; mkdir -p "$out"
    echo; echo "──────── run-$run_idx  side=$side  seed=$seed ────────"
    docker compose stop traffic-simulator >/dev/null 2>&1 || true
    echo "[run] applying $side env-file + recreating decision plane..."
    ( cd "$REPO_ROOT" && docker compose --env-file "$env_file" up -d --force-recreate \
        anomaly-detector forecasting rl-engine lb-sidecar >> "$out/run.log" 2>&1 ) \
        || { echo "[run] ERROR recreate failed (see $out/run.log)"; return 1; }
    _wait_for_status
    _reset_delays                       # clean backend state each side
    sleep 3
    curl -fsS "http://localhost:8090/api/v1/status" > "$out/pre_status.json" 2>/dev/null || true

    _schedule_anomalies "$side" &
    local apid=$!
    echo "[run] launching locust ($side) ${END_SECS}s ... (anomalies scheduled, pid=$apid)"
    ( cd "$REPO_ROOT" && docker run --rm --network smartload_smartload-net \
        -e BENCH_SEED="$seed" -e STEADY_USERS="$STEADY_USERS" -e SPIKE_USERS="$SPIKE_USERS" \
        -e RAMP_SECS="$RAMP_SECS" -e B_END_SECS="$B_END_SECS" -e C_END_SECS="$C_END_SECS" \
        -e D_END_SECS="$D_END_SECS" -e END_SECS="$END_SECS" \
        -v "$HERE/locust:/locust:ro" -v "$out:/out" python:3.11-slim \
        sh -c "pip install --quiet locust && locust -f /locust/locustfile.py \
            --host=http://load-balancer --headless --users $SPIKE_USERS \
            --spawn-rate $SPIKE_USERS --run-time ${END_SECS}s \
            --csv /out/locust --csv-full-history --html /out/locust_report.html \
            --logfile /out/locust.log --only-summary" >> "$out/run.log" 2>&1 ) \
        || echo "[run] locust exit nonzero (failures expected on baseline)"

    kill "$apid" >/dev/null 2>&1 || true
    _reset_delays
    curl -fsS "http://localhost:8090/api/v1/status" > "$out/post_status.json" 2>/dev/null || true
    curl -fsS "http://localhost:8085/api/v1/audit/scaling?limit=200" > "$out/scaling_audit.json" 2>/dev/null || true
    echo "[run] side=$side complete -> $out"
}

for k in $(seq 1 "$RUNS"); do
    echo; echo "################  RUN $k / $RUNS  (seed=$(( SEED_BASE + k - 1 )))  ################"
    for side in $SIDES; do _run_side "$side" "$k" "$(( SEED_BASE + k - 1 ))"; done
done

_reset_delays
docker compose start traffic-simulator >/dev/null 2>&1 || true
echo; echo "[run] batch complete -> $RUN_ROOT"
python3 "$HERE/compare.py" "$RUN_ROOT" 2>/dev/null || echo "[run] (run compare.py manually: python3 $HERE/compare.py $RUN_ROOT)"
