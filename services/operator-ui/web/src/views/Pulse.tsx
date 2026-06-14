// ============================================================================
// Pulse -- per-backend vitals + resource utilisation
// ----------------------------------------------------------------------------
// The fleet's vital signs, node by node: p95 latency with a live trend, request
// throughput, error rate, a health-score bar, and a status pill, with the
// excluded / unhealthy node called out on its evidence. Below, a resource panel
// rolls per-container CPU and memory up by service. Cluster roll-up KPIs sit at
// the top. Every panel tries the live API and falls back to sample data on
// error or timeout, so the page renders complete with no backend running, and
// the shell sample/live indicator reflects which path each panel took.
// ============================================================================

import { useEffect, useMemo, useState } from "react";
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
  type BackendMetrics,
  type BackendStat,
  type ResourcesResponse,
  type ServiceResource,
} from "../api";
import {
  Badge,
  Card,
  DataTable,
  EvidenceLine,
  KpiStat,
  Sparkline,
  StatusPill,
  type Column,
  type Status,
} from "../ui";
import { loadWithFallback, type DataSource } from "./loader";
import { useShell } from "./shell-context";
import {
  PULSE_EXCLUSION_P95_MS,
  PULSE_SAMPLE_BACKENDS,
  PULSE_SAMPLE_RESOURCES,
  PULSE_SLO_P95_MS,
  PULSE_SPARK_LATENCY,
} from "./_samplePulse";

const REFRESH_MS = 20_000;

const fmtInt = (n: number) => n.toLocaleString("en-US", { maximumFractionDigits: 0 });

// ── derived per-backend vital row ────────────────────────────────────────────
// A BackendStat enriched with a verdict the view can style: a health score
// (0..1), an ok/warn/crit status, an excluded flag, the evidence behind a
// breach, and a short latency trend for the row sparkline.

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
  spark: number[];
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

  const spark = PULSE_SPARK_LATENCY[b.instance] ?? (b.p95_ms != null ? [p95, p95] : [0, 0]);

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
    spark,
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

// ── component ────────────────────────────────────────────────────────────────

export default function Pulse() {
  const { setDataSource, setPlane, setPlaneNodes } = useShell();

  const [backends, setBackends] = useState<BackendMetrics>(PULSE_SAMPLE_BACKENDS);
  const [resources, setResources] = useState<ResourcesResponse>(PULSE_SAMPLE_RESOURCES);
  const [backendsLive, setBackendsLive] = useState(false);

  useEffect(() => {
    let cancelled = false;

    async function tick() {
      const [bkR, resR] = await Promise.all([
        loadWithFallback(() => api.getBackendMetrics(), PULSE_SAMPLE_BACKENDS),
        loadWithFallback(() => api.getResources(), PULSE_SAMPLE_RESOURCES),
      ]);

      if (cancelled) return;

      setBackends(bkR.value);
      setResources(resR.value);
      setBackendsLive(bkR.source === "live");

      const sources: DataSource[] = [bkR.source, resR.source];
      const anySample = sources.some((s) => s === "sample");
      const allSample = sources.every((s) => s === "sample");
      setDataSource(anySample ? "sample" : "live");
      setPlane(allSample ? "bad" : anySample ? "warn" : "ok");
      setPlaneNodes(bkR.value.backends.length);
    }

    tick();
    const id = window.setInterval(tick, REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [setDataSource, setPlane, setPlaneNodes]);

  // ── derived vitals + roll-up ───────────────────────────────────────────────

  const rows = useMemo(
    () =>
      backends.backends.map((b) => toVitalRow(b, PULSE_SLO_P95_MS, PULSE_EXCLUSION_P95_MS)),
    [backends],
  );

  const totalCount = rows.length;
  const excludedRows = rows.filter((r) => r.excluded);
  const healthyCount = rows.filter((r) => r.status === "ok").length;
  const inRotation = rows.filter((r) => !r.excluded);

  const totalRpm = inRotation.reduce((sum, r) => sum + r.rpm, 0);
  // Worst p95 across the nodes still in rotation -- the SLO-relevant signal.
  const worstP95 = inRotation.reduce((m, r) => Math.max(m, r.p95_ms ?? 0), 0);
  // Request-weighted cluster error rate across the routed pool.
  const weightedErr =
    backends.aggregate?.error_rate_pct ??
    (totalRpm > 0
      ? inRotation.reduce((s, r) => s + r.error_rate_pct * r.rpm, 0) / totalRpm
      : 0);

  const services = useMemo(() => {
    const byService = resourcesByService(resources);
    return Object.entries(byService).sort((a, b) => (b[1].cpu_percent ?? 0) - (a[1].cpu_percent ?? 0));
  }, [resources]);

  // ── render ─────────────────────────────────────────────────────────────────

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 22 }}>
      <Header backendsLive={backendsLive} excludedCount={excludedRows.length} />

      <KpiRail
        totalRpm={totalRpm}
        worstP95={worstP95}
        errorRate={weightedErr}
        healthyCount={healthyCount}
        totalCount={totalCount}
        excludedCount={excludedRows.length}
      />

      {excludedRows.length > 0 ? <ExcludedCallout rows={excludedRows} /> : null}

      <SectionHead
        title="Per-backend vitals"
        sub={`${totalCount} nodes. Each row is a live vital reading -- p95 with its trend, throughput, error rate, and a health verdict. ${
          excludedRows.length > 0
            ? "An excluded node is held out of rotation; its traffic redistributes automatically."
            : "All nodes are in rotation."
        }`}
      />

      <VitalsTable rows={rows} />

      <SectionHead
        title="Resource utilisation"
        sub="Per-service CPU and memory across the running containers. The test-backend fleet is summed across its replicas; everything else is a single container."
      />

      <ResourcePanel services={services} />
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
          {backendsLive ? "LIVE" : "SAMPLE DATA"}
        </Badge>
      </div>
    </section>
  );
}

// ── KPI rail ─────────────────────────────────────────────────────────────────

function KpiRail({
  totalRpm,
  worstP95,
  errorRate,
  healthyCount,
  totalCount,
  excludedCount,
}: {
  totalRpm: number;
  worstP95: number;
  errorRate: number;
  healthyCount: number;
  totalCount: number;
  excludedCount: number;
}) {
  const p95Status: Status = worstP95 > PULSE_EXCLUSION_P95_MS ? "crit" : worstP95 > PULSE_SLO_P95_MS ? "warn" : "ok";
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 16 }}>
      <KpiStat
        label={<><TrendingUp size={12} strokeWidth={2} /> Total throughput</>}
        value={(totalRpm / 1000).toFixed(1)}
        unit="k rpm"
        footnote={`across ${totalCount - excludedCount} routed nodes`}
        spark={PULSE_SPARK_LATENCY["api-01"].map((_, i) =>
          PULSE_SPARK_LATENCY["api-01"][i] / 8 + 480 - i * 2,
        )}
        sparkTone="mint"
      />
      <KpiStat
        label={<><Gauge size={12} strokeWidth={2} /> Worst p95</>}
        value={Math.round(worstP95).toString()}
        unit="ms"
        deltaDir={p95Status === "ok" ? "flat" : "down"}
        delta={p95Status === "ok" ? "within SLO" : "over SLO"}
        footnote={`SLO ${PULSE_SLO_P95_MS} ms`}
        spark={PULSE_SPARK_LATENCY["api-03"]}
        sparkTone="graphite"
      />
      <KpiStat
        label={<><Activity size={12} strokeWidth={2} /> Error rate</>}
        value={errorRate.toFixed(2)}
        unit="%"
        deltaDir={errorRate > 0.5 ? "down" : "flat"}
        delta={errorRate > 0.5 ? "elevated" : "nominal"}
        footnote="request-weighted"
        spark={PULSE_SPARK_LATENCY["api-05"].map((v) => v / 400)}
        sparkTone="graphite"
      />
      <KpiStat
        label={<><Boxes size={12} strokeWidth={2} /> Healthy backends</>}
        value={`${healthyCount}`}
        unit={`/ ${totalCount}`}
        deltaDir={excludedCount > 0 ? "down" : "flat"}
        delta={excludedCount > 0 ? `${excludedCount} excluded` : "all healthy"}
        footnote={excludedCount > 0 ? "node isolated" : "in rotation"}
        spark={PULSE_SPARK_LATENCY["api-06"].map((v) => v / 20)}
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

function VitalsTable({ rows }: { rows: VitalRow[] }) {
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
      key: "trend",
      header: "Latency trend",
      render: (r) => (
        <div style={{ display: "flex", justifyContent: "flex-start" }}>
          <Sparkline data={r.spark} tone={r.status === "ok" ? "graphite" : "mint"} width={96} height={24} />
        </div>
      ),
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
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.instance}
        rowMuted={(r) => r.excluded}
      />
    </Card>
  );
}

// ── resource panel ───────────────────────────────────────────────────────────

function ResourcePanel({ services }: { services: [string, ServiceResource][] }) {
  return (
    <Card flush>
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
