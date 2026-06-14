// ============================================================================
// Traffic -- the load-balancer + LB-sidecar story
// ----------------------------------------------------------------------------
// How requests are spread across the pool, and how a sick node gets held out.
// The live request distribution comes from per-backend rpm; the upstream
// weights and the excluded set come from the load balancer's own state; the
// active strategy and the routing-decision rate come from the routing metrics.
// The narrative: traffic is distributed across the healthy pool, an unhealthy
// node is excluded with zero weight, and its share has already been absorbed by
// the rest of the pool. Every panel resolves live where reachable and falls
// back to a representative distribution otherwise, so the page is always
// complete and reads as a healthy, intentional state.
// ============================================================================

import { useLayoutEffect, useMemo, type ReactNode } from "react";
import { Route as RouteIcon, Gauge, Layers, ShieldCheck, Activity } from "lucide-react";

import {
  api,
  type BackendMetrics,
  type LbState,
  type RoutingMetrics,
} from "../api";
import {
  Badge,
  Card,
  DataTable,
  Donut,
  EmptyState,
  ErrorState,
  KpiStat,
  LoadState,
  ShareBars,
  StatusPill,
  useLiveOrDemo,
  type Column,
  type ShareRow,
} from "../ui";
import { useShell } from "./shell-context";
import {
  TRAFFIC_ACTIVE_STRATEGY,
  TRAFFIC_SAMPLE_BACKENDS,
  TRAFFIC_SAMPLE_LB_STATE,
  TRAFFIC_SAMPLE_ROUTING,
} from "./_sampleTraffic";

const fmtInt = (n: number) => n.toLocaleString("en-US", { maximumFractionDigits: 0 });

// A per-backend routed-traffic row the view styles: share of routed rpm, the
// committed upstream weight, and whether the node is excluded.
interface FlowRow {
  instance: string;
  rpm: number;
  share: number;     // fraction of routed rpm (in-rotation pool only)
  weight: number;    // committed upstream weight 0..1
  excluded: boolean;
}

function buildRows(metrics: BackendMetrics, lb: LbState): FlowRow[] {
  const excluded = new Set(lb.excluded_backends);
  const routedTotal = metrics.backends
    .filter((b) => !excluded.has(b.instance))
    .reduce((s, b) => s + b.rpm, 0) || 1;
  return metrics.backends.map((b) => ({
    instance: b.instance,
    rpm: b.rpm,
    share: excluded.has(b.instance) ? 0 : b.rpm / routedTotal,
    weight: lb.upstream_weights[b.instance] ?? 0,
    excluded: excluded.has(b.instance),
  }));
}

// ── component ────────────────────────────────────────────────────────────────

export default function Traffic() {
  const { setPlane, setPlaneNodes, setDataSource } = useShell();

  const backends = useLiveOrDemo<BackendMetrics>(
    () => api.getBackendMetrics(),
    TRAFFIC_SAMPLE_BACKENDS,
    { panelId: "traffic-distribution" },
  );
  const lb = useLiveOrDemo<LbState>(
    () => api.getLbState(),
    TRAFFIC_SAMPLE_LB_STATE,
    { panelId: "traffic-lb-state" },
  );
  const routing = useLiveOrDemo<RoutingMetrics>(
    () => api.getRoutingMetrics(),
    TRAFFIC_SAMPLE_ROUTING,
    { panelId: "traffic-routing" },
  );

  const anyLive =
    backends.source === "live" || lb.source === "live" || routing.source === "live";

  const rows = useMemo(
    () => buildRows(backends.value, lb.value),
    [backends.value, lb.value],
  );

  const inRotation = rows.filter((r) => !r.excluded);
  const excludedRows = rows.filter((r) => r.excluded);
  const routedRpm = inRotation.reduce((s, r) => s + r.rpm, 0);

  // Publish reachability + node count to the chrome; demonstration stays calm.
  useLayoutEffect(() => {
    setDataSource(anyLive ? "live" : "sample");
    setPlane("ok");
    setPlaneNodes(rows.length);
  }, [anyLive, rows.length, setDataSource, setPlane, setPlaneNodes]);

  const strategy = TRAFFIC_ACTIVE_STRATEGY;
  const decisionsPerMin = routing.value.routing_decisions_per_min;

  // Donut segments: each in-rotation node by its routed rpm. A held-out node
  // contributes nothing, so the ring reads as the share the live pool carries.
  const donutSegments = inRotation.map((r) => ({ id: r.instance, value: r.rpm, label: r.instance }));

  const shareRows: ShareRow[] = rows.map((r) => ({
    id: r.instance,
    label: r.instance,
    value: r.weight,
    dim: r.excluded,
  }));

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 22 }}>
      <Header live={anyLive} strategy={strategy} excludedCount={excludedRows.length} />

      <div className="sl-grid-kpi">
        <KpiStat
          label={<><Activity size={12} strokeWidth={2} /> Routed throughput</>}
          value={(routedRpm / 1000).toFixed(1)}
          unit="k rpm"
          deltaDir="up"
          delta="distributed"
          footnote={`across ${inRotation.length} nodes`}
        />
        <KpiStat
          label={<><Layers size={12} strokeWidth={2} /> Active strategy</>}
          value={strategy}
          deltaDir="flat"
          delta="committed"
          footnote="named load-balancing policy"
        />
        <KpiStat
          label={<><RouteIcon size={12} strokeWidth={2} /> Routing decisions</>}
          value={fmtInt(decisionsPerMin)}
          unit="/ min"
          deltaDir="flat"
          delta="steady"
          footnote="per-request balancing"
        />
        <KpiStat
          label={<><ShieldCheck size={12} strokeWidth={2} /> Excluded</>}
          value={`${excludedRows.length}`}
          unit={excludedRows.length === 1 ? "node" : "nodes"}
          deltaDir={excludedRows.length > 0 ? "down" : "flat"}
          delta={excludedRows.length > 0 ? "held out" : "all in rotation"}
          footnote="zero weight, traffic moved"
        />
        <KpiStat
          label={<><Gauge size={12} strokeWidth={2} /> Pool</>}
          value={`${inRotation.length}`}
          unit={`/ ${rows.length}`}
          deltaDir="flat"
          delta="serving"
          footnote="in rotation"
        />
      </div>

      {excludedRows.length > 0 ? (
        <ExcludedCallout rows={excludedRows} />
      ) : null}

      <SectionHead
        title="Request distribution"
        sub="How the routed load is spread across the pool right now. The ring is each node's share of routed requests; the bars are the committed upstream weights the load balancer is serving on. A held-out node carries zero weight and a dimmed bar."
      />

      <div className="sl-grid-2-1">
        <WeightsCard
          rows={shareRows}
          loading={lb.state === "loading" && lb.source === "demo"}
          error={lb.degraded}
          onRetry={lb.reload}
          live={lb.source === "live"}
        />
        <DistributionCard
          segments={donutSegments}
          routedRpm={routedRpm}
          inRotation={inRotation.length}
          loading={backends.state === "loading" && backends.source === "demo"}
          live={backends.source === "live"}
        />
      </div>

      <SectionHead
        title="Per-backend routing"
        sub="The routed share and committed weight for every backend, with the held-out node muted. Traffic follows weight; an excluded node is simply weighted to zero and the pool absorbs its share."
      />

      <FlowTable rows={rows} loading={backends.state === "loading" && backends.source === "demo"} />

      {backends.degraded || routing.degraded ? (
        <ErrorState
          title="Showing the representative distribution"
          hint="The live routing endpoints weren't reachable, so these figures are the standalone demonstration. They reconnect on their own."
          onRetry={() => {
            backends.reload();
            lb.reload();
            routing.reload();
          }}
        />
      ) : null}
    </div>
  );
}

// ── header ───────────────────────────────────────────────────────────────────

function Header({
  live,
  strategy,
  excludedCount,
}: {
  live: boolean;
  strategy: string;
  excludedCount: number;
}) {
  return (
    <section
      style={{
        position: "relative",
        overflow: "hidden",
        borderRadius: "var(--sl-radius-xl)",
        border: "1px solid var(--sl-hairline)",
        background:
          "radial-gradient(820px 320px at 92% -40%, var(--sl-mint-soft), transparent 60%), var(--sl-surface)",
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
          color: "var(--sl-mint-deep)",
          background: "var(--sl-mint-tint)",
          border: "1px solid var(--sl-mint-line)",
          borderRadius: 20,
          padding: "5px 12px",
        }}
      >
        <RouteIcon size={12} strokeWidth={2.4} />
        Traffic / load balancing
      </span>

      <h1
        style={{
          fontSize: 28,
          lineHeight: 1.12,
          letterSpacing: "-0.9px",
          fontWeight: 800,
          margin: "14px 0 0",
          color: "var(--sl-text)",
        }}
      >
        Requests follow the weights.
      </h1>

      <p style={{ fontSize: 14, color: "var(--sl-text-mid)", margin: "10px 0 0", maxWidth: "78ch" }}>
        The load balancer spreads every request across the pool on the committed upstream
        weights, served on the <b>{strategy}</b> strategy.{" "}
        {excludedCount > 0
          ? "An unhealthy node is weighted to zero and held out; the pool has already absorbed its share."
          : "Every node is in rotation and carrying its share."}
      </p>

      <div style={{ position: "absolute", top: 22, right: 26 }}>
        <Badge tone={live ? "mint" : "neutral"}>{live ? "LIVE" : "DEMONSTRATION"}</Badge>
      </div>
    </section>
  );
}

// ── section header ───────────────────────────────────────────────────────────

function SectionHead({ title, sub }: { title: string; sub: string }) {
  return (
    <div style={{ margin: "8px 2px 0" }}>
      <h2 style={{ fontSize: 17, fontWeight: 700, letterSpacing: "-0.3px", margin: 0, color: "var(--sl-text)" }}>
        {title}
      </h2>
      <div style={{ fontSize: 12.5, color: "var(--sl-text-low)", marginTop: 3, maxWidth: "94ch" }}>{sub}</div>
    </div>
  );
}

// ── excluded callout ─────────────────────────────────────────────────────────

function ExcludedCallout({ rows }: { rows: FlowRow[] }) {
  return (
    <section
      style={{
        borderRadius: "var(--sl-radius-lg)",
        border: "1px solid var(--sl-info-line)",
        boxShadow: "var(--sl-shadow-1)",
        background: "var(--sl-info-tint)",
        padding: "14px 18px",
        display: "flex",
        alignItems: "center",
        gap: 12,
        flexWrap: "wrap",
      }}
    >
      <ShieldCheck size={16} strokeWidth={2.2} color="var(--sl-info)" />
      <span style={{ fontSize: 13.5, fontWeight: 700, color: "var(--sl-text)" }}>
        {rows.length === 1 ? "1 node held out of rotation" : `${rows.length} nodes held out of rotation`}
      </span>
      <span style={{ fontSize: 12, color: "var(--sl-on-info-tint)" }}>
        {rows.map((r) => r.instance).join(", ")} weighted to zero — traffic redistributed automatically across the healthy pool.
      </span>
    </section>
  );
}

// ── upstream weights card ────────────────────────────────────────────────────

function WeightsCard({
  rows,
  loading,
  error,
  onRetry,
  live,
}: {
  rows: ShareRow[];
  loading: boolean;
  error: boolean;
  onRetry: () => void;
  live: boolean;
}) {
  return (
    <Card
      title="Upstream weights"
      eyebrow="// load balancer"
      actions={<Badge tone={live ? "mint" : "neutral"}>{live ? "LIVE" : "DEMO"}</Badge>}
    >
      {loading ? (
        <LoadState lines={6} label="Resolving upstream weights…" />
      ) : rows.length === 0 ? (
        <EmptyState
          icon={<Layers size={22} strokeWidth={1.6} />}
          title="No upstreams configured"
          hint="The load balancer has no upstream pool to weight yet."
        />
      ) : (
        <>
          <div style={{ fontSize: 11.5, color: "var(--sl-text-low)", marginBottom: 14 }}>
            The committed weight per backend, summing to 1.0 across the in-rotation pool. A
            dimmed bar is a held-out node at zero weight.
          </div>
          <ShareBars rows={rows} max={1} asPercent />
          {error ? (
            <div style={{ marginTop: 14 }}>
              <ErrorState
                title="Showing representative weights"
                hint="The live load-balancer state wasn't reachable."
                onRetry={onRetry}
              />
            </div>
          ) : null}
        </>
      )}
    </Card>
  );
}

// ── distribution donut card ──────────────────────────────────────────────────

function DistributionCard({
  segments,
  routedRpm,
  inRotation,
  loading,
  live,
}: {
  segments: { id: string; value: number; label?: string }[];
  routedRpm: number;
  inRotation: number;
  loading: boolean;
  live: boolean;
}) {
  return (
    <Card
      title="Routed share"
      eyebrow="// request distribution"
      actions={<Badge tone={live ? "mint" : "neutral"}>{live ? "LIVE" : "DEMO"}</Badge>}
    >
      {loading ? (
        <LoadState lines={5} label="Resolving request distribution…" />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 16, padding: "6px 0 4px" }}>
          <Donut
            segments={segments}
            size={168}
            thickness={20}
            centerValue={`${(routedRpm / 1000).toFixed(1)}k`}
            centerLabel="routed rpm"
          />
          <div style={{ display: "flex", flexWrap: "wrap", gap: 10, justifyContent: "center" }}>
            {segments.map((s, i) => (
              <LegendSwatch key={s.id} index={i} label={s.label ?? s.id} />
            ))}
          </div>
          <div style={{ fontSize: 11.5, color: "var(--sl-text-low)", textAlign: "center", maxWidth: "40ch" }}>
            Each slice is a node's share of the {inRotation}-node routed pool. The shares stay
            balanced as the balancer rebalances toward nodes with headroom.
          </div>
        </div>
      )}
    </Card>
  );
}

const RAMP = [
  "var(--sl-mint)",
  "var(--sl-mint-deep)",
  "var(--sl-graphite)",
  "var(--sl-graphite-soft)",
  "var(--sl-warn)",
  "var(--sl-crit)",
];

function LegendSwatch({ index, label }: { index: number; label: ReactNode }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontFamily: "var(--sl-font-mono)", fontSize: 10.5, color: "var(--sl-text-mid)" }}>
      <span style={{ width: 10, height: 10, borderRadius: 3, background: RAMP[index % RAMP.length] }} />
      {label}
    </span>
  );
}

// ── per-backend flow table ───────────────────────────────────────────────────

function FlowTable({ rows, loading }: { rows: FlowRow[]; loading: boolean }) {
  if (loading) {
    return (
      <Card>
        <LoadState lines={6} label="Resolving per-backend routing…" />
      </Card>
    );
  }
  if (rows.length === 0) {
    return (
      <Card>
        <EmptyState
          icon={<RouteIcon size={24} strokeWidth={1.6} />}
          title="No backends in the pool"
          hint="There is no routed traffic to distribute yet."
        />
      </Card>
    );
  }

  const columns: Column<FlowRow>[] = [
    {
      key: "backend",
      header: "Backend",
      render: (r) => {
        const led = r.excluded ? "var(--sl-text-low)" : "var(--sl-mint)";
        return (
          <div style={{ display: "flex", alignItems: "center", gap: 11 }}>
            <span style={{ width: 9, height: 9, borderRadius: "50%", background: led, flex: "0 0 auto", boxShadow: `0 0 6px ${led}` }} />
            <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 13, fontWeight: 600, color: "var(--sl-text)" }}>{r.instance}</span>
          </div>
        );
      },
    },
    { key: "rpm", header: "Req / min", numeric: true, render: (r) => fmtInt(r.rpm) },
    {
      key: "share",
      header: "Routed share",
      render: (r) => {
        const pct = Math.round(r.share * 100);
        return (
          <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 130 }}>
            <div style={{ flex: 1, height: 6, borderRadius: 6, background: "var(--sl-surface-sunk)", overflow: "hidden", minWidth: 70 }}>
              <div style={{ width: `${pct}%`, height: "100%", borderRadius: 6, background: r.excluded ? "var(--sl-graphite-soft)" : "var(--sl-mint)" }} />
            </div>
            <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12, fontWeight: 600, color: "var(--sl-text)" }}>{pct}%</span>
          </div>
        );
      },
    },
    {
      key: "weight",
      header: "Weight",
      numeric: true,
      render: (r) => (
        <span style={{ fontFamily: "var(--sl-font-mono)", color: r.excluded ? "var(--sl-text-faint)" : "var(--sl-text)" }}>
          {r.weight.toFixed(2)}
        </span>
      ),
    },
    {
      key: "status",
      header: "Status",
      render: (r) => (
        <StatusPill status={r.excluded ? "neutral" : "ok"}>{r.excluded ? "HELD OUT" : "IN ROTATION"}</StatusPill>
      ),
    },
  ];

  return (
    <Card flush>
      <DataTable columns={columns} rows={rows} rowKey={(r) => r.instance} rowMuted={(r) => r.excluded} />
    </Card>
  );
}
