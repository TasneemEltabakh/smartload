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

// ── Benchmark surface (v1.0.7r harness consumer) ───────────────────────────

export interface BenchmarkManifestKnobs {
  RAMP_USERS?: number;
  RAMP_SECS?: number;
  ANOMALY_AT_SECS?: number;
  ANOMALY_HOLD_SECS?: number;
  SUSTAIN_END_SECS?: number;
  SHORT?: string;
}

export interface BenchmarkManifest {
  timestamp_utc?: string;
  git_sha?: string;
  git_state?: string;
  sides?: string;
  knobs?: BenchmarkManifestKnobs;
  parse_error?: boolean;
}

export interface BenchmarkRun {
  timestamp: string;
  manifest: BenchmarkManifest;
  plots: string[];           // canonical plot keys (rps, p50_p95_p99, ...)
  has_summary: boolean;
  sides_present: string[];   // ["baseline", "smartload"] when full
}

export interface BenchmarkRunListResponse {
  results_dir: string;
  runs: BenchmarkRun[];
  note?: string;
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

  // Benchmark surface — list runs + fetch SUMMARY.md.
  // Plot images are fetched via <img src="..."> URLs constructed by the page,
  // not through this typed wrapper (avoids loading PNGs into JS memory).
  listBenchmarkRuns: () =>
    _fetchJson<BenchmarkRunListResponse>("/api/ui/demo/benchmark/runs"),

  getBenchmarkSummary: async (timestamp: string): Promise<string> => {
    const r = await fetch(`/api/ui/demo/benchmark/runs/${encodeURIComponent(timestamp)}/summary`);
    if (!r.ok) throw new Error(`summary fetch failed: HTTP ${r.status}`);
    return r.text();
  },
};

export function benchmarkPlotUrl(timestamp: string, plotKey: string): string {
  return `/api/ui/demo/benchmark/runs/${encodeURIComponent(timestamp)}/plot/${encodeURIComponent(plotKey)}`;
}
