// ============================================================================
// Flightdeck -- the flagship live overview
// ----------------------------------------------------------------------------
// Tells the closed-loop story: forecast leading actual throughput, the KPI
// rail, the backend fleet with evidence on the excluded node, recent anomaly
// verdicts, and the decision stream. Every panel tries the live API and falls
// back to sample data on error or timeout, so the page renders complete with no
// backend running. The safe_mode kill switch is owned here and surfaced in the
// Topbar through the shell context.
// ============================================================================

import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  Activity,
  ArrowRight,
  BookOpen,
  Boxes,
  Gauge,
  ShieldCheck,
  TrendingUp,
} from "lucide-react";

import {
  api,
  type ActivityItem,
  type AlertItem,
  type BackendMetrics,
  type OpsMetrics,
  type Policy,
  type RelatedMetrics,
  type RoutingMetrics,
  type ThroughputResponse,
} from "../api";
import {
  Badge,
  Button,
  Card,
  DataTable,
  EvidenceLine,
  ForecastChart,
  KpiStat,
  StatusPill,
  Toggle,
  type Column,
  type Status,
} from "../ui";
import { loadWithFallback, type DataSource } from "./loader";
import { useShell } from "./shell-context";
import {
  SAMPLE_ACTIVITY,
  SAMPLE_ACTUAL,
  SAMPLE_ALERTS,
  SAMPLE_BACKENDS,
  SAMPLE_BACKEND_METRICS,
  SAMPLE_CONF_HIGH,
  SAMPLE_CONF_LOW,
  SAMPLE_FORECAST,
  SAMPLE_OPS,
  SAMPLE_POLICY,
  SAMPLE_RELATED,
  SAMPLE_ROUTING,
  SAMPLE_SCALE_INDEX,
  SAMPLE_SPARK,
  SAMPLE_THROUGHPUT,
  SAMPLE_X_LABELS,
  type SampleBackend,
} from "./sample";

const REFRESH_MS = 20_000;

// ── small formatting helpers ─────────────────────────────────────────────────

const fmtInt = (n: number) => n.toLocaleString("en-US", { maximumFractionDigits: 0 });

function statusOfBackend(b: SampleBackend): Status {
  if (b.excluded || b.status === "crit") return "crit";
  if (b.status === "warn") return "warn";
  return "ok";
}

function alertStatus(a: AlertItem): Status {
  if (a.severity === "critical" || a.status === "unhealthy") return "crit";
  if (a.severity === "warning" || a.status === "degraded") return "warn";
  return "ok";
}

function activityStatus(kind: ActivityItem["severity"]): Status {
  if (kind === "bad") return "crit";
  if (kind === "warn") return "warn";
  return "ok";
}

// Merge the api BackendMetrics shape with the richer sample fields (zone,
// health score, excluded flag, evidence). When on live data we synthesize a
// status and health score from the live measurements so the fleet table still
// renders its evidence styling.
function toFleetRows(
  metrics: BackendMetrics,
  fromSample: boolean,
  policySlo: number,
): SampleBackend[] {
  if (fromSample) {
    // Sample metrics map 1:1 to the rich SampleBackend list; reuse it directly.
    return metricsToSample(metrics);
  }
  const latencyThreshold = policySlo > 0 ? policySlo * 1.5 : 300;
  return metrics.backends.map((b) => {
    const p95 = b.p95_ms ?? 0;
    const err = b.error_rate_pct;
    const overLatency = b.p95_ms != null && b.p95_ms > latencyThreshold;
    const overError = err > 0.5;
    const status: SampleBackend["status"] = overLatency
      ? "crit"
      : overError
        ? "warn"
        : "ok";
    const health = Math.max(0, Math.min(1, 1 - (p95 / (latencyThreshold * 2)) - err / 100));
    return {
      instance: b.instance,
      zone: "",
      p95_ms: p95,
      rpm: b.rpm,
      error_rate_pct: err,
      health_score: Number(health.toFixed(2)),
      status,
      excluded: overLatency,
      evidence: overLatency
        ? { metric: "p95_latency_ms", observed: p95, threshold: Math.round(latencyThreshold) }
        : undefined,
    };
  });
}

// Rebuild the rich SampleBackend list from the BackendMetrics sample so the
// table always renders the prototype's zones / evidence when offline.
function metricsToSample(metrics: BackendMetrics): SampleBackend[] {
  // The sample BackendMetrics is derived from SAMPLE_BACKENDS, so look those up.
  // Falling back to a synthesized row keeps this safe if the shapes diverge.
  return metrics.backends.map((b) => {
    const rich = SAMPLE_BACKEND_LOOKUP[b.instance];
    if (rich) return rich;
    return {
      instance: b.instance,
      zone: "",
      p95_ms: b.p95_ms ?? 0,
      rpm: b.rpm,
      error_rate_pct: b.error_rate_pct,
      health_score: 0.9,
      status: "ok",
      excluded: false,
    };
  });
}

const SAMPLE_BACKEND_LOOKUP: Record<string, SampleBackend> = Object.fromEntries(
  SAMPLE_BACKENDS.map((b) => [b.instance, b]),
);

// ── component ────────────────────────────────────────────────────────────────

export default function Flightdeck() {
  const shell = useShell();

  const [ops, setOps] = useState<OpsMetrics>(SAMPLE_OPS);
  const [related, setRelated] = useState<RelatedMetrics>(SAMPLE_RELATED);
  const [throughput, setThroughput] = useState<ThroughputResponse>(SAMPLE_THROUGHPUT);
  const [routing, setRouting] = useState<RoutingMetrics>(SAMPLE_ROUTING);
  const [backends, setBackends] = useState<BackendMetrics>(SAMPLE_BACKEND_METRICS);
  const [alerts, setAlerts] = useState<AlertItem[]>(SAMPLE_ALERTS);
  const [activity, setActivity] = useState<ActivityItem[]>(SAMPLE_ACTIVITY);
  const [policy, setPolicy] = useState<Policy>(SAMPLE_POLICY);

  // Per-panel source flags; the page is "live" only if every panel is live.
  const [backendsLive, setBackendsLive] = useState<boolean>(false);

  const { setDataSource, setSafeMode, setPlane, setPlaneNodes } = shell;

  // ── data load (live, with sample fallback) ─────────────────────────────────
  useEffect(() => {
    let cancelled = false;

    async function tick() {
      const sources: DataSource[] = [];

      const results = await Promise.all([
        loadWithFallback(() => api.getOpsMetrics(), SAMPLE_OPS),
        loadWithFallback(() => api.getRelatedMetrics(), SAMPLE_RELATED),
        loadWithFallback(() => api.getThroughput(13), SAMPLE_THROUGHPUT),
        loadWithFallback(() => api.getRoutingMetrics(), SAMPLE_ROUTING),
        loadWithFallback(() => api.getBackendMetrics(), SAMPLE_BACKEND_METRICS),
        loadWithFallback(() => api.getAlerts(), SAMPLE_ALERTS),
        loadWithFallback(() => api.getActivity(8), SAMPLE_ACTIVITY),
        loadWithFallback(() => api.getPolicy(), SAMPLE_POLICY),
      ]);

      if (cancelled) return;

      const [
        opsR,
        relR,
        thrR,
        rouR,
        bkR,
        alR,
        acR,
        poR,
      ] = results;

      results.forEach((r) => sources.push(r.source));

      setOps(opsR.value);
      setRelated(relR.value);
      setThroughput(thrR.value);
      setRouting(rouR.value);
      setBackends(bkR.value);
      setBackendsLive(bkR.source === "live");
      setAlerts(alR.value);
      setActivity(acR.value);
      setPolicy(poR.value);
      // Only let a live policy reading drive the kill switch; offline we keep
      // the operator's manual choice so the periodic refresh can't revert it.
      if (poR.source === "live") setSafeMode(Boolean(poR.value.safe_mode));

      const anySample = sources.some((s) => s === "sample");
      const allSample = sources.every((s) => s === "sample");
      setDataSource(anySample ? "sample" : "live");
      setPlane(allSample ? "bad" : anySample ? "warn" : "ok");
      setPlaneNodes(rouR.value.cluster_size_current ?? opsR.value.services_total);
    }

    tick();
    const id = window.setInterval(tick, REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [setDataSource, setSafeMode, setPlane, setPlaneNodes]);

  // ── derived KPI readings ───────────────────────────────────────────────────

  const throughputRpm = ops.throughput_rpm ?? related.rps_current ?? 0;
  const p95 = related.p95_latency_ms ?? backends.aggregate?.p95_ms ?? 0;
  const slo = ops.policy_compliance_pct ?? related.slo_compliance_pct ?? 0;
  const errorRate = backends.aggregate?.error_rate_pct ?? 0;

  const fleet = useMemo(
    () => toFleetRows(backends, !backendsLive, policy.slo_p95_latency_ms),
    [backends, backendsLive, policy.slo_p95_latency_ms],
  );
  const activeCount = fleet.filter((b) => !b.excluded).length;
  const excludedCount = fleet.length - activeCount;

  // Forecast chart series. When live throughput is available, derive a short
  // forecast tail from the trend; otherwise use the sample series.
  const chart = useMemo(() => {
    if (backendsLive && throughput.buckets.length >= 4) {
      const actual = throughput.buckets.map((b) => Number((b.rpm / 1000).toFixed(2)));
      const last = actual[actual.length - 1];
      const slope = last - actual[actual.length - 2];
      const f1 = Number((last + slope * 1.4).toFixed(2));
      const f2 = Number((last + slope * 3.0).toFixed(2));
      return {
        actual,
        forecast: [last, f1, f2],
        confLow: [last, Number((f1 * 0.96).toFixed(2)), Number((f2 * 0.92).toFixed(2))],
        confHigh: [last, Number((f1 * 1.04).toFixed(2)), Number((f2 * 1.1).toFixed(2))],
        xLabels: SAMPLE_X_LABELS,
        scaleIndex: SAMPLE_SCALE_INDEX,
      };
    }
    return {
      actual: SAMPLE_ACTUAL,
      forecast: SAMPLE_FORECAST,
      confLow: SAMPLE_CONF_LOW,
      confHigh: SAMPLE_CONF_HIGH,
      xLabels: SAMPLE_X_LABELS,
      scaleIndex: SAMPLE_SCALE_INDEX,
    };
  }, [backendsLive, throughput]);

  const actualNow = chart.actual[chart.actual.length - 1];
  const forecastNext = chart.forecast[chart.forecast.length - 1];

  // ── safe_mode kill switch ──────────────────────────────────────────────────
  // The kill switch is owned by the app shell so the Topbar switch and this
  // card drive the same path (optimistic state, toast, best-effort policy
  // write). The card also reflects the change in its local policy snapshot.
  const onToggleSafeMode = (next: boolean) => {
    setPolicy((p) => ({ ...p, safe_mode: next }));
    shell.toggleSafeMode(next);
  };

  // ── render ─────────────────────────────────────────────────────────────────

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 22 }}>
      <HeroBand
        actualNow={actualNow}
        forecastNext={forecastNext}
        slo={slo}
        activeCount={activeCount}
        excludedCount={excludedCount}
        chart={chart}
      />

      <KpiRail
        throughputRpm={throughputRpm}
        p95={p95}
        slo={slo}
        errorRate={errorRate}
        activeCount={activeCount}
        totalCount={fleet.length}
        excludedCount={excludedCount}
      />

      <SectionHead
        title="The story right now"
        sub="Forecast is leading actual by one step. The pool scaled out ahead of the spike, p95 held flat, and an unhealthy node was excluded on anomaly evidence."
      />

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(0, 1.55fr) minmax(0, 1fr)",
          gap: 18,
          alignItems: "start",
        }}
      >
        <ForecastCard chart={chart} actualNow={actualNow} forecastNext={forecastNext} />
        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <SafeModeCard armed={shell.safeMode} onToggle={onToggleSafeMode} />
          <PolicyCard policy={policy} routing={routing} />
        </div>
      </div>

      <SectionHead
        title="Backend fleet"
        sub={`${fleet.length} nodes. Health is an anomaly verdict carrying evidence. ${
          excludedCount > 0 ? "An excluded node is held out of rotation; traffic redistributes automatically." : "All nodes are in rotation."
        }`}
      />

      <FleetCard fleet={fleet} />

      <SectionHead
        title="Verdicts and decisions"
        sub="Every automated call carries evidence and a timestamp. Recent anomaly verdicts on the left, the live decision stream on the right."
      />

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1.2fr)",
          gap: 18,
          alignItems: "start",
        }}
      >
        <VerdictsPanel alerts={alerts} />
        <DecisionStream activity={activity} />
      </div>
    </div>
  );
}

// ── hero band ────────────────────────────────────────────────────────────────

interface ChartData {
  actual: number[];
  forecast: number[];
  confLow: number[];
  confHigh: number[];
  xLabels: string[];
  scaleIndex: number;
}

function HeroBand({
  actualNow,
  forecastNext,
  slo,
  activeCount,
  excludedCount,
  chart,
}: {
  actualNow: number;
  forecastNext: number;
  slo: number;
  activeCount: number;
  excludedCount: number;
  chart: ChartData;
}) {
  return (
    <section
      style={{
        position: "relative",
        overflow: "hidden",
        borderRadius: "var(--sl-radius-xl)",
        border: "1px solid var(--sl-hairline)",
        background:
          "radial-gradient(900px 380px at 88% -30%, var(--sl-mint-soft), transparent 60%), linear-gradient(180deg, var(--sl-surface), var(--sl-surface))",
        boxShadow: "var(--sl-shadow-2)",
        padding: "30px 32px",
        display: "grid",
        gridTemplateColumns: "minmax(0, 1.05fr) minmax(0, 0.95fr)",
        gap: 30,
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
            color: "var(--sl-mint-deep)",
            background: "var(--sl-mint-tint)",
            border: "1px solid var(--sl-mint-line)",
            borderRadius: 20,
            padding: "5px 12px",
          }}
        >
          <span
            style={{
              width: 7,
              height: 7,
              borderRadius: "50%",
              background: "var(--sl-mint)",
              boxShadow: "0 0 8px var(--sl-mint)",
            }}
          />
          Adaptive load-management middleware
        </span>

        <h1
          style={{
            fontSize: 36,
            lineHeight: 1.08,
            letterSpacing: "-1.3px",
            fontWeight: 800,
            margin: "16px 0 0",
            color: "var(--sl-text)",
          }}
        >
          It scales <span style={{ color: "var(--sl-mint)" }}>before</span> the spike,
          <br />
          not after the page.
        </h1>

        <p style={{ fontSize: 15, color: "var(--sl-text-mid)", margin: "14px 0 0", maxWidth: "48ch" }}>
          The decision plane forecasts demand, rules on backend health, and routes
          accordingly, so the pool steps up ahead of the load and a sick node is
          excluded before users feel it.
        </p>

        <div
          style={{
            display: "flex",
            gap: 22,
            marginTop: 24,
            flexWrap: "wrap",
            fontFamily: "var(--sl-font-mono)",
            fontSize: 12,
          }}
        >
          <HeroStat label="actual now" value={`${actualNow.toFixed(1)}k`} unit="rpm" />
          <HeroStat label="forecast +5m" value={`${forecastNext.toFixed(1)}k`} unit="rpm" tone="mint" />
          <HeroStat label="SLO" value={slo.toFixed(2)} unit="%" />
          <HeroStat
            label="pool"
            value={`${activeCount}${excludedCount > 0 ? ` / ${activeCount + excludedCount}` : ""}`}
            unit="active"
          />
        </div>
      </div>

      <div
        style={{
          position: "relative",
          height: 236,
          borderRadius: "var(--sl-radius-lg)",
          background: "linear-gradient(180deg, var(--sl-surface), var(--sl-surface-sunk))",
          border: "1px solid var(--sl-hairline)",
          overflow: "hidden",
          padding: "10px 6px 0",
        }}
      >
        <div
          style={{
            position: "absolute",
            top: 12,
            left: 16,
            fontFamily: "var(--sl-font-mono)",
            fontSize: 10.5,
            color: "var(--sl-text-low)",
            letterSpacing: "0.5px",
            zIndex: 2,
          }}
        >
          FORECAST vs ACTUAL - throughput (k rpm)
        </div>
        <ForecastChart
          actual={chart.actual}
          forecast={chart.forecast}
          confLow={chart.confLow}
          confHigh={chart.confHigh}
          xLabels={chart.xLabels}
          scaleIndex={chart.scaleIndex}
          scaleLabel="scale 5 to 6"
          unit="k rpm"
          height={226}
        />
      </div>
    </section>
  );
}

function HeroStat({
  label,
  value,
  unit,
  tone,
}: {
  label: string;
  value: string;
  unit: string;
  tone?: "mint";
}) {
  return (
    <div>
      <div style={{ fontSize: 10, color: "var(--sl-text-low)", letterSpacing: "0.5px" }}>{label}</div>
      <div
        style={{
          fontSize: 19,
          fontWeight: 700,
          letterSpacing: "-0.5px",
          marginTop: 3,
          color: tone === "mint" ? "var(--sl-mint-deep)" : "var(--sl-text)",
        }}
      >
        {value}
        <span style={{ fontSize: 11, color: "var(--sl-text-low)", fontWeight: 500, marginLeft: 4 }}>{unit}</span>
      </div>
    </div>
  );
}

// ── KPI rail ─────────────────────────────────────────────────────────────────

function KpiRail({
  throughputRpm,
  p95,
  slo,
  errorRate,
  activeCount,
  totalCount,
  excludedCount,
}: {
  throughputRpm: number;
  p95: number;
  slo: number;
  errorRate: number;
  activeCount: number;
  totalCount: number;
  excludedCount: number;
}) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 16 }}>
      <KpiStat
        label={<><TrendingUp size={12} strokeWidth={2} /> Throughput</>}
        value={(throughputRpm / 1000).toFixed(1)}
        unit="k rpm"
        deltaDir="up"
        delta="+12.6%"
        footnote="vs 1h ago"
        spark={SAMPLE_SPARK.throughput}
        sparkTone="mint"
      />
      <KpiStat
        label={<><Gauge size={12} strokeWidth={2} /> p95 latency</>}
        value={Math.round(p95).toString()}
        unit="ms"
        deltaDir="flat"
        delta="0.7%"
        footnote="SLO 200 ms"
        spark={SAMPLE_SPARK.p95}
        sparkTone="graphite"
      />
      <KpiStat
        label={<><ShieldCheck size={12} strokeWidth={2} /> SLO compliance</>}
        value={slo.toFixed(2)}
        unit="%"
        deltaDir="up"
        delta="+0.05%"
        footnote="7-day window"
        spark={SAMPLE_SPARK.slo}
        sparkTone="mint"
      />
      <KpiStat
        label={<><Activity size={12} strokeWidth={2} /> Error rate</>}
        value={errorRate.toFixed(2)}
        unit="%"
        deltaDir="down"
        delta="-0.04%"
        footnote="vs 1h ago"
        spark={SAMPLE_SPARK.error}
        sparkTone="graphite"
      />
      <KpiStat
        label={<><Boxes size={12} strokeWidth={2} /> Active backends</>}
        value={`${activeCount}`}
        unit={`/ ${totalCount}`}
        deltaDir={excludedCount > 0 ? "down" : "flat"}
        delta={excludedCount > 0 ? `${excludedCount} excluded` : "all in rotation"}
        footnote={excludedCount > 0 ? "node isolated" : "healthy"}
        spark={SAMPLE_SPARK.backends}
        sparkTone="mint"
      />
    </div>
  );
}

// ── section header ───────────────────────────────────────────────────────────

function SectionHead({ title, sub }: { title: string; sub: string }) {
  return (
    <div style={{ margin: "8px 2px 0" }}>
      <h2 style={{ fontSize: 17, fontWeight: 700, letterSpacing: "-0.3px", margin: 0, color: "var(--sl-text)" }}>
        {title}
      </h2>
      <div style={{ fontSize: 12.5, color: "var(--sl-text-low)", marginTop: 3, maxWidth: "84ch" }}>{sub}</div>
    </div>
  );
}

// ── forecast card ────────────────────────────────────────────────────────────

function ForecastCard({
  chart,
  actualNow,
  forecastNext,
}: {
  chart: ChartData;
  actualNow: number;
  forecastNext: number;
}) {
  const headroom = Math.max(0, Math.round((1 - actualNow / (forecastNext * 1.45)) * 100));
  return (
    <Card
      title="Foresight and throughput"
      eyebrow="// 5-min horizon"
      actions={<Badge tone="mint">LIVE</Badge>}
      flush
    >
      <div style={{ padding: "0 18px 12px", fontSize: 11.5, color: "var(--sl-text-low)" }}>
        Mint forecast runs one step ahead of graphite actual; the band is the 90%
        confidence interval. The dashed marker is the scale-ahead decision.
      </div>

      <div style={{ display: "flex", gap: 26, padding: "2px 18px 14px", flexWrap: "wrap" }}>
        <FcStat label="Actual now" value={`${(actualNow * 1000).toLocaleString("en-US", { maximumFractionDigits: 0 })}`} unit="rpm" />
        <FcStat label="Forecast +5 min" value={`${(forecastNext * 1000).toLocaleString("en-US", { maximumFractionDigits: 0 })}`} unit="rpm" tone="mint" />
        <FcStat label="Confidence" value="92" unit="%" />
        <FcStat label="Headroom" value={`${headroom}`} unit="%" />
      </div>

      <div style={{ padding: "0 14px" }}>
        <ForecastChart
          actual={chart.actual}
          forecast={chart.forecast}
          confLow={chart.confLow}
          confHigh={chart.confHigh}
          xLabels={chart.xLabels}
          scaleIndex={chart.scaleIndex}
          scaleLabel="scale 5 to 6"
          unit="k rpm"
          height={300}
        />
      </div>

      <div style={{ display: "flex", gap: 18, padding: "8px 18px 18px", flexWrap: "wrap", fontFamily: "var(--sl-font-mono)", fontSize: 11 }}>
        <LegendItem swatch="var(--sl-graphite)" label="Actual throughput" />
        <LegendItem swatch="var(--sl-mint)" label="Forecast" />
        <LegendItem swatch="var(--sl-mint)" label="90% confidence band" band />
        <LegendItem swatch="var(--sl-text-faint)" label="Scale-ahead decision" dashed />
      </div>
    </Card>
  );
}

function FcStat({ label, value, unit, tone }: { label: string; value: string; unit: string; tone?: "mint" }) {
  return (
    <div>
      <div style={{ fontSize: 11, color: "var(--sl-text-low)", fontWeight: 600 }}>{label}</div>
      <div
        style={{
          fontFamily: "var(--sl-font-mono)",
          fontWeight: 700,
          fontSize: 20,
          letterSpacing: "-0.5px",
          marginTop: 3,
          color: tone === "mint" ? "var(--sl-mint-deep)" : "var(--sl-text)",
        }}
      >
        {value} <span style={{ fontSize: 12, color: "var(--sl-text-low)", fontWeight: 500 }}>{unit}</span>
      </div>
    </div>
  );
}

function LegendItem({ swatch, label, band, dashed }: { swatch: string; label: string; band?: boolean; dashed?: boolean }) {
  return (
    <span style={{ display: "flex", alignItems: "center", gap: 7, color: "var(--sl-text-mid)" }}>
      <span
        style={{
          width: 16,
          height: band ? 10 : 3,
          borderRadius: band ? 3 : 2,
          background: dashed ? "transparent" : swatch,
          opacity: band ? 0.4 : 1,
          borderTop: dashed ? `2px dashed ${swatch}` : undefined,
        }}
      />
      {label}
    </span>
  );
}

// ── safe_mode kill switch card ───────────────────────────────────────────────

function SafeModeCard({ armed, onToggle }: { armed: boolean; onToggle: (next: boolean) => void }) {
  return (
    <section
      style={{
        borderRadius: "var(--sl-radius-lg)",
        border: `1px solid ${armed ? "var(--sl-crit)" : "var(--sl-hairline)"}`,
        boxShadow: armed ? "0 0 0 3px rgba(220,38,38,.08), var(--sl-shadow-1)" : "var(--sl-shadow-1)",
        background: armed ? "var(--sl-crit-tint)" : "var(--sl-surface)",
        overflow: "hidden",
      }}
    >
      <div style={{ padding: "15px 18px 6px", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div style={{ fontSize: 14, fontWeight: 700, display: "flex", alignItems: "center", gap: 9, color: "var(--sl-text)" }}>
          <ShieldCheck size={17} strokeWidth={2} color={armed ? "var(--sl-crit)" : "var(--sl-mint)"} />
          Safe mode
        </div>
        <StatusPill status={armed ? "crit" : "ok"} hideDot>
          {armed ? "AUTOMATION FROZEN" : "ENGINE AUTONOMOUS"}
        </StatusPill>
      </div>

      <div style={{ padding: "6px 18px 18px" }}>
        <p style={{ fontSize: 12, color: armed ? "var(--sl-crit)" : "var(--sl-text-mid)", margin: "4px 0 14px" }}>
          {armed
            ? "Automation is frozen at its last known-good state. The load balancer keeps routing on the last committed weights; traffic never stops."
            : "The decision plane is making automated routing and scaling calls. Flip this to freeze every automated decision and hold the deterministic fallback."}
        </p>

        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 14,
            background: "var(--sl-surface-sunk)",
            border: "1px solid var(--sl-hairline)",
            borderRadius: 12,
            padding: "12px 14px",
          }}
        >
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 13, fontWeight: 700, color: "var(--sl-text)" }}>Freeze automation</div>
            <div style={{ fontFamily: "var(--sl-font-mono)", fontSize: 10.5, color: "var(--sl-text-low)", marginTop: 2 }}>
              safe_mode = {armed ? "on" : "off"}
            </div>
          </div>
          <Toggle checked={armed} onChange={onToggle} armedTone label="Toggle safe mode" />
        </div>

        <div style={{ display: "flex", gap: 8, marginTop: 12, fontSize: 11, color: "var(--sl-text-low)", alignItems: "flex-start" }}>
          <ShieldCheck size={14} strokeWidth={2} style={{ flex: "0 0 auto", marginTop: 1 }} />
          <span>Engaging safe mode is reversible and audit-logged. The load balancer keeps serving on the last committed weights.</span>
        </div>
      </div>
    </section>
  );
}

// ── operating policy card ────────────────────────────────────────────────────

function PolicyCard({ policy, routing }: { policy: Policy; routing: RoutingMetrics }) {
  const rows: Array<[string, ReactNode]> = [
    ["Operating mode", <Badge tone="mint" key="mode">{(policy.operating_mode ?? "adaptive").toUpperCase()}</Badge>],
    ["Strategy", <span key="strat" style={{ fontFamily: "var(--sl-font-mono)", fontWeight: 600 }}>{policy.strategy_name ?? "custom"}</span>],
    ["Backend range", `min ${policy.min_backends} - max ${policy.max_backends}`],
    ["SLO target (p95)", `${policy.slo_p95_latency_ms} ms`],
    ["Cluster size", `${routing.cluster_size_current ?? "-"} nodes`],
  ];
  return (
    <Card title="Operating policy" actions={<Badge tone="neutral">v{policy.policy_version}</Badge>}>
      <div>
        {rows.map(([label, value], i) => (
          <div
            key={label}
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
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
    </Card>
  );
}

// ── fleet table ──────────────────────────────────────────────────────────────

function FleetCard({ fleet }: { fleet: SampleBackend[] }) {
  const columns: Column<SampleBackend>[] = [
    {
      key: "backend",
      header: "Backend",
      render: (b) => {
        const s = statusOfBackend(b);
        const led = s === "crit" ? "var(--sl-crit)" : s === "warn" ? "var(--sl-warn)" : "var(--sl-ok)";
        return (
          <div style={{ display: "flex", alignItems: "center", gap: 11 }}>
            <span style={{ width: 9, height: 9, borderRadius: "50%", background: led, flex: "0 0 auto", boxShadow: `0 0 6px ${led}` }} />
            <div>
              <div style={{ fontFamily: "var(--sl-font-mono)", fontSize: 13, fontWeight: 600, color: "var(--sl-text)" }}>{b.instance}</div>
              {b.zone ? <div style={{ fontFamily: "var(--sl-font-mono)", fontSize: 10, color: "var(--sl-text-faint)" }}>{b.zone}</div> : null}
            </div>
          </div>
        );
      },
    },
    { key: "p95", header: "p95 latency", numeric: true, render: (b) => <span>{b.p95_ms}<span style={{ color: "var(--sl-text-faint)", fontSize: 10.5 }}> ms</span></span> },
    { key: "rpm", header: "Req / min", numeric: true, render: (b) => fmtInt(b.rpm) },
    { key: "err", header: "Error rate", numeric: true, render: (b) => <span>{b.error_rate_pct.toFixed(2)}<span style={{ color: "var(--sl-text-faint)", fontSize: 10.5 }}> %</span></span> },
    {
      key: "health",
      header: "Health score",
      render: (b) => {
        const s = statusOfBackend(b);
        const color = s === "crit" ? "var(--sl-crit)" : s === "warn" ? "var(--sl-warn)" : "var(--sl-mint)";
        return (
          <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 120 }}>
            <div style={{ flex: 1, height: 6, borderRadius: 6, background: "var(--sl-surface-sunk)", overflow: "hidden", minWidth: 64 }}>
              <div style={{ width: `${Math.round(b.health_score * 100)}%`, height: "100%", borderRadius: 6, background: color }} />
            </div>
            <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12, fontWeight: 600, color: "var(--sl-text)" }}>
              {b.health_score.toFixed(2)}
            </span>
          </div>
        );
      },
    },
    {
      key: "status",
      header: "Status",
      render: (b) => {
        const s = statusOfBackend(b);
        const word = b.excluded ? "EXCLUDED" : s === "warn" ? "DEGRADED" : "HEALTHY";
        return (
          <div style={{ display: "flex", flexDirection: "column", gap: 6, alignItems: "flex-start" }}>
            <StatusPill status={s}>{word}</StatusPill>
            {b.evidence ? (
              <EvidenceLine
                metric={b.evidence.metric}
                observed={String(b.evidence.observed)}
                threshold={String(b.evidence.threshold)}
                verdict={b.evidence.observed > b.evidence.threshold ? "over" : "under"}
                status={s}
              />
            ) : null}
          </div>
        );
      },
    },
  ];

  return (
    <Card flush>
      <DataTable
        columns={columns}
        rows={fleet}
        rowKey={(b) => b.instance}
        rowMuted={(b) => b.excluded}
      />
    </Card>
  );
}

// ── verdicts panel ───────────────────────────────────────────────────────────

function VerdictsPanel({ alerts }: { alerts: AlertItem[] }) {
  return (
    <Card title="Anomaly verdicts" eyebrow="// evidence-carrying" flush>
      {alerts.length === 0 ? (
        <div style={{ padding: 18, fontSize: 13, color: "var(--sl-text-low)" }}>No active verdicts. The fleet is healthy.</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column" }}>
          {alerts.map((a, i) => {
            const s = alertStatus(a);
            return (
              <div
                key={`${a.backend_id}-${i}`}
                style={{
                  display: "grid",
                  gridTemplateColumns: "1fr auto",
                  gap: 12,
                  padding: "14px 18px",
                  borderBottom: i < alerts.length - 1 ? "1px solid var(--sl-hairline-soft)" : undefined,
                }}
              >
                <div>
                  <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
                    <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 13, fontWeight: 600, color: "var(--sl-text)" }}>{a.backend_id}</span>
                    <StatusPill status={s}>{(a.status ?? a.severity).toUpperCase()}</StatusPill>
                  </div>
                  <div style={{ fontSize: 11.5, color: "var(--sl-text-mid)", marginTop: 5, lineHeight: 1.45 }}>{a.summary}</div>
                  {a.metric && a.observed_value != null && a.threshold != null ? (
                    <div style={{ marginTop: 7 }}>
                      <EvidenceLine
                        metric={a.metric}
                        observed={String(a.observed_value)}
                        threshold={String(a.threshold)}
                        verdict={a.observed_value > a.threshold ? "over" : "under"}
                        status={s}
                      />
                    </div>
                  ) : null}
                </div>
                {a.time ? (
                  <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 10.5, color: "var(--sl-text-faint)", whiteSpace: "nowrap" }}>{a.time}</span>
                ) : null}
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

// ── decision stream ──────────────────────────────────────────────────────────

const KIND_TONE: Record<ActivityItem["kind"], { bg: string; fg: string; label: string }> = {
  scaling: { bg: "var(--sl-mint-tint)", fg: "var(--sl-mint-deep)", label: "SCALE" },
  anomaly: { bg: "var(--sl-crit-tint)", fg: "var(--sl-crit)", label: "VERDICT" },
  policy: { bg: "var(--sl-surface-sunk)", fg: "var(--sl-graphite)", label: "POLICY" },
};

function DecisionStream({ activity }: { activity: ActivityItem[] }) {
  return (
    <Card
      title="Decision stream"
      eyebrow="// audit"
      actions={
        <Button variant="ghost" size="sm" icon={<BookOpen size={13} strokeWidth={2} />}>
          Open Ledger
        </Button>
      }
      flush
    >
      <div style={{ display: "flex", flexDirection: "column" }}>
        {activity.map((ev, i) => {
          const tone = KIND_TONE[ev.kind];
          const s = activityStatus(ev.severity);
          return (
            <div
              key={`${ev.time}-${i}`}
              style={{
                display: "grid",
                gridTemplateColumns: "auto 1fr auto",
                gap: 13,
                padding: "14px 18px",
                borderBottom: i < activity.length - 1 ? "1px solid var(--sl-hairline-soft)" : undefined,
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
                  background: tone.bg,
                  color: tone.fg,
                }}
              >
                {ev.kind === "scaling" ? <ArrowRight size={15} strokeWidth={2} /> : ev.kind === "anomaly" ? <ShieldCheck size={15} strokeWidth={2} /> : <BookOpen size={15} strokeWidth={2} />}
              </span>
              <div>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ fontSize: 13, fontWeight: 600, color: "var(--sl-text)" }}>
                    {ev.actor ? `${ev.actor}` : tone.label}
                  </span>
                  <StatusPill status={s} hideDot>{tone.label}</StatusPill>
                </div>
                <div style={{ fontSize: 11.5, color: "var(--sl-text-mid)", marginTop: 4, lineHeight: 1.45 }}>{ev.summary}</div>
              </div>
              <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 10.5, color: "var(--sl-text-faint)", whiteSpace: "nowrap" }}>{ev.time}</span>
            </div>
          );
        })}
      </div>
    </Card>
  );
}
