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
};
