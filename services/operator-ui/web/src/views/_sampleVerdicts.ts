// ============================================================================
// Sample data -- offline fallback for the Verdicts view
// ----------------------------------------------------------------------------
// Realistic, hardcoded data shaped so the Verdicts page renders fully with no
// backend running. The verdict feed and the per-backend health board mirror the
// approved Daylight prototype: six nodes api-01..06; api-04 unhealthy on a p95
// breach (842 > 400) and excluded from rotation, api-05 degraded near its error
// threshold (0.44 vs 0.50) with weight reduced, the rest clear. The shapes are
// the api.ts response types (AlertItem, ActivityItem) plus a richer board row
// the live loader synthesizes from /metrics/backends + /lb/state when online.
// ============================================================================

import type { ActivityItem, AlertItem, AnomalyHistoryRow } from "../api";

// A per-backend ruling for the health board. `evidence` carries the triggering
// metric for non-healthy nodes; `metrics` is the full per-metric breakdown the
// detail Drawer reads. Healthy nodes carry no evidence but still expose metrics.
export interface VerdictBackend {
  instance: string;
  zone: string;
  status: "healthy" | "degraded" | "unhealthy";
  score: number; // 0..1 health confidence; lower is worse
  excluded: boolean;
  action: string; // the auto-action the decision plane took
  time: string | null; // last ruling timestamp
  evidence?: { metric: string; observed: number; threshold: number };
  metrics: VerdictMetric[];
}

// One metric reading in the per-backend breakdown. `breach` flags the metric(s)
// that drove the ruling; `unit` and `direction` shape how the Drawer renders it.
export interface VerdictMetric {
  metric: string;
  observed: number;
  threshold: number;
  unit: string;
  direction: "over" | "under"; // breach when observed is over/under threshold
  breach: boolean;
}

// SLO p95 target the sample policy carries (matches SAMPLE_POLICY in sample.ts).
// The anomaly latency threshold is the SLO times the latency multiplier (200 x 2
// here = 400 ms), which is what api-04 breaches.
export const SAMPLE_VERDICT_SLO_MS = 200;
export const SAMPLE_VERDICT_LATENCY_THRESHOLD_MS = 400;
export const SAMPLE_VERDICT_ERROR_THRESHOLD_PCT = 0.5;

export const SAMPLE_VERDICT_BACKENDS: VerdictBackend[] = [
  {
    instance: "api-01",
    zone: "eu-west-1a",
    status: "healthy",
    score: 0.97,
    excluded: false,
    action: "In rotation, full weight",
    time: "14:31:08",
    metrics: [
      { metric: "p95_latency_ms", observed: 118, threshold: 400, unit: "ms", direction: "over", breach: false },
      { metric: "error_rate_pct", observed: 0.18, threshold: 0.5, unit: "%", direction: "over", breach: false },
      { metric: "req_per_min", observed: 4120, threshold: 7200, unit: "rpm", direction: "over", breach: false },
    ],
  },
  {
    instance: "api-02",
    zone: "eu-west-1a",
    status: "healthy",
    score: 0.95,
    excluded: false,
    action: "In rotation, full weight",
    time: "14:31:08",
    metrics: [
      { metric: "p95_latency_ms", observed: 131, threshold: 400, unit: "ms", direction: "over", breach: false },
      { metric: "error_rate_pct", observed: 0.22, threshold: 0.5, unit: "%", direction: "over", breach: false },
      { metric: "req_per_min", observed: 3980, threshold: 7200, unit: "rpm", direction: "over", breach: false },
    ],
  },
  {
    instance: "api-03",
    zone: "eu-west-1b",
    status: "healthy",
    score: 0.91,
    excluded: false,
    action: "In rotation, full weight",
    time: "14:31:08",
    metrics: [
      { metric: "p95_latency_ms", observed: 149, threshold: 400, unit: "ms", direction: "over", breach: false },
      { metric: "error_rate_pct", observed: 0.31, threshold: 0.5, unit: "%", direction: "over", breach: false },
      { metric: "req_per_min", observed: 3760, threshold: 7200, unit: "rpm", direction: "over", breach: false },
    ],
  },
  {
    instance: "api-04",
    zone: "eu-west-1b",
    status: "unhealthy",
    score: 0.21,
    excluded: true,
    action: "Excluded from rotation; traffic redistributed in 1.2 s",
    time: "14:26:09",
    evidence: { metric: "p95_latency_ms", observed: 842, threshold: 400 },
    metrics: [
      { metric: "p95_latency_ms", observed: 842, threshold: 400, unit: "ms", direction: "over", breach: true },
      { metric: "error_rate_pct", observed: 7.4, threshold: 0.5, unit: "%", direction: "over", breach: true },
      { metric: "req_per_min", observed: 140, threshold: 7200, unit: "rpm", direction: "over", breach: false },
    ],
  },
  {
    instance: "api-05",
    zone: "eu-west-1c",
    status: "degraded",
    score: 0.62,
    excluded: false,
    action: "Kept in rotation, weight reduced; under watch",
    time: "14:22:17",
    evidence: { metric: "error_rate_pct", observed: 0.44, threshold: 0.5 },
    metrics: [
      { metric: "p95_latency_ms", observed: 163, threshold: 400, unit: "ms", direction: "over", breach: false },
      { metric: "error_rate_pct", observed: 0.44, threshold: 0.5, unit: "%", direction: "over", breach: true },
      { metric: "req_per_min", observed: 3340, threshold: 7200, unit: "rpm", direction: "over", breach: false },
    ],
  },
  {
    instance: "api-06",
    zone: "eu-west-1c",
    status: "healthy",
    score: 0.96,
    excluded: false,
    action: "In rotation, full weight",
    time: "14:31:08",
    metrics: [
      { metric: "p95_latency_ms", observed: 127, threshold: 400, unit: "ms", direction: "over", breach: false },
      { metric: "error_rate_pct", observed: 0.19, threshold: 0.5, unit: "%", direction: "over", breach: false },
      { metric: "req_per_min", observed: 3080, threshold: 7200, unit: "rpm", direction: "over", breach: false },
    ],
  },
];

// Structured alerts in the api.ts AlertItem shape (what /alerts returns). These
// drive the chronological verdict feed alongside anomaly-kind activity.
export const SAMPLE_VERDICT_ALERTS: AlertItem[] = [
  {
    backend_id: "api-04",
    status: "unhealthy",
    severity: "critical",
    metric: "p95_latency_ms",
    observed_value: 842,
    threshold: 400,
    score: 0.21,
    time: "14:26:09",
    summary: "p95 latency 2.1x over the anomaly threshold; node ruled unhealthy and excluded from rotation, traffic redistributed in 1.2 s.",
  },
  {
    backend_id: "api-05",
    status: "degraded",
    severity: "warning",
    metric: "error_rate_pct",
    observed_value: 0.44,
    threshold: 0.5,
    score: 0.62,
    time: "14:22:17",
    summary: "Error rate approaching the threshold; node ruled degraded, kept in rotation with reduced weight and placed under watch.",
  },
];

// Anomaly-kind activity in the api.ts ActivityItem shape (what /activity
// returns). The view filters this to anomaly rulings and merges with the
// structured alerts to build one chronological feed.
export const SAMPLE_VERDICT_ACTIVITY: ActivityItem[] = [
  {
    kind: "anomaly",
    time: "14:26:09",
    actor: "anomaly-detector",
    summary: "api-04 ruled unhealthy. p95_latency_ms 842 vs threshold 400 (2.1x over). Node excluded from rotation; traffic redistributed in 1.2 s.",
    source: "anomaly-detector",
    severity: "bad",
  },
  {
    kind: "anomaly",
    time: "14:22:17",
    actor: "anomaly-detector",
    summary: "api-05 ruled degraded. error_rate_pct 0.44 approaching threshold 0.50. Kept in rotation with reduced weight, under watch.",
    source: "anomaly-detector",
    severity: "warn",
  },
  {
    kind: "anomaly",
    time: "13:58:44",
    actor: "anomaly-detector",
    summary: "api-04 ruling escalated unhealthy after three consecutive p95 breaches inside the recovery window. Auto-exclusion held.",
    source: "anomaly-detector",
    severity: "bad",
  },
  {
    kind: "anomaly",
    time: "13:41:02",
    actor: "anomaly-detector",
    summary: "api-03 cleared healthy. error_rate_pct fell back to 0.31, below the 0.50 threshold; weight restored to full.",
    source: "anomaly-detector",
    severity: "info",
  },
];

// Last-scan age in seconds for the KPI rail (sample only; live derives it from
// the freshest verdict timestamp).
export const SAMPLE_VERDICT_SCAN_AGE_SECONDS = 6;

// ── Per-backend anomaly verdict history ───────────────────────────────────────
// Sample shaped to the api.ts AnomalyHistoryRow (what /anomaly/history returns
// per backend). The detail Drawer tries the live endpoint for the selected
// backend and falls back to this so the score sparkline + status-change timeline
// still render with no backend running. Shaped as api-04's decline: healthy →
// degraded → unhealthy as its p95 climbed past the threshold.
const _vnow = Date.now();
const _viso = (minsAgo: number) =>
  new Date(_vnow - minsAgo * 60_000).toISOString();

export function sampleAnomalyHistory(backendId: string): AnomalyHistoryRow[] {
  // Newest first, matching the upstream ordering convention. Rows trace a
  // backend sliding from healthy into a breach and being held unhealthy.
  return [
    { time: _viso(0), backend_id: backendId, status: "unhealthy", score: 0.21 },
    { time: _viso(5), backend_id: backendId, status: "unhealthy", score: 0.28 },
    { time: _viso(10), backend_id: backendId, status: "degraded", score: 0.47 },
    { time: _viso(15), backend_id: backendId, status: "degraded", score: 0.58 },
    { time: _viso(20), backend_id: backendId, status: "healthy", score: 0.79 },
    { time: _viso(25), backend_id: backendId, status: "healthy", score: 0.9 },
    { time: _viso(30), backend_id: backendId, status: "healthy", score: 0.93 },
    { time: _viso(35), backend_id: backendId, status: "healthy", score: 0.95 },
  ];
}
