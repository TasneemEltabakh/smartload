// Typed wrapper around the BFF surface (/api/ui/*).
// Mirrors the Python SDK shape so the UI and the SDK speak the same language.

export interface Policy {
  operating_mode: string;
  safe_mode: boolean;
  min_backends: number;
  max_backends: number;
  slo_p95_latency_ms: number;
  anomaly_latency_multiplier: number;
  per_instance_capacity_rps: number;
  autoscaler_cooldown_seconds: number;
  policy_version: number;
  anomaly_response?: string;
  anomaly_recovery_window_seconds?: number;
  rl_exploration_rate?: number;
  rl_confidence_threshold?: number;
  [k: string]: unknown;
}

export interface PolicyUpdateResponse {
  status: "updated" | "no-op";
  policy: Policy;
  changed_fields: string[];
  policy_version: number;
  event_id: string | null;
}

export interface AuditRow {
  time: string;
  policy_version: number;
  field: string;
  old_value: unknown;
  new_value: unknown;
  actor: string;
}

export interface ScalingAuditRow {
  time: string;
  action: "scale_out" | "scale_in";
  instance_count: number;
  reason: string | null;
}

export interface ManualScaleResponse {
  status: "applied" | "noop";
  action: "scale_out" | "scale_in" | "noop";
  target_count: number;
  previous_count: number;
  final_count: number;
  steps_actuated: number;
  steps_requested: number;
  reason: string;
  event_id: string;
}

export type IsolateStatus = "healthy" | "degraded" | "unhealthy";

export interface ManualIsolateResponse {
  status: "applied";
  backend_id: string;
  anomaly_status: IsolateStatus;
  score: number;
  actor: string;
  reason: string;
  event_id: string;
}

export interface LbState {
  upstream_weights: Record<string, number>;
  excluded_backends: string[];
}

export interface LbWeightOverrideResponse {
  ok: boolean;
  applied_weights: Record<string, number>;
}

export interface ServiceHealth {
  status: "ok" | "degraded" | "unreachable" | string;
  status_code: number | null;
  redis?: boolean | null;
  timescaledb?: boolean | null;
  error?: string;
  extra?: Record<string, unknown>;
}

export interface HealthSummary {
  all_ok: boolean;
  services: Record<string, ServiceHealth>;
}

// ── Live Engines (#121) ──────────────────────────────────────────────────────

export interface EngineDescriptor {
  kind: "engine" | "policy";
  requested: string;
  loaded: string;
  ready: boolean;
  error: string | null;
}

export interface EngineStats {
  ticks_total: number;
  publishes_total: number;
  last_tick_at: string | null;
  last_publish_at: string | null;
  last_tick_age_seconds: number | null;
}

// AI services return this; the BFF wraps it with `reachable: true`.
// On failure the BFF returns `{reachable: false, error}` only.
export interface EngineStateBody {
  reachable: boolean;
  service?: string;
  channel?: string;
  runloop_enabled?: boolean;
  rl_mode_env?: string;
  engine?: EngineDescriptor;
  policy_snapshot?: Record<string, unknown>;
  stats?: EngineStats;
  last_output?: unknown;
  error?: string;
}

export interface EnvelopeMeta {
  event_id: string;
  source: string;
  version: number;
  timestamp: string;
}

export interface EngineStreamEvent {
  channel: string;
  envelope: EnvelopeMeta;
  payload: Record<string, unknown>;
}

export interface EnginesSnapshot {
  services: Record<string, EngineStateBody>;
  channels: Record<string, EngineStreamEvent[]>;
  recent: EngineStreamEvent[];
}

// ── UI redesign aggregations (#132) ──────────────────────────────────────────

export interface OpsMetrics {
  services_total: number;
  services_healthy: number;
  services_degraded: number;
  active_alerts: number;
  policy_compliance_pct: number | null;
  throughput_rpm: number | null;
  requests_total: number | null;
  last_refreshed: string;
  notes: string[];
}

export type ActivityKind = "policy" | "scaling" | "anomaly";
export type ActivitySeverity = "info" | "warn" | "bad";

export interface ActivityItem {
  kind: ActivityKind;
  time: string;
  actor: string | null;
  summary: string;
  source: string;
  severity: ActivitySeverity;
}

export interface PolicyDiffEntry {
  field: string;
  old: unknown;
  new: unknown;
}

export interface PolicyPreviewResponse {
  valid: boolean;
  errors: string[];
  changed_fields: string[];
  diff: PolicyDiffEntry[];
  warnings: string[];
}

export interface AuditCounts {
  total_events: number;
  policy_changes: number;
  scaling_actions: number;
  anomaly_events: number;
  active_alerts: number;
  actors_unique: number;
  last_event_at: string | null;
}

// ── Per-backend request stats + structured alerts ────────────────────────────

export interface BackendStat {
  instance: string;
  p95_ms: number | null;     // null until >= 10 samples in the window
  rpm: number;               // requests per minute over the window
  error_rate_pct: number;
  samples: number;
}

// `aggregate` is the GROUPING SETS grand total (no `instance`) — the
// load-balancer's view across every backend. null when no request traffic.
export interface BackendMetrics {
  backends: BackendStat[];
  aggregate: Omit<BackendStat, "instance"> | null;
  window_seconds: number;
}

export type AlertSeverity = "critical" | "warning";

export interface AlertItem {
  backend_id: string;
  status: IsolateStatus;
  severity: AlertSeverity;
  metric: string | null;
  observed_value: number | null;
  threshold: number | null;
  score: number | null;
  time: string | null;
  summary: string;
}

// ── Throughput, routing/scaling, environment, related metrics (#132 follow-up) ─

export interface ThroughputBucket {
  time: string;
  rpm: number;
}

export interface ThroughputResponse {
  buckets: ThroughputBucket[];
  current_rpm: number;
  total_requests: number;
}

export interface AutoscalerLastActuation {
  time: string | null;
  action: string | null;
  instance_count: number | null;
  reason: string | null;
}

export interface AutoscalerHeartbeat {
  decisions_total: number;
  decisions_noop: number;
  decisions_actuated: number;
  policy_version: number | null;
  status: string | null;
  redis: boolean | null;
  timescaledb: boolean | null;
  last_actuation?: AutoscalerLastActuation;
}

export interface RoutingMetrics {
  routing_decisions_per_min: number;
  scale_events_1h: number;
  cluster_size_current: number | null;
  autoscaler: AutoscalerHeartbeat | null;
}

export interface EnvironmentScope {
  active: string;
  available: string[];
}

export interface RelatedMetrics {
  slo_compliance_pct: number | null;
  p95_latency_ms: number | null;
  rps_current: number | null;
}

// ── Per-container CPU / memory (resource-collector, v1.0.7bb) ─────────────────

export interface ResourceSample {
  instance: string;
  service: string;
  cpu_percent: number | null;
  memory_used_bytes: number | null;
  memory_limit_bytes: number | null;
  memory_percent: number | null;
  time: string | null;
}

export interface ResourcesResponse {
  instances: ResourceSample[];
  window_seconds: number;
  count: number;
}

export interface ServiceResource {
  cpu_percent: number | null;     // summed across the service's instances
  memory_used_bytes: number | null;
  memory_limit_bytes: number | null;
  memory_percent: number | null;
  instances: number;
}

// Aggregate the flat per-instance list into a {serviceName: rollup} map so the
// engine cards / service-health rows can look a service up by name. Most
// SmartLoad services are a single container; test-backend has several, so CPU
// and used-memory are summed and the limit takes the max (per-container limits
// are identical in the prototype).
export function resourcesByService(
  resp: ResourcesResponse | null | undefined,
): Record<string, ServiceResource> {
  const out: Record<string, ServiceResource> = {};
  for (const s of resp?.instances ?? []) {
    const cur =
      out[s.service] ??
      { cpu_percent: null, memory_used_bytes: null, memory_limit_bytes: null, memory_percent: null, instances: 0 };
    cur.instances += 1;
    if (s.cpu_percent != null) cur.cpu_percent = (cur.cpu_percent ?? 0) + s.cpu_percent;
    if (s.memory_used_bytes != null) cur.memory_used_bytes = (cur.memory_used_bytes ?? 0) + s.memory_used_bytes;
    if (s.memory_limit_bytes != null) cur.memory_limit_bytes = Math.max(cur.memory_limit_bytes ?? 0, s.memory_limit_bytes);
    if (s.memory_percent != null) cur.memory_percent = (cur.memory_percent ?? 0) + s.memory_percent;
    out[s.service] = cur;
  }
  return out;
}

export interface ServiceBackendStat {
  p95_ms: number | null;   // worst (max) p95 across the service's instances
  rpm: number;             // summed across instances
  error_rate_pct: number;  // worst (max) across instances
  samples: number;
  instances: number;
}

// Derive a service name from a backend instance address. Instances are
// "<container>:<port>" and replicas carry a "-<n>" suffix, so
// "test-backend-1:8080" → "test-backend" and "load-balancer:80" →
// "load-balancer". Keeps the Home table able to look a service up by name.
export function serviceOfInstance(instance: string): string {
  const host = instance.split(":")[0];
  return host.replace(/-\d+$/, "");
}

// Roll the flat per-instance backend list into a {serviceName: rollup} map.
// p95 / error take the worst instance (the SLO-relevant signal); rpm sums.
export function backendsByService(
  resp: BackendMetrics | null | undefined,
): Record<string, ServiceBackendStat> {
  const out: Record<string, ServiceBackendStat> = {};
  for (const b of resp?.backends ?? []) {
    const name = serviceOfInstance(b.instance);
    const cur =
      out[name] ??
      { p95_ms: null, rpm: 0, error_rate_pct: 0, samples: 0, instances: 0 };
    cur.instances += 1;
    cur.rpm += b.rpm;
    cur.samples += b.samples;
    if (b.p95_ms != null) cur.p95_ms = Math.max(cur.p95_ms ?? 0, b.p95_ms);
    cur.error_rate_pct = Math.max(cur.error_rate_pct, b.error_rate_pct);
    out[name] = cur;
  }
  return out;
}

// Compact byte formatter — MB up to 1 GiB, then GB. Null → em-dash.
export function formatBytes(n: number | null | undefined): string {
  if (n == null) return "—";
  const mb = n / (1024 * 1024);
  if (mb < 1024) return `${mb.toFixed(mb < 10 ? 1 : 0)} MB`;
  return `${(mb / 1024).toFixed(2)} GB`;
}

async function _fetchJson<T>(input: string, init?: RequestInit): Promise<T> {
  const r = await fetch(input, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
  });
  const text = await r.text();
  let body: any = {};
  try {
    body = text ? JSON.parse(text) : {};
  } catch {
    body = { error: text };
  }
  if (!r.ok) {
    const err = new Error(body?.error || `HTTP ${r.status}`);
    (err as any).status = r.status;
    (err as any).field = body?.field;
    throw err;
  }
  return body as T;
}

export const api = {
  health: () => _fetchJson<HealthSummary>("/api/ui/health"),

  getPolicy: () => _fetchJson<Policy>("/api/ui/policy"),

  setPolicy: (patch: Partial<Policy>, actor?: string) =>
    _fetchJson<PolicyUpdateResponse>("/api/ui/policy", {
      method: "POST",
      headers: actor ? { "X-Actor": actor } : undefined,
      body: JSON.stringify(patch),
    }),

  auditPolicy: (limit = 50) =>
    _fetchJson<AuditRow[]>(`/api/ui/audit/policy?limit=${limit}`),

  auditScaling: (limit = 50) =>
    _fetchJson<ScalingAuditRow[]>(`/api/ui/audit/scaling?limit=${limit}`),

  scale: (target_count: number, actor: string, reason?: string) =>
    _fetchJson<ManualScaleResponse>("/api/ui/scale", {
      method: "POST",
      headers: { "X-Actor": actor },
      body: JSON.stringify({ target_count, actor, ...(reason ? { reason } : {}) }),
    }),

  isolate: (
    backend_id: string,
    status: IsolateStatus,
    actor: string,
    reason?: string,
  ) =>
    _fetchJson<ManualIsolateResponse>("/api/ui/isolate", {
      method: "POST",
      headers: { "X-Actor": actor },
      body: JSON.stringify({
        backend_id,
        status,
        actor,
        ...(reason ? { reason } : {}),
      }),
    }),

  getLbState: () => _fetchJson<LbState>("/api/ui/lb/state"),

  setLbWeights: (weights: Record<string, number>) =>
    _fetchJson<LbWeightOverrideResponse>("/api/ui/lb/weights", {
      method: "POST",
      body: JSON.stringify(weights),
    }),

  enginesSnapshot: () =>
    _fetchJson<EnginesSnapshot>("/api/ui/engines/snapshot"),

  // ── UI redesign aggregations (#132) ───────────────────────────────────────

  getOpsMetrics: () => _fetchJson<OpsMetrics>("/api/ui/metrics/ops"),

  getActivity: (limit = 50) =>
    _fetchJson<ActivityItem[]>(`/api/ui/activity?limit=${limit}`),

  previewPolicy: (patch: Partial<Policy>) =>
    _fetchJson<PolicyPreviewResponse>("/api/ui/policy/preview", {
      method: "POST",
      body: JSON.stringify({ patch }),
    }),

  getAuditCounts: () => _fetchJson<AuditCounts>("/api/ui/audit/counts"),

  // ── Throughput, routing/scaling, environment, related metrics (#132 f/u) ──

  getThroughput: (buckets?: number) =>
    _fetchJson<ThroughputResponse>(
      `/api/ui/metrics/throughput${buckets ? `?buckets=${buckets}` : ""}`,
    ),

  getRoutingMetrics: () =>
    _fetchJson<RoutingMetrics>("/api/ui/metrics/routing"),

  getEnvironmentScope: () =>
    _fetchJson<EnvironmentScope>("/api/ui/policy/environment"),

  getRelatedMetrics: () =>
    _fetchJson<RelatedMetrics>("/api/ui/policy/related-metrics"),

  getResources: (windowSeconds?: number) =>
    _fetchJson<ResourcesResponse>(
      `/api/ui/metrics/resources${windowSeconds ? `?window=${windowSeconds}` : ""}`,
    ),

  getBackendMetrics: (windowSeconds?: number) =>
    _fetchJson<BackendMetrics>(
      `/api/ui/metrics/backends${windowSeconds ? `?window=${windowSeconds}` : ""}`,
    ),

  getAlerts: (windowSeconds?: number) =>
    _fetchJson<AlertItem[]>(
      `/api/ui/alerts${windowSeconds ? `?window=${windowSeconds}` : ""}`,
    ),
};

// SSE stream URL — opened by the LiveEngines page with new EventSource(...).
// Kept out of `api` because EventSource has its own lifecycle (open/close,
// auto-reconnect) and isn't a one-shot fetch.
export const ENGINES_STREAM_URL = "/api/ui/engines/stream";
