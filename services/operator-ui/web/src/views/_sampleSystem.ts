// ============================================================================
// Sample data -- offline fallback for the System view
// ----------------------------------------------------------------------------
// A representative whole-system topology shaped to the api.ts SystemTopology
// type, so the System view renders the complete architecture with no backend
// running. Every one of the eleven SmartLoad services is present as a node, and
// the data-flow edges between them describe the closed loop: telemetry feeds the
// decision plane, the decision plane rules on health and demand, and the load
// balancer routes accordingly. The two headless OTLP shippers (lb-otel-shipper,
// resource-collector) report a calm "headless" status -- they are healthy infra,
// not errors. The numbers mirror the approved demonstration story: ~18.4k rpm,
// 142 ms p95, six-node pool, one node held out, forecast leading actual.
// ============================================================================

import type { SystemTopology } from "../api";

// Plane grouping used by the System view to lay nodes out in layers. Kept here
// next to the sample so the layout and the demonstration data stay in step.
export type SystemPlane =
  | "ingress"
  | "observability"
  | "decision"
  | "presentation";

export const SYSTEM_PLANE_OF: Record<string, SystemPlane> = {
  "load-balancer": "ingress",
  "lb-sidecar": "ingress",
  "lb-otel-shipper": "observability",
  "resource-collector": "observability",
  "telemetry": "observability",
  "forecasting": "decision",
  "anomaly-detector": "decision",
  "rl-engine": "decision",
  "autoscaler": "decision",
  "policy-manager": "decision",
  "operator-ui": "presentation",
};

// Human labels + ordering for each plane lane in the diagram.
export const SYSTEM_PLANE_META: Record<
  SystemPlane,
  { label: string; caption: string; order: number }
> = {
  ingress: {
    label: "Ingress & traffic",
    caption: "Where requests arrive and get routed across the pool.",
    order: 0,
  },
  observability: {
    label: "Observability pipeline",
    caption: "Headless shippers and the time-series store that feed the loop.",
    order: 1,
  },
  decision: {
    label: "Decision plane",
    caption: "Forecasts demand, rules on health, and commits the operating policy.",
    order: 2,
  },
  presentation: {
    label: "Presentation",
    caption: "This console -- the operator's window on the whole system.",
    order: 3,
  },
};

// The eleven services as topology nodes. Headless shippers carry status
// "headless" and http:false; everything else is a healthy HTTP service.
export const SAMPLE_SYSTEM_TOPOLOGY: SystemTopology = {
  nodes: [
    {
      id: "load-balancer",
      display_name: "Load balancer",
      role: "Routes every request across the backend pool on the committed weights.",
      status: "ok",
      http: true,
      last_activity: "14:31:08",
      key_metric: { label: "throughput", value: "18.4k rpm" },
    },
    {
      id: "lb-sidecar",
      display_name: "LB sidecar",
      role: "Applies live upstream weights and exclusions to the load balancer.",
      status: "ok",
      http: true,
      last_activity: "14:31:06",
      key_metric: { label: "active upstreams", value: "5 / 6" },
    },
    {
      id: "lb-otel-shipper",
      display_name: "LB OTLP shipper",
      role: "Streams the load balancer's request telemetry into the pipeline.",
      status: "headless",
      http: false,
      last_activity: "14:31:09",
      key_metric: { label: "spans / min", value: "18.4k" },
    },
    {
      id: "resource-collector",
      display_name: "Resource collector",
      role: "Ships per-container CPU and memory samples into the pipeline.",
      status: "headless",
      http: false,
      last_activity: "14:31:04",
      key_metric: { label: "containers", value: 12 },
    },
    {
      id: "telemetry",
      display_name: "Telemetry store",
      role: "Time-series store of request, latency, and resource signals.",
      status: "ok",
      http: true,
      last_activity: "14:31:09",
      key_metric: { label: "ingest", value: "21.7k pts/min" },
    },
    {
      id: "forecasting",
      display_name: "Forecasting",
      role: "Predicts demand a few minutes ahead with a confidence band.",
      status: "ok",
      http: true,
      last_activity: "14:30:55",
      key_metric: { label: "horizon", value: "+10 min" },
    },
    {
      id: "anomaly-detector",
      display_name: "Anomaly detector",
      role: "Rules on backend health and carries the evidence behind a verdict.",
      status: "ok",
      http: true,
      last_activity: "14:30:58",
      key_metric: { label: "verdicts / min", value: 6 },
    },
    {
      id: "rl-engine",
      display_name: "Routing engine",
      role: "Scores routing weights against the live router in shadow.",
      status: "ok",
      http: true,
      last_activity: "14:30:51",
      key_metric: { label: "mode", value: "shadow" },
    },
    {
      id: "autoscaler",
      display_name: "Autoscaler",
      role: "Steps the pool up ahead of demand and back down on a cooldown.",
      status: "ok",
      http: true,
      last_activity: "14:28:41",
      key_metric: { label: "pool", value: "6 nodes" },
    },
    {
      id: "policy-manager",
      display_name: "Policy manager",
      role: "Owns the operating policy and the audit-logged change history.",
      status: "ok",
      http: true,
      last_activity: "14:05:02",
      key_metric: { label: "policy", value: "v42" },
    },
    {
      id: "operator-ui",
      display_name: "Operator console",
      role: "The single pane of glass over the whole system -- this view.",
      status: "ok",
      http: true,
      last_activity: "14:31:10",
      key_metric: { label: "session", value: "live" },
    },
  ],
  edges: [
    // Ingress emits telemetry through the headless shippers.
    { source: "load-balancer", target: "lb-otel-shipper", label: "request spans" },
    { source: "lb-otel-shipper", target: "telemetry", label: "OTLP" },
    { source: "resource-collector", target: "telemetry", label: "cpu / mem" },
    { source: "lb-sidecar", target: "load-balancer", label: "weights" },
    // Telemetry feeds the decision plane.
    { source: "telemetry", target: "forecasting", label: "history" },
    { source: "telemetry", target: "anomaly-detector", label: "signals" },
    { source: "telemetry", target: "rl-engine", label: "signals" },
    // Decision plane converges on the autoscaler + policy.
    { source: "forecasting", target: "autoscaler", label: "forecast" },
    { source: "anomaly-detector", target: "policy-manager", label: "verdict" },
    { source: "rl-engine", target: "policy-manager", label: "proposal" },
    { source: "autoscaler", target: "policy-manager", label: "scale event" },
    // Policy closes the loop back onto the routing path.
    { source: "policy-manager", target: "lb-sidecar", label: "policy" },
    { source: "autoscaler", target: "load-balancer", label: "pool size" },
    { source: "anomaly-detector", target: "lb-sidecar", label: "exclusion" },
    // Everything surfaces in the console.
    { source: "policy-manager", target: "operator-ui", label: "state" },
  ],
  generated_at: new Date().toISOString(),
};

// Convenience roll-up the view shows as headline counts.
export const SAMPLE_SYSTEM_COUNTS = {
  services_total: SAMPLE_SYSTEM_TOPOLOGY.nodes.length,
  http_services: SAMPLE_SYSTEM_TOPOLOGY.nodes.filter((n) => n.http).length,
  headless_shippers: SAMPLE_SYSTEM_TOPOLOGY.nodes.filter((n) => n.status === "headless").length,
  data_flows: SAMPLE_SYSTEM_TOPOLOGY.edges.length,
};
