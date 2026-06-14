// ============================================================================
// Sample ledger data -- offline fallback for the Ledger view
// ----------------------------------------------------------------------------
// Realistic, hardcoded audit rows shaped to the api.ts response types so the
// Ledger renders a full, time-ordered trail with no backend running. Each live
// load (counts + policy audit + scaling audit) tries the API first and falls
// back to these values on error or timeout.
//
// The story mirrors the Daylight prototype: a policy commit (slo_p95 = 200,
// max_backends 8 -> 9) by an operator, an autoscaler scale-out 5 -> 6 ahead of
// the spike, api-04 isolated on anomaly evidence, and supporting policy / scale
// events around them. Timestamps are ISO so the view can range-filter; the table
// renders them in a compact mono form.
// ============================================================================

import type { AuditCounts, AuditRow, ScalingAuditRow } from "../api";

// A small helper so the sample trail sits in a believable recent window. The
// offsets (in minutes before "now") keep the time-range filter meaningful when
// the page renders offline.
function ago(minutes: number): string {
  return new Date(Date.now() - minutes * 60_000).toISOString();
}

// ── Policy change audit (field-level old -> new with actor) ───────────────────
// One commit can touch several fields; each field is its own immutable row that
// shares a policy_version. Versions run newest-first to match audit ordering.

export const SAMPLE_AUDIT_POLICY: AuditRow[] = [
  {
    time: ago(27),
    policy_version: 42,
    field: "slo_p95_latency_ms",
    old_value: 220,
    new_value: 200,
    actor: "S. Rahman",
  },
  {
    time: ago(27),
    policy_version: 42,
    field: "max_backends",
    old_value: 8,
    new_value: 9,
    actor: "S. Rahman",
  },
  {
    time: ago(96),
    policy_version: 41,
    field: "anomaly_latency_multiplier",
    old_value: 2.5,
    new_value: 3,
    actor: "S. Rahman",
  },
  {
    time: ago(214),
    policy_version: 40,
    field: "strategy_name",
    old_value: "latency-aware",
    new_value: "ai-hybrid",
    actor: "M. Okafor",
  },
  {
    time: ago(214),
    policy_version: 40,
    field: "min_backends",
    old_value: 2,
    new_value: 3,
    actor: "M. Okafor",
  },
  {
    time: ago(1440),
    policy_version: 39,
    field: "autoscaler_cooldown_seconds",
    old_value: 180,
    new_value: 120,
    actor: "M. Okafor",
  },
];

// ── Scaling action audit (action, resulting instance_count, reason) ───────────
// instance_count is the pool size AFTER the action. Reasons carry the evidence
// that drove the decision so the trail is self-explaining.

export const SAMPLE_AUDIT_SCALING: ScalingAuditRow[] = [
  {
    time: ago(4),
    action: "scale_out",
    instance_count: 6,
    reason:
      "forecast crossed +18% RPS over the 5-min horizon at 92% confidence; pool grew ahead of the spike",
  },
  {
    time: ago(58),
    action: "scale_in",
    instance_count: 5,
    reason: "demand fell below the scale-in threshold for two consecutive cooldown windows",
  },
  {
    time: ago(132),
    action: "scale_out",
    instance_count: 6,
    reason: "p95 trended toward the SLO ceiling under rising load; added headroom",
  },
  {
    time: ago(1380),
    action: "scale_out",
    instance_count: 5,
    reason: "operator-initiated scale to target 5 ahead of a planned campaign",
  },
];

// ── Anomaly / isolation audit ─────────────────────────────────────────────────
// The isolate path is not yet exposed as a dedicated list endpoint, so these
// rows live only in the sample set today. They merge into the unified trail as
// "scaling"-kind operational events (action = isolate / restore) carrying the
// evidence reason, keeping the offline view complete. When an endpoint lands,
// swap this for a live load; the row shape already matches ScalingAuditRow.

export const SAMPLE_AUDIT_ISOLATION: ScalingAuditRow[] = [
  {
    time: ago(6),
    action: "scale_in",
    instance_count: 5,
    reason:
      "api-04 isolated by anomaly-detector: p95_latency_ms 842 vs threshold 300 (2.8x over); traffic redistributed in 1.2 s",
  },
  {
    time: ago(38),
    action: "scale_in",
    instance_count: 6,
    reason:
      "api-05 flagged degraded by anomaly-detector: error_rate_pct 0.44 approaching threshold 0.50; kept in rotation at reduced weight",
  },
];

// ── Audit counts (KPI rail) ───────────────────────────────────────────────────
// Derivable from the rows above, but the live endpoint returns its own totals,
// so keep an explicit sample that lines up with the sample trail.

export const SAMPLE_AUDIT_COUNTS: AuditCounts = {
  total_events: 12,
  policy_changes: 6,
  scaling_actions: 6,
  anomaly_events: 2,
  active_alerts: 1,
  actors_unique: 3,
  last_event_at: ago(4),
};

// ── Planned: load-balancer weight / algorithm change history ──────────────────
// A dedicated endpoint for upstream-weight and algorithm changes is planned (see
// LbState / setLbWeights in api.ts, which apply changes but do not yet expose a
// history). Until it lands the Ledger shows a clearly-labelled empty slot; this
// single sample row illustrates the intended shape so the section reads as real.

export interface LbChangeRow {
  time: string;
  kind: "lb";
  change: string; // human summary of the weight / algorithm change
  detail: string; // before -> after detail
  actor: string;
}

export const SAMPLE_LB_CHANGES: LbChangeRow[] = [
  {
    time: ago(27),
    kind: "lb",
    change: "algorithm",
    detail: "least-connections -> latency-aware (derived from ai-hybrid strategy)",
    actor: "policy-manager",
  },
];
