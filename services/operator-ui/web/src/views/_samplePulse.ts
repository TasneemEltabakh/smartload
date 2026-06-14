// ============================================================================
// Sample data -- offline fallback for the Pulse view
// ----------------------------------------------------------------------------
// Realistic, hardcoded data shaped to the api.ts response types so the Pulse
// view renders fully with no backend running. Six backends api-01..06; api-04
// is excluded on a p95 breach (842 > 300) and api-05 is degraded near its
// error threshold. A per-container resource list drives the CPU / memory panel,
// and a representative KPI-trends payload drives the headline rail (recent
// series + measured deltas). The numbers mirror the approved Daylight prototype.
// ============================================================================

import type {
  BackendMetrics,
  BackendStat,
  ResourcesResponse,
  TrendsResponse,
} from "../api";

// SLO target (p95) the offline view assumes; mirrors SAMPLE_POLICY.
export const PULSE_SLO_P95_MS = 200;
// Exclusion threshold used by the anomaly plane in the prototype.
export const PULSE_EXCLUSION_P95_MS = 300;

// Per-backend request stats in the api.ts BackendStat shape. api-04 is bleeding
// requests (mostly errored, low rpm because traffic is being held off it) and
// api-05 is near its error threshold; the rest are healthy.
const PULSE_BACKEND_STATS: BackendStat[] = [
  { instance: "api-01", p95_ms: 118, rpm: 4120, error_rate_pct: 0.18, samples: 612 },
  { instance: "api-02", p95_ms: 131, rpm: 3980, error_rate_pct: 0.22, samples: 598 },
  { instance: "api-03", p95_ms: 149, rpm: 3760, error_rate_pct: 0.31, samples: 571 },
  { instance: "api-04", p95_ms: 842, rpm: 140, error_rate_pct: 7.4, samples: 96 },
  { instance: "api-05", p95_ms: 163, rpm: 3340, error_rate_pct: 0.44, samples: 503 },
  { instance: "api-06", p95_ms: 127, rpm: 3080, error_rate_pct: 0.19, samples: 467 },
];

// Same pool expressed in the api.ts BackendMetrics shape (what /metrics/backends
// returns), so the live-or-sample path can hand the table a single type. The
// aggregate is the grand total across the routed pool.
export const PULSE_SAMPLE_BACKENDS: BackendMetrics = {
  backends: PULSE_BACKEND_STATS,
  aggregate: { p95_ms: 842, rpm: 15420, error_rate_pct: 0.51, samples: 2851 },
  window_seconds: 60,
};

// ── Per-container CPU / memory (resource-collector shape) ─────────────────────
// One sample per service container; the test-backend fleet runs several
// replicas so resourcesByService sums them. Memory limits are 1 GiB / 512 MiB
// per container in the prototype. The excluded backend's container is hot.
const MiB = 1024 * 1024;
const GiB = 1024 * MiB;

export const PULSE_SAMPLE_RESOURCES: ResourcesResponse = {
  instances: [
    { instance: "api-01:8080", service: "test-backend", cpu_percent: 38.4, memory_used_bytes: 412 * MiB, memory_limit_bytes: GiB, memory_percent: 40.2, time: null },
    { instance: "api-02:8080", service: "test-backend", cpu_percent: 41.1, memory_used_bytes: 437 * MiB, memory_limit_bytes: GiB, memory_percent: 42.7, time: null },
    { instance: "api-03:8080", service: "test-backend", cpu_percent: 44.7, memory_used_bytes: 468 * MiB, memory_limit_bytes: GiB, memory_percent: 45.7, time: null },
    { instance: "api-04:8080", service: "test-backend", cpu_percent: 91.6, memory_used_bytes: 902 * MiB, memory_limit_bytes: GiB, memory_percent: 88.1, time: null },
    { instance: "api-05:8080", service: "test-backend", cpu_percent: 52.3, memory_used_bytes: 514 * MiB, memory_limit_bytes: GiB, memory_percent: 50.2, time: null },
    { instance: "api-06:8080", service: "test-backend", cpu_percent: 36.9, memory_used_bytes: 398 * MiB, memory_limit_bytes: GiB, memory_percent: 38.9, time: null },
    { instance: "load-balancer:80", service: "load-balancer", cpu_percent: 22.8, memory_used_bytes: 96 * MiB, memory_limit_bytes: 512 * MiB, memory_percent: 18.8, time: null },
    { instance: "routing-engine:8000", service: "routing-engine", cpu_percent: 14.2, memory_used_bytes: 174 * MiB, memory_limit_bytes: 512 * MiB, memory_percent: 34.0, time: null },
    { instance: "forecaster:8000", service: "forecaster", cpu_percent: 18.7, memory_used_bytes: 221 * MiB, memory_limit_bytes: 512 * MiB, memory_percent: 43.2, time: null },
    { instance: "anomaly-detector:8000", service: "anomaly-detector", cpu_percent: 11.5, memory_used_bytes: 158 * MiB, memory_limit_bytes: 512 * MiB, memory_percent: 30.9, time: null },
    { instance: "autoscaler:8000", service: "autoscaler", cpu_percent: 6.3, memory_used_bytes: 121 * MiB, memory_limit_bytes: 512 * MiB, memory_percent: 23.6, time: null },
    { instance: "policy-manager:8000", service: "policy-manager", cpu_percent: 4.9, memory_used_bytes: 109 * MiB, memory_limit_bytes: 512 * MiB, memory_percent: 21.3, time: null },
  ],
  window_seconds: 60,
  count: 12,
};

// ── KPI trends (sparklines + window-over-window deltas) ───────────────────────
// Representative TrendsResponse so the headline KPI tiles draw a real recent
// series and a measured delta on the demonstration path, exactly as they would
// from /api/ui/metrics/trends when a backend is reachable. The series end on
// the same readings the rolled-up vitals produce (~15.4k rpm, worst p95 ~149 ms
// across the routed pool, ~0.5 % request-weighted error, 5 healthy backends),
// so the rail and the table tell one consistent, healthy story.
export const PULSE_SAMPLE_TRENDS: TrendsResponse = {
  throughput_rpm: {
    series: [14_180, 14_420, 14_760, 14_980, 15_120, 15_040, 15_260, 15_420],
    current: 15_420,
    delta_pct: 4.2,
    label: "vs prior 6m",
    unit: "rpm",
  },
  p95_latency_ms: {
    series: [156, 152, 150, 153, 149, 151, 148, 149],
    current: 149,
    delta_pct: -2.6,
    label: "vs prior 6m",
    unit: "ms",
  },
  slo_compliance_pct: {
    series: [99.4, 99.5, 99.5, 99.6, 99.6, 99.7, 99.7, 99.7],
    current: 99.7,
    delta_pct: 0.1,
    label: "vs prior 6m",
    unit: "%",
  },
  error_rate_pct: {
    series: [0.62, 0.58, 0.55, 0.53, 0.51, 0.5, 0.49, 0.5],
    current: 0.5,
    delta_pct: -1.4,
    label: "vs prior 6m",
    unit: "%",
  },
  active_backends: {
    series: [5, 5, 5, 5, 5, 5, 5, 5],
    current: 5,
    delta_pct: 0,
    label: "in rotation",
    unit: "count",
  },
  window_minutes: 6,
  last_refreshed: new Date().toISOString(),
  notes: [],
};
