// ============================================================================
// Helmsman -- the RL routing engine
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
// This view reads the engines snapshot (mode, server_rankings, policy version,
// exploration / confidence) and the load-balancer state (algorithm, applied
// weights, excluded backends), and puts the RL-proposed share next to the
// currently-applied LB share so the gap is legible. Every panel tries the live
// API and falls back to sample data on error or timeout, so the page renders
// complete with no backend running.
//
// A routing-decision history / replay endpoint is planned; until then this
// view shows the current rankings plus a sample of recent decisions taken from
// the events the engines snapshot already carries on smartload.routing.
// ============================================================================

import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  Compass,
  Eye,
  GitCompareArrows,
  Layers,
  Radio,
  ShieldCheck,
  Zap,
} from "lucide-react";

import {
  api,
  type EngineStreamEvent,
  type EnginesSnapshot,
  type LbState,
} from "../api";
import {
  Badge,
  Button,
  Card,
  DataTable,
  ShareBars,
  StatusPill,
  useToast,
  type Column,
  type ShareRow,
  type Status,
} from "../ui";
import { loadWithFallback } from "./loader";
import {
  RL_SERVICE,
  ROUTING_CHANNEL,
  SAMPLE_ENGINES_SNAPSHOT,
  SAMPLE_LB_ALGORITHM,
  SAMPLE_LB_STATE,
} from "./_sampleHelmsman";

const REFRESH_MS = 20_000;

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
function joinShares(
  rankings: Ranking[],
  lb: LbState,
): BackendShare[] {
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
  const toast = useToast();

  const [snap, setSnap] = useState<EnginesSnapshot>(SAMPLE_ENGINES_SNAPSHOT);
  const [lb, setLb] = useState<LbState>(SAMPLE_LB_STATE);
  const [live, setLive] = useState<boolean>(false);

  // The load-balancing algorithm is not part of the LbState surface, so it
  // stays sampled. (Comment kept so a future lb/state extension can drive it.)
  const algorithm = SAMPLE_LB_ALGORITHM;

  // Optimistic, local-only "promoted" intent. Promotion to active mode is a
  // policy decision served elsewhere (Controls + policy diff/commit); here we
  // only record the operator's request and surface it, best-effort. We do NOT
  // mutate shared state or call a new API.
  const [promotionRequested, setPromotionRequested] = useState(false);

  useEffect(() => {
    let cancelled = false;

    async function tick() {
      const [snapR, lbR] = await Promise.all([
        loadWithFallback(() => api.enginesSnapshot(), SAMPLE_ENGINES_SNAPSHOT),
        loadWithFallback(() => api.getLbState(), SAMPLE_LB_STATE),
      ]);
      if (cancelled) return;
      setSnap(snapR.value);
      setLb(lbR.value);
      setLive(snapR.source === "live" && lbR.source === "live");
    }

    tick();
    const id = window.setInterval(tick, REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  const engine = useMemo(() => readEngine(snap), [snap]);
  const shares = useMemo(() => joinShares(engine.rankings, lb), [engine.rankings, lb]);

  const isShadow = engine.mode !== "active";

  // KPI: top backend by proposed share.
  const topBackend = shares.find((s) => !s.excluded && s.proposedPct > 0) ?? null;

  // Recent routing decisions sampled from the channel the snapshot carries.
  const decisions: EngineStreamEvent[] = useMemo(() => {
    const evts = snap.channels?.[ROUTING_CHANNEL] ?? snap.recent ?? [];
    return evts.slice(0, 6);
  }, [snap]);

  const onPromote = () => {
    setPromotionRequested(true);
    // Best-effort affordance: in shadow mode active promotion is governed by
    // the policy plane (diff + commit), so this is a staged request, not an
    // immediate apply. Offline this is a pure no-op with a toast.
    toast.push({
      title: isShadow ? "Promotion staged" : "Routing engine already active",
      detail: isShadow
        ? "RL active-mode promotion queued for policy diff review"
        : "Weights are already applied to the load balancer",
      tone: isShadow ? "info" : "ok",
    });
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 22 }}>
      <ModeBanner
        mode={engine.mode}
        rlModeEnv={engine.rlModeEnv}
        live={live}
        promotionRequested={promotionRequested}
        onPromote={onPromote}
      />

      <KpiRail
        mode={engine.mode}
        topBackend={topBackend?.backend_id ?? "—"}
        topShare={topBackend?.proposedPct ?? 0}
        explorationRate={engine.explorationRate}
        confidenceThreshold={engine.confidenceThreshold}
        policyVersion={engine.policyVersion}
      />

      <SectionHead
        title="Proposed vs applied routing share"
        sub={
          isShadow
            ? "Left is what the routing engine proposes this cycle. Right is the deterministic split the load balancer is actually serving. In shadow mode the proposal is observed and scored, never applied."
            : "The routing engine is active: its proposed share is driving the load-balancer weights. Left and right should track closely; any gap is the latest re-score not yet committed."
        }
      />

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)",
          gap: 18,
          alignItems: "start",
        }}
      >
        <ProposedShareCard shares={shares} mode={engine.mode} />
        <AppliedShareCard shares={shares} algorithm={algorithm} excluded={lb.excluded_backends ?? []} />
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

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1.15fr)",
          gap: 18,
          alignItems: "start",
        }}
      >
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
  promotionRequested,
  onPromote,
}: {
  mode: Mode;
  rlModeEnv: string | null;
  live: boolean;
  promotionRequested: boolean;
  onPromote: () => void;
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
        display: "grid",
        gridTemplateColumns: "minmax(0, 1fr) auto",
        gap: 26,
        alignItems: "center",
      }}
    >
      <div>
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
              deterministic split until the proposal is promoted to active.
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
            {live ? "live" : "sample"} · {ROUTING_CHANNEL}
          </span>
          {rlModeEnv ? (
            <span style={{ display: "inline-flex", alignItems: "center", gap: 7 }}>
              RL_MODE pin = <b style={{ color: "var(--sl-text-mid)" }}>{rlModeEnv}</b>
            </span>
          ) : null}
        </div>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 10, alignItems: "flex-end" }}>
        <Button
          variant={isShadow ? "primary" : "secondary"}
          icon={<Zap size={14} strokeWidth={2} />}
          onClick={onPromote}
          disabled={!isShadow}
        >
          {isShadow ? "Promote to active" : "Active"}
        </Button>
        {promotionRequested && isShadow ? (
          <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 10.5, color: "var(--sl-mint-deep)" }}>
            promotion staged · awaiting policy commit
          </span>
        ) : (
          <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 10.5, color: "var(--sl-text-faint)", maxWidth: 200, textAlign: "right" }}>
            {isShadow
              ? "active mode is gated by a policy diff + commit"
              : "weights are applied to the load balancer"}
          </span>
        )}
      </div>
    </section>
  );
}

// ── KPI rail ──────────────────────────────────────────────────────────────────

function KpiRail({
  mode,
  topBackend,
  topShare,
  explorationRate,
  confidenceThreshold,
  policyVersion,
}: {
  mode: Mode;
  topBackend: string;
  topShare: number;
  explorationRate: number | null;
  confidenceThreshold: number | null;
  policyVersion: number | null;
}) {
  const isShadow = mode !== "active";
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 16 }}>
      <KpiCard
        label={<><Eye size={12} strokeWidth={2} /> Active mode</>}
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

// ── proposed share card (RL) ──────────────────────────────────────────────────

function ProposedShareCard({ shares, mode }: { shares: BackendShare[]; mode: Mode }) {
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
      {rows.length > 0 ? (
        <ShareBars rows={rows} max={100} asPercent />
      ) : (
        <div style={{ fontSize: 13, color: "var(--sl-text-low)", padding: "8px 0" }}>
          No ranking published yet. The engine publishes once the state query
          returns at least one backend.
        </div>
      )}
    </Card>
  );
}

// ── applied share card (LB) ───────────────────────────────────────────────────

function AppliedShareCard({
  shares,
  algorithm,
  excluded,
}: {
  shares: BackendShare[];
  algorithm: string;
  excluded: string[];
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
      {rows.length > 0 ? (
        <ShareBars rows={rows} max={100} asPercent />
      ) : (
        <div style={{ fontSize: 13, color: "var(--sl-text-low)", padding: "8px 0" }}>
          No upstream weights reported.
        </div>
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
        <div style={{ padding: 18, fontSize: 13, color: "var(--sl-text-low)" }}>
          No recommendations on this channel yet. The engine publishes one per
          cycle once the pool reports state.
        </div>
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
