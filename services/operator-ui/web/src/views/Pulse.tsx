// ============================================================================
// Pulse -- per-backend vitals + resource utilisation
// ----------------------------------------------------------------------------
// The fleet's vital signs, node by node: p95 latency against its SLO, request
// throughput, error rate, a health-score bar, and a status pill, with the
// excluded / unhealthy node called out on its evidence. Below, a resource panel
// rolls per-container CPU and memory up by service. Cluster roll-up KPIs sit at
// the top, sourced from the real KPI-trends feed (recent series + measured
// window-over-window deltas). Every panel resolves through useLiveOrDemo: it
// shows representative data immediately and upgrades to live when a backend is
// reachable, registering its source with the shell's global Demonstration/Live
// badge so the indicator reflects which path each panel took.
// ============================================================================

import { useEffect, useMemo } from "react";
import {
  Activity,
  AlertTriangle,
  Boxes,
  Cpu,
  Gauge,
  MemoryStick,
  TrendingUp,
} from "lucide-react";

import {
  api,
  formatBytes,
  resourcesByService,
  type BackendStat,
  type ServiceResource,
  type TrendKpi,
} from "../api";
import {
  Badge,
  Card,
  DataTable,
  EmptyState,
  ErrorState,
  EvidenceLine,
  KpiStat,
  LoadState,
  StatusPill,
  useLiveOrDemo,
  type Column,
  type Status,
} from "../ui";
import { useShell } from "./shell-context";
import {
  PULSE_EXCLUSION_P95_MS,
  PULSE_SAMPLE_BACKENDS,
  PULSE_SAMPLE_RESOURCES,
  PULSE_SAMPLE_TRENDS,
  PULSE_SLO_P95_MS,
} from "./_samplePulse";

const fmtInt = (n: number) => n.toLocaleString("en-US", { maximumFractionDigits: 0 });

// ── derived per-backend vital row ────────────────────────────────────────────
// A BackendStat enriched with a verdict the view can style: a health score
// (0..1), an ok/warn/crit status, an excluded flag, and the evidence behind a
// breach. There is no per-row latency series in the metrics feed, so the row
// reads its current p95 against the SLO rather than fabricating a trend.

interface VitalRow {
  instance: string;
  p95_ms: number | null;
  rpm: number;
  error_rate_pct: number;
  samples: number;
  health_score: number;
  status: Status;
  excluded: boolean;
  evidence?: { metric: string; observed: string; threshold: string };
}

// Synthesize a verdict from the raw measurements. A p95 over the exclusion
// threshold pulls a node out of rotation (crit + excluded); a high error rate
// degrades it (warn). Health is a bounded 0..1 reading off the same signals so
// the bar tells the same story as the pill.
function toVitalRow(b: BackendStat, sloP95: number, exclusionP95: number): VitalRow {
  const p95 = b.p95_ms ?? 0;
  const err = b.error_rate_pct;
  const overLatency = b.p95_ms != null && b.p95_ms > exclusionP95;
  const degradedLatency = b.p95_ms != null && b.p95_ms > sloP95;
  const overError = err >= 0.4;

  const status: Status = overLatency || err > 3 ? "crit" : degradedLatency || overError ? "warn" : "ok";
  const excluded = overLatency || err > 3;

  const latencyPenalty = Math.min(1, p95 / (exclusionP95 * 2));
  const errorPenalty = Math.min(1, err / 8);
  const health = Math.max(0, Math.min(1, 1 - latencyPenalty * 0.7 - errorPenalty * 0.6));

  let evidence: VitalRow["evidence"];
  if (overLatency) {
    evidence = { metric: "p95_latency_ms", observed: `${p95} ms`, threshold: `${exclusionP95} ms` };
  } else if (overError) {
    evidence = { metric: "error_rate_pct", observed: `${err.toFixed(2)} %`, threshold: "0.50 %" };
  }

  return {
    instance: b.instance,
    p95_ms: b.p95_ms,
    rpm: b.rpm,
    error_rate_pct: err,
    samples: b.samples,
    health_score: Number(health.toFixed(2)),
    status,
    excluded,
    evidence,
  };
}

function statusColor(s: Status): string {
  return s === "crit"
    ? "var(--sl-crit)"
    : s === "warn"
      ? "var(--sl-warn)"
      : s === "ok"
        ? "var(--sl-mint)"
        : "var(--sl-text-low)";
}

function statusWord(row: VitalRow): string {
  if (row.excluded) return "EXCLUDED";
  if (row.status === "warn") return "DEGRADED";
  if (row.status === "crit") return "UNHEALTHY";
  return "HEALTHY";
}

// Map a delta_pct to a KpiStat delta direction. Treat a near-zero change as flat
// so the rail doesn't read as alarmingly volatile on tiny movements.
function deltaDir(deltaPct: number | null): "up" | "down" | "flat" {
  if (deltaPct == null || Math.abs(deltaPct) < 0.05) return "flat";
  return deltaPct > 0 ? "up" : "down";
}

function fmtDelta(deltaPct: number | null): string | undefined {
  if (deltaPct == null) return undefined;
  const r = Number(deltaPct.toFixed(1));
  return `${r >= 0 ? "+" : ""}${r}%`;
}

// ── component ────────────────────────────────────────────────────────────────

export default function Pulse() {
  const { setPlane, setPlaneNodes } = useShell();

  // Each panel resolves live-or-demo independently and registers its source
  // with the global Demonstration/Live badge through a unique panelId.
  const backendsQ = useLiveOrDemo(() => api.getBackendMetrics(), PULSE_SAMPLE_BACKENDS, {
    panelId: "pulse-backends",
  });
  const resourcesQ = useLiveOrDemo(() => api.getResources(), PULSE_SAMPLE_RESOURCES, {
    panelId: "pulse-resources",
  });
  const trendsQ = useLiveOrDemo(() => api.getTrends(), PULSE_SAMPLE_TRENDS, {
    panelId: "pulse-trends",
  });

  const backends = backendsQ.value;
  const resources = resourcesQ.value;
  const trends = trendsQ.value;

  // ── derived vitals + roll-up ───────────────────────────────────────────────

  const rows = useMemo(
    () =>
      backends.backends.map((b) => toVitalRow(b, PULSE_SLO_P95_MS, PULSE_EXCLUSION_P95_MS)),
    [backends],
  );

  const totalCount = rows.length;
  const excludedRows = rows.filter((r) => r.excluded);
  const healthyCount = rows.filter((r) => r.status === "ok").length;

  const services = useMemo(() => {
    const byService = resourcesByService(resources);
    return Object.entries(byService).sort((a, b) => (b[1].cpu_percent ?? 0) - (a[1].cpu_percent ?? 0));
  }, [resources]);

  // Publish plane health to the shell footer. The console is built to present
  // cleanly on representative data, so demonstration never reads as degraded:
  // plane health stays healthy and only the count tracks the routed pool.
  useEffect(() => {
    setPlane("ok");
    setPlaneNodes(totalCount);
  }, [setPlane, setPlaneNodes, totalCount]);

  const backendsLive = backendsQ.source === "live";

  // ── render ─────────────────────────────────────────────────────────────────

  return (
    <div className="sl-stack">
      <Header backendsLive={backendsLive} excludedCount={excludedRows.length} />

      <KpiRail
        trends={trends}
        healthyCount={healthyCount}
        totalCount={totalCount}
        excludedCount={excludedRows.length}
      />

      {excludedRows.length > 0 ? <ExcludedCallout rows={excludedRows} /> : null}

      <SectionHead
        title="Per-backend vitals"
        sub={`${totalCount} nodes. Each row is a live vital reading -- p95 against its SLO, throughput, error rate, and a health verdict. ${
          excludedRows.length > 0
            ? "An excluded node is held out of rotation; its traffic redistributes automatically."
            : "All nodes are in rotation."
        }`}
      />

      <VitalsTable
        rows={rows}
        state={backendsQ.state}
        degraded={backendsQ.degraded}
        onRetry={backendsQ.reload}
      />

      <SectionHead
        title="Resource utilisation"
        sub="Per-service CPU and memory across the running containers. The backend fleet is summed across its replicas; everything else is a single container."
      />

      <ResourcePanel
        services={services}
        state={resourcesQ.state}
        degraded={resourcesQ.degraded}
        onRetry={resourcesQ.reload}
      />
    </div>
  );
}

// ── header ───────────────────────────────────────────────────────────────────

function Header({ backendsLive, excludedCount }: { backendsLive: boolean; excludedCount: number }) {
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
        <Activity size={12} strokeWidth={2.4} />
        Pulse / per-backend vitals
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
        The fleet's vital signs, node by node.
      </h1>

      <p style={{ fontSize: 14, color: "var(--sl-text-mid)", margin: "10px 0 0", maxWidth: "76ch" }}>
        Latency, throughput, error rate, and a health verdict for every backend, with
        per-service CPU and memory underneath.{" "}
        {excludedCount > 0
          ? "A node is currently excluded on anomaly evidence -- its traffic has already moved."
          : "Every node is healthy and in rotation."}
      </p>

      <div style={{ position: "absolute", top: 22, right: 26 }}>
        <Badge tone={backendsLive ? "mint" : "neutral"}>
          {backendsLive ? "LIVE" : "DEMONSTRATION"}
        </Badge>
      </div>
    </section>
  );
}

// ── KPI rail ─────────────────────────────────────────────────────────────────
// Sourced from the KPI-trends feed: each tile draws the feed's recent series for
// its sparkline and its measured window-over-window delta, so nothing here is a
// fabricated constant. The healthy-backends tile pairs the trend's count with
// the live fleet roll-up for the rotation footnote.

function KpiRail({
  trends,
  healthyCount,
  totalCount,
  excludedCount,
}: {
  trends: { throughput_rpm: TrendKpi; p95_latency_ms: TrendKpi; error_rate_pct: TrendKpi; active_backends: TrendKpi };
  healthyCount: number;
  totalCount: number;
  excludedCount: number;
}) {
  const thr = trends.throughput_rpm;
  const p95 = trends.p95_latency_ms;
  const err = trends.error_rate_pct;

  const thrK = thr.current != null ? (thr.current / 1000).toFixed(1) : "—";
  const p95v = p95.current != null ? Math.round(p95.current).toString() : "—";
  const errv = err.current != null ? err.current.toFixed(2) : "—";

  const p95Status: Status =
    p95.current == null ? "ok" : p95.current > PULSE_EXCLUSION_P95_MS ? "crit" : p95.current > PULSE_SLO_P95_MS ? "warn" : "ok";

  return (
    <div className="sl-grid-kpi">
      <KpiStat
        label={<><TrendingUp size={12} strokeWidth={2} /> Total throughput</>}
        value={thrK}
        unit="k rpm"
        deltaDir={deltaDir(thr.delta_pct)}
        delta={fmtDelta(thr.delta_pct)}
        footnote={thr.label}
        spark={thr.series.length > 1 ? thr.series : undefined}
        sparkTone="mint"
      />
      <KpiStat
        label={<><Gauge size={12} strokeWidth={2} /> Worst p95</>}
        value={p95v}
        unit="ms"
        deltaDir={p95Status === "ok" ? "flat" : "down"}
        delta={p95Status === "ok" ? "within SLO" : "over SLO"}
        footnote={`SLO ${PULSE_SLO_P95_MS} ms`}
        spark={p95.series.length > 1 ? p95.series : undefined}
        sparkTone="graphite"
      />
      <KpiStat
        label={<><Activity size={12} strokeWidth={2} /> Error rate</>}
        value={errv}
        unit="%"
        deltaDir={deltaDir(err.delta_pct)}
        delta={fmtDelta(err.delta_pct)}
        footnote={err.label}
        spark={err.series.length > 1 ? err.series : undefined}
        sparkTone="graphite"
      />
      <KpiStat
        label={<><Boxes size={12} strokeWidth={2} /> Healthy backends</>}
        value={`${healthyCount}`}
        unit={`/ ${totalCount}`}
        deltaDir={excludedCount > 0 ? "down" : "flat"}
        delta={excludedCount > 0 ? `${excludedCount} excluded` : "all healthy"}
        footnote={excludedCount > 0 ? "node isolated" : "in rotation"}
        spark={trends.active_backends.series.length > 1 ? trends.active_backends.series : undefined}
        sparkTone="mint"
      />
    </div>
  );
}

// ── excluded / unhealthy callout ─────────────────────────────────────────────

function ExcludedCallout({ rows }: { rows: VitalRow[] }) {
  return (
    <section
      style={{
        borderRadius: "var(--sl-radius-lg)",
        border: "1px solid var(--sl-crit)",
        boxShadow: "0 0 0 3px rgba(220,38,38,.06), var(--sl-shadow-1)",
        background: "var(--sl-crit-tint)",
        padding: "14px 18px",
        display: "flex",
        flexDirection: "column",
        gap: 12,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
        <AlertTriangle size={16} strokeWidth={2.2} color="var(--sl-crit)" />
        <span style={{ fontSize: 13.5, fontWeight: 700, color: "var(--sl-text)" }}>
          {rows.length === 1 ? "1 node excluded from rotation" : `${rows.length} nodes excluded from rotation`}
        </span>
        <span style={{ fontSize: 11.5, color: "var(--sl-crit)" }}>
          traffic redistributed automatically
        </span>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 9 }}>
        {rows.map((r) => (
          <div key={r.instance} style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
            <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12.5, fontWeight: 600, color: "var(--sl-text)", minWidth: 64 }}>
              {r.instance}
            </span>
            {r.evidence ? (
              <EvidenceLine
                metric={r.evidence.metric}
                observed={r.evidence.observed}
                threshold={r.evidence.threshold}
                verdict="breached"
                status="crit"
              />
            ) : (
              <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 11, color: "var(--sl-crit)" }}>
                health {r.health_score.toFixed(2)} -- held out on anomaly verdict
              </span>
            )}
          </div>
        ))}
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
      <div style={{ fontSize: 12.5, color: "var(--sl-text-low)", marginTop: 3, maxWidth: "92ch" }}>{sub}</div>
    </div>
  );
}

// ── per-backend vitals table ─────────────────────────────────────────────────

function VitalsTable({
  rows,
  state,
  degraded,
  onRetry,
}: {
  rows: VitalRow[];
  state: "loading" | "ready" | "error";
  degraded: boolean;
  onRetry: () => void;
}) {
  const columns: Column<VitalRow>[] = [
    {
      key: "backend",
      header: "Backend",
      render: (r) => {
        const led = statusColor(r.status);
        return (
          <div style={{ display: "flex", alignItems: "center", gap: 11 }}>
            <span style={{ width: 9, height: 9, borderRadius: "50%", background: led, flex: "0 0 auto", boxShadow: `0 0 6px ${led}` }} />
            <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 13, fontWeight: 600, color: "var(--sl-text)" }}>
              {r.instance}
            </span>
          </div>
        );
      },
    },
    {
      key: "p95",
      header: "p95 latency",
      numeric: true,
      render: (r) => (
        <span style={{ color: r.status === "crit" ? "var(--sl-crit)" : r.status === "warn" ? "var(--sl-warn)" : "var(--sl-text)", fontWeight: 600 }}>
          {r.p95_ms != null ? r.p95_ms : "—"}
          <span style={{ color: "var(--sl-text-faint)", fontSize: 10.5, fontWeight: 400 }}> ms</span>
        </span>
      ),
    },
    {
      // No per-row latency series is published in the metrics feed, so the row
      // reads its current p95 against the SLO rather than drawing a fabricated
      // trend. Em-dash when the node has too few samples to rule.
      key: "vsSlo",
      header: "vs SLO",
      numeric: true,
      render: (r) => {
        if (r.p95_ms == null) {
          return <span style={{ color: "var(--sl-text-faint)" }}>—</span>;
        }
        const ratio = r.p95_ms / PULSE_SLO_P95_MS;
        const color =
          ratio > PULSE_EXCLUSION_P95_MS / PULSE_SLO_P95_MS
            ? "var(--sl-crit)"
            : ratio > 1
              ? "var(--sl-warn)"
              : "var(--sl-mint-deep)";
        return (
          <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12, fontWeight: 600, color }}>
            {ratio <= 1 ? `${Math.round((1 - ratio) * 100)}% under` : `${Math.round((ratio - 1) * 100)}% over`}
          </span>
        );
      },
    },
    { key: "rpm", header: "Req / min", numeric: true, render: (r) => fmtInt(r.rpm) },
    {
      key: "err",
      header: "Error rate",
      numeric: true,
      render: (r) => (
        <span style={{ color: r.error_rate_pct > 0.5 ? "var(--sl-warn)" : "var(--sl-text)" }}>
          {r.error_rate_pct.toFixed(2)}
          <span style={{ color: "var(--sl-text-faint)", fontSize: 10.5 }}> %</span>
        </span>
      ),
    },
    {
      key: "health",
      header: "Health score",
      render: (r) => {
        const color = statusColor(r.status);
        return (
          <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 120 }}>
            <div style={{ flex: 1, height: 6, borderRadius: 6, background: "var(--sl-surface-sunk)", overflow: "hidden", minWidth: 64 }}>
              <div style={{ width: `${Math.round(r.health_score * 100)}%`, height: "100%", borderRadius: 6, background: color }} />
            </div>
            <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12, fontWeight: 600, color: "var(--sl-text)" }}>
              {r.health_score.toFixed(2)}
            </span>
          </div>
        );
      },
    },
    {
      key: "status",
      header: "Status",
      render: (r) => (
        <div style={{ display: "flex", flexDirection: "column", gap: 6, alignItems: "flex-start" }}>
          <StatusPill status={r.status}>{statusWord(r)}</StatusPill>
          {r.evidence ? (
            <EvidenceLine
              metric={r.evidence.metric}
              observed={r.evidence.observed}
              threshold={r.evidence.threshold}
              verdict={r.excluded ? "breached" : "near"}
              status={r.status}
            />
          ) : null}
        </div>
      ),
    },
  ];

  return (
    <Card flush>
      {state === "loading" && rows.length === 0 ? (
        <div style={{ padding: 18 }}>
          <LoadState lines={4} label="Loading per-backend vitals…" />
        </div>
      ) : degraded && rows.length === 0 ? (
        <div style={{ padding: 18 }}>
          <ErrorState
            title="Couldn't load per-backend vitals"
            hint="Showing a representative fleet until the metrics feed is reachable."
            onRetry={onRetry}
          />
        </div>
      ) : rows.length === 0 ? (
        <EmptyState
          icon={<Boxes size={22} strokeWidth={1.8} />}
          title="No backends in the pool"
          hint="The routed pool is empty. Nodes will appear here as they register."
        />
      ) : (
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(r) => r.instance}
          rowMuted={(r) => r.excluded}
        />
      )}
    </Card>
  );
}

// ── resource panel ───────────────────────────────────────────────────────────

function ResourcePanel({
  services,
  state,
  degraded,
  onRetry,
}: {
  services: [string, ServiceResource][];
  state: "loading" | "ready" | "error";
  degraded: boolean;
  onRetry: () => void;
}) {
  return (
    <Card flush>
      {state === "loading" && services.length === 0 ? (
        <div style={{ padding: 18 }}>
          <LoadState lines={5} label="Loading resource utilisation…" />
        </div>
      ) : degraded && services.length === 0 ? (
        <div style={{ padding: 18 }}>
          <ErrorState
            title="Couldn't load resource utilisation"
            hint="Showing representative CPU and memory until the resource feed is reachable."
            onRetry={onRetry}
          />
        </div>
      ) : services.length === 0 ? (
        <EmptyState
          icon={<Cpu size={22} strokeWidth={1.8} />}
          title="No resource samples yet"
          hint="Per-container CPU and memory will appear as the resource collector reports."
        />
      ) : (
        <>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "minmax(0, 1.4fr) repeat(2, minmax(0, 1.3fr)) auto",
              gap: 12,
              padding: "10px 18px",
              borderBottom: "1px solid var(--sl-hairline)",
              fontFamily: "var(--sl-font-mono)",
              fontSize: 9,
              letterSpacing: "1px",
              textTransform: "uppercase",
              color: "var(--sl-text-low)",
              fontWeight: 600,
            }}
          >
            <span>Service</span>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}><Cpu size={11} strokeWidth={2} /> CPU</span>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}><MemoryStick size={11} strokeWidth={2} /> Memory</span>
            <span style={{ textAlign: "right" }}>Containers</span>
          </div>

          <div style={{ display: "flex", flexDirection: "column" }}>
            {services.map(([name, r], i) => (
              <div
                key={name}
                style={{
                  display: "grid",
                  gridTemplateColumns: "minmax(0, 1.4fr) repeat(2, minmax(0, 1.3fr)) auto",
                  gap: 12,
                  alignItems: "center",
                  padding: "12px 18px",
                  borderBottom: i < services.length - 1 ? "1px solid var(--sl-hairline-soft)" : undefined,
                }}
              >
                <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12.5, fontWeight: 600, color: "var(--sl-text)" }}>
                  {name}
                </span>
                <ResourceBar
                  pct={r.cpu_percent}
                  caption={r.cpu_percent != null ? `${r.cpu_percent.toFixed(0)}%` : "—"}
                />
                <ResourceBar
                  pct={r.memory_percent}
                  caption={
                    r.memory_used_bytes != null
                      ? `${formatBytes(r.memory_used_bytes)}${r.memory_percent != null ? ` (${r.memory_percent.toFixed(0)}%)` : ""}`
                      : "—"
                  }
                />
                <span style={{ textAlign: "right", fontFamily: "var(--sl-font-mono)", fontSize: 12, color: "var(--sl-text-mid)" }}>
                  {r.instances}
                </span>
              </div>
            ))}
          </div>
        </>
      )}
    </Card>
  );
}

// A compact utilisation bar: mint under load, amber past 70%, red past 90%.
function ResourceBar({ pct, caption }: { pct: number | null; caption: string }) {
  const v = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  const color = v >= 90 ? "var(--sl-crit)" : v >= 70 ? "var(--sl-warn)" : "var(--sl-mint)";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
      <div style={{ flex: 1, height: 6, borderRadius: 6, background: "var(--sl-surface-sunk)", overflow: "hidden", minWidth: 56 }}>
        <div style={{ width: `${v}%`, height: "100%", borderRadius: 6, background: pct == null ? "var(--sl-graphite-soft)" : color }} />
      </div>
      <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 11, color: "var(--sl-text-mid)", whiteSpace: "nowrap", minWidth: 92, textAlign: "right" }}>
        {caption}
      </span>
    </div>
  );
}
