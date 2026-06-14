// ============================================================================
// Flightdeck -- the flagship live overview
// ----------------------------------------------------------------------------
// Tells the closed-loop story: forecast leading actual throughput, the KPI
// rail, the backend fleet with evidence on the excluded node, recent anomaly
// verdicts, and the decision stream. Every panel resolves its data through
// useLiveOrDemo: it shows representative demonstration data immediately, then
// upgrades in place when the live API is reachable, and reports its source to
// the global Demonstration / Live badge. The safe_mode kill switch is owned
// here and surfaced in the Topbar through the shell context.
// ============================================================================

import { useEffect, useMemo, type ReactNode } from "react";
import {
  Activity,
  ArrowRight,
  BookOpen,
  Boxes,
  CheckCircle2,
  Gauge,
  ShieldCheck,
  TrendingUp,
} from "lucide-react";

import {
  api,
  type ActivityItem,
  type AlertItem,
  type BackendMetrics,
  type ForecastSummary,
  type OpsMetrics,
  type Policy,
  type RelatedMetrics,
  type RoutingMetrics,
  type TrendKpi,
  type TrendsResponse,
} from "../api";
import {
  Badge,
  Button,
  Card,
  DataTable,
  EmptyState,
  ErrorState,
  EvidenceLine,
  ForecastChart,
  KpiStat,
  LoadState,
  StatusPill,
  Toggle,
  useLiveOrDemo,
  type Column,
  type DeltaDir,
  type LoadStatus,
  type Status,
} from "../ui";
import { useShell } from "./shell-context";
import {
  SAMPLE_ACTIVITY,
  SAMPLE_ALERTS,
  SAMPLE_BACKENDS,
  SAMPLE_BACKEND_METRICS,
  SAMPLE_FORECAST_SUMMARY,
  SAMPLE_OPS,
  SAMPLE_POLICY,
  SAMPLE_RELATED,
  SAMPLE_ROUTING,
  SAMPLE_TRENDS,
  type SampleBackend,
} from "./sample";

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

// Direction of a KPI delta from the signed percentage. A flat reading (|Δ| < a
// hair) reads as "flat" so tiny noise doesn't paint a colour.
function deltaDirFromPct(pct: number | null): DeltaDir {
  if (pct == null || Math.abs(pct) < 0.005) return "flat";
  return pct > 0 ? "up" : "down";
}

// Format a signed percentage delta with a leading arrow, e.g. "▲ 12.6%".
function fmtDeltaPct(pct: number | null): string {
  if (pct == null) return "—";
  if (Math.abs(pct) < 0.005) return "0.00%";
  const arrow = pct > 0 ? "▲" : "▼";
  return `${arrow} ${Math.abs(pct).toFixed(2)}%`;
}

// Convert a requests/sec reading to k-rpm for the throughput chart and hero
// stats (rps * 60 / 1000), matching the prototype's "k rpm" axis.
const rpsToKrpm = (rps: number) => (rps * 60) / 1000;

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

// ── forecast summary -> chart data ────────────────────────────────────────────

interface ChartData {
  actual: number[];
  forecast: number[];
  confLow: number[];
  confHigh: number[];
  xLabels: string[];
  scaleIndex?: number;
}

interface ForecastReadout {
  chart: ChartData;
  actualNow: number;   // k-rpm
  forecastNext: number; // k-rpm
  confidencePct: number | null; // derived from band width, not a literal
  modelName: string | null;
  scaleAction: string | null;
  empty: boolean;
}

// Build the hero / forecast-card readout straight from a ForecastSummary: the
// actual + forecast series (converted to k-rpm), the confidence band, the
// scale-ahead marker index, and a confidence derived from the band's relative
// width at the furthest horizon (a tighter band reads as higher confidence).
function readForecast(summary: ForecastSummary): ForecastReadout {
  const actual = summary.actual.map((p) => Number(rpsToKrpm(p.rps).toFixed(2)));
  const forecast = summary.forecast.map((p) => Number(rpsToKrpm(p.predicted_rps).toFixed(2)));

  const hasActual = actual.length > 0;
  const hasForecast = forecast.length > 0;

  // Confidence band, aligned to the forecast series. Fall back to the predicted
  // point itself where a bound is missing so the band never collapses oddly.
  const confLow = summary.forecast.map((p, i) =>
    Number(rpsToKrpm(p.confidence_lower ?? p.predicted_rps).toFixed(2)) || forecast[i],
  );
  const confHigh = summary.forecast.map((p, i) =>
    Number(rpsToKrpm(p.confidence_upper ?? p.predicted_rps).toFixed(2)) || forecast[i],
  );

  // X labels: minutes-before for actual (… -10, now), minutes-ahead for the
  // forecast tail (+5, +10), so the hand-off at "now" reads cleanly.
  const step = 5; // 5-min buckets in the demonstration + typical live cadence
  const xLabels: string[] = [];
  for (let i = 0; i < actual.length; i++) {
    const minsAgo = (actual.length - 1 - i) * step;
    xLabels.push(minsAgo === 0 ? "now" : `-${minsAgo}`);
  }
  // Forecast index 0 aligns with the last actual ("now"); subsequent steps lead.
  for (let i = 1; i < forecast.length; i++) {
    const mins = summary.forecast[i].horizon_minutes;
    xLabels.push(mins != null && mins > 0 ? `+${mins}` : `+${i * step}`);
  }

  // Scale-ahead marker: place it on the forecast step nearest the marker time,
  // defaulting to the first lead step (the typical "scaled ahead" position).
  let scaleIndex: number | undefined;
  if (summary.scale_ahead) {
    const markerTime = summary.scale_ahead.time ? Date.parse(summary.scale_ahead.time) : NaN;
    if (!Number.isNaN(markerTime) && hasForecast) {
      let best = 0;
      let bestGap = Infinity;
      summary.forecast.forEach((p, i) => {
        const t = p.time ? Date.parse(p.time) : NaN;
        if (Number.isNaN(t)) return;
        const gap = Math.abs(t - markerTime);
        if (gap < bestGap) {
          bestGap = gap;
          best = i;
        }
      });
      scaleIndex = best;
    } else if (hasForecast) {
      scaleIndex = Math.min(1, forecast.length - 1);
    }
  }

  const actualNow = hasActual ? actual[actual.length - 1] : 0;
  const forecastNext = hasForecast ? forecast[forecast.length - 1] : actualNow;

  // Confidence from the band: relative half-width at the furthest horizon,
  // mapped to a percentage and clamped to a believable 80–99% range. A literal
  // is never used; an empty/absent band yields null so the tile shows "—".
  let confidencePct: number | null = null;
  if (hasForecast) {
    const lastIdx = forecast.length - 1;
    const mid = forecast[lastIdx];
    const lo = confLow[lastIdx];
    const hi = confHigh[lastIdx];
    if (mid > 0 && hi >= lo && hi - lo >= 0) {
      const relHalfWidth = (hi - lo) / 2 / mid; // 0 = perfectly tight
      const pct = Math.round((1 - relHalfWidth) * 100);
      confidencePct = Math.max(80, Math.min(99, pct));
    }
  }

  return {
    chart: { actual, forecast, confLow, confHigh, xLabels, scaleIndex },
    actualNow,
    forecastNext,
    confidencePct,
    modelName: summary.model_name,
    scaleAction: summary.scale_ahead?.action ?? null,
    empty: !hasActual && !hasForecast,
  };
}

// ── component ────────────────────────────────────────────────────────────────

export default function Flightdeck() {
  const shell = useShell();
  const { setDataSource, setSafeMode, setPlane, setPlaneNodes } = shell;

  // Each data domain resolves live-or-demo independently and registers a unique
  // panelId so the global Demonstration / Live badge reflects reality. The demo
  // fallback is shown immediately, so the page is never blank and reads healthy.
  const ops = useLiveOrDemo<OpsMetrics>(() => api.getOpsMetrics(), SAMPLE_OPS, {
    panelId: "flightdeck-ops",
  });
  const related = useLiveOrDemo<RelatedMetrics>(() => api.getRelatedMetrics(), SAMPLE_RELATED, {
    panelId: "flightdeck-related",
  });
  const trends = useLiveOrDemo<TrendsResponse>(() => api.getTrends(), SAMPLE_TRENDS, {
    panelId: "flightdeck-trends",
  });
  const forecast = useLiveOrDemo<ForecastSummary>(
    () => api.getForecastSummary(),
    SAMPLE_FORECAST_SUMMARY,
    { panelId: "flightdeck-forecast" },
  );
  const routing = useLiveOrDemo<RoutingMetrics>(() => api.getRoutingMetrics(), SAMPLE_ROUTING, {
    panelId: "flightdeck-routing",
  });
  const backends = useLiveOrDemo<BackendMetrics>(() => api.getBackendMetrics(), SAMPLE_BACKEND_METRICS, {
    panelId: "flightdeck-fleet",
  });
  const alerts = useLiveOrDemo<AlertItem[]>(() => api.getAlerts(), SAMPLE_ALERTS, {
    panelId: "flightdeck-verdicts",
  });
  const activity = useLiveOrDemo<ActivityItem[]>(() => api.getActivity(8), SAMPLE_ACTIVITY, {
    panelId: "flightdeck-stream",
  });
  const policyState = useLiveOrDemo<Policy>(() => api.getPolicy(), SAMPLE_POLICY, {
    panelId: "flightdeck-policy",
  });

  // The kill switch reads from the shell (Topbar + this card share one path).
  // The card reflects the live policy reading too, so the surface is consistent.
  const policy = useMemo<Policy>(
    () => ({ ...policyState.value, safe_mode: shell.safeMode }),
    [policyState.value, shell.safeMode],
  );

  // Only a live policy reading drives the kill switch; offline we keep the
  // operator's manual choice so a refresh can't revert it.
  useEffect(() => {
    if (policyState.source === "live") setSafeMode(Boolean(policyState.value.safe_mode));
  }, [policyState.source, policyState.value.safe_mode, setSafeMode]);

  // Publish data-mode + plane health to the shell. Demonstration is an
  // intentional, healthy posture -- never degraded -- so on demo the plane is
  // "ok". Excluding a sick node is the plane working as designed, so that alone
  // never reads as degraded; only a fleet with no node in rotation escalates.
  const fleet = useMemo(
    () => toFleetRows(backends.value, backends.source !== "live", policy.slo_p95_latency_ms),
    [backends.value, backends.source, policy.slo_p95_latency_ms],
  );
  const activeCount = fleet.filter((b) => !b.excluded).length;
  const excludedCount = fleet.length - activeCount;

  const anyLive =
    ops.source === "live" ||
    related.source === "live" ||
    trends.source === "live" ||
    forecast.source === "live" ||
    routing.source === "live" ||
    backends.source === "live" ||
    alerts.source === "live" ||
    activity.source === "live" ||
    policyState.source === "live";

  useEffect(() => {
    setDataSource(anyLive ? "live" : "sample");
    // Calm by default. Demonstration is intentional, and a live fleet that has
    // isolated a node is healthy operation, so the plane only reads degraded
    // when live data shows no backend left in rotation at all.
    const liveOutage = anyLive && fleet.length > 0 && activeCount === 0;
    setPlane(liveOutage ? "warn" : "ok");
  }, [anyLive, fleet, activeCount, setDataSource, setPlane]);

  useEffect(() => {
    setPlaneNodes(routing.value.cluster_size_current ?? ops.value.services_total);
  }, [routing.value.cluster_size_current, ops.value.services_total, setPlaneNodes]);

  // ── derived KPI readings ───────────────────────────────────────────────────

  const throughputRpm = ops.value.throughput_rpm ?? related.value.rps_current ?? 0;
  const p95 = related.value.p95_latency_ms ?? backends.value.aggregate?.p95_ms ?? 0;
  const slo = ops.value.policy_compliance_pct ?? related.value.slo_compliance_pct ?? 0;
  const errorRate = backends.value.aggregate?.error_rate_pct ?? 0;

  // ── forecast readout (hero chart + ForecastCard) ───────────────────────────

  const fc = useMemo(() => readForecast(forecast.value), [forecast.value]);

  // ── safe_mode kill switch ──────────────────────────────────────────────────
  // Owned by the app shell so the Topbar switch and this card drive the same
  // path (optimistic state, toast, best-effort policy write).
  const onToggleSafeMode = (next: boolean) => {
    shell.toggleSafeMode(next);
  };

  // ── render ─────────────────────────────────────────────────────────────────

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 22 }}>
      <HeroBand
        fc={fc}
        forecastState={forecast.state}
        slo={slo}
        activeCount={activeCount}
        excludedCount={excludedCount}
      />

      <KpiRail
        trends={trends.value}
        state={trends.state}
        degraded={trends.degraded}
        onRetry={trends.reload}
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

      <div className="sl-grid-2-1">
        <ForecastCard fc={fc} state={forecast.state} degraded={forecast.degraded} live={forecast.source === "live"} onRetry={forecast.reload} />
        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <SafeModeCard armed={shell.safeMode} onToggle={onToggleSafeMode} />
          <PolicyCard policy={policy} routing={routing.value} state={policyState.state} degraded={policyState.degraded} onRetry={policyState.reload} />
        </div>
      </div>

      <SectionHead
        title="Backend fleet"
        sub={`${fleet.length} nodes. Health is an anomaly verdict carrying evidence. ${
          excludedCount > 0 ? "An excluded node is held out of rotation; traffic redistributes automatically." : "All nodes are in rotation."
        }`}
      />

      <FleetCard fleet={fleet} state={backends.state} degraded={backends.degraded} onRetry={backends.reload} />

      <SectionHead
        title="Verdicts and decisions"
        sub="Every automated call carries evidence and a timestamp. Recent anomaly verdicts on the left, the live decision stream on the right."
      />

      <div className="sl-grid-1-1">
        <VerdictsPanel alerts={alerts.value} state={alerts.state} degraded={alerts.degraded} onRetry={alerts.reload} />
        <DecisionStream activity={activity.value} state={activity.state} degraded={activity.degraded} onRetry={activity.reload} />
      </div>
    </div>
  );
}

// ── hero band ────────────────────────────────────────────────────────────────

function HeroBand({
  fc,
  forecastState,
  slo,
  activeCount,
  excludedCount,
}: {
  fc: ForecastReadout;
  forecastState: LoadStatus;
  slo: number;
  activeCount: number;
  excludedCount: number;
}) {
  const horizonLabel =
    fc.chart.xLabels.length > 0 ? fc.chart.xLabels[fc.chart.xLabels.length - 1] : "+5";
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
          <HeroStat label="actual now" value={`${fc.actualNow.toFixed(1)}k`} unit="rpm" />
          <HeroStat
            label={`forecast ${horizonLabel}m`}
            value={`${fc.forecastNext.toFixed(1)}k`}
            unit="rpm"
            tone="mint"
          />
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
          minHeight: 236,
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
        {forecastState === "loading" ? (
          <div style={{ padding: "34px 16px 16px" }}>
            <LoadState lines={5} lineHeight={18} label="Loading forecast…" />
          </div>
        ) : fc.empty ? (
          <EmptyState
            icon={<TrendingUp size={20} strokeWidth={2} />}
            title="No throughput in this window"
            hint="The forecast appears as soon as request traffic is observed."
          />
        ) : (
          <ForecastChart
            actual={fc.chart.actual}
            forecast={fc.chart.forecast}
            confLow={fc.chart.confLow}
            confHigh={fc.chart.confHigh}
            xLabels={fc.chart.xLabels}
            scaleIndex={fc.chart.scaleIndex}
            scaleLabel={fc.scaleAction === "scale_in" ? "scale in" : "scale out"}
            unit="k rpm"
            height={226}
          />
        )}
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

// One KPI tile, sourced entirely from a TrendKpi: the delta and its direction
// come from delta_pct, the footnote from the trend's label, and the sparkline
// from the trend's series. The headline value stays caller-formatted because
// each KPI renders its number differently (k-rpm, ms, %, ratio).
function TrendStat({
  label,
  value,
  unit,
  kpi,
  sparkTone,
}: {
  label: ReactNode;
  value: ReactNode;
  unit?: string;
  kpi: TrendKpi;
  sparkTone: "mint" | "graphite";
}) {
  return (
    <KpiStat
      label={label}
      value={value}
      unit={unit}
      deltaDir={deltaDirFromPct(kpi.delta_pct)}
      delta={fmtDeltaPct(kpi.delta_pct)}
      footnote={kpi.label}
      spark={kpi.series.length > 0 ? kpi.series : undefined}
      sparkTone={sparkTone}
    />
  );
}

function KpiRail({
  trends,
  state,
  degraded,
  onRetry,
  throughputRpm,
  p95,
  slo,
  errorRate,
  activeCount,
  totalCount,
  excludedCount,
}: {
  trends: TrendsResponse;
  state: LoadStatus;
  degraded: boolean;
  onRetry: () => void;
  throughputRpm: number;
  p95: number;
  slo: number;
  errorRate: number;
  activeCount: number;
  totalCount: number;
  excludedCount: number;
}) {
  if (state === "loading") {
    return (
      <div className="sl-grid-kpi">
        {Array.from({ length: 5 }).map((_, i) => (
          <div
            key={i}
            style={{
              background: "var(--sl-surface)",
              border: "1px solid var(--sl-hairline)",
              borderRadius: "var(--sl-radius-lg)",
              boxShadow: "var(--sl-shadow-1)",
              padding: "15px 17px",
            }}
          >
            <LoadState lines={3} />
          </div>
        ))}
      </div>
    );
  }

  // The active-backends delta is a count/posture, not a percentage, so it is
  // expressed in the trend's label rather than a percent.
  const backendsKpi = trends.active_backends;
  const backendsDir: DeltaDir = excludedCount > 0 ? "down" : "flat";

  return (
    <div className="sl-grid-kpi">
      <TrendStat
        label={<><TrendingUp size={12} strokeWidth={2} /> Throughput</>}
        value={(throughputRpm / 1000).toFixed(1)}
        unit="k rpm"
        kpi={trends.throughput_rpm}
        sparkTone="mint"
      />
      <TrendStat
        label={<><Gauge size={12} strokeWidth={2} /> p95 latency</>}
        value={Math.round(p95).toString()}
        unit="ms"
        kpi={trends.p95_latency_ms}
        sparkTone="graphite"
      />
      <TrendStat
        label={<><ShieldCheck size={12} strokeWidth={2} /> SLO compliance</>}
        value={slo.toFixed(2)}
        unit="%"
        kpi={trends.slo_compliance_pct}
        sparkTone="mint"
      />
      <TrendStat
        label={<><Activity size={12} strokeWidth={2} /> Error rate</>}
        value={errorRate.toFixed(2)}
        unit="%"
        kpi={trends.error_rate_pct}
        sparkTone="graphite"
      />
      <KpiStat
        label={<><Boxes size={12} strokeWidth={2} /> Active backends</>}
        value={`${activeCount}`}
        unit={`/ ${totalCount}`}
        deltaDir={backendsDir}
        delta={excludedCount > 0 ? `${excludedCount} excluded` : "all in rotation"}
        footnote={backendsKpi.label}
        spark={backendsKpi.series.length > 0 ? backendsKpi.series : undefined}
        sparkTone="mint"
      />
      {degraded ? (
        <div style={{ gridColumn: "1 / -1" }}>
          <ErrorState
            title="Trend deltas are showing representative data"
            hint="The KPI values above are current; the window-over-window deltas couldn't be refreshed."
            onRetry={onRetry}
          />
        </div>
      ) : null}
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
  fc,
  state,
  degraded,
  live,
  onRetry,
}: {
  fc: ForecastReadout;
  state: LoadStatus;
  degraded: boolean;
  live: boolean;
  onRetry: () => void;
}) {
  const headroom = Math.max(0, Math.round((1 - fc.actualNow / (fc.forecastNext * 1.45)) * 100));
  const horizonLabel =
    fc.chart.xLabels.length > 0 ? fc.chart.xLabels[fc.chart.xLabels.length - 1] : "+5";
  return (
    <Card
      title="Foresight and throughput"
      eyebrow="// forecast horizon"
      actions={<Badge tone={live ? "mint" : "neutral"}>{live ? "LIVE" : "DEMO"}</Badge>}
      flush
    >
      <div style={{ padding: "0 18px 12px", fontSize: 11.5, color: "var(--sl-text-low)" }}>
        Mint forecast runs one step ahead of graphite actual; the band is the
        confidence interval. The dashed marker is the scale-ahead decision.
      </div>

      {degraded ? (
        <div style={{ padding: "0 18px 16px" }}>
          <ErrorState
            title="Showing a representative forecast"
            hint="The live forecast couldn't be reached just now. The chart below is representative."
            onRetry={onRetry}
          />
        </div>
      ) : null}

      <div style={{ display: "flex", gap: 26, padding: "2px 18px 14px", flexWrap: "wrap" }}>
        <FcStat
          label="Actual now"
          value={`${(fc.actualNow * 1000).toLocaleString("en-US", { maximumFractionDigits: 0 })}`}
          unit="rpm"
        />
        <FcStat
          label={`Forecast ${horizonLabel} min`}
          value={`${(fc.forecastNext * 1000).toLocaleString("en-US", { maximumFractionDigits: 0 })}`}
          unit="rpm"
          tone="mint"
        />
        <FcStat label="Confidence" value={fc.confidencePct != null ? `${fc.confidencePct}` : "—"} unit="%" />
        <FcStat label="Headroom" value={`${headroom}`} unit="%" />
      </div>

      <div style={{ padding: "0 14px", minHeight: 300 }}>
        {state === "loading" ? (
          <div style={{ padding: "12px 6px" }}>
            <LoadState lines={7} lineHeight={20} label="Loading forecast…" />
          </div>
        ) : fc.empty ? (
          <EmptyState
            icon={<TrendingUp size={22} strokeWidth={2} />}
            title="No throughput in this window"
            hint="The forecast and actual series appear as soon as request traffic is observed."
          />
        ) : (
          <ForecastChart
            actual={fc.chart.actual}
            forecast={fc.chart.forecast}
            confLow={fc.chart.confLow}
            confHigh={fc.chart.confHigh}
            xLabels={fc.chart.xLabels}
            scaleIndex={fc.chart.scaleIndex}
            scaleLabel={fc.scaleAction === "scale_in" ? "scale in" : "scale out"}
            unit="k rpm"
            height={300}
          />
        )}
      </div>

      <div style={{ display: "flex", gap: 18, padding: "8px 18px 18px", flexWrap: "wrap", fontFamily: "var(--sl-font-mono)", fontSize: 11 }}>
        <LegendItem swatch="var(--sl-graphite)" label="Actual throughput" />
        <LegendItem swatch="var(--sl-mint)" label="Forecast" />
        <LegendItem swatch="var(--sl-mint)" label="Confidence band" band />
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

function PolicyCard({
  policy,
  routing,
  state,
  degraded,
  onRetry,
}: {
  policy: Policy;
  routing: RoutingMetrics;
  state: LoadStatus;
  degraded: boolean;
  onRetry: () => void;
}) {
  const rows: Array<[string, ReactNode]> = [
    ["Operating mode", <Badge tone="mint" key="mode">{(policy.operating_mode ?? "adaptive").toUpperCase()}</Badge>],
    ["Strategy", <span key="strat" style={{ fontFamily: "var(--sl-font-mono)", fontWeight: 600 }}>{policy.strategy_name ?? "custom"}</span>],
    ["Backend range", `min ${policy.min_backends} - max ${policy.max_backends}`],
    ["SLO target (p95)", `${policy.slo_p95_latency_ms} ms`],
    ["Cluster size", `${routing.cluster_size_current ?? "-"} nodes`],
  ];
  return (
    <Card title="Operating policy" actions={<Badge tone="neutral">v{policy.policy_version}</Badge>}>
      {state === "loading" ? (
        <LoadState lines={5} lineHeight={16} label="Loading policy…" />
      ) : (
        <div>
          {degraded ? (
            <div style={{ marginBottom: 12 }}>
              <ErrorState
                title="Showing the last representative policy"
                hint="The live policy snapshot couldn't be refreshed just now."
                onRetry={onRetry}
              />
            </div>
          ) : null}
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
      )}
    </Card>
  );
}

// ── fleet table ──────────────────────────────────────────────────────────────

function FleetCard({
  fleet,
  state,
  degraded,
  onRetry,
}: {
  fleet: SampleBackend[];
  state: LoadStatus;
  degraded: boolean;
  onRetry: () => void;
}) {
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
      {state === "loading" ? (
        <div style={{ padding: 18 }}>
          <LoadState lines={6} lineHeight={18} label="Loading fleet…" />
        </div>
      ) : fleet.length === 0 ? (
        <EmptyState
          icon={<Boxes size={22} strokeWidth={2} />}
          title="No backends reporting yet"
          hint="Nodes appear here as soon as they register with the load balancer."
        />
      ) : (
        <>
          {degraded ? (
            <div style={{ padding: "14px 16px 0" }}>
              <ErrorState
                title="Showing a representative fleet"
                hint="Live backend metrics couldn't be reached just now. The pool below is representative."
                onRetry={onRetry}
              />
            </div>
          ) : null}
          <DataTable
            columns={columns}
            rows={fleet}
            rowKey={(b) => b.instance}
            rowMuted={(b) => b.excluded}
          />
        </>
      )}
    </Card>
  );
}

// ── verdicts panel ───────────────────────────────────────────────────────────

function VerdictsPanel({
  alerts,
  state,
  degraded,
  onRetry,
}: {
  alerts: AlertItem[];
  state: LoadStatus;
  degraded: boolean;
  onRetry: () => void;
}) {
  return (
    <Card title="Anomaly verdicts" eyebrow="// evidence-carrying" flush>
      {state === "loading" ? (
        <div style={{ padding: 18 }}>
          <LoadState lines={4} lineHeight={18} label="Loading verdicts…" />
        </div>
      ) : alerts.length === 0 ? (
        <EmptyState
          icon={<CheckCircle2 size={22} strokeWidth={2} />}
          title="No active verdicts"
          hint="The fleet is healthy. Verdicts appear here when a node crosses an anomaly threshold."
        />
      ) : (
        <div style={{ display: "flex", flexDirection: "column" }}>
          {degraded ? (
            <div style={{ padding: "14px 16px 0" }}>
              <ErrorState
                title="Showing representative verdicts"
                hint="Live anomaly verdicts couldn't be reached just now."
                onRetry={onRetry}
              />
            </div>
          ) : null}
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

function DecisionStream({
  activity,
  state,
  degraded,
  onRetry,
}: {
  activity: ActivityItem[];
  state: LoadStatus;
  degraded: boolean;
  onRetry: () => void;
}) {
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
      {state === "loading" ? (
        <div style={{ padding: 18 }}>
          <LoadState lines={5} lineHeight={18} label="Loading decision stream…" />
        </div>
      ) : activity.length === 0 ? (
        <EmptyState
          icon={<BookOpen size={22} strokeWidth={2} />}
          title="No decisions in this window"
          hint="Scaling, anomaly, and policy events appear here as the decision plane acts."
        />
      ) : (
        <div style={{ display: "flex", flexDirection: "column" }}>
          {degraded ? (
            <div style={{ padding: "14px 16px 0" }}>
              <ErrorState
                title="Showing a representative stream"
                hint="The live decision stream couldn't be reached just now."
                onRetry={onRetry}
              />
            </div>
          ) : null}
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
      )}
    </Card>
  );
}
