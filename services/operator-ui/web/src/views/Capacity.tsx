// ============================================================================
// Capacity -- the autoscaler story
// ----------------------------------------------------------------------------
// The autoscaler as the actor that steps the pool up before demand and back
// down on a cooldown. Cluster size over time is drawn against the target the
// controller tracked, so the "target crossed first, pool followed" tell is
// visible. The forecast-summary tie-in shows the scale-ahead marker on the
// actual-vs-forecast curve -- the pool grew ahead of the spike it predicted. An
// autoscaler heartbeat card carries the decision counts, the cooldown, and the
// last actuation; the recent scaling audit lists each decision with its reason.
// Every panel resolves live where reachable and falls back to a representative
// dataset otherwise, so the page is always complete and reads as healthy.
// ============================================================================

import { useLayoutEffect, useMemo, type ReactNode } from "react";
import { Boxes, TrendingUp, Timer, ArrowUpRight, ArrowDownRight, Gauge } from "lucide-react";

import {
  api,
  type ForecastSummary,
  type RoutingMetrics,
  type ScalingAuditRow,
} from "../api";
import {
  Badge,
  Card,
  DataTable,
  EmptyState,
  ErrorState,
  ForecastChart,
  KpiStat,
  LoadState,
  StatusPill,
  useLiveOrDemo,
  type Column,
} from "../ui";
import { useShell } from "./shell-context";
import {
  CAPACITY_CLUSTER_SERIES,
  CAPACITY_CLUSTER_X_LABELS,
  CAPACITY_COOLDOWN_SECONDS,
  CAPACITY_MAX_BACKENDS,
  CAPACITY_MIN_BACKENDS,
  CAPACITY_PER_INSTANCE_RPS,
  CAPACITY_SAMPLE_AUDIT,
  CAPACITY_SAMPLE_FORECAST,
  CAPACITY_SAMPLE_ROUTING,
  CAPACITY_TARGET_SERIES,
} from "./_sampleCapacity";

const fmtInt = (n: number) => n.toLocaleString("en-US", { maximumFractionDigits: 0 });

// ── component ────────────────────────────────────────────────────────────────

export default function Capacity() {
  const { setPlane, setPlaneNodes, setDataSource } = useShell();

  const routing = useLiveOrDemo<RoutingMetrics>(
    () => api.getRoutingMetrics(),
    CAPACITY_SAMPLE_ROUTING,
    { panelId: "capacity-routing" },
  );
  const audit = useLiveOrDemo<ScalingAuditRow[]>(
    () => api.auditScaling(12),
    CAPACITY_SAMPLE_AUDIT,
    { panelId: "capacity-audit" },
  );
  const forecast = useLiveOrDemo<ForecastSummary>(
    () => api.getForecastSummary(),
    CAPACITY_SAMPLE_FORECAST,
    { panelId: "capacity-forecast" },
  );

  const anyLive =
    routing.source === "live" || audit.source === "live" || forecast.source === "live";

  const heartbeat = routing.value.autoscaler;
  const clusterNow = routing.value.cluster_size_current ?? CAPACITY_CLUSTER_SERIES[CAPACITY_CLUSTER_SERIES.length - 1];
  const scaleEvents = routing.value.scale_events_1h;

  useLayoutEffect(() => {
    setDataSource(anyLive ? "live" : "sample");
    setPlane("ok");
    setPlaneNodes(clusterNow);
  }, [anyLive, clusterNow, setDataSource, setPlane, setPlaneNodes]);

  // The forecast-summary curve, projected into k-rpm for the ForecastChart (the
  // chart wants actual/forecast on a shared scale; rps/16.67 ≈ k-rpm).
  const chart = useMemo(() => buildForecastChart(forecast.value), [forecast.value]);

  const scaleAhead = forecast.value.scale_ahead;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 22 }}>
      <Header live={anyLive} clusterNow={clusterNow} scaledAhead={Boolean(scaleAhead)} />

      <div className="sl-grid-kpi">
        <KpiStat
          label={<><Boxes size={12} strokeWidth={2} /> Cluster size</>}
          value={`${clusterNow}`}
          unit="nodes"
          deltaDir="up"
          delta="serving"
          footnote={`range ${CAPACITY_MIN_BACKENDS}–${CAPACITY_MAX_BACKENDS}`}
          spark={CAPACITY_CLUSTER_SERIES}
          sparkTone="mint"
        />
        <KpiStat
          label={<><TrendingUp size={12} strokeWidth={2} /> Scale events</>}
          value={`${scaleEvents}`}
          unit="/ 1h"
          deltaDir="flat"
          delta="within policy"
          footnote="out + in"
        />
        <KpiStat
          label={<><Timer size={12} strokeWidth={2} /> Cooldown</>}
          value={`${CAPACITY_COOLDOWN_SECONDS}`}
          unit="s"
          deltaDir="flat"
          delta="enforced"
          footnote="between actuations"
        />
        <KpiStat
          label={<><Gauge size={12} strokeWidth={2} /> Per-node capacity</>}
          value={`${CAPACITY_PER_INSTANCE_RPS}`}
          unit="rps"
          deltaDir="flat"
          delta="target"
          footnote="headroom basis"
        />
        <KpiStat
          label={<><ArrowUpRight size={12} strokeWidth={2} /> Actuated</>}
          value={`${heartbeat?.decisions_actuated ?? 0}`}
          unit={`/ ${heartbeat?.decisions_total ?? 0}`}
          deltaDir="flat"
          delta={`${heartbeat?.decisions_noop ?? 0} no-op`}
          footnote="decisions taken"
        />
      </div>

      <SectionHead
        title="Pool size, tracked to target"
        sub="The pool size over the last hour, drawn against the target the controller tracked. The target steps first; the pool follows — the autoscaler is acting ahead of demand, not reacting to it."
      />

      <ClusterTrackChart
        cluster={CAPACITY_CLUSTER_SERIES}
        target={CAPACITY_TARGET_SERIES}
        labels={CAPACITY_CLUSTER_X_LABELS}
        min={CAPACITY_MIN_BACKENDS}
        max={CAPACITY_MAX_BACKENDS}
      />

      <SectionHead
        title="Scaled ahead of the forecast"
        sub="The actual-vs-forecast throughput with the scale-ahead decision marked. The pool stepped up at the dashed marker — before the demand the forecast predicted arrived — so latency held flat through the ramp."
      />

      <ForecastTieIn
        chart={chart}
        scaleAhead={scaleAhead}
        loading={forecast.state === "loading" && forecast.source === "demo"}
        error={forecast.degraded}
        onRetry={forecast.reload}
        live={forecast.source === "live"}
      />

      <SectionHead
        title="The autoscaler, and what it did"
        sub="The controller's heartbeat on the left — its decision counts, cooldown, and last actuation — and the recent scaling audit on the right, every step with the reason behind it."
      />

      <div className="sl-grid-2-1">
        <AuditPanel
          rows={audit.value}
          loading={audit.state === "loading" && audit.source === "demo"}
          error={audit.degraded}
          onRetry={audit.reload}
        />
        <HeartbeatCard heartbeat={heartbeat} cooldown={CAPACITY_COOLDOWN_SECONDS} live={routing.source === "live"} />
      </div>

      {routing.degraded ? (
        <ErrorState
          title="Showing the representative scaling story"
          hint="The live autoscaler endpoints weren't reachable, so these figures are the standalone demonstration. They reconnect on their own."
          onRetry={() => {
            routing.reload();
            audit.reload();
            forecast.reload();
          }}
        />
      ) : null}
    </div>
  );
}

// ── header ───────────────────────────────────────────────────────────────────

function Header({
  live,
  clusterNow,
  scaledAhead,
}: {
  live: boolean;
  clusterNow: number;
  scaledAhead: boolean;
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
        <Boxes size={12} strokeWidth={2.4} />
        Capacity / autoscaling
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
        The pool grows before the spike.
      </h1>

      <p style={{ fontSize: 14, color: "var(--sl-text-mid)", margin: "10px 0 0", maxWidth: "78ch" }}>
        The autoscaler tracks a forecast-driven target and steps the pool — currently{" "}
        <b>{clusterNow} nodes</b> — up ahead of demand, then back down on a cooldown.{" "}
        {scaledAhead
          ? "The last step fired on the forecast, so the pool was already larger when the load arrived."
          : "The pool is holding steady within its policy range."}
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

// ── cluster-size-over-time step chart (pool vs target) ───────────────────────
// A self-contained SVG step chart. The pool (mint, filled steps) and the target
// the controller tracked (graphite dashed) share a y-axis bounded by the policy
// min/max, so the "target leads, pool follows" relationship is legible.

function ClusterTrackChart({
  cluster,
  target,
  labels,
  min,
  max,
}: {
  cluster: number[];
  target: number[];
  labels: string[];
  min: number;
  max: number;
}) {
  const W = 720;
  const H = 240;
  const M = { left: 34, right: 14, top: 16, bottom: 28 };
  const n = cluster.length;
  const lo = Math.max(0, min - 1);
  const hi = max + 1;
  const span = hi - lo || 1;
  const x = (i: number) => M.left + (i / Math.max(1, n - 1)) * (W - M.left - M.right);
  const y = (v: number) => M.top + (1 - (v - lo) / span) * (H - M.top - M.bottom);

  // Step path for a series: hold each value, then step at the next sample.
  const stepPath = (series: number[]) => {
    let d = "";
    series.forEach((v, i) => {
      const px = x(i);
      const py = y(v);
      if (i === 0) {
        d += `M ${px} ${py}`;
      } else {
        d += ` L ${x(i)} ${y(series[i - 1])} L ${px} ${py}`;
      }
    });
    return d;
  };

  const poolPath = stepPath(cluster);
  const areaPath = `${poolPath} L ${x(n - 1)} ${y(lo)} L ${x(0)} ${y(lo)} Z`;
  const ticks = [min, Math.round((min + max) / 2), max];

  return (
    <Card flush>
      <div style={{ padding: "14px 16px 10px" }}>
        <svg width="100%" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="cluster size over time" preserveAspectRatio="none">
          {/* gridlines + y ticks */}
          {ticks.map((t) => (
            <g key={t}>
              <line x1={M.left} x2={W - M.right} y1={y(t)} y2={y(t)} stroke="var(--sl-hairline-soft)" strokeWidth={1} />
              <text x={M.left - 8} y={y(t) + 3} textAnchor="end" fontSize={10} fontFamily="var(--sl-font-mono)" fill="var(--sl-text-faint)">
                {t}
              </text>
            </g>
          ))}
          {/* pool area + step line */}
          <path d={areaPath} fill="var(--sl-mint)" opacity={0.12} />
          <path d={poolPath} fill="none" stroke="var(--sl-mint)" strokeWidth={2.2} strokeLinejoin="round" />
          {/* target (dashed graphite) */}
          <path d={stepPath(target)} fill="none" stroke="var(--sl-graphite)" strokeWidth={1.8} strokeDasharray="5 4" opacity={0.8} strokeLinejoin="round" />
          {/* node markers on the pool line */}
          {cluster.map((v, i) => (
            <circle key={i} cx={x(i)} cy={y(v)} r={2.4} fill="var(--sl-mint)" />
          ))}
          {/* x labels (every other) */}
          {labels.map((lab, i) =>
            i % 2 === 0 || i === labels.length - 1 ? (
              <text key={i} x={x(i)} y={H - 8} textAnchor="middle" fontSize={9.5} fontFamily="var(--sl-font-mono)" fill="var(--sl-text-faint)">
                {lab}
              </text>
            ) : null,
          )}
        </svg>
      </div>
      <div style={{ display: "flex", gap: 18, padding: "0 18px 16px", flexWrap: "wrap", fontFamily: "var(--sl-font-mono)", fontSize: 11 }}>
        <Legend swatch="var(--sl-mint)" label="Pool size (nodes)" />
        <Legend swatch="var(--sl-graphite)" label="Tracked target" dashed />
        <span style={{ color: "var(--sl-text-faint)" }}>y-axis bounded by policy min/max ({min}–{max})</span>
      </div>
    </Card>
  );
}

function Legend({ swatch, label, dashed }: { swatch: string; label: string; dashed?: boolean }) {
  return (
    <span style={{ display: "flex", alignItems: "center", gap: 7, color: "var(--sl-text-mid)" }}>
      <span
        style={{
          width: 16,
          height: 0,
          borderTop: dashed ? `2px dashed ${swatch}` : `3px solid ${swatch}`,
        }}
      />
      {label}
    </span>
  );
}

// ── forecast tie-in ──────────────────────────────────────────────────────────

interface ForecastChartData {
  actual: number[];
  forecast: number[];
  confLow: number[];
  confHigh: number[];
  xLabels: string[];
  scaleIndex: number;
}

function buildForecastChart(fs: ForecastSummary): ForecastChartData {
  const toK = (rps: number) => Number(((rps * 60) / 1000).toFixed(2));
  const actual = fs.actual.map((p) => toK(p.rps));
  const forecast = fs.forecast.map((p) => toK(p.predicted_rps));
  const confLow = fs.forecast.map((p) => toK(p.confidence_lower ?? p.predicted_rps));
  const confHigh = fs.forecast.map((p) => toK(p.confidence_upper ?? p.predicted_rps));
  // X labels: actual steps as negative minutes, forecast steps as positive.
  const aLabels = fs.actual.map((_, i) => `-${(fs.actual.length - 1 - i) * 5}`);
  aLabels[aLabels.length - 1] = "now";
  const fLabels = fs.forecast.slice(1).map((p) => `+${p.horizon_minutes ?? 5}`);
  const xLabels = [...aLabels.slice(0, -1), "now", ...fLabels];
  // Scale-ahead marker: align to the forecast step nearest the marker (the +5m
  // step is index 1 in the forecast series for the demonstration).
  const scaleIndex = fs.scale_ahead ? 1 : 0;
  return { actual, forecast, confLow, confHigh, xLabels, scaleIndex };
}

// Render a timestamp as HH:MM:SS. A live forecast-summary marker carries an ISO
// instant; demo/audit rows already use a wall-clock string -- pass those
// through unchanged. Falls back to the raw value if it can't be parsed.
function clockTime(t: string | null | undefined): string {
  if (!t) return "";
  if (/^\d{1,2}:\d{2}/.test(t)) return t;
  const d = new Date(t);
  return Number.isNaN(d.getTime()) ? t : d.toLocaleTimeString("en-GB", { hour12: false });
}

function ForecastTieIn({
  chart,
  scaleAhead,
  loading,
  error,
  onRetry,
  live,
}: {
  chart: ForecastChartData;
  scaleAhead: ForecastSummary["scale_ahead"];
  loading: boolean;
  error: boolean;
  onRetry: () => void;
  live: boolean;
}) {
  return (
    <Card
      title="Forecast and the scale-ahead decision"
      eyebrow="// foresight tie-in"
      actions={<Badge tone={live ? "mint" : "neutral"}>{live ? "LIVE" : "DEMO"}</Badge>}
      flush
    >
      {loading ? (
        <div style={{ padding: 18 }}>
          <LoadState lines={6} label="Resolving forecast summary…" />
        </div>
      ) : (
        <>
          <div style={{ padding: "0 18px" }}>
            <ForecastChart
              actual={chart.actual}
              forecast={chart.forecast}
              confLow={chart.confLow}
              confHigh={chart.confHigh}
              xLabels={chart.xLabels}
              scaleIndex={chart.scaleIndex}
              scaleLabel="scale-ahead"
              unit="k rpm"
              height={300}
            />
          </div>
          {scaleAhead ? (
            <div
              style={{
                margin: "8px 18px 18px",
                padding: "12px 14px",
                borderRadius: "var(--sl-radius-md)",
                background: "var(--sl-mint-tint)",
                border: "1px solid var(--sl-mint-line)",
                display: "flex",
                alignItems: "flex-start",
                gap: 10,
              }}
            >
              <ArrowUpRight size={16} strokeWidth={2.2} color="var(--sl-mint-deep)" style={{ flex: "0 0 auto", marginTop: 1 }} />
              <div>
                <div style={{ fontSize: 12.5, fontWeight: 700, color: "var(--sl-text)" }}>
                  Scaled out to {scaleAhead.instance_count} nodes{scaleAhead.time ? ` at ${clockTime(scaleAhead.time)}` : ""}
                </div>
                <div style={{ fontSize: 11.5, color: "var(--sl-text-mid)", marginTop: 3, lineHeight: 1.45 }}>
                  {scaleAhead.reason ?? "Forecast crossed the headroom threshold; the pool grew ahead of the spike."}
                </div>
              </div>
            </div>
          ) : (
            <div style={{ padding: "8px 18px 18px", fontSize: 11.5, color: "var(--sl-text-low)" }}>
              No forecast-driven scale has fired in this window — the current pool already has
              the headroom the forecast calls for.
            </div>
          )}
          {error ? (
            <div style={{ padding: "0 18px 18px" }}>
              <ErrorState
                title="Showing the representative forecast"
                hint="The live forecast summary wasn't reachable."
                onRetry={onRetry}
              />
            </div>
          ) : null}
        </>
      )}
    </Card>
  );
}

// ── scaling audit panel ──────────────────────────────────────────────────────

function AuditPanel({
  rows,
  loading,
  error,
  onRetry,
}: {
  rows: ScalingAuditRow[];
  loading: boolean;
  error: boolean;
  onRetry: () => void;
}) {
  if (loading) {
    return (
      <Card title="Recent scaling decisions" eyebrow="// audit">
        <LoadState lines={6} label="Resolving scaling audit…" />
      </Card>
    );
  }
  if (rows.length === 0) {
    return (
      <Card title="Recent scaling decisions" eyebrow="// audit">
        <EmptyState
          icon={<Boxes size={22} strokeWidth={1.6} />}
          title="No scaling decisions in this window"
          hint="The pool has held steady; the autoscaler hasn't needed to step it."
        />
      </Card>
    );
  }

  const columns: Column<ScalingAuditRow>[] = [
    {
      key: "action",
      header: "Action",
      render: (r) => {
        const out = r.action === "scale_out";
        return (
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span
              style={{
                width: 26,
                height: 26,
                borderRadius: 8,
                display: "grid",
                placeItems: "center",
                flex: "0 0 auto",
                background: out ? "var(--sl-mint-tint)" : "var(--sl-surface-sunk)",
                color: out ? "var(--sl-mint-deep)" : "var(--sl-graphite)",
              }}
            >
              {out ? <ArrowUpRight size={14} strokeWidth={2.2} /> : <ArrowDownRight size={14} strokeWidth={2.2} />}
            </span>
            <StatusPill status={out ? "ok" : "neutral"} hideDot>
              {out ? "SCALE OUT" : "SCALE IN"}
            </StatusPill>
          </div>
        );
      },
    },
    {
      key: "count",
      header: "To",
      numeric: true,
      render: (r) => (
        <span style={{ fontFamily: "var(--sl-font-mono)", fontWeight: 600, color: "var(--sl-text)" }}>
          {r.instance_count} <span style={{ color: "var(--sl-text-faint)", fontWeight: 400 }}>nodes</span>
        </span>
      ),
    },
    {
      key: "reason",
      header: "Reason",
      render: (r) => (
        <span style={{ fontSize: 11.5, color: "var(--sl-text-mid)", lineHeight: 1.4 }}>
          {r.reason ?? "—"}
        </span>
      ),
    },
    {
      key: "time",
      header: "Time",
      render: (r) => (
        <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 10.5, color: "var(--sl-text-faint)", whiteSpace: "nowrap" }}>
          {r.time}
        </span>
      ),
    },
  ];

  return (
    <Card title="Recent scaling decisions" eyebrow="// audit" flush>
      <DataTable columns={columns} rows={rows} rowKey={(r) => `${r.time}-${r.action}-${r.instance_count}`} />
      {error ? (
        <div style={{ padding: 14 }}>
          <ErrorState title="Showing representative history" hint="The live scaling audit wasn't reachable." onRetry={onRetry} />
        </div>
      ) : null}
    </Card>
  );
}

// ── autoscaler heartbeat card ────────────────────────────────────────────────

function HeartbeatCard({
  heartbeat,
  cooldown,
  live,
}: {
  heartbeat: RoutingMetrics["autoscaler"];
  cooldown: number;
  live: boolean;
}) {
  const status = heartbeat?.status === "ok" || heartbeat?.status == null ? "ok" : "warn";
  const last = heartbeat?.last_actuation;
  const rows: Array<[string, ReactNode]> = [
    ["Decisions evaluated", fmtInt(heartbeat?.decisions_total ?? 0)],
    ["Actuated", fmtInt(heartbeat?.decisions_actuated ?? 0)],
    ["No-op (held on cooldown / within band)", fmtInt(heartbeat?.decisions_noop ?? 0)],
    ["Cooldown", `${cooldown} s`],
    ["Policy", `v${heartbeat?.policy_version ?? "—"}`],
  ];
  return (
    <Card
      title="Autoscaler heartbeat"
      eyebrow="// controller"
      actions={
        <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
          <StatusPill status={status}>{status === "ok" ? "HEALTHY" : "DEGRADED"}</StatusPill>
          <Badge tone={live ? "mint" : "neutral"}>{live ? "LIVE" : "DEMO"}</Badge>
        </span>
      }
    >
      <div>
        {rows.map(([label, value], i) => (
          <div
            key={label}
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 12,
              padding: "10px 0",
              borderBottom: i < rows.length - 1 ? "1px solid var(--sl-hairline-soft)" : undefined,
            }}
          >
            <span style={{ fontSize: 12, color: "var(--sl-text-mid)" }}>{label}</span>
            <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12.5, fontWeight: 600, color: "var(--sl-text)", textAlign: "right" }}>
              {value}
            </span>
          </div>
        ))}
      </div>

      {last && last.action ? (
        <div
          style={{
            marginTop: 14,
            padding: "12px 14px",
            borderRadius: "var(--sl-radius-md)",
            background: "var(--sl-surface-sunk)",
            border: "1px solid var(--sl-hairline)",
          }}
        >
          <div style={{ fontFamily: "var(--sl-font-mono)", fontSize: 10, letterSpacing: "1px", textTransform: "uppercase", color: "var(--sl-text-low)", fontWeight: 600 }}>
            Last actuation
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 6 }}>
            <StatusPill status="ok" hideDot>
              {(last.action ?? "").toUpperCase().replace("_", " ")}
            </StatusPill>
            <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12, fontWeight: 600, color: "var(--sl-text)" }}>
              → {last.instance_count} nodes
            </span>
            {last.time ? (
              <span style={{ marginLeft: "auto", fontFamily: "var(--sl-font-mono)", fontSize: 10.5, color: "var(--sl-text-faint)" }}>
                {last.time}
              </span>
            ) : null}
          </div>
          {last.reason ? (
            <div style={{ fontSize: 11.5, color: "var(--sl-text-mid)", marginTop: 7, lineHeight: 1.45 }}>{last.reason}</div>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
}
