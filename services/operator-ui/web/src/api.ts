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
  last_event_at: string | null;
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
};

// SSE stream URL — opened by the LiveEngines page with new EventSource(...).
// Kept out of `api` because EventSource has its own lifecycle (open/close,
// auto-reconnect) and isn't a one-shot fetch.
export const ENGINES_STREAM_URL = "/api/ui/engines/stream";
