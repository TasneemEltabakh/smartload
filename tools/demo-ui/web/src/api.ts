// Typed wrapper around the demo BFF surface (/api/ui/demo/* and /api/ui/events).

export interface BackendRanking {
  backend_id: string;
  score: number;
}

export interface DemoState {
  upstream_weights:            Record<string, number>;
  excluded_backends:           string[];
  algorithm:                   string;
  rl_mode:                     string | null;
  policy_type:                 string | null;
  policy_ready:                boolean | null;
  last_inference_age_seconds:  number | null;
  last_rankings:               BackendRanking[] | null;
  anomaly_engine:              string | null;
  safe_mode:                   boolean | null;
  backend_names:               string[];
}

export type DemoScenario =
  | "backend_failure"
  | "latency_spike"
  | "recovery"
  | "high_traffic"
  | "ai_disabled";

export type DemoAlgorithm = "round_robin" | "least_conn" | "random" | "ppo";

export interface DemoMetrics {
  window:             string;
  p95_latency_ms:     number | null;
  mean_latency_ms:    number | null;
  slo_violation_pct:  number;
  sample_count:       number;
  total_requests:     number;
}

export interface DemoStepResult {
  step:  string;
  ok:    boolean;
  error?: string;
}

export interface DemoScenarioResponse {
  ok:       boolean;
  scenario: DemoScenario;
  steps:    DemoStepResult[];
}

export interface DemoResetResponse {
  ok:    boolean;
  steps: DemoStepResult[];
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
    throw err;
  }
  return body as T;
}

export const api = {
  getDemoState: () => _fetchJson<DemoState>("/api/ui/demo/state"),

  demoDegrade: (backend_id: string, level: "degraded" | "unhealthy") =>
    _fetchJson<unknown>("/api/ui/demo/degrade", {
      method: "POST",
      body: JSON.stringify({ backend_id, level }),
    }),

  demoRecover: (backend_id: string) =>
    _fetchJson<unknown>("/api/ui/demo/recover", {
      method: "POST",
      body: JSON.stringify({ backend_id }),
    }),

  demoMode: (safe_mode: boolean) =>
    _fetchJson<unknown>("/api/ui/demo/mode", {
      method: "POST",
      body: JSON.stringify({ safe_mode }),
    }),

  demoTraffic: (users: number, spawn_rate = 10) =>
    _fetchJson<unknown>("/api/ui/demo/traffic", {
      method: "POST",
      body: JSON.stringify({ users, spawn_rate }),
    }),

  demoChaos: (backend_id: string, delay_ms: number, fail_health = false, fail_all = false) =>
    _fetchJson<unknown>("/api/ui/demo/chaos", {
      method: "POST",
      body: JSON.stringify({ backend_id, delay_ms, fail_health, fail_all }),
    }),

  demoReset: () => _fetchJson<DemoResetResponse>("/api/ui/demo/reset", { method: "POST" }),

  demoScenario: (scenario: DemoScenario) =>
    _fetchJson<DemoScenarioResponse>("/api/ui/demo/scenario", {
      method: "POST",
      body: JSON.stringify({ scenario }),
    }),

  demoAlgorithm: (algorithm: DemoAlgorithm) =>
    _fetchJson<unknown>("/api/ui/demo/algorithm", {
      method: "POST",
      body: JSON.stringify({ algorithm }),
    }),

  getDemoMetrics: (window = "5 minutes") =>
    _fetchJson<DemoMetrics>(`/api/ui/demo/metrics?window=${encodeURIComponent(window)}`),
};
