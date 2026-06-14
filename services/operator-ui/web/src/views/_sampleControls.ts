// ============================================================================
// Controls sample data -- offline fallbacks specific to the Controls view
// ----------------------------------------------------------------------------
// The Controls view reuses SAMPLE_POLICY / SAMPLE_BACKENDS from ./sample for the
// policy snapshot and the backend roster. These extra values cover the surfaces
// that are unique to Controls (the live load-balancer weight map and the
// available named strategies with one-line descriptions) so every section
// renders fully with no backend running.
// ============================================================================

import type { LbState, StrategyName } from "../api";
import { SAMPLE_BACKENDS } from "./sample";

// Load-balancer weight map mirroring the api.ts LbState shape. Weights sum to
// 1.0 across the in-rotation nodes; api-04 is held out (excluded) and carries
// zero share, matching the Flightdeck's isolated-node story.
export const SAMPLE_LB_STATE: LbState = {
  upstream_weights: {
    "api-01": 0.22,
    "api-02": 0.21,
    "api-03": 0.19,
    "api-04": 0.0,
    "api-05": 0.18,
    "api-06": 0.2,
  },
  excluded_backends: ["api-04"],
};

// The backend roster the action cards pick from (isolate, force-weights). Drawn
// from the shared sample pool so the offline view stays consistent with the
// Flightdeck fleet.
export const SAMPLE_BACKEND_IDS: string[] = SAMPLE_BACKENDS.map((b) => b.instance);

// One-line plain-language descriptions for each named strategy, used by the
// quick-apply selector so the operator knows what an alias does before applying.
export const STRATEGY_BLURB: Record<StrategyName, string> = {
  "round-robin": "Even rotation across every in-rotation backend. Deterministic, ignores load.",
  "least-connections": "Send each request to the node with the fewest in-flight connections.",
  "latency-aware": "Weight routing toward the backends with the lowest observed p95.",
  "forecast-aware": "Scale and route ahead of demand using the throughput forecast.",
  "anomaly-aware": "Pull weight off nodes the anomaly detector flags; redistribute fast.",
  "ai-hybrid": "Forecast plus anomaly evidence drive routing; the full adaptive plane.",
  "safe-fallback": "Deterministic round-robin on last known-good weights. Automation held.",
};

// Seed entries for the session operations strip so it reads as a real worklog
// before the operator performs anything. These are local-only and reset on
// reload; live actions performed in-session are prepended above them.
export interface OpEntry {
  id: number;
  time: string;
  kind: "policy" | "strategy" | "scale" | "isolate" | "weights" | "safe_mode";
  summary: string;
  outcome: "ok" | "failed";
  source: "live" | "local";
}

export const SAMPLE_OP_HISTORY: OpEntry[] = [
  {
    id: -3,
    time: "14:31:02",
    kind: "scale",
    summary: "Scaled out to 6 backends (was 5). Reason: forecast +18% over 5-min horizon.",
    outcome: "ok",
    source: "local",
  },
  {
    id: -2,
    time: "14:26:09",
    kind: "isolate",
    summary: "Isolated api-04 (unhealthy). p95 842 ms over 300 ms threshold.",
    outcome: "ok",
    source: "local",
  },
  {
    id: -1,
    time: "14:05:02",
    kind: "policy",
    summary: "Committed policy: max_backends 8 -> 9. Diff reviewed.",
    outcome: "ok",
    source: "local",
  },
];
