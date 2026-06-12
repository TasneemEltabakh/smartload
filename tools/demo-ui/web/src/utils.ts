/**
 * tools/demo-ui/web/src/utils.ts
 * ───────────────────────────────
 * Helpers shared across demo-ui pages: backend-id shortening, mode/decision
 * label composition, SSE feed parsing, palette constants, recharts tooltip
 * style. Extracted from the monolithic Demo.tsx so per-page components can
 * import what they need without duplicating logic.
 */

import type { BackendRanking, DemoState } from "./api";

export const POLL_MS = 2_000;
export const METRICS_POLL_MS = 5_000;
export const SERVICES_POLL_MS = 5_000;
export const LIVESTATS_POLL_MS = 2_000;
export const FEED_MAX = 50;

export const CLR_OK = "#3fb950";
export const CLR_WARN = "#d29922";
export const CLR_BAD = "#f85149";
export const CLR_BLUE = "#4dabf7";
export const CLR_MUTED = "#7d8590";

export const TOOLTIP_STYLE = {
  background: "#161b22",
  border: "1px solid #2d333b",
  color: "#e6edf3",
  fontSize: 12,
};

export function shortName(id: string): string {
  return id.replace("smartload-test-backend-", "b").replace(":8080", "");
}

export function modeLabel(state: DemoState | null): string {
  if (!state) return "—";
  if (state.safe_mode) return "Safe Mode (Equal Weights)";
  if (state.rl_mode === "active") return "AI Active";
  return "AI Observing (Shadow)";
}

export function modeBadgeClass(state: DemoState | null): string {
  if (!state) return "health-pill";
  if (state.safe_mode) return "health-pill degraded";
  if (state.rl_mode === "active") return "health-pill ok";
  return "health-pill";
}

export function decisionBasis(
  state: DemoState | null,
  rankings: BackendRanking[] | null,
): string {
  if (!state) return "Loading…";
  if (!rankings || rankings.length === 0) return "Awaiting first inference…";
  if (state.safe_mode) return "Safe mode active (equal weights)";
  if (state.excluded_backends.length > 0) {
    return `${shortName(state.excluded_backends[0])} excluded (unhealthy)`;
  }
  const sorted = [...rankings].sort((a, b) => b.score - a.score);
  const top = sorted[0];
  const bottom = sorted[sorted.length - 1];
  if (top && bottom && top.score > 0.8 && bottom.score < 0.35) {
    return `${shortName(bottom.backend_id)} deprioritized (routing active)`;
  }
  if (top) return `RL routing active (${shortName(top.backend_id)} preferred)`;
  return "RL routing active";
}

export function topRanked(rankings: BackendRanking[] | null): string {
  if (!rankings || rankings.length === 0) return "—";
  const top = [...rankings].sort((a, b) => b.score - a.score)[0];
  return `${shortName(top.backend_id)} (score ${top.score.toFixed(2)})`;
}

export function bottomRanked(rankings: BackendRanking[] | null): string {
  if (!rankings || rankings.length === 0) return "—";
  const bottom = [...rankings].sort((a, b) => a.score - b.score)[0];
  return `${shortName(bottom.backend_id)} (score ${bottom.score.toFixed(2)})`;
}

export function barColor(score: number): string {
  return score > 0.7 ? CLR_OK : score > 0.4 ? CLR_WARN : CLR_BAD;
}

export function channelColor(channel: string): string {
  if (channel === "smartload.routing") return CLR_BLUE;
  if (channel === "smartload.anomaly") return CLR_WARN;
  if (channel === "smartload.policy") return "#a78bfa";
  if (channel === "smartload.scale") return CLR_OK;
  return CLR_MUTED;
}

export function feedSummary(channel: string, envelope: any): string {
  const p = envelope?.payload ?? {};
  if (channel === "smartload.routing") {
    const rankings = (p.server_rankings ?? p.rankings ?? []) as any[];
    const top = [...rankings].sort((a: any, b: any) => b.score - a.score)[0];
    const topStr = top ? `${shortName(top.backend_id)}=${top.score.toFixed(2)}` : "—";
    return `RL(${p.mode ?? "?"}): ${topStr}`;
  }
  if (channel === "smartload.anomaly") {
    return `Anomaly: ${shortName(p.backend_id ?? "?")} → ${p.status ?? "?"}`;
  }
  if (channel === "smartload.policy") {
    const fields = ((p.changed_fields ?? []) as string[]).join(", ") || "—";
    return `Policy v${p.policy_version ?? "?"}: ${fields}`;
  }
  if (channel === "smartload.scale") {
    const action = p.action ?? p.mechanism ?? "scale";
    const to = p.target_count ?? p.to ?? p.instance_count ?? "?";
    const reason = p.reason ? ` — ${String(p.reason).slice(0, 60)}` : "";
    return `Scale ${action} → ${to}${reason}`;
  }
  return channel;
}

export interface FeedItem {
  id: string;
  channel: string;
  ts: string;
  summary: string;
}
