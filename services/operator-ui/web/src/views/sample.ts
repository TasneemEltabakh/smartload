// ============================================================================
// Sample data -- offline fallback for the Flightdeck
// ----------------------------------------------------------------------------
// Realistic, hardcoded data shaped to the api.ts response types. Each
// Flightdeck data load tries the live API first and falls back to these values
// on error or timeout, so the page renders fully with no backend running. The
// numbers mirror the approved Daylight prototype: six backends api-01..06,
// api-04 excluded on a p95 breach (842 > 300), ~18.4k rpm throughput, 142 ms
// p95, 99.94% SLO compliance, forecast leading actual, pool scaled 5 -> 6.
// ============================================================================

import type {
  ActivityItem,
  AlertItem,
  BackendMetrics,
  HealthSummary,
  OpsMetrics,
  Policy,
  RelatedMetrics,
  RoutingMetrics,
  ThroughputResponse,
} from "../api";

// ── Backend pool ────────────────────────────────────────────────────────────
// Six nodes; api-04 isolated on a p95 breach, api-05 degraded near its error
// threshold. health_score is a derived 0..1 reading surfaced in the table.

export interface SampleBackend {
  instance: string;
  zone: string;
  p95_ms: number;
  rpm: number;
  error_rate_pct: number;
  health_score: number;
  status: "ok" | "warn" | "crit";
  excluded: boolean;
  evidence?: { metric: string; observed: number; threshold: number };
}

export const SAMPLE_BACKENDS: SampleBackend[] = [
  { instance: "api-01", zone: "eu-west-1a", p95_ms: 118, rpm: 4120, error_rate_pct: 0.18, health_score: 0.97, status: "ok", excluded: false },
  { instance: "api-02", zone: "eu-west-1a", p95_ms: 131, rpm: 3980, error_rate_pct: 0.22, health_score: 0.95, status: "ok", excluded: false },
  { instance: "api-03", zone: "eu-west-1b", p95_ms: 149, rpm: 3760, error_rate_pct: 0.31, health_score: 0.91, status: "ok", excluded: false },
  {
    instance: "api-04",
    zone: "eu-west-1b",
    p95_ms: 842,
    rpm: 140,
    error_rate_pct: 7.4,
    health_score: 0.21,
    status: "crit",
    excluded: true,
    evidence: { metric: "p95_latency_ms", observed: 842, threshold: 300 },
  },
  {
    instance: "api-05",
    zone: "eu-west-1c",
    p95_ms: 163,
    rpm: 3340,
    error_rate_pct: 0.44,
    health_score: 0.88,
    status: "warn",
    excluded: false,
    evidence: { metric: "error_rate_pct", observed: 0.44, threshold: 0.5 },
  },
  { instance: "api-06", zone: "eu-west-1c", p95_ms: 127, rpm: 3080, error_rate_pct: 0.19, health_score: 0.96, status: "ok", excluded: false },
];

// Same pool expressed in the api.ts BackendMetrics shape (what /metrics/backends
// returns), so the live-or-sample merge can hand the table a single type.
export const SAMPLE_BACKEND_METRICS: BackendMetrics = {
  backends: SAMPLE_BACKENDS.map((b) => ({
    instance: b.instance,
    p95_ms: b.p95_ms,
    rpm: b.rpm,
    error_rate_pct: b.error_rate_pct,
    samples: 600,
  })),
  aggregate: { p95_ms: 142, rpm: 18420, error_rate_pct: 0.27, samples: 3000 },
  window_seconds: 60,
};

// ── Forecast vs actual (flagship chart) ──────────────────────────────────────
// Throughput in k-rpm over the last hour in 5-min steps; forecast leads actual
// by one step and projects 5 min ahead beyond the last actual sample.

export const SAMPLE_ACTUAL = [9.8, 10.4, 10.1, 11.2, 12.0, 12.6, 13.1, 12.8, 13.6, 14.9, 16.2, 17.1, 18.4];
export const SAMPLE_FORECAST = [18.4, 19.3, 21.96];
export const SAMPLE_CONF_LOW = [18.2, 18.6, 20.5];
export const SAMPLE_CONF_HIGH = [18.6, 20.4, 23.4];
export const SAMPLE_X_LABELS = ["-60", "-50", "-40", "-30", "-20", "-10", "now", "+5", "+10"];
export const SAMPLE_SCALE_INDEX = 1; // forecast step where the scale-ahead decision fired

// ── KPI rail ────────────────────────────────────────────────────────────────

export const SAMPLE_OPS: OpsMetrics = {
  services_total: 7,
  services_healthy: 7,
  services_degraded: 0,
  active_alerts: 2,
  policy_compliance_pct: 99.94,
  throughput_rpm: 18420,
  requests_total: 4_182_004,
  last_refreshed: new Date().toISOString(),
  notes: [],
};

export const SAMPLE_RELATED: RelatedMetrics = {
  slo_compliance_pct: 99.94,
  p95_latency_ms: 142,
  rps_current: 307,
};

export const SAMPLE_THROUGHPUT: ThroughputResponse = {
  buckets: SAMPLE_ACTUAL.map((v, i) => ({
    time: new Date(Date.now() - (SAMPLE_ACTUAL.length - 1 - i) * 5 * 60_000).toISOString(),
    rpm: Math.round(v * 1000),
  })),
  current_rpm: 18420,
  total_requests: 4_182_004,
};

export const SAMPLE_ROUTING: RoutingMetrics = {
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
      reason: "forecast crossed +18% RPS over the 5-min horizon at 92% confidence",
    },
  },
};

// Sparkline series for each KPI (most-recent last).
export const SAMPLE_SPARK = {
  throughput: [9.8, 10.4, 11.2, 12.6, 13.1, 13.6, 14.9, 16.2, 17.1, 18.4],
  p95: [151, 148, 144, 146, 140, 143, 142, 139, 141, 142],
  slo: [99.88, 99.9, 99.91, 99.92, 99.93, 99.92, 99.93, 99.94, 99.94, 99.94],
  error: [0.41, 0.38, 0.34, 0.31, 0.29, 0.3, 0.28, 0.27, 0.27, 0.27],
  backends: [5, 5, 5, 5, 5, 6, 6, 6, 6, 5],
};

// ── Anomaly verdicts ─────────────────────────────────────────────────────────

export const SAMPLE_ALERTS: AlertItem[] = [
  {
    backend_id: "api-04",
    status: "unhealthy",
    severity: "critical",
    metric: "p95_latency_ms",
    observed_value: 842,
    threshold: 300,
    score: 0.21,
    time: "14:26:09",
    summary: "p95 latency 2.8x over threshold; node isolated, traffic redistributed in 1.2 s.",
  },
  {
    backend_id: "api-05",
    status: "degraded",
    severity: "warning",
    metric: "error_rate_pct",
    observed_value: 0.44,
    threshold: 0.5,
    score: 0.88,
    time: "14:22:17",
    summary: "Error rate approaching threshold; kept in rotation with reduced weight, under watch.",
  },
];

// ── Decision stream ──────────────────────────────────────────────────────────

export const SAMPLE_ACTIVITY: ActivityItem[] = [
  {
    kind: "scaling",
    time: "14:28:41",
    actor: "autoscaler",
    summary: "Scaled out, pool 5 -> 6. Forecast crossed +18% RPS over the 5-min horizon at 92% confidence; pool grew ahead of the spike and p95 held at 142 ms.",
    source: "autoscaler",
    severity: "info",
  },
  {
    kind: "anomaly",
    time: "14:26:09",
    actor: "anomaly-detector",
    summary: "api-04 excluded, unhealthy. p95_latency_ms 842 vs threshold 300 (2.8x over). Verdict unhealthy; node isolated, traffic redistributed in 1.2 s.",
    source: "anomaly-detector",
    severity: "bad",
  },
  {
    kind: "scaling",
    time: "14:25:50",
    actor: "routing-engine",
    summary: "Routing weights re-scored in shadow. Proposed shifting +0.04 share toward api-01; scored against the live router, not applied (shadow mode).",
    source: "routing-engine",
    severity: "info",
  },
  {
    kind: "anomaly",
    time: "14:22:17",
    actor: "anomaly-detector",
    summary: "api-05 flagged, degraded. Error rate 0.44% approaching threshold 0.50%. Verdict degraded; kept in rotation with reduced weight, under watch.",
    source: "anomaly-detector",
    severity: "warn",
  },
  {
    kind: "policy",
    time: "14:05:02",
    actor: "S. Rahman",
    summary: "Policy committed: max_backends set to 9 (was 8). Diff reviewed and audit-logged.",
    source: "policy-manager",
    severity: "info",
  },
];

// ── Operating policy snapshot ─────────────────────────────────────────────────

export const SAMPLE_POLICY: Policy = {
  operating_mode: "adaptive",
  safe_mode: false,
  min_backends: 3,
  max_backends: 9,
  slo_p95_latency_ms: 200,
  anomaly_latency_multiplier: 3,
  per_instance_capacity_rps: 120,
  autoscaler_cooldown_seconds: 120,
  policy_version: 42,
  strategy_name: "ai-hybrid",
};

export const SAMPLE_HEALTH: HealthSummary = {
  all_ok: true,
  services: {
    "policy-manager": { status: "ok", status_code: 200 },
    "autoscaler": { status: "ok", status_code: 200 },
    "anomaly-detector": { status: "ok", status_code: 200 },
    "forecaster": { status: "ok", status_code: 200 },
    "routing-engine": { status: "ok", status_code: 200 },
    "load-balancer": { status: "ok", status_code: 200 },
    "bff": { status: "ok", status_code: 200 },
  },
};

// Decision-plane node count surfaced in the sidebar footer.
export const SAMPLE_PLANE_NODES = 7;

// Operator identity surfaced in the sidebar footer.
export const SAMPLE_OPERATOR = { initials: "SR", name: "S. Rahman", role: "Reliability operator" };
