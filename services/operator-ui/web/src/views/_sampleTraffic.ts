// ============================================================================
// Sample data -- offline fallback for the Traffic view
// ----------------------------------------------------------------------------
// Realistic, hardcoded data shaped to the api.ts response types (BackendMetrics,
// LbState, RoutingMetrics) so the Traffic view renders fully with no backend
// running. The story: traffic is spread across the pool, an unhealthy node
// (api-04) is held out of rotation with zero weight, and the rest of the pool
// has absorbed its share. The numbers mirror the approved demonstration: six
// backends api-01..06, api-04 excluded on a p95 breach, ~18.4k rpm distributed,
// 612 routing decisions per minute on the forecast-aware strategy.
// ============================================================================

import type { BackendMetrics, LbState, RoutingMetrics } from "../api";

// Active load-balancing strategy the demonstration runs on. Surfaced as the
// "active strategy" pill; mirrors SAMPLE_POLICY.strategy_name.
export const TRAFFIC_ACTIVE_STRATEGY = "forecast-aware";

// Per-backend request distribution. api-04 is held out (near-zero rpm because
// the balancer stopped sending it traffic); the other five carry its share.
export const TRAFFIC_SAMPLE_BACKENDS: BackendMetrics = {
  backends: [
    { instance: "api-01", p95_ms: 118, rpm: 4120, error_rate_pct: 0.18, samples: 612 },
    { instance: "api-02", p95_ms: 131, rpm: 3980, error_rate_pct: 0.22, samples: 598 },
    { instance: "api-03", p95_ms: 149, rpm: 3760, error_rate_pct: 0.31, samples: 571 },
    { instance: "api-04", p95_ms: 842, rpm: 140, error_rate_pct: 7.4, samples: 96 },
    { instance: "api-05", p95_ms: 163, rpm: 3340, error_rate_pct: 0.44, samples: 503 },
    { instance: "api-06", p95_ms: 127, rpm: 3080, error_rate_pct: 0.19, samples: 467 },
  ],
  aggregate: { p95_ms: 142, rpm: 18420, error_rate_pct: 0.27, samples: 3000 },
  window_seconds: 60,
};

// Live upstream weights + the excluded set. api-04 carries zero weight and is in
// the excluded list; the rest share the routed load roughly in proportion to
// their headroom. Weights sum to 1.0 across the in-rotation pool.
export const TRAFFIC_SAMPLE_LB_STATE: LbState = {
  upstream_weights: {
    "api-01": 0.23,
    "api-02": 0.22,
    "api-03": 0.2,
    "api-04": 0.0,
    "api-05": 0.18,
    "api-06": 0.17,
  },
  excluded_backends: ["api-04"],
};

// Routing + autoscaler heartbeat. 612 routing decisions per minute on the
// forecast-aware strategy, a six-node pool, three scale events in the last hour.
export const TRAFFIC_SAMPLE_ROUTING: RoutingMetrics = {
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
