#!/usr/bin/env bash
# experiments/adaptive-advantage/ablation.sh
# ──────────────────────────────────────────
# Leave-one-out ABLATION of the SmartLoad C_spike fix stack. Runs the
# equal-capacity (5v5) adaptive-advantage scenario once with the FULL stack and
# once per fix REMOVED, so each fix's marginal contribution is measured. Produces
# a contribution table (ablation_compare.py): "removing fix X costs +Y% errors".
#
# The five toggles (each defaults to the full-stack value; one config flips one):
#   LB_SIDECAR_CLAMP_MIN_FRACTION      0.75  -> 0      (T0.1/T0.2 anti-concentration clamp)
#   ANOMALY_ABSOLUTE_OVERLOAD_SUPPRESSION true -> false (T0.3 #3 absolute-overload guard)
#   MIN_BACKENDS                       5     -> 1      (T0.6a equal-capacity pin)
#   RESET_UPSTREAM                     1     -> 0      (T0.6b per-side routing reset)
# (The detector exclusion-clock hydration + sidecar health reconciliation, T0.4/
# T0.5, are restart-correctness fixes with no runtime toggle — they stay on; the
# RESET ablation exercises the path they protect.)
#
# Each config recreates the decision plane via run.sh, which picks up the service
# toggles through docker-compose env substitution (shell env > --env-file), so NO
# per-config rebuild is needed — only ONE rebuild of lb-sidecar + anomaly-detector
# beforehand to bake in the env-reading code.
#
# Usage:
#   # one-time, before the first ablation:
#   docker compose build lb-sidecar anomaly-detector
#   # then:
#   RUNS=2 bash experiments/adaptive-advantage/ablation.sh
#   # RUNS>=2 strongly recommended for the final table (per-config variance).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNS="${RUNS:-1}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
ABL_ROOT="$HERE/results/ablation-$TS"; mkdir -p "$ABL_ROOT"
MANIFEST="$ABL_ROOT/manifest.tsv"; : > "$MANIFEST"

# Fixed equal-capacity (5v5) spike scenario — identical across all configs so the
# only thing that varies is the fix under ablation.
export STEADY_USERS="${STEADY_USERS:-70}" SPIKE_USERS="${SPIKE_USERS:-110}"
export SEVERE_MS="${SEVERE_MS:-1800}" MODERATE_MS="${MODERATE_MS:-400}"
export MAX_BACKENDS=5

echo "[ablation] ts=$TS runs=$RUNS  scenario: 5v5 steady=${STEADY_USERS} spike=${SPIKE_USERS}"
echo "[ablation] results -> $ABL_ROOT"

_run_cfg() {  # _run_cfg <label> <sides> <clamp> <guard> <pin> <reset>
    local label="$1" sides="$2" clamp="$3" guard="$4" pin="$5" reset="$6"
    echo; echo "################  ablation config: $label  ################"
    echo "[ablation] clamp=$clamp guard=$guard pin=$pin reset=$reset sides=[$sides]"
    export LB_SIDECAR_CLAMP_MIN_FRACTION="$clamp"
    export ANOMALY_ABSOLUTE_OVERLOAD_SUPPRESSION="$guard"
    local out root
    out=$( SIDES="$sides" MIN_BACKENDS="$pin" RESET_UPSTREAM="$reset" RUNS="$RUNS" \
           bash "$HERE/run.sh" 2>&1 | tee "$ABL_ROOT/$label.log" )
    root=$(printf '%s\n' "$out" | sed -n 's/.*batch complete -> //p' | tail -1)
    if [[ -z "$root" ]]; then
        echo "[ablation] WARN no RUN_ROOT parsed for $label (see $ABL_ROOT/$label.log)"
        return 0
    fi
    local s
    for s in $sides; do printf '%s\t%s\t%s\n' "$label" "$s" "$root" >> "$MANIFEST"; done
}

#         label      sides                 clamp guard  pin reset
_run_cfg  full      "baseline smartload"   0.75  true   5   1     # full stack (+ baseline reference)
_run_cfg  no-clamp  "smartload"            0     true   5   1     # remove anti-concentration clamp
_run_cfg  no-guard  "smartload"            0.75  false  5   1     # remove #3 absolute-overload guard
_run_cfg  no-pin    "smartload"            0.75  true   1   1     # let the autoscaler flap (no pin)
_run_cfg  no-reset  "smartload"            0.75  true   5   0     # no per-side routing reset

echo; echo "[ablation] batch complete -> $ABL_ROOT"
python3 "$HERE/ablation_compare.py" "$MANIFEST" | tee "$ABL_ROOT/ablation_report.md"
echo "[ablation] report -> $ABL_ROOT/ablation_report.md"
