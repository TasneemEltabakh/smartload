// ============================================================================
// Foresight sample data -- offline fallback for the forecaster view
// ----------------------------------------------------------------------------
// Realistic, hardcoded values shaped to the api.ts response types. The Foresight
// view tries the live API first (throughput, routing, scaling audit) and falls
// back to these on error or timeout, so the page renders fully with no backend
// running. The numbers mirror the approved Daylight prototype: throughput
// climbing to ~18.4k rpm with the forecast leading to ~21.96k rpm (+5 min) at
// 92% confidence, and the pool stepping 5 -> 6 ahead of the spike.
// ============================================================================

import type {
  ForecastHistoryResponse,
  ForecastSummary,
  RoutingMetrics,
  ScalingAuditRow,
  ThroughputResponse,
  TrendsResponse,
} from "../api";

// ── Throughput history (1h, 5-min steps), in rpm ─────────────────────────────
// Same trace the Flightdeck uses, expressed at full rpm resolution so the
// forecaster can derive a forward tail from the live trend when offline.
const SAMPLE_ACTUAL_KRPM = [
  9.8, 10.4, 10.1, 11.2, 12.0, 12.6, 13.1, 12.8, 13.6, 14.9, 16.2, 17.1, 18.4,
];

export const SAMPLE_FORESIGHT_THROUGHPUT: ThroughputResponse = {
  buckets: SAMPLE_ACTUAL_KRPM.map((v, i) => ({
    time: new Date(
      Date.now() - (SAMPLE_ACTUAL_KRPM.length - 1 - i) * 5 * 60_000,
    ).toISOString(),
    rpm: Math.round(v * 1000),
  })),
  current_rpm: 18_420,
  total_requests: 4_182_004,
};

// ── Routing / autoscaler snapshot ────────────────────────────────────────────
// Surfaces the current cluster size (pool of 6) and the last actuation that the
// forecast drove, used to anchor the scaling-ahead timeline.
export const SAMPLE_FORESIGHT_ROUTING: RoutingMetrics = {
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
      reason:
        "forecast crossed +18% RPS over the 5-min horizon at 92% confidence",
    },
  },
};

// ── Scaling audit (recent scale-ahead actions) ───────────────────────────────
// Newest first, matching the api.ts auditScaling shape. Each row ties a forecast
// signal to a concrete pool change so the timeline reads forecast -> action.
export const SAMPLE_FORESIGHT_SCALING: ScalingAuditRow[] = [
  {
    time: "14:28:41",
    action: "scale_out",
    instance_count: 6,
    reason:
      "Forecast crossed +18% RPS over the 5-min horizon at 92% confidence; pool grew ahead of the spike.",
  },
  {
    time: "13:54:12",
    action: "scale_out",
    instance_count: 5,
    reason:
      "Predicted demand trending up through the SLO headroom margin; added one instance pre-emptively.",
  },
  {
    time: "12:31:55",
    action: "scale_in",
    instance_count: 4,
    reason:
      "Forecast settled below 60% of pool capacity for the cooldown window; released one instance.",
  },
  {
    time: "11:48:03",
    action: "scale_out",
    instance_count: 5,
    reason:
      "Morning ramp forecast at 88% confidence; stepped up before the actual climb.",
  },
  {
    time: "10:02:27",
    action: "scale_in",
    instance_count: 4,
    reason:
      "Overnight trough confirmed; demand forecast flat and well within capacity.",
  },
];

// ── Forecast accuracy callout ────────────────────────────────────────────────
// Sample backtest of recent +5-min predictions vs the actual that followed.
// MAPE = mean absolute percentage error over the window; sourced from sample
// until a dedicated forecast-history endpoint lands (see Foresight.tsx note).
export interface ForecastAccuracy {
  windowLabel: string;
  mapePct: number; // mean absolute percentage error, lower is better
  withinBandPct: number; // share of actuals that landed inside the 90% band
  samples: number;
  recent: Array<{ time: string; predicted: number; actual: number }>;
}

export const SAMPLE_FORESIGHT_ACCURACY: ForecastAccuracy = {
  windowLabel: "last 12 horizons",
  mapePct: 4.1,
  withinBandPct: 91.7,
  samples: 12,
  recent: [
    { time: "14:10", predicted: 16_900, actual: 16_200 },
    { time: "14:15", predicted: 17_400, actual: 17_100 },
    { time: "14:20", predicted: 18_050, actual: 18_400 },
  ],
};

// ── Forecast history (forecasting service) ────────────────────────────────────
// Sample shaped to the api.ts ForecastHistoryResponse. The forecaster view tries
// the live forecast-history endpoint first and falls back to this on error or
// timeout, so the forward forecast tail, confidence band, and accuracy callout
// still render with no backend running. Predictions trail the sample throughput
// (~18.4k rpm now) climbing toward ~22k rpm on a +5/+10-min horizon.
const _now = Date.now();
const _iso = (minsAgo: number) =>
  new Date(_now - minsAgo * 60_000).toISOString();

export const SAMPLE_FORESIGHT_FORECAST_HISTORY: ForecastHistoryResponse = {
  forecasts: [
    // Backtest tail: predictions made earlier, now overlapping with realised
    // actual throughput so the accuracy callout has predicted/actual pairs.
    { time: _iso(20), horizon_minutes: 5, predicted_rps: 268, confidence_lower: 252, confidence_upper: 284, model_name: "seasonal-naive", model_version: "1.4.0" },
    { time: _iso(15), horizon_minutes: 5, predicted_rps: 282, confidence_lower: 266, confidence_upper: 298, model_name: "seasonal-naive", model_version: "1.4.0" },
    { time: _iso(10), horizon_minutes: 5, predicted_rps: 295, confidence_lower: 279, confidence_upper: 311, model_name: "seasonal-naive", model_version: "1.4.0" },
    { time: _iso(5), horizon_minutes: 5, predicted_rps: 304, confidence_lower: 288, confidence_upper: 320, model_name: "seasonal-naive", model_version: "1.4.0" },
    // Forward tail: predictions for the next two horizons (not yet realised).
    { time: _iso(0), horizon_minutes: 5, predicted_rps: 332, confidence_lower: 312, confidence_upper: 352, model_name: "seasonal-naive", model_version: "1.4.0" },
    { time: _iso(-5), horizon_minutes: 10, predicted_rps: 366, confidence_lower: 333, confidence_upper: 399, model_name: "seasonal-naive", model_version: "1.4.0" },
  ],
  models: ["seasonal-naive"],
  window_seconds: 3600,
};

// ── Forecast summary (flagship hero chart) ────────────────────────────────────
// Representative ForecastSummary shaped to the api.ts type, so the main chart
// draws a real aligned actual-vs-forecast trace, a genuine confidence band, and
// a scale-ahead decision marker on the demonstration path, exactly as it would
// from /api/ui/metrics/forecast-summary when a backend is reachable. Actual is
// normalised to requests/sec (the chart converts to k-rpm); the trace climbs to
// ~307 rps now (~18.4k rpm) and the forecast leads to ~366 rps (~22k rpm) on the
// +10-min horizon, the band tightening at the hand-off and widening with the
// horizon. The scale-ahead marker pins the first forward step.
const _actualRps = [
  163, 173, 168, 187, 200, 210, 218, 213, 227, 248, 270, 285, 307,
];

export const SAMPLE_FORESIGHT_SUMMARY: ForecastSummary = {
  actual: _actualRps.map((rps, i) => ({
    time: new Date(
      _now - (_actualRps.length - 1 - i) * 5 * 60_000,
    ).toISOString(),
    rps,
  })),
  forecast: [
    // First forward step pins to the last actual so the lines hand off cleanly.
    {
      time: _iso(0),
      predicted_rps: 307,
      confidence_lower: 307,
      confidence_upper: 307,
      horizon_minutes: 0,
    },
    {
      time: _iso(-5),
      predicted_rps: 332,
      confidence_lower: 315,
      confidence_upper: 349,
      horizon_minutes: 5,
    },
    {
      time: _iso(-10),
      predicted_rps: 366,
      confidence_lower: 333,
      confidence_upper: 399,
      horizon_minutes: 10,
    },
  ],
  scale_ahead: {
    time: "14:28:41",
    action: "scale_out",
    instance_count: 6,
    reason:
      "Forecast crossed +18% RPS over the 5-min horizon; pool grew ahead of the spike.",
  },
  model_name: "seasonal-naive",
  model_version: "1.4.0",
  horizon_minutes: 10,
  window_seconds: 3600,
  notes: [],
};

// Representative forecast confidence (%) for the demonstration path. The view
// derives a real confidence from the live band width; this is the demo fallback,
// supplied so the render never carries a hardcoded literal.
export const SAMPLE_FORESIGHT_CONFIDENCE_PCT = 92;

// ── KPI trends (sparklines for the rail) ──────────────────────────────────────
// Representative TrendsResponse so the forecaster rail draws real recent series
// rather than fabricated arrays. Only the active-backends trail is consumed here
// (the pool-size tile); the rest are populated for shape-completeness and mirror
// the throughput climb the summary shows (pool stepping 4 -> 5 -> 6 ahead of the
// spike, holding at 6 now).
export const SAMPLE_FORESIGHT_TRENDS: TrendsResponse = {
  throughput_rpm: {
    series: [13_100, 12_800, 13_600, 14_900, 16_200, 17_100, 18_400],
    current: 18_400,
    delta_pct: 7.6,
    label: "vs prior 5m",
    unit: "rpm",
  },
  p95_latency_ms: {
    series: [171, 168, 165, 162, 160, 158, 156],
    current: 156,
    delta_pct: -1.3,
    label: "vs prior 5m",
    unit: "ms",
  },
  slo_compliance_pct: {
    series: [99.5, 99.5, 99.6, 99.6, 99.7, 99.7, 99.7],
    current: 99.7,
    delta_pct: 0.1,
    label: "vs prior 5m",
    unit: "%",
  },
  error_rate_pct: {
    series: [0.55, 0.53, 0.52, 0.51, 0.5, 0.49, 0.5],
    current: 0.5,
    delta_pct: -0.9,
    label: "vs prior 5m",
    unit: "%",
  },
  active_backends: {
    series: [4, 4, 5, 5, 5, 6, 6],
    current: 6,
    delta_pct: 20,
    label: "scaled ahead",
    unit: "count",
  },
  window_minutes: 5,
  last_refreshed: new Date().toISOString(),
  notes: [],
};
