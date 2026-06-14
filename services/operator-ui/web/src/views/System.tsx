// ============================================================================
// System -- the "show the whole system" architecture screen
// ----------------------------------------------------------------------------
// The centerpiece for walking a stakeholder through SmartLoad end to end. It
// renders every one of the eleven services as a node, grouped into four planes
// (ingress & traffic, the observability pipeline, the decision plane, and this
// presentation console), and draws the data-flow edges between them as a clear
// layered architecture diagram. Each node shows its purpose, a health pill, its
// last activity, and one live metric. The two headless OTLP shippers are shown
// as healthy infrastructure, not errors. An observability-pipeline panel calls
// out the shippers and the telemetry store explicitly. Data resolves live where
// reachable and falls back to a representative topology otherwise, so the page
// is always complete and always reads healthy.
// ============================================================================

import { useMemo, useRef, useState, useLayoutEffect, type ReactNode } from "react";
import { Network, Boxes, GitBranch, Radio, Activity } from "lucide-react";

import { api, type SystemTopology, type TopologyNode } from "../api";
import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  KpiStat,
  LoadState,
  StatusPill,
  useLiveOrDemo,
  type Status,
} from "../ui";
import { useShell } from "./shell-context";
import {
  SAMPLE_SYSTEM_COUNTS,
  SAMPLE_SYSTEM_TOPOLOGY,
  SYSTEM_PLANE_META,
  SYSTEM_PLANE_OF,
  type SystemPlane,
} from "./_sampleSystem";

const PLANE_ORDER: SystemPlane[] = ["ingress", "observability", "decision", "presentation"];

// Map a topology node status onto the design-kit Status used by StatusPill.
// "headless" is healthy infra (calm), not a warning.
function nodeStatus(s: TopologyNode["status"]): Status {
  if (s === "unreachable") return "crit";
  if (s === "degraded") return "warn";
  if (s === "headless") return "neutral";
  return "ok";
}

function statusWord(s: TopologyNode["status"]): string {
  if (s === "unreachable") return "UNREACHABLE";
  if (s === "degraded") return "DEGRADED";
  if (s === "headless") return "HEALTHY · HEADLESS";
  return "HEALTHY";
}

function planeOf(id: string): SystemPlane {
  return SYSTEM_PLANE_OF[id] ?? "decision";
}

// ── component ────────────────────────────────────────────────────────────────

export default function System() {
  const { setPlane, setPlaneNodes, setDataSource } = useShell();

  const topo = useLiveOrDemo<SystemTopology>(
    () => api.getSystemTopology(),
    SAMPLE_SYSTEM_TOPOLOGY,
    { panelId: "system-topology", timeoutMs: 4000 },
  );

  // Publish reachability + node count to the chrome. Demonstration stays calm:
  // only a genuinely unreachable live node raises the plane above healthy.
  useLayoutEffect(() => {
    const t = topo.value;
    const anyUnreachable = t.nodes.some((n) => n.status === "unreachable");
    const anyDegraded = t.nodes.some((n) => n.status === "degraded");
    if (topo.source === "live") {
      setPlane(anyUnreachable ? "bad" : anyDegraded ? "warn" : "ok");
    } else {
      setPlane("ok");
    }
    setPlaneNodes(t.nodes.length);
    setDataSource(topo.source === "live" ? "live" : "sample");
  }, [topo.value, topo.source, setPlane, setPlaneNodes, setDataSource]);

  const counts = useMemo(() => {
    if (topo.source === "demo") return SAMPLE_SYSTEM_COUNTS;
    const nodes = topo.value.nodes;
    return {
      services_total: nodes.length,
      http_services: nodes.filter((n) => n.http).length,
      headless_shippers: nodes.filter((n) => n.status === "headless").length,
      data_flows: topo.value.edges.length,
    };
  }, [topo.value, topo.source]);

  const healthy = topo.value.nodes.filter((n) => nodeStatus(n.status) === "ok" || n.status === "headless").length;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 22 }}>
      <Header live={topo.source === "live"} counts={counts} />

      <div className="sl-grid-kpi">
        <KpiStat
          label={<><Boxes size={12} strokeWidth={2} /> Services</>}
          value={`${counts.services_total}`}
          unit="online"
          deltaDir="flat"
          delta="all reporting"
          footnote="across four planes"
        />
        <KpiStat
          label={<><Activity size={12} strokeWidth={2} /> Healthy</>}
          value={`${healthy}`}
          unit={`/ ${counts.services_total}`}
          deltaDir="up"
          delta="nominal"
          footnote="incl. headless infra"
        />
        <KpiStat
          label={<><Radio size={12} strokeWidth={2} /> Telemetry shippers</>}
          value={`${counts.headless_shippers}`}
          unit="headless"
          deltaDir="flat"
          delta="streaming"
          footnote="OTLP into the pipeline"
        />
        <KpiStat
          label={<><GitBranch size={12} strokeWidth={2} /> Data flows</>}
          value={`${counts.data_flows}`}
          unit="edges"
          deltaDir="flat"
          delta="closed loop"
          footnote="telemetry → decision → routing"
        />
        <KpiStat
          label={<><Network size={12} strokeWidth={2} /> HTTP services</>}
          value={`${counts.http_services}`}
          unit="with API"
          deltaDir="flat"
          delta="reachable"
          footnote="health-checked"
        />
      </div>

      <SectionHead
        title="System architecture"
        sub="Every SmartLoad service, grouped by plane, with the data-flow edges between them. Telemetry feeds the decision plane, the decision plane rules on demand and health, and the load balancer routes accordingly — the closed loop, end to end."
      />

      {topo.state === "loading" && topo.source === "demo" ? (
        <Card>
          <LoadState lines={6} label="Resolving system topology…" />
        </Card>
      ) : topo.value.nodes.length === 0 ? (
        <Card>
          <EmptyState
            icon={<Network size={26} strokeWidth={1.6} />}
            title="No services reported"
            hint="The topology endpoint returned an empty system. Nothing to draw yet."
          />
        </Card>
      ) : (
        <TopologyDiagram topo={topo.value} />
      )}

      {topo.degraded ? (
        <ErrorState
          title="Showing the representative topology"
          hint="The live topology endpoint wasn't reachable, so this diagram is the standalone demonstration system. It reconnects on its own."
          onRetry={topo.reload}
        />
      ) : null}

      <SectionHead
        title="Observability pipeline"
        sub="The headless OTLP shippers and the time-series store that feed every decision. These have no operator surface of their own — they are healthy infrastructure, called out here so the whole pipeline is represented."
      />

      <ObservabilityPanel topo={topo.value} />
    </div>
  );
}

// ── header ───────────────────────────────────────────────────────────────────

function Header({
  live,
  counts,
}: {
  live: boolean;
  counts: typeof SAMPLE_SYSTEM_COUNTS;
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
        <Network size={12} strokeWidth={2.4} />
        System / whole-system topology
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
        The whole system, on one screen.
      </h1>

      <p style={{ fontSize: 14, color: "var(--sl-text-mid)", margin: "10px 0 0", maxWidth: "78ch" }}>
        {counts.services_total} services across four planes, wired into a single closed
        loop: signals flow up through the observability pipeline, the decision plane
        forecasts and rules, and the routing path acts on the result.
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

// ── topology diagram ─────────────────────────────────────────────────────────
// A layered architecture diagram: each plane is a horizontal lane of node
// cards, and the data-flow edges are drawn as SVG connectors on an overlay that
// tracks the real card geometry. The overlay measures node centers after layout
// (and on resize) so the connectors stay glued to the cards as the grid reflows.

interface NodeRect {
  cx: number; // center x relative to the diagram
  topY: number;
  bottomY: number;
  leftX: number;
  rightX: number;
}

function TopologyDiagram({ topo }: { topo: SystemTopology }) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const nodeRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  const [rects, setRects] = useState<Map<string, NodeRect>>(new Map());
  const [size, setSize] = useState({ w: 0, h: 0 });

  const lanes = useMemo(() => {
    const byPlane = new Map<SystemPlane, TopologyNode[]>();
    for (const p of PLANE_ORDER) byPlane.set(p, []);
    for (const n of topo.nodes) {
      const plane = planeOf(n.id);
      byPlane.get(plane)?.push(n);
    }
    return PLANE_ORDER
      .map((p) => ({ plane: p, nodes: byPlane.get(p) ?? [] }))
      .filter((l) => l.nodes.length > 0);
  }, [topo.nodes]);

  // Measure node centers after layout + on resize, so the SVG edges align.
  useLayoutEffect(() => {
    const measure = () => {
      const wrap = wrapRef.current;
      if (!wrap) return;
      const base = wrap.getBoundingClientRect();
      const next = new Map<string, NodeRect>();
      nodeRefs.current.forEach((el, id) => {
        const r = el.getBoundingClientRect();
        next.set(id, {
          cx: r.left - base.left + r.width / 2,
          topY: r.top - base.top,
          bottomY: r.bottom - base.top,
          leftX: r.left - base.left,
          rightX: r.right - base.left,
        });
      });
      setRects(next);
      setSize({ w: base.width, h: base.height });
    };
    measure();
    const ro = new ResizeObserver(measure);
    if (wrapRef.current) ro.observe(wrapRef.current);
    window.addEventListener("resize", measure);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [lanes]);

  return (
    <Card flush>
      <div ref={wrapRef} style={{ position: "relative", padding: "20px 18px 16px" }}>
        {/* Edge overlay sits under the node cards (cards have their own bg). */}
        <svg
          width={size.w}
          height={size.h}
          viewBox={`0 0 ${size.w} ${size.h}`}
          style={{ position: "absolute", inset: 0, pointerEvents: "none", zIndex: 0 }}
          aria-hidden
        >
          <defs>
            <marker
              id="sl-arrow"
              viewBox="0 0 10 10"
              refX="8"
              refY="5"
              markerWidth="7"
              markerHeight="7"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--sl-mint)" opacity="0.7" />
            </marker>
          </defs>
          {topo.edges.map((e, i) => {
            const a = rects.get(e.source);
            const b = rects.get(e.target);
            if (!a || !b) return null;
            // Route source-bottom -> target-top for the common downward flow,
            // otherwise connect nearest vertical edges. A gentle cubic keeps the
            // diagram legible without crossing through cards.
            const downward = b.topY >= a.bottomY - 4;
            const x1 = a.cx;
            const y1 = downward ? a.bottomY : a.topY;
            const x2 = b.cx;
            const y2 = downward ? b.topY : b.bottomY;
            const my = (y1 + y2) / 2;
            const d = `M ${x1} ${y1} C ${x1} ${my}, ${x2} ${my}, ${x2} ${y2}`;
            return (
              <g key={`${e.source}-${e.target}-${i}`}>
                <path
                  d={d}
                  fill="none"
                  stroke="var(--sl-mint)"
                  strokeOpacity={0.34}
                  strokeWidth={1.6}
                  markerEnd="url(#sl-arrow)"
                />
              </g>
            );
          })}
        </svg>

        {/* Lanes (above the edge overlay). */}
        <div style={{ position: "relative", zIndex: 1, display: "flex", flexDirection: "column", gap: 34 }}>
          {lanes.map((lane) => {
            const meta = SYSTEM_PLANE_META[lane.plane];
            return (
              <div key={lane.plane}>
                <div style={{ display: "flex", alignItems: "baseline", gap: 10, marginBottom: 12, paddingLeft: 2 }}>
                  <span
                    style={{
                      fontFamily: "var(--sl-font-mono)",
                      fontSize: 10,
                      letterSpacing: "1.4px",
                      textTransform: "uppercase",
                      color: "var(--sl-text-low)",
                      fontWeight: 700,
                    }}
                  >
                    {meta.label}
                  </span>
                  <span style={{ fontSize: 11.5, color: "var(--sl-text-faint)" }}>{meta.caption}</span>
                </div>
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: `repeat(auto-fit, minmax(220px, 1fr))`,
                    gap: 14,
                  }}
                >
                  {lane.nodes.map((n) => (
                    <NodeCard
                      key={n.id}
                      node={n}
                      registerRef={(el) => {
                        if (el) nodeRefs.current.set(n.id, el);
                        else nodeRefs.current.delete(n.id);
                      }}
                    />
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </Card>
  );
}

// ── node card ────────────────────────────────────────────────────────────────

function NodeCard({
  node,
  registerRef,
}: {
  node: TopologyNode;
  registerRef: (el: HTMLDivElement | null) => void;
}) {
  const s = nodeStatus(node.status);
  const led = s === "crit" ? "var(--sl-crit)" : s === "warn" ? "var(--sl-warn)" : s === "neutral" ? "var(--sl-text-low)" : "var(--sl-mint)";
  const headless = node.status === "headless";
  return (
    <div
      ref={registerRef}
      style={{
        position: "relative",
        background: "var(--sl-surface)",
        border: `1px solid ${headless ? "var(--sl-info-line)" : "var(--sl-hairline)"}`,
        borderRadius: "var(--sl-radius-md)",
        boxShadow: "var(--sl-shadow-1)",
        padding: "13px 14px",
        display: "flex",
        flexDirection: "column",
        gap: 8,
        minWidth: 0,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
        <span style={{ width: 9, height: 9, borderRadius: "50%", background: led, flex: "0 0 auto", boxShadow: `0 0 6px ${led}` }} />
        <span style={{ fontSize: 13.5, fontWeight: 700, color: "var(--sl-text)", letterSpacing: "-0.2px" }}>
          {node.display_name}
        </span>
        <span style={{ marginLeft: "auto" }}>
          <StatusPill status={s} hideDot>{statusWord(node.status)}</StatusPill>
        </span>
      </div>

      <div style={{ fontSize: 11.5, color: "var(--sl-text-mid)", lineHeight: 1.45 }}>{node.role}</div>

      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 10,
          marginTop: 2,
          fontFamily: "var(--sl-font-mono)",
          fontSize: 10.5,
        }}
      >
        {node.key_metric ? (
          <span style={{ color: "var(--sl-text)", fontWeight: 600 }}>
            <span style={{ color: "var(--sl-text-low)", fontWeight: 500 }}>{node.key_metric.label}</span>{" "}
            {String(node.key_metric.value)}
          </span>
        ) : (
          <span style={{ color: "var(--sl-text-faint)" }}>—</span>
        )}
        {node.last_activity ? (
          <span style={{ color: "var(--sl-text-faint)", whiteSpace: "nowrap" }}>{node.last_activity}</span>
        ) : null}
      </div>
    </div>
  );
}

// ── observability pipeline panel ─────────────────────────────────────────────

function ObservabilityPanel({ topo }: { topo: SystemTopology }) {
  const obs = topo.nodes.filter((n) => planeOf(n.id) === "observability");
  if (obs.length === 0) {
    return (
      <Card>
        <EmptyState
          icon={<Radio size={24} strokeWidth={1.6} />}
          title="No observability nodes reported"
          hint="The telemetry shippers and store aren't in the current topology payload."
        />
      </Card>
    );
  }
  return (
    <div className="sl-grid-3">
      {obs.map((n) => {
        const headless = n.status === "headless";
        const explain: Record<string, ReactNode> = {
          "lb-otel-shipper": "Reads the load balancer's request stream and forwards it as OTLP spans into the telemetry store. No HTTP surface of its own.",
          "resource-collector": "Samples per-container CPU and memory and ships them into the store, so the decision plane can see resource pressure.",
          "telemetry": "The time-series store every other service reads from. Forecasting, anomaly detection, and routing all draw their signals here.",
        };
        return (
          <Card key={n.id} title={n.display_name} eyebrow={headless ? "// headless infra" : "// store"}>
            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
                <Radio size={15} strokeWidth={2} color={headless ? "var(--sl-info)" : "var(--sl-mint)"} />
                <StatusPill status={nodeStatus(n.status)} hideDot>{statusWord(n.status)}</StatusPill>
              </div>
              <div style={{ fontSize: 12, color: "var(--sl-text-mid)", lineHeight: 1.5 }}>
                {explain[n.id] ?? n.role}
              </div>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  paddingTop: 10,
                  borderTop: "1px solid var(--sl-hairline-soft)",
                  fontFamily: "var(--sl-font-mono)",
                  fontSize: 11.5,
                }}
              >
                {n.key_metric ? (
                  <span style={{ color: "var(--sl-text)", fontWeight: 700 }}>
                    {String(n.key_metric.value)}
                    <span style={{ color: "var(--sl-text-low)", fontWeight: 500, marginLeft: 5 }}>{n.key_metric.label}</span>
                  </span>
                ) : (
                  <span style={{ color: "var(--sl-text-faint)" }}>streaming</span>
                )}
                {n.last_activity ? (
                  <span style={{ color: "var(--sl-text-faint)" }}>last {n.last_activity}</span>
                ) : null}
              </div>
            </div>
          </Card>
        );
      })}
    </div>
  );
}
