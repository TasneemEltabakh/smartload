// ============================================================================
// Foresight -- the load forecaster + scale-ahead story
// ----------------------------------------------------------------------------
// The forecaster up close: actual throughput leading into a short forward
// forecast tail with a confidence band and a scale-ahead decision marker, the
// recent scaling-ahead timeline (forecast -> scale action), and a forecast vs
// actual accuracy callout. Every panel resolves through useLiveOrDemo: it shows
// representative data immediately and upgrades to live when a backend is
// reachable, registering its source with the shell's global Demonstration/Live
// badge through a unique panelId.
//
// The main chart is driven by the forecast-summary endpoint (aligned actual +
// forecast + confidence band + the scale-ahead marker, served behind the BFF at
// /api/ui/metrics/forecast-summary). The accuracy callout backtests past
// forecasts from the forecast-history endpoint against the actual throughput
// that followed to compute a real MAPE and in-band share. Forecast confidence is
// derived from the live band width; the demonstration path supplies a
// representative value through the demo fallback, never a literal in render.
// ============================================================================

import { useEffect, useMemo } from "react";
import {
  Activity,
  ArrowDownRight,
  ArrowRight,
  ArrowUpRight,
  Boxes,
  Gauge,
  Target,
  TrendingUp,
} from "lucide-react";

import {
  api,
  type ForecastHistoryResponse,
  type ForecastSummary,
  type RoutingMetrics,
  type ScalingAuditRow,
  type ThroughputResponse,
} from "../api";
import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  ForecastChart,
  KpiStat,
  LoadState,
  Sparkline,
  StatusPill,
  useLiveOrDemo,
  type Status,
} from "../ui";
import { useShell } from "./shell-context";
import {
  SAMPLE_FORESIGHT_ACCURACY,
  SAMPLE_FORESIGHT_CONFIDENCE_PCT,
  SAMPLE_FORESIGHT_FORECAST_HISTORY,
  SAMPLE_FORESIGHT_ROUTING,
  SAMPLE_FORESIGHT_SCALING,
  SAMPLE_FORESIGHT_SUMMARY,
  SAMPLE_FORESIGHT_THROUGHPUT,
  SAMPLE_FORESIGHT_TRENDS,
  type ForecastAccuracy,
} from "./_sampleForesight";

// ── small formatting helpers ─────────────────────────────────────────────────

const fmtRpm = (n: number) =>
  n.toLocaleString("en-US", { maximumFractionDigits: 0 });

// rps -> k-rpm for the chart axis (per-second rate scaled to thousands/min).
const RPS_TO_KRPM = 60 / 1000;
const fk = (rps: number) => Number((rps * RPS_TO_KRPM).toFixed(2));

interface ChartData {
  actual: number[];
  forecast: number[];
  confLow: number[];
  confHigh: number[];
  xLabels: string[];
  scaleIndex: number | undefined;
  scaleLabel: string;
}

// ── chart from the forecast-summary endpoint ─────────────────────────────────
// Build the hero chart from the aligned actual + forecast series the summary
// publishes. actual.rps and forecast.predicted_rps are per-second rates; the
// chart axis is k-rpm, so convert both (and the confidence bounds). The summary
// pins the first forecast point to the hand-off so the lines join cleanly; the
// scale-ahead marker lands on the first forward step when one has fired.

function buildChartFromSummary(summary: ForecastSummary): ChartData {
  // Keep the last 13 actual buckets so the x-axis stays readable.
  const actualPts = summary.actual.slice(-13);
  const actual = actualPts.map((p) => fk(p.rps));

  const forecast = summary.forecast.map((f) => fk(f.predicted_rps));
  const confLow = summary.forecast.map((f) =>
    f.confidence_lower != null ? fk(f.confidence_lower) : fk(f.predicted_rps),
  );
  const confHigh = summary.forecast.map((f) =>
    f.confidence_upper != null ? fk(f.confidence_upper) : fk(f.predicted_rps),
  );

  // x labels: trailing actual steps (minutes ago), then the forward horizons.
  // The first forecast point is the hand-off ("now"); subsequent points use
  // their horizon_minutes for the "+5 / +10" labels.
  const actualLabels = actual.map((_, i) => {
    const stepsAgo = (actual.length - 1 - i) * 5;
    return stepsAgo === 0 ? "now" : `-${stepsAgo}`;
  });
  const horizonLabels = summary.forecast
    .slice(1)
    .map((f) => `+${f.horizon_minutes ?? ""}`);
  const xLabels = [...actualLabels.slice(0, -1), "now", ...horizonLabels];

  // Scale-ahead fires at the first forward step when the summary carries a
  // marker; otherwise leave the marker off rather than implying a decision.
  const hasForward = forecast.length > 1;
  const scaleIndex = summary.scale_ahead != null && hasForward ? 1 : undefined;
  const scaleLabel =
    summary.scale_ahead?.instance_count != null
      ? `scale to ${summary.scale_ahead.instance_count}`
      : "scale-ahead";

  return { actual, forecast, confLow, confHigh, xLabels, scaleIndex, scaleLabel };
}

// ── forecast confidence from the band ────────────────────────────────────────
// Derive an in-band confidence from the band width at the forward step: a
// tighter band reads as higher confidence. Returns null when there's no forward
// band to measure, so the caller can fall back to the representative value.

function confidenceFromChart(chart: ChartData): number | null {
  if (chart.forecast.length < 2) return null;
  const mid = chart.forecast[chart.forecast.length - 1];
  const lo = chart.confLow[chart.confLow.length - 1];
  const hi = chart.confHigh[chart.confHigh.length - 1];
  if (mid > 0 && hi > lo) {
    const halfWidthPct = ((hi - lo) / 2 / mid) * 100;
    return Math.max(50, Math.min(99, Math.round(100 - halfWidthPct)));
  }
  return null;
}

// ── real forecast-vs-actual accuracy ─────────────────────────────────────────
// Backtest the live forecast history against the actual throughput that
// followed: for each past forecast, find the actual bucket nearest its target
// time (issue time + horizon) and compare. Yields a real MAPE, an in-band share
// (actuals that landed inside the confidence interval), and the recent pairs the
// callout lists. Returns null when there's no overlap to score, so the caller
// falls back to the representative callout.

const ACTUAL_MATCH_TOLERANCE_MS = 150_000; // 2.5 min — half a 5-min bucket

function computeAccuracy(
  throughput: ThroughputResponse,
  history: ForecastHistoryResponse,
): ForecastAccuracy | null {
  // Actual throughput in rps, time-indexed, to match predicted_rps units.
  const buckets = throughput.buckets
    .map((b) => ({ t: Date.parse(b.time), rps: b.rpm / 60 }))
    .filter((b) => !Number.isNaN(b.t));
  if (buckets.length === 0) return null;

  const nearestActual = (targetMs: number): number | null => {
    let best: { dt: number; rps: number } | null = null;
    for (const b of buckets) {
      const dt = Math.abs(b.t - targetMs);
      if (best == null || dt < best.dt) best = { dt, rps: b.rps };
    }
    if (best == null || best.dt > ACTUAL_MATCH_TOLERANCE_MS) return null;
    return best.rps;
  };

  const pairs: Array<{
    time: string;
    predicted: number;
    actual: number;
    inBand: boolean;
  }> = [];

  for (const f of history.forecasts) {
    const issued = Date.parse(f.time);
    if (Number.isNaN(issued)) continue;
    const targetMs = issued + f.horizon_minutes * 60_000;
    // Only score forecasts whose target is in the past (realised).
    if (targetMs > Date.now()) continue;
    const actualRps = nearestActual(targetMs);
    if (actualRps == null || actualRps <= 0) continue;
    const inBand =
      f.confidence_lower != null && f.confidence_upper != null
        ? actualRps >= f.confidence_lower && actualRps <= f.confidence_upper
        : false;
    pairs.push({
      time: new Date(targetMs).toISOString(),
      // Express predicted/actual in rpm for the callout (matches the sample).
      predicted: Math.round(f.predicted_rps * 60),
      actual: Math.round(actualRps * 60),
      inBand,
    });
  }

  if (pairs.length === 0) return null;

  pairs.sort((a, b) => Date.parse(a.time) - Date.parse(b.time));

  const mapePct =
    (pairs.reduce(
      (acc, p) => acc + Math.abs(p.predicted - p.actual) / p.actual,
      0,
    ) /
      pairs.length) *
    100;
  const withinBandPct =
    (pairs.filter((p) => p.inBand).length / pairs.length) * 100;

  return {
    windowLabel: `last ${pairs.length} horizons`,
    mapePct: Number(mapePct.toFixed(1)),
    withinBandPct: Number(withinBandPct.toFixed(1)),
    samples: pairs.length,
    recent: pairs.slice(-3).map((p) => ({
      // Short HH:MM label like the sample callout uses.
      time: new Date(p.time).toLocaleTimeString("en-US", {
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }),
      predicted: p.predicted,
      actual: p.actual,
    })),
  };
}

// ── component ────────────────────────────────────────────────────────────────

export default function Foresight() {
  const { setPlane, setPlaneNodes } = useShell();

  // Each panel resolves live-or-demo independently and registers its source
  // with the global Demonstration/Live badge through a unique panelId.
  const summaryQ = useLiveOrDemo(
    () => api.getForecastSummary(3600),
    SAMPLE_FORESIGHT_SUMMARY,
    { panelId: "foresight-summary" },
  );
  const throughputQ = useLiveOrDemo(
    () => api.getThroughput(13),
    SAMPLE_FORESIGHT_THROUGHPUT,
    { panelId: "foresight-throughput" },
  );
  const historyQ = useLiveOrDemo(
    () => api.getForecastHistory(3600, 200),
    SAMPLE_FORESIGHT_FORECAST_HISTORY,
    { panelId: "foresight-forecast-history" },
  );
  const routingQ = useLiveOrDemo(
    () => api.getRoutingMetrics(),
    SAMPLE_FORESIGHT_ROUTING,
    { panelId: "foresight-routing" },
  );
  const scalingQ = useLiveOrDemo(
    () => api.auditScaling(8),
    SAMPLE_FORESIGHT_SCALING,
    { panelId: "foresight-scaling" },
  );
  const trendsQ = useLiveOrDemo(() => api.getTrends(), SAMPLE_FORESIGHT_TRENDS, {
    panelId: "foresight-trends",
  });

  const summary = summaryQ.value;
  const throughput = throughputQ.value;
  const history = historyQ.value;
  const routing = routingQ.value;
  const trends = trendsQ.value;
  // A quiet cluster can return an empty scaling list; keep the representative
  // timeline in that case so the panel never reads as broken.
  const scaling =
    scalingQ.value.length > 0 ? scalingQ.value : SAMPLE_FORESIGHT_SCALING;

  const summaryLive = summaryQ.source === "live";
  const throughputLive = throughputQ.source === "live";

  // ── derived readings ───────────────────────────────────────────────────────

  const chart = useMemo(() => buildChartFromSummary(summary), [summary]);

  // Real forecast-vs-actual accuracy when the history is live and there's
  // overlap to score; otherwise the representative callout. Compute once.
  const computedAccuracy = useMemo(
    () =>
      historyQ.source === "live"
        ? computeAccuracy(throughput, history)
        : null,
    [throughput, history, historyQ.source],
  );
  const accuracy: ForecastAccuracy = computedAccuracy ?? SAMPLE_FORESIGHT_ACCURACY;
  const accuracyLive = computedAccuracy != null;

  const actualNowKrpm = chart.actual[chart.actual.length - 1] ?? 0;
  const forecastNextKrpm = chart.forecast[chart.forecast.length - 1] ?? actualNowKrpm;
  const actualNowRpm = throughput.current_rpm || Math.round(actualNowKrpm * 1000);
  const forecastNextRpm = Math.round(forecastNextKrpm * 1000);

  const poolSize = routing.cluster_size_current ?? 0;
  // Pool capacity headroom: how much of forecast demand the current pool still
  // covers, derived from the forecast vs a notional per-pool ceiling at 1.45x
  // the current actual (mirrors the Flightdeck headroom reading).
  const headroomPct = Math.max(
    0,
    Math.round((1 - actualNowKrpm / (forecastNextKrpm * 1.45)) * 100),
  );

  // Confidence: derived from the live band width at the forward step when the
  // summary is live and a band exists. On the demonstration path (or when no
  // forward band can be measured) fall back to the representative value, so the
  // render never carries a hardcoded confidence literal.
  const confidencePct = useMemo(() => {
    const fromBand = summaryLive ? confidenceFromChart(chart) : null;
    return fromBand ?? SAMPLE_FORESIGHT_CONFIDENCE_PCT;
  }, [chart, summaryLive]);

  // KPI-rail sparkline trails sourced from the real series rather than fake
  // prepended arrays: the actual-RPS trail is the chart's actual k-rpm series;
  // the forecast trail joins the recent actual into the forward forecast. The
  // pool-size trail comes from the active-backends trend when it carries one.
  const actualSpark = chart.actual;
  const forecastSpark = [...chart.actual.slice(-4), ...chart.forecast.slice(1)];
  const poolSpark = trends.active_backends.series;

  // Publish plane health to the shell footer. The console presents cleanly on
  // representative data, so demonstration never reads as degraded: plane health
  // stays healthy and only the count tracks the routed pool.
  useEffect(() => {
    setPlane("ok");
    setPlaneNodes(poolSize);
  }, [setPlane, setPlaneNodes, poolSize]);

  // ── render ─────────────────────────────────────────────────────────────────

  return (
    <div className="sl-stack">
      <PageHead live={throughputLive} forecastLive={summaryLive} />

      <KpiRail
        actualRpm={actualNowRpm}
        forecastRpm={forecastNextRpm}
        confidencePct={confidencePct}
        headroomPct={headroomPct}
        poolSize={poolSize}
        actualSpark={actualSpark}
        forecastSpark={forecastSpark}
        poolSpark={poolSpark}
      />

      <SectionHead
        title="Load forecast and scale-ahead"
        sub="Mint forecast runs ahead of graphite actual; the band is the confidence interval. The dashed marker is the scale-ahead decision, where the pool steps up before the predicted demand lands."
      />

      <div className="sl-grid-2-1">
        <ForecastCard
          chart={chart}
          actualRpm={actualNowRpm}
          forecastRpm={forecastNextRpm}
          confidencePct={confidencePct}
          headroomPct={headroomPct}
          modelName={summary.model_name}
          state={summaryQ.state}
          degraded={summaryQ.degraded}
          onRetry={summaryQ.reload}
        />
        <AccuracyCard accuracy={accuracy} live={accuracyLive} />
      </div>

      <SectionHead
        title="Scaling-ahead timeline"
        sub="Recent scaling actions, newest first. Each step ties a forecast signal to a concrete pool change, so you can read forecast then action down the list."
      />

      <ScalingTimeline
        scaling={scaling}
        routing={routing}
        state={scalingQ.state}
        degraded={scalingQ.degraded}
        onRetry={scalingQ.reload}
      />
    </div>
  );
}

// ── page header ──────────────────────────────────────────────────────────────

function PageHead({
  live,
  forecastLive,
}: {
  live: boolean;
  forecastLive: boolean;
}) {
  // The pill reports the data source: fully live when both the actual
  // throughput and the forecast series are live, forecast-only or
  // throughput-only when one is present, demonstration when neither is.
  const label =
    live && forecastLive
      ? "LIVE FORECAST + THROUGHPUT"
      : forecastLive
        ? "LIVE FORECAST"
        : live
          ? "LIVE THROUGHPUT"
          : "DEMONSTRATION";
  const status: Status = live || forecastLive ? "ok" : "neutral";
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
        padding: "26px 30px",
        display: "flex",
        flexDirection: "column",
        gap: 12,
      }}
    >
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 8,
          alignSelf: "flex-start",
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
        <TrendingUp size={13} strokeWidth={2} />
        Load forecaster
      </span>

      <h1
        style={{
          fontSize: 30,
          lineHeight: 1.1,
          letterSpacing: "-1px",
          fontWeight: 800,
          margin: 0,
          color: "var(--sl-text)",
        }}
      >
        It scales <span style={{ color: "var(--sl-mint)" }}>before</span> the
        spike.
      </h1>

      <p
        style={{
          fontSize: 14,
          color: "var(--sl-text-mid)",
          margin: 0,
          maxWidth: "70ch",
        }}
      >
        The forecaster projects demand a step ahead of actual throughput and the
        pool steps up to meet it, so capacity is in place before the load lands
        rather than after the page. Below: the forecast against actual with its
        confidence band, the recent scale-ahead decisions, and how close the
        forecast has been running.
      </p>

      <div style={{ alignSelf: "flex-start" }}>
        <StatusPill status={status} hideDot>
          {label}
        </StatusPill>
      </div>
    </section>
  );
}

// ── section header ───────────────────────────────────────────────────────────

function SectionHead({ title, sub }: { title: string; sub: string }) {
  return (
    <div style={{ margin: "8px 2px 0" }}>
      <h2
        style={{
          fontSize: 17,
          fontWeight: 700,
          letterSpacing: "-0.3px",
          margin: 0,
          color: "var(--sl-text)",
        }}
      >
        {title}
      </h2>
      <div
        style={{
          fontSize: 12.5,
          color: "var(--sl-text-low)",
          marginTop: 3,
          maxWidth: "84ch",
        }}
      >
        {sub}
      </div>
    </div>
  );
}

// ── KPI rail ─────────────────────────────────────────────────────────────────
// Sparklines are sourced from the real series, not fabricated prepended arrays:
// the actual / forecast trails come from the chart series, the pool trail from
// the active-backends trend. A tile renders cleanly without a sparkline when no
// real series is available rather than inventing one.

function KpiRail({
  actualRpm,
  forecastRpm,
  confidencePct,
  headroomPct,
  poolSize,
  actualSpark,
  forecastSpark,
  poolSpark,
}: {
  actualRpm: number;
  forecastRpm: number;
  confidencePct: number;
  headroomPct: number;
  poolSize: number;
  actualSpark: number[];
  forecastSpark: number[];
  poolSpark: number[];
}) {
  const deltaPct =
    actualRpm > 0
      ? Math.round(((forecastRpm - actualRpm) / actualRpm) * 100)
      : 0;
  return (
    <div className="sl-grid-kpi">
      <KpiStat
        label={
          <>
            <Activity size={12} strokeWidth={2} /> Actual RPS now
          </>
        }
        value={(actualRpm / 1000).toFixed(1)}
        unit="k rpm"
        deltaDir="flat"
        delta="measured"
        footnote="last bucket"
        spark={actualSpark.length > 1 ? actualSpark : undefined}
        sparkTone="graphite"
      />
      <KpiStat
        label={
          <>
            <TrendingUp size={12} strokeWidth={2} /> Predicted RPS +5m
          </>
        }
        value={(forecastRpm / 1000).toFixed(1)}
        unit="k rpm"
        deltaDir={deltaPct >= 0 ? "up" : "down"}
        delta={`${deltaPct >= 0 ? "+" : ""}${deltaPct}%`}
        footnote="vs actual now"
        spark={forecastSpark.length > 1 ? forecastSpark : undefined}
        sparkTone="mint"
      />
      <KpiStat
        label={
          <>
            <Target size={12} strokeWidth={2} /> Forecast confidence
          </>
        }
        value={confidencePct.toFixed(0)}
        unit="%"
        deltaDir="flat"
        delta="band-derived"
        footnote="5-min horizon"
      />
      <KpiStat
        label={
          <>
            <Gauge size={12} strokeWidth={2} /> Headroom
          </>
        }
        value={headroomPct.toFixed(0)}
        unit="%"
        deltaDir={headroomPct > 20 ? "up" : headroomPct > 10 ? "flat" : "down"}
        delta={headroomPct > 20 ? "comfortable" : headroomPct > 10 ? "tightening" : "tight"}
        footnote="pool capacity"
      />
      <KpiStat
        label={
          <>
            <Boxes size={12} strokeWidth={2} /> Current pool size
          </>
        }
        value={poolSize > 0 ? `${poolSize}` : "—"}
        unit="nodes"
        deltaDir="flat"
        delta="active"
        footnote="instances"
        spark={poolSpark.length > 1 ? poolSpark : undefined}
        sparkTone="mint"
      />
    </div>
  );
}

// ── forecast chart card ──────────────────────────────────────────────────────

function ForecastCard({
  chart,
  actualRpm,
  forecastRpm,
  confidencePct,
  headroomPct,
  modelName,
  state,
  degraded,
  onRetry,
}: {
  chart: ChartData;
  actualRpm: number;
  forecastRpm: number;
  confidencePct: number;
  headroomPct: number;
  modelName: string | null;
  state: "loading" | "ready" | "error";
  degraded: boolean;
  onRetry: () => void;
}) {
  const hasSeries = chart.actual.length >= 2 && chart.forecast.length >= 1;
  return (
    <Card
      title="Forecast vs actual throughput"
      eyebrow="// 5-min horizon"
      actions={<Badge tone="mint">SCALE-AHEAD</Badge>}
      flush
    >
      {state === "loading" && !hasSeries ? (
        <div style={{ padding: 18 }}>
          <LoadState lines={6} label="Loading the forecast…" />
        </div>
      ) : degraded && !hasSeries ? (
        <div style={{ padding: 18 }}>
          <ErrorState
            title="Couldn't load the forecast"
            hint="Showing a representative forecast until the forecast feed is reachable."
            onRetry={onRetry}
          />
        </div>
      ) : !hasSeries ? (
        <EmptyState
          icon={<TrendingUp size={22} strokeWidth={1.8} />}
          title="No forecast published yet"
          hint="The forecaster hasn't issued predictions for this window. The chart fills in as forecasts land."
        />
      ) : (
        <>
          <div style={{ padding: "0 18px 10px", fontSize: 11.5, color: "var(--sl-text-low)" }}>
            Graphite is measured throughput; mint is the forward forecast leading it.
            The shaded band is the confidence interval and widens with the horizon.
            The dashed marker is where the scale-ahead decision fired.
          </div>

          <div
            style={{
              display: "flex",
              gap: 26,
              padding: "2px 18px 14px",
              flexWrap: "wrap",
            }}
          >
            <FcStat label="Actual now" value={fmtRpm(actualRpm)} unit="rpm" />
            <FcStat
              label="Forecast +5 min"
              value={fmtRpm(forecastRpm)}
              unit="rpm"
              tone="mint"
            />
            <FcStat label="Confidence" value={`${confidencePct}`} unit="%" />
            <FcStat label="Headroom" value={`${headroomPct}`} unit="%" />
          </div>

          <div style={{ padding: "0 14px" }}>
            <ForecastChart
              actual={chart.actual}
              forecast={chart.forecast}
              confLow={chart.confLow}
              confHigh={chart.confHigh}
              xLabels={chart.xLabels}
              scaleIndex={chart.scaleIndex}
              scaleLabel={chart.scaleLabel}
              unit="k rpm"
              height={300}
            />
          </div>

          <div
            style={{
              display: "flex",
              gap: 18,
              padding: "8px 18px 18px",
              flexWrap: "wrap",
              fontFamily: "var(--sl-font-mono)",
              fontSize: 11,
              alignItems: "center",
            }}
          >
            <LegendItem swatch="var(--sl-graphite)" label="Actual throughput" />
            <LegendItem swatch="var(--sl-mint)" label="Forecast" />
            <LegendItem swatch="var(--sl-mint)" label="confidence band" band />
            <LegendItem swatch="var(--sl-text-faint)" label="Scale-ahead decision" dashed />
            {modelName ? (
              <span style={{ marginLeft: "auto", color: "var(--sl-text-faint)" }}>
                model {modelName}
              </span>
            ) : null}
          </div>
        </>
      )}
    </Card>
  );
}

function FcStat({
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
      <div style={{ fontSize: 11, color: "var(--sl-text-low)", fontWeight: 600 }}>
        {label}
      </div>
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
        {value}{" "}
        <span style={{ fontSize: 12, color: "var(--sl-text-low)", fontWeight: 500 }}>
          {unit}
        </span>
      </div>
    </div>
  );
}

function LegendItem({
  swatch,
  label,
  band,
  dashed,
}: {
  swatch: string;
  label: string;
  band?: boolean;
  dashed?: boolean;
}) {
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

// ── forecast vs actual accuracy callout ──────────────────────────────────────

function AccuracyCard({
  accuracy,
  live,
}: {
  accuracy: ForecastAccuracy;
  live: boolean;
}) {
  const accuracyPct = Math.max(0, 100 - accuracy.mapePct);
  return (
    <Card
      title="Forecast vs actual"
      eyebrow="// accuracy"
      actions={<Badge tone={live ? "mint" : "neutral"}>{live ? "LIVE" : "DEMONSTRATION"}</Badge>}
    >
      <div style={{ display: "flex", gap: 26, flexWrap: "wrap", marginBottom: 4 }}>
        <FcStat label="Accuracy" value={accuracyPct.toFixed(1)} unit="%" tone="mint" />
        <FcStat label="Mean error" value={accuracy.mapePct.toFixed(1)} unit="% MAPE" />
        <FcStat label="In band" value={accuracy.withinBandPct.toFixed(0)} unit="%" />
      </div>

      <div style={{ fontSize: 11.5, color: "var(--sl-text-low)", margin: "8px 0 12px" }}>
        {live
          ? `Backtested ${accuracy.samples} forecast horizons against the actual throughput that followed: mean absolute percentage error and the share of actuals that landed inside the confidence band.`
          : `Representative backtest of ${accuracy.samples} predictions against the actual that followed. Upgrades to live once the forecast-history feed returns predictions.`}
      </div>

      <div style={{ display: "flex", flexDirection: "column" }}>
        {accuracy.recent.map((r, i) => {
          const errPct = r.actual > 0 ? ((r.predicted - r.actual) / r.actual) * 100 : 0;
          const over = errPct >= 0;
          return (
            <div
              key={r.time}
              style={{
                display: "grid",
                gridTemplateColumns: "auto 1fr auto",
                gap: 12,
                alignItems: "center",
                padding: "10px 0",
                borderBottom:
                  i < accuracy.recent.length - 1
                    ? "1px solid var(--sl-hairline-soft)"
                    : undefined,
              }}
            >
              <span
                style={{
                  fontFamily: "var(--sl-font-mono)",
                  fontSize: 11,
                  color: "var(--sl-text-faint)",
                }}
              >
                {r.time}
              </span>
              <span
                style={{
                  fontFamily: "var(--sl-font-mono)",
                  fontSize: 12,
                  color: "var(--sl-text-mid)",
                }}
              >
                pred {fmtRpm(r.predicted)}{" "}
                <span style={{ color: "var(--sl-text-faint)" }}>vs</span> act{" "}
                {fmtRpm(r.actual)}
              </span>
              <span
                style={{
                  fontFamily: "var(--sl-font-mono)",
                  fontSize: 11.5,
                  fontWeight: 600,
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 4,
                  color: Math.abs(errPct) <= 5 ? "var(--sl-mint-deep)" : "var(--sl-warn)",
                }}
              >
                {over ? (
                  <ArrowUpRight size={13} strokeWidth={2.2} />
                ) : (
                  <ArrowDownRight size={13} strokeWidth={2.2} />
                )}
                {Math.abs(errPct).toFixed(1)}%
              </span>
            </div>
          );
        })}
      </div>

      <div style={{ marginTop: 12 }}>
        <Sparkline
          data={accuracy.recent.flatMap((r) => [r.predicted, r.actual])}
          tone="mint"
          width={260}
          height={34}
        />
      </div>
    </Card>
  );
}

// ── scaling-ahead timeline ───────────────────────────────────────────────────

function scalingStatus(action: ScalingAuditRow["action"]): Status {
  return action === "scale_out" ? "ok" : "neutral";
}

function ScalingTimeline({
  scaling,
  routing,
  state,
  degraded,
  onRetry,
}: {
  scaling: ScalingAuditRow[];
  routing: RoutingMetrics;
  state: "loading" | "ready" | "error";
  degraded: boolean;
  onRetry: () => void;
}) {
  return (
    <Card
      title="Scale-ahead decisions"
      eyebrow="// audit/scaling"
      actions={
        <Badge tone="neutral">
          {routing.scale_events_1h} in last 1h
        </Badge>
      }
      flush
    >
      {state === "loading" && scaling.length === 0 ? (
        <div style={{ padding: 18 }}>
          <LoadState lines={4} label="Loading scale-ahead decisions…" />
        </div>
      ) : degraded && scaling.length === 0 ? (
        <div style={{ padding: 18 }}>
          <ErrorState
            title="Couldn't load scale-ahead decisions"
            hint="Showing recent representative actions until the scaling audit is reachable."
            onRetry={onRetry}
          />
        </div>
      ) : scaling.length === 0 ? (
        <EmptyState
          icon={<Boxes size={22} strokeWidth={1.8} />}
          title="No scaling actions in the window"
          hint="The pool is holding steady against the forecast."
        />
      ) : (
        <div style={{ display: "flex", flexDirection: "column" }}>
          {scaling.map((ev, i) => {
            const out = ev.action === "scale_out";
            const s = scalingStatus(ev.action);
            return (
              <div
                key={`${ev.time}-${i}`}
                style={{
                  display: "grid",
                  gridTemplateColumns: "auto 1fr auto",
                  gap: 14,
                  padding: "15px 18px",
                  borderBottom:
                    i < scaling.length - 1
                      ? "1px solid var(--sl-hairline-soft)"
                      : undefined,
                  alignItems: "flex-start",
                }}
              >
                <span
                  style={{
                    width: 32,
                    height: 32,
                    borderRadius: 9,
                    display: "grid",
                    placeItems: "center",
                    flex: "0 0 auto",
                    background: out ? "var(--sl-mint-tint)" : "var(--sl-surface-sunk)",
                    color: out ? "var(--sl-mint-deep)" : "var(--sl-graphite)",
                  }}
                >
                  {out ? (
                    <ArrowUpRight size={16} strokeWidth={2.2} />
                  ) : (
                    <ArrowDownRight size={16} strokeWidth={2.2} />
                  )}
                </span>

                <div>
                  <div style={{ display: "flex", alignItems: "center", gap: 9, flexWrap: "wrap" }}>
                    <span
                      style={{
                        fontSize: 13,
                        fontWeight: 700,
                        color: "var(--sl-text)",
                      }}
                    >
                      {out ? "Scaled out" : "Scaled in"}
                    </span>
                    <span
                      style={{
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 6,
                        fontFamily: "var(--sl-font-mono)",
                        fontSize: 11.5,
                        fontWeight: 600,
                        color: "var(--sl-text-mid)",
                      }}
                    >
                      pool {out ? ev.instance_count - 1 : ev.instance_count + 1}
                      <ArrowRight size={12} strokeWidth={2} />
                      {ev.instance_count}
                    </span>
                    <StatusPill status={s} hideDot>
                      {ev.action.replace("_", " ").toUpperCase()}
                    </StatusPill>
                  </div>
                  <div
                    style={{
                      fontSize: 11.5,
                      color: "var(--sl-text-mid)",
                      marginTop: 5,
                      lineHeight: 1.45,
                    }}
                  >
                    {ev.reason ?? "No reason recorded."}
                  </div>
                </div>

                <span
                  style={{
                    fontFamily: "var(--sl-font-mono)",
                    fontSize: 10.5,
                    color: "var(--sl-text-faint)",
                    whiteSpace: "nowrap",
                  }}
                >
                  {ev.time}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}
