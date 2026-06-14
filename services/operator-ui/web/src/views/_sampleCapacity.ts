// ============================================================================
// Sample data -- offline fallback for the Capacity view
// ----------------------------------------------------------------------------
// Realistic, hardcoded data shaped to the api.ts response types (RoutingMetrics
// with the autoscaler heartbeat, ScalingAuditRow[], ForecastSummary with the
// scale-ahead marker) so the Capacity view renders fully with no backend
// running. The story: the autoscaler stepped the pool from five to six nodes
// ahead of a forecast spike, p95 held flat through the step, and the recent
// audit shows the decisions with their reasons and cooldown. Numbers mirror the
// approved demonstration: min 3 / max 9, 120 s cooldown, scaled 5 -> 6 at 92%
// confidence, forecast leading actual toward ~366 rps.
// ============================================================================

import type {
  ForecastSummary,
  RoutingMetrics,
  ScalingAuditRow,
} from "../api";

// Policy guard-rails the demonstration assumes; mirror SAMPLE_POLICY.
export const CAPACITY_MIN_BACKENDS = 3;
export const CAPACITY_MAX_BACKENDS = 9;
export const CAPACITY_COOLDOWN_SECONDS = 120;
export const CAPACITY_PER_INSTANCE_RPS = 120;

// Cluster size over the last hour in 5-min steps (most-recent last). The pool
// sat at five, stepped to six ahead of the spike, and is holding at six.
export const CAPACITY_CLUSTER_SERIES = [4, 4, 4, 5, 5, 5, 5, 5, 5, 6, 6, 6, 6];
export const CAPACITY_CLUSTER_X_LABELS = [
  "-60", "-55", "-50", "-45", "-40", "-35", "-30", "-25", "-20", "-15", "-10", "-5", "now",
];
// Target the controller tracked over the same window -- it crossed six before
// the actual pool stepped, which is the "scaled ahead" tell.
export const CAPACITY_TARGET_SERIES = [4, 4, 5, 5, 5, 5, 5, 5, 6, 6, 6, 6, 6];

// Routing + autoscaler heartbeat. The heartbeat is the actor's pulse: total
// decisions, how many were no-ops vs actuated, and the last actuation.
export const CAPACITY_SAMPLE_ROUTING: RoutingMetrics = {
  routing_decisions_per_min: 612,
  scale_events_1h: 3,
  cluster_size_current: 6,
  autoscaler: {
    decisions_total: 184,
    decisions_noop: 151,
    decisions_actuated: 33,
    policy_version: 42,
    status: "ok",
    redis: true,
    timescaledb: true,
    last_actuation: {
      time: "14:28:41",
      action: "scale_out",
      instance_count: 6,
      reason: "forecast crossed +18% RPS over the 5-min horizon at 92% confidence; pool grew ahead of the spike",
    },
  },
};

// Recent scaling audit (most-recent first), each with the reason behind it.
export const CAPACITY_SAMPLE_AUDIT: ScalingAuditRow[] = [
  {
    time: "14:28:41",
    action: "scale_out",
    instance_count: 6,
    reason: "forecast crossed +18% RPS over the 5-min horizon at 92% confidence; pool grew ahead of the spike",
  },
  {
    time: "13:54:18",
    action: "scale_out",
    instance_count: 5,
    reason: "sustained utilisation 78% over two windows; added a node to restore headroom",
  },
  {
    time: "13:12:07",
    action: "scale_in",
    instance_count: 4,
    reason: "demand eased below 45% for the full cooldown; released a node to trim cost",
  },
  {
    time: "12:39:52",
    action: "scale_out",
    instance_count: 5,
    reason: "forecast crossed the headroom threshold ahead of the lunchtime ramp",
  },
  {
    time: "11:58:30",
    action: "scale_in",
    instance_count: 4,
    reason: "off-peak trough held for the cooldown; pool trimmed to the floor + 1",
  },
];

// Forecast summary with the scale-ahead marker -- the tie-in that shows the
// autoscaler acting before the demand it predicted. Aligned actual-vs-forecast
// in rps (~307 now, climbing toward ~366 in five minutes), band tightening near
// now and widening to the +10m horizon.
const _NOW = Date.now();
const _stepIso = (stepsAgo: number) => new Date(_NOW - stepsAgo * 5 * 60_000).toISOString();
const _aheadIso = (stepsAhead: number) => new Date(_NOW + stepsAhead * 5 * 60_000).toISOString();

// Actual throughput (rps) over the last hour in 5-min steps, climbing from
// ~163 rps to ~307 rps now.
const _ACTUAL_KRPM = [9.8, 10.4, 10.1, 11.2, 12.0, 12.6, 13.1, 12.8, 13.6, 14.9, 16.2, 17.1, 18.4];

export const CAPACITY_SAMPLE_FORECAST: ForecastSummary = {
  actual: _ACTUAL_KRPM.map((krpm, i) => ({
    time: _stepIso(_ACTUAL_KRPM.length - 1 - i),
    rps: Math.round((krpm * 1000) / 60),
  })),
  forecast: [
    {
      time: _stepIso(0),
      predicted_rps: Math.round((18.4 * 1000) / 60),
      confidence_lower: Math.round((18.2 * 1000) / 60),
      confidence_upper: Math.round((18.6 * 1000) / 60),
      horizon_minutes: 0,
    },
    {
      time: _aheadIso(1),
      predicted_rps: Math.round((19.3 * 1000) / 60),
      confidence_lower: Math.round((18.6 * 1000) / 60),
      confidence_upper: Math.round((20.4 * 1000) / 60),
      horizon_minutes: 5,
    },
    {
      time: _aheadIso(2),
      predicted_rps: Math.round((21.96 * 1000) / 60),
      confidence_lower: Math.round((20.5 * 1000) / 60),
      confidence_upper: Math.round((23.4 * 1000) / 60),
      horizon_minutes: 10,
    },
  ],
  scale_ahead: {
    time: _aheadIso(1),
    action: "scale_out",
    instance_count: 6,
    reason: "forecast crossed +18% RPS over the 5-min horizon; pool grew ahead of the spike",
  },
  model_name: "forecast-aware",
  model_version: "1.0.7",
  horizon_minutes: 10,
  window_seconds: 3600,
  notes: [],
};
