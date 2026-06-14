// ============================================================================
// Helmsman -- the RL routing engine (proposed vs applied)
// ----------------------------------------------------------------------------
// The routing engine ranks the backend pool every cycle and publishes a
// per-backend share recommendation. Whether that share is *applied* depends on
// the mode:
//
//   SHADOW  the recommendation is computed, scored against the live router, and
//           published for explainability, but the load balancer keeps serving
//           on its committed deterministic split. The RL weights are PROPOSED,
//           not applied.
//   ACTIVE  the recommendation drives the load-balancer weights directly.
//
// IMPORTANT — the mode is a DEPLOY-TIME PIN, not a live toggle. The rl-engine's
// RL_MODE environment variable sets it; the policy plane deliberately rejects
// rl_mode as a writable field. So there is no in-app "promote" action: the
// effective published mode is RL_MODE plus the operator-writable policy gates
// (safe_mode, operating_mode). This view presents that honestly via a
// "Promotion readiness" panel driven by api.getRlMode() — no fake success, no
// claim that the engine beats the classical router.
//
// The real story is the comparison: this view reads the engines snapshot (mode,
// server_rankings, policy version, exploration / confidence) and the load-
// balancer state (algorithm, applied weights, excluded backends), and puts the
// RL-proposed share next to the currently-applied LB share so the gap is
// legible. Every panel resolves live-or-demo through useLiveOrDemo, so the page
// renders complete on representative data with no backend running and quietly
// upgrades to live when the decision plane is reachable.
//
// A routing-decision history / replay endpoint is planned; until then this view
// shows the current rankings plus a sample of recent decisions taken from the
// events the engines snapshot already carries on smartload.routing.
// ============================================================================

import { useMemo, type ReactNode } from "react";
import {
  Compass,
  Eye,
  GitCompareArrows,
  Layers,
  Radio,
  ShieldCheck,
  SlidersHorizontal,
} from "lucide-react";

import {
  api,
  type EngineStreamEvent,
  type EnginesSnapshot,
  type LbState,
  type RlMode,
  type RlModeStatus,
} from "../api";
import {
  Badge,
  Card,
  DataTable,
  EmptyState,
  ErrorState,
  LoadState,
  ShareBars,
  StatusPill,
  useLiveOrDemo,
  type Column,
  type ShareRow,
  type Status,
} from "../ui";
import {
  RL_SERVICE,
  ROUTING_CHANNEL,
  SAMPLE_ENGINES_SNAPSHOT,
  SAMPLE_LB_ALGORITHM,
  SAMPLE_LB_STATE,
  SAMPLE_RL_MODE,
} from "./_sampleHelmsman";

// Panel ids registered with the DataModeProvider so the global indicator
// reflects which panels resolved live vs demonstration.
const PANEL_ENGINES = "helmsman.engines";
const PANEL_LB = "helmsman.lb";
const PANEL_RL_MODE = "helmsman.rl-mode";

// ── derived-reading helpers ───────────────────────────────────────────────────

type Mode = "shadow" | "active";

interface Ranking {
  backend_id: string;
  score: number;
}

interface BackendShare {
  backend_id: string;
  // RL-proposed share, 0..100, normalised across eligible (scored) backends.
  proposedPct: number;
  // Currently-applied LB share, 0..100, normalised across the committed weights.
  appliedPct: number;
  proposedScore: number | null; // raw policy score; null when not ranked
  appliedWeight: number; // raw LB weight
  excluded: boolean;
}

// Pull the routing engine's last cycle output out of the snapshot. The engine
// appears under services["rl-engine"]; last_output carries mode, the raw
// server_rankings, and the policy_version it was reasoning under.
function readEngine(snap: EnginesSnapshot): {
  mode: Mode;
  rankings: Ranking[];
  policyVersion: number | null;
  explorationRate: number | null;
  confidenceThreshold: number | null;
  rlModeEnv: string | null;
  reachable: boolean;
} {
  const body = snap.services?.[RL_SERVICE];
  const out = (body?.last_output ?? null) as
    | { mode?: string; server_rankings?: Ranking[]; policy_version?: number }
    | null;
  const snapshot = (body?.policy_snapshot ?? {}) as Record<string, unknown>;

  const rawMode = (out?.mode ?? body?.rl_mode_env ?? "shadow").toString().toLowerCase();
  const mode: Mode = rawMode === "active" ? "active" : "shadow";

  const rankings: Ranking[] = Array.isArray(out?.server_rankings)
    ? out!.server_rankings
        .filter((r) => r && typeof r.backend_id === "string")
        .map((r) => ({ backend_id: r.backend_id, score: Number(r.score) || 0 }))
    : [];

  const explorationRate = numOrNull(snapshot.rl_exploration_rate);
  const confidenceThreshold = numOrNull(snapshot.rl_confidence_threshold);
  const policyVersion =
    numOrNull(out?.policy_version) ?? numOrNull(snapshot.policy_version);

  return {
    mode,
    rankings,
    policyVersion,
    explorationRate,
    confidenceThreshold,
    rlModeEnv: body?.rl_mode_env ?? null,
    reachable: Boolean(body?.reachable),
  };
}

function numOrNull(v: unknown): number | null {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

// Normalise a {id: weight} map into a 0..100 share, ignoring zero / excluded
// entries so the bars read as the live rotation split.
function shareFromWeights(weights: Record<string, number>): Record<string, number> {
  const total = Object.values(weights).reduce((s, w) => s + (w > 0 ? w : 0), 0);
  const out: Record<string, number> = {};
  for (const [id, w] of Object.entries(weights)) {
    out[id] = total > 0 && w > 0 ? (w / total) * 100 : 0;
  }
  return out;
}

// Build the joined per-backend view: union of every backend the engine ranked
// and every backend the load balancer knows about, with both shares normalised.
function joinShares(rankings: Ranking[], lb: LbState): BackendShare[] {
  const excluded = new Set(lb.excluded_backends ?? []);
  const scoreById = new Map(rankings.map((r) => [r.backend_id, r.score]));
  const weights = lb.upstream_weights ?? {};

  // Normalise RL scores across the backends that actually carry a score.
  const scoreTotal = rankings.reduce((s, r) => s + (r.score > 0 ? r.score : 0), 0);
  const appliedShare = shareFromWeights(weights);

  const ids = new Set<string>([
    ...rankings.map((r) => r.backend_id),
    ...Object.keys(weights),
  ]);

  const rows: BackendShare[] = [...ids].map((id) => {
    const score = scoreById.get(id) ?? null;
    const proposedPct =
      score != null && scoreTotal > 0 ? (score / scoreTotal) * 100 : 0;
    return {
      backend_id: id,
      proposedScore: score,
      proposedPct,
      appliedPct: appliedShare[id] ?? 0,
      appliedWeight: weights[id] ?? 0,
      excluded: excluded.has(id),
    };
  });

  // Sort by proposed share desc, excluded last, then by id for stability.
  rows.sort((a, b) => {
    if (a.excluded !== b.excluded) return a.excluded ? 1 : -1;
    if (b.proposedPct !== a.proposedPct) return b.proposedPct - a.proposedPct;
    return a.backend_id.localeCompare(b.backend_id);
  });
  return rows;
}

// ── component ─────────────────────────────────────────────────────────────────

export default function Helmsman() {
  // Each panel resolves independently so one slow upstream never blanks the
  // others. The demonstration fallback shows immediately, then upgrades to live.
  const enginesQ = useLiveOrDemo<EnginesSnapshot>(
    () => api.enginesSnapshot(),
    SAMPLE_ENGINES_SNAPSHOT,
    { panelId: PANEL_ENGINES },
  );
  const lbQ = useLiveOrDemo<LbState>(
    () => api.getLbState(),
    SAMPLE_LB_STATE,
    { panelId: PANEL_LB },
  );
  const rlModeQ = useLiveOrDemo<RlModeStatus>(
    () => api.getRlMode(),
    SAMPLE_RL_MODE,
    { panelId: PANEL_RL_MODE },
  );

  const snap = enginesQ.value;
  const lb = lbQ.value;

  const engine = useMemo(() => readEngine(snap), [snap]);
  const shares = useMemo(() => joinShares(engine.rankings, lb), [engine.rankings, lb]);

  // The load-balancing algorithm is not part of the LbState surface. When the
  // engines snapshot is live the engine kind is the closest live signal we have;
  // otherwise we read the sampled algorithm. Never hardcode the sample when live.
  const algorithm = useMemo(() => {
    if (enginesQ.source !== "live") return SAMPLE_LB_ALGORITHM;
    const body = snap.services?.[RL_SERVICE];
    const kind = body?.engine?.loaded ?? body?.engine?.kind ?? null;
    return kind ? `policy · ${kind}` : SAMPLE_LB_ALGORITHM;
  }, [enginesQ.source, snap]);

  const isShadow = engine.mode !== "active";

  // KPI: top backend by proposed share.
  const topBackend = shares.find((s) => !s.excluded && s.proposedPct > 0) ?? null;
  // KPI: how much of the pool the engine scored this cycle (eligible vs held).
  const rankedCount = shares.filter((s) => s.proposedScore != null).length;
  const excludedCount = shares.filter((s) => s.excluded).length;

  // Recent routing decisions sampled from the channel the snapshot carries.
  const decisions: EngineStreamEvent[] = useMemo(() => {
    const evts = snap.channels?.[ROUTING_CHANNEL] ?? snap.recent ?? [];
    return evts.slice(0, 6);
  }, [snap]);

  // The mode banner reads "live" only when the engines snapshot is actually
  // resolved from the decision plane.
  const enginesLive = enginesQ.source === "live";

  return (
    <div className="sl-stack">
      <ModeBanner
        mode={engine.mode}
        rlModeEnv={engine.rlModeEnv}
        live={enginesLive}
      />

      <KpiRail
        mode={engine.mode}
        topBackend={topBackend?.backend_id ?? "—"}
        topShare={topBackend?.proposedPct ?? 0}
        rankedCount={rankedCount}
        excludedCount={excludedCount}
        explorationRate={engine.explorationRate}
        confidenceThreshold={engine.confidenceThreshold}
        policyVersion={engine.policyVersion}
      />

      <SectionHead
        title="Promotion readiness"
        sub="The routing engine evaluates the pool in shadow and its proposals are scored against the live applied weights. Promoting to active is a deploy-time change to the engine, not an in-console toggle; the panel below shows the live mode, the recommendation, and the operator-writable policy gates that shape the effective mode."
      />

      <PromotionReadiness
        rl={rlModeQ.value}
        state={rlModeQ.state}
        source={rlModeQ.source}
        degraded={rlModeQ.degraded}
        onRetry={rlModeQ.reload}
      />

      <SectionHead
        title="Proposed vs applied routing share"
        sub={
          isShadow
            ? "Left is what the routing engine proposes this cycle. Right is the deterministic split the load balancer is actually serving. In shadow mode the proposal is observed and scored, never applied."
            : "The routing engine is active: its proposed share is driving the load-balancer weights. Left and right should track closely; any gap is the latest re-score not yet committed."
        }
      />

      <div className="sl-grid-1-1">
        <ProposedShareCard
          shares={shares}
          mode={engine.mode}
          state={enginesQ.state}
          onRetry={enginesQ.reload}
        />
        <AppliedShareCard
          shares={shares}
          algorithm={algorithm}
          excluded={lb.excluded_backends ?? []}
          state={lbQ.state}
          onRetry={lbQ.reload}
        />
      </div>

      <SectionHead
        title="Per-backend comparison"
        sub="Proposed RL share against the applied load-balancer share, side by side. The delta is how far the live split would move if the proposal were promoted to active."
      />

      <ComparisonCard shares={shares} mode={engine.mode} />

      <SectionHead
        title="Routing state and recent decisions"
        sub="The load-balancer state below is the ground truth the engine is compared against. A routing-decision replay endpoint is planned; for now the stream samples recent recommendations published on smartload.routing."
      />

      <div className="sl-grid-1-1">
        <LbStatePanel
          algorithm={algorithm}
          shares={shares}
          excluded={lb.excluded_backends ?? []}
        />
        <DecisionHistory decisions={decisions} fallbackMode={engine.mode} />
      </div>
    </div>
  );
}

// ── mode banner ───────────────────────────────────────────────────────────────

function ModeBanner({
  mode,
  rlModeEnv,
  live,
}: {
  mode: Mode;
  rlModeEnv: string | null;
  live: boolean;
}) {
  const isShadow = mode !== "active";
  const accent = isShadow ? "var(--sl-graphite)" : "var(--sl-mint)";
  const accentSoft = isShadow ? "var(--sl-surface-sunk)" : "var(--sl-mint-tint)";

  return (
    <section
      style={{
        position: "relative",
        overflow: "hidden",
        borderRadius: "var(--sl-radius-xl)",
        border: `1px solid ${isShadow ? "var(--sl-hairline)" : "var(--sl-mint-line)"}`,
        background: isShadow
          ? "linear-gradient(180deg, var(--sl-surface), var(--sl-surface))"
          : "radial-gradient(900px 380px at 88% -30%, var(--sl-mint-soft), transparent 60%), linear-gradient(180deg, var(--sl-surface), var(--sl-surface))",
        boxShadow: "var(--sl-shadow-2)",
        padding: "26px 30px",
      }}
    >
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 8,
          fontFamily: "var(--sl-font-mono)",
          fontSize: 11,
          fontWeight: 600,
          color: accent,
          background: accentSoft,
          border: `1px solid ${isShadow ? "var(--sl-hairline)" : "var(--sl-mint-line)"}`,
          borderRadius: 20,
          padding: "5px 12px",
        }}
      >
        <Compass size={13} strokeWidth={2} />
        Helmsman · RL routing engine
      </span>

      <h1
        style={{
          fontSize: 30,
          lineHeight: 1.1,
          letterSpacing: "-1px",
          fontWeight: 800,
          margin: "14px 0 0",
          color: "var(--sl-text)",
          display: "flex",
          alignItems: "center",
          gap: 12,
          flexWrap: "wrap",
        }}
      >
        Routing is
        <span style={{ color: accent }}>{isShadow ? "shadowed" : "active"}</span>
        <StatusPill status={isShadow ? "neutral" : "ok"} hideDot>
          {isShadow ? "SHADOW MODE" : "ACTIVE MODE"}
        </StatusPill>
      </h1>

      <p style={{ fontSize: 14, color: "var(--sl-text-mid)", margin: "12px 0 0", maxWidth: "70ch" }}>
        {isShadow ? (
          <>
            The engine computes a per-backend share every cycle and scores it
            against the live router, but the weights are <b>RL-proposed, not
            applied</b>. The load balancer keeps serving on its committed
            deterministic split.
          </>
        ) : (
          <>
            The engine's proposed share is driving the load-balancer weights
            directly. Every cycle re-scores the pool and the live split tracks
            the recommendation.
          </>
        )}
      </p>

      <div
        style={{
          display: "flex",
          gap: 18,
          marginTop: 18,
          flexWrap: "wrap",
          fontFamily: "var(--sl-font-mono)",
          fontSize: 11.5,
          color: "var(--sl-text-low)",
        }}
      >
        <span style={{ display: "inline-flex", alignItems: "center", gap: 7 }}>
          <Radio size={13} strokeWidth={2} />
          {live ? "live" : "demonstration"} · {ROUTING_CHANNEL}
        </span>
        {rlModeEnv ? (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 7 }}>
            RL_MODE pin = <b style={{ color: "var(--sl-text-mid)" }}>{rlModeEnv}</b>
          </span>
        ) : null}
      </div>
    </section>
  );
}

// ── KPI rail ──────────────────────────────────────────────────────────────────

function KpiRail({
  mode,
  topBackend,
  topShare,
  rankedCount,
  excludedCount,
  explorationRate,
  confidenceThreshold,
  policyVersion,
}: {
  mode: Mode;
  topBackend: string;
  topShare: number;
  rankedCount: number;
  excludedCount: number;
  explorationRate: number | null;
  confidenceThreshold: number | null;
  policyVersion: number | null;
}) {
  const isShadow = mode !== "active";
  return (
    <div className="sl-grid-kpi">
      <KpiCard
        label={<><Eye size={12} strokeWidth={2} /> Routing mode</>}
        value={isShadow ? "shadow" : "active"}
        foot={isShadow ? "proposed, not applied" : "driving LB weights"}
        tone={isShadow ? "graphite" : "mint"}
      />
      <KpiCard
        label={<><Layers size={12} strokeWidth={2} /> Top backend</>}
        value={topBackend}
        unit={topShare > 0 ? `${Math.round(topShare)}%` : undefined}
        foot="highest proposed share"
        tone="mint"
      />
      <KpiCard
        label={<><Layers size={12} strokeWidth={2} /> Backends ranked</>}
        value={`${rankedCount}`}
        foot={excludedCount > 0 ? `${excludedCount} held out of rotation` : "full pool eligible"}
        tone="graphite"
      />
      <KpiCard
        label={<><GitCompareArrows size={12} strokeWidth={2} /> Exploration</>}
        value={explorationRate != null ? `${(explorationRate * 100).toFixed(0)}` : "—"}
        unit={explorationRate != null ? "%" : undefined}
        foot={
          confidenceThreshold != null
            ? `confidence ≥ ${confidenceThreshold.toFixed(2)}`
            : "exploration rate"
        }
        tone="graphite"
      />
      <KpiCard
        label={<><ShieldCheck size={12} strokeWidth={2} /> Policy version</>}
        value={policyVersion != null ? `v${policyVersion}` : "—"}
        foot="rankings reasoning under"
        tone="graphite"
      />
    </div>
  );
}

function KpiCard({
  label,
  value,
  unit,
  foot,
  tone,
}: {
  label: ReactNode;
  value: string;
  unit?: string;
  foot: string;
  tone: "mint" | "graphite";
}) {
  return (
    <div
      style={{
        background: "var(--sl-surface)",
        border: "1px solid var(--sl-hairline)",
        borderRadius: "var(--sl-radius-lg)",
        padding: "16px 18px",
        boxShadow: "var(--sl-shadow-1)",
      }}
    >
      <div
        style={{
          fontSize: 11.5,
          color: "var(--sl-text-low)",
          fontWeight: 600,
          display: "flex",
          alignItems: "center",
          gap: 7,
        }}
      >
        {label}
      </div>
      <div
        style={{
          fontFamily: "var(--sl-font-mono)",
          fontWeight: 700,
          fontSize: 26,
          letterSpacing: "-1px",
          marginTop: 9,
          lineHeight: 1,
          color: tone === "mint" ? "var(--sl-mint-deep)" : "var(--sl-text)",
        }}
      >
        {value}
        {unit ? (
          <span style={{ fontSize: 13, color: "var(--sl-text-low)", fontWeight: 500, marginLeft: 4, letterSpacing: 0 }}>
            {unit}
          </span>
        ) : null}
      </div>
      <div style={{ fontSize: 11, color: "var(--sl-text-faint)", marginTop: 11 }}>{foot}</div>
    </div>
  );
}

// ── section header ────────────────────────────────────────────────────────────

function SectionHead({ title, sub }: { title: string; sub: string }) {
  return (
    <div style={{ margin: "8px 2px 0" }}>
      <h2 style={{ fontSize: 17, fontWeight: 700, letterSpacing: "-0.3px", margin: 0, color: "var(--sl-text)" }}>
        {title}
      </h2>
      <div style={{ fontSize: 12.5, color: "var(--sl-text-low)", marginTop: 3, maxWidth: "92ch" }}>{sub}</div>
    </div>
  );
}

// ── promotion readiness (honest, no no-op) ──────────────────────────────────────
// Replaces the old "Promote to active" button, which fired a toast but wrote
// nothing. RL mode is a deploy-time pin (RL_MODE env), so there is no live
// promote action. This panel shows the live mode, the advisory recommended
// mode, the runloop state, and the operator-writable policy gates that shape the
// EFFECTIVE mode — pointing the operator to Controls for the gates they can move,
// and noting that the RL mode itself changes at deploy time.

function modePillStatus(mode: RlMode | null): Status {
  if (mode === "active") return "ok";
  if (mode === "shadow") return "neutral";
  return "warn";
}

function modeLabel(mode: RlMode | null): string {
  return mode ? mode.toUpperCase() : "UNKNOWN";
}

function PromotionReadiness({
  rl,
  state,
  source,
  degraded,
  onRetry,
}: {
  rl: RlModeStatus;
  state: "loading" | "ready" | "error";
  source: "live" | "demo";
  degraded: boolean;
  onRetry: () => void;
}) {
  // The endpoint can return current_mode = null when the rl-engine is down; the
  // demonstration fallback is always populated, so this only bites if a live
  // call resolved but the engine itself was unreachable upstream.
  if (degraded) {
    return (
      <Card title="Promotion readiness" eyebrow="// rl/mode">
        <ErrorState
          title="Couldn't read the routing mode"
          hint="The rl-engine state wasn't reachable. Showing nothing rather than a guess."
          onRetry={onRetry}
        />
      </Card>
    );
  }

  const current = rl.current_mode;
  const recommended = rl.recommended_mode;
  const gates = rl.policy_gates ?? {
    safe_mode: null,
    operating_mode: null,
    strategy_name: null,
  };

  const matchesRecommendation =
    current != null && recommended != null && current === recommended;

  // What an operator can actually move toward active. safe_mode = true forces
  // shadow; operating_mode gates whether RL can drive weights at all.
  const safeModeBlocks = gates.safe_mode === true;
  const opModeGate = gates.operating_mode ?? null;

  const gateRows: Array<{
    label: string;
    value: ReactNode;
    note: string;
    tone: Status;
  }> = [
    {
      label: "safe_mode",
      value: gates.safe_mode == null ? "—" : gates.safe_mode ? "on" : "off",
      note: safeModeBlocks
        ? "Engaged — forces shadow regardless of the deploy-time pin."
        : "Released — does not hold routing in shadow.",
      tone: safeModeBlocks ? "warn" : "ok",
    },
    {
      label: "operating_mode",
      value: opModeGate ?? "—",
      note: "Operator-writable. Gates whether the engine may drive weights when the pin is active.",
      tone: "neutral",
    },
    {
      label: "strategy_name",
      value: gates.strategy_name ?? "custom",
      note: "The live named strategy the recommended mode is derived from.",
      tone: "neutral",
    },
  ];

  return (
    <Card
      title="RL routing — shadow evaluation"
      eyebrow="// rl/mode"
      actions={
        <Badge tone={source === "live" ? "mint" : "neutral"}>
          {source === "live" ? "LIVE" : "DEMONSTRATION"}
        </Badge>
      }
    >
      {state === "loading" && source !== "live" ? (
        <LoadState lines={4} />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          {/* mode summary row */}
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))",
              gap: 12,
            }}
          >
            <ModeStat
              label="Current mode"
              pill={
                <StatusPill status={modePillStatus(current)} hideDot>
                  {modeLabel(current)}
                </StatusPill>
              }
              foot="env-pinned (RL_MODE)"
            />
            <ModeStat
              label="Recommended"
              pill={
                <StatusPill status={modePillStatus(recommended)} hideDot>
                  {modeLabel(recommended)}
                </StatusPill>
              }
              foot={
                matchesRecommendation
                  ? "current matches the recommendation"
                  : "advisory, from the live strategy"
              }
            />
            <ModeStat
              label="Run loop"
              pill={
                <StatusPill
                  status={rl.runloop_enabled === false ? "warn" : "ok"}
                  hideDot
                >
                  {rl.runloop_enabled == null
                    ? "UNKNOWN"
                    : rl.runloop_enabled
                      ? "EVALUATING"
                      : "IDLE"}
                </StatusPill>
              }
              foot="ranks the pool each cycle"
            />
            <ModeStat
              label="Promotion path"
              pill={<Badge tone="neutral">DEPLOY-TIME</Badge>}
              foot="not an in-console toggle"
            />
          </div>

          {/* explanation — verbatim from the endpoint, honest about the pin */}
          <p
            style={{
              fontSize: 12.5,
              color: "var(--sl-text-mid)",
              lineHeight: 1.55,
              margin: 0,
              maxWidth: "82ch",
            }}
          >
            {rl.explanation}
          </p>

          {/* operator-writable gates */}
          <div>
            <div
              style={{
                fontSize: 10.5,
                fontFamily: "var(--sl-font-mono)",
                color: "var(--sl-text-low)",
                letterSpacing: "0.6px",
                textTransform: "uppercase",
                margin: "0 0 8px",
              }}
            >
              Policy gates · operator-adjustable in Controls
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 1, borderRadius: 10, overflow: "hidden", border: "1px solid var(--sl-hairline)" }}>
              {gateRows.map((g) => (
                <div
                  key={g.label}
                  style={{
                    display: "grid",
                    gridTemplateColumns: "minmax(0, 160px) auto 1fr",
                    gap: 12,
                    alignItems: "center",
                    padding: "10px 14px",
                    background: "var(--sl-surface)",
                  }}
                >
                  <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12, fontWeight: 600, color: "var(--sl-text)" }}>
                    {g.label}
                  </span>
                  <StatusPill status={g.tone} hideDot>
                    {String(g.value)}
                  </StatusPill>
                  <span style={{ fontSize: 11.5, color: "var(--sl-text-low)", lineHeight: 1.4 }}>
                    {g.note}
                  </span>
                </div>
              ))}
            </div>
          </div>

          <div
            style={{
              display: "flex",
              gap: 8,
              alignItems: "flex-start",
              fontSize: 11.5,
              color: "var(--sl-text-low)",
              borderTop: "1px solid var(--sl-hairline-soft)",
              paddingTop: 12,
            }}
          >
            <SlidersHorizontal size={14} strokeWidth={2} style={{ flex: "0 0 auto", marginTop: 1 }} />
            <span>
              safe_mode and operating_mode are set on the <b>Controls</b> view.
              The RL mode itself (the RL_MODE pin) changes with the engine's
              deployment — there is no live promotion here, so what you see is
              the engine evaluating in shadow and its proposals compared against
              the applied weights below.
            </span>
          </div>
        </div>
      )}
    </Card>
  );
}

function ModeStat({
  label,
  pill,
  foot,
}: {
  label: string;
  pill: ReactNode;
  foot: string;
}) {
  return (
    <div
      style={{
        background: "var(--sl-surface-sunk)",
        border: "1px solid var(--sl-hairline)",
        borderRadius: "var(--sl-radius-md)",
        padding: "12px 14px",
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
    >
      <span style={{ fontSize: 11, fontWeight: 600, color: "var(--sl-text-low)" }}>{label}</span>
      <span>{pill}</span>
      <span style={{ fontSize: 10.5, color: "var(--sl-text-faint)" }}>{foot}</span>
    </div>
  );
}

// ── proposed share card (RL) ──────────────────────────────────────────────────

function ProposedShareCard({
  shares,
  mode,
  state,
  onRetry,
}: {
  shares: BackendShare[];
  mode: Mode;
  state: "loading" | "ready" | "error";
  onRetry: () => void;
}) {
  const isShadow = mode !== "active";
  const rows: ShareRow[] = shares.map((s) => ({
    id: s.backend_id,
    label: s.backend_id,
    value: s.proposedPct,
    dim: s.excluded || s.proposedScore == null,
  }));
  return (
    <Card
      title="RL-proposed share"
      eyebrow="// routing engine"
      actions={
        <Badge tone={isShadow ? "graphite" : "mint"}>
          {isShadow ? "PROPOSED" : "APPLIED"}
        </Badge>
      }
    >
      <div style={{ fontSize: 11.5, color: "var(--sl-text-low)", margin: "0 0 14px" }}>
        Per-backend share the policy recommends this cycle, normalised across the
        eligible pool. {isShadow ? "Scored against the live router, not served." : "Currently served by the load balancer."}
      </div>
      {state === "loading" && rows.length === 0 ? (
        <LoadState lines={5} />
      ) : rows.length > 0 ? (
        <ShareBars rows={rows} max={100} asPercent />
      ) : (
        <EmptyState
          icon={<Compass size={20} strokeWidth={1.8} />}
          title="No ranking published yet"
          hint="The engine publishes once the state query returns at least one backend."
          action={
            <button
              type="button"
              onClick={onRetry}
              style={emptyRetryStyle}
            >
              Refresh
            </button>
          }
        />
      )}
    </Card>
  );
}

// ── applied share card (LB) ───────────────────────────────────────────────────

function AppliedShareCard({
  shares,
  algorithm,
  excluded,
  state,
  onRetry,
}: {
  shares: BackendShare[];
  algorithm: string;
  excluded: string[];
  state: "loading" | "ready" | "error";
  onRetry: () => void;
}) {
  const rows: ShareRow[] = shares.map((s) => ({
    id: s.backend_id,
    label: s.backend_id,
    value: s.appliedPct,
    dim: s.excluded || s.appliedWeight <= 0,
  }));
  return (
    <Card
      title="Applied LB share"
      eyebrow="// load balancer"
      actions={<Badge tone="neutral">{algorithm}</Badge>}
    >
      <div style={{ fontSize: 11.5, color: "var(--sl-text-low)", margin: "0 0 14px" }}>
        The deterministic split the load balancer is serving right now, derived
        from the committed upstream weights.
        {excluded.length > 0 ? ` ${excluded.join(", ")} held out of rotation.` : ""}
      </div>
      {state === "loading" && rows.length === 0 ? (
        <LoadState lines={5} />
      ) : rows.length > 0 ? (
        <ShareBars rows={rows} max={100} asPercent />
      ) : (
        <EmptyState
          icon={<Layers size={20} strokeWidth={1.8} />}
          title="No upstream weights reported"
          hint="The load balancer hasn't published a weight map yet."
          action={
            <button type="button" onClick={onRetry} style={emptyRetryStyle}>
              Refresh
            </button>
          }
        />
      )}
    </Card>
  );
}

// ── per-backend comparison table ──────────────────────────────────────────────

function ComparisonCard({ shares, mode }: { shares: BackendShare[]; mode: Mode }) {
  const isShadow = mode !== "active";
  const columns: Column<BackendShare>[] = [
    {
      key: "backend",
      header: "Backend",
      render: (s) => {
        const led = s.excluded ? "var(--sl-crit)" : "var(--sl-ok)";
        return (
          <div style={{ display: "flex", alignItems: "center", gap: 11 }}>
            <span
              style={{
                width: 9,
                height: 9,
                borderRadius: "50%",
                background: led,
                flex: "0 0 auto",
                boxShadow: `0 0 6px ${led}`,
              }}
            />
            <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 13, fontWeight: 600, color: "var(--sl-text)" }}>
              {s.backend_id}
            </span>
          </div>
        );
      },
    },
    {
      key: "proposed",
      header: "RL proposed",
      numeric: true,
      render: (s) =>
        s.proposedScore == null ? (
          <span style={{ color: "var(--sl-text-faint)" }}>—</span>
        ) : (
          <span>
            {Math.round(s.proposedPct)}
            <span style={{ color: "var(--sl-text-faint)", fontSize: 10.5 }}> %</span>
          </span>
        ),
    },
    {
      key: "score",
      header: "Raw score",
      numeric: true,
      render: (s) =>
        s.proposedScore == null ? (
          <span style={{ color: "var(--sl-text-faint)" }}>—</span>
        ) : (
          s.proposedScore.toFixed(3)
        ),
    },
    {
      key: "applied",
      header: "LB applied",
      numeric: true,
      render: (s) => (
        <span>
          {Math.round(s.appliedPct)}
          <span style={{ color: "var(--sl-text-faint)", fontSize: 10.5 }}> %</span>
        </span>
      ),
    },
    {
      key: "weight",
      header: "LB weight",
      numeric: true,
      render: (s) => s.appliedWeight,
    },
    {
      key: "delta",
      header: "Delta",
      numeric: true,
      render: (s) => {
        if (s.excluded) {
          return <StatusPill status="crit" hideDot>EXCLUDED</StatusPill>;
        }
        const delta = s.proposedPct - s.appliedPct;
        const sign = delta > 0 ? "+" : "";
        const tone =
          Math.abs(delta) < 1
            ? "var(--sl-text-low)"
            : delta > 0
              ? "var(--sl-mint-deep)"
              : "var(--sl-warn)";
        return (
          <span style={{ color: tone, fontWeight: 600 }}>
            {sign}
            {delta.toFixed(1)}
            <span style={{ color: "var(--sl-text-faint)", fontSize: 10.5 }}> pp</span>
          </span>
        );
      },
    },
  ];

  return (
    <Card flush>
      <DataTable
        columns={columns}
        rows={shares}
        rowKey={(s) => s.backend_id}
        rowMuted={(s) => s.excluded}
      />
      <div
        style={{
          padding: "12px 18px",
          borderTop: "1px solid var(--sl-hairline-soft)",
          fontSize: 11.5,
          color: "var(--sl-text-low)",
        }}
      >
        {isShadow
          ? "Delta is the move the live split would make if the proposal were promoted. In shadow mode this column is informational only."
          : "Delta is the gap between the latest re-score and the committed weights; it closes as the recommendation is applied."}
      </div>
    </Card>
  );
}

// ── load-balancer state panel ─────────────────────────────────────────────────

function LbStatePanel({
  algorithm,
  shares,
  excluded,
}: {
  algorithm: string;
  shares: BackendShare[];
  excluded: string[];
}) {
  const active = shares.filter((s) => !s.excluded && s.appliedWeight > 0).length;
  const rows: Array<[string, ReactNode]> = [
    ["Algorithm", <span key="alg" style={{ fontFamily: "var(--sl-font-mono)", fontWeight: 600 }}>{algorithm}</span>],
    ["Backends in rotation", `${active} of ${shares.length}`],
    [
      "Excluded backends",
      excluded.length > 0 ? (
        <span key="exc" style={{ display: "inline-flex", gap: 6, flexWrap: "wrap", justifyContent: "flex-end" }}>
          {excluded.map((id) => (
            <StatusPill key={id} status="crit" hideDot>{id}</StatusPill>
          ))}
        </span>
      ) : (
        <span key="none" style={{ color: "var(--sl-mint-deep)" }}>none</span>
      ),
    ],
  ];

  return (
    <Card title="Load-balancer state" eyebrow="// lb/state">
      <div style={{ marginBottom: 8 }}>
        {rows.map(([label, value], i) => (
          <div
            key={label}
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 12,
              padding: "11px 0",
              borderBottom: i < rows.length - 1 ? "1px solid var(--sl-hairline-soft)" : undefined,
            }}
          >
            <span style={{ fontSize: 12.5, color: "var(--sl-text-mid)" }}>{label}</span>
            <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12.5, fontWeight: 600, color: "var(--sl-text)" }}>
              {value}
            </span>
          </div>
        ))}
      </div>

      <div style={{ fontSize: 10.5, fontFamily: "var(--sl-font-mono)", color: "var(--sl-text-low)", margin: "4px 0 8px", letterSpacing: "0.4px", textTransform: "uppercase" }}>
        Applied upstream weights
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {shares.map((s) => (
          <div
            key={s.backend_id}
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              opacity: s.excluded ? 0.55 : 1,
            }}
          >
            <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12, color: "var(--sl-text-mid)" }}>
              {s.backend_id}
            </span>
            <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12, fontWeight: 700, color: s.excluded ? "var(--sl-crit)" : "var(--sl-text)" }}>
              {s.appliedWeight}
            </span>
          </div>
        ))}
      </div>
    </Card>
  );
}

// ── recent decisions ──────────────────────────────────────────────────────────

function timeOfDay(iso: string | null | undefined): string {
  if (!iso) return "—";
  const m = iso.match(/T(\d{2}:\d{2}:\d{2})/);
  return m ? m[1] : iso;
}

function DecisionHistory({
  decisions,
  fallbackMode,
}: {
  decisions: EngineStreamEvent[];
  fallbackMode: Mode;
}) {
  return (
    <Card title="Recent routing decisions" eyebrow="// smartload.routing" flush>
      {decisions.length === 0 ? (
        <EmptyState
          icon={<Radio size={20} strokeWidth={1.8} />}
          title="No recommendations on this channel yet"
          hint="The engine publishes one per cycle once the pool reports state."
        />
      ) : (
        <div style={{ display: "flex", flexDirection: "column" }}>
          {decisions.map((ev, i) => {
            const payload = (ev.payload ?? {}) as {
              mode?: string;
              server_rankings?: Array<{ backend_id: string; score: number }>;
              policy_version?: number;
            };
            const mode = (payload.mode ?? fallbackMode).toString().toLowerCase();
            const isShadow = mode !== "active";
            const ranks = Array.isArray(payload.server_rankings) ? payload.server_rankings : [];
            const top = ranks
              .slice()
              .sort((a, b) => (Number(b.score) || 0) - (Number(a.score) || 0))[0];
            const status: Status = isShadow ? "neutral" : "ok";
            return (
              <div
                key={`${ev.envelope?.event_id ?? i}`}
                style={{
                  display: "grid",
                  gridTemplateColumns: "auto 1fr auto",
                  gap: 13,
                  padding: "13px 18px",
                  borderBottom: i < decisions.length - 1 ? "1px solid var(--sl-hairline-soft)" : undefined,
                  alignItems: "flex-start",
                }}
              >
                <span
                  style={{
                    width: 30,
                    height: 30,
                    borderRadius: 9,
                    display: "grid",
                    placeItems: "center",
                    flex: "0 0 auto",
                    background: isShadow ? "var(--sl-surface-sunk)" : "var(--sl-mint-tint)",
                    color: isShadow ? "var(--sl-graphite)" : "var(--sl-mint-deep)",
                  }}
                >
                  <Compass size={15} strokeWidth={2} />
                </span>
                <div>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={{ fontSize: 13, fontWeight: 600, color: "var(--sl-text)" }}>
                      Recommendation
                    </span>
                    <StatusPill status={status} hideDot>
                      {isShadow ? "SHADOW" : "ACTIVE"}
                    </StatusPill>
                    {payload.policy_version != null ? (
                      <Badge tone="neutral">v{payload.policy_version}</Badge>
                    ) : null}
                  </div>
                  <div style={{ fontSize: 11.5, color: "var(--sl-text-mid)", marginTop: 4, lineHeight: 1.45 }}>
                    Ranked {ranks.length} backend{ranks.length === 1 ? "" : "s"}
                    {top ? (
                      <>
                        ; top is{" "}
                        <span style={{ fontFamily: "var(--sl-font-mono)", color: "var(--sl-text)" }}>
                          {top.backend_id}
                        </span>{" "}
                        at score {(Number(top.score) || 0).toFixed(3)}
                      </>
                    ) : null}
                    {isShadow ? ". Scored, not applied." : ". Applied to the load balancer."}
                  </div>
                </div>
                <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 10.5, color: "var(--sl-text-faint)", whiteSpace: "nowrap" }}>
                  {timeOfDay(ev.envelope?.timestamp)}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

// ── shared styling ──────────────────────────────────────────────────────────────

const emptyRetryStyle: React.CSSProperties = {
  fontFamily: "var(--sl-font-sans)",
  fontSize: 11.5,
  fontWeight: 600,
  color: "var(--sl-text)",
  background: "var(--sl-surface)",
  border: "1px solid var(--sl-hairline)",
  borderRadius: "var(--sl-radius-sm)",
  padding: "5px 12px",
  cursor: "pointer",
};
