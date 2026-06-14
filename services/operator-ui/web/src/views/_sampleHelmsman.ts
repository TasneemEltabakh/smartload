// ============================================================================
// Helmsman sample data -- offline fallback for the routing-engine view
// ----------------------------------------------------------------------------
// Realistic, hardcoded data shaped to the api.ts response types so the
// Helmsman view renders fully with no backend running. The picture mirrors the
// approved Daylight prototype: six backends api-01..06, api-04 held out of
// rotation (excluded), the routing engine running in SHADOW mode -- its
// per-backend share is proposed and scored against the live router but the
// deterministic load-balancer split is what actually serves.
//
// Shapes:
//   EnginesSnapshot.services["rl-engine"]  -> EngineStateBody
//     .last_output      = { mode, server_rankings:[{backend_id,score}], policy_version }
//     .policy_snapshot  = EnginePolicy { rl_*, safe_mode, policy_version, operating_mode }
//     .rl_mode_env      = the deploy-time RL_MODE pin
//   snapshot.channels["smartload.routing"] = recent RoutingRecommendation events
//   LbState                                = { upstream_weights, excluded_backends }
// ============================================================================

import type {
  EnginesSnapshot,
  EngineStreamEvent,
  LbState,
} from "../api";

// The channel the routing engine publishes RoutingRecommendation envelopes on.
export const ROUTING_CHANNEL = "smartload.routing";

// The key the routing engine appears under in EnginesSnapshot.services.
export const RL_SERVICE = "rl-engine";

// ── RL-proposed per-backend scores ───────────────────────────────────────────
// Raw policy scores (not yet normalised to a share). api-04 is excluded, so it
// carries no score. The view normalises the eligible scores to percentages.
export interface SampleRanking {
  backend_id: string;
  score: number;
}

export const SAMPLE_RANKINGS: SampleRanking[] = [
  { backend_id: "api-01", score: 0.24 },
  { backend_id: "api-02", score: 0.23 },
  { backend_id: "api-03", score: 0.21 },
  { backend_id: "api-05", score: 0.17 },
  { backend_id: "api-06", score: 0.15 },
];

// ── Recent routing decisions on smartload.routing ─────────────────────────────
// A short replay of the last few published recommendations. A routing-decision
// history / replay endpoint is planned; until then the view samples the recent
// channel events the engines snapshot already carries.
function routingEvent(
  secondsAgo: number,
  mode: string,
  rankings: SampleRanking[],
  policyVersion: number,
): EngineStreamEvent {
  const ts = new Date(Date.now() - secondsAgo * 1000).toISOString();
  return {
    channel: ROUTING_CHANNEL,
    envelope: {
      event_id: `evt-${secondsAgo}`,
      source: RL_SERVICE,
      version: 1,
      timestamp: ts,
    },
    payload: {
      mode,
      server_rankings: rankings.map((r) => ({ backend_id: r.backend_id, score: r.score })),
      policy_version: policyVersion,
    },
  };
}

export const SAMPLE_ROUTING_EVENTS: EngineStreamEvent[] = [
  routingEvent(7, "shadow", SAMPLE_RANKINGS, 42),
  routingEvent(22, "shadow", [
    { backend_id: "api-01", score: 0.22 },
    { backend_id: "api-02", score: 0.23 },
    { backend_id: "api-03", score: 0.21 },
    { backend_id: "api-05", score: 0.18 },
    { backend_id: "api-06", score: 0.16 },
  ], 42),
  routingEvent(37, "shadow", [
    { backend_id: "api-01", score: 0.21 },
    { backend_id: "api-02", score: 0.22 },
    { backend_id: "api-03", score: 0.22 },
    { backend_id: "api-05", score: 0.19 },
    { backend_id: "api-06", score: 0.16 },
  ], 42),
  routingEvent(52, "shadow", [
    { backend_id: "api-01", score: 0.2 },
    { backend_id: "api-02", score: 0.22 },
    { backend_id: "api-03", score: 0.22 },
    { backend_id: "api-05", score: 0.2 },
    { backend_id: "api-06", score: 0.16 },
  ], 41),
];

// ── Engines snapshot (rl-engine slice) ────────────────────────────────────────
// SHADOW mode: the policy ranks backends and the recommendation is published,
// but rl_mode_env is "shadow" so the load balancer never acts on it.
export const SAMPLE_ENGINES_SNAPSHOT: EnginesSnapshot = {
  services: {
    [RL_SERVICE]: {
      reachable: true,
      service: RL_SERVICE,
      channel: ROUTING_CHANNEL,
      runloop_enabled: true,
      rl_mode_env: "shadow",
      engine: {
        kind: "policy",
        requested: "ppo",
        loaded: "ppo",
        ready: true,
        error: null,
      },
      policy_snapshot: {
        rl_confidence_threshold: 0.6,
        rl_exploration_rate: 0.08,
        safe_mode: false,
        policy_version: 42,
        operating_mode: "shadow",
      },
      stats: {
        ticks_total: 1840,
        publishes_total: 1788,
        last_tick_at: new Date(Date.now() - 7 * 1000).toISOString(),
        last_publish_at: new Date(Date.now() - 7 * 1000).toISOString(),
        last_tick_age_seconds: 7,
      },
      last_output: {
        mode: "shadow",
        server_rankings: SAMPLE_RANKINGS.map((r) => ({
          backend_id: r.backend_id,
          score: r.score,
        })),
        policy_version: 42,
      },
    },
  },
  channels: {
    [ROUTING_CHANNEL]: SAMPLE_ROUTING_EVENTS,
  },
  recent: SAMPLE_ROUTING_EVENTS,
};

// ── Load-balancer state ───────────────────────────────────────────────────────
// The deterministic split that NGINX actually serves. api-04 is excluded on an
// anomaly verdict; its weight is dropped to 0 and held out of rotation. The
// remaining weights are an even-ish committed split that the RL proposal is
// compared against.
export const SAMPLE_LB_STATE: LbState = {
  upstream_weights: {
    "api-01": 20,
    "api-02": 20,
    "api-03": 20,
    "api-04": 0,
    "api-05": 20,
    "api-06": 20,
  },
  excluded_backends: ["api-04"],
};

// The load-balancing algorithm the sidecar reports as in effect. Not part of
// LbState (the BFF surface exposes weights + exclusions only), so it is sampled
// here for the current-state panel.
export const SAMPLE_LB_ALGORITHM = "weighted-round-robin";
