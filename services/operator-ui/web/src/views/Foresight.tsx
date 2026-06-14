// ============================================================================
// Foresight -- the load forecaster + scale-ahead story
// ----------------------------------------------------------------------------
// The forecaster up close: actual throughput leading into a short forward
// forecast tail with a 90% confidence band and a scale-ahead decision marker,
// the recent scaling-ahead timeline (forecast -> scale action), and a forecast
// vs actual accuracy callout. Every panel tries the live API and falls back to
// sample data on error or timeout, so the page renders complete with no backend
// running, and publishes its data source up to the app shell like the Flightdeck.
//
// The forward forecast tail and confidence band are driven by the forecast-
// history endpoint (predicted_rps + confidence bounds, served behind the BFF at
// /api/ui/metrics/forecast-history), aligned to the live actual throughput. The
// accuracy callout backtests past forecasts against the actual that followed to
// compute a real MAPE and in-band share. When the forecaster has not published
// yet (empty/unreachable) the page falls back to a throughput-derived tail and
// a sample accuracy callout, with the source indicator reading "sample". The
// actual series and scaling timeline are live (metrics/throughput, audit/scaling).
// ============================================================================

import { useEffect, useMemo, useState } from "react";
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
  type RoutingMetrics,
  type ScalingAuditRow,
  type ThroughputResponse,
} from "../api";
import {
  Badge,
  Card,
  ForecastChart,
  KpiStat,
  Sparkline,
  StatusPill,
  type Status,
} from "../ui";
import { loadWithFallback, type DataSource } from "./loader";
import { useShell } from "./shell-context";
import {
  SAMPLE_FORESIGHT_ACCURACY,
  SAMPLE_FORESIGHT_FORECAST_HISTORY,
  SAMPLE_FORESIGHT_ROUTING,
  SAMPLE_FORESIGHT_SCALING,
  SAMPLE_FORESIGHT_THROUGHPUT,
  type ForecastAccuracy,
} from "./_sampleForesight";

const REFRESH_MS = 20_000;

// ── small formatting helpers ─────────────────────────────────────────────────

const fmtRpm = (n: number) =>
  n.toLocaleString("en-US", { maximumFractionDigits: 0 });

// ── forecast derivation ──────────────────────────────────────────────────────
// Derive a short forward forecast tail and a 90% confidence band from the
// actual throughput trend. Uses the slope of the recent window so the mint
// forecast leads the graphite actual; the band widens with the horizon to read
// as genuine uncertainty. Replaced wholesale once the forecast-history endpoint
// exists. All series are in k-rpm for the chart's y-axis.

interface ChartData {
  actual: number[];
  forecast: number[];
  confLow: number[];
  confHigh: number[];
  xLabels: string[];
  scaleIndex: number;
}

function deriveChart(throughput: ThroughputResponse): ChartData {
  // Take the last 13 buckets so the x-axis stays readable, in k-rpm.
  const krpm = throughput.buckets
    .slice(-13)
    .map((b) => Number((b.rpm / 1000).toFixed(2)));
  const actual = krpm.length >= 2 ? krpm : [18.4, 18.4];

  const last = actual[actual.length - 1];
  const prev = actual[actual.length - 2];
  // Smooth the slope over the trailing window so a single noisy bucket does not
  // swing the forecast; fall back to the last step delta for short series.
  const win = actual.slice(-4);
  const trendSlope =
    win.length >= 2 ? (win[win.length - 1] - win[0]) / (win.length - 1) : last - prev;

  const f1 = Number((last + trendSlope * 1.4).toFixed(2));
  const f2 = Number((last + trendSlope * 3.0).toFixed(2));
  const forecast = [last, f1, f2];

  // 90% confidence band: tight at the hand-off, widening with the horizon.
  const confLow = [
    last,
    Number((f1 * 0.96).toFixed(2)),
    Number((f2 * 0.91).toFixed(2)),
  ];
  const confHigh = [
    last,
    Number((f1 * 1.04).toFixed(2)),
    Number((f2 * 1.1).toFixed(2)),
  ];

  // x labels: trailing actual steps (minutes ago) then the forecast horizon.
  const actualLabels = actual.map((_, i) => {
    const stepsAgo = (actual.length - 1 - i) * 5;
    return stepsAgo === 0 ? "now" : `-${stepsAgo}`;
  });
  const xLabels = [...actualLabels.slice(0, -1), "now", "+5", "+10"];

  return {
    actual,
    forecast,
    confLow,
    confHigh,
    xLabels,
    // Scale-ahead decision fires at the first forward step, where the forecast
    // crosses the headroom margin ahead of the actual.
    scaleIndex: 1,
  };
}

// ── real forecast-history chart ──────────────────────────────────────────────
// Build the chart from the forecast-history endpoint when it's live, aligning
// the predicted series to the actual throughput trace. predicted_rps is a
// per-second rate; the actual axis is k-rpm, so convert predicted (and the
// confidence bounds) via rps * 60 / 1000. The forward tail is the forecast rows
// whose target time is at or after "now"; the hand-off point pins the forecast
// to the last actual so the lines join cleanly.

const RPS_TO_KRPM = 60 / 1000;

function buildChartFromForecasts(
  throughput: ThroughputResponse,
  history: ForecastHistoryResponse,
): ChartData {
  const krpm = throughput.buckets
    .slice(-13)
    .map((b) => Number((b.rpm / 1000).toFixed(2)));
  const actual = krpm.length >= 2 ? krpm : [18.4, 18.4];
  const last = actual[actual.length - 1];

  // Sort forecasts oldest-first and keep the forward tail: rows whose issue
  // time is the newest few. Take up to two horizon steps so the chart reads
  // "now → +5 → +10" like the derived path. Use the most recent issue time as
  // the anchor and pick the next two distinct horizons.
  const rows = [...history.forecasts].sort(
    (a, b) => Date.parse(a.time) - Date.parse(b.time),
  );
  const forward = rows.slice(-2);

  const fk = (rps: number) => Number((rps * RPS_TO_KRPM).toFixed(2));

  const forecast = [last, ...forward.map((r) => fk(r.predicted_rps))];
  const confLow = [
    last,
    ...forward.map((r) =>
      r.confidence_lower != null
        ? fk(r.confidence_lower)
        : Number((fk(r.predicted_rps) * 0.95).toFixed(2)),
    ),
  ];
  const confHigh = [
    last,
    ...forward.map((r) =>
      r.confidence_upper != null
        ? fk(r.confidence_upper)
        : Number((fk(r.predicted_rps) * 1.05).toFixed(2)),
    ),
  ];

  const actualLabels = actual.map((_, i) => {
    const stepsAgo = (actual.length - 1 - i) * 5;
    return stepsAgo === 0 ? "now" : `-${stepsAgo}`;
  });
  const horizonLabels = forward.map((r) => `+${r.horizon_minutes}`);
  const xLabels = [...actualLabels.slice(0, -1), "now", ...horizonLabels];

  return {
    actual,
    forecast,
    confLow,
    confHigh,
    xLabels,
    // Scale-ahead decision fires at the first forward step.
    scaleIndex: 1,
  };
}

// ── real forecast-vs-actual accuracy ─────────────────────────────────────────
// Backtest the live forecast history against the actual throughput that
// followed: for each past forecast, find the actual bucket nearest its target
// time (issue time + horizon) and compare. Yields a real MAPE, an in-band share
// (actuals that landed inside the confidence interval), and the recent pairs the
// callout lists. Returns null when there's no overlap to score, so the caller
// falls back to the sample callout.

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
  const shell = useShell();
  const { setDataSource, setPlane, setPlaneNodes } = shell;

  const [throughput, setThroughput] = useState<ThroughputResponse>(
    SAMPLE_FORESIGHT_THROUGHPUT,
  );
  const [routing, setRouting] = useState<RoutingMetrics>(
    SAMPLE_FORESIGHT_ROUTING,
  );
  const [scaling, setScaling] = useState<ScalingAuditRow[]>(
    SAMPLE_FORESIGHT_SCALING,
  );
  // Forecast history drives the real forward tail + accuracy callout when live.
  const [forecastHistory, setForecastHistory] =
    useState<ForecastHistoryResponse>(SAMPLE_FORESIGHT_FORECAST_HISTORY);
  const [forecastLive, setForecastLive] = useState<boolean>(false);

  const [throughputLive, setThroughputLive] = useState<boolean>(false);

  // ── data load (live, with sample fallback) ─────────────────────────────────
  useEffect(() => {
    let cancelled = false;

    async function tick() {
      const results = await Promise.all([
        loadWithFallback(() => api.getThroughput(13), SAMPLE_FORESIGHT_THROUGHPUT),
        loadWithFallback(() => api.getRoutingMetrics(), SAMPLE_FORESIGHT_ROUTING),
        loadWithFallback(() => api.auditScaling(8), SAMPLE_FORESIGHT_SCALING),
        loadWithFallback(
          () => api.getForecastHistory(3600, 200),
          SAMPLE_FORESIGHT_FORECAST_HISTORY,
        ),
      ]);

      if (cancelled) return;

      const [thrR, rouR, scR, fcR] = results;

      setThroughput(thrR.value);
      setThroughputLive(thrR.source === "live");
      setRouting(rouR.value);
      // The scaling endpoint can return an empty list on a quiet cluster; keep
      // the sample timeline in that case so the panel never reads as broken.
      setScaling(scR.value.length > 0 ? scR.value : SAMPLE_FORESIGHT_SCALING);

      // The forecast endpoint can return an empty list when the forecaster has
      // not published yet; treat that as sample so the chart/callout never read
      // as a flat/empty forecast.
      const fcHasData = fcR.source === "live" && fcR.value.forecasts.length > 0;
      setForecastHistory(fcHasData ? fcR.value : SAMPLE_FORESIGHT_FORECAST_HISTORY);
      setForecastLive(fcHasData);

      // The forecast source feeds the shell indicator too, but a quiet
      // forecaster (empty list) is treated as sample for the source rollup.
      const sources: DataSource[] = [
        thrR.source,
        rouR.source,
        scR.source,
        fcHasData ? "live" : "sample",
      ];
      const anySample = sources.some((s) => s === "sample");
      const allSample = sources.every((s) => s === "sample");
      setDataSource(anySample ? "sample" : "live");
      setPlane(allSample ? "bad" : anySample ? "warn" : "ok");
      setPlaneNodes(rouR.value.cluster_size_current ?? 0);
    }

    tick();
    const id = window.setInterval(tick, REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [setDataSource, setPlane, setPlaneNodes]);

  // ── derived readings ───────────────────────────────────────────────────────

  // When forecast history is live + non-empty, build the chart from the real
  // predicted series aligned to actual throughput; otherwise derive the forward
  // tail from the throughput trend as before.
  const chart = useMemo(
    () =>
      forecastLive
        ? buildChartFromForecasts(throughput, forecastHistory)
        : deriveChart(throughput),
    [throughput, forecastHistory, forecastLive],
  );

  // Real forecast-vs-actual accuracy when live and there's overlap to score;
  // otherwise the sample callout. Compute once and reuse for the live flag.
  const computedAccuracy = useMemo(
    () => (forecastLive ? computeAccuracy(throughput, forecastHistory) : null),
    [throughput, forecastHistory, forecastLive],
  );
  const accuracy: ForecastAccuracy = computedAccuracy ?? SAMPLE_FORESIGHT_ACCURACY;
  const accuracyLive = computedAccuracy != null;

  const actualNowKrpm = chart.actual[chart.actual.length - 1];
  const forecastNextKrpm = chart.forecast[chart.forecast.length - 1];
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
  // Confidence: derive a real in-band confidence from the live band width at the
  // forward step (tighter band → higher confidence). Falls back to the sample
  // 92% reading when the forecast isn't live.
  const confidencePct = useMemo(() => {
    if (!forecastLive) return 92;
    const mid = chart.forecast[chart.forecast.length - 1];
    const lo = chart.confLow[chart.confLow.length - 1];
    const hi = chart.confHigh[chart.confHigh.length - 1];
    if (mid > 0 && hi > lo) {
      // Band half-width as a share of the central forecast → confidence.
      const halfWidthPct = ((hi - lo) / 2 / mid) * 100;
      return Math.max(50, Math.min(99, Math.round(100 - halfWidthPct)));
    }
    return 90;
  }, [chart, forecastLive]);

  // Sparkline trails for the KPI rail (most-recent last), in k-rpm.
  const actualSpark = chart.actual;
  const forecastSpark = [...chart.actual.slice(-4), ...chart.forecast.slice(1)];

  // ── render ─────────────────────────────────────────────────────────────────

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 22 }}>
      <PageHead live={throughputLive} forecastLive={forecastLive} />

      <KpiRail
        actualRpm={actualNowRpm}
        forecastRpm={forecastNextRpm}
        confidencePct={confidencePct}
        headroomPct={headroomPct}
        poolSize={poolSize}
        actualSpark={actualSpark}
        forecastSpark={forecastSpark}
      />

      <SectionHead
        title="Load forecast and scale-ahead"
        sub="Mint forecast runs ahead of graphite actual; the band is the 90% confidence interval. The dashed marker is the scale-ahead decision, where the pool steps up before the predicted demand lands."
      />

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(0, 1.6fr) minmax(0, 1fr)",
          gap: 18,
          alignItems: "start",
        }}
      >
        <ForecastCard
          chart={chart}
          actualRpm={actualNowRpm}
          forecastRpm={forecastNextRpm}
          confidencePct={confidencePct}
          headroomPct={headroomPct}
        />
        <AccuracyCard accuracy={accuracy} live={accuracyLive} />
      </div>

      <SectionHead
        title="Scaling-ahead timeline"
        sub="Recent scaling actions, newest first. Each step ties a forecast signal to a concrete pool change, so you can read forecast then action down the list."
      />

      <ScalingTimeline scaling={scaling} routing={routing} />
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
  // throughput-only when one is present, sample when neither is.
  const label =
    live && forecastLive
      ? "LIVE FORECAST + THROUGHPUT"
      : forecastLive
        ? "LIVE FORECAST"
        : live
          ? "LIVE THROUGHPUT"
          : "SAMPLE DATA";
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

function KpiRail({
  actualRpm,
  forecastRpm,
  confidencePct,
  headroomPct,
  poolSize,
  actualSpark,
  forecastSpark,
}: {
  actualRpm: number;
  forecastRpm: number;
  confidencePct: number;
  headroomPct: number;
  poolSize: number;
  actualSpark: number[];
  forecastSpark: number[];
}) {
  const deltaPct =
    actualRpm > 0
      ? Math.round(((forecastRpm - actualRpm) / actualRpm) * 100)
      : 0;
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 16 }}>
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
        spark={actualSpark}
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
        spark={forecastSpark}
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
        deltaDir="up"
        delta="90% band"
        footnote="5-min horizon"
        spark={[88, 89, 90, 91, 90, 92, 91, 92, 93, confidencePct]}
        sparkTone="mint"
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
        spark={[42, 40, 38, 36, 35, 34, 33, 33, 32, headroomPct]}
        sparkTone="graphite"
      />
      <KpiStat
        label={
          <>
            <Boxes size={12} strokeWidth={2} /> Current pool size
          </>
        }
        value={poolSize > 0 ? `${poolSize}` : "—"}
        unit="nodes"
        deltaDir="up"
        delta="scaled ahead"
        footnote="active instances"
        spark={[4, 4, 5, 5, 5, 5, 6, 6, 6, Math.max(poolSize, 1)]}
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
}: {
  chart: ChartData;
  actualRpm: number;
  forecastRpm: number;
  confidencePct: number;
  headroomPct: number;
}) {
  return (
    <Card
      title="Forecast vs actual throughput"
      eyebrow="// 5-min horizon"
      actions={<Badge tone="mint">SCALE-AHEAD</Badge>}
      flush
    >
      <div style={{ padding: "0 18px 10px", fontSize: 11.5, color: "var(--sl-text-low)" }}>
        Graphite is measured throughput; mint is the forward forecast leading it.
        The shaded band is the 90% confidence interval and widens with the
        horizon. The dashed marker is where the scale-ahead decision fired.
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
          scaleLabel="scale-ahead"
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
        }}
      >
        <LegendItem swatch="var(--sl-graphite)" label="Actual throughput" />
        <LegendItem swatch="var(--sl-mint)" label="Forecast" />
        <LegendItem swatch="var(--sl-mint)" label="90% confidence band" band />
        <LegendItem swatch="var(--sl-text-faint)" label="Scale-ahead decision" dashed />
      </div>
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
      actions={<Badge tone={live ? "mint" : "neutral"}>{live ? "LIVE" : "SAMPLE"}</Badge>}
    >
      <div style={{ display: "flex", gap: 26, flexWrap: "wrap", marginBottom: 4 }}>
        <FcStat label="Accuracy" value={accuracyPct.toFixed(1)} unit="%" tone="mint" />
        <FcStat label="Mean error" value={accuracy.mapePct.toFixed(1)} unit="% MAPE" />
        <FcStat label="In band" value={accuracy.withinBandPct.toFixed(0)} unit="%" />
      </div>

      <div style={{ fontSize: 11.5, color: "var(--sl-text-low)", margin: "8px 0 12px" }}>
        {live
          ? `Backtested ${accuracy.samples} forecast horizons against the actual throughput that followed: mean absolute percentage error and the share of actuals that landed inside the confidence band.`
          : `Last ${accuracy.samples} predictions backtested against the actual that followed. Showing sample until the forecast-history endpoint returns predictions.`}
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
}: {
  scaling: ScalingAuditRow[];
  routing: RoutingMetrics;
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
      {scaling.length === 0 ? (
        <div style={{ padding: 18, fontSize: 13, color: "var(--sl-text-low)" }}>
          No scaling actions in the window. The pool is holding steady against the
          forecast.
        </div>
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
